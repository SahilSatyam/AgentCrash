"""Tests for Phase 3: HMAC signing, OTel spans, PostgresStorage, and schedule fuzz/shrink."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentcrash import (
    AgentCrash,
    PostgresStorage,
    Storage,
    WorkflowConfig,
    compare_reports,
    evaluate_trace,
    export_otel_spans,
    fuzz_and_shrink,
    fuzz_schedules,
    get_storage,
    set_storage,
    shrink_schedule,
    to_otel_spans,
    verify_sdk_chain,
)
from agentcrash.cli import EXIT_CORRUPT, EXIT_OK, EXIT_VERDICT_FAIL, main
from agentcrash.invariants import DuplicateMutatingEffect
from agentcrash.tracer import TraceWriter, verify_chain


# -----------------------------------------------------------------------------
# Item 6: Tamper-Evident Chains (HMAC-SHA256 Signing)
# -----------------------------------------------------------------------------
def test_sdk_hmac_signing_and_verification(tmp_path: Path) -> None:
    key = "super-secret-hmac-key"
    ac = AgentCrash(WorkflowConfig(project="sec_test", signing_key=key))

    with ac.run("payment_flow") as run, run.step("charge") as step:
        step.record_tool("stripe", inputs={"amount": 100}, outputs={"status": "paid"})

    events = ac.trace(run.run_id)
    assert len(events) >= 3  # run_start, step_start, tool, step_end, run_end
    # All events must have 'sig'
    for ev in events:
        assert "sig" in ev
        assert isinstance(ev["sig"], str)
        assert len(ev["sig"]) == 64  # SHA256 hex digest

    # Verify with correct key
    check_ok = verify_sdk_chain(events, signing_key=key)
    assert check_ok["ok"] is True

    # Verify without key (pass-through mode)
    check_no_key = verify_sdk_chain(events, signing_key=None)
    assert check_no_key["ok"] is True

    # Verify with wrong key
    check_wrong_key = verify_sdk_chain(events, signing_key="wrong-key")
    assert check_wrong_key["ok"] is False
    assert check_wrong_key["reason"] == "signature mismatch"

    # Tampered signature
    tampered_sig = [dict(e) for e in events]
    tampered_sig[1]["sig"] = "0" * 64
    check_tampered = verify_sdk_chain(tampered_sig, signing_key=key)
    assert check_tampered["ok"] is False
    assert check_tampered["reason"] == "signature mismatch"

    # Unsigned events verified with required key
    ac_unsigned = AgentCrash(WorkflowConfig(project="unsigned"))
    with ac_unsigned.run("unsigned_flow") as run_u:
        run_u.step("step1")
    unsigned_events = ac_unsigned.trace(run_u.run_id)
    check_unsigned = verify_sdk_chain(unsigned_events, signing_key=key)
    assert check_unsigned["ok"] is False
    assert check_unsigned["reason"] == "signature mismatch"


def test_tracewriter_hmac_signing_and_verify_chain(tmp_path: Path) -> None:
    trace_path = tmp_path / "signed_trace.jsonl"
    key = "tracer-secret-key"

    with TraceWriter("run_tw", "checkout", str(trace_path), signing_key=key) as tw:
        tw.write(step=0, tool="auth", outcome="success")
        tw.write(step=1, tool="pay", outcome="success")

    # Read lines to check sig
    lines = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    for l in lines:
        assert "sig" in l
        assert len(l["sig"]) == 64

    # Verify chain with correct key
    assert verify_chain(str(trace_path), signing_key=key)["ok"] is True

    # Verify chain with wrong key
    bad = verify_chain(str(trace_path), signing_key="bad-key")
    assert bad["ok"] is False
    assert bad["reason"] == "signature mismatch"


def test_cli_check_verify_chain_with_signing_key(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    key = "cli-key-123"
    trace_path = tmp_path / "sdk_signed.jsonl"
    ac = AgentCrash(WorkflowConfig(project="cli_p", signing_key=key))
    with ac.run("cli_run") as run, run.step("s1") as s:
        s.record_tool("t1", inputs={"a": 1}, outputs={"b": 2})

    events = ac.trace(run.run_id)
    trace_path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    # CLI check with valid signing key
    exit_code = main([
        "check",
        "--trace", str(trace_path),
        "--invariant", "required_step:s1",
        "--signing-key", key,
    ])
    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "chain: ok" in out

    # CLI check with invalid signing key -> exits EXIT_CORRUPT
    exit_code_bad = main([
        "check",
        "--trace", str(trace_path),
        "--invariant", "required_step:s1",
        "--signing-key", "wrong-cli-key",
    ])
    assert exit_code_bad == EXIT_CORRUPT


# -----------------------------------------------------------------------------
# Item 5: OpenTelemetry Spans & Export
# -----------------------------------------------------------------------------
def test_to_otel_spans_mapping() -> None:
    events = [
        {
            "event_id": "evt_r1_0001",
            "run_id": "run_12345",
            "project": "demo_project",
            "workflow": "checkout",
            "kind": "run_start",
            "seq": 1,
            "status": "ok",
        },
        {
            "event_id": "evt_r1_0002",
            "run_id": "run_12345",
            "project": "demo_project",
            "workflow": "checkout",
            "step": "step_auth",
            "kind": "step_start",
            "seq": 2,
            "status": "ok",
        },
        {
            "event_id": "evt_r1_0003",
            "run_id": "run_12345",
            "project": "demo_project",
            "workflow": "checkout",
            "step": "step_auth",
            "tool": "verify_token",
            "kind": "tool",
            "seq": 3,
            "status": "ok",
            "ts_virtual": 150,
        },
        {
            "event_id": "evt_r1_0004",
            "run_id": "run_12345",
            "project": "demo_project",
            "workflow": "checkout",
            "step": "step_auth",
            "tool": "verify_token",
            "kind": "fault",
            "fault": {"kind": "timeout_after_commit"},
            "seq": 4,
            "status": "error",
            "error": "TimeoutError",
            "ts_virtual": 200,
        },
    ]

    spans = to_otel_spans(events)
    assert len(spans) == 4

    # Check trace_id consistency and W3C traceparent formatting
    trace_id = spans[0]["trace_id"]
    assert len(trace_id) == 32
    for s in spans:
        assert s["trace_id"] == trace_id
        assert len(s["span_id"]) == 16
        assert s["traceparent"] == f"00-{trace_id}-{s['span_id']}-01"
        assert s["attributes"]["agentcrash.run_id"] == "run_12345"
        assert s["attributes"]["agentcrash.project"] == "demo_project"
        assert s["attributes"]["gen_ai.system"] == "agentcrash"

    # Step parented to run; tool parented to step
    run_span = spans[0]
    step_span = spans[1]
    tool_span = spans[2]
    fault_span = spans[3]

    assert run_span["parent_span_id"] is None
    assert step_span["parent_span_id"] is not None
    assert tool_span["parent_span_id"] == step_span["span_id"]
    assert tool_span["attributes"]["agentcrash.tool"] == "verify_token"
    assert tool_span["attributes"]["agentcrash.ts_virtual"] == 150

    # Fault status error
    assert fault_span["status"]["code"] == "ERROR"
    assert fault_span["status"]["description"] == "TimeoutError"
    assert fault_span["attributes"]["agentcrash.fault_kind"] == "timeout_after_commit"


def test_export_otel_spans_with_mock_tracer() -> None:
    events = [
        {"run_id": "r1", "seq": 1, "kind": "run_start", "workflow": "wf"},
        {"run_id": "r1", "seq": 2, "kind": "tool", "tool": "calc", "status": "ok"},
    ]

    mock_span = MagicMock()
    mock_tracer = MagicMock()
    mock_tracer.start_span.return_value = mock_span

    count = export_otel_spans(events, tracer=mock_tracer)
    assert count == 2
    assert mock_tracer.start_span.call_count == 2
    assert mock_span.end.call_count == 2


def test_export_otel_spans_missing_dep_raises_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # If opentelemetry is not present and tracer is None, informative error is raised
    monkeypatch.setattr("builtins.__import__", lambda name, *args, **kwargs: (_ for _ in ()).throw(ImportError("No module named 'opentelemetry'")) if "opentelemetry" in name else __import__(name, *args, **kwargs))
    with pytest.raises(ImportError, match="OpenTelemetry is not installed"):
        export_otel_spans([{"run_id": "r1", "seq": 1, "kind": "run_start"}], tracer=None)


# -----------------------------------------------------------------------------
# Item 7: PostgreSQL Adapter
# -----------------------------------------------------------------------------
def test_postgres_storage_conforms_to_protocol() -> None:
    # PostgresStorage class fulfills Storage protocol
    assert issubclass(PostgresStorage, Storage)


def test_postgres_storage_missing_driver_raises_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulating driver missing
    import builtins
    orig_import = builtins.__import__

    def mock_import(name: str, *args: object, **kwargs: object) -> object:
        if name in ("psycopg", "psycopg2"):
            raise ImportError(f"No module named '{name}'")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    with pytest.raises(ImportError, match="PostgresStorage requires 'psycopg' or 'psycopg2'"):
        PostgresStorage("postgresql://localhost:5432/test")


def test_postgres_storage_operations_with_mock_connection() -> None:
    mock_cur = MagicMock()
    mock_cur.__enter__.return_value = mock_cur
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    # Setup return values for mock cursor
    mock_cur.fetchone.side_effect = [
        ("my_proj", "production", 0),  # register_project
        None,                          # ingest_events (SELECT 1 FROM events)
        ("my_proj", "my_wf"),          # run_detail run_row
        (1,),                          # list_runs ev_count
        (0,),                          # prune_runs kept count
    ]
    mock_cur.fetchall.side_effect = [
        [("inv1", 1, "evidence ok")],                               # submit_evaluation
        [("inv1", 1, "evidence ok")],                               # evaluation_summary
        [(json.dumps({"seq": 1, "event_id": "e1"}),)],              # run_detail ev_rows
        [("inv1", 1, "evidence ok")],                               # run_detail eval_rows
        [("run_1", "my_proj", "my_wf", 1000.0)],                   # list_runs runs
        [(1,)],                                                     # list_runs eval_rows
        [("run_1", 500.0)],                                         # prune_runs rows
        [("run_1",)],                                               # reliability_summary runs
        [(json.dumps({"seq": 1, "fault": {"kind": "timeout"}}),)],  # reliability_summary events
    ]

    storage = PostgresStorage(conn=mock_conn)
    assert isinstance(storage, Storage)

    # register_project
    reg = storage.register_project("my_proj", "production")
    assert reg["project"] == "my_proj"

    # ingest_events
    ingest = storage.ingest_events([
        {
            "schema_version": 1,
            "event_id": "e1",
            "run_id": "r1",
            "project": "my_proj",
            "workflow": "wf",
            "step": "s1",
            "kind": "tool",
            "status": "ok",
            "seq": 1,
        }
    ])
    assert ingest["accepted"] == 1

    # submit_evaluation
    sub = storage.submit_evaluation("r1", [{"id": "inv1", "passed": True, "evidence": "evidence ok"}])
    assert sub["verdict"] == "PASS"

    # evaluation_summary
    summ = storage.evaluation_summary("r1")
    assert summ is not None and summ["verdict"] == "PASS"

    # run_detail
    detail = storage.run_detail("r1")
    assert detail is not None
    assert detail["project"] == "my_proj"

    # list_runs
    runs = storage.list_runs("my_proj")
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run_1"

    # prune_runs
    pruned = storage.prune_runs("my_proj", keep_last_n=1)
    assert "dropped" in pruned

    # reliability_summary
    rel = storage.reliability_summary("my_proj")
    assert rel["runs"] == 1
    assert rel["fault_runs"] == 1

    # reset and close
    storage.reset()
    storage.close()
    assert mock_conn.close.called


def test_get_storage_selects_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = MagicMock()

    monkeypatch.setenv("AGENTCRASH_STORAGE", "postgres")
    monkeypatch.setattr(
        "agentcrash.server.PostgresStorage",
        lambda *args, **kwargs: PostgresStorage(conn=mock_conn),
    )
    set_storage(None)
    st = get_storage()
    assert isinstance(st, PostgresStorage)
    set_storage(None)


# -----------------------------------------------------------------------------
# Item 8: Schedule Fuzzer & Shrinker
# -----------------------------------------------------------------------------
def test_fuzz_schedules_deterministic_and_respects_budget() -> None:
    boundaries = ["auth", "payment", "inventory"]
    schedules_1 = fuzz_schedules(boundaries, seed=42, max_cases=10)
    schedules_2 = fuzz_schedules(boundaries, seed=42, max_cases=10)

    assert len(schedules_1) == 10
    assert schedules_1 == schedules_2  # deterministic seeds produce identical schedules

    # Every schedule is a dict of boundary -> fault descriptor
    for s in schedules_1:
        assert isinstance(s, dict)
        for b, fault in s.items():
            assert b in boundaries
            assert fault["kind"] in ("timeout_after_commit", "concurrent_update", "poisoned_doc")


def test_shrink_schedule_delta_debugging() -> None:
    # Complex schedule with 3 faults, but only 'payment' failure matters
    initial_schedule = {
        "auth": {"kind": "poisoned_doc"},
        "payment": {"kind": "timeout_after_commit", "occurrence": 1},
        "inventory": {"kind": "concurrent_update"},
    }

    def test_fn(candidate: dict) -> bool:
        # Fails only if 'payment' has timeout_after_commit
        return "payment" in candidate and candidate["payment"]["kind"] == "timeout_after_commit"

    shrunk = shrink_schedule(initial_schedule, test_fn)
    assert shrunk == {"payment": {"kind": "timeout_after_commit", "occurrence": 1}}


def test_fuzz_and_shrink_with_invariants() -> None:
    # A runner that simulates a 2-step workflow: auth and charge.
    # If 'charge' has timeout_after_commit, DuplicateMutatingEffect triggers.
    def runner_fn(sched: dict) -> tuple[list[dict], dict]:
        events = [
            {"seq": 1, "run_id": "r1", "kind": "run_start"},
            {"seq": 2, "run_id": "r1", "kind": "tool", "tool": "auth", "status": "ok"},
            {"seq": 3, "run_id": "r1", "kind": "tool", "tool": "charge", "inputs": {"id": "ord1"}, "outputs": {"ok": True}, "status": "ok"},
        ]
        # In case of timeout_after_commit on charge, a retry duplicate occurs
        if "charge" in sched and sched["charge"]["kind"] == "timeout_after_commit":
            events.append(
                {"seq": 4, "run_id": "r1", "kind": "tool", "tool": "charge", "inputs": {"id": "ord1"}, "outputs": {"ok": True}, "status": "ok"}
            )
        return events, {}

    invariants = [DuplicateMutatingEffect()]
    res = fuzz_and_shrink(
        runner_fn,
        invariants,
        boundaries=["auth", "charge"],
        seed=101,
        max_cases=10,
    )

    assert res["failed"] is True
    assert res["minimal_schedule"] is not None
    assert "charge" in res["minimal_schedule"]
    assert res["minimal_schedule"]["charge"]["kind"] == "timeout_after_commit"
    assert res["report"]["verdict"] == "FAIL"

    # Verify compare_reports flags this regression
    baseline_report = evaluate_trace(
        [
            {"seq": 1, "run_id": "r0", "kind": "run_start"},
            {"seq": 2, "run_id": "r0", "kind": "tool", "tool": "charge", "inputs": {"id": "ord1"}, "outputs": {"ok": True}},
        ],
        invariants,
    )
    diff = compare_reports(baseline_report, res["report"])
    assert diff["regression"] is True
    assert "duplicate_mutating_effect" in diff["regressions"]


def test_cli_check_fuzz_finds_violation_and_shrinks(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    trace_path = tmp_path / "base_trace.jsonl"
    events = [
        {"seq": 1, "run_id": "r_fuzz", "kind": "run_start"},
        {"seq": 2, "run_id": "r_fuzz", "kind": "tool", "tool": "orders_db", "inputs": {"item": "A"}, "outputs": {"version": 1}},
    ]
    trace_path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    # When fuzzing with NoLostUpdate invariant, concurrent_update on orders_db will trigger violation
    exit_code = main([
        "check",
        "--trace", str(trace_path),
        "--invariant", "no_lost_update",
        "--fuzz",
        "--max-cases", "5",
        "--seed", "42",
        "--report-out", str(tmp_path / "fuzz_rep.json"),
    ])

    assert exit_code == EXIT_VERDICT_FAIL
    out = capsys.readouterr().out
    assert "fuzz: invariant failure found" in out
    assert "fuzz: minimal schedule:" in out
    assert "no_lost_update" in out


# -----------------------------------------------------------------------------
# Follow-up Cleanups: End-to-end scenario faults, short tokens, finders
# -----------------------------------------------------------------------------
def test_scenario_faults_reach_intercept_output_e2e() -> None:
    scenario = {
        "scenario": "docs_and_db_flow",
        "task": "read doc and update counter",
        "tools": ["kb_search", "db_update"],
        "policy": "policy.yaml",
        "faults": [
            {
                "kind": "poisoned_doc",
                "params": {"tool": "kb_search", "payload": "[MALICIOUS_PROMPT]", "occurrence": 1},
            },
            {
                "kind": "concurrent_update",
                "params": {"tool": "db_update", "updates": {"counter": 99}, "occurrence": 1},
            },
        ],
    }

    ac = AgentCrash(WorkflowConfig(project="e2e_scenario", fault_injection=True))
    ac.configure_faults_from_scenario(scenario)

    with ac.run("scenario_wf") as run:
        # Tool 1: kb_search output should be intercepted and poisoned
        out_search = run.intercept_output("kb_search", {"text": "genuine doc content"})
        assert out_search["poisoned"] is True
        assert out_search["text"] == "[MALICIOUS_PROMPT]"

        # Tool 2: db_update output should be intercepted with simulated version conflict
        out_update = run.intercept_output("db_update", {"version": 1})
        assert out_update["conflict"] is True
        assert out_update["version"] == 2
        assert out_update["counter"] == 99


def test_short_cloud_token_redaction_and_scan() -> None:
    from agentcrash.scan_pii import scan_text
    from agentcrash.tracer import REDACTED, redact

    # Short test tokens with 8+ chars after prefix
    short_sk = "sk-test12345"
    short_ghp = "ghp_abcdef12"

    assert redact(f"key is {short_sk}") == f"key is {REDACTED}"
    assert redact(f"token is {short_ghp}") == f"token is {REDACTED}"

    # scan_pii flags raw occurrences
    hits = scan_text(f"sk={short_sk}\nghp={short_ghp}", "test.txt")
    assert len(hits) == 2
    assert any(h.sample == short_sk for h in hits)
    assert any(h.sample == short_ghp for h in hits)


def test_fault_schedule_generic_finders() -> None:
    from agentcrash.injector import FaultSchedule

    faults = [
        {"kind": "timeout_after_commit", "at_step": 1, "params": {"tool": "pay"}},
        {"kind": "concurrent_update", "params": {"tool": "db", "occurrence": 1}},
        {"kind": "poisoned_doc", "at_step": 2, "params": {"tool": "search"}},
    ]
    sched = FaultSchedule(faults)

    # find_step_fault
    assert sched.find_step_fault(1, "pay") == faults[0]
    assert sched.find_step_fault(2, "search") == faults[2]
    assert sched.find_step_fault(1, "pay", kinds={"concurrent_update"}) is None

    # find_call_fault
    assert sched.find_call_fault("db", None, 1) == faults[1]
    assert sched.find_call_fault("db", None, 2) is None
    assert sched.find_call_fault("db", None, 1, kinds={"poisoned_doc"}) is None

    # Backward-compatible convenience finders delegate cleanly
    assert sched.fault_for(1, "pay") == faults[0]
    assert sched.concurrent_for_call("db", None, 1) == faults[1]
    assert sched.poison_for(2, "search") == faults[2]


def test_occurrence_all_fires_always_in_policy_and_schedule() -> None:
    from agentcrash.injector import FaultSchedule
    from agentcrash.sdk import FaultPolicy

    # 1. FaultPolicy directly with occurrence: "all"
    policy = FaultPolicy(faults={"api": {"kind": "poisoned_doc", "occurrence": "all"}})
    for call_idx in (1, 2, 3, 10):
        fault = policy.should_fire("api", call_index=call_idx)
        assert fault is not None
        assert fault["kind"] == "poisoned_doc"

    # 2. Scenario mapping with occurrence: "all" through AgentCrash
    scenario = {
        "scenario": "fire_always_test",
        "task": "repeat search",
        "tools": ["search"],
        "policy": "policy.yaml",
        "faults": [
            {
                "kind": "poisoned_doc",
                "params": {"tool": "search", "payload": "[POISONED]", "occurrence": "all"},
            }
        ],
    }
    ac = AgentCrash(WorkflowConfig(project="fire_all", fault_injection=True))
    ac.configure_faults_from_scenario(scenario)

    with ac.run("repeated_calls") as run:
        for _ in range(3):
            out = run.intercept_output("search", {"text": "original"})
            assert out["poisoned"] is True
            assert out["text"] == "[POISONED]"

    # 3. FaultSchedule with occurrence: "all"
    sched = FaultSchedule([
        {"kind": "timeout_after_commit", "params": {"tool": "search", "occurrence": "all"}}
    ])
    assert sched.find_call_fault("search", None, 1) is not None
    assert sched.find_call_fault("search", None, 2) is not None
    assert sched.find_call_fault("search", None, 100) is not None


