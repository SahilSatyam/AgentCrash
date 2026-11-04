"""Second sample: minimal counter agent. Zero ODRL imports by construction."""

from __future__ import annotations

from typing import Any

from agentcrash import AgentCrash, WorkflowConfig

ac = AgentCrash(WorkflowConfig(project="counter-agent", environment="test", fault_injection=True))


def _counter_tool(state: dict[str, int]) -> int:
    state["n"] = state.get("n", 0) + 1
    return state["n"]


@ac.workflow(name="count-twice")
def count_twice(state: dict[str, int] | None = None) -> dict[str, Any]:
    """Host-owned toy workflow observed by the SDK."""
    state = dict(state or {})
    with ac.run(name="count-twice-manual") as run, run.step("mutating-increment") as step:
        first = _counter_tool(state)
        step.record_tool("counter.increment", inputs={}, outputs={"n": first})
        fault = run.maybe_fault("counter.increment")
        if fault and fault.get("kind") == "duplicate_call":
            second = _counter_tool(state)  # injected duplicate side effect
            step.record_tool("counter.increment", inputs={}, outputs={"n": second})
    return {"state": state, "run_id": run.run_id}
