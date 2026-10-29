# 08 — Architecture Decision Records (Template + Ratified ADRs)

**Status:** Approved · **Owner:** Tech lead (startup CTO) · **Milestone:** M1 Foundations
**Applies to:** All AgentCrash contributors, human and agent · **Lens:** ADLC for a startup idea

## Why ADRs exist here

AgentCrash is chaos engineering for tool-using AI agents, and its load-bearing choices
are about determinism, evidence formats, clocks, and what the diagnostic model may
change — they decide whether a passing run means anything. This file is the blank
template (Section 1) plus the five ratified v0 decisions, so a new contributor or fresh
agent session sees what is settled without digging through history. Milestones gate
progress (M1–M4) — no day-wise timelines. Undecided items are **Open Questions** with
owners, never silent TBDs. New ADRs copy Section 1 as ADR-006+; superseding adds a new
ADR, never edits history.

## 1. Blank template (copy for ADR-006 onward)
```markdown
# ADR-NNN: [Decision title]

- **Status:** Proposed | Accepted | Rejected | Superseded
- **Milestone:** M1 | M2 | M3 | M4
- **Decision owner:** [name or role]
- **Related requirements:** [links to 03-requirements sections]
- **Related ADRs:** [ADR-NNN links]

## Context

What problem requires a decision? Requirements, evidence, reproduction data, cost or
risk pressure. Startup framing: what breaks for the user or demo if decided wrong?

## Options considered

### Option A — [name]: benefits, costs, risks.
### Option B — [name]: benefits, costs, risks.

## Decision

The choice, why it fits, and the milestone it lands in.

## Consequences

- Benefits: / Tradeoffs and risks: / Follow-up work (owner + milestone):
- Revisit when: [concrete trigger, e.g. second scenario lands, traces exceed N MB]
```

---

## ADR-001: Deterministic verification is separated from LLM diagnosis

- **Status:** Accepted
- **Milestone:** M1 Foundations
- **Decision owner:** Tech lead
- **Related requirements:** exactly-once mutation invariant, task-completion invariant
- **Related ADRs:** ADR-002 (trace evidence), ADR-004 (diagnoser edit scope)

### Context

AgentCrash grades the path, not just the final answer: an agent can sound correct while
double-charging a customer. If the same LLM that acted also judges safety, judgment is
correlated with the mistake — early spikes showed the model excusing a double charge
because the invoice ended PAID. Guardrail practice agrees: model output is untrusted
and policy is enforced in code, since prompt-level instructions fail open.

### Options considered

| Option | Fate | Reason |
|---|---|---|
| Model grades its own run | Rejected | Correlated failure; identical traces pass and fail across retries |
| Deterministic verifiers own pass/fail | Chosen | Identical trace + state always yields the identical verdict |

### Decision

Verification is deterministic code run by `agentcrash check` against the recorded trace
and post-run environment state. The LLM diagnoser may explain failures and propose
policy diffs but cannot change a verdict: a run passes iff every verifier passes. Lands
in M1 and gates M2 — no scenario enters the suite without its verifier.

### Consequences

- Benefits: reproducible safety claims; meaningful replay comparisons; debuggable demos.
- Tradeoffs and risks: verifier bugs become safety bugs — mitigated by positive and
  negative fixture traces per verifier (see doc 10).
- Follow-up work: verifier SDK (`count_calls()`, `env_value()`) — backend eng, M2.
- Revisit when: a scenario needs a genuinely probabilistic invariant (new ADR then).

---

## ADR-002: JSONL trace as the single evidence format

- **Status:** Accepted
- **Milestone:** M1 Foundations
- **Decision owner:** Tech lead
- **Related requirements:** unified trace schema, replay inputs
- **Related ADRs:** ADR-001 (verifiers consume traces), ADR-003 (virtual-clock fields)

### Context

Run, Inject, Trace, Check, Diagnose and Replay all need the same facts: tool called,
arguments, outcome, injector action, policy decision. Without one shared format replay
cannot reconstruct the run. Agent audit logging converges on append-only line-oriented
logs: crash-safe, streamable, greppable, attachable to an issue.

### Options considered

| Option | Fate | Reason |
|---|---|---|
| SQLite event database | Rejected | Expressive queries, but binary files do not diff, stream, or attach well |
| Single JSONL file, one event per line | Chosen | Crash-safe appends; independently parseable lines; plain-text diffs |

### Decision

