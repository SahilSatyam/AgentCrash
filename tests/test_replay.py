"""Replay-command tests: fix verification, regression, fail-closed.

Exit contract: 0 fix VERIFIED, 1 VERDICT_FAIL, 2 usage, 4 rejected
patch (pre-run, no trace written). Re-runs use scripted transport
(no network, no key); the scripts replay feasible live behaviors,
including the reconcile-first fix the live model discovered.
"""

import json
import urllib.request
from pathlib import Path

import pytest

from agentcrash.cli import main
from agentcrash.policy import reference_patch_for

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "odrl_convert_timeout" / "scenario.yaml"
FIXED_POLICY = REPO / "policies" / "fixed-odrl-timeout.yaml"
TRACE_DIR = REPO / "tests" / "fixtures" / "traces"
FAILING_TRACE = TRACE_DIR / "odrl_convert_timeout-naive-seed42.jsonl"
FAILING_STATE = TRACE_DIR / "odrl_convert_timeout-naive-seed42.state.json"

STRICT_ODRL = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "assigner": "data-steward",
    "permission": [
        {
            "action": "use",
            "target": "urn:data:dataset-7",
            "assignee": "research-team",
        }
    ],
}

SLOPPY_ODRL = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "permission": [{"action": "use", "assignee": "research-team"}],
}

DONE = {"type": "done", "reason": "saved"}

