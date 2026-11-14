# 02 - Product Requirements Document and Project Brief (AgentCrash)

**Status:** Draft | **Owner:** Founder (product) | **Review gate:** M0 exit review
**ADLC lens:** Startup PRD for an AI/Agentic Development Lifecycle idea. Milestone-gated; no calendar dates.

## 1. Summary

**Product:** AgentCrash, chaos engineering for tool-using AI agents, shipped as the `agentcrash-core` package (`import agentcrash`).
**What it does:** The host developer wraps named tool boundaries in their own agent workflow. AgentCrash injects reproducible faults at those boundaries (ambiguous timeout, concurrent update, poisoned document marker), records each step as hash-chained JSONL, and judges the trace with deterministic invariants.
**For whom:** developers who embed the SDK in an agent they already run, whatever the framework (plain Python, LangGraph, OpenAI Agents SDK, CrewAI, MCP).
**What it is not:** an agent framework or runner. The host owns the loop (ADR-009).
**Why now:** agents get real side effects (pay, update, send) while evals still grade final text. A correct-looking answer can hide a duplicate charge or a lost update.

Lifecycle loop: Wrap -> Inject -> Trace -> Check -> (opt-in) Propose a policy patch -> Re-check.

## 2. Problem statement

A **host developer** needs **proof that their agent handles tool failures safely** because **a correct final answer can hide a duplicate mutation or a stale write**. Today they **eyeball outputs or write one-off mocks**, which causes **silent path failures reaching production**.

A **platform/SRE team** needs **regression-proof behavior under faults** because **production tools are flaky and stateful**. Today they **use tracing dashboards with no fault injection**, which causes **bugs that appear only when production is adversarial**.

A **safety researcher** needs **repeatable fault scenarios with ground-truth verdicts** because **manual red-teaming does not compose into a suite**. Today they **hand-craft prompts per model**, which causes **findings that go stale**.

## 3. Personas

| Persona | Role | Integration touchpoint |
|---|---|---|
| Host developer (primary) | Owns an agent workflow; adds the SDK | `AgentCrash(WorkflowConfig(...))`, `ac.run`, `step`, `record_tool`, `maybe_fault`, `@ac.workflow` |
| Platform / SRE engineer | Runs checks in CI and watches reliability | `agentcrash check` exit codes, `agentcrash serve`, `/v1/reliability/{project}` |
| Invariant author | Encodes a domain rule for a host | `register_verifier`, `check --plugin`, `FreshWriteOnly(tools=...)` |
| Safety researcher | Builds fault suites | Fault catalog, `ac.configure_faults`, trace and report files |

## 4. User journeys

### Journey 1: first chaos run (host developer)

1. `pip install agentcrash-core`.
2. Wrap the workflow: `ac = AgentCrash(WorkflowConfig(project="billing", environment="dev", fault_injection=True, seed=42))`, then `with ac.run("pay-invoice") as run:` and `with run.step("charge") as s:`.
3. Call `run.maybe_fault("payments.charge")` at the boundary and `s.record_tool(tool="payments.charge", inputs=..., outputs=..., status=...)` after the call.
4. Arm a fault: `ac.configure_faults({"payments.charge": {"kind": "timeout_after_commit", "occurrence": 1}})`.
5. Export or write the trace, then `python -m agentcrash check --trace run.jsonl --invariant duplicate_mutating_effect`.
6. Read the verdict. Exit code 1 means the workflow retried a committed charge.

Success: the developer reaches a verdict without a model, a network or a bundled demo.

### Journey 2: add a domain invariant (invariant author)

1. Implement the verifier protocol and call `register_verifier`, or use `FreshWriteOnly(tools=["crm.update"])`.
2. Run `agentcrash check --plugin my_agent.invariants --trace run.jsonl --invariant my_rule`.
3. Add positive and negative trace fixtures to the host repo.

### Journey 3: gate CI (platform engineer)

1. Run `agentcrash check --format json --report-out report.json` in the host pipeline, with `--scan-pii` where traces leave the machine.
2. Fail the job on exit 1, 2 or 3.
3. Optionally run `agentcrash serve` and point `WorkflowConfig.endpoint` at it for run history and `compare_reports` regression diffs.

### Journey 4: propose a fix (opt-in)

1. Set `AGENTCRASH_LLM_BASE_URL` and `AGENTCRASH_LLM_API_KEY` for any OpenAI-compatible endpoint.
2. Call `fetch_live_patch`. Output must validate as `policy_patch/v1` or it is refused.
3. Review the diff from `diff_policies`, apply with `policy.apply_patch`, re-run the host workflow, and re-check. The verdict still comes from the invariants.

## 5. Goals and success metrics

| Goal | Metric | Target | Measurement |
|---|---|---|---|
| Low integration cost | Lines to wrap one tool boundary | Under 10 with the SDK; one line with an adapter (R1) | Journey 1 dry-run; adapter exit tests |
| Deterministic reproduction | Same seed, same trace after normalization | 100% | `tests/test_tracer.py`, `tests/test_sdk.py`, ACR001 lint test |
| Low overhead | Per-run overhead vs bare 12-step loop, 2 ms stub steps | At most 10% (measured about 5.7%) | `bench/run_overhead.py`, `docs/phase7-evidence/bench.json` |
| Compact evidence | 100-step trace size | At most 2 MB (measured about 38 KB) | bench |
| Safe by default | Host crashes caused by the SDK | 0 (never raises into host) | `tests/test_sdk.py` |
| Domain-free core | Domain vocabulary in `src/` | 0 | `tests/test_generic_core.py` |
| Offline verification | Tests needing network or a model | 0 | Offline test suite, CI matrix py3.11-3.13 |

## 6. Scope

In scope today: SDK, fault catalog (4 kinds), 4 built-in invariants plus plugins, tracer, policy schemas and merge patch, control plane, `check` and `serve` CLI.

Planned (roadmap R1-R7, doc 15): framework adapters, fault catalog v2, invariant library v2, sweeps and pass^k, OpenTelemetry export, generic diagnose and replay driven by a host-supplied runner, release hygiene.

## 7. Non-goals

- Hosted multi-tenant SaaS, billing, SLAs.
- Domain logic in core. Domain checks live in host or plugin packages.
- LLM-as-judge for verdicts. Models may propose policy patches only.
- Orchestrating or running the host's agent.
- Automatic repair of host source code. Patches change policy files only.
- Real payment rails, real PII or production credentials in fixtures or traces.
- Certified compliance (SOC 2, HIPAA). Synthetic-data-only posture per doc 11.
- A bundled domain demo, scenario runner or model adapter. These were removed in 0.3.0 (ADR-009, `CHANGELOG.md`).

## 8. Requirements pointers

Behavioral requirements are in doc 03, terms in doc 04, design in doc 05, data in doc 06, API in doc 07, NFRs in doc 12. Decisions are in doc 08 (ADR-001 to ADR-009).

## 9. Open questions

| # | Question | Owner | Resolve by |
|---|---|---|---|
| OQ-P1 | Which adapter lands first after R1's core decorator: LangGraph or OpenAI Agents SDK? | Product lead | M1 entry |
| OQ-P2 | Is `agentcrash sweep` (R4) a CLI command or only an SDK function? | Product lead | M2 entry |
| OQ-P3 | What does a host-supplied runner callable look like for R6 diagnose and replay? | Tech lead | M3 entry |
