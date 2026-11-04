# agentcrash-core — Reliability framework for agentic workflows

`agentcrash-core` (import package `agentcrash`) is the reusable core
extracted from the AgentCrash reliability project. It observes host
workflows without owning them: buffered SDK instrumentation, a
versioned event schema, domain-free invariants, deterministic fault
injection, and a loopback control plane (Lab + `/v1` API).

Domain workflows live in their own repos and depend on this package —
e.g. the NL-to-ODRL agent in
[`nl2odrl-responsible-ai`](https://github.com/SahilSatyam/nl2odrl-responsible-ai).
Any agentic workflow can do the same.

## Install

```bash
pip install agentcrash-core            # SDK + control-plane client
pip install "agentcrash-core[server]"  # control-plane service extras
```

## Use it in your workflow

```python
from agentcrash import AgentCrash, WorkflowConfig

ac = AgentCrash(WorkflowConfig(project="my-workflow", fault_injection=True))

with ac.run(name="my-run") as run:
    with run.step("my-step"):
        ...  # your business logic; AgentCrash never orchestrates it
```

Disabled mode (`AGENTCRASH_DISABLED=1`) turns every call into a no-op
passthrough, so hosts work with or without the framework installed.

## Judge a trace

```bash
# verify registry invariants against an SDK-schema JSONL trace
agentcrash check --trace run.jsonl --invariant required_step:my-step --report-out verdict.json

# run the control plane (Lab + /v1 API, local only)
agentcrash serve --port 8901
```

Hosts register their own invariants (e.g. `odrl_valid`,
`exactly_once_save`) alongside the generic core checks
(`required_step:*`, `duplicate_mutating_effect`, `fresh_write_only`,
`no_lost_update`).

## Keys and offline mode

No API key is required. The SDK, `check`, `serve`, the Lab UI, and the
full test suite run offline out of the box.

Two opt-in environment variables exist for hosts that want live behavior:

| Variable | Effect when set |
|---|---|
| `LLAMA_BASE_URL` | Enables the opt-in live diagnosis path (`fetch_live_patch`): a failing trace is sent to an OpenAI-compatible `{LLAMA_BASE_URL}/chat/completions` endpoint for a candidate policy patch. When unset, diagnosis falls back to the fixture-backed reference patch. Transport failures also fall back; only a 200 response with unparsable output is rejected. |
| `LLAMA_API_KEY` | Sent as an `Authorization: Bearer` header on the live diagnosis call above — header only, never logged or traced. Optional; the request is sent without it when empty. |

Control-plane auth is separate: `AGENTCRASH_API_TOKENS`
(comma-separated) requires a matching `Authorization: Bearer` token on
every `/v1/*` call. When empty (the default), the server runs in
loopback mode and accepts all local traffic.

## Core principle

**Model-generated diagnosis is strictly separated from deterministic
verification.** The model can explain a failure and propose candidate
policy edits, but independent checks against recorded trace events and
environment state determine whether a run passes or fails.

## Develop

Prerequisites: WSL Ubuntu-24.04, Python 3.11+, `uv`.

```bash
git clone https://github.com/SahilSatyam/AgentCrash.git && cd AgentCrash
uv venv && uv pip install -e ".[dev]"
pytest -q
```

## Layout

- `src/agentcrash/` — `sdk`, `events`, `invariants`, `evaluate`,
  `server`, `verifiers` (registry + generic checks), `tracer`,
  `clock`, `injector`, `ledger`, `policy`, `scan_pii`, `db`,
  `lab/` (control-plane UI assets), `schemas/` (bundled policy schemas)
- `examples/minimal_agent/` — second-app sample with zero domain imports
- `examples/README.md` — writing your own host

## Docs

See [`docs/`](docs/) — start with
[`docs/architecture-decoupling.md`](docs/architecture-decoupling.md),
which defines the core/host boundary.