Every run emits one JSONL trace; each line is self-contained with `trace_id`, `seq`,
`vclock_ms`, `event_type`, `tool`, `args`, `outcome`, plus optional `injector_event`
and `policy_decision`. Schema versioned via per-line `schema_version` (v0 starts at 1);
the Tracer validates at write time and rejects unknown event types in strict mode.

```json
{"schema_version": 1, "trace_id": "t-7f3a", "seq": 4, "vclock_ms": 1250,
 "event_type": "tool_result", "tool": "charge_card",
 "args": {"invoice_id": 9821, "amount": 150, "idempotency_key": "inv-9821-a3f"},
 "outcome": "TIMEOUT_AMBIGUOUS", "injector_event": {"fault": "ambiguous_timeout"}}
```

Core v0 event types:

| event_type | Emitted when | Key fields |
|---|---|---|
| `run_start` | run begins | `scenario`, `policy_version`, `seed` |
| `tool_call` | agent invokes a tool | `tool`, `args`, `idempotency_key` |
| `tool_result` | tool returns / times out | `outcome`, `latency_ms`, `vclock_ms` |
| `injector_event` | fault fires | `fault`, `target_seq` |
| `policy_decision` | policy gates retry/confirm | `rule`, `action_taken` |
| `run_end` | run finishes | `final_state_ref`, `verdict` |

### Consequences

- Benefits: verifiers, diagnoser, replay share one input; readable fixtures;
  `agentcrash check --trace traces/latest.jsonl` identical in CI and locally.
- Tradeoffs and risks: size grows with argument payloads — mitigated by an 8 KB
  per-event cap in v0 (full payloads behind a debug flag).
- Follow-up work: JSON Schema file + `trace validate` — backend engineer, M2.
- Revisit when: traces exceed 100 MB per run or cross-run analytics need joins.

---

## ADR-003: Virtual clock over wall-clock for synthetic tools

- **Status:** Accepted
- **Milestone:** M1 Foundations
- **Decision owner:** Backend engineer
- **Related requirements:** reproducible timeouts, seeded runs
- **Related ADRs:** ADR-002 (`vclock_ms` field), ADR-005 (scenario-first slice)

### Context

The v0 slice lives or dies on the ambiguous timeout. Wall-clock timeouts make the same
seed pass on a laptop and fail on loaded CI, and replay proves nothing. Chaos tooling
for agents treats latency as a controlled input (scripted delays, injected faults), not
an observation. Startup stakes: one flaky demo timeout destroys trust in the platform.

### Options considered

| Option | Fate | Reason |
|---|---|---|
| Wall-clock timeouts with generous margins | Rejected | Huge margins slow the suite; flakes persist on loaded runners |
| Harness-owned virtual clock | Chosen | Seed + virtual time always yields the same timeout behavior |

### Decision

Synthetic Tools advance a harness-owned virtual clock (`vclock_ms`); timeouts,
latencies, and injector firing times are virtual milliseconds derived from the run
seed. Concrete v0 values: default tool latency 40 vms; `charge_card` timeout threshold
800 vms; injector fires the ambiguous timeout at `vclock_ms = 1250` with outcome
`TIMEOUT_AMBIGUOUS` (effect applied, response dropped). Wall-clock is informational
metadata only, never a decision input.

### Consequences

- Benefits: deterministic chaos; fast suite (no real sleeping); exact replay.
- Tradeoffs and risks: production-fidelity gap — mitigated by an M4 wall-clock soak
  mode running the same scenarios against real sleeps and reporting divergence.
- Follow-up work: `VirtualClock` (`advance()`, `deadline()`, seed derivation) — M1.
- Revisit when: real-model latency studies show virtual timings mislead policy tuning.

---

## ADR-004: Policy-file-only diagnoser edits

- **Status:** Accepted
- **Milestone:** M2 Prototype Slice
- **Decision owner:** Tech lead
- **Related requirements:** machine-editable recovery policy, safe auto-remediation
- **Related ADRs:** ADR-001 (diagnoser cannot set verdicts)

### Context

The Diagnoser is an LLM reading failing traces. Free-form code or shell output from it
is a remote-code-execution vector that replay cannot safely apply. Defense patterns
agree: constrain the action surface (least privilege), validate arguments in code,
confirm consequential actions, and treat tool outputs as untrusted data. Restricting
the diagnoser to one declarative file is the cheapest strong version of that.

### Options considered

