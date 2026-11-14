"""The core stays domain-free: no host vocabulary, no baked-in tool targets."""

import re
from pathlib import Path

import pytest

from agentcrash.injector import FAULT_CATALOG, UnknownFaultError, validate_fault_entry
from agentcrash.policy import fetch_live_patch
from agentcrash.verifiers import FreshWriteOnly

SRC = Path(__file__).resolve().parents[1] / "src" / "agentcrash"
BANNED = re.compile(r"odrl|shacl|dspy|rdflib|nl[-_ ]?to[-_ ]?odrl", re.IGNORECASE)


def test_core_source_has_no_domain_vocabulary():
    hits = []
    for path in SRC.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".json", ".html", ".js", ".css"}:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if BANNED.search(line):
                    hits.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert not hits, "\n".join(hits)


def test_fault_catalog_targets_no_default_tool():
    for entry in FAULT_CATALOG:
        assert "tool" not in entry["default_params"], entry["kind"]


def test_fault_entry_targets_user_named_tool():
    fault = validate_fault_entry("timeout_after_commit", {"tool": "payments.charge"})
    assert fault["params"]["tool"] == "payments.charge"
    assert fault["params"]["occurrence"] == 1


def test_concurrent_update_requires_key_and_updates():
    with pytest.raises(UnknownFaultError, match="missing required params: key, updates"):
        validate_fault_entry("concurrent_update", {"tool": "kv"})
    ok = validate_fault_entry("concurrent_update", {"key": "order-1", "updates": {"s": "x"}})
    assert ok["params"]["key"] == "order-1"


def _write(step, tool, expected, conflict):
    return {
        "step": step,
        "tool": tool,
        "outcome": "success",
        "args": {"op": "write"},
        "detail": {
            "phase": "tool_result",
            "data": {"expected_version": expected, "version_conflict": conflict},
        },
    }


def test_fresh_write_only_accepts_user_named_store_tools():
    trace = [_write(1, "orders_db", 1, False), _write(2, "orders_db", None, False)]
    assert FreshWriteOnly().check(trace, {}).passed is True  # default tool not in trace
    verdict = FreshWriteOnly(tools=["orders_db"]).check(trace, {})
    assert verdict.passed is False and "[2]" in verdict.evidence


def test_live_patch_is_opt_in(monkeypatch):
    for name in ("AGENTCRASH_LLM_BASE_URL", "LLAMA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    out = fetch_live_patch(trace_summary="t", verdict_summary="v", base_policy={}, model="m")
    assert out is None


def test_check_cli_knows_builtin_invariants(tmp_path, capsys):
    import json

    from agentcrash import AgentCrash, WorkflowConfig
    from agentcrash.cli import EXIT_VERDICT_FAIL, main

    ac = AgentCrash(WorkflowConfig(project="p", fault_injection=True))
    ac.configure_faults({"pay": {"kind": "timeout_after_commit", "occurrence": 1}})
    with ac.run(name="checkout") as run, run.step("charge") as step:
        step.record_tool("pay", inputs={"order": "o-1"}, outputs={"ok": True})
        if run.maybe_fault("pay"):
            step.record_tool("pay", inputs={"order": "o-1"}, outputs={"ok": True})
    trace = tmp_path / "run.jsonl"
    trace.write_text("\n".join(json.dumps(e) for e in ac.trace(run.run_id)), encoding="utf-8")
    code = main(
        [
            "check",
            "--trace",
            str(trace),
            "--invariant",
            "duplicate_mutating_effect",
            "--invariant",
            "required_step:charge",
            "--report-out",
            str(tmp_path / "v.json"),
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_VERDICT_FAIL
    assert "[FAIL] Invariant duplicate_mutating_effect" in out
    assert "[PASS] Invariant required_step:charge" in out


def test_tracer_never_redacts_its_own_hash_fields(tmp_path):
    from agentcrash.tracer import TraceWriter, verify_chain

    path = tmp_path / "t.jsonl"
    with TraceWriter("r1", "s", str(path)) as writer:
        writer._prev_hash = "1234567890123456" + "a" * 48  # card-like digit run
        event = writer.write(step=1, args={"email": "a@b.co"})
    assert event["prev_hash"].startswith("1234567890123456")
    assert event["args"]["email"] == "redacted@example.com"
    with TraceWriter("r2", "s", str(path)) as writer:
        for i in range(200):  # many real hashes: chain must always verify
            writer.write(step=i, args={"n": i})
    assert verify_chain(str(path))["ok"] is True


def test_sdk_chain_is_consistent_under_threads():
    import hashlib
    import threading

    from agentcrash import AgentCrash, WorkflowConfig
    from agentcrash.sdk import _HASH_ENCODER

    ac = AgentCrash(WorkflowConfig(project="p", max_queue=0))
    with ac.run(name="wf") as run:

        def work(n):
            with run.step(f"s{n}") as step:
                for i in range(50):
                    step.record_tool("t", inputs={"i": i})

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    events = sorted(ac.trace(run.run_id), key=lambda e: e["seq"])
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    prev = "GENESIS"
    for event in events:
        body = {k: v for k, v in event.items() if k != "prev_hash"}
        prev = hashlib.sha256(f"{prev}:{_HASH_ENCODER.encode(body)}".encode()).hexdigest()
        assert event["prev_hash"] == prev
    assert ac.exporter.dropped == 0 and len(ac.exporter.drain()) == len(events)


def test_sdk_accepts_non_json_tool_outputs():
    import datetime

    from agentcrash import AgentCrash, WorkflowConfig

    class Obj:
        def __str__(self):
            return "obj"

    ac = AgentCrash(WorkflowConfig(project="p"))
    with ac.run(name="wf") as run, run.step("s") as step:
        step.record_tool("t", outputs={"when": datetime.date(2026, 1, 1), "o": Obj()})
    assert any(e["kind"] == "tool" for e in ac.trace(run.run_id))
