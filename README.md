# AgentCrash — Chaos Engineering for AI Agents

AI agents are increasingly capable of executing multi-step workflows, but a successful final response does not guarantee that the actions taken along the way were safe or correct. Tool timeouts, ambiguous execution outcomes, duplicate requests, stale state, and untrusted instructions can turn an apparently successful run into a serious failure.

AgentCrash is an experimental reliability-testing platform for tool-using AI agents. It runs agents against controlled synthetic environments, injects reproducible failures, captures execution traces, and evaluates whether the agent respects explicit safety and correctness invariants.

## How it works

1. **Run** — an agent attempts a task in a synthetic environment with scripted tools.
2. **Inject** — the harness injects a reproducible failure (timeout, poisoned document, concurrent update).
3. **Trace** — every tool call, result, and environment event is recorded.
4. **Check** — deterministic verifiers test the trace and final environment state against invariants.
5. **Diagnose & replay** — a diagnostic assistant proposes a targeted recovery-policy change; the experiment replays to verify the fix resolves the violation without breaking legitimate task completion.

## Core principle

Model-generated diagnosis is separated from deterministic verification. The model can explain a failure, but independent checks against recorded events and environment state determine whether a run passes.

## Failure scenarios (prototype)

1. **Ambiguous tool timeouts** — a timed-out call with an unknown outcome tempts retries that cause duplicate side effects.
2. **Untrusted instructions** — malicious instructions embedded in documents the agent reads.
3. **Stale state** — concurrent updates leave the agent acting on outdated reads.

The prototype starts with scenario 1; scenarios 2–3 follow once the run → verify → replay loop works end to end.

## Status

Early prototype. The initial implementation uses Meta's models for agent reasoning and diagnostic assistance, with a controlled execution harness and synthetic tools for reproducible experiments.

## Docs

See [`docs/`](docs/) — start with [overview](docs/overview.md).

## Goal

Make agent reliability measurable and regression-testable: reproduce the failure, inspect the evidence, apply the fix, and verify the result.
