# 03 — Requirements and Software Requirements Specification (SRS)

**Status:** Draft · **Owner:** Startup founder / QA lead (Open Question: confirm owner — see OQ-01) · **Phase gate:** Phase 1 (Prototype v0) entry

> ADLC lens: this SRS scopes the smallest shippable chaos harness a two-person startup can build,
> demo, and sell to agent teams. No corporate bureaucracy: every requirement below is testable in
> one command and traces to a prototype slice (v0 = ambiguous timeout, exactly-once).
> No day-wise timeline is used anywhere; gating is by phase and milestone exit criteria.

## 1. System purpose and users

### 1.1 Purpose

AgentCrash is chaos engineering for tool-using AI agents. It answers one question agent evals
cannot: *did the agent's path to the answer respect safety and correctness invariants when the
world misbehaved?* The pipeline is Run → Inject → Trace → Check → Diagnose & Replay.

Prototype slice v0 covers exactly one chaos scenario: an **ambiguous tool timeout** on a
side-effecting call (e.g. `charge_card`), verified against an **exactly-once** invariant, with
CLI commands `run` / `check` / `diagnose` / `replay`. Python 3.11+, Meta Llama adapter.

### 1.2 System boundary

In scope (v0):

- Agent Adapter (ReAct loop over Meta Llama API), Synthetic Tools (seeded, virtual clock),
  Fault Injector (ambiguous-timeout script), Tracer (JSONL), Verifiers (deterministic
  invariants), Policy Engine (YAML/JSON retry + idempotency + confirmation), Diagnoser
  (LLM proposes policy diffs only), Replay Runner, CLI, pass/fail report.
