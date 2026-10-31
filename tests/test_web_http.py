"""HTTP-level round trip through the real handler (scripted transport)."""

from __future__ import annotations

import http.server
import json
import threading
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SLOPPY = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "permission": [{"action": "use", "assignee": "research-team"}],
}


def _transport(monkeypatch, actions):
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
        action = actions[idx] if idx < len(actions) else {"type": "done", "reason": "end"}
        state["i"] += 1
        return _FakeResponse({"choices": [{"message": {"content": json.dumps(action)}}]})

    monkeypatch.setenv("LLAMA_API_KEY", "test-key")
    monkeypatch.setenv("LLAMA_BASE_URL", "https://llama.example/v1")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


def _clean_naive_actions():
    parse = {"type": "call", "tool": "parse_policy", "args": {"doc_id": "pasted-request-1"}}
    write = {
        "type": "call",
        "tool": "save_odrl",
        "args": {"op": "write", "record": "odrl-policy-1", "value": SLOPPY},
    }
    return [parse, write, {"type": "done", "reason": "saved"}]


def _server(monkeypatch, tmp_path):
    real_urlopen = urllib.request.urlopen
    _transport(monkeypatch, _clean_naive_actions())

    from agentcrash import web

    registry = web.RunRegistry(REPO, out_dir=tmp_path)
    web.ConvertHandler.registry = registry
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), web.ConvertHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, real_urlopen


def test_http_convert_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(REPO)
    server, port, real_urlopen = _server(monkeypatch, tmp_path)
    try:
        payload = json.dumps(
            {
                "nl_text": "Allow the research team to use dataset-7.",
                "faults": [],
                "policy_variant": "naive",
                "seed": 42,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/convert", data=payload, method="POST"
        )
        with real_urlopen(request, timeout=10) as response:
            assert response.status == 200
            started = json.loads(response.read().decode("utf-8"))
        assert started["status"] == "running"

        from agentcrash import web

        final = web.ConvertHandler.registry.wait(started["run_key"], timeout=30.0)
        assert final is not None and final["status"] == "done"
        assert final["verdict"]["verdict"] == "FAIL"
        assert {r["id"] for r in final["verdict"]["results"] if not r["passed"]} >= {
            "odrl_valid",
            "fresh_write_only",
        }
        assert final["patch"] is not None

        with real_urlopen(
            f"http://127.0.0.1:{port}/api/runs/{started['run_key']}", timeout=30
        ) as response:
            via_http = json.loads(response.read().decode("utf-8"))
        assert via_http["verdict"]["verdict"] == "FAIL"
        assert via_http["events"] == final["events"]

        with real_urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=10) as response:
            listed = json.loads(response.read().decode("utf-8"))
        assert any(r["run_key"] == started["run_key"] for r in listed["runs"])

        with real_urlopen(f"http://127.0.0.1:{port}/api/odrl?valid=false", timeout=10) as response:
            pairs = json.loads(response.read().decode("utf-8"))
        assert any(p["run_key"] == started["run_key"] for p in pairs["pairs"])

        with real_urlopen(f"http://127.0.0.1:{port}/api/faults", timeout=10) as response:
            catalog = json.loads(response.read().decode("utf-8"))
        assert {f["kind"] for f in catalog["faults"]} >= {
            "timeout_after_commit",
            "timeout_before_commit",
            "concurrent_update",
        }
    finally:
        from agentcrash import web

        server.shutdown()
        server.server_close()
        web.ConvertHandler.registry = None
