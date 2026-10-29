"""Phase 3 check-command tests: golden suite, JSON determinism, exit codes.

Exit contract: 0 PASS, 1 VERDICT_FAIL, 2 usage/config, 3 CORRUPT_TRACE.
Malformed traces are rejected (exit 3) and never PASS.
"""

import json
from pathlib import Path

import yaml

from agentcrash.cli import main

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "timeout_retry" / "scenario.yaml"
POLICY_FILE = REPO / "policies" / "default.yaml"
GOLDEN_DIR = REPO / "traces" / "golden"

CASES = [
    ("timeout_retry-seed42.jsonl", "timeout_retry-seed42.state.json", 1, "FAIL"),
    ("timeout_retry-fixed-seed43.jsonl", "timeout_retry-fixed-seed43.state.json", 0, "PASS"),
    ("timeout_retry-giveup-seed44.jsonl", "timeout_retry-giveup-seed44.state.json", 1, "FAIL"),
]


def check(trace, state, report, *extra):
    """Run check with explicit scenario/state/report; return the exit code."""
    return main(
        [
            "check",
            "--trace",
            str(trace),
            "--scenario",
            str(SCENARIO_FILE),
            "--state",
            str(state),
            "--report-out",
            str(report),
            *extra,
        ]
    )


def test_run_to_check_shows_double_charge_fail_and_task_pass(tmp_path, capsys) -> None:
    trace = GOLDEN_DIR / "timeout_retry-seed42.jsonl"
    state = GOLDEN_DIR / "timeout_retry-seed42.state.json"
    code = check(trace, state, tmp_path / "v.json")
    assert code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "exactly_once_mutation" in out
    fail_line = next(line for line in out.splitlines() if "exactly_once_mutation" in line)
    assert "3" in fail_line and "4" in fail_line
    assert "[PASS]" in out and "task_completion" in out
    assert "verdict=FAIL" in out


def test_golden_suite_matches_pinned_verdicts(tmp_path) -> None:
    for name, state_name, want_code, want_verdict in CASES:
        report = tmp_path / f"{name}.verdict.json"
        code = check(GOLDEN_DIR / name, GOLDEN_DIR / state_name, report, "--strict-schema")
        assert code == want_code, name
        fresh = json.loads(report.read_text(encoding="utf-8"))
        pinned = json.loads((GOLDEN_DIR / name.replace(".jsonl", ".verdict.json")).read_text())
        assert fresh == pinned, name
        assert fresh["verdict"] == want_verdict, name
        assert fresh["api_version"] == "v1"


def test_golden_results_carry_required_fields(tmp_path) -> None:
    report = tmp_path / "v.json"
    assert check(GOLDEN_DIR / CASES[0][0], GOLDEN_DIR / CASES[0][1], report) == 1
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
    assert rows["exactly_once_mutation"]["passed"] is False
    assert rows["task_completion"]["passed"] is True


def test_giveup_golden_exactly_once_passes_task_fails(tmp_path) -> None:
    report = tmp_path / "v.json"
    assert check(GOLDEN_DIR / CASES[2][0], GOLDEN_DIR / CASES[2][1], report) == 1
    verdict = json.loads(report.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in verdict["results"]}
    assert rows["exactly_once_mutation"]["passed"] is True
    assert rows["task_completion"]["passed"] is False
    assert verdict["task_completion"] is False


def test_json_format_is_deterministic_and_pure(tmp_path, capsys) -> None:
    trace, state_name, _, _ = CASES[0]
    bodies = []
    for i in range(3):
        report = tmp_path / f"v{i}.json"
        code = check(
            GOLDEN_DIR / trace,
            GOLDEN_DIR / state_name,
            report,
            "--format",
            "json",
        )
        assert code == 1
        captured = capsys.readouterr()
        bodies.append(captured.out)
        assert "[FAIL]" in captured.err and "task_completion" in captured.err
    assert bodies[0] == bodies[1] == bodies[2]
    payload = json.loads(bodies[0])
    assert payload["api_version"] == "v1"
    assert payload["verdict"] == "FAIL"


def test_fixed_golden_passes_with_exit_zero(tmp_path, capsys) -> None:
    report = tmp_path / "v.json"
    code = check(GOLDEN_DIR / CASES[1][0], GOLDEN_DIR / CASES[1][1], report, "--strict-schema")
    assert code == 0
    out = capsys.readouterr().out
    assert out.count("[PASS]") == 2 and "verdict=PASS" in out


def test_scenario_inferred_from_trace(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    report = tmp_path / "v.json"
    code = main(
        [
            "check",
            "--trace",
            str(GOLDEN_DIR / CASES[0][0]),
            "--state",
            str(GOLDEN_DIR / CASES[0][1]),
            "--report-out",
            str(report),
        ]
    )
    assert code == 1
    assert "exactly_once_mutation" in capsys.readouterr().out


def test_tampered_chain_names_first_broken_link(tmp_path, capsys) -> None:
    lines = (GOLDEN_DIR / CASES[0][0]).read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[3])
    event["step"] = 999
    lines[3] = json.dumps(event, sort_keys=True)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code = check(bad, GOLDEN_DIR / CASES[0][1], tmp_path / "v.json")
    assert code == 3
    out = capsys.readouterr().out
    assert "broken chain at" in out


def test_malformed_trace_rejected_never_passes(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"event_id": "evt_x"}\nnot json\n', encoding="utf-8")
    report = tmp_path / "v.json"
    code = check(bad, GOLDEN_DIR / CASES[0][1], report)
    assert code == 3
    assert not report.exists()
    captured = capsys.readouterr()
    assert "FAIL" not in captured.out and "PASS" not in captured.out


def test_missing_trace_is_usage_error(capsys) -> None:
    assert main(["check", "--trace", "nope.jsonl"]) == 2


def test_unknown_verifier_is_config_error(tmp_path) -> None:
    scenario = yaml.safe_load(SCENARIO_FILE.read_text(encoding="utf-8"))
    scenario["invariants"] = [{"id": "exactly_once_mutation"}, {"id": "nope_not_real"}]
    bad_scenario = tmp_path / "scenario.yaml"
    bad_scenario.write_text(yaml.safe_dump(scenario), encoding="utf-8")
    code = main(
        [
            "check",
            "--trace",
            str(GOLDEN_DIR / CASES[0][0]),
            "--scenario",
            str(bad_scenario),
            "--state",
            str(GOLDEN_DIR / CASES[0][1]),
            "--report-out",
            str(tmp_path / "v.json"),
        ]
    )
    assert code == 2


def test_no_verify_chain_still_judges(tmp_path) -> None:
    report = tmp_path / "v.json"
    code = check(
        GOLDEN_DIR / CASES[1][0],
        GOLDEN_DIR / CASES[1][1],
        report,
        "--no-verify-chain",
    )
    assert code == 0
