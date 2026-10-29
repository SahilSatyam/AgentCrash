"""Phase 4 replay-command tests: fix verification, regression, fail-closed.

Exit contract: 0 fix VERIFIED, 1 VERDICT_FAIL, 2 usage, 4 rejected
patch (pre-run, no trace written). Full loop stays offline.
"""

import json
from pathlib import Path

from agentcrash.cli import main
from agentcrash.policy import reference_patch

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "timeout_retry" / "scenario.yaml"
FIXED_POLICY = REPO / "policies" / "fixed-timeout.yaml"
GOLDEN_DIR = REPO / "traces" / "golden"
FAILING_TRACE = GOLDEN_DIR / "timeout_retry-seed42.jsonl"
FAILING_STATE = GOLDEN_DIR / "timeout_retry-seed42.state.json"


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
    """Diagnose-equivalent patch file (offline reference patch, seed42)."""
    patch = reference_patch("43e3064f37f779fe")
    if mutate:
        mutate(patch)
    path = tmp_path / "patch.json"
    path.write_text(json.dumps(patch, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def test_replay_patch_verifies_fix_and_regression(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    patch = write_patch(tmp_path)
    trace_out = tmp_path / "replay.jsonl"
    state_out = tmp_path / "replay.state.json"
    report_out = tmp_path / "replay.report.json"
    code = replay(
        FAILING_TRACE,
        ["--patch", str(patch), "--trace-out", str(trace_out)],
        "--state",
        str(FAILING_STATE),
        "--state-out",
        str(state_out),
        "--report-out",
        str(report_out),
        state=FAILING_STATE,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "fix: VERIFIED" in out and "exactly_once_mutation PASS" in out
    assert "task PAID" in out and "regression:" in out and "PASS" in out
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["verdict"] == "PASS"
    assert report["fixed"] == ["exactly_once_mutation"]
    assert report["broken"] == []
    assert report["golden"]["passed"] == report["golden"]["total"] >= 3
    assert report["api_version"] == "v1"
    snapshot = json.loads(state_out.read_text(encoding="utf-8"))
    assert len(snapshot["charges"]) == 1
    assert snapshot["invoices"][0]["status"] == "PAID"


def test_replay_fixed_policy_verifies_without_regression(tmp_path, monkeypatch, capsys) -> None:
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


def test_replay_time_travel_patch_fails_closed_with_no_trace(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    patch = write_patch(
        tmp_path,
        mutate=lambda p: p["patch"]["rules"].__setitem__(
            0,
            {
                "rule": "on_tool_timeout",
                "tool": "charge_card",
                "action": "time_travel",
                "retry_with_idempotency_key": True,
                "max_retries": 1,
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


def test_fixed_policy_golden_is_strict_clean_and_pinned(tmp_path) -> None:
    fixed_scenario = REPO / "scenarios" / "timeout_retry_fixed" / "scenario.yaml"
    trace = GOLDEN_DIR / "timeout_retry-fixed-seed42.jsonl"
    state = GOLDEN_DIR / "timeout_retry-fixed-seed42.state.json"
    report = tmp_path / "fixed.verdict.json"
    code = main(
        [
            "check",
            "--trace",
            str(trace),
            "--scenario",
            str(fixed_scenario),
            "--state",
            str(state),
            "--report-out",
            str(report),
            "--strict-schema",
        ]
    )
    assert code == 0
    fresh = json.loads(report.read_text(encoding="utf-8"))
    pinned = json.loads(
        (GOLDEN_DIR / "timeout_retry-fixed-seed42.verdict.json").read_text(encoding="utf-8")
    )
    assert fresh == pinned
    assert fresh["verdict"] == "PASS"
    assert fresh["side_effect_count"] == 1
    assert fresh["task_completion"] is True


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