| Option | Fate | Reason |
|---|---|---|
| Free-form fix proposals, human applies them | Rejected | Expressive but not machine-replayable; every fix costs human time |
| Structured diffs against the policy file only | Chosen | Validatable, bounded blast radius, unattended replay, one-screen review |

### Decision

The Diagnoser outputs only a structured policy diff against the declared schema (retry
limits, idempotency-key rules, confirmation gates). The Replay Runner validates the
patch, shows the exact diff (`policy diff`), then applies it; anything outside the
schema is rejected with an error, never silently ignored. Canonical v0 patch:

```json
{"patch_version": 1, "base_policy": "policy/base.yaml",
 "changes": [{"rule": "on_tool_timeout", "tool": "charge_card",
               "action": "check_transaction_status", "retry_with_idempotency_key": true,
               "max_retries": 1}]}
```

Enforced v0 schema limits:

| Field | Type | Allowed values |
|---|---|---|
| `max_retries` | int | 0–3 (default 1 for mutating tools) |
| `retry_with_idempotency_key` | bool | true required for mutating tools |
| `action` | enum | `check_transaction_status`, `confirm_with_user`, `abort` |
| `confirmation_required_above` | int (cents) | e.g. 10000 = confirm above $100 |

### Consequences

- Benefits: safe auto-remediation loop; reviewable diffs; bounded blast radius.
- Tradeoffs and risks: edits can over-fit one trace — mitigated by running the
  regression suite on replay and failing patches that regress prior scenarios.
- Follow-up work: `policy diff` rendering + schema validator — backend eng, M2.
- Revisit when: a fix class provably needs code changes (e.g. new tool semantics).

---

## ADR-005: Scenario-first prototype slice (ambiguous timeout exactly-once)

- **Status:** Accepted
- **Milestone:** M2 Prototype Slice
- **Decision owner:** Founder / product lead
- **Related requirements:** MVP boundaries, PRD outcomes
- **Related ADRs:** ADR-001, ADR-002, ADR-003, ADR-004 (all serve this slice)

### Context

A startup prototype must do one thing end to end, not three things thinly. Of the three
failure classes (ambiguous timeouts, poisoned docs, stale reads), the timeout path is
the universal distributed-systems failure: the agent cannot tell whether the timed-out
mutation happened, and naive retry double-applies it. One closed loop gives a two-minute
demo (double charge, then fixed with idempotency keys) anyone grasps immediately.

### Options considered

| Option | Fate | Reason |
|---|---|---|
| Breadth-first: all three scenarios thinly | Rejected | Three half-built verifiers; tripled flaky demo surface; nothing solid |
| Scenario-first: timeout exactly-once, full loop | Chosen | Real invariants, real patch, real replay evidence in v0 |

### Decision

V0 implements one scenario: ambiguous timeout on `charge_card` for invoice #9821
($150). Invariants: (1) exactly-once mutation — `charge_card` executes at most once
per idempotency key; (2) task completion — invoice ends PAID. CLI: `run`, `check`,
`diagnose`, `replay`. Per-run metrics: violation (yes/no), task completion (yes/no),
side-effect count. M3 adds poisoned-document and stale-read scenarios with new ADRs.

### Consequences

- Benefits: shippable, demoable, testable v0; clear M3 scope; focused costs.
- Tradeoffs and risks: narrow early signal — accepted deliberately; depth first.
- Follow-up work: scenarios 2–3 design spikes — product lead, M3.
- Revisit when: the v0 loop is green end to end and demoed; expand only then.

---

## ADR index

| ADR | Title | Status | Milestone |
|---|---|---|---|
| ADR-001 | Deterministic verification separated from LLM diagnosis | Accepted | M1 |
| ADR-002 | JSONL trace as single evidence format | Accepted | M1 |
| ADR-003 | Virtual clock over wall-clock | Accepted | M1 |
| ADR-004 | Policy-file-only diagnoser edits | Accepted | M2 |
| ADR-005 | Scenario-first prototype slice | Accepted | M2 |

## Open Questions (genuinely undecided, with owners)

| # | Question | Owner | Resolves by |
|---|---|---|---|
| OQ-1 | Multi-model comparison harness across Llama variants — M3 or M4? | Tech lead | M3 planning |
| OQ-2 | Trace retention for shared demo instances (local files vs. hosted bucket)? | Backend engineer | M4 |
| OQ-3 | `confirm_with_user` in headless CI replay — auto-abort or scripted approver? | Product lead | M2 exit |
