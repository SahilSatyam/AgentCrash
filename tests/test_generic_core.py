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
