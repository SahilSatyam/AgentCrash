# ADLC Document Set - AgentCrash

**Status:** Approved | **Owner:** Tech lead | **Milestone:** M0 Core package
**Lens:** ADLC (AI/Agentic Development Lifecycle) for a startup idea: problem, users, MVP, risks, costs. No corporate bureaucracy, no day-wise timelines.

AgentCrash is chaos engineering for tool-using AI agents, shipped as the PyPI package `agentcrash-core` (import `agentcrash`, MIT, Python 3.11+). It works with any agentic workflow (plain Python, LangGraph, OpenAI Agents SDK, CrewAI, MCP). The host owns the agent loop. AgentCrash observes, injects faults at named tool boundaries, and judges traces with deterministic invariants. A model may only propose schema-gated policy patches. The core is domain-free (ADR-009), and `tests/test_generic_core.py` fails if domain vocabulary enters `src/`.

Public surface in one line each:

- **SDK:** `AgentCrash(WorkflowConfig(...))`, `ac.run`, `run.step`, `record_tool`, `maybe_fault`, `@ac.workflow`, `ac.configure_faults`, `ac.trace`.
- **Faults:** `timeout_after_commit`, `timeout_before_commit`, `concurrent_update`, `poisoned_doc` (marker).
- **Invariants:** `required_step:<name>`, `duplicate_mutating_effect`, `fresh_write_only`, `no_lost_update`, plus plugins.
- **Policy:** `policy/v1`, `policy_patch/v1` (RFC 7396 merge patch), opt-in `fetch_live_patch`.
- **Control plane:** `agentcrash serve`, `/v1` API, Lab UI, bearer auth via `AGENTCRASH_API_TOKENS`.
- **CLI:** `agentcrash check` and `agentcrash serve` only (plus `--version`). Exit codes for `check`: 0 PASS, 1 FAIL or PII leak, 2 usage or config, 3 corrupt trace. On Windows, `python -m agentcrash` is equivalent to the console script.

Features from the original prototype (scenario runner, `run`/`diagnose`/`replay` commands, model adapter, bundled domain demo) were removed in 0.3.0. See ADR-009 and `CHANGELOG.md`. Generic diagnose and replay return as roadmap item R6.

## Status convention

| Status | Meaning | Who sets it |
|---|---|---|
| **Draft** | Proposed wording; requires review | Doc author |
| **Approved** | Explicitly agreed by the project owner | Tech lead or founder |
| **Superseded** | Replaced by a named successor; kept for history, never edited | Tech lead |
| **Open Question** | Genuinely undecided; always paired with an owner and a resolve-by milestone | Doc author |

Placeholders are never silent: an unknown is an Open Question with an owner. Dates never appear as schedule commitments. Milestones (M0-M3, ADR-006, doc 15) are the only gating mechanism.

## Milestones

| Milestone | Name | Roadmap items | Exit gate |
|---|---|---|---|
| M0 | Core package | R7 | Domain-free `agentcrash-core` released; offline test suite green on CI py3.11-3.13; NFR-PERF-01 met; docs match code |
| M1 | Adapters, faults, invariants | R1, R2, R3 | Same verdict across adapters; fault and invariant catalogs v2 with fixtures |
| M2 | Metrics and standard traces | R4, R5 | Sweep with pass^k; OTel export; regression table in CI |
| M3 | Generic diagnose and replay | R6 | Host-runner diagnose/replay with a policy-diff review gate |

Roadmap detail: `docs/plans/2026-11-14-sota-roadmap.md`. Mapping and checklists: doc 15.

## Document map

| # | Document | Question it answers | Owner | Gated by |
|---|---|---|---|---|
| 01 | [Planning and feasibility](01-planning-and-feasibility.md) | Should we build this? Problem, options, MVP, risks, costs | Founder | M0 |
| 02 | [PRD and project brief](02-prd-and-project-brief.md) | Who integrates it, what outcomes, what is out of scope? | Product lead | M0 |
| 03 | [Requirements and SRS](03-requirements-and-srs.md) | What testable behaviors must hold? | Product lead | M0 |
| 04 | [Glossary and domain model](04-glossary-and-domain-model.md) | What do our words mean? | Tech lead | M0 |
| 05 | [System design](05-system-design.md) | How do the components fit and flow? | Tech lead | M0 |
| 06 | [Data design](06-data-design.md) | What state exists and where does it live? | Backend engineer | M0 |
| 07 | [API contracts](07-api-contracts.md) | What are the SDK, CLI and HTTP boundaries? | Backend engineer | M0 |
| 08 | [ADR template + ratified ADRs](08-adr-template.md) | What did we decide and why? (ADR-001 to ADR-009) | Tech lead | Each ADR |
| 09 | [Agent context](09-agent-context.md) | What must an agent session load to contribute? | Tech lead | M0 |
| 10 | [Testing strategy](10-testing-strategy.md) | How do we prove it works? | Backend engineer | M0 |
| 11 | [Security and compliance](11-security-and-compliance.md) | What can go wrong adversarially? | Tech lead | M0 |
| 12 | [Non-functional requirements](12-non-functional-requirements.md) | How fast, how reliable, how cheap? | Tech lead | M0 |
| 13 | [Contributing and workflow](13-contributing-and-workflow.md) | How do we work, review, and release? | Tech lead | M0 |
| 14 | [Runbook](14-runbook.md) | How do we install, run, and operate it? | Tech lead | M0 |
| 15 | [Roadmap and changelog](15-roadmap-and-changelog.md) | What ships when, and what changed? | Founder | Each gate |

