# 07 -- API and CLI contracts (AgentCrash)

**Status:** Draft -- **Owner:** Startup CTO (interim) -- **Last reviewed:** Phase-gated (no calendar date)
**Describes:** `agentcrash-core` 0.3.0. Flags, exit codes and signatures below come from `src/agentcrash/` and from `python -m agentcrash check --help` / `serve --help`.

## 1. Surfaces

| Surface | Stability | Notes |
|---|---|---|
| CLI: `check`, `serve`, `--version` | v1 | Only these commands exist. |
| Python API (`agentcrash`, submodules) | v1, pre-1.0 package | Names re-exported in `agentcrash.__all__` are the supported set. |
| HTTP `/v1` control plane | v1 | Versioned by path prefix. |
| File formats | See doc 06 | Event schema 1, `policy/v1`, `policy_patch/v1`, `env_state/v1`, verdict `api_version: "v1"`. |

Removed in 0.3.0 (ADR-009): the `run`, `diagnose` and `replay` commands and their flags (`--scenario`, `--model`, `--seed`, `--agent`, `--policy`). Host-supplied runners for diagnose and replay are roadmap item R6.

Environment variables read by the core:

| Variable | Used by | Meaning |
|---|---|---|
| `AGENTCRASH_DISABLED` | SDK | `1` makes every SDK API a no-op passthrough. |
| `AGENTCRASH_API_TOKENS` | server | Comma-separated bearer tokens. Empty means loopback mode with no auth. |
| `AGENTCRASH_LLM_BASE_URL`, `AGENTCRASH_LLM_API_KEY` | `fetch_live_patch` | OpenAI-compatible endpoint and token. `policy.py` also reads legacy fallback names. |

## 2. CLI

Invoke as `python -m agentcrash <command>` (identical to the console script `agentcrash`; prefer `python -m` on Windows when Smart App Control blocks the script).

```
usage: agentcrash [--version] {check, serve} ...
```

- `--version` anywhere in the arguments prints `agentcrash 0.3.0` and exits 0.
- No arguments prints usage and exits 2. `-h` or `--help` prints usage and exits 0.
- Unknown command prints `agentcrash: unknown command '<x>'` to stderr and exits 2.

### 2.1 `agentcrash check`

Verify registry invariants against an SDK-schema or `TraceWriter` JSONL trace.

```
usage: agentcrash check [-h] --trace TRACE [--invariant INVARIANT]
                        [--plugin PLUGIN] [--state STATE]
                        [--report-out REPORT_OUT] [--format {text,json}]
                        [--scan-pii]
```

| Flag | Required | Default | Meaning |
|---|---|---|---|
| `--trace TRACE` | yes | none | JSONL trace file. |
| `--invariant INVARIANT` | in practice yes | none | Invariant id. Repeatable. With none, `check` exits 2 (`no invariants: pass at least one --invariant <id>`). |
| `--plugin PLUGIN` | no | none | Dotted module imported before judging so it can register host invariants (for example `my_agent.invariants`). Repeatable. |
| `--state STATE` | no | none | `env_state/v1` JSON file passed to verifiers as `state`. |
| `--report-out REPORT_OUT` | no | `reports/<run_id>.verdict.json` | Verdict report path. Parent directories are created. |
| `--format {text,json}` | no | `text` | In `json` mode human lines go to stderr and the report JSON goes to stdout. |
| `--verify-chain` | no | off | Verify the hash chain before judging: `verify_sdk_chain` for one SDK run, `verify_chain` for a `TraceWriter` file. A break exits 3 (`E_TRACE_CORRUPT`). |
| `--scan-pii` | no | off | Scan the raw trace and state files for emails and card-like digit runs (redaction placeholders exempt). Any hit exits 1. |

Built-in invariant ids, registered automatically:

| Id | Reads | Passes when |
|---|---|---|
| `duplicate_mutating_effect` | SDK events (`kind == "tool"`, `status == "ok"`) | No tool succeeds twice with identical `inputs`. |
| `required_step:<name>` | any event `step` | At least one event has `step == <name>`. |
| `fresh_write_only`, `fresh_write_only:<tool>[,<tool>]` | SDK events (`kind == "tool"`: `inputs.op`, `inputs.expected_version`, `outputs.version_conflict` or `outputs.conflict`) or `TraceWriter` envelopes (`args.op`, `detail.data`) | Every successful write (`op` is `write` or absent) to the named tools (default `key_value_store`) sent an `expected_version` and got no conflict. With no matching write it passes and says `nothing to judge (vacuous)`. |
| `no_lost_update` | SDK `fault` events (`kind: concurrent_update`, `key`, `updates`) or envelope `injector_event`, plus `state.record_versions` | Every rival value survives in the final state. Vacuous pass with no rival writer; a rival writer with no `record_versions` in `--state` exits 3. |

