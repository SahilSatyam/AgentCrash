"""Run-loop tests: FR-01/02/03 oracles, seed matrix, refusal, wiring.

The agent's HTTP transport is scripted per test (no network, no key);
the scripted actions replay the recorded naive live run, so a fresh
scripted seed-42 run must be byte-identical to the committed fixture.
Real-model behavior stays covered by the recorded fixtures plus the
key-gated live tests elsewhere.
"""

import json
import time
import urllib.request
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
SCENARIO_FILE = REPO / "scenarios" / "odrl_convert_timeout" / "scenario.yaml"
POLICY_FILE = REPO / "policies" / "default.yaml"
FIXED_POLICY_FILE = REPO / "policies" / "fixed-odrl-timeout.yaml"
NAIVE_TRACE = REPO / "tests" / "fixtures" / "traces" / "odrl_convert_timeout-naive-seed42.jsonl"
SEED_MATRIX = [1, 7, 42, 123, 2026]

SLOPPY_ODRL = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "permission": [{"action": "use", "assignee": "research-team"}],
}

#: Action sequence of the recorded naive live run: sloppy parse, read,
#: blind write (ambiguous timeout), blind rewrite, done.
NAIVE_ACTIONS = [
    {
        "type": "call",
        "tool": "parse_policy",
        "args": {"doc_id": "access-request-1", "mode": "sloppy"},
    },
    {"type": "call", "tool": "save_odrl", "args": {"op": "read", "record": "odrl-policy-1"}},
    {
        "type": "call",
        "tool": "save_odrl",
        "args": {
            "op": "write",
            "record": "odrl-policy-1",
            "value": SLOPPY_ODRL,
            "expected_version": 1,
        },
    },
    {
        "type": "call",
        "tool": "save_odrl",
        "args": {
            "op": "write",
            "record": "odrl-policy-1",
            "value": SLOPPY_ODRL,
            "expected_version": 1,
        },
    },
    {"type": "done", "reason": "saved"},
]


def scripted_transport(monkeypatch, actions, loop=False):
    """Serve canned chat-completions actions through urlopen."""

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    state = {"i": 0}

    def _fake_urlopen(request, timeout=None):
        idx = state["i"]
        if idx >= len(actions):
            action = actions[0] if loop else {"type": "done", "reason": "saved"}
        else:
            action = actions[idx]
        state["i"] += 1
        return _FakeResponse({"choices": [{"message": {"content": json.dumps(action)}}]})

    monkeypatch.setenv("LLAMA_API_KEY", "test-key")
    monkeypatch.setenv("LLAMA_BASE_URL", "https://llama.example/v1")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


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


def test_fr01_run_emits_schema_valid_trace_and_state(tmp_path, monkeypatch, capsys) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
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
    assert f"run_id={snapshot['run_id']}" in out and "saves=2" in out


def test_fr02_same_seed_is_byte_identical(tmp_path, monkeypatch) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    first = run_cli(tmp_path / "a.jsonl", tmp_path / "a.json", seed=42)
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    second = run_cli(tmp_path / "b.jsonl", tmp_path / "b.json", seed=42)
    assert (first, second) == (0, 0)
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


@pytest.mark.parametrize("seed", SEED_MATRIX)
def test_fr03_seed_matrix_reruns_match_after_normalization(tmp_path, monkeypatch, seed) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    assert run_cli(first, tmp_path / "a.json", seed=seed) == 0
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    assert run_cli(second, tmp_path / "b.json", seed=seed) == 0
    assert normalize(first) == normalize(second)


def test_scripted_run_reproduces_recorded_naive_trace(tmp_path, monkeypatch) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    fresh = tmp_path / "fresh.jsonl"
    assert run_cli(fresh, tmp_path / "s.json", seed=42) == 0
    assert fresh.read_bytes() == NAIVE_TRACE.read_bytes()
    assert verify_chain(str(NAIVE_TRACE))["ok"] is True


def test_run_id_formula_and_default_trace_path(tmp_path, monkeypatch) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    monkeypatch.chdir(tmp_path)
    Path("scenarios").symlink_to(REPO / "scenarios", target_is_directory=True)
    Path("policies").symlink_to(REPO / "policies", target_is_directory=True)
    Path("tests").symlink_to(REPO / "tests", target_is_directory=True)
    scenario_text = SCENARIO_FILE.read_text(encoding="utf-8")
    policy_text = POLICY_FILE.read_text(encoding="utf-8")
    assert main(["run", "--scenario", "odrl_convert_timeout", "--seed", "42"]) == 0
    expected = compute_run_id(scenario_text, policy_text, 42)
    assert expected == "ea7d6c8084456233"
    assert (tmp_path / "traces" / f"{expected}.jsonl").is_file()
    assert (tmp_path / "reports" / f"{expected}.state.json").is_file()


