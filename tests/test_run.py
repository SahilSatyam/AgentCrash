"""Phase 2 run-loop tests: FR-01/02/03 oracles, seed matrix, refusal, wiring.

All runs use the scripted stub/fake adapters: fully offline, no model key,
no wall-clock sleeps (per-tool virtual deadlines keep faulted runs fast).
"""

import json
import time
from pathlib import Path

import jsonschema
import pytest
import yaml

from agentcrash.cli import main
from agentcrash.clock import VirtualClock
from agentcrash.ledger import Ledger
from agentcrash.tools import ToolRegistry
from agentcrash.tracer import compute_run_id, verify_chain

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "timeout_retry" / "scenario.yaml"
POLICY_FILE = REPO / "policies" / "default.yaml"
GOLDEN = REPO / "traces" / "golden" / "timeout_retry-seed42.jsonl"
SEED_MATRIX = [1, 7, 42, 123, 2026]


def run_cli(trace_path: Path, state_path: Path, *extra: str, seed: int = 42) -> int:
    """Run the reference scenario through cli.main; return the exit code."""
    return main(
        [
            "run",
            "--scenario",
            str(SCENARIO_FILE),
            "--seed",
            str(seed),
            "--trace-out",
            str(trace_path),
            "--state-out",
            str(state_path),
            *extra,
        ]
    )


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def normalize(path: Path) -> list[str]:
    """Normalize run-specific ids/hashes so reruns can be byte-compared."""
    normalized = []
    for i, event in enumerate(read_events(path), start=1):
        event = dict(event)
        event["run_id"] = "R"
        event["event_id"] = f"evt_R_{i:04d}"
        event["prev_hash"] = "H"
        normalized.append(json.dumps(event, sort_keys=True))
    return normalized


def validate_against(path: Path, schema_name: str, payload: dict) -> None:
    schema = json.loads((REPO / "schemas" / schema_name).read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)


def test_fr01_run_emits_schema_valid_trace_and_state(tmp_path, capsys) -> None:
    trace, state = tmp_path / "t.jsonl", tmp_path / "s.json"
    assert run_cli(trace, state, seed=42) == 0
    events = read_events(trace)
    assert len(events) > 0
    for event in events:
        validate_against(trace, "trace-event.json", event)
    snapshot = json.loads(state.read_text(encoding="utf-8"))
    validate_against(state, "env-state.json", snapshot)
    assert verify_chain(str(trace))["ok"] is True
    ts = [e["ts_virtual"] for e in events]
    assert ts == sorted(ts) and ts[0] == 0
    assert Ledger.ledger_hash(snapshot) == snapshot["ledger_hash"]
    out = capsys.readouterr().out
    assert f"run_id={snapshot['run_id']}" in out and "charges=2" in out


def test_fr02_same_seed_is_byte_identical(tmp_path) -> None:
    first = run_cli(tmp_path / "a.jsonl", tmp_path / "a.json", seed=42)
    second = run_cli(tmp_path / "b.jsonl", tmp_path / "b.json", seed=42)
    assert (first, second) == (0, 0)
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


@pytest.mark.parametrize("seed", SEED_MATRIX)
def test_fr03_seed_matrix_reruns_match_after_normalization(tmp_path, seed) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    assert run_cli(first, tmp_path / "a.json", seed=seed) == 0
    assert run_cli(second, tmp_path / "b.json", seed=seed) == 0
    assert normalize(first) == normalize(second)