A plugin imported with `--plugin` may override any built-in id.

Order of operations: validate `--invariant` -> register built-ins -> import plugins -> load trace -> load state -> run verifiers -> write report -> PII scan -> print results. The report is written before the PII scan runs.

Output in text mode, one line per invariant and a footer:

```
[PASS] Invariant duplicate_mutating_effect satisfied (no duplicate mutating tool effects)
[FAIL] Invariant required_step:confirm violated! (step 'confirm' missing from trace)
verdict=FAIL report=reports/run_ab12cd34ef56.verdict.json
```

With `--scan-pii` a `PII scan: clean (<files>)` line or a hit list (`<file>:<line> <kind> <sample>`) precedes the invariant lines.

#### Exit codes

| Code | Name | Condition |
|---|---|---|
| 0 | OK | Verdict `PASS` (and PII scan clean if requested). |
| 1 | VERDICT_FAIL | Verdict `FAIL`, or `--scan-pii` found a raw PII hit (`E_PII_LEAK`). |
| 2 | USAGE | `E_CONFIG`: no `--invariant`, unknown invariant id, plugin import failure, trace file or state file not found, or argparse usage error. |
| 3 | CORRUPT | `E_TRACE_CORRUPT`: trace or state unreadable, invalid JSON, empty trace, non-object event, or a verifier raised `VerifierError`. |

`cli.py` also defines `EXIT_POLICY_REJECTED = 4` and `EXIT_RUNABORTED = 5` as shared constants for host CLIs. `check` and `serve` never return them.

Error reporting: text mode prints `agentcrash check: <code>: <hint>` to stderr. `--format json` prints `{"api_version": "v1", "code": ..., "exit": ..., "hint": ...}` to stdout.

Example:

```
python -m agentcrash check --trace traces/run.jsonl --invariant duplicate_mutating_effect --invariant required_step:confirm --format json --report-out reports/run.verdict.json
```

### 2.2 `agentcrash serve`

Run the control plane (Lab UI plus `/v1` API) using the stdlib server.

```
usage: agentcrash serve [-h] [--port PORT] [--bind BIND]
```

| Flag | Default | Meaning |
|---|---|---|
| `--port PORT` | `8901` | TCP port (int). |
| `--bind BIND` | `127.0.0.1` | Bind address. Keep loopback unless `AGENTCRASH_API_TOKENS` is set. |

Prints `AgentCrash control plane on http://<bind>:<port>/ (Lab + /v1 API)` and serves until Ctrl-C. Exit 0 on Ctrl-C. Argparse errors (for example a non-integer port) exit 2.

## 3. Python API

All signatures below are in 0.3.0. Names marked (top) are importable from `agentcrash`.

### 3.1 SDK (`agentcrash.sdk`)

```python
@dataclass
class WorkflowConfig:                       # (top)
    project: str = "default"
    environment: str = "development"
    fault_injection: bool = False
    enabled: bool = True
    endpoint: str | None = None             # e.g. "http://127.0.0.1:8901"
    api_key: str | None = None              # sent as Authorization: Bearer
    max_queue: int = 1000                   # <= 0 means unbounded
    seed: int | None = None

class AgentCrash:                           # (top)
    def __init__(self, config: WorkflowConfig | None = None, **kwargs) -> None
    def run(self, name: str) -> RunContext
    def workflow(self, name: str | None = None) -> Callable   # decorator, sync and async
    def configure_faults(self, faults: dict[str, dict[str, Any]]) -> None
    def trace(self, run_id: str) -> list[dict[str, Any]]
    exporter: _Exporter    # .flush(), .drain(), counters .sent .failed .dropped

class RunContext:                           # (top)
    run_id: str
    faults_fired: list[dict[str, Any]]
    def __enter__(self) -> RunContext        # records run_start
    def __exit__(...)                        # records run_end, flushes exporter
    def step(self, name: str) -> _StepContext
    def maybe_fault(self, boundary: str, call_index: int | None = None) -> dict[str, Any] | None

class _StepContext:                         # returned by RunContext.step
    def record_tool(self, tool: str, *, inputs=None, outputs=None,
                    status: str = "ok", error: str | None = None,
                    fault: dict | None = None) -> None

@dataclass
class FaultPolicy:                          # (top)
    faults: dict[str, dict[str, Any]]
    seed: int | None
    def should_fire(self, boundary: str, call_index: int = 1) -> dict | None
```

