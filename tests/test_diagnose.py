"""Diagnose-command tests: reference patch, refusal, read-only proof.

Exit contract: 0 patch written, 2 usage/refusal, 3 corrupt trace,
4 rejected patch. The recorded live traces under tests/fixtures/traces
are the oracles: the naive timeout run must FAIL, the fixed run PASS.
Offline paths unset LLAMA_BASE_URL; localhost tests pin the live path
with a stubbed HTTP 200 payload (no key, no network).
"""

import hashlib
import json
import os
from pathlib import Path

from agentcrash.cli import main
from agentcrash.policy import validate_patch_data

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "odrl_convert_timeout" / "scenario.yaml"
POLICY_FILE = REPO / "policies" / "default.yaml"
FIXED_POLICY_FILE = REPO / "policies" / "fixed-odrl-timeout.yaml"
TRACE_DIR = REPO / "tests" / "fixtures" / "traces"
FAILING_TRACE = TRACE_DIR / "odrl_convert_timeout-naive-seed42.jsonl"
FAILING_STATE = TRACE_DIR / "odrl_convert_timeout-naive-seed42.state.json"
PASSING_TRACE = TRACE_DIR / "odrl_convert_timeout-fixed-seed42.jsonl"
PASSING_STATE = TRACE_DIR / "odrl_convert_timeout-fixed-seed42.state.json"


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


def test_recorded_naive_trace_fails_check(tmp_path) -> None:
    report = tmp_path / "naive.verdict.json"
    code = main(
        [
            "check",
            "--trace",
            str(FAILING_TRACE),
            "--state",
            str(FAILING_STATE),
            "--report-out",
            str(report),
        ]
    )
    assert code == 1
    verdict = json.loads(report.read_text(encoding="utf-8"))
    assert verdict["verdict"] == "FAIL"
    failed = {row["id"] for row in verdict["results"] if not row["passed"]}
    assert {"odrl_valid", "exactly_once_save", "fresh_write_only"} <= failed


def test_recorded_fixed_trace_passes_check(tmp_path) -> None:
    report = tmp_path / "fixed.verdict.json"
    code = main(
        [
            "check",
            "--trace",
            str(PASSING_TRACE),
            "--state",
            str(PASSING_STATE),
            "--report-out",
            str(report),
        ]
    )
    assert code == 0
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"] == "PASS"


def test_diagnose_failing_trace_writes_reference_patch(tmp_path, capsys) -> None:
    out = tmp_path / "patch.json"
    code = diagnose(FAILING_TRACE, out, state=FAILING_STATE)
    assert code == 0
    patch = json.loads(out.read_text(encoding="utf-8"))
    validate_patch_data(patch)
    rule = patch["patch"]["rules"][0]
    assert (rule["rule"], rule["tool"]) == ("on_tool_timeout", "save_odrl")
    assert rule["action"] == "check_then_act"
    assert rule["check"] == {"tool": "save_odrl", "by": "version"}
    stdout = capsys.readouterr().out
    assert "patch=" in stdout and "policy_patch/v1" in stdout
    assert "read-only" in stdout


def test_diagnose_refuses_passing_trace(tmp_path, capsys) -> None:
    out = tmp_path / "patch.json"
    code = diagnose(PASSING_TRACE, out, state=PASSING_STATE)
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
    assert new_policy["rules"][0]["check"] == {"tool": "save_odrl", "by": "version"}
    fixed_rules = yaml.safe_load(FIXED_POLICY_FILE.read_text(encoding="utf-8"))["rules"]
    timeout_rule = next(r for r in fixed_rules if r["rule"] == "on_tool_timeout")
    assert timeout_rule["check"] == new_policy["rules"][0]["check"]
    assert timeout_rule["retry"] == new_policy["rules"][0]["retry"]


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
                str(PASSING_TRACE),
                "--scenario",
                str(SCENARIO_FILE),
                "--state",
                str(PASSING_STATE),
                "--report-out",
                str(passing_verdict),
            ]
        )
        == 0
    )
    out2 = tmp_path / "patch2.json"
    assert (
        diagnose(PASSING_TRACE, out2, "--verdict", str(passing_verdict), state=PASSING_STATE) == 2
    )
    assert not out2.exists()
    assert "No invariant violations to diagnose" in capsys.readouterr().err


def test_diagnose_live_patch_without_source_run_id_is_backfilled(tmp_path) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    model_patch = {
        "schema_version": "policy_patch/v1",
        "rationale": "test double",
        "patch": {
            "rules": [
                {
                    "rule": "on_tool_timeout",
                    "tool": "save_odrl",
                    "action": "check_then_act",
                    "check": {"tool": "save_odrl", "by": "version"},
                    "retry": {"max_attempts": 1, "require_idempotency_key": False},
                }
            ]
        },
    }
    received: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = self.rfile.read(length)
            received["auth"] = self.headers.get("Authorization", "")
            payload = json.dumps(
                {"choices": [{"message": {"content": json.dumps(model_patch)}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        out = tmp_path / "patch.json"
        code = diagnose(
            FAILING_TRACE,
            out,
            state=FAILING_STATE,
            env={"LLAMA_BASE_URL": url, "LLAMA_API_KEY": "test-key"},
        )
    finally:
        server.shutdown()
        thread.join()
    assert code == 0
    assert received["auth"] == "Bearer test-key"
    assert "response_format" not in received["body"].decode()
    assert "policy_patch/v1" in received["body"].decode()
    run_id = json.loads(FAILING_TRACE.read_text(encoding="utf-8").splitlines()[0])["run_id"]
    patch = json.loads(out.read_text(encoding="utf-8"))
    assert patch["source_run_id"] == run_id
    validate_patch_data(patch)
