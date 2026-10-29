# 07 -- API and CLI contracts (AgentCrash)

**Status:** Draft -- **Owner:** Startup CTO (interim) -- **Last reviewed:** Phase-gated (no calendar date)
**Gate:** Approved when all four CLI commands implement EXACTLY these flags and exit codes against the schemas in `06-data-design.md`. No day-wise timeline is used anywhere -- only phase/milestone gates.

## 1. Startup lens

The CLI IS the product surface in v0: no REST API, no server, no SDK. A design partner must go from `pip install` to a failing-then-passing chaos experiment in under ten minutes, using only the four commands below. Python module boundaries (section 4) exist so the CLI stays thin and a future CI job or viewer can reuse the same functions without forking behavior.

## 2. CLI command reference

Global flags (all commands): `--help`, `--version`, `--json` (machine-readable stdout), `--quiet` (errors only), `--verbose` (debug to stderr; never to stdout, so stdout stays parseable).

### 2.1 `agentcrash run` -- execute an agent against a chaos scenario

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--scenario` | string (required) | -- | Scenario id or path, e.g. `timeout_retry` resolves `scenarios/timeout_retry.yaml` |
| `--model` | string | `llama-3.3-70b` | Agent model via the adapter (Meta Llama family in v0) |
| `--policy` | path | From scenario file | Override recovery policy, e.g. `policies/fixed-timeout.yaml` |
| `--seed` | int | 42 | RNG seed; recorded in `run_started` for reproducibility |
| `--max-steps` | int | From scenario (12) | Abort bound; exceeded => `E_RUNAWAY` |
| `--trace-out` | path | `traces/<run_id>.jsonl` | Where the JSONL trace is written |
| `--report-out` | path | `reports/<run_id>.verdict.json` | Reserved: `run` writes the trace; the verdict is written by `check` |
| `--dry-run` | flag | false | Validate scenario + policy schemas, print resolved plan, run nothing |

Example:

```bash
agentcrash run --scenario timeout_retry --model llama-3.3-70b --seed 42
# stdout:
# run_id=a3f9c1d2e4b50607 scenario=payment_gateway_timeout steps=4 trace=traces/a3f9c1d2e4b50607.jsonl
# status=completed ledger_hash=sha256:77f1c4a9... charges=2
```

`--json` stdout shape:

```json
{"trace": "traces/a3f9c1d2e4b50607.jsonl", "status": "completed", "steps": 4, "scenario": "payment_gateway_timeout", "run_id": "a3f9c1d2e4b50607", "ledger_hash": "sha256:77f1c4a9..."}
```

### 2.2 `agentcrash check` -- deterministically verify invariants against a trace

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--trace` | path (required) | -- | Trace file, e.g. `traces/a3f9c1d2e4b50607.jsonl` |
| `--scenario` | path | Inferred from trace | Override scenario file (verifier + schema source) |
| `--strict-schema` | flag | false | Enforce all section-8 validation rules from `06-data-design.md` |
| `--verify-chain` | flag | true | Verify the `prev_hash` chain; names the first broken link |
| `--report-out` | path | `reports/<run_id>.verdict.json` | Machine-readable verdict output |

Example (failing run):

```bash
agentcrash check --trace traces/a3f9c1d2e4b50607.jsonl
# stdout:
# [FAIL] Invariant exactly_once_mutation violated!
#   - Step 3: charge_card(invoice_id=9821, amount=150) -> TIMEOUT (outcome ambiguous)
#   - Step 4: charge_card(invoice_id=9821, amount=150) -> SUCCESS
#   - Error: 2 charges registered for invoice #9821 (double charge detected)
# [PASS] Invariant task_completion satisfied (invoice #9821 status=PAID)
# verdict=FAIL report=reports/a3f9c1d2e4b50607.verdict.json
```

Verdict JSON (`reports/<run_id>.verdict.json`):

```json
{
  "run_id": "a3f9c1d2e4b50607",
  "verdict": "FAIL",
  "results": [
    {"id": "exactly_once_mutation", "passed": false, "evidence": "2 charges for invoice #9821 (ch_001, ch_002)"},
    {"id": "task_completion", "passed": true, "evidence": "invoice #9821 status=PAID"}
  ]
}
```

