"""Lab migration: evaluations API, Lab static serving, reliability reports."""

import json
import threading
import urllib.request

from agentcrash import (
    AgentCrash,
    RequiredStep,
    WorkflowConfig,
    compare_reports,
    evaluate_trace,
    evaluation_summary,
    ingest_events,
    register_project,
    reset_store,
    run_detail,
    submit_evaluation,
)
from agentcrash.server import serve


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return resp.read(), resp.headers.get_content_type()


def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def test_evaluation_submit_and_run_detail():
    reset_store()
    register_project("odrl-policy-engine")
    ac = AgentCrash(WorkflowConfig(project="odrl-policy-engine", fault_injection=True))
    ac.configure_faults({"x": {"kind": "duplicate_call", "occurrence": 1}})
    with ac.run(name="wf") as run, run.step("validate-odrl"):
        pass
    report = evaluate_trace(
        list(ac.traces[run.run_id]), [RequiredStep("validate-odrl")], run_id=run.run_id
    )
    assert report["verdict"] == "PASS"
    assert ingest_events(list(ac.traces[run.run_id]))["accepted"] > 0
    stored = submit_evaluation(run.run_id, report["results"])
    assert stored["verdict"] == "PASS"
    again = submit_evaluation(run.run_id, report["results"])  # idempotent
    assert again["invariants"] == stored["invariants"]
    detail = run_detail(run.run_id)
    assert detail["evaluation"] is not None
    assert evaluation_summary(run.run_id)["verdict"] == "PASS"


def test_malformed_evaluation_is_client_error():
    reset_store()
    try:
        submit_evaluation("run_missing", "not-a-list")  # type: ignore[arg-type]
    except TypeError as exc:
        assert "must be a list" in str(exc)
    else:
        raise AssertionError("expected TypeError for non-list results")


def test_core_import_pulls_no_domain_stacks():
    reset_store()
    import subprocess
    import sys

    code = (
        "import sys, agentcrash; "
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('rdflib', 'pyshacl', 'dspy', 'fastapi') or m == 'agentcrash.odrl']; "
        "assert not bad, bad; print('clean')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True, cwd="."
    )
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout


def test_compare_reports_flags_regression():
    baseline = {
        "verdict": "PASS",
        "results": [
            {"id": "a", "passed": True, "evidence": "ok"},
            {"id": "b", "passed": True, "evidence": "ok"},
        ],
    }
    candidate = {
        "verdict": "FAIL",
        "results": [
            {"id": "a", "passed": True, "evidence": "ok"},
            {"id": "b", "passed": False, "evidence": "dupe"},
        ],
    }
    diff = compare_reports(baseline, candidate)
    assert diff["regressions"] == ["b"] and diff["regression"] is True
    assert compare_reports(baseline, baseline)["regression"] is False


def test_lab_static_and_evaluation_roundtrip():
    reset_store()
    server, port = serve(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body, ctype = _get(port, "/")
        assert ctype == "text/html" and b"AgentCrash Lab" in body
        body, ctype = _get(port, "/lab/app.js")
        assert ctype == "text/javascript" and b"v1/runs" in body
        assert (
            _post(port, "/v1/projects", {"project": "counter-agent"})["project"] == "counter-agent"
        )
        ac = AgentCrash(WorkflowConfig(project="counter-agent"))
        with ac.run(name="count") as run:
            pass
        posted = _post(port, "/v1/events", {"events": list(ac.traces[run.run_id])})
        assert posted["accepted"] > 0
        verdict = _post(
            port,
            "/v1/evaluations",
            {
                "run_id": run.run_id,
                "results": [{"id": "step-present", "passed": True, "evidence": "run completed"}],
            },
        )
        assert verdict["verdict"] == "PASS"
        detail = json.loads(_get(port, f"/v1/runs/{run.run_id}")[0].decode())
        assert detail["evaluation"][0]["id"] == "step-present"
    finally:
        server.shutdown()
        server.server_close()
