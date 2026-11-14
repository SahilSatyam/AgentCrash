# AgentCrash reference integrations

Sample hosts prove the framework is reusable. Each owns its workflow;
AgentCrash only observes and injects faults at the boundaries you name.

## Install

```bash
pip install agentcrash-core            # core: SDK + control plane client
pip install "agentcrash-core[server]"  # control-plane service extras
```

## Minimal counter agent (`minimal_agent/app.py`)

Stdlib + `agentcrash` only (enforced by `tests/test_second_app.py`):

```python
from examples.minimal_agent.app import count_twice
count_twice({})
```

With the SDK enabled, the same call records runs, steps, and faults;
`AGENTCRASH_DISABLED=1` restores plain behavior.

## Writing your own host

```python
from agentcrash import AgentCrash, WorkflowConfig

ac = AgentCrash(WorkflowConfig(project="my-workflow", fault_injection=True))
ac.configure_faults({"payments.charge": {"kind": "timeout_after_commit", "occurrence": 1}})

with ac.run(name="my-run") as run:
    with run.step("charge") as step:
        fault = run.maybe_fault("payments.charge")
        ...  # your business logic; AgentCrash never orchestrates it
        step.record_tool("payments.charge", inputs={"order": "o-1"}, outputs={"ok": True})
```

Any framework works (LangGraph, OpenAI Agents SDK, CrewAI, MCP tools,
plain Python): wrap the tool boundary, record it, and judge the trace
with invariants.

## Auth

Set `AGENTCRASH_API_TOKENS` (comma-separated) on the control plane to
require credentials on every `/v1/*` API call; pass the same value as
`api_key` in `WorkflowConfig` (sent as an `Authorization` header).
Unset means loopback mode: all local traffic accepted. Lab static
assets stay public either way.
