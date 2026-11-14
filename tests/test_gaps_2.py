"""Closes the remaining doc 03 gaps: FR-02/03/08/12/13/14/29/31/39/40/44/46/49/50."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

from agentcrash import AgentCrash, WorkflowConfig, ingest_events, reset_store
from agentcrash.events import make_event, validate_event
from agentcrash.injector import FaultSchedule, UnknownFaultError, validate_fault_entry
from agentcrash.invariants import DuplicateMutatingEffect, RequiredStep
from agentcrash.policy import (
    PolicyError,
    PolicyRejectedError,
    diff_policies,
    fetch_live_patch,
    load_policy,
    validate_policy_data,
)
from agentcrash.server import serve
from agentcrash.verifiers import (
    FreshWriteOnly,
    NoLostUpdate,
    UnknownVerifierError,
    Verdict,
    VerifierError,
    register_verifier,
    run_verifiers,
)

# -- FR-02: workflow decorator error path ----------------------------------


def test_workflow_error_propagates_and_is_recorded():
    ac = AgentCrash(WorkflowConfig(project="p"))

    @ac.workflow(name="sync")
    def boom():
        raise ValueError("bad input")

    @ac.workflow(name="async")
    async def aboom():
        raise RuntimeError("down")

    with pytest.raises(ValueError, match="bad input"):
        boom()
    with pytest.raises(RuntimeError, match="down"):
        asyncio.run(aboom())
    ends = [e for events in ac.traces.values() for e in events if e["kind"] == "run_end"]
    assert sorted(e["error"] for e in ends) == [
        "RuntimeError: down",
        "ValueError: bad input",
    ]
    assert all(e["status"] == "error" for e in ends)
    assert not any(e["kind"] == "result" for events in ac.traces.values() for e in events)


def test_workflow_result_is_truncated_to_2000_chars():
    ac = AgentCrash(WorkflowConfig(project="p"))

    @ac.workflow(name="big")
    def big():
        return "x" * 5000

    assert big() == "x" * 5000  # host sees the full value
    result = next(e for es in ac.traces.values() for e in es if e["kind"] == "result")
    assert len(result["outputs"]["value"]) == 2000


# -- FR-03: enabled=False ---------------------------------------------------


def test_enabled_false_is_a_full_passthrough():
    ac = AgentCrash(WorkflowConfig(project="p", enabled=False, fault_injection=True))
    ac.configure_faults({"pay": {"kind": "timeout_after_commit", "occurrence": 1}})

    @ac.workflow(name="wf")
    def host(x):
        return x + 1

    assert host(1) == 2
    with ac.run(name="manual") as run, run.step("s") as step:
        step.record_tool("pay", inputs={"n": 1})
        assert run.maybe_fault("pay") is None  # no faults when disabled
    assert run.run_id.startswith("disabled_")
    assert ac.traces == {}
    assert ac.exporter.drain() == []


# -- FR-08 / FR-39: event validation and invalid ingestion ------------------


def _event(**overrides):
    event = make_event(event_id="e1", run_id="r1", project="p", workflow="w", kind="tool", seq=1)
    event.update(overrides)
    return event


def test_validate_event_reports_each_problem():
    assert validate_event(_event()) == []
    missing = _event()
    del missing["run_id"]
    assert "missing key 'run_id'" in validate_event(missing)
    assert any("schema_version" in p for p in validate_event(_event(schema_version=2)))
    for bad_seq in (0, -1, "1", None):
        assert "seq must be a positive int" in validate_event(_event(seq=bad_seq))


def test_ingestion_counts_and_drops_invalid_events():
    reset_store()
    batch = [
        "not-an-object",
        {"no": "event_id"},
        _event(event_id="bad-seq", seq=0),
        _event(event_id="bad-version", schema_version=9),
        _event(event_id="ok-1"),
    ]
    result = ingest_events(batch)  # type: ignore[arg-type]
    assert (result["accepted"], result["invalid"], result["duplicate"]) == (1, 4, 0)
    assert ingest_events([_event(event_id="ok-1")])["duplicate"] == 1


# -- FR-12 / FR-13: FaultSchedule refusal and matching ----------------------


def test_fault_schedule_refuses_unknown_kinds_and_params():
    with pytest.raises(UnknownFaultError, match="known kinds: timeout_after_commit"):
        FaultSchedule.from_scenario({"faults": [{"kind": "alien_fault", "at_step": 2}]})
    with pytest.raises(UnknownFaultError, match="must be a list"):
        FaultSchedule.from_scenario({"faults": "timeout"})
    with pytest.raises(UnknownFaultError, match="unknown params: colour"):
        validate_fault_entry("timeout_after_commit", {"colour": "red"})
    with pytest.raises(UnknownFaultError, match="unknown fault kind"):
        validate_fault_entry("alien_fault", {})
    with pytest.raises(UnknownFaultError, match="must be a string"):
        validate_fault_entry(7, {})
    with pytest.raises(UnknownFaultError, match="must be an object"):
        validate_fault_entry("timeout_after_commit", [])


def test_fault_schedule_matches_by_step_and_by_call_count():
    schedule = FaultSchedule.from_scenario(
        {
            "faults": [
                {"kind": "timeout_after_commit", "at_step": 3, "params": {"tool": "pay"}},
                {
                    "kind": "timeout_before_commit",
                    "at_step": None,
                    "params": {"tool": "kv", "op": "write", "occurrence": 2},
                },
                {
                    "kind": "concurrent_update",
                    "at_step": None,
                    "params": {"tool": "kv", "op": "read", "occurrence": 1},
                },
                {"kind": "poisoned_doc", "at_step": 1, "params": {"tool": "fetch"}},
            ]
        }
    )
    # at_step trigger, constrained by tool
    assert schedule.fault_for(3, "pay")["kind"] == "timeout_after_commit"
    assert schedule.fault_for(3, "kv") is None
    assert schedule.fault_for(2, "pay") is None
    assert schedule.fault_for(1, "fetch") is None  # inert kinds never fire as timeouts
    # call-count trigger, constrained by tool and op
    assert schedule.fault_for_call("kv", "write", 1) is None
    assert schedule.fault_for_call("kv", "write", 2)["kind"] == "timeout_before_commit"
    assert schedule.fault_for_call("kv", "read", 2) is None
    assert schedule.fault_for_call("pay", "write", 2) is None
    # rival writer before the first read
    assert schedule.concurrent_for_call("kv", "read", 1)["kind"] == "concurrent_update"
    assert schedule.concurrent_for_call("kv", "write", 1) is None


# -- FR-14: fault_injection=False -------------------------------------------


def test_faults_never_fire_without_fault_injection():
    ac = AgentCrash(WorkflowConfig(project="p", fault_injection=False))
    ac.configure_faults({"pay": {"kind": "timeout_after_commit", "occurrence": 1}})
    with ac.run(name="wf") as run:
        assert run.maybe_fault("pay") is None
    assert not any(e["kind"] == "fault" for e in ac.trace(run.run_id))
    assert run.faults_fired == []


# -- FR-29 / FR-31: run_verifiers error paths and purity --------------------


class _BadVerdict:
    id = "bad_verdict"

    def check(self, trace, state):
        return {"passed": True}  # not a Verdict


class _WrongId:
    id = "wrong_id"

    def check(self, trace, state):
        return Verdict("someone_else", True, "ok")


def test_run_verifiers_refuses_unknown_and_malformed_verdicts():
    register_verifier(_BadVerdict())
    register_verifier(_WrongId())
    with pytest.raises(UnknownVerifierError, match="unknown invariant 'nope'"):
        run_verifiers([], {}, ["nope"])
    with pytest.raises(VerifierError, match="bad verdict object"):
        run_verifiers([], {}, ["bad_verdict"])
    with pytest.raises(VerifierError, match="bad verdict object"):
        run_verifiers([], {}, ["wrong_id"])
    with pytest.raises(VerifierError, match="must name an id string"):
        run_verifiers([], {}, [{"id": 3}])
    with pytest.raises(VerifierError):
        DuplicateMutatingEffect().check("not-a-list", {})  # type: ignore[arg-type]
    with pytest.raises(VerifierError):
        FreshWriteOnly().check([1, 2], {})  # type: ignore[list-item]


def test_run_verifiers_accepts_id_mappings_and_aggregates():
    trace = [{"step": "a", "kind": "tool", "status": "ok", "tool": "t", "inputs": {}}]
    register_verifier(RequiredStep("a"))
    register_verifier(RequiredStep("b"))
    out = run_verifiers(trace, {}, ["required_step:a", {"id": "required_step:b"}])
    assert out["verdict"] == "FAIL"
    assert [r["passed"] for r in out["results"]] == [True, False]


def test_builtin_verifiers_are_pure(monkeypatch):
    """No network, no clock, no randomness, and no mutation of inputs."""
    import random
    import socket
    import time

    def forbidden(*args, **kwargs):
        raise AssertionError("verifier touched a forbidden resource")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "monotonic", forbidden)
    monkeypatch.setattr(random, "random", forbidden)
    trace = [
        {
            "seq": 1,
            "step": "s",
            "kind": "tool",
            "status": "ok",
            "tool": "kv",
            "inputs": {"op": "write", "expected_version": 1},
            "outputs": {"conflict": False},
        },
        {
            "seq": 2,
            "step": "s",
            "kind": "fault",
            "status": "ok",
            "fault": {"kind": "concurrent_update", "key": "r", "updates": {"a": 1}},
        },
    ]
    state = {"record_versions": [{"record": "r", "value": {"a": 1}}]}
    before = json.dumps([trace, state], sort_keys=True)
    verifiers = [
        RequiredStep("s"),
        DuplicateMutatingEffect(),
        FreshWriteOnly(["kv"]),
        NoLostUpdate(),
    ]
    first = [v.check(trace, state).to_dict() for v in verifiers]
    second = [v.check(trace, state).to_dict() for v in verifiers]
    assert first == second
    assert json.dumps([trace, state], sort_keys=True) == before


# -- FR-40: FastAPI auth path ----------------------------------------------


def test_fastapi_app_enforces_bearer_tokens(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agentcrash.server import create_fastapi_app

    reset_store()
    monkeypatch.setenv("AGENTCRASH_API_TOKENS", "t1, t2")
    client = TestClient(create_fastapi_app())
    assert client.post("/v1/projects", json={"project": "p"}).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/v1/projects", json={"project": "p"}, headers=bad).status_code == 401
    good = {"Authorization": "Bearer t2"}
    ok = client.post("/v1/projects", json={"project": "p"}, headers=good)
    assert ok.status_code == 200 and ok.json()["project"] == "p"
    for path in ("/v1/runs", "/v1/reliability/p", "/v1/runs/x", "/v1/runs/x/evaluation"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=good).status_code in (200, 404), path
    assert client.get("/lab/index.html").status_code == 200  # Lab stays public

    monkeypatch.delenv("AGENTCRASH_API_TOKENS")
    assert TestClient(create_fastapi_app()).get("/v1/runs").status_code == 200  # loopback mode


# -- FR-44: Lab traversal ----------------------------------------------------


def test_lab_rejects_traversal_and_unknown_assets():
    server, port = serve(port=0)
    assert server.server_address[0] == "127.0.0.1"  # loopback by default
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def status(path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        assert status("/lab") == 200
        assert status("/lab/app.js") == 200
        for path in (
            "/lab/../server.py",
            "/lab/..%2Fserver.py",
            "/lab/%2e%2e/server.py",
            "/lab/..\\server.py",
            "/lab/sub/app.js",
            "/lab/server.py",
            "/lab/missing.js",
            "/schemas/policy.json",
        ):
            assert status(path) == 404, path
    finally:
        server.shutdown()
        server.server_close()


def test_send_lab_blocks_backslash_escape_to_a_real_file(tmp_path, monkeypatch):
    """Only the ``..``/``\\`` guard stops this name: suffix ok, no ``/``, target exists."""
    from agentcrash import server as server_mod

    lab = tmp_path / "lab"
    lab.mkdir()
    (tmp_path / "secret.html").write_text("<p>secret</p>", encoding="utf-8")
    monkeypatch.setattr(server_mod, "LAB_DIR", lab)
    sent = []

    class Probe(server_mod._Handler):
        def __init__(self):  # no socket
            pass

        def _send(self, code, payload):
            sent.append(code)

        def send_response(self, code, message=None):
            sent.append(code)

    Probe()._send_lab("..\\secret.html")
    assert sent == [404]


def test_send_lab_guards_names_directly():
    """Unit-level check of the asset guard, independent of routing."""
    from agentcrash.server import _Handler

    sent = []

    class Probe(_Handler):
        def __init__(self):  # no socket
            pass

        def _send(self, code, payload):
            sent.append(code)

    probe = Probe()
    for name in ("../server.py", "a/b.js", "x\\y.js", "server.py", "", "nope.css"):
        probe._send_lab(name)
    assert sent == [404] * 6


# -- FR-46: policy loading ----------------------------------------------------

VALID = """\
schema_version: policy/v1
rules:
  - rule: on_tool_timeout
    tool: payments.charge
    action: check_then_act
    retry: {max_attempts: 1, require_idempotency_key: true}
