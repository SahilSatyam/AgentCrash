# AgentCrash SOTA roadmap — state-of-the-art chaos engineering for AI agents

- **Status:** Draft. Do not implement until the owner replies `go`.
- **Package:** `agentcrash-core` on PyPI (import name `agentcrash`).
- **Goal:** any agentic workflow can `pip install agentcrash-core`, wrap its tool
  boundaries, break them on purpose, and get a deterministic verdict on the
  *path* the agent took, plus a replayable fix.

## 1. Where AgentCrash stands (0.3.0)

| Capability | State |
|---|---|
| Framework-neutral SDK (`AgentCrash`, `run`, `step`, `record_tool`, `maybe_fault`) | Shipped |
| Deterministic, seeded fault triggers by tool + op + occurrence | Shipped |
| Fault kinds: `timeout_after_commit`, `timeout_before_commit`, `concurrent_update`, `poisoned_doc` marker | Shipped |
| Hash-chained JSONL traces with write-time redaction | Shipped |
| Pure invariants (`required_step`, `duplicate_mutating_effect`, `fresh_write_only`, `no_lost_update`) + plugin registry | Shipped |
| Policy file + RFC 7396 merge-patch + opt-in LLM patch proposal | Shipped |
| Control plane (`serve`, `/v1` API, Lab UI, bearer auth, retention) | Shipped |
| Domain code in core | None (guarded by `tests/test_generic_core.py`) |

## 2. Landscape (Nov 2026)

| Project | Focus | What AgentCrash adds |
|---|---|---|
| [AgentChaos](https://github.com/IntelligentDDS/AgentChaos) ([paper](https://arxiv.org/abs/2608.06790)) | Faults at the LLM API layer (errors, truncation, corruption) | Faults at the **tool / side-effect** layer, invariant verdicts, policy replay |
| [ReliabilityBench](https://arxiv.org/abs/2601.06112) | pass^k, perturbations, tool-fault intensity, end-state checks | A drop-in package for *your* workflow, not a fixed benchmark |
| [AgentDojo](https://arxiv.org/pdf/2406.13352v3.pdf) | Indirect prompt injection over tools | Injection as one fault among many, judged by the same verifiers |
| [tau-bench](https://ar5iv.labs.arxiv.org/html/2406.12045) | Tool-agent-user tasks, pass^k metric | Fault injection and a fix loop |
| [khaos-agent](https://pypi.org/project/khaos-agent/1.0.0/), [agentfuzz](https://pypi.org/project/agentfuzz/0.2.0/) | PyPI chaos/security toolkits for agents | Hash-chained evidence, model-free verdicts, policy-only diagnosis |

Lessons to adopt from recent audits:

- Agent-safety benchmarks can measure capability instead of safety
  ([validity audit](https://arxiv.org/html/2607.28685v1)). Every invariant must
  document what it proves and ship positive + negative fixtures.
- A failed injection is often *ignored*, not *refused*
  ([audit](https://arxiv.org/pdf/2610.23254)). Report the two separately.

## 3. Roadmap

### R1 — Framework adapters (P0)

One-line integration for the common stacks, each an optional extra with lazy imports:

- `agentcrash.adapters.langgraph` — wrap tool nodes.
- `agentcrash.adapters.openai_agents` — wrap `function_tool`s.
- `agentcrash.adapters.mcp` — proxy an MCP server and inject faults per tool.
- `agentcrash.adapters.crewai`, `agentcrash.adapters.pydantic_ai` (P1).
- A generic `@ac.tool("name")` decorator that records + injects in one place.

Exit: same invariant verdict from a toy agent on each adapter; core import
pulls none of them.

### R2 — Fault catalog v2 (P0)

Add, all seeded and deterministic:

- `rate_limit` (429 + `retry_after`), `server_error` (5xx), `latency` spikes.
- `partial_response`, `schema_drift` (renamed / missing / extra fields).
- `stale_read` (old version returned), `duplicate_delivery` (callback fires twice).
- `poisoned_output` that *actually* rewrites tool output (today `poisoned_doc` is a marker).
- LLM-layer faults behind the same API (truncation, malformed tool-call JSON),
  so tool and model faults compose in one run.

Exit: each kind has catalog params, an SDK hook, and a test proving the trace
records it.

### R3 — Invariant library v2 (P0)

- `exactly_once(tool, key=...)`, `idempotency_key_present(tool)`.
- `no_action_after_untrusted(tool)` (taint from flagged outputs to mutating calls).
- `bounded_retries(tool, n)`, `no_unconfirmed_mutation(tool)`.
- `end_state_equals(...)` over the state snapshot (action-metamorphic checks).
- Separate `injection_ignored` vs `injection_refused` outcomes.

Exit: each invariant has a docstring stating what it proves, plus ± fixtures.

### R4 — Reliability metrics (P1)

- `agentcrash sweep`: run a workflow N times across seeds × fault intensities.
- Report pass^k, fault-tolerance curve, and side-effect counts per fault kind.
- `compare_reports` across policy versions → regression table in CI.

### R5 — Standard traces (P1)

- Export events as OpenTelemetry spans using the GenAI conventions
  (`invoke_agent` → `chat` → `execute_tool`); the conventions are still marked
  Development, so pin the version we target.
- Keep the hash-chained JSONL as the evidence format of record; OTel is an export.

### R6 — Diagnose → replay, generic (P1)

- `agentcrash diagnose` / `replay` in core, driven by a host-supplied runner
  callable (core never owns the workflow).
- Patch providers: rule-based defaults (idempotency key, reconcile-first,
  confirm-on-untrusted) + opt-in LLM via `AGENTCRASH_LLM_BASE_URL`.
- Review gate: show the policy diff before replay; refuse non-schema output.

### R7 — Release hygiene (P0, before 0.3.0 upload)

- [x] MIT `LICENSE` + `license` metadata.
- [x] Harness overhead within NFR-PERF-01 (5.7% at 2 ms/step; see `CHANGELOG.md`).
- [x] Migration table for the 0.3.0 breaking changes (`CHANGELOG.md`).
- [ ] Trusted publishing from GitHub Actions on tag; build sdist + wheel.
- [ ] Refresh `docs/adlc/` to the SDK-first design.
- [ ] Decide whether to yank 0.2.0 (it shipped domain code).

## 4. Non-goals

- No domain logic in core. Domain checks live in host or plugin packages.
- No LLM-as-judge for verdicts. Models may propose policy patches only.
- No orchestration: AgentCrash observes and injects; the host runs the agent.
