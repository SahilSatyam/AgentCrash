# 15 - Roadmap and Changelog (AgentCrash)

**Status:** Draft | **Owner:** Founder / technical lead | **Review gate:** each milestone gate
**Gating rule:** Milestone-gated only (ADR-006). No calendar dates, no day-wise timeline. A milestone exits only when every exit criterion is evidenced.
**Source of truth for future work:** `docs/plans/2026-11-14-sota-roadmap.md` (Draft; not implemented until the owner replies `go`). Shipped history: `CHANGELOG.md`.

## 1. Milestone map

M0 (core package) -> M1 (adapters, faults, invariants) -> M2 (metrics, standard traces) -> M3 (generic diagnose/replay, hardening).

| Milestone | Outcome | Roadmap items | Exit criteria | Status |
|---|---|---|---|---|
| M0 - Core package | Domain-free `agentcrash-core` installable from PyPI; any workflow can wrap, inject, trace and check | R7 (release hygiene); shipped SDK, tracer, invariants, policy, control plane, `check`/`serve` | (1) The offline test suite passes and CI is green on py3.11-3.13. (2) NFR-PERF-01 met (about 5.7% vs 10% target). (3) 0.3.0 migration table published. (4) Trusted-publishing release on tag. (5) `docs/adlc/` matches the SDK-first design. (6) Decision recorded on yanking 0.2.0 | In progress (R7 items 4 and 6 open) |
| M1 - Adapters, faults, invariants | One-line integration for common stacks; richer faults; richer verdicts | R1, R2, R3 | (1) Same invariant verdict from a toy agent on each adapter. (2) Core import pulls no adapter. (3) Each new fault kind has catalog params, an SDK hook, and a test proving the trace records it. (4) Each new invariant has a docstring stating what it proves plus positive and negative fixtures | Not started |
| M2 - Metrics and standard traces | Reliability numbers across seeds and intensities; traces readable by standard tooling | R4, R5 | (1) A sweep reports pass^k, a fault-tolerance curve and side-effect counts per fault kind. (2) `compare_reports` yields a regression table in CI. (3) OTel export targets a pinned GenAI convention version. (4) Hash-chained JSONL stays the evidence of record | Not started |
| M3 - Generic diagnose and replay | Host-driven fix loop with a review gate | R6 | (1) `diagnose` and `replay` run via a host-supplied runner callable; core never owns the workflow. (2) Policy diff is shown before replay. (3) Non-schema output is refused. (4) Rule-based and opt-in model patch providers both pass the same gate | Not started |

## 2. Roadmap items R1-R7

Priorities are P0 (must) and P1 (should), per the roadmap document. P-labels are requirement priority, not milestones (ADR-006).

| ID | Item | Priority | Milestone | What it adds |
|---|---|---|---|---|
| R1 | Framework adapters | P0 | M1 | Optional extras with lazy imports: `agentcrash.adapters.langgraph`, `openai_agents`, `mcp` (proxy that injects per tool); `crewai` and `pydantic_ai` at P1; a generic `@ac.tool("name")` decorator |
| R2 | Fault catalog v2 | P0 | M1 | `rate_limit`, `server_error`, `latency`, `partial_response`, `schema_drift`, `stale_read`, `duplicate_delivery`; `poisoned_output` that rewrites tool output (today `poisoned_doc` is a marker); LLM-layer faults behind the same API |
| R3 | Invariant library v2 | P0 | M1 | `exactly_once`, `idempotency_key_present`, `no_action_after_untrusted`, `bounded_retries`, `no_unconfirmed_mutation`, `end_state_equals`; separate `injection_ignored` and `injection_refused` outcomes |
| R4 | Reliability metrics | P1 | M2 | `agentcrash sweep` over seeds and fault intensities; pass^k; fault-tolerance curve; `compare_reports` regression table |
| R5 | Standard traces | P1 | M2 | OpenTelemetry GenAI span export (pinned version) |
| R6 | Diagnose and replay, generic | P1 | M3 | `diagnose` and `replay` commands driven by a host runner; rule-based and opt-in model patch providers; review gate |
| R7 | Release hygiene | P0 | M0 | See checklist below |

### R7 checklist (from the roadmap)

- [x] MIT `LICENSE` and `license` metadata.
- [x] Harness overhead within NFR-PERF-01 (5.7% at 2 ms/step).
- [x] Migration table for 0.3.0 breaking changes.
- [ ] Trusted publishing from GitHub Actions on tag; build sdist and wheel.
- [ ] Refresh `docs/adlc/` to the SDK-first design (this doc set).
- [ ] Decide whether to yank 0.2.0 (it shipped domain code).

