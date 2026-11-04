"""Control-plane contract: idempotent ingestion, runs, reliability."""

import json
import threading
import urllib.request

from agentcrash import (
    AgentCrash,
    WorkflowConfig,
    ingest_events,
    register_project,
    reliability_summary,
    reset_store,
    run_detail,
)
from agentcrash.server import serve


def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return json.load(resp)


def _code(port, path, payload=None, credential=None):
    """HTTP status for a request, with optional auth."""
    headers = {"Content-Type": "application/json"}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5):
            return 200
    except urllib.error.HTTPError as exc:
        return exc.code


def test_idempotent_ingestion_and_run_detail():
    reset_store()
    register_project("odrl-policy-engine")
    ac = AgentCrash(WorkflowConfig(project="odrl-policy-engine"))
    with ac.run(name="nl-to-odrl") as run, run.step("parse-policy"):
        pass
    batch = list(ac.traces[run.run_id])
    first = ingest_events(batch)
    assert first["accepted"] == len(batch) and first["duplicate"] == 0
    second = ingest_events(batch)  # replay -> deduped
    assert second["accepted"] == 0 and second["duplicate"] == len(batch)
    detail = run_detail(run.run_id)
    assert detail["project"] == "odrl-policy-engine"
    assert [e["seq"] for e in detail["events"]] == sorted(e["seq"] for e in detail["events"])
    assert reliability_summary("odrl-policy-engine")["runs"] == 1


def test_http_roundtrip_multi_project():
    reset_store()
    server, port = serve(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert (
            _post(port, "/v1/projects", {"project": "counter-agent"})["project"] == "counter-agent"
        )
        ac = AgentCrash(WorkflowConfig(project="counter-agent"))
        with ac.run(name="count-twice") as run, run.step("mutating-increment"):
            pass
        result = _post(port, "/v1/events", {"events": list(ac.traces[run.run_id])})
        assert result["duplicate"] == 0
        assert result["accepted"] > 0
        detail = _get(port, f"/v1/runs/{run.run_id}")
        assert detail["project"] == "counter-agent"
        summary = _get(port, "/v1/reliability/counter-agent")
        assert summary["runs"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_prune_enforces_retention():
    from agentcrash import prune_runs

    reset_store()
    register_project("p")
    ac = AgentCrash(WorkflowConfig(project="p"))
    run_ids = []
    for _ in range(3):
        with ac.run(name="w") as run, run.step("s"):
            pass
        run_ids.append(run.run_id)
    assert ingest_events([e for r in run_ids for e in ac.traces[r]])["accepted"] > 0
    assert prune_runs("p", 2) == {"project": "p", "dropped": 1, "kept": 2}
    assert run_detail(run_ids[0]) is None
    assert run_detail(run_ids[2]) is not None
    assert reliability_summary("p")["runs"] == 2
    try:
        prune_runs("p", -1)
    except ValueError as exc:
        assert "non-negative" in str(exc)
    else:
        raise AssertionError("expected ValueError for negative keep_last_n")
    try:
        prune_runs("p")
    except ValueError as exc:
        assert "and/or" in str(exc)
    else:
        raise AssertionError("expected ValueError without criteria")
    aged = prune_runs("p", older_than_days=0)
    assert aged == {"project": "p", "dropped": 2, "kept": 0}
    assert reliability_summary("p")["runs"] == 0


def test_prune_over_http_requires_fields():
    reset_store()
    server, port = serve(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _code(port, "/v1/admin/prune", {"project": "p"}) == 400
        out = _post(port, "/v1/admin/prune", {"project": "p", "keep_last_n": 5})
        assert out == {"project": "p", "dropped": 0, "kept": 0}
        out = _post(port, "/v1/admin/prune", {"project": "p", "older_than_days": 30})
        assert out == {"project": "p", "dropped": 0, "kept": 0}
    finally:
        server.shutdown()
        server.server_close()


def test_auth_allowlist_gates_api_but_not_lab(monkeypatch):
    reset_store()
    monkeypatch.setenv("AGENTCRASH_API_TOKENS", "s3cret")
    server, port = serve(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _code(port, "/v1/projects", {"project": "p"}) == 401
        assert _code(port, "/v1/projects", {"project": "p"}, credential="nope") == 401
        assert _code(port, "/v1/projects", {"project": "p"}, credential="s3cret") == 200
        assert _code(port, "/") == 200  # Lab stays public
        assert _code(port, "/v1/runs?project=p", credential="s3cret") == 200
    finally:
        server.shutdown()
        server.server_close()