All docs here describe `agentcrash-core` 0.3.0. If a doc and the code disagree, the code, `CHANGELOG.md` and ADR-009 win; please file the drift.

## How to read these docs (in order)

### Humans (founders, engineers, reviewers)

1. This README for the map and milestones.
2. `01` (why this bet), `02` (personas, journeys, scope), `03` (testable requirements).
3. `05` (architecture), `08` (ADRs; ADR-009 first, it shapes everything else).
4. `13` (how to contribute), `14` (how to run it).
5. Deep dives as needed: `04`, `06`, `07`, `09`, `10`, `11`, `12`, `15`.

### AI agents (new session, onboarding to contribute)

Read in this order before writing code, and cite the docs that justify each change in the PR:

1. This README.
2. `04` glossary. Use canonical terms exactly (`boundary`, `occurrence`, `ts_virtual`, idempotency key).
3. `08` ADRs. Hard constraints: model output never sets a verdict; evidence is JSONL only; no wall-clock in deterministic paths; patches follow the `policy_patch/v1` schema; no domain vocabulary in `src/` (ADR-009).
4. `09` agent context for session bootstrap and command recipes.
5. `05` and `07` for where the change lands.
6. `13` for branch, PR and lint/test gates (ruff, black at line 100, pytest).
7. `10` for which tests to add or update.
8. `14` if the change touches install or operations.

## Core loop

```text
Wrap     host names tool boundaries with the SDK (host owns the agent loop)
Inject   seeded faults at those boundaries (timeout, concurrent update, poisoned doc marker)
Trace    hash-chained JSONL, schema v1, write-time redaction
Check    deterministic invariants -> PASS / FAIL (exit 0 / 1)
Propose  opt-in: model suggests a policy_patch/v1; schema gate; human reviews the diff
```

## Anchors (verifiable in code)

| Anchor | Value | Where |
|---|---|---|
| Package / import | `agentcrash-core` / `agentcrash` | `pyproject.toml` |
| Python | 3.11+ (CI 3.11-3.13) | `.github/workflows/ci.yml` |
| Dependencies | `pyyaml`, `jsonschema`; extras `server`, `dev` | `pyproject.toml` |
| Event schema | v1, `prev_hash` chain | `src/agentcrash/events.py`, `tracer.py` |
| Policy schemas | `policy/v1`, `policy_patch/v1` | `src/agentcrash/schemas/` |
| Disable switch | `AGENTCRASH_DISABLED=1` | `sdk.py` |
| Perf evidence | about 5.7% overhead, about 114 us/step, SDK about 14 us/event, tracer about 31 us/event, 100-step trace about 38 KB | `docs/phase7-evidence/bench.json` |
| Tests | Offline pytest suite | `tests/` |
| Neutral example tools | `payments.charge`, `crm.update`, `kv` store | docs and tests |

## Open Questions (docs-level)

| # | Question | Owner | Resolves by |
|---|---|---|---|
| OQ-D1 | Finish refreshing docs 03-14 to the SDK-first design | Tech lead | M0 exit |
| OQ-D2 | Hosted docs site vs. repo-rendered Markdown for external reviewers? | Product lead | M3 |

## Contributing to these docs

Docs change by PR like code: one concern per PR, owner review required, status transitions explicit. New architectural decisions go in `08` as ADR-010 and later. Never add a calendar date or Gantt-style schedule; use milestone gates.

## Doc ownership

| Docs | Approver | Consulted |
|---|---|---|
| 01, 02, 15 (direction, scope, changelog) | Founder | Product lead, tech lead |
| 03, 04 (requirements, glossary) | Product lead | Tech lead |
| 05, 06, 07, 10, 11, 12 (design, data, API, testing, security, NFRs) | Tech lead | Backend engineer |
| 08 (ADRs) | Tech lead | Founder for scope-affecting decisions |
| 09, 13, 14, this README | Tech lead | All contributors |

## FAQ

**Where does a behavior change start?** In doc 03 (requirement) or doc 08 (ADR), then code, then tests. Code-first behavior changes are sent back.

**What if two docs disagree?** File the conflict as a PR against the older doc and let the owner rule. ADRs win over design prose until a superseding ADR says otherwise. Code and `CHANGELOG.md` win over all docs.

**What stays out of these docs?** Calendar dates, sprint assignments, and performance reviews.