Contract:

- No SDK call raises into host code for export or recording problems. Exceptions raised by the host inside a `run` or `step` propagate unchanged and are recorded as `status="error"`.
- `AGENTCRASH_DISABLED=1` or `enabled=False`: `@workflow` calls the function directly, and `_record` returns `None`.
- Faults fire only with `fault_injection=True`. `maybe_fault` returns the descriptor dict or `None`. The host decides how to simulate it.
- Each `maybe_fault(boundary)` call counts as one call of that boundary in the run, so `occurrence: N` fires on exactly the Nth call. Counting restarts per run. Pass `call_index` to supply your own count.
- `@workflow` records a `result` event with `{"value": str(out)[:2000]}`.
- Delivery: batches of 50, or at run end, to `<endpoint>/v1/events` with a 2 s timeout.

### 3.2 Events (`agentcrash.events`)

```python
SCHEMA_VERSION = 1
def make_event(*, event_id, run_id, project, workflow, step=None, kind, status="ok",
               seq, tool=None, inputs=None, outputs=None, error=None, fault=None,
               parent_step=None) -> dict[str, Any]     # (top)
def validate_event(event: dict[str, Any]) -> list[str]  # (top) empty list means valid
```

### 3.3 Tracer (`agentcrash.tracer`)

```python
class TraceWriter:
    def __init__(self, run_id: str, scenario: str, path: str) -> None
    def write(self, *, step: int, tool=None, args=None, outcome="info", latency=0,
              injector_event=None, policy_decision=None, detail=None,
              ts_virtual: int | None = None) -> dict[str, Any]
    def close(self) -> None            # also a context manager
def verify_chain(path: str) -> dict[str, Any]      # {"ok", "first_bad", "reason"}
def compute_run_id(scenario_text: str, policy_text: str, seed: int) -> str  # 16 hex chars
def redact(obj: Any) -> Any
```

`VirtualClock(tick_ms=50, t=0)` in `agentcrash.clock` has `now()`, `sleep(ms)` and `deadline(ms)`.

### 3.4 Faults (`agentcrash.injector`)

```python
KNOWN_FAULT_KINDS = ("timeout_after_commit", "timeout_before_commit",
                     "poisoned_doc", "concurrent_update")
FAULT_CATALOG: tuple[dict, ...]
def catalog_entry(kind: str) -> dict
def validate_fault_entry(kind: object, params: object) -> dict    # raises UnknownFaultError
class FaultSchedule:
    def __init__(self, faults: list[dict], rng=None)
    @classmethod
    def from_scenario(cls, scenario: dict, rng=None) -> FaultSchedule
    def fault_for(self, step: int, tool: str) -> dict | None
    def fault_for_call(self, tool: str, op: str | None, occurrence: int) -> dict | None
    def concurrent_for(self, step: int, tool: str) -> dict | None
    def concurrent_for_call(self, tool: str, op: str | None, occurrence: int) -> dict | None
```

### 3.5 Verifier protocol and registry (`agentcrash._registry`, re-exported by `agentcrash.verifiers`)

```python
class Verdict:
    def __init__(self, invariant_id: str, passed: bool, evidence: str) -> None
    def to_dict(self) -> dict      # {"id", "passed", "evidence"}

class Verifier(Protocol):
    id: str
    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict: ...

def register_verifier(verifier: Verifier) -> None
def get_verifier(invariant_id: str) -> Verifier      # raises UnknownVerifierError
def verifier_ids() -> list[str]
```

Rules for a verifier:

1. Pure: read only `trace` and `state`. No network, wall clock or model call.
2. Return `Verdict(self.id, ...)`. `run_verifiers` rejects a verdict whose `invariant_id` differs from the requested id.
3. A violated invariant is `Verdict(passed=False)` with step-level evidence. A trace or state the verifier cannot judge raises `VerifierError` (reported as exit 3, never as PASS or FAIL).
4. Register at import time of the plugin module so `check --plugin my_pkg.invariants` picks it up.

