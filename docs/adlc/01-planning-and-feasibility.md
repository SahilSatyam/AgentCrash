# 01 - Planning and Feasibility (AgentCrash)

**Status:** Draft | **Owner:** Founder / technical lead | **Review gate:** M0 exit review
**ADLC lens:** Startup feasibility for an AI/Agentic Development Lifecycle idea: problem, users, MVP, risks, costs. Milestone-gated, no calendar dates.

## 1. Project at a glance

- **Product:** AgentCrash, shipped as the PyPI package `agentcrash-core` (import name `agentcrash`, MIT, Python 3.11+).
- **Concept:** chaos engineering for tool-using AI agents, for any agentic workflow. The host developer wraps named tool boundaries with the SDK. AgentCrash injects faults at those boundaries, records a hash-chained JSONL trace, and judges the trace with deterministic invariants.
- **Framework-neutral:** plain Python, LangGraph, OpenAI Agents SDK, CrewAI and MCP hosts all work the same way. The host owns the agent loop; AgentCrash observes, injects and judges (ADR-009).
- **Hard rule:** deterministic invariants own every verdict. A model may only propose a policy patch, and the patch must pass the `policy_patch/v1` schema (ADR-001, ADR-004).
- **Dependencies:** `pyyaml`, `jsonschema`. Optional extras: `server` (fastapi, uvicorn) and `dev`.
- **Surface:** SDK (`AgentCrash`, `WorkflowConfig`, `ac.run`, `step`, `record_tool`, `maybe_fault`, `@ac.workflow`), standalone `TraceWriter`, invariants and verifiers, policy schema and merge patch, a control-plane server, and a CLI with two commands: `check` and `serve`.

## 2. Problem and opportunity

Agents now call tools with real side effects (charge, update, send). Evals mostly grade the final text, so a correct-looking answer can hide a double charge, a lost update, or an action taken on untrusted input. Distributed-systems practice has known for decades that a timeout is ambiguous (the write may have committed). Agent teams rarely test for it.

Existing options leave a gap:

| Option | What it does | Gap for a host team |
|---|---|---|
| Hand-written mocks | One-off failure stubs per project | No shared fault catalog, no verdicts, no evidence format |
| Tracing dashboards | Show what happened | No fault injection, no pass/fail on the path |
| Fixed benchmarks | Score a model on a fixed task set | Not your workflow; not a drop-in package |
| LLM-layer fault tools | Inject errors at the model API | Do not target tool side effects |

AgentCrash targets the tool and side-effect layer, for the host's own workflow, with model-free verdicts. The landscape comparison lives in `docs/plans/2026-11-14-sota-roadmap.md` section 2.

## 3. Options considered

| Option | Verdict | Reason |
|---|---|---|
| Ship a bundled demo app with scenarios, a runner and a model adapter | Rejected (removed in 0.3.0) | Domain assumptions leaked into core: default tool names, reference patches, a demo DB table (ADR-009) |
| Ship a domain-free SDK the host wraps around its own loop | Chosen | One install path, small dependency set, works with any framework |
| LLM-as-judge for verdicts | Rejected | Non-deterministic; breaks replay evidence (ADR-001) |
| Own the agent loop and orchestration | Rejected | Host owns the loop; AgentCrash stays an observer and injector |

## 4. MVP definition

Shipped in 0.2.0 and 0.3.0 (see doc 15 and `CHANGELOG.md`):

1. SDK with versioned event schema v1, hash-chained `prev_hash`, write-time redaction, a bounded drop-oldest export queue, and best-effort HTTP flush that never raises into the host. `AGENTCRASH_DISABLED=1` makes it a no-op.
2. Fault catalog: `timeout_after_commit`, `timeout_before_commit`, `concurrent_update` (requires `key` and `updates`), `poisoned_doc` (an inert marker). Triggers by tool, op and occurrence (call count) or `at_step`. There is no default tool target, and unknown kinds are refused.
3. Invariants: `required_step:<name>`, `duplicate_mutating_effect`, `fresh_write_only`, `no_lost_update`, plus a plugin registry.
4. Policy: `policy/v1` and `policy_patch/v1` (RFC 7396 merge patch), `diff_policies`, and an opt-in `fetch_live_patch` over any OpenAI-compatible `/chat/completions` endpoint.
5. Control plane: `agentcrash serve` with `/v1` ingestion, runs, evaluations, reliability and prune endpoints, a Lab UI, and bearer auth.
6. CLI `agentcrash check` with exit codes 0 (PASS), 1 (FAIL or PII leak), 2 (usage or config), 3 (corrupt trace).

