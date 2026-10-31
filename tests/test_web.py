"""Convert API: validation, catalog, background run, verdict, patch (scripted)."""

from __future__ import annotations

import pytest

from agentcrash.web import (
    ConvertError,
    RunRegistry,
    build_inline_run,
    normalize_faults,
    validate_convert,
)


def test_validate_convert_rejects_bad_input() -> None:
    with pytest.raises(ConvertError):
        validate_convert({})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "   "})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "x" * 4001})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "ok", "fault": "alien"})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "ok", "policy_variant": "maybe"})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "ok", "seed": -1})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "ok", "max_steps": 99})
    assert validate_convert({"nl_text": "  Allow X.  "})[0] == "Allow X."


def test_normalize_rejects_unknown_and_inline_unsupported() -> None:
    from agentcrash.web import ConvertError

    with pytest.raises(ConvertError):
        normalize_faults([{"kind": "alien_fault"}])
    with pytest.raises(ConvertError):
        normalize_faults([{"kind": "concurrent_update", "params": {"bogus": 1}}])
    with pytest.raises(ConvertError):
        normalize_faults([{"kind": "poisoned_doc"}])


def test_normalize_fills_defaults() -> None:
    entries = normalize_faults([{"kind": "timeout_after_commit"}])
    assert entries[0]["kind"] == "timeout_after_commit"
    assert entries[0]["params"]["tool"] == "save_odrl"
    assert entries[0]["params"]["occurrence"] == 1


def test_build_inline_run_composes_faults_and_invariants(tmp_path) -> None:
    from agentcrash.web import PRESETS

    assert PRESETS["none"] == []
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    faults = normalize_faults([{"kind": "timeout_after_commit"}, {"kind": "concurrent_update"}])
    first = build_inline_run("Allow X.", faults, "naive", 42, 12, root, 1, tmp_path)
    second = build_inline_run("Allow X.", faults, "naive", 42, 12, root, 2, tmp_path)
    assert first["run_id"] != second["run_id"]
    assert first["patch_scenario_id"] == "odrl_convert_timeout"
    assert first["policy_file"] == "policies/default.yaml"
    assert first["scenario"]["faults"] and len(first["scenario"]["faults"]) == 2
    assert any(i.get("id") == "no_lost_update" for i in first["scenario"]["invariants"])
    assert first["scenario"]["environment"]["nl_docs"][0]["text"] == "Allow X."
    assert "author the ODRL" in first["scenario"]["task"]
    fixed = build_inline_run("Allow X.", faults, "fixed", 42, 12, root, 1, tmp_path)
    assert fixed["policy_file"] == "policies/fixed-odrl-timeout.yaml"


def test_fault_catalog_lists_executable_kinds() -> None:
    from agentcrash.web import fault_catalog

    kinds = {f["kind"] for f in fault_catalog()}
    assert {"timeout_after_commit", "timeout_before_commit", "concurrent_update"} <= kinds
    by_kind = {f["kind"]: f for f in fault_catalog()}
    assert by_kind["concurrent_update"]["inline_supported"] is True
    assert by_kind["poisoned_doc"]["inline_supported"] is False


SLOPPY_DOC = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "permission": [{"action": "use", "assignee": "research-team"}],
}


def _scripted(monkeypatch, actions):
    import json
    import urllib.request

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


def _timeout_naive_actions():
    parse = {"type": "call", "tool": "parse_policy", "args": {"doc_id": "pasted-request-1"}}
    write = {
        "type": "call",
        "tool": "save_odrl",
        "args": {"op": "write", "record": "odrl-policy-1", "value": SLOPPY_DOC},
    }
    return [parse, write, write, {"type": "done", "reason": "saved"}]


def test_convert_flow_reports_verdict_and_patch(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    _scripted(monkeypatch, _timeout_naive_actions())
    root = Path(__file__).resolve().parent.parent
    registry = RunRegistry(root, out_dir=tmp_path)
    faults = normalize_faults([{"kind": "timeout_after_commit"}])
    started = registry.start("Allow the team to use dataset-7.", faults, "naive", 42, 12)
    assert started["status"] == "running"
    done = registry.wait(started["run_key"], timeout=30.0)
    assert done is not None and done["status"] == "done"
    assert done["verdict"]["verdict"] == "FAIL"
    assert {r["id"] for r in done["verdict"]["results"] if not r["passed"]} >= {
        "odrl_valid",
        "exactly_once_save",
    }
    assert done["patch"] is not None
    assert done["patch"]["patch"]["rules"], "reference patch must carry rules"
    assert done["chain_ok"] is True
    assert done["model_calls"] == 0
    assert done["usage_total"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    assert len(done["events"]) > 4
    assert done["fault_summary"] == ["timeout_after_commit"]


def test_convert_without_model_key_reports_error(monkeypatch, tmp_path) -> None:
    from pathlib import Path as _P

    monkeypatch.chdir(_P(__file__).resolve().parent.parent)
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_BASE_URL", raising=False)
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    registry = RunRegistry(root, out_dir=tmp_path)
    faults = normalize_faults([])
    started = registry.start("Allow the team to use dataset-7.", faults, "naive", 42, 12)
    done = registry.wait(started["run_key"], timeout=30.0)
    assert done is not None and done["status"] == "error"
    assert done["error"]["code"] == "E_MODEL_API"
