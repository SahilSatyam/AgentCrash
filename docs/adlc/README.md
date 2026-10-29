# ADLC Document Set — AgentCrash

**Status:** Approved · **Owner:** Tech lead · **Milestone:** M0 Baseline
**Lens:** ADLC (AI/Agentic Development Lifecycle) for a startup idea: problem, users,
MVP, risks, costs — no corporate bureaucracy, no day-wise timelines.

AgentCrash is chaos engineering for tool-using AI agents: Run → Inject → Trace → Check →
Diagnose and Replay. Components: Agent Adapter, Synthetic Tools (seeded, virtual clock),
Fault Injector (ambiguous timeouts, poisoned docs, concurrent updates), Tracer (JSONL),
Verifiers (deterministic invariants), Policy Engine (YAML/JSON retry / idempotency /
confirmation), Diagnoser (LLM proposes policy diffs only), Replay Runner. Prototype
slice v0 is the ambiguous-timeout exactly-once scenario (`charge_card`, invoice #9821,
$150); CLI is `run` / `check` / `diagnose` / `replay`; Python 3.11+ with a Meta Llama
adapter. This folder is the working home of the lifecycle docs that define it.

## Status convention

| Status | Meaning | Who sets it |
|---|---|---|
| **Draft** | Proposed wording; requires review | Doc author |
| **Approved** | Explicitly agreed by the project owner | Tech lead or founder |
| **Superseded** | Replaced by a named successor; kept for history, never edited | Tech lead |
| **Open Question** | Genuinely undecided; always paired with an owner and a resolve-by milestone | Doc author |

Rules: placeholders are never silent — an unknown is an Open Question with an owner, not
a bare TBD. Dates never appear as schedule commitments; milestones (M0–M3, per
ADR-006 and doc 15 §1) are the only gating mechanism. A status change happens by
PR reviewed by the doc owner.

## Milestones (the only schedule this project has)

| Milestone | Name | Exit gate |
|---|---|---|
| M0 | Prototype | S1 reproduces the double charge deterministically; CLI loop closes (`run`/`check`/`replay` + reference patch PASS) |
| M1 | Three-scenario suite | S1+S2+S3 with verifiers; diagnoser policy diffs; precision ≥ 0.95 / recall ≥ 0.90 |
| M2 | Policy-diff review + regression | Reviewable diffs; full regression green on every policy change |
| M3 | Multi-model + packaging | Per-model comparison table; shippable package; rehearsed demo |

## Document map

| # | Document | Question it answers | Owner | Gated by |
|---|---|---|---|---|
| 01 | [Planning and feasibility](01-planning-and-feasibility.md) | Should we build this? Problem, options, MVP, risks, costs | Founder | M1 |
| 02 | [PRD and project brief](02-prd-and-project-brief.md) | Who is it for, what outcomes, what is out of scope? | Product lead | M1 |
| 03 | [Requirements and SRS](03-requirements-and-srs.md) | What testable behaviors must hold? | Product lead | M1 |
| 04 | [Glossary and domain model](04-glossary-and-domain-model.md) | What do our words mean? | Tech lead | M1 |
| 05 | [System design](05-system-design.md) | How do the components fit and flow? | Tech lead | M1 |
| 06 | [Data design](06-data-design.md) | What state exists and where does it live? | Backend engineer | M2 |
| 07 | [API contracts](07-api-contracts.md) | What are the CLI/tool integration boundaries? | Backend engineer | M2 |
| 08 | [ADR template + ratified ADRs](08-adr-template.md) | What did we decide and why? (ADR-001–005) | Tech lead | M1–M2 |
| 09 | [Agent context](09-agent-context.md) | What must an agent session load to contribute? | Tech lead | M2 |
| 10 | [Testing strategy](10-testing-strategy.md) | How do we prove it works? | Backend engineer | M2 |
| 11 | [Security and compliance](11-security-and-compliance.md) | What can go wrong adversarially? | Tech lead | M2 |
| 12 | [Non-functional requirements](12-non-functional-requirements.md) | How fast, how reliable, how cheap? | Tech lead | M2 |
| 13 | [Contributing and workflow](13-contributing-and-workflow.md) | How do we work, review, and release? | Tech lead | M0 |
| 14 | [Runbook](14-runbook.md) | How do we demo and operate it? | Tech lead (demo captain) | M2 |
| 15 | [Roadmap and changelog](15-roadmap-and-changelog.md) | What changed per milestone? | Founder | Each gate |

## How to read these docs (in order)

### Humans (founders, engineers, reviewers) — first pass

1. Start here (this README) for the map and the milestones.
2. `01` (why this startup bet) → `02` (users and MVP boundaries) → `03` (testable
   requirements) — the problem half.
3. `05` (architecture) → `08` (the five ADRs: determinism, JSONL, virtual clock,
   policy-only diagnoser, scenario-first slice) — the solution half.
