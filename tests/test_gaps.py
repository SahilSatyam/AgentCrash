"""Closes the doc 03 test gaps: SDK-format verifiers, per-run chain,
--scan-pii, card redaction, NoLostUpdate, check exits 2/3, patch rejection."""

from __future__ import annotations

import json

import pytest

from agentcrash import AgentCrash, WorkflowConfig, verify_sdk_chain
from agentcrash.cli import EXIT_CORRUPT, EXIT_OK, EXIT_USAGE, EXIT_VERDICT_FAIL, main
from agentcrash.ledger import Ledger
from agentcrash.policy import (
    PolicyRejectedError,
    apply_patch,
    load_patch_doc,
    validate_patch_data,
)
from agentcrash.tracer import REDACTED_CARD, TraceWriter, redact
from agentcrash.verifiers import FreshWriteOnly, NoLostUpdate, VerifierError

# -- helpers -------------------------------------------------------------


def _write_jsonl(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return path


def _check(tmp_path, *args):
    return main(["check", "--report-out", str(tmp_path / "v.json"), *args])


def _sdk_kv_run(*, fresh: bool, rival: bool):
    """A host agent writing to a versioned store through the SDK."""
    ledger = Ledger()
    ledger.kv_seed("order-1", {"status": "new"})
    ac = AgentCrash(WorkflowConfig(project="p", fault_injection=True))
    faults = {}
    if rival:
        faults["kv"] = {
            "kind": "concurrent_update",
            "occurrence": 1,
            "key": "order-1",
            "updates": {"note": "rival"},
        }
    ac.configure_faults(faults)
    with ac.run(name="update-order") as run, run.step("update") as step:
        fault = run.maybe_fault("kv")
        if fault:  # inject the rival writer before the agent reads
            ledger.kv_apply_concurrent(fault["key"], fault["updates"])
        seen = ledger.kv_read("order-1")
        step.record_tool("kv", inputs={"op": "read", "key": "order-1"}, outputs=seen)
        value = {**seen["value"], "status": "paid"} if fresh else {"status": "paid"}
        expected = seen["version"] if fresh else None
        result = ledger.kv_write("order-1", value, expected)
        step.record_tool(
            "kv",
            inputs={"op": "write", "key": "order-1", "expected_version": expected},
            outputs=result,
        )
    return ac.trace(run.run_id), ledger.snapshot(run.run_id)


# -- 1. fresh_write_only / no_lost_update on SDK traces ------------------


def test_fresh_write_only_judges_sdk_traces():
    stale_trace, _ = _sdk_kv_run(fresh=False, rival=False)
    fresh_trace, _ = _sdk_kv_run(fresh=True, rival=False)
    verifier = FreshWriteOnly(tools=["kv"])
    stale = verifier.check(stale_trace, {})
    assert stale.passed is False and "seq" in stale.evidence
    assert verifier.check(fresh_trace, {}).passed is True


def test_fresh_write_only_says_when_nothing_to_judge():
    trace, _ = _sdk_kv_run(fresh=False, rival=False)
    verdict = FreshWriteOnly().check(trace, {})  # default tool not in this trace
    assert verdict.passed is True and "vacuous" in verdict.evidence


def test_fresh_write_only_flags_store_conflict_on_sdk_trace():
    trace, _ = _sdk_kv_run(fresh=True, rival=False)
    tool_write = [e for e in trace if e["kind"] == "tool"][-1]
    tool_write["outputs"]["conflict"] = True  # store said: stale version
    assert FreshWriteOnly(tools=["kv"]).check(trace, {}).passed is False


def test_no_lost_update_judges_sdk_traces():
    lost_trace, lost_state = _sdk_kv_run(fresh=False, rival=True)
    kept_trace, kept_state = _sdk_kv_run(fresh=True, rival=True)
    lost = NoLostUpdate().check(lost_trace, lost_state)
    assert lost.passed is False and "order-1.note" in lost.evidence
    assert NoLostUpdate().check(kept_trace, kept_state).passed is True


def test_no_lost_update_envelope_format_and_edge_cases():
    rival = {"fault": "concurrent_update", "record": "r1", "updates": {"a": 1}}
    trace = [{"step": 3, "injector_event": rival}]
    kept = {"record_versions": [{"record": "r1", "value": {"a": 1, "b": 2}}]}
    lost = {"record_versions": [{"record": "r1", "value": {"a": 0}}]}
    assert NoLostUpdate().check(trace, kept).passed is True
    assert NoLostUpdate().check(trace, lost).passed is False
    assert NoLostUpdate().check([{"step": 1}], {}).passed is True  # no rival: vacuous
    with pytest.raises(VerifierError, match="record_versions"):
        NoLostUpdate().check(trace, {})  # rival but no snapshot: cannot judge
    with pytest.raises(VerifierError):
        NoLostUpdate().check(trace, "not-a-dict")  # type: ignore[arg-type]


def test_check_cli_fresh_write_only_with_named_tools(tmp_path):
    stale_trace, state = _sdk_kv_run(fresh=False, rival=True)
    trace = _write_jsonl(tmp_path / "run.jsonl", stale_trace)
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    code = _check(
        tmp_path,
        "--trace",
        str(trace),
        "--state",
        str(tmp_path / "state.json"),
        "--invariant",
        "fresh_write_only:kv",
        "--invariant",
        "no_lost_update",
    )
    assert code == EXIT_VERDICT_FAIL
    report = json.loads((tmp_path / "v.json").read_text(encoding="utf-8"))
    by_id = {row["id"]: row["passed"] for row in report["results"]}
    assert by_id == {"fresh_write_only:kv": False, "no_lost_update": False}


# -- 2. per-run hash chain -----------------------------------------------


def test_each_run_chain_verifies_on_its_own():
    ac = AgentCrash(WorkflowConfig(project="p"))
    run_ids = []
    for _ in range(3):
        with ac.run(name="wf") as run, run.step("s") as step:
            step.record_tool("t", inputs={"n": 1})
        run_ids.append(run.run_id)
    for run_id in run_ids:
        events = ac.trace(run_id)
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        assert verify_sdk_chain(events)["ok"] is True


def test_sdk_chain_survives_jsonl_roundtrip_and_detects_tampering(tmp_path):
    ac = AgentCrash(WorkflowConfig(project="p"))
    with ac.run(name="wf") as run, run.step("s") as step:
        step.record_tool("t", inputs={"n": 1}, outputs={"ok": True})
    path = _write_jsonl(tmp_path / "run.jsonl", ac.trace(run.run_id))
    loaded = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert verify_sdk_chain(loaded)["ok"] is True
    loaded[2]["outputs"]["ok"] = False
    broken = verify_sdk_chain(loaded)
    assert broken["ok"] is False and broken["reason"] == "prev_hash mismatch"
    assert broken["first_bad"] == loaded[2]["event_id"]
    assert verify_sdk_chain(loaded[:1] + loaded[2:])["reason"] == "seq gap"


def test_sdk_chain_refuses_mixed_runs():
    ac = AgentCrash(WorkflowConfig(project="p"))
    with ac.run(name="a") as first:
        pass
    with ac.run(name="b") as second:
        pass
    mixed = ac.trace(first.run_id) + ac.trace(second.run_id)
    assert "mixes 2 runs" in verify_sdk_chain(mixed)["reason"]


def test_check_verify_chain_flag_both_formats(tmp_path):
    ac = AgentCrash(WorkflowConfig(project="p"))
    with ac.run(name="wf") as run, run.step("s"):
        pass
    events = ac.trace(run.run_id)
    good = _write_jsonl(tmp_path / "good.jsonl", events)
    args = ["--invariant", "required_step:s", "--verify-chain"]
    assert _check(tmp_path, "--trace", str(good), *args) == EXIT_OK
    events[1]["status"] = "error"
    bad = _write_jsonl(tmp_path / "bad.jsonl", events)
    assert _check(tmp_path, "--trace", str(bad), *args) == EXIT_CORRUPT

    envelope = tmp_path / "env.jsonl"
    with TraceWriter("r1", "s", str(envelope)) as writer:
        writer.write(step=1, tool="kv", outcome="success")
    code = _check(
        tmp_path, "--trace", str(envelope), "--invariant", "no_lost_update", "--verify-chain"
    )
    assert code == EXIT_OK


# -- 3a. --scan-pii and card redaction -----------------------------------


def test_scan_pii_flags_raw_pii_and_passes_redacted(tmp_path, capsys):
    raw = _write_jsonl(
        tmp_path / "raw.jsonl",
        [{"step": "s", "note": "mail bob@acme.test card 4242 4242 4242 4242"}],
    )
    code = _check(tmp_path, "--trace", str(raw), "--invariant", "required_step:s", "--scan-pii")
    assert code == EXIT_VERDICT_FAIL
    captured = capsys.readouterr()
    assert "E_PII_LEAK" in captured.err
    assert " email bob@acme.test" in captured.out and " card " in captured.out

    ac = AgentCrash(WorkflowConfig(project="p"))
    with ac.run(name="wf") as run, run.step("s") as step:
        step.record_tool("t", inputs={"email": "bob@acme.test", "card": "4242 4242 4242 4242"})
    clean = _write_jsonl(tmp_path / "clean.jsonl", ac.trace(run.run_id))
    code = _check(tmp_path, "--trace", str(clean), "--invariant", "required_step:s", "--scan-pii")
    assert code == EXIT_OK


@pytest.mark.parametrize(
    "text",
    ["4242424242424242", "4242 4242 4242 4242", "4242-4242-4242-4242", "pay 378282246310005 now"],
)
def test_card_like_runs_are_redacted(text):
    out = redact({"note": text})["note"]
    assert REDACTED_CARD in out
    assert "4242 4242" not in out and "378282246310005" not in out


@pytest.mark.parametrize(
    "text",
    [
        "order 12345",
        "2026-11-14",
        "v1.2.3",
        "id 123456789012",
        "order 1234567890123456",
    ],
)
def test_short_digit_runs_are_left_alone(text):
    assert redact({"note": text})["note"] == text


def test_scan_pii_flags_raw_jwt_and_cloud_tokens(tmp_path, capsys):
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjbE_qGsWTODk_wFv_I"
    raw = _write_jsonl(tmp_path / "raw_jwt.jsonl", [{"step": "s", "auth": f"Bearer {jwt}"}])
    code = _check(tmp_path, "--trace", str(raw), "--invariant", "required_step:s", "--scan-pii")
    assert code == EXIT_VERDICT_FAIL
    out = capsys.readouterr().out
    assert "jwt" in out


# -- 3b. check exit codes 2 and 3 ----------------------------------------


def test_check_exit_2_usage_errors(tmp_path):
    trace = _write_jsonl(tmp_path / "t.jsonl", [{"step": "s"}])
    assert _check(tmp_path, "--trace", str(trace)) == EXIT_USAGE  # no --invariant
    assert _check(tmp_path, "--trace", str(trace), "--invariant", "nope") == EXIT_USAGE
    missing = str(tmp_path / "missing.jsonl")
    assert _check(tmp_path, "--trace", missing, "--invariant", "required_step:s") == EXIT_USAGE
    plugin = ["--plugin", "no_such_module_xyz", "--invariant", "required_step:s"]
    assert _check(tmp_path, "--trace", str(trace), *plugin) == EXIT_USAGE
    state = ["--state", missing, "--invariant", "required_step:s"]
    assert _check(tmp_path, "--trace", str(trace), *state) == EXIT_USAGE


def test_check_exit_3_corrupt_inputs(tmp_path, capsys):
    bad_json = tmp_path / "bad.jsonl"
    bad_json.write_text('{"step": "s"}\n{not json', encoding="utf-8")
    args = ["--invariant", "required_step:s", "--format", "json"]
    assert _check(tmp_path, "--trace", str(bad_json), *args) == EXIT_CORRUPT
    error = json.loads(capsys.readouterr().out)
    assert error["code"] == "E_TRACE_CORRUPT" and error["exit"] == EXIT_CORRUPT

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    assert _check(tmp_path, "--trace", str(empty), "--invariant", "required_step:s") == 3

    good = _write_jsonl(tmp_path / "t.jsonl", [{"step": "s"}])
    bad_state = tmp_path / "state.json"
    bad_state.write_text("[1, 2]", encoding="utf-8")
    code = _check(
        tmp_path, "--trace", str(good), "--state", str(bad_state), "--invariant", "no_lost_update"
    )
    assert code == EXIT_CORRUPT


# -- 3c. patch rejection -------------------------------------------------

BASE = {
    "schema_version": "policy/v1",
    "rules": [{"rule": "on_tool_timeout", "tool": "payments.charge", "action": "retry_same"}],
}


def _patch(rules, **extra):
    doc = {
        "schema_version": "policy_patch/v1",
        "source_run_id": "run_1",
        "rationale": "reconcile before retry",
        "patch": {"rules": rules},
    }
    doc.update(extra)
    return doc


def test_apply_patch_merges_and_normalizes_shorthand():
    rules = [
        {
            "rule": "on_tool_timeout",
            "tool": "payments.charge",
            "action": "check_transaction_status",
            "max_retries": 1,
            "retry_with_idempotency_key": True,
        }
    ]
    merged, text = apply_patch(BASE, _patch(rules))
    rule = merged["rules"][0]
    assert rule["action"] == "check_then_act"
    assert rule["retry"] == {"max_attempts": 1, "require_idempotency_key": True}
    assert "policy/v1" in text


@pytest.mark.parametrize(
    "doc, match",
    [
        ([], "must contain a JSON object"),
        ({**_patch([]), "schema_version": "policy_patch/v2"}, "policy_patch/v1"),
        ({k: v for k, v in _patch([]).items() if k != "rationale"}, "violates"),
        ({**_patch([]), "patch": "drop table"}, "violates"),
    ],
)
def test_malformed_patch_documents_are_rejected(doc, match):
    with pytest.raises(PolicyRejectedError, match=match):
        validate_patch_data(doc)


@pytest.mark.parametrize(
    "rules, match",
    [
        ([{"rule": "on_tool_timeout", "tool": "t", "action": "time_travel"}], "violates"),
        ([{"rule": "on_tool_timeout", "tool": "t", "action": "fail", "max_retries": 9}], "0-3"),
        ([{"rule": "on_tool_timeout", "tool": "t"}], "violates"),
        (["not-an-object"], "must be an object"),
    ],
)
def test_patches_that_break_the_policy_schema_are_rejected(rules, match):
    with pytest.raises(PolicyRejectedError, match=match):
        apply_patch(BASE, _patch(rules))


def test_patch_file_errors_are_rejections(tmp_path):
    with pytest.raises(PolicyRejectedError, match="cannot read"):
        load_patch_doc(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(PolicyRejectedError, match="not valid JSON"):
        load_patch_doc(bad)


def test_check_reports_are_byte_identical_across_runs(tmp_path):
    trace, state = _sdk_kv_run(fresh=False, rival=True)
    path = _write_jsonl(tmp_path / "run.jsonl", trace)
    (tmp_path / "s.json").write_text(json.dumps(state), encoding="utf-8")
    reports = []
    for n in range(3):
        out = tmp_path / f"r{n}.json"
        main(
            [
                "check",
                "--trace",
                str(path),
                "--state",
                str(tmp_path / "s.json"),
                "--invariant",
                "fresh_write_only:kv",
                "--invariant",
                "no_lost_update",
                "--invariant",
                "duplicate_mutating_effect",
                "--report-out",
                str(out),
            ]
        )
        reports.append(out.read_bytes())
    assert reports[0] == reports[1] == reports[2]
