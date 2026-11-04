# AgentCrash Decoupling Architecture (v1)

Status: accepted for implementation. Current monolith: `src/agentcrash/` mixes
host business logic with the reliability framework; `web.py`+`ui/` serve the
NL-to-ODRL app and Agent Lab from one loopback server backed by `db.py`.

## 1. Component classification

| Component (current path) | Owner after split | Notes |
|---|---|---|
| NL→ODRL conversion, ReAct loop (`adapter.py`) | Host app | Business orchestration; must run without AgentCrash |
| Synthetic ODRL tools (`tools.py`), ODRL validation (`odrl.py`), SHACL shapes (`schemas/odrl-*.ttl`), prompt packs (`dspy_opt.py`, `prompts/`) | Host app / ODRL extension | Domain-specific; core must not import `rdflib`/`pyshacl`/`dspy` |
| Trace writer + hash chain + redaction (`tracer.py`), seed (`seed.py`), clock (`clock.py`) | SDK runtime | Generic; keep, add versioned event schema |
| Fault catalog + schedule (`injector.py`), policy file (`policy.py`), ledger (`ledger.py` as test double) | SDK runtime (generic) + host-provided stores | Generalize params; ledger becomes a host-side test double behind a `StateStore` protocol |
| Verifier registry + `Verdict` (`verifiers.py` core) | SDK runtime | Keep `Verifier` protocol, `register/get`, strict validation |
| ODRL invariants (`odrl_valid`, `no_attacker_grant`, `exactly_once_save`, `fresh_write_only`, `no_lost_update`) | ODRL extension (`agentcrash.extensions.odrl`) | Host/extension registers them; core ships only domain-agnostic checks (chain integrity, duplicate mutating effects, missing tool call) |
| SQLite query layer (`db.py`), run registry, trace/state artifact paths (`traces/`, `reports/`) | Control plane | Multi-project; idempotent ingestion |
| Loopback server (`web.py`) + `ui/` | Split: host API stays with host; Agent Lab moves to control plane | No shared server; SDK talks to control plane over versioned HTTP, never shared memory |
| CLI (`cli.py`) | Split: `agentcrash` CLI keeps `check`/`replay`/`serve` (control plane); host gets its own `odrl-host` CLI | |

## 2. Target package structure

```
src/agentcrash/                 # pip install agentcrash — framework only
  __init__.py                   # exports AgentCrash, WorkflowConfig, verifiers, __version__
  sdk.py                      # AgentCrash, workflow decorator, run/step CMs, buffered exporter
  events.py                   # versioned event schema (SCHEMA_VERSION=1), make/validate
  faults.py                   # generic FaultPolicy, deterministic seeded decisions
  invariants.py                 # Invariant protocol (alias of Verifier), Evaluation report helpers
  exporter.py                 # bounded queue, batching, backpressure, graceful degradation
  server.py                   # control-plane app (stdlib http + optional FastAPI factory)
  extensions/odrl_verifiers.py  # ODRL invariants; NOT imported by core
examples/
  odrl_host/app.py              # independent NL-to-ODRL engine; works with SDK disabled
  minimal_agent/app.py          # second sample (counter tool); zero ODRL imports
docs/architecture-decoupling.md # this file
```

Dependency direction: `examples/*` → `agentcrash` (optional). Core never
imports `examples`, `odrl`, `dspy`, `pyshacl`, `fastapi` at module import time
(all lazy/optional).

## 3. Integration contract (v1)

```python
from agentcrash import AgentCrash, WorkflowConfig

ac = AgentCrash(WorkflowConfig(project="odrl-policy-engine",
                               environment="development",
                               fault_injection=True))

@ac.workflow(name="nl-to-odrl")
async def convert_policy(policy_text: str):
    return await run_existing_agent(policy_text)  # host-owned

with ac.run(name="policy-conversion") as run:
    with run.step("parse-policy"):
        ...  # sync CM; async variant: async with run.astep(...)
```

Delivery semantics: at-least-once with idempotent ingestion (`event_id`
primary key; server dedupes). Bounded queue (default 1000, drop-oldest with
counter); background flush; never blocks host on network failure.

Hash chain: integrity signal only (gapless `event_id`, monotonic `ts`,
`prev_hash` linkage). Threat model in `sdk.py`: detects accidental loss /
reordering, NOT malicious tampering (no keys/signatures in v1).

## 4. Migration strategy

Phase 1 (this change): additive only. New modules + examples + tests; zero
changes to existing modules except `__init__.py` exports and `pyproject`
extras. All 136 existing tests must stay green.
Phase 2+: move ODRL loop/tools out of core into `examples/odrl_host` with
re-export shims; FastAPI bearer-auth; retention/redaction config.

## 5. Risks

- Import-time `pyshacl`/`dspy` cost in core → mitigated: new modules import
  neither; server factory imports FastAPI lazily.
- `web.py`/`db.py` single-project schema → server adds `project` column with
  default so existing rows keep working.
- Breaking `verifiers` imports → mitigated: registry stays; ODRL verifiers
  remain registered by `verifiers.py` in v1, duplicated into extension with a
  deprecation note (removal later, not now).

## 6. Acceptance (this phase)

1. `pip install -e .` works; `from agentcrash import AgentCrash` works.
2. `examples/odrl_host` runs with `AGENTCRASH_DISABLED=1` (no SDK).
3. SDK captures a full trace offline (no server) with run/step/tool events.
4. Deterministic fault forces a failing invariant; verifier reports evidence.
5. `examples/minimal_agent` passes with zero ODRL imports.
6. `pytest tests/test_sdk.py tests/test_server_ingestion.py tests/test_second_app.py` green.

## 7. Open follow-ups (not blocking v1)

- Lab page verified at HTTP level (served markup + `/v1` data path) and
  `app.js` parses cleanly; no browser-rendered check exists in this
  environment (no JS runtime).
- SDK flush is batched synchronous I/O on run end / batch threshold —
  no background-thread exporter yet; per-step calls never touch the network.
- `adapter.py` / `tools.py` / `odrl.py` / `dspy_opt.py` moved to
  `agentcrash.extensions.odrl`; thin deprecated shims keep the old import
  paths working (verified by the untouched legacy suite). `verifiers.py`
  keeps its public API via the domain-free `_registry.py` split.
  Full shim removal is a separate breaking-change release.
- Retention is keep-last-N and/or max-age days per project
  (`POST /v1/admin/prune`); no per-project quota configuration yet.
