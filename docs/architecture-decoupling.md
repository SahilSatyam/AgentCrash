# AgentCrash architecture: core vs host (v2)

Status: accepted (0.3.0). The core is domain-free; see ADR-009 in
`docs/adlc/08-adr-template.md`.

## 1. Who owns what

| Component | Owner | Notes |
|---|---|---|
| Agent loop, prompts, real tools, business rules | Host workflow | Runs with or without AgentCrash installed |
| Domain invariants (e.g. "no refund over limit") | Host or plugin package | Registered via `register_verifier`, loaded with `check --plugin` |
| SDK (`sdk.py`), events (`events.py`), exporter | Core | Observes and injects; never orchestrates |
| Fault catalog + schedule (`injector.py`) | Core | Faults name no default tool; host names every boundary |
| Trace writer, hash chain, redaction (`tracer.py`), virtual clock (`clock.py`) | Core | Deterministic, no wall-clock in judged paths |
| Generic invariants (`invariants.py`, `verifiers.py`), registry (`_registry.py`) | Core | `required_step`, `duplicate_mutating_effect`, `fresh_write_only`, `no_lost_update` |
| Policy engine (`policy.py`) | Core | `policy/v1`, merge patches, opt-in LLM patch proposal |
| Synthetic versioned store (`ledger.py`) | Core (test double) | For hosts that want a seeded store in tests |
| Control plane (`server.py`, `lab/`) | Core | Loopback stdlib server or FastAPI factory (`[server]` extra) |

## 2. Package layout

```
src/agentcrash/
  __init__.py     # public API: AgentCrash, WorkflowConfig, invariants, server helpers
  sdk.py          # run/step context managers, workflow decorator, exporter, faults
  events.py       # versioned event schema (SCHEMA_VERSION=1)
  injector.py     # fault catalog + deterministic schedule
  tracer.py       # JSONL writer, prev_hash chain, redaction
  clock.py        # virtual clock
  invariants.py   # RequiredStep, DuplicateMutatingEffect
  verifiers.py    # FreshWriteOnly, NoLostUpdate, run_verifiers, loaders
  _registry.py    # Verdict, Verifier protocol, register/get
  evaluate.py     # evaluate_trace, compare_reports
  policy.py       # policy/v1 + policy_patch/v1
  ledger.py       # synthetic versioned store
  server.py       # control plane
  cli.py          # check, serve
  lab/, schemas/  # bundled assets
examples/minimal_agent/  # stdlib + agentcrash only
```

Dependency direction: host → `agentcrash`. Core never imports host code
or heavy stacks at import time; `fastapi` loads lazily.
`tests/test_generic_core.py` fails if domain vocabulary enters `src/`.

## 3. Integration contract (v1)

```python
from agentcrash import AgentCrash, WorkflowConfig

ac = AgentCrash(WorkflowConfig(project="support-agent", fault_injection=True, seed=7))
ac.configure_faults({"crm.update": {"kind": "timeout_after_commit", "occurrence": 1}})

@ac.workflow(name="triage")
async def triage(ticket: str):
    return await run_existing_agent(ticket)  # host-owned

with ac.run(name="triage-manual") as run:
    with run.step("update-crm") as step:
        fault = run.maybe_fault("crm.update")
        step.record_tool("crm.update", inputs={...}, outputs={...})
```

Delivery: at-least-once with idempotent ingestion (`event_id` primary
key; server dedupes). Bounded queue (default 1000, drop-oldest with a
counter); flush on batch threshold or run end; network failures never
reach host code.

Hash chain: integrity signal for accidental loss or reordering, not
tamper resistance (no signatures in v1).

## 4. Risks

- Adapter sprawl: framework adapters ship as optional extras with lazy
  imports (roadmap R1).
- Trace-writer overhead is above the 10% target today (roadmap R7).
