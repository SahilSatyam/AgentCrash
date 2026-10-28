# AgentCrash overview

## Problem

Evals for AI agents typically grade the final answer. For tool-using agents that is not enough: an agent can produce a correct-looking response while double-charging a customer, following injected instructions, or acting on stale data. AgentCrash tests the *path*, not just the outcome.

## Architecture (prototype)

- **Agent adapter** — model-agnostic interface for the agent under test (Meta models first, simple ReAct loop).
- **Synthetic tools** — deterministic, seeded tools with a virtual clock so timeouts are reproducible.
- **Fault injector** — scripted failure scenarios: ambiguous timeouts, poisoned documents, concurrent writes.
- **Tracer** — one shared JSONL trace schema for all scenarios (tool calls, args, results, injector events, policy decisions).
- **Verifiers** — deterministic invariant checks per scenario, run against the trace and final environment state.
- **Policy file** — machine-editable recovery policy (retry limits, idempotency keys, confirmation rules) in YAML/JSON.
- **Diagnoser** — model assistant that reads failing traces and proposes policy-file edits only (no free-form fixes).
- **Replay runner** — re-runs the experiment with the patched policy and reports fix + regression status.

## Prototype slice (v0)

1. Scenario 1 only: ambiguous tool timeout → duplicate side effect.
2. Invariants: exactly-once side effect, legitimate task still completes.
3. Commands: `run`, `check`, `replay`, and a pass/fail report.
4. Metrics per run: violation (yes/no), task completion (yes/no), side-effect count.

## Roadmap

- Scenarios 2 (untrusted instructions) and 3 (stale state) with their own verifiers.
- Policy-diff review: show exactly what the diagnoser changed before replay.
- Multi-model comparison on the same scenario suite.
- Regression suite: previously fixed failures re-run on every policy change.