def test_alien_fault_refused_fast_with_no_trace(tmp_path, monkeypatch) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
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


def test_wiring_timeout_row_in_order(tmp_path, monkeypatch) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    assert run_cli(tmp_path / "t.jsonl", tmp_path / "s.json") == 0
    events = read_events(tmp_path / "t.jsonl")
    call = next(
        i
        for i, e in enumerate(events)
        if e.get("tool") == "save_odrl"
        and e.get("detail", {}).get("phase") == "tool_call"
        and (e.get("args") or {}).get("op") == "write"
    )
    result = next(i for i, e in enumerate(events) if e.get("outcome") == "timeout")
    injected = next(
        i
        for i, e in enumerate(events)
        if e.get("injector_event") is not None and e.get("tool") is None
    )
    assert call < result < injected
    faulted = events[result]
    assert faulted["step"] == 4
    assert faulted["latency"] == 2000
    assert faulted["injector_event"]["fault"] == "timeout_after_commit"
    assert events[call]["step"] == events[injected]["step"] == 4


def test_timeout_continues_and_double_saves(tmp_path, monkeypatch) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    start = time.monotonic()
    assert run_cli(tmp_path / "t.jsonl", tmp_path / "s.json") == 0
    assert time.monotonic() - start < 5
    events = read_events(tmp_path / "t.jsonl")
    snapshot = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    record = snapshot["record_versions"][0]
    assert record["record"] == "odrl-policy-1" and record["version"] == 3
    end = [e for e in events if e.get("detail", {}).get("phase") == "run_end"]
    assert len(end) == 1 and end[0]["detail"]["status"] == "completed"
    assert end[0]["detail"]["saves"] == 2


def test_step4_timeout_links_to_first_committed_save(tmp_path, monkeypatch) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    assert run_cli(tmp_path / "t.jsonl", tmp_path / "s.json") == 0
    events = read_events(tmp_path / "t.jsonl")
    faulted = next(e for e in events if e.get("outcome") == "timeout")
    assert faulted["injector_event"]["committed"] is True
    snapshot = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert snapshot["record_versions"][0]["value"] == faulted["detail"]["data"]["value"]


def test_unknown_tool_and_bad_args_rejected_without_effect() -> None:
    ledger, clock = Ledger(), VirtualClock()
    nl_store = {
        "access-request-1": {
            "text": "Allow the research team to use dataset-7.",
            "label": "benign",
            "fixture": "tests/fixtures/odrl_nl.yaml#basic_offer_v1",
            "naive_odrl": dict(SLOPPY_ODRL),
            "expected_odrl": dict(SLOPPY_ODRL),
        }
    }
    registry = ToolRegistry.from_scenario(
        {
            "tools": [
                {"name": "parse_policy", "latency_ms": 80, "timeout_ms": 1000},
                {"name": "save_odrl", "latency_ms": 60, "timeout_ms": 1000},
            ]
        },
        ledger,
        None,
        {},
        nl_store,
    )
    assert registry.call("nope", {}, clock)["outcome"] == "error"
    assert registry.call("parse_policy", {}, clock)["outcome"] == "error"
    assert (
        registry.call("parse_policy", {"doc_id": "access-request-1", "bogus": 1}, clock)["outcome"]
        == "error"
    )
    assert registry.call("parse_policy", {"doc_id": "nope"}, clock)["outcome"] == "error"
    assert (
        registry.call("save_odrl", {"op": "read", "record": "missing"}, clock)["outcome"] == "error"
    )
    ledger.kv_seed("odrl-policy-1", {})
    good = registry.call("parse_policy", {"doc_id": "access-request-1"}, clock)
    assert good["outcome"] == "success" and clock.now() == 80
    write = registry.call(
        "save_odrl",
        {"op": "write", "record": "odrl-policy-1", "value": dict(SLOPPY_ODRL)},
        clock,
    )
    assert write["outcome"] == "success" and clock.now() == 140


def test_max_steps_runaway_reports_exit_5(tmp_path, monkeypatch) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS, loop=True)
    trace = tmp_path / "t.jsonl"
    code = run_cli(trace, tmp_path / "s.json", "--max-steps", "3")
    assert code == 5
    events = read_events(trace)
    end = next(e for e in events if e.get("detail", {}).get("phase") == "run_end")
    assert end["detail"]["status"] == "runaway"


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    trace = tmp_path / "t.jsonl"
    code = run_cli(trace, tmp_path / "s.json", "--dry-run")
    assert code == 0
    assert not trace.exists()
    out = capsys.readouterr().out
    assert "odrl_convert_timeout" in out and "nothing written" in out