#: Reconcile-first behavior under the diagnosed patch: sloppy parse,
#: blind write (ambiguous timeout), re-read, stop. Exactly one save.
RECONCILE_ACTIONS = [
    {
        "type": "call",
        "tool": "parse_policy",
        "args": {"doc_id": "access-request-1", "mode": "sloppy"},
    },
    {
        "type": "call",
        "tool": "save_odrl",
        "args": {"op": "read", "record": "odrl-policy-1"},
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
    {
        "type": "call",
        "tool": "save_odrl",
        "args": {"op": "read", "record": "odrl-policy-1"},
    },
    DONE,
]

#: Strict + reconcile behavior under the fixed policy: the fixed live run.
FIXED_ACTIONS = [
    {
        "type": "call",
        "tool": "parse_policy",
        "args": {"doc_id": "access-request-1", "mode": "strict"},
    },
    {"type": "call", "tool": "validate_odrl", "args": {"odrl": STRICT_ODRL}},
    {
        "type": "call",
        "tool": "save_odrl",
        "args": {"op": "read", "record": "odrl-policy-1"},
    },
    {
        "type": "call",
        "tool": "save_odrl",
        "args": {
            "op": "write",
            "record": "odrl-policy-1",
            "value": STRICT_ODRL,
            "expected_version": 1,
        },
    },
    {
        "type": "call",
        "tool": "save_odrl",
        "args": {"op": "read", "record": "odrl-policy-1"},
    },
    DONE,
]


def scripted_transport(monkeypatch, actions):
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
        action = actions[idx] if idx < len(actions) else DONE
        state["i"] += 1
        return _FakeResponse({"choices": [{"message": {"content": json.dumps(action)}}]})

    monkeypatch.setenv("LLAMA_API_KEY", "test-key")
    monkeypatch.setenv("LLAMA_BASE_URL", "https://llama.example/v1")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


def replay(trace, output_args, *extra, state=None):
    """Run replay against the reference scenario; return the exit code."""
    return main(
        [
            "replay",
            "--trace",
            str(trace),
            "--scenario",
            str(SCENARIO_FILE),
            *(["--state", str(state)] if state else []),
            *output_args,
            *extra,
        ]
    )


def write_patch(tmp_path, mutate=None):
    """Diagnose-equivalent patch file (offline reference patch)."""
    patch = reference_patch_for("odrl_convert_timeout", "ea7d6c8084456233")
    if mutate:
        mutate(patch)
    path = tmp_path / "patch.json"
    path.write_text(json.dumps(patch, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def test_replay_patch_fixes_exactly_once_but_not_validity(tmp_path, monkeypatch, capsys) -> None:
    scripted_transport(monkeypatch, RECONCILE_ACTIONS)
    monkeypatch.chdir(REPO)
    patch = write_patch(tmp_path)
    report_out = tmp_path / "replay.report.json"
    code = replay(
        FAILING_TRACE,
        ["--patch", str(patch), "--trace-out", str(tmp_path / "replay.jsonl")],
        "--state",
        str(FAILING_STATE),
        "--state-out",
        str(tmp_path / "replay.state.json"),
        "--report-out",
        str(report_out),
        "--no-regression",
        state=FAILING_STATE,
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "fix: NOT_FIXED" in out and "verdict FAIL" in out
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["verdict"] == "FAIL"
    assert sorted(report["fixed"]) == ["exactly_once_save", "fresh_write_only"]
    assert report["broken"] == []
    assert report["api_version"] == "v1"


def test_replay_fixed_policy_verifies(tmp_path, monkeypatch, capsys) -> None:
    scripted_transport(monkeypatch, FIXED_ACTIONS)
    monkeypatch.chdir(REPO)
    code = replay(
        FAILING_TRACE,
        ["--policy", str(FIXED_POLICY), "--no-regression"],
        "--trace-out",
        str(tmp_path / "r.jsonl"),
        "--state-out",
        str(tmp_path / "r.json"),
        "--report-out",
        str(tmp_path / "r.report.json"),
        state=FAILING_STATE,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "fix: VERIFIED" in out and "regression: skipped" in out
    report = json.loads((tmp_path / "r.report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "PASS"
    assert sorted(report["fixed"]) == [
        "exactly_once_save",
        "fresh_write_only",
        "odrl_valid",
    ]


def test_replay_time_travel_patch_fails_closed_with_no_trace(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    patch = write_patch(
        tmp_path,
        mutate=lambda p: p["patch"]["rules"].__setitem__(
            0,
            {
                "rule": "on_tool_timeout",
                "tool": "save_odrl",
                "action": "time_travel",
                "retry": {"max_attempts": 1, "require_idempotency_key": False},
            },
        ),
    )
    trace_out = tmp_path / "evil.jsonl"
    code = replay(
        FAILING_TRACE,
        ["--patch", str(patch), "--trace-out", str(trace_out)],
        "--state-out",
        str(tmp_path / "evil.json"),
        "--report-out",
        str(tmp_path / "evil.report.json"),
        state=FAILING_STATE,
    )
    assert code == 4
    assert not trace_out.exists()
    assert "E_POLICY_REJECTED" in capsys.readouterr().err


def test_replay_malformed_patch_rejected_pre_run(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"patch_version": 1}), encoding="utf-8")
    trace_out = tmp_path / "bad.jsonl"
    code = replay(
        FAILING_TRACE,
        ["--patch", str(bad), "--trace-out", str(trace_out)],
        "--state-out",
        str(tmp_path / "bad.json"),
        state=FAILING_STATE,
    )
    assert code == 4
    assert not trace_out.exists()
    assert "E_POLICY_REJECTED" in capsys.readouterr().err


def test_replay_requires_exactly_one_of_policy_or_patch(tmp_path, capsys) -> None:
    patch = write_patch(tmp_path)
    assert (
        replay(
            FAILING_TRACE,
            ["--policy", str(FIXED_POLICY), "--patch", str(patch)],
            "--trace-out",
            str(tmp_path / "x.jsonl"),
            state=FAILING_STATE,
        )
        == 2
    )
    assert (
        replay(FAILING_TRACE, ["--trace-out", str(tmp_path / "y.jsonl")], state=FAILING_STATE) == 2
    )
    assert "E_CONFIG" in capsys.readouterr().err


def test_replay_live_model_end_to_end(tmp_path, monkeypatch, capsys) -> None:
    """Full replay against the real model; needs LLAMA_API_KEY (skipped in CI)."""
    import os

    if not os.environ.get("LLAMA_API_KEY", "").strip():
        pytest.skip("live replay needs LLAMA_API_KEY")
    monkeypatch.chdir(REPO)
    code = replay(
        FAILING_TRACE,
        ["--policy", str(FIXED_POLICY), "--no-regression"],
        "--trace-out",
        str(tmp_path / "live.jsonl"),
        "--state-out",
        str(tmp_path / "live.json"),
        "--report-out",
        str(tmp_path / "live.report.json"),
        state=FAILING_STATE,
    )
    assert code in (0, 1)
    report = json.loads((tmp_path / "live.report.json").read_text(encoding="utf-8"))
    assert report["api_version"] == "v1"
    assert "fixed" in report and "broken" in report
