"""SDK contract: workflow decorator, run/step CMs, faults, degradation."""

import asyncio
import os

from agentcrash import AgentCrash, WorkflowConfig


def test_workflow_decorator_sync_captures_run():
    ac = AgentCrash(WorkflowConfig(project="p1", fault_injection=False))

    @ac.workflow(name="wf")
    def host_fn(x):
        return x * 2

    assert host_fn(21) == 42
    assert len(ac.traces) == 1
    run_id = next(iter(ac.traces))
    kinds = [e["kind"] for e in ac.traces[run_id]]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert all(e["project"] == "p1" for e in ac.traces[run_id])


def test_workflow_decorator_async_and_steps():
    ac = AgentCrash(WorkflowConfig(project="p1"))

    @ac.workflow(name="wf")
    async def host_fn():
        return "done"

    assert asyncio.run(host_fn()) == "done"

    with ac.run(name="manual") as run:
        with run.step("a"):
            pass
        run.maybe_fault("nope")  # unconfigured -> None, no event
    events = ac.trace(run.run_id)
    assert [e["kind"] for e in events] == ["run_start", "step_start", "step_end", "run_end"]
    assert events[0]["run_id"] == run.run_id


def test_deterministic_fault_and_redaction():
    ac = AgentCrash(WorkflowConfig(project="p1", fault_injection=True, seed=7))
    ac.configure_faults({"save": {"kind": "timeout_after_commit", "occurrence": 1}})
    with ac.run(name="wf") as run:
        assert run.maybe_fault("save")["kind"] == "timeout_after_commit"
        assert run.maybe_fault("save")["kind"] == "timeout_after_commit"  # repeatable
        assert run.maybe_fault("other") is None
        with run.step("s") as step:
            step.record_tool("t", inputs={"api_key": "secret", "n": 1})
    tool_event = next(e for e in ac.trace(run.run_id) if e["kind"] == "tool")
    assert tool_event["inputs"]["api_key"] == "[REDACTED]"
    assert tool_event["fault"] is None
    fault_events = [e for e in ac.trace(run.run_id) if e["kind"] == "fault"]
    assert len(fault_events) == 2  # one per maybe_fault("save") call


def test_disabled_mode_passthrough():
    os.environ["AGENTCRASH_DISABLED"] = "1"
    try:
        ac = AgentCrash(WorkflowConfig(project="p1"))
        with ac.run(name="wf"):
            pass
        assert ac.traces == {}
    finally:
        del os.environ["AGENTCRASH_DISABLED"]


def test_bounded_queue_drops_oldest():
    ac = AgentCrash(WorkflowConfig(project="p1", max_queue=4))
    with ac.run(name="wf") as run:
        for _ in range(10):
            with run.step("s"):
                pass
    assert ac.exporter.dropped > 0


def test_dead_endpoint_never_breaks_host():
    # Port 1 is unroutable: every flush fails, host still unaffected.
    ac = AgentCrash(WorkflowConfig(project="p1", endpoint="http://127.0.0.1:1"))
    with ac.run(name="wf") as run:
        with run.step("s"):
            pass
        assert ac.exporter.failed == 0  # no network I/O per step
    assert ac.exporter.failed > 0  # run-end flush attempted and counted
    assert ac.exporter.sent == 0
    assert len(ac.trace(run.run_id)) > 0  # local capture intact