### 2.3 `agentcrash diagnose` -- LLM proposes a policy diff (and nothing else)

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--trace` | path (required) | -- | Failing trace to analyze |
| `--verdict` | path | `reports/<run_id>.verdict.json` | Verdict file for context (recomputed if missing) |
| `--model` | string | `llama-3.3-70b` | Diagnoser model (may differ from agent model) |
| `--output` | path (required) | -- | Where to write `policy_patch.json` |
| `--max-rules` | int | 1 | Cap on rules in the proposed patch (single-rule default keeps review trivial) |

Example:

```bash
agentcrash diagnose --trace traces/a3f9c1d2e4b50607.jsonl --output policy_patch.json
# stdout:
# diagnosis: blind retry_same on ambiguous charge_card timeout caused duplicate charge
# proposed: on_tool_timeout/charge_card -> check_then_act(get_transaction_status) + require idempotency key
# patch=policy_patch.json (policy_patch/v1, 1 rule)
```

Hard constraint (also enforced in code): the diagnoser CANNOT pass a run. Its output is validated against `policy_patch/v1`; free-form text, tool calls, or verdict claims in its output are rejected with `E_POLICY_REJECTED`. Only `check`/`replay` verdicts count.

### 2.4 `agentcrash replay` -- re-execute with a patched policy and report fix + regressions

| Flag | Type | Default | Meaning |
|---|---|---|---|
| `--trace` | path (required) | -- | Original (failing) trace; scenario + seed are reused from it |
| `--policy` | path (required) | -- | Patched policy file (or `--patch` for a `policy_patch.json` applied onto the original policy) |
| `--patch` | path | -- | Alternative to `--policy`: a diagnoser `policy_patch.json` |
| `--regression` | path | `traces/golden/` | Extra golden traces re-run under the patched policy; any newly failing one is reported |
| `--report-out` | path | `reports/<run_id>.replay.json` | Machine-readable replay report |

Example:

```bash
agentcrash replay --trace traces/a3f9c1d2e4b50607.jsonl --patch policy_patch.json
# stdout:
# replay_id=b7e20d4f8a119833 verdict=PASS (exactly_once_mutation fixed, task_completion still PASS)
# regression: 3/3 golden traces still PASS
```

Replay report shape:

```json
{"fixed": ["exactly_once_mutation"], "broken": [], "golden": {"passed": 3, "total": 3}, "replay_id": "b7e20d4f8a119833", "verdict": "PASS", "source_run_id": "a3f9c1d2e4b50607"}
```

## 3. Exit codes and error model

| Exit code | Name | Meaning | Example |
|---|---|---|---|
| 0 | OK | `run` completed; `check`/`replay` verdict PASS | Patched replay: 1 charge, invoice PAID |
| 1 | VERDICT_FAIL | Deterministic invariant violated (NOT a crash) | `exactly_once_mutation`: 2 charges |
| 2 | USAGE_ERROR | Bad flags, missing files, schema validation failure | Unknown `--scenario`, malformed YAML |
| 3 | CORRUPT_TRACE | Trace fails structural validation (bad JSON, broken hash chain) | Edited trace line, missing `prev_hash` |
| 4 | POLICY_REJECTED | Diagnoser output is not a valid `policy_patch/v1`, or patch violates policy schema | Free-form text instead of JSON patch |
| 5 | RUN_ABORTED | Runaway (over `--max-steps`/event cap), tool panic storm, or model API failure | Agent loops 12 steps without paying |

| Error code (in `--json` stderr payloads) | Maps to exit | Retryable? | Client action |
|---|---|---|---|
| `E_CONFIG` | 2 | No | Fix flags/paths, rerun |
| `E_SCHEMA` | 2 | No | Fix YAML/JSON to match `06-data-design.md` |
| `E_TRACE_CORRUPT` | 3 | No | Re-run the scenario; do not hand-edit traces |
| `E_POLICY_REJECTED` | 4 | No | Re-run `diagnose` (optionally new seed); review rules |
| `E_RUNAWAY` | 5 | No | Raise `--max-steps` or fix the agent loop |
| `E_MODEL_API` | 5 | Yes (backoff) | Check `LLAMA_API_KEY`, retry with `--seed` unchanged |
| `E_VERDICT_FAIL` | 1 | No (fix policy, then `replay`) | Run `diagnose` then `replay` |

`--json` error payload shape (stderr, exit != 0):

```json
{"code": "E_SCHEMA", "exit": 2, "hint": "tools[0].timeout_ms must be a positive integer (got -1)", "command": "run"}
```

## 4. Python module boundaries (import contracts, not HTTP)

Package layout: `src/agentcrash/{adapter,tools,injector,tracer,verifiers,policy,diagnoser,replay}.py`. CLI layers import these; no cross-imports except toward `tracer` and `policy` (shared primitives). Every public function is typed; `mypy --strict` runs in CI (P2 gate).

```python
# adapter.py -- drives the agent under test (ReAct loop)
def run_react_loop(task: str, tools: ToolRegistry, policy: Policy, tracer: TraceWriter, llm: LlamaAdapter, max_steps: int = 12) -> RunResult: ...