4. `13` (how to contribute) → `14` (how to run the demo) — the action half.
5. Deep dives as needed: `04`, `06`, `07`, `09`, `10`, `11`, `12`, `15`.

### AI agents (new session, onboarding to contribute) — required sequence

Agents must read in this order before writing code, and cite the docs that justify
each change in the PR:

1. This README (map + status convention + milestones).
2. `04` glossary — use canonical terms (`timeout`, idempotency key,
   `ts_virtual`) exactly; synonyms break verifiers (per ADR-006).
3. `08` ADRs — treat ADR-001–005 as hard constraints: never let model output set a
   verdict, never emit non-JSONL evidence, never use wall-clock in tools, never exceed
   the policy schema, never widen v0 beyond the timeout scenario without a new ADR.
4. `09` agent context — session bootstrap, file pointers, and command recipes.
5. `05` + `07` — where the change lands architecturally and contractually.
6. `13` workflow — branch, PR template, definition of done, lint/test gates.
7. `10` testing strategy — which fixtures to add or update for the change.
8. `14` runbook — if the change affects anything demo-visible, rehearse it.

## Core loop reminder (for every reader)

```text
Run (agent acts in synthetic env, seeded, virtual clock)
  → Inject (ambiguous timeout / poisoned doc / concurrent update)
  → Trace (one JSONL file, schema v1, append-only)
  → Check (deterministic verifiers: exactly-once + task completion)
  → Diagnose (LLM proposes policy diff ONLY) & Replay (verify fix + regressions)
```

Concrete v0 anchor values are frozen in ADR-006 (doc 08) and used consistently
across all docs: scenario `payment_gateway_timeout` at
`scenarios/timeout_retry/scenario.yaml`, tool `charge_card`, invoice #9821,
amount $150, tick 50 ms, `charge_card` latency 300 vms, `get_transaction_status`
latency 120 vms, timeout threshold 1500 vms, `timeout_after_commit` fault at
step 3 with simulated latency 2000 vms, outcome `timeout` (ambiguity in
`injector_event`), seed 42 (matrix 1, 7, 42, 123, 2026), `max_steps` 12,
`run_id = sha256(scenario_file + policy_file + seed)[:16]`, example idempotency
key `inv-9821-a3f`, policy `max_retries` 0–3 (default 1 for mutating tools),
confirmation above $100 (10000 cents).

## Open Questions (docs-level, with owners)

| # | Question | Owner | Resolves by |
|---|---|---|---|
| OQ-D1 | Do docs 06/07 need full field-level specs in M2, or are the v0 anchors above enough? | Backend engineer | M2 exit |
| OQ-D2 | Hosted docs site vs. repo-rendered Markdown for external reviewers? | Product lead | M3 |

## Contributing to these docs

Docs change by PR like code: one concern per PR, owner review required, status
transitions explicit. New architectural decisions go in `08` as ADR-006+. Never add a
day-wise date or Gantt-style schedule to any doc — milestone gates only.

## Doc ownership (who approves what)

| Docs | Approver | Consulted |
|---|---|---|
| 01, 02, 15 (direction, scope, changelog) | Founder | Product lead, tech lead |
| 03, 04 (requirements, glossary) | Product lead | Tech lead |
| 05, 06, 07, 10, 11, 12 (design, data, API, testing, security, NFRs) | Tech lead | Backend engineer |
| 08 (ADRs) | Tech lead | Founder for scope-affecting decisions |
| 09, 13, 14, this README (agent context, workflow, runbook, map) | Tech lead | All contributors |

## Doc lifecycle across milestones

- M0 entry: docs 01–05 and 08 exist at least as Draft; 13 is Approved so work can start.
- M0 exit review: docs 01–08 Approved; Open Questions carry owners into M1.
- M1 exit review: docs 09–14 Approved; the runbook demo (doc 14) executed twice clean.
- M2/M3: docs 06–08 extended for new scenarios; doc 15 changelog updated at each gate.
- A doc regresses Draft → Approved only forward: re-review the changed sections, never
  the whole set, and record the transition in the PR.

## FAQ for new contributors

**Where does a behavior change start?** In doc 03 (requirement) or doc 08 (ADR) first,
then code, then fixtures and runbook. Code-first behavior changes are sent back.

**Can the diagnoser or any agent edit these docs?** Agents may draft doc changes, but a
human owner approves and merges — same rule as code (see doc 13, Section 2).

**What if two docs disagree?** The higher-numbered design doc does not win
automatically: file the conflict as a PR against the older doc, link the newer one, and
let the doc owner rule. ADRs (doc 08) win over design prose when they conflict, until
a superseding ADR says otherwise.

**How are costs tracked here?** Model calls, tokens, and CI minutes per milestone live
in doc 15; the budget itself lives in doc 01. Docs PRs that expand live-model usage
must note the expected cost delta.

**What stays out of these docs?** Calendar dates, individual sprint assignments, and
performance reviews — startup ADLC keeps people logistics out of engineering records.