"""


def test_load_policy_accepts_valid_and_rejects_invalid(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text(VALID, encoding="utf-8")
    data, text = load_policy(good)
    assert data["rules"][0]["action"] == "check_then_act" and text == VALID

    cases = {
        "missing.yaml": None,
        "malformed.yaml": "rules: [unclosed",
        "list.yaml": "- 1\n- 2\n",
        "version.yaml": VALID.replace("policy/v1", "policy/v2"),
        "rule.yaml": VALID.replace("on_tool_timeout", "on_full_moon"),
        "action.yaml": VALID.replace("check_then_act", "time_travel"),
        "retries.yaml": VALID.replace("max_attempts: 1", "max_attempts: 9"),
        "no_tool.yaml": VALID.replace("    tool: payments.charge\n", ""),
    }
    for name, body in cases.items():
        path = tmp_path / name
        if body is not None:
            path.write_text(body, encoding="utf-8")
        with pytest.raises(PolicyError):
            load_policy(path)
    with pytest.raises(PolicyError, match="must contain a YAML mapping"):
        validate_policy_data(None)


# -- FR-49: diff_policies -----------------------------------------------------


def test_diff_policies_renders_added_removed_changed_unchanged():
    old = {
        "rules": [
            {"rule": "on_tool_timeout", "tool": "pay", "action": "retry_same"},
            {"rule": "on_stale_read", "tool": "kv", "action": "fail"},
            {"rule": "on_untrusted_content", "tool": "fetch", "action": "fail"},
        ]
    }
    new = {
        "rules": [
            {
                "rule": "on_tool_timeout",
                "tool": "pay",
                "action": "check_then_act",
                "retry": {"max_attempts": 1},
            },
            {"rule": "on_untrusted_content", "tool": "fetch", "action": "fail"},
            {"rule": "on_mutating_retry", "tool": "crm.update", "action": "require_confirmation"},
        ]
    }
    lines = diff_policies(old, new).splitlines()
    assert lines[0] == "policy diff (3 rule(s) after patch):"
    assert '~ on_tool_timeout/pay: action "retry_same" -> "check_then_act"' in lines
    assert "~ on_tool_timeout/pay: retry.max_attempts (absent) -> 1" in lines
    assert "- on_stale_read/kv (removed)" in lines
    assert "= on_untrusted_content/fetch (unchanged)" in lines
    assert any(line.startswith("+ on_mutating_retry/crm.update: ") for line in lines)


# -- FR-50: live patch transport and output gating ---------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _live(monkeypatch, *, content=None, payload=None, raises=None):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data)
        if raises is not None:
            raise raises
        body = payload if payload is not None else {"choices": [{"message": {"content": content}}]}
        return _FakeResponse(body)

    monkeypatch.setenv("AGENTCRASH_LLM_BASE_URL", "https://llm.invalid/v1/")
    monkeypatch.setenv("AGENTCRASH_LLM_API_KEY", "sk-test")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = fetch_live_patch(
        trace_summary="t", verdict_summary="v", base_policy={}, model="m", source_run_id="run_9"
    )
    return out, seen


GOOD_PATCH = {
    "schema_version": "policy_patch/v1",
    "rationale": "reconcile first",
    "patch": {"rules": []},
}


def test_live_patch_accepts_valid_json_even_inside_prose(monkeypatch):
    content = "Here you go:\n" + json.dumps(GOOD_PATCH) + "\nThanks."
    out, seen = _live(monkeypatch, content=content)
    assert out["source_run_id"] == "run_9"  # backfilled by the harness
    assert seen["url"] == "https://llm.invalid/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "m"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": "I cannot help with that."},  # no JSON at all
        {"content": "{not json"},
        {"content": json.dumps({**GOOD_PATCH, "schema_version": "v9"})},
        {"payload": {"choices": []}},  # malformed envelope
        {"payload": {"unexpected": True}},
    ],
)
def test_live_patch_rejects_unparsable_or_invalid_output(monkeypatch, kwargs):
    with pytest.raises(PolicyRejectedError):
        _live(monkeypatch, **kwargs)


def test_live_patch_transport_failure_returns_none(monkeypatch):
    out, _ = _live(monkeypatch, raises=urllib.error.URLError("down"))
    assert out is None


def test_live_patch_legacy_env_names_still_work(monkeypatch):
    monkeypatch.delenv("AGENTCRASH_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AGENTCRASH_LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLAMA_BASE_URL", "https://legacy.invalid")
    monkeypatch.setenv("LLAMA_API_KEY", "k")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return _FakeResponse({"choices": [{"message": {"content": json.dumps(GOOD_PATCH)}}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetch_live_patch(
        trace_summary="t", verdict_summary="v", base_policy={}, model="m", source_run_id="r"
    )
    assert seen == {"url": "https://legacy.invalid/chat/completions", "auth": "Bearer k"}
