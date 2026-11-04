# AgentCrash reference integrations

Sample hosts prove the framework is reusable. Each owns its workflow;
AgentCrash only observes. Domain hosts (ODRL, ...) live in their own
repos — e.g. `nl2odrl-responsible-ai` — and depend on this package.

## Install

```bash
pip install agentcrash-core            # core: SDK + control plane client
pip install "agentcrash-core[server]"  # control-plane service extras
```

## Minimal counter agent (`minimal_agent/app.py`)

Zero ODRL imports (enforced by `tests/test_second_app.py`):

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

with ac.run(name="my-run") as run:
    with run.step("my-step"):
        ...  # your business logic; AgentCrash never orchestrates it
```

See `nl2odrl-responsible-ai` for a full ODRL host built this way.

## Auth

Set `AGENTCRASH_API_TOKENS` (comma-separated) on the control plane to
require credentials on every `/v1/*` API call; pass the same value as
`api_key` in `WorkflowConfig` (sent as an `Authorization` header).
Unset means loopback mode: all local traffic accepted. Lab static
assets stay public either way.