Error types: `VerifierError` (harness problem, exit 3), `UnknownVerifierError` (exit 2), `TraceCorruptError` (exit 3).

Example plugin:

```python
# my_agent/invariants.py
from agentcrash.verifiers import Verdict, register_verifier

class RefundLimit:
    id = "refund_limit"
    def check(self, trace, state):
        bad = [e["step"] for e in trace
               if e.get("tool") == "payments.refund" and e.get("inputs", {}).get("amount", 0) > 100]
        return Verdict(self.id, not bad, f"refund over limit at steps {bad}" if bad else "within limit")

register_verifier(RefundLimit())
```

### 3.6 Invariants and evaluation

```python
class RequiredStep:               # (top)  id = "required_step:<step>"
    def __init__(self, step: str) -> None
class DuplicateMutatingEffect: ...   # (top)  id = "duplicate_mutating_effect"
class FreshWriteOnly:             # agentcrash.verifiers  id = "fresh_write_only"
    def __init__(self, tools: tuple[str, ...] | list[str] | None = None) -> None
class NoLostUpdate: ...           # agentcrash.verifiers  id = "no_lost_update"

def run_verifiers(trace, state, invariants: list[str | dict]) -> dict   # {"verdict", "results"}
def load_trace_events(path: str) -> list[dict]       # TraceCorruptError on problems
def load_state_snapshot(path: str) -> dict
def evaluate_trace(trace, invariants: list[Invariant], state=None, run_id="") -> dict   # (top)
def compare_reports(baseline: dict, candidate: dict) -> dict                          # (top)
```

### 3.7 Policy (`agentcrash.policy`)

```python
def load_policy(path) -> tuple[dict, str]              # PolicyError
def validate_policy_data(data, source="policy") -> dict
def load_patch_doc(path) -> dict                       # PolicyRejectedError
def validate_patch_data(data, source="patch") -> dict
def normalize_rule(rule) -> dict
def merge_patch(base, patch)                           # RFC 7396
def apply_patch(base_policy: dict, patch_doc: dict) -> tuple[dict, str]
def diff_policies(old: dict, new: dict) -> str
def fetch_live_patch(*, trace_summary: str, verdict_summary: str, base_policy: dict,
                     model: str, timeout_s: int = 30, source_run_id: str = "") -> dict | None
```

`fetch_live_patch` returns `None` when `AGENTCRASH_LLM_BASE_URL` (or its legacy fallback) is unset or on transport failure. It raises `PolicyRejectedError` when the model answers with something that is not a valid `policy_patch/v1`. The model name is a caller argument. `policy.DEFAULT_MODEL` exists as a constant for hosts that want a default.

### 3.8 Ledger (`agentcrash.ledger`)

```python
class Ledger:
    def kv_seed(self, key: str, value: dict) -> dict
    def kv_read(self, key: str) -> dict | None
    def kv_write(self, key: str, value: dict, expected_version: int | None) -> dict   # {"version", "conflict"}
    def kv_apply_concurrent(self, key: str, updates: dict) -> dict
    def record_doc(self, doc_id: str, fixture: str, label: str) -> None
    def snapshot(self, run_id: str) -> dict            # env_state/v1
    @staticmethod
    def ledger_hash(snapshot: dict) -> str
```

### 3.9 Control-plane helpers (`agentcrash.server`, all re-exported at top level except `serve` and `create_fastapi_app`)

```python
register_project(project: str, environment: str = "development") -> dict
ingest_events(events: list[dict]) -> dict              # {"accepted","duplicate","invalid","schema_version"}
submit_evaluation(run_id: str, results: list[dict]) -> dict
evaluation_summary(run_id: str) -> dict | None
run_detail(run_id: str) -> dict | None
reliability_summary(project: str) -> dict
prune_runs(project: str, keep_last_n: int | None = None,
           older_than_days: float | None = None) -> dict
reset_store() -> None                                  # tests only
serve(host: str = "127.0.0.1", port: int = 0) -> tuple[HTTPServer, int]
create_fastapi_app() -> FastAPI                        # needs the `server` extra
```

## 4. HTTP API (`/v1`)

Transport: JSON over HTTP. Delivery is at-least-once and ingestion is idempotent on `event_id`.

