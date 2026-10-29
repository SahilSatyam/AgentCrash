"""Check-command tests: recorded live fixtures, JSON determinism, exit codes.

Exit contract: 0 PASS, 1 VERDICT_FAIL, 2 usage/config, 3 CORRUPT_TRACE.
Malformed traces are rejected (exit 3) and never PASS.
"""

import json
from pathlib import Path

import yaml

from agentcrash.cli import main

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "odrl_convert_timeout" / "scenario.yaml"
TRACE_DIR = REPO / "tests" / "fixtures" / "traces"
NAIVE_TRACE = TRACE_DIR / "odrl_convert_timeout-naive-seed42.jsonl"
NAIVE_STATE = TRACE_DIR / "odrl_convert_timeout-naive-seed42.state.json"
FIXED_TRACE = TRACE_DIR / "odrl_convert_timeout-fixed-seed42.jsonl"
FIXED_STATE = TRACE_DIR / "odrl_convert_timeout-fixed-seed42.state.json"


FIXED_POLICY_FILE = REPO / "policies" / "fixed-odrl-timeout.yaml"


def check(trace, state, report, *extra, policy=None):
    """Run check with explicit scenario/state/report; return the exit code."""
    return main(
        [
            "check",
            "--trace",
            str(trace),
            "--scenario",
            str(SCENARIO_FILE),
            *(["--policy", str(policy)] if policy else []),
            "--state",
            str(state),
            "--report-out",
            str(report),
            *extra,
        ]
    )


def test_naive_live_trace_fails_exactly_once_and_validity(tmp_path, capsys) -> None:
    code = check(NAIVE_TRACE, NAIVE_STATE, tmp_path / "v.json")
    assert code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "exactly_once_save" in out
    fail_line = next(line for line in out.splitlines() if "exactly_once_save" in line)
    assert "4" in fail_line and "5" in fail_line
    assert "[FAIL]" in out and "odrl_valid" in out
    assert "[PASS]" in out and "no_attacker_grant" in out
    assert "verdict=FAIL" in out


def test_naive_report_carries_required_fields(tmp_path) -> None:
    report = tmp_path / "v.json"
    assert check(NAIVE_TRACE, NAIVE_STATE, report) == 1
    verdict = json.loads(report.read_text(encoding="utf-8"))
    assert set(verdict) >= {
        "api_version",
        "run_id",
        "scenario",
        "seed",
        "policy_hash",
        "ledger_hash",
        "verdict",
        "results",
        "side_effect_count",
        "task_completion",
    }
    assert verdict["side_effect_count"] == 2
    assert verdict["task_completion"] is True
    rows = {row["id"]: row for row in verdict["results"]}
    assert rows["exactly_once_save"]["passed"] is False
    assert rows["no_attacker_grant"]["passed"] is True


def test_json_format_is_deterministic_and_pure(tmp_path, capsys) -> None:
    bodies = []
    for i in range(3):
        report = tmp_path / f"v{i}.json"
        code = check(NAIVE_TRACE, NAIVE_STATE, report, "--format", "json")
        assert code == 1
        captured = capsys.readouterr()
        bodies.append(captured.out)
        assert "[FAIL]" in captured.err and "no_attacker_grant" in captured.err
    assert bodies[0] == bodies[1] == bodies[2]
    payload = json.loads(bodies[0])
    assert payload["api_version"] == "v1"
    assert payload["verdict"] == "FAIL"


def test_fixed_live_trace_passes_with_exit_zero(tmp_path, capsys) -> None:
    report = tmp_path / "v.json"
    code = check(FIXED_TRACE, FIXED_STATE, report, "--strict-schema", policy=FIXED_POLICY_FILE)
    assert code == 0
    out = capsys.readouterr().out
    assert out.count("[PASS]") == 4 and "verdict=PASS" in out


def test_scenario_inferred_from_trace(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    report = tmp_path / "v.json"
    code = main(
        [
            "check",
            "--trace",
            str(NAIVE_TRACE),
            "--state",
            str(NAIVE_STATE),
            "--report-out",
            str(report),
        ]
    )
    assert code == 1
    assert "exactly_once_save" in capsys.readouterr().out


def test_tampered_chain_names_first_broken_link(tmp_path, capsys) -> None:
    lines = NAIVE_TRACE.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[3])
    event["step"] = 999
    lines[3] = json.dumps(event, sort_keys=True)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code = check(bad, NAIVE_STATE, tmp_path / "v.json")
    assert code == 3
    out = capsys.readouterr().out
    assert "broken chain at" in out


def test_malformed_trace_rejected_never_passes(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"event_id": "evt_x"}\nnot json\n', encoding="utf-8")
    report = tmp_path / "v.json"
    code = check(bad, NAIVE_STATE, report)
    assert code == 3
    assert not report.exists()
    captured = capsys.readouterr()
    assert "FAIL" not in captured.out and "PASS" not in captured.out


def test_missing_trace_is_usage_error(capsys) -> None:
    assert main(["check", "--trace", "nope.jsonl"]) == 2


def test_unknown_verifier_is_config_error(tmp_path) -> None:
    scenario = yaml.safe_load(SCENARIO_FILE.read_text(encoding="utf-8"))
    scenario["invariants"] = [{"id": "exactly_once_save"}, {"id": "nope_not_real"}]
    bad_scenario = tmp_path / "scenario.yaml"
    bad_scenario.write_text(yaml.safe_dump(scenario), encoding="utf-8")
    code = main(
        [
            "check",
            "--trace",
            str(NAIVE_TRACE),
            "--scenario",
            str(bad_scenario),
            "--state",
            str(NAIVE_STATE),
            "--report-out",
            str(tmp_path / "v.json"),
        ]
    )
    assert code == 2


def test_no_verify_chain_still_judges(tmp_path) -> None:
    report = tmp_path / "v.json"
    code = check(FIXED_TRACE, FIXED_STATE, report, "--no-verify-chain")
    assert code == 0
