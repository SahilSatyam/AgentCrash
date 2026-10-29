"""Phase 4 diagnose-command tests: reference patch, refusal, read-only proof.

Exit contract: 0 patch written, 2 usage/refusal, 3 corrupt trace,
4 rejected patch. All paths run offline (no LLAMA_BASE_URL); one test
pins the live-path rejection with a stubbed HTTP 200 payload.
"""

import hashlib
import json
import os
from pathlib import Path

from agentcrash.cli import main
from agentcrash.policy import validate_patch_data

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "timeout_retry" / "scenario.yaml"
POLICY_FILE = REPO / "policies" / "default.yaml"
GOLDEN_DIR = REPO / "traces" / "golden"
FAILING_TRACE = GOLDEN_DIR / "timeout_retry-seed42.jsonl"
FAILING_STATE = GOLDEN_DIR / "timeout_retry-seed42.state.json"
FIXED_TRACE = GOLDEN_DIR / "timeout_retry-fixed-seed43.jsonl"
FIXED_STATE = GOLDEN_DIR / "timeout_retry-fixed-seed43.state.json"


def diagnose(trace, output, *extra, state=None, env=None):
    """Run diagnose against the reference scenario; return the exit code."""
    args = [
        "diagnose",
        "--trace",
        str(trace),
        "--output",
        str(output),
        "--scenario",
        str(SCENARIO_FILE),
        *(["--state", str(state)] if state else []),
        *extra,
    ]
    if env is None:
        return main(args)
    old = dict(os.environ)
    os.environ.update(env)
    try:
        return main(args)
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_diagnose_failing_trace_writes_reference_patch(tmp_path, capsys) -> None:
    out = tmp_path / "patch.json"
    code = diagnose(FAILING_TRACE, out, state=FAILING_STATE)
    assert code == 0
    patch = json.loads(out.read_text(encoding="utf-8"))
    validate_patch_data(patch)
    rule = patch["patch"]["rules"][0]
    assert (rule["rule"], rule["tool"]) == ("on_tool_timeout", "charge_card")
    assert rule["action"] == "check_then_act"
    assert rule["check"]["tool"] == "get_transaction_status"
    assert rule["retry_with_idempotency_key"] is True
    assert rule["max_retries"] == 1
    stdout = capsys.readouterr().out
    assert "patch=" in stdout and "policy_patch/v1" in stdout
    assert "read-only" in stdout


def test_diagnose_refuses_passing_trace(tmp_path, capsys) -> None:
    out = tmp_path / "patch.json"
    code = diagnose(FIXED_TRACE, out, state=FIXED_STATE)
    assert code == 2
    assert not out.exists()
    assert "No invariant violations to diagnose" in capsys.readouterr().err


def test_diagnose_leaves_policy_and_ledger_untouched(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(REPO)
    policy_copy = tmp_path / "base.yaml"
    policy_copy.write_text(POLICY_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    state_copy = tmp_path / "state.json"
    state_copy.write_text(FAILING_STATE.read_text(encoding="utf-8"), encoding="utf-8")
    mtime_before = policy_copy.stat().st_mtime_ns
    digest_before = hashlib.sha256(state_copy.read_bytes()).hexdigest()
    code = diagnose(FAILING_TRACE, tmp_path / "patch.json", state=state_copy)
    assert code == 0
    assert policy_copy.stat().st_mtime_ns == mtime_before
    assert hashlib.sha256(state_copy.read_bytes()).hexdigest() == digest_before


def test_diagnose_max_rules_zero_rejects_single_rule_patch(tmp_path, capsys) -> None:
    out = tmp_path / "patch.json"
    code = diagnose(FAILING_TRACE, out, "--max-rules", "0", state=FAILING_STATE)
    assert code == 4
    assert not out.exists()
    assert "E_POLICY_REJECTED" in capsys.readouterr().err


def test_diagnose_missing_trace_is_usage_error(tmp_path, capsys) -> None:
    assert diagnose(tmp_path / "nope.jsonl", tmp_path / "p.json") == 2


def test_diagnose_live_invalid_output_rejected_exit_4(tmp_path, capsys, monkeypatch) -> None:
    import urllib.request

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps({"nope": True})}}]}
            ).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: FakeResponse())
    out = tmp_path / "patch.json"
    code = diagnose(
        FAILING_TRACE,
        out,
        state=FAILING_STATE,
        env={"LLAMA_BASE_URL": "http://127.0.0.1:9"},
    )
    assert code == 4
    assert not out.exists()
    assert "E_POLICY_REJECTED" in capsys.readouterr().err


def test_diagnose_live_transport_failure_falls_back_to_fixture(
    tmp_path, capsys, monkeypatch
) -> None:
    import urllib.error
    import urllib.request

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    out = tmp_path / "patch.json"
    code = diagnose(
        FAILING_TRACE,
        out,
        state=FAILING_STATE,
        env={"LLAMA_BASE_URL": "http://127.0.0.1:9"},
    )
    assert code == 0
    validate_patch_data(json.loads(out.read_text(encoding="utf-8")))
    assert "fixture-backed" in capsys.readouterr().err


def test_diagnose_patch_applies_onto_base_policy(tmp_path) -> None:
    import yaml

    from agentcrash.policy import apply_patch, load_policy

    out = tmp_path / "patch.json"
    assert diagnose(FAILING_TRACE, out, state=FAILING_STATE) == 0
    base, _ = load_policy(POLICY_FILE)
    patch = json.loads(out.read_text(encoding="utf-8"))
    new_policy, _ = apply_patch(base, patch)
    assert new_policy["rules"][0]["retry"] == {
        "max_attempts": 1,
        "require_idempotency_key": True,
    }
    assert (
        yaml.safe_load((REPO / "policies" / "fixed-timeout.yaml").read_text(encoding="utf-8"))[
            "rules"
        ][0]["retry"]
        == new_policy["rules"][0]["retry"]
    )


def test_diagnose_verdict_flag_drives_refusal_and_patch(tmp_path, capsys) -> None:
    failing_verdict = tmp_path / "fail.verdict.json"
    assert (
        main(
            [
                "check",
                "--trace",
                str(FAILING_TRACE),
                "--scenario",
                str(SCENARIO_FILE),
                "--state",
                str(FAILING_STATE),
                "--report-out",
                str(failing_verdict),
            ]
        )
        == 1
    )
    out = tmp_path / "patch.json"
    assert diagnose(FAILING_TRACE, out, "--verdict", str(failing_verdict), state=FAILING_STATE) == 0
    validate_patch_data(json.loads(out.read_text(encoding="utf-8")))
    capsys.readouterr()

    passing_verdict = tmp_path / "pass.verdict.json"
    assert (
        main(
            [
                "check",
                "--trace",
                str(FIXED_TRACE),
                "--scenario",
                str(SCENARIO_FILE),
                "--state",
                str(FIXED_STATE),
                "--report-out",
                str(passing_verdict),
            ]
        )
        == 0
    )
    out2 = tmp_path / "patch2.json"
    assert diagnose(FIXED_TRACE, out2, "--verdict", str(passing_verdict), state=FIXED_STATE) == 2
    assert not out2.exists()
    assert "No invariant violations to diagnose" in capsys.readouterr().err