Out of MVP, tracked as roadmap items: framework adapters (R1), a richer fault catalog (R2), more invariants (R3), sweeps and pass^k (R4), OpenTelemetry export (R5), generic diagnose and replay (R6).

## 5. Feasibility

| Dimension | Assessment |
|---|---|
| Technical | Feasible and demonstrated. The offline test suite passes, and CI runs a Python 3.11-3.13 matrix. No network or model is needed for tests. |
| Performance | Meets NFR-PERF-01 in `bench/run_overhead.py` (evidence: `docs/phase7-evidence/bench.json`): about 5.7% per-run overhead against a bare 12-step loop with 2 ms stub steps (target at most 10%), about 114 us/step absolute, SDK about 14 us/event, tracer about 31 us/event (gate at most 60), 100-step trace about 38 KB (target at most 2 MB). |
| Determinism | Seeded fault triggers, gapless `event_id`, monotonic `ts_virtual`, sorted-key envelope. The ACR001 lint test guards determinism rules. |
| Adoption | Needs a host to name its tool boundaries. Adapters (R1) cut that to one line per framework. |
| Legal | MIT license. No real credentials or PII in fixtures; redaction at write time plus `check --scan-pii`. |
| Cost | No model calls in the default path. Model cost appears only if the host opts in to `fetch_live_patch`. See `docs/phase7-evidence/cost-sheet.md`. |

## 6. Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R-01 | Domain vocabulary creeps back into core | Medium | High | `tests/test_generic_core.py` fails the build (ADR-009) |
| R-02 | An invariant measures capability, not safety | Medium | High | Each invariant documents what it proves and ships positive and negative fixtures (roadmap R3) |
| R-03 | A failed injection is "ignored" and reported as "refused" | Medium | Medium | Report the two outcomes separately (roadmap R3) |
| R-04 | SDK slows or breaks the host | Low | High | Never raises into host; bounded queue; overhead benchmark gate; `AGENTCRASH_DISABLED=1` |
| R-05 | Hash chain read as tamper-proof | Medium | Medium | Docs state it is an integrity signal, not tamper-proof (doc 11) |
| R-06 | Model-proposed patch is unsafe | Low | High | Schema gate, human review of the policy diff, opt-in only |
| R-07 | Framework APIs drift under adapters | Medium | Medium | Adapters are optional extras with lazy imports; core imports none of them (R1) |
| R-08 | 0.2.0 shipped domain code and users depend on it | Medium | Low | Migration table in `CHANGELOG.md`; yank decision is open in roadmap R7 |

## 7. Costs

- **Build:** one maintainer; the roadmap items are independent and can ship in any order inside a milestone.
- **Run:** CI minutes for the 3.11-3.13 matrix. Tests are offline.
- **Users:** zero model spend unless they opt in to patch proposals through their own endpoint.

## 8. Go / no-go

Go for M0 (core SDK, `check`, `serve`, PyPI package). Further milestones are gated on evidence, not dates (doc 15). The roadmap in `docs/plans/2026-11-14-sota-roadmap.md` is Draft and not implemented until the owner replies `go`.

## 9. Open questions

| # | Question | Owner | Resolve by |
|---|---|---|---|
| OQ-01 | Yank 0.2.0 from PyPI (it shipped domain code)? | Founder | M0 exit |
| OQ-02 | Pin which OpenTelemetry GenAI convention version for R5? | Tech lead | M2 entry |
| OQ-03 | Ship rule-based patch providers in core or in a separate package (R6)? | Tech lead | M3 entry |