# tools.py -- deterministic synthetic tools + virtual clock
class VirtualClock:
    def __init__(self, tick_ms: int = 50) -> None: ...
    def now(self) -> int: ...
    def sleep(self, ms: int) -> int: ...
class ToolRegistry:
    def register(self, spec: ToolSpec) -> None: ...
    def call(self, name: str, args: dict, clock: VirtualClock) -> ToolResult: ...

# injector.py -- scripted faults
def load_faults(scenario: Scenario) -> FaultSchedule: ...
def pre_call_hook(schedule: FaultSchedule, step: int, tool: str) -> InjectorAction: ...

# tracer.py -- append-only evidence
class TraceWriter:
    def __init__(self, run_id: str, scenario: str, path: str) -> None: ...
    def append(self, *, step: int, tool: str | None, args: dict, outcome: str, latency: int,
               injector_event: dict | None, policy_decision: dict | None, detail: dict) -> TraceEvent: ...
    def finish(self, ledger_hash: str) -> Path: ...

# verifiers.py -- deterministic checks
def run_verifiers(trace: list[TraceEvent], state: EnvState, invariants: list[Invariant]) -> VerdictReport: ...

# policy.py -- load / validate / patch
def load_policy(path: str) -> Policy: ...
def apply_patch(policy: Policy, patch: PolicyPatch) -> Policy: ...
def diff_policies(old: Policy, new: Policy) -> str: ...

# diagnoser.py -- LLM proposes diffs ONLY
def propose_patch(trace: list[TraceEvent], verdict: VerdictReport, policy: Policy, llm: LlamaAdapter) -> PolicyPatch: ...

# replay.py -- pinned re-execution + regression
def replay(source_trace: Path, policy: Policy, seed: int) -> ReplayReport: ...
def run_regression(policy: Policy, golden_dir: str) -> RegressionReport: ...
```

## 5. Verifier plugin interface (how P2 scenarios plug in without touching `check`)

```python
# verifiers.py
from typing import Protocol

class Verdict(BaseModel):
    invariant_id: str
    passed: bool
    evidence: str          # one-line, human-readable; shown in CLI output

class Verifier(Protocol):
    id: str                # e.g. "exactly_once_mutation"
    def check(self, trace: TraceView, state: EnvState) -> Verdict: ...

def register_verifier(v: Verifier) -> None: ...
def get_verifier(invariant_id: str) -> Verifier: ...   # raises E_CONFIG if unknown
```

Rules: verifiers are pure functions of `(trace, state)` -- no network, no wall-clock, no model calls; violations raise through `Verdict(passed=False)`, harness bugs raise `VerifierError` (exit 3, never misreported as verdict FAIL). v0 ships `exactly_once_mutation` + `task_completion`; P2 adds `no_untrusted_tool_call` (poisoned doc) and `no_stale_write` (concurrent update) behind the same interface.

## 6. Compatibility and versioning

- CLI flag changes are additive within a phase; removing/renaming a flag requires a major version bump and a changelog entry in `15-roadmap-and-changelog.md`.
- `--json` stdout shapes are versioned with `"api_version": "v1"` top-level field; parsers must ignore unknown fields.
- Python function signatures follow the same rule: new optional params only; removal => major bump.

## 7. Open questions (genuinely undecided -- owner assigned)

| ID | Question | Owner | Resolves by gate |
|---|---|---|---|
| OQ-07-1 | Should `run` also print the verdict inline (auto-`check`) or stay single-purpose (Unix philosophy)? Proposal: stay single-purpose | Startup CTO | P1 scope freeze |
| OQ-07-2 | `mypy --strict` vs `pyright` for the type gate | Startup CTO | P2 planning |
| OQ-07-3 | Plugin discovery: entry-points vs explicit `verifiers/` directory import | ML lead (to hire) | P2 planning |