## 3. Entry and exit detail

### M0 exit -> M1 entry

- R7 checklist complete.
- Doc set (01-15) reviewed against the code; no references to removed commands, scenarios or tools.
- Roadmap `go` received from the owner.

### M1 exit -> M2 entry

- Adapter parity test: one toy agent per adapter, same invariant verdict.
- Fault and invariant catalogs documented in doc 04 and doc 07.
- Validity review: each invariant's "what it proves" statement reviewed.

### M2 exit -> M3 entry

- Sweep report sample stored.
- OTel convention version recorded in an ADR.

### M3 exit

- Review gate demonstrated end to end: policy diff shown, then replay, then verdict.
- Changelog entry for the release that includes R6.

## 4. Non-goals (carried from the roadmap)

- No domain logic in core.
- No LLM-as-judge for verdicts. Models may propose policy patches only.
- No orchestration: AgentCrash observes and injects; the host runs the agent.

## 5. Changelog

Mirrors `CHANGELOG.md`; that file wins on any difference. Versions follow SemVer. While the version is `0.x`, minor releases may break the API.

### 0.3.0 - unreleased

Chaos engineering for any agentic workflow: the core no longer ships domain code (ADR-009).

**Breaking changes**

| Removed | Why | Do this instead |
|---|---|---|
| `agentcrash.db` | Tables were shaped for one demo app and nothing in core used it | Use the control plane (`agentcrash serve`, `/v1/runs`, `/v1/events`) or persist `ac.trace(run_id)` yourself |
| `agentcrash.policy.reference_patch_for` and `reference_patch_s4_*` | Hard-coded fixes for one demo | Build a `policy_patch/v1` dict and apply it with `policy.apply_patch`, or opt in to `fetch_live_patch` |
| A demo-specific tool-name constant in `agentcrash.verifiers` | Demo tool name | `FreshWriteOnly(tools=["your_store_tool"])` |
| The demo-domain extra | Domain stacks do not belong in core | Depend on them directly in your own package |
| Fault `default_params` naming a tool | Silently targeted a demo tool | Name the tool yourself; `concurrent_update` now requires `key` and `updates` |
| Exporter de-duplication by `event_id` | Unbounded memory in long-running hosts | Nothing to do; the server de-duplicates on ingestion |

The preferred LLM endpoint variables are `AGENTCRASH_LLM_BASE_URL` and `AGENTCRASH_LLM_API_KEY`; the older names still work.

**Added**

- `agentcrash check` registers the built-in invariants (`duplicate_mutating_effect`, `fresh_write_only`, `no_lost_update`, `required_step:<name>`), so it works without plugins.
- `FreshWriteOnly(tools=...)` for your own versioned-store tools.
- MIT `LICENSE`; PyPI classifiers, keywords and project URLs.
- `tests/test_generic_core.py`: fails if domain vocabulary returns to `src/`.

**Fixed**

- Tracer redacted its own `prev_hash` and `event_id` fields; a hash with 13+ digits in a row could be rewritten as a card number and break the chain.
- SDK hash chain could fork under concurrent steps.
- `record_tool` raised `TypeError` into host code for non-JSON outputs.
- Server reset the connection on 401 before reading the body (Windows; flaky auth test).
- Bench crashed on Windows (unclosed trace files).

**Performance**

- Tracer about 31 us per event (down from about 75).
- SDK about 14 us per event.
- `bench/run_overhead.py` measures NFR-PERF-01 as written: 5.7% at a 2 ms stub step, target at most 10%.

### 0.2.0

First installable split of the SDK, control plane and `check` CLI.

## 6. Cost tracking

Default runs make no model calls. Cost appears only when a host opts in to `fetch_live_patch` against its own endpoint. CI cost is the py3.11-3.13 matrix. See `docs/phase7-evidence/cost-sheet.md`.

## 7. Open questions

| # | Question | Owner | Resolve by |
|---|---|---|---|
| OQ-R1 | Yank 0.2.0? | Founder | M0 exit |
| OQ-R2 | Does R4 `sweep` ship as CLI, SDK function, or both? | Product lead | M2 entry |
| OQ-R3 | Which OTel GenAI convention version do we pin? | Tech lead | M2 entry |