Auth: if `AGENTCRASH_API_TOKENS` is non-empty, every `/v1/*` request needs `Authorization: Bearer <token>` with a token from the list, else 401. Lab assets stay public. With no tokens set, local traffic is accepted and the deployment is effectively `untrusted-local`.

Two servers share one in-memory store. `python -m agentcrash serve` runs the stdlib server. `create_fastapi_app()` builds the FastAPI app (for example under uvicorn). They differ in a few routes and error shapes, listed in 4.3.

### 4.1 Endpoints

| Method and path | Request | Success response |
|---|---|---|
| `POST /v1/projects` | `{"project": str, "environment"?: str}` (default `development`) | `{"project", "environment", "runs"}`. Idempotent. |
| `POST /v1/events` | `{"events": [event, ...]}` | `{"accepted": int, "duplicate": int, "invalid": int, "schema_version": 1}` |
| `GET /v1/runs?project=<p>` | none. `project` optional filter | `{"runs": [{"run_id", "project", "workflow", "events": <count>}]}` |
| `GET /v1/runs/{run_id}` | none | `{"run_id", "project", "workflow", "faults": [fault...], "events": [event... by seq], "evaluation": [row...] or null}` |
| `POST /v1/evaluations` | `{"run_id": str, "results": [{"id": str, "passed": bool, "evidence"?: str}]}` | `{"run_id", "invariants", "passed", "failed", "verdict": "PASS"/"FAIL"}`. Re-posting the same ids overwrites them. |
| `GET /v1/runs/{run_id}/evaluation` | none | `{"run_id", "results": [{"id","passed","evidence"}], "verdict"}` |
| `GET /v1/reliability/{project}` | none | `{"project", "runs", "fault_runs", "delivery": "at-least-once/idempotent"}` |
| `POST /v1/admin/prune` | `{"project": str, "keep_last_n"?: int >= 0, "older_than_days"?: number >= 0}` (at least one) | `{"project", "dropped": int, "kept": int}` |
| `GET /`, `GET /lab`, `/lab/app.js`, `/lab/styles.css` | none | Lab UI static files. |

Event objects follow doc 06 section 5.1. An event is `invalid` when it is not an object, has no `event_id`, or fails `validate_event`. A duplicate `event_id` is counted as `duplicate` and the first copy is kept. A run's events are keyed by `run_id` and ordered by `seq` on read.

### 4.2 Errors

| Status | When | Stdlib body | FastAPI body |
|---|---|---|---|
| 400 | Missing `project`, `events` not a list, missing `run_id`, bad `results`, bad prune arguments | `{"error": "<message>"}` | `{"detail": "<message>"}` |
| 401 | Token required and missing or wrong | `{"error": "unauthorized"}` | `{"detail": "unauthorized"}` |
| 404 | Unknown path, unknown run, or no evaluation | `{"error": "not found"}` or `{"error": "run not found"}` | `{"detail": "run not found"}` or `{"detail": "no evaluation for run"}` |

Messages: `project is required`, `events must be a list`, `run_id and results are required`, `project and keep_last_n and/or older_than_days required`, plus the `ValueError` or `TypeError` text from `submit_evaluation` and `prune_runs`.

### 4.3 Server differences

Both servers expose the same `/v1` routes and the same `GET /v1/runs` row
shape (`run_id`, `project`, `workflow`, `events` count); a test pins the
route parity. Remaining differences:

| Behavior | stdlib `serve` | FastAPI app |
|---|---|---|
| Error body key | `{"error": ...}` | `{"detail": ...}` (FastAPI convention) |
| Lab | Explicit routes above | Mounted at `/lab` |

Both stores are in-memory and reset on restart.

## 5. Compatibility and versioning

- HTTP routes are versioned by the `/v1` prefix. A breaking change adds `/v2`.
- The event `schema_version` is `1`. The server rejects other values as `invalid`.
- Exit codes 0 to 3 are stable for `check`. New failure classes must reuse a code or add a new one and be recorded in CHANGELOG.
- The verdict `api_version` is `"v1"`. Adding keys is non-breaking. Removing or renaming keys needs `v2`.
- Python API changes are recorded in CHANGELOG.

## 6. Open questions

| # | Question | Owner | Resolves by |
|---|---|---|---|
| 1 | Unify the error body key (`error` vs FastAPI `detail`) | CTO | R4 |
| 2 | Let `check` take a `FreshWriteOnly` tool list from the command line instead of requiring a plugin | CTO | R3 |