def test_run_id_formula_and_default_trace_path(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path("scenarios").symlink_to(REPO / "scenarios", target_is_directory=True)
    Path("policies").symlink_to(REPO / "policies", target_is_directory=True)
    scenario_text = SCENARIO_FILE.read_text(encoding="utf-8")
    policy_text = POLICY_FILE.read_text(encoding="utf-8")
    assert main(["run", "--scenario", "timeout_retry", "--seed", "42"]) == 0
    expected = compute_run_id(scenario_text, policy_text, 42)
    assert (tmp_path / "traces" / f"{expected}.jsonl").is_file()
    assert (tmp_path / "reports" / f"{expected}.state.json").is_file()


def test_alien_fault_refused_fast_with_no_trace(tmp_path) -> None:
    scenario = yaml.safe_load(SCENARIO_FILE.read_text(encoding="utf-8"))
    scenario["faults"] = [{"at_step": 1, "kind": "alien_fault", "params": {}}]
    bad = tmp_path / "alien.yaml"
    bad.write_text(yaml.safe_dump(scenario), encoding="utf-8")
    trace = tmp_path / "alien.jsonl"
    start = time.monotonic()
    code = main(
        [
            "run",
            "--scenario",
            str(bad),
            "--policy",
            str(POLICY_FILE),
            "--trace-out",
            str(trace),
            "--state-out",
            str(tmp_path / "alien.json"),
        ]
    )
    assert time.monotonic() - start < 2
    assert code != 0
    assert not trace.exists()


def test_wiring_timeout_row_in_order(tmp_path) -> None:
    assert run_cli(tmp_path / "t.jsonl", tmp_path / "s.json") == 0
    events = read_events(tmp_path / "t.jsonl")
    call = next(
        i
        for i, e in enumerate(events)
        if e.get("tool") == "charge_card" and e.get("detail", {}).get("phase") == "tool_call"
    )
    result = next(i for i, e in enumerate(events) if e.get("outcome") == "timeout")
    injected = next(
        i
        for i, e in enumerate(events)
        if e.get("injector_event") is not None and e.get("tool") is None
    )
    assert call < result < injected
    faulted = events[result]
    assert faulted["step"] == 3
    assert faulted["latency"] == 2000
    assert faulted["injector_event"]["fault"] == "timeout_after_commit"
    assert events[call]["step"] == events[injected]["step"] == 3


def test_timeout_continues_and_double_charges_with_fresh_key(tmp_path) -> None:
    start = time.monotonic()
    assert run_cli(tmp_path / "t.jsonl", tmp_path / "s.json") == 0
    assert time.monotonic() - start < 5
    events = read_events(tmp_path / "t.jsonl")
    snapshot = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert snapshot["invoices"][0]["status"] == "PAID"
    assert [c["charge_id"] for c in snapshot["charges"]] == ["ch_001", "ch_002"]
    keys = [c["idempotency_key"] for c in snapshot["charges"]]
    assert keys[0] == "inv-9821-a3f" and keys[1] != keys[0]
    end = [e for e in events if e.get("detail", {}).get("phase") == "run_end"]
    assert len(end) == 1 and end[0]["detail"]["status"] == "completed"


def test_step3_timeout_links_to_first_committed_charge(tmp_path) -> None:
    assert run_cli(tmp_path / "t.jsonl", tmp_path / "s.json") == 0
    events = read_events(tmp_path / "t.jsonl")
    faulted = next(e for e in events if e.get("outcome") == "timeout")
    assert faulted["injector_event"]["committed"] is True
    snapshot = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert snapshot["charges"][0]["charge_id"] == faulted["detail"]["data"]["charge_id"]


def test_unknown_tool_and_bad_args_rejected_without_effect() -> None:
    ledger, clock = Ledger(), VirtualClock()
    ledger.add_invoice(9821, "Acme Corp", 150)
    registry = ToolRegistry.from_scenario(
        {
            "tools": [
                {"name": "charge_card", "latency_ms": 300, "timeout_ms": 1500},
                {"name": "get_transaction_status", "latency_ms": 120, "timeout_ms": 1000},
            ]
        },
        ledger,
    )
    assert registry.call("nope", {}, clock)["outcome"] == "error"
    assert registry.call("charge_card", {"invoice_id": 9821}, clock)["outcome"] == "error"
    assert (
        registry.call("charge_card", {"invoice_id": 9821, "amount": 150, "tip": 1}, clock)[
            "outcome"
        ]
        == "error"
    )
    assert (
        registry.call("charge_card", {"invoice_id": True, "amount": 150}, clock)["outcome"]
        == "error"
    )
    assert ledger.charges == [] and clock.now() == 0
    good = registry.call("charge_card", {"invoice_id": 9821, "amount": 150}, clock)
    assert good["outcome"] == "success" and clock.now() == 300


def test_max_steps_runaway_reports_exit_5(tmp_path) -> None:
    trace = tmp_path / "t.jsonl"
    code = run_cli(trace, tmp_path / "s.json", "--max-steps", "3")
    assert code == 5
    events = read_events(trace)
    end = next(e for e in events if e.get("detail", {}).get("phase") == "run_end")
    assert end["detail"]["status"] == "runaway"


def test_dry_run_writes_nothing(tmp_path, capsys) -> None:
    trace = tmp_path / "t.jsonl"
    code = run_cli(trace, tmp_path / "s.json", "--dry-run")
    assert code == 0
    assert not trace.exists()
    out = capsys.readouterr().out
    assert "payment_gateway_timeout" in out and "nothing written" in out


def test_golden_trace_matches_fresh_seed42_run(tmp_path) -> None:
    assert GOLDEN.is_file(), "golden failing trace must be checked in"
    fresh = tmp_path / "fresh.jsonl"
    assert run_cli(fresh, tmp_path / "s.json", seed=42) == 0
    assert normalize(GOLDEN) == normalize(fresh)
    assert verify_chain(str(GOLDEN))["ok"] is True