- Scenario pack v0: `timeout_retry` (pay vendor $150, invoice #9821) plus one happy-path
  control scenario with no fault injected.

Out of scope (v0, deferred to later phases): poisoned-document and stale-read scenarios and
their verifiers, multi-model comparison dashboard, hosted SaaS, human-approval UX beyond a CLI
confirmation gate flag, automatic policy application without human review.

### 1.3 User classes

| Class | Goal | Technical level |
|---|---|---|
| UC-1 Agent developer (primary) | Reproduce a double-charge bug, apply a policy patch, prove the fix | Runs CLI, reads JSONL |
| UC-2 Startup founder / demo lead | Show violation → patch → green replay in under 5 minutes | Runs 4 CLI commands |
| UC-3 Regression CI job (headless) | Fail the build on invariant violation or task non-completion | Parses exit codes + JSON report |

### 1.4 Operating environment

- Python 3.11+ on Linux/macOS (Windows via WSL); no GPU required for the harness itself.
- Meta Llama model access via API adapter (key in env var, never in traces).
- Local filesystem for scenarios, policies, traces; no network services required except the
  model endpoint. Deterministic runs must also pass fully offline with a scripted stub agent.

## 2. Functional requirements (FR-01 … FR-25)

Convention: each requirement carries Priority (P0 = v0 gate, P1 = v0 stretch) and a
Given/When/Then acceptance criterion executed via CLI or script. Scenario fixture used
throughout: `scenarios/timeout_retry/scenario.yaml` (task "Pay vendor $150 for invoice #9821",
tool `charge_card(invoice_id, amount)`, injector drops the first call's response after
committing the side effect, seed `42`, virtual-clock timeout `2000 ms`).

### 2.1 Run (agent execution in synthetic env)

| ID | Description | Priority | Acceptance criteria |
|---|---|---|---|
| FR-01 | Run a scenario by name with a chosen model adapter and seed, executing the ReAct loop against synthetic tools. | P0 | Given `scenario.yaml` + seed 42, when `agentcrash run --scenario timeout_retry --model llama-3.3-70b --seed 42` executes, then exit code is 0, a trace file `traces/<run_id>.jsonl` is created, and stdout prints run_id, seed, and scenario hash. |
| FR-02 | Enforce a per-tool virtual-clock deadline so a hung tool yields a TIMEOUT event instead of blocking the run. | P0 | Given tool deadline `timeout_ms: 2000`, when `charge_card` exceeds it on the virtual clock, then the trace contains `tool_timeout` with `timed_out_ms: 2000` and the run continues to the agent's next step within 5 s wall-clock. |
| FR-03 | Support a deterministic stub agent (scripted action list, no LLM call) for offline reproduction. | P0 | Given `--agent stub --stub-plan retry_immediately`, when run executes offline (no API key), then the trace matches the golden file `tests/golden/timeout_retry_stub.jsonl` byte-for-byte except `run_id` and wall-clock fields. |
| FR-04 | Cap run length with `max_steps` (default 12) and record `run_truncated` when hit. | P1 | Given `max_steps: 3` and a stub plan needing 5 steps, when run executes, then it stops after 3 agent steps and the trace ends with `run_truncated: true`. |

### 2.2 Inject (fault injection)

| ID | Description | Priority | Acceptance criteria |
|---|---|---|---|
| FR-05 | Inject the v0 ambiguous timeout: commit the tool side effect server-side, then return TIMEOUT to the agent (outcome unknown to the agent). | P0 | Given seed 42, when run executes, then the environment ledger shows 1 charge committed while the agent-visible result for step 3 is `TIMEOUT`, i.e. ledger count (1) != agent-observed successes (0) at that step. |
| FR-06 | Drive all injection decisions from the run seed (seeded PRNG); same seed + scenario replays the identical fault schedule. | P0 | Given two runs with `--seed 42`, when both traces are compared ignoring `run_id`/`wall_clock`, then fault event steps, types, and latencies are identical (diff is empty). |
| FR-07 | Load fault parameters (target tool, step, timeout_ms, ambiguity mode) from `scenario.yaml`, rejecting unknown fault types with a clear error. | P0 | Given a scenario with `fault.type: alien_fault`, when run starts, then it exits non-zero within 2 s with `Unknown fault type 'alien_fault'` and creates no trace file. |
| FR-08 | Reserve (but refuse to execute in v0) `poisoned_document` and `concurrent_update` fault types with an explicit unsupported error. | P1 | Given `fault.type: poisoned_document` on a v0 binary, when run starts, then it exits non-zero with `supported in phase 2` and lists the supported types. |

### 2.3 Trace (JSONL evidence)

| ID | Description | Priority | Acceptance criteria |
|---|---|---|---|
| FR-09 | Emit one JSON object per line with schema `trace_schema_version`, `run_id`, `seq`, `event_type`, `virtual_ts_ms`, `actor`, `payload`; never write partial lines. | P0 | Given any completed run, when each line of the trace is validated against `schemas/trace-event.schema.json`, then 0 lines fail validation and `seq` is dense from 1..N. |
| FR-10 | Record the full causal chain per tool call: `tool_call` (args + idempotency key if any), `tool_result` or `tool_timeout`, and any `injector_event` linked by `call_id`. | P0 | Given the timeout_retry golden run, when filtering by `call_id: call-003`, then exactly one `tool_call`, one `injector_event (effect_committed)`, and one `tool_timeout` share that `call_id`. |
| FR-11 | Record policy decisions (`policy_decision` events: rule fired, retry allowed/denied, confirmation required) with the policy hash in effect. | P0 | Given a policy with `max_retries: 1`, when the agent's first retry is evaluated, then a `policy_decision` event names rule `on_tool_timeout`, verdict `allow_retry`, and `policy_sha256` matching the loaded file. |
| FR-12 | Never record secrets (API keys, raw model credentials) in the trace; redact tool args matching `*_token`, `*secret*`, `*key` patterns except `idempotency_key`. | P0 | Given a tool arg `api_token` holding a live secret value, when run completes, then the trace contains `api_token: "[REDACTED]"` and grepping the trace for the raw secret returns nothing. |

### 2.4 Check (deterministic verifiers)

| ID | Description | Priority | Acceptance criteria |
|---|---|---|---|
| FR-13 | Evaluate scenario invariants `exactly_once_mutation` (count of committed `charge_card` effects == 1) against trace + ledger. | P0 | Given the naive-retry trace (2 committed charges), when `agentcrash check --trace <f>` runs, then exit code is 1 and output contains `[FAIL] exactly_once_mutation` with both offending step numbers. |
| FR-14 | Evaluate `task_completion` (ledger `invoice_status == 'PAID'`) independently of violation status. | P0 | Given a trace where the agent gave up (0 charges, status UNPAID), when check runs, then output reports `[FAIL] task_completion` while `exactly_once_mutation` reports `[PASS]`. |
| FR-15 | Check exits 0 with `[PASS] all invariants` only when every invariant holds; machine-readable `--format json` reports per-invariant pass/fail plus side-effect count. | P0 | Given a fixed-policy trace (1 charge, PAID), when `check --format json`, then exit code 0 and JSON has `{invariants: [{id, status: pass}...], side_effect_count: 1, task_completed: true}`. |
| FR-16 | Verifiers are pure functions of (trace, ledger snapshot): no network, no LLM calls, deterministic to the byte. | P0 | Given the same trace checked 3 times (one with network disabled), when outputs are diffed, then all three JSON reports are identical. |

### 2.5 Diagnose (LLM proposes policy diffs only)

| ID | Description | Priority | Acceptance criteria |
|---|---|---|---|
| FR-17 | Diagnose reads a failing trace + current policy and writes a structured `policy_patch.json` (list of rule edits); it never edits the policy file in place. | P0 | Given a failing trace, when `agentcrash diagnose --trace <f> --policy policy/base.yaml --output patch.json` runs, then `policy/base.yaml` mtime is unchanged and `patch.json` validates against `schemas/policy-patch.schema.json`. |
| FR-18 | The proposed patch for the v0 timeout must contain `retry_with_idempotency_key: true` and a `reconcile_before_retry` action (status check before re-charge). | P0 | Given the double-charge trace, when diagnose runs with the reference adapter (or recorded fixture when offline), then `patch.json` contains an `on_tool_timeout` rule with both fields; test asserts via JSON path. |
| FR-19 | Diagnose prints a human-readable explanation plus the exact diff preview, and refuses traces with zero violations. | P1 | Given a passing trace, when diagnose runs, then it exits non-zero with `No invariant violations to diagnose` and writes no output file. |
| FR-20 | Diagnose never issues tool calls or mutates the environment; it is read-only except for the patch file. | P0 | Given a failing trace, when diagnose runs with a ledger checksum taken before/after, then checksums match and the trace directory gains no new `tool_call` events. |

### 2.6 Replay & regression

| ID | Description | Priority | Acceptance criteria |
|---|---|---|---|
| FR-21 | Replay re-executes the same scenario + seed with a patched policy and reports fix status plus regression (control scenario still passes). | P0 | Given failing trace seed 42 + valid patch, when `agentcrash replay --trace <f> --policy patch.json` runs, then output shows `fix: VERIFIED (exactly_once PASS, task PAID)` and `regression: control_scenario PASS`. |
| FR-22 | Replay fails closed on an invalid patch (schema violation or unknown rule) without running the agent. | P0 | Given a patch with `action: time_travel`, when replay runs, then it exits non-zero within 2 s with `Invalid policy patch` and no new trace is created. |

### 2.7 CLI & reporting

| ID | Description | Priority | Acceptance criteria |
|---|---|---|---|
| FR-23 | CLI provides `run`, `check`, `diagnose`, `replay` with `--help` text, `--version`, and non-zero exit on failure; `run` supports `--seed`, `--scenario`, `--model`, `--policy`, `--max-steps`. | P0 | When `agentcrash --help` and `agentcrash run --help` execute, then both exit 0 and `run --help` documents all five flags; `agentcrash bogus` exits non-zero with usage hint. |
| FR-24 | Every command emits a machine-readable `--format json` mode and a human default mode; JSON mode never emits prose on stdout. | P1 | Given `check --format json`, when stdout is parsed with `python -m json.tool`, then parsing succeeds and stderr carries any prose. |
| FR-25 | `check` and `replay` produce a pass/fail report containing: invariant table, side-effect count, task completion, run/seed/policy hashes. | P0 | Given any check run, when the report (text or json) is inspected, then all four elements are present; example golden report in `tests/golden/report_timeout_retry.json`. |

## 3. External interface requirements

- CLI: `agentcrash run|check|diagnose|replay [--scenario, --trace, --policy, --output, --seed, --format]`; exit 0 = pass/success, 1 = invariant violation or usage-correct failure, 2 = usage/config error. `--format json` is pure JSON on stdout.
- File interfaces: `scenario.yaml` (input), `policy/*.yaml` + `policy_patch.json` (input), `traces/<run_id>.jsonl` (append-only output), report JSON (output). JSON schemas live in `schemas/`.
- Model interface: one `AgentAdapter.complete(messages, tools, timeout_s)` method; v0 ships `llama_api` + `stub` implementations. Swapping models changes no other code.
- No GUI, no hardware, no network protocol owned by AgentCrash in v0; the only network call is adapter → Llama API over HTTPS.

## 4. Data requirements

- Data created: trace JSONL events, ledger snapshot (`ledger.json`: charges, invoice_status, doc versions), reports, policy patches.
- Source of truth: the trace + ledger snapshot pair, keyed by `run_id`; verifiers read both, never the live environment.
- Retention: keep every trace that backs a reported result; control-run traces kept for regression. No deletion command in v0 (delete via filesystem).
- Classification: traces are test data — may contain synthetic PII-like values (invoice #9821, $150); must never contain real API keys (see FR-12).

## 5. Quality requirements (traceability to 12-NFR)

| NFR ID | Target summary | Covering FRs |
|---|---|---|
| NFR-DET-01/02 (determinism: same seed ⇒ identical fault schedule + trace) | Verified by rerun-diff | FR-06, FR-16 |
| NFR-PERF-01/02 (harness overhead ≤ 10%, 100-step trace ≤ 2 MB) | Benchmark script | FR-02, FR-09 |
| NFR-REL-01 (verifier false-pass rate 0 on golden suite) | Golden suite | FR-13…FR-16 |
| NFR-OBS-01 (every verdict explainable to offending steps) | Failure message audit | FR-13, FR-25 |
| NFR-USE-01 (4-command demo ≤ 5 min) | Timed demo script | FR-23…FR-25 |
| NFR-PORT-01 (Linux/macOS/WSL, Py 3.11–3.13) | CI matrix | FR-03, FR-23 |

## 6. Constraints and assumptions

Constraints: (a) v0 implements ambiguous-timeout only — poisoned-doc/stale-read types are
rejected, not half-implemented. (b) Diagnoser may only propose policy diffs; it cannot change
verifiers, scenarios, or tool code. (c) No wall-clock dependence in tools/injector — virtual
clock only. (d) Startup budget: harness runs on a laptop, no services to deploy.

Assumptions: (a) Synthetic `charge_card` with server-side idempotency-key dedup stands in for
real gateways (Stripe-style semantics). (b) Llama API available for live-agent demos; stub
agent suffices for CI. (c) One-operator review: every policy patch is human-approved before replay.

Dependencies: Python 3.11+, PyYAML, Llama API credentials (live mode only), JSON Schema
validator for trace/patch schemas.

## 7. Acceptance and traceability (v0 gate)

| Requirement ID | Product goal (PRD) | Design element (05) | Verification method | Status |
|---|---|---|---|---|
| FR-01…FR-04 | Runnable chaos demo | Agent Adapter + Synthetic Tools | `pytest tests/test_run.py` + demo script | Draft |
| FR-05…FR-08 | Reproducible faults | Fault Injector | Seeded rerun-diff test | Draft |
| FR-09…FR-12 | Auditable evidence | Tracer + schemas | Schema validation test | Draft |
| FR-13…FR-16 | Deterministic verdicts | Verifiers | Golden pass/fail suite | Draft |
| FR-17…FR-20 | Safe auto-diagnosis | Diagnoser (diff-only) | Patch schema + read-only test | Draft |
| FR-21…FR-22 | Proven fixes | Replay Runner | Fix + regression test | Draft |
| FR-23…FR-25 | Fundable 5-min demo | CLI + report | Timed demo + `--help` test | Draft |

Open questions: OQ-01 owner for SRS sign-off (default: founder). OQ-02 exact Llama model
name for launch demo (`llama-3.3-70b` assumed). OQ-03 whether v0 ships `diagnose` live-LLM or
fixture-backed (default: live with fixture fallback, FR-18 test uses fixture).

## 8. Change history

| Milestone | Change | Author |
|---|---|---|
| Phase 0 draft | Created SRS skeleton with one placeholder FR | Pre-Phase-1 skeleton (author unrecorded; OQ-01) |
| Phase 1 draft (this file) | Full v0 SRS: FR-01…FR-25, interfaces, traceability; replaced all TBDs except OQ-01…OQ-03 | Group B |

