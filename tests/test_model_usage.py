"""Model token usage is recorded on tool calls and in the run summary."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from agentcrash.adapter import run_react_loop
from agentcrash.cli import load_yaml

REPO = Path(__file__).resolve().parent.parent

SLOPPY = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "permission": [{"action": "use", "assignee": "research-team"}],
}

USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


def _actions():
    return [
        {"type": "call", "tool": "parse_policy", "args": {"doc_id": "access-request-1"}},
        {
            "type": "call",
            "tool": "save_odrl",
            "args": {"op": "write", "record": "odrl-policy-1", "value": SLOPPY},
        },
        {"type": "done", "reason": "saved"},
    ]


def _transport(monkeypatch, actions, usage):
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
        payload = {"choices": [{"message": {"content": json.dumps(action)}}]}
        if usage is not None:
            payload["usage"] = dict(usage)
        return _FakeResponse(payload)

    monkeypatch.setenv("LLAMA_API_KEY", "test-key")
    monkeypatch.setenv("LLAMA_BASE_URL", "https://llama.example/v1")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


def _run(monkeypatch, tmp_path, usage):
    monkeypatch.chdir(REPO)
    scenario, scenario_text = load_yaml(REPO / "scenarios" / "odrl_convert" / "scenario.yaml")
    policy, policy_text = load_yaml(REPO / "policies" / "default.yaml")
    _transport(monkeypatch, _actions(), usage)
    trace = tmp_path / "u.jsonl"
    state = tmp_path / "u.json"
    summary = run_react_loop(
        scenario=scenario,
        policy=policy,
        seed=42,
        trace_path=str(trace),
        state_path=str(state),
        scenario_text=scenario_text,
        policy_text=policy_text,
        max_steps=scenario.get("max_steps", 12),
    )
    return trace, summary


def test_usage_recorded_when_reported(monkeypatch, tmp_path) -> None:
    trace, summary = _run(monkeypatch, tmp_path, USAGE)
    assert summary["model_calls"] == 3
    assert summary["model_usage_total"] == {
        "prompt_tokens": 360,
        "completion_tokens": 90,
        "total_tokens": 450,
    }
    calls = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("detail", {}).get("phase") == "tool_call"
    ]
    assert len(calls) == 2
    assert all(call["detail"]["model_usage"] == USAGE for call in calls)


def test_no_usage_key_when_unreported(monkeypatch, tmp_path) -> None:
    trace, summary = _run(monkeypatch, tmp_path, None)
    assert summary["model_calls"] == 3
    assert summary["model_usage_total"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    assert "model_usage" not in trace.read_text(encoding="utf-8")
