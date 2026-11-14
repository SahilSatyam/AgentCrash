# 03 — Requirements and Software Requirements Specification (SRS)

**Status:** Current for `agentcrash-core` 0.3.0 · **Owner:** Founder (scope sign-off) · **Milestone scheme:** M0–M3 (ADR-006)

> ADLC lens: the SRS describes what the shipped package does today, and every requirement is
> testable in one command. A requirement names the test that proves it. "Gap" means no test
> exists yet; gaps are listed in §7 and mapped to roadmap items. No day-wise timeline is used;
> gating is by milestone exit criteria.

## 1. System purpose and users

### 1.1 Purpose

AgentCrash is chaos engineering for tool-using AI agents, shipped as the PyPI package
`agentcrash-core` (import name `agentcrash`, MIT, Python 3.11+, runtime deps `pyyaml` and
`jsonschema`; extras `server` = fastapi + uvicorn, and `dev`). It answers one question agent
evals cannot: *did the agent's path to the answer respect safety and correctness invariants
when the world misbehaved?*

It is framework-neutral. The host owns the agent loop (plain Python, LangGraph, OpenAI Agents
SDK, CrewAI, MCP). AgentCrash observes it, injects faults at named tool boundaries, and judges
the resulting trace. Hard rule: deterministic invariants own verdicts; a model may only
PROPOSE policy patches, and every patch is schema-gated.

The core is domain-free (ADR-009). `tests/test_generic_core.py` fails if domain vocabulary
enters `src/`. Earlier prototype scope (scenario runner, stub agent, `run` / `diagnose` /
`replay` commands, payment scenarios, golden suite, model adapter) was removed in 0.3.0; see
ADR-009 and `CHANGELOG.md`.

### 1.2 System boundary

In scope (0.3.0):

| Component | Module | Role |
|---|---|---|
| SDK | `sdk.py`, `events.py` | Capture runs, steps, tool calls, faults as v1 events; export best-effort |
| Fault injector | `injector.py` | Fault catalog, parameter validation, trigger matching |
| Tracer | `tracer.py`, `clock.py` | Standalone JSONL writer, redaction, hash chain, `verify_chain`, `VirtualClock` |
| Invariants | `invariants.py`, `verifiers.py`, `_registry.py`, `evaluate.py` | Verdicts, registry, report comparison |
| Policy | `policy.py`, `schemas/` | `policy/v1`, `policy_patch/v1`, merge, diff, opt-in live proposer |
| Ledger | `ledger.py` | Synthetic versioned store, a test double |
| Control plane | `server.py`, `lab/` | `/v1` ingestion + query API, retention, Lab UI |
| CLI | `cli.py` | `check`, `serve`, `--version` only |

Out of scope (0.3.0): running the host's agent, replaying it, and diagnosing from the CLI.
These return as a host-supplied runner under roadmap R6. Also out: framework adapters (R1),
new fault kinds (R2), more invariants (R3), reliability sweeps (R4), OpenTelemetry export
(R5), hosted SaaS, tamper-proof traces (the hash chain is an integrity signal only).

### 1.3 User classes

| Class | Goal | Interface |
|---|---|---|
| UC-1 Agent developer (primary) | Instrument a workflow, inject a fault, see which invariant breaks | SDK + `agentcrash check` |
| UC-2 CI job (headless) | Fail the build on a violated invariant or a PII leak | `check` exit codes + JSON report |
| UC-3 Team lead / reviewer | Browse runs, verdicts and reliability per project | `agentcrash serve` Lab UI + `/v1` API |

### 1.4 Operating environment

- Python 3.11, 3.12, 3.13 (CI matrix in `.github/workflows/ci.yml`, ubuntu-latest). Developed on Windows 11.
- All tests are offline. No network, GPU or model is needed for the core.
- On Windows with Smart App Control, prefer `python -m agentcrash` over the console script; both run the same `main()`.

## 2. Functional requirements

Convention: Priority P0 = required for 0.3.0, P1 = should have. Acceptance is Given/When/Then.
"Proof" names the test file (and test) that exercises it; `gap` means no automated test yet.
Neutral example tools: `payments.charge`, `crm.update`, a `kv` store.

### 2.1 SDK capture

| ID | Requirement | P | Acceptance | Proof |
|---|---|---|---|---|
| FR-01 | `AgentCrash(WorkflowConfig(...))` records `run_start`, `step_start`, `tool`, `fault`, `step_end`, `run_end` events through `ac.run(name)`, `run.step(name)` and `step.record_tool(tool, inputs, outputs, status, error, fault)`. | P0 | **Given** an SDK with default config, **when** a run opens one step and closes, **then** `ac.trace(run_id)` lists `run_start, step_start, step_end, run_end` in order, each carrying the run's `run_id` and the configured `project`. | `tests/test_sdk.py::test_workflow_decorator_async_and_steps` |
| FR-02 | `@ac.workflow(name)` traces a host function as one run, sync or async, and records its return value (truncated to 2000 chars) as a `result` event. Exceptions propagate to the host and are recorded as `run_end` with `status=error`. | P0 | **Given** a decorated function returning 42, **when** it is called, **then** the host gets 42, one trace exists, and its first and last kinds are `run_start` and `run_end`. | `tests/test_sdk.py::test_workflow_decorator_sync_captures_run`, `::test_workflow_decorator_async_and_steps` (error path: gap) |
| FR-03 | `AGENTCRASH_DISABLED=1` or `WorkflowConfig(enabled=False)` makes every SDK call a no-op passthrough. | P0 | **Given** `AGENTCRASH_DISABLED=1`, **when** a run is opened and closed, **then** `ac.traces == {}` and the host code ran unchanged. | `tests/test_sdk.py::test_disabled_mode_passthrough` (`enabled=False` path: gap) |
| FR-04 | With no `endpoint`, capture is local only and never blocks. | P0 | **Given** no endpoint, **when** a run completes, **then** events are available from `ac.trace(run_id)` and no network call is made. | `tests/test_sdk.py` (all offline tests) |
| FR-05 | The export queue is bounded by `max_queue` (default 1000) and drops the OLDEST event when full, counting drops in `exporter.dropped`. `max_queue <= 0` means unbounded. | P0 | **Given** `max_queue=4`, **when** a run emits more than 4 events, **then** `exporter.dropped > 0`. | `tests/test_sdk.py::test_bounded_queue_drops_oldest` |
| FR-06 | With an `endpoint`, events flush in batches of 50 or at run end (never per step) as `POST {endpoint}/v1/events` with `Authorization: Bearer <api_key>` when `api_key` is set. Failures are counted in `exporter.failed` and the batch is re-queued. Delivery is at-least-once. | P0 | **Given** an unreachable endpoint, **when** a run with one step finishes, **then** `exporter.failed == 0` before run end, `> 0` after it, `sent == 0`, and local capture is intact. | `tests/test_sdk.py::test_dead_endpoint_never_breaks_host` |
| FR-07 | The SDK never raises into host code because of capture. Non-JSON values (objects, dates) are stringified for hashing. | P0 | **Given** a tool output containing a `datetime.date` and a custom object, **when** `record_tool` runs, **then** a `tool` event is captured and no exception escapes. | `tests/test_generic_core.py::test_sdk_accepts_non_json_tool_outputs`, `tests/test_sdk.py::test_dead_endpoint_never_breaks_host` |
| FR-08 | Events follow versioned schema v1 (`schema_version`, `event_id`, `run_id`, `project`, `workflow`, `step`, `kind`, `status`, `seq`, plus `tool`, `inputs`, `outputs`, `error`, `fault`, `parent_step`). `validate_event` returns a list of problems; `seq` must be a positive int. | P0 | **Given** an event with a missing key or `schema_version != 1`, **when** `validate_event` runs, **then** it returns one problem per defect. The server applies it on ingestion (FR-37). | `tests/test_server_ingestion.py` (valid path); invalid path: gap |

### 2.2 Fault triggers

| ID | Requirement | P | Acceptance | Proof |
|---|---|---|---|---|
| FR-09 | The fault catalog (`injector.FAULT_CATALOG`) holds four kinds: `timeout_after_commit` (commit, then report TIMEOUT), `timeout_before_commit` (TIMEOUT, no commit), `concurrent_update` (rival write lands first; requires `key` and `updates`), `poisoned_doc` (inert marker; the fixture bytes carry the injection). | P0 | **Given** the catalog, **when** it is read, **then** exactly these four kinds exist and each has a `params_schema`. | `tests/test_generic_core.py::test_fault_catalog_targets_no_default_tool` |
| FR-10 | No catalog entry targets a default tool. The user names the target tool through `params.tool`. | P0 | **Given** `validate_fault_entry("timeout_after_commit", {"tool": "payments.charge"})`, **then** the result has `params.tool == "payments.charge"` and `params.occurrence == 1`; **and** no `default_params` contains `tool`. | `tests/test_generic_core.py::test_fault_entry_targets_user_named_tool` |
| FR-11 | `concurrent_update` without both `key` and `updates` is refused. | P0 | **Given** `{"tool": "kv"}` only, **when** validated, **then** `UnknownFaultError` says `missing required params: key, updates`. | `tests/test_generic_core.py::test_concurrent_update_requires_key_and_updates` |
| FR-12 | Unknown fault kinds and unknown params are refused with `UnknownFaultError` that lists the known kinds. `FaultSchedule.from_scenario` refuses before any trace is written. | P0 | **Given** kind `alien_fault`, **when** validated, **then** `UnknownFaultError` is raised and its message names `timeout_after_commit` among the known kinds. | gap |
| FR-13 | `FaultSchedule` matches a fault by `at_step` (1-based loop step) or, when `at_step` is null, by `params.tool` / `params.op` / `params.occurrence` (Nth matching call). Call-count triggers are preferred. `tool` or `op`, when set, constrain the match. | P1 | **Given** a fault with `tool=payments.charge, occurrence=2`, **when** `fault_for_call("payments.charge", None, 2)` runs, **then** it returns the fault; for occurrence 1 or another tool it returns `None`. | gap |
| FR-14 | In the SDK, `configure_faults({boundary: {kind, occurrence, ...}})` sets descriptors. `run.maybe_fault(boundary, call_index=None)` counts calls per boundary per run, so `occurrence: N` fires on exactly the Nth call (pass `call_index` to supply your own count). It returns the descriptor and records a `fault` event only when `fault_injection=True` and the boundary is configured; otherwise it returns `None` and records nothing. | P0 | **Given** `fault_injection=True` and `occurrence: 3` on `pay`, **when** `maybe_fault("pay")` is called 4 times in a run, **then** only the 3rd call returns the descriptor, and counting restarts in the next run. With `occurrence: 1`, the 1st call fires and the 2nd returns `None`. | `tests/test_sdk.py::test_fault_fires_on_nth_call_per_run`, `::test_deterministic_fault_and_redaction` (`fault_injection=False` path: gap) |

### 2.3 Redaction

| ID | Requirement | P | Acceptance | Proof |
|---|---|---|---|---|
| FR-15 | Write-time redaction replaces the value of any mapping key matching `*_token`, `*secret*` or `*key*` (case-insensitive) with `[REDACTED]`. `idempotency_key` is exempt. It applies to SDK `inputs`/`outputs` and to TraceWriter `tool`/`args`/`injector_event`/`policy_decision`/`detail`. | P0 | **Given** a tool input `{"api_key": "secret", "n": 1}`, **when** recorded, **then** the event holds `api_key: "[REDACTED]"` and `n: 1`; **given** `idempotency_key`, **then** it is kept. | `tests/test_sdk.py::test_deterministic_fault_and_redaction`, `tests/test_tracer.py::test_redaction_strips_secrets_but_keeps_idempotency_key` |
| FR-16 | Email shapes in strings become `redacted@example.com`; card-like digit runs (13 to 19 digits, optional spaces or dashes) become `4111111111111111`. | P0 | **Given** the arg `{"email": "a@b.co"}`, **when** written by TraceWriter, **then** it reads `redacted@example.com`. | `tests/test_gaps.py::test_card_like_runs_are_redacted`, `::test_short_digit_runs_are_left_alone`, `tests/test_generic_core.py::test_tracer_never_redacts_its_own_hash_fields` |
| FR-17 | Harness-owned fields (`event_id`, `prev_hash`, counters) are never redacted, even when a hash starts with a card-like digit run. | P0 | **Given** a `prev_hash` beginning with 16 digits, **when** an event is written, **then** `prev_hash` is unchanged and `verify_chain` returns `ok`. | `tests/test_generic_core.py::test_tracer_never_redacts_its_own_hash_fields` |
| FR-18 | `check --scan-pii` scans the raw trace and state files for emails and card-like runs (the two redaction placeholders are exempt) and fails the run on any hit. | P1 | See FR-34. | `tests/test_gaps.py::test_scan_pii_flags_raw_pii_and_passes_redacted` |
### 2.4 Trace and hash chain

| ID | Requirement | P | Acceptance | Proof |
|---|---|---|---|---|
| FR-19 | Each SDK event carries `prev_hash = sha256(prev_hash ":" canonical_json(event))`, starting from `GENESIS`. `seq` and `prev_hash` advance together under a lock, so concurrent steps cannot fork the chain. The chain is an integrity signal against accidental loss or reordering, not tamper resistance. | P0 | **Given** 8 threads recording 50 tool events each, **when** events are sorted by `seq`, **then** `seq` is 1..N with no gaps and every `prev_hash` recomputes. | `tests/test_generic_core.py::test_sdk_chain_is_consistent_under_threads` |
| FR-20 | `TraceWriter` writes one sorted-key JSON object per line with `event_id`, `prev_hash`, `ts_virtual`, `run_id`, `scenario`, `step`, `tool`, `args`, `outcome`, `latency`, `injector_event`, `policy_decision`, `detail`. `event_id` ids are gapless; `ts_virtual` is monotonic non-decreasing; `outcome` is one of `success, timeout, error, blocked, info`. | P0 | **Given** a writer, **when** an event has a decreasing `ts_virtual`, an unknown `outcome`, or a negative `step`/`latency`, **then** `ValueError` is raised and nothing is written. | `tests/test_tracer.py::test_envelope_fields_and_sorted_keys`, `::test_gapless_ids_and_monotonic_ts`, `::test_non_monotonic_ts_rejected` |
| FR-21 | Hash chains verify one run on its own. `verify_chain(path)` checks a `TraceWriter` file (gapless ids, `prev_hash`, monotonic `ts_virtual`). `verify_sdk_chain(events)` checks one SDK run: a single `run_id`, gapless `seq` from 1, and `prev_hash = sha256(<previous>:<canonical event JSON without prev_hash>)` from `GENESIS`; it works on events loaded from JSONL. `seq` and the chain are per run. Both name the first bad event. `check --verify-chain` runs the right one and exits 3 on a break. | P0 | **Given** a run's events written to JSONL and one output edited, **when** verified, **then** `ok` is false, `reason` is `prev_hash mismatch` and `first_bad` is that event; a dropped event gives `seq gap`; two runs mixed are refused. | `tests/test_tracer.py::test_chain_verifies_and_names_first_bad_link`, `tests/test_gaps.py::test_each_run_chain_verifies_on_its_own`, `::test_sdk_chain_survives_jsonl_roundtrip_and_detects_tampering`, `::test_sdk_chain_refuses_mixed_runs`, `::test_check_verify_chain_flag_both_formats` |
| FR-22 | `detail` larger than 65,536 bytes is replaced by `{"_truncated": true, "original_bytes": N, "preview": ...}` ending in `...[TRUNCATED detail]`. | P1 | **Given** a 100 KB detail, **when** written, **then** the stored event carries the marker and `original_bytes`. | `tests/test_tracer.py::test_detail_truncation_marker` |
| FR-23 | `compute_run_id(scenario_text, policy_text, seed)` = first 16 hex chars of `sha256(scenario_text + policy_text + str(seed))`. | P1 | **Given** fixed inputs, **then** the id equals the documented rule and is stable across calls. | `tests/test_tracer.py::test_compute_run_id_matches_authority_rule` |
| FR-24 | `VirtualClock` is the only time source for synthetic tools: integer ms, starts at 0, advances only via `sleep(ms)`; `deadline(ms)` does not advance it; negative inputs are refused. | P1 | **Given** a new clock, **then** `now() == 0`; **after** `sleep(50)`, `now() == 50`. | `tests/test_clock.py` (4 tests) |

### 2.5 Invariants

| ID | Requirement | P | Acceptance | Proof |
|---|---|---|---|---|
| FR-25 | `RequiredStep(step)` (id `required_step:<name>`) passes when at least one event has `step == <name>`. | P0 | **Given** a trace with a `charge` step, **when** `required_step:charge` runs, **then** it passes with evidence `step 'charge' present (N event(s))`; for a missing step it fails with `missing from trace`. | `tests/test_generic_core.py::test_check_cli_knows_builtin_invariants` (pass path) |
| FR-26 | `DuplicateMutatingEffect` (id `duplicate_mutating_effect`) fails when two events with `kind=tool`, `status=ok` share the same tool and identical inputs. Traces without such a repeat pass vacuously. | P0 | **Given** two `pay` tool events with equal inputs, **when** checked, **then** the verdict is FAIL and evidence names the tool and both steps. | `tests/test_generic_core.py::test_check_cli_knows_builtin_invariants` |
| FR-27 | `FreshWriteOnly(tools=...)` (id `fresh_write_only`, or `fresh_write_only:<tool>[,<tool>]` on the CLI) judges SDK traces and `TraceWriter` envelopes. A call to a named tool is a write when its `op` is `write` or absent. A write fails when it sent no `expected_version` or the store reported a conflict (`version_conflict` or `conflict`). With no writes to the named tools, it passes and says `nothing to judge (vacuous)`. Default tool list: `key_value_store`. | P1 | **Given** an SDK run whose `kv` write carries no `expected_version`, **when** checked with `fresh_write_only:kv`, **then** FAIL citing that event's `seq`; the fresh run passes. | `tests/test_gaps.py::test_fresh_write_only_judges_sdk_traces`, `::test_fresh_write_only_says_when_nothing_to_judge`, `::test_fresh_write_only_flags_store_conflict_on_sdk_trace`, `::test_check_cli_fresh_write_only_with_named_tools`, `tests/test_generic_core.py::test_fresh_write_only_accepts_user_named_store_tools` |
| FR-28 | `NoLostUpdate` (id `no_lost_update`) reads rival writes from SDK `fault` events (`kind: concurrent_update`, `key`, `updates`) or envelope `injector_event`s, and fails when a rival field does not hold its value in `state["record_versions"]`. No rival: vacuous pass. A rival but no `record_versions`: `VerifierError` (exit 3), never a PASS. | P1 | **Given** an SDK run where a rival sets `note` and the agent blind-writes, **when** checked with the ledger snapshot, **then** FAIL naming `order-1.note`; the read-merge-write run passes. | `tests/test_gaps.py::test_no_lost_update_judges_sdk_traces`, `::test_no_lost_update_envelope_format_and_edge_cases` |
| FR-29 | `run_verifiers(trace, state, invariants)` returns `{"verdict": "PASS"|"FAIL", "results": [{id, passed, evidence}]}`. Unknown ids raise `UnknownVerifierError` (config error). A verifier that returns a bad verdict object or malformed input raises `VerifierError` and is never reported as PASS. | P0 | **Given** an unregistered id, **then** `UnknownVerifierError` lists the known ids. | exercised through `tests/test_generic_core.py::test_check_cli_knows_builtin_invariants`; error paths: gap |
| FR-30 | `evaluate_trace` builds a report (`invariants`, `passed`, `failed`, `verdict`, `results`); `compare_reports(baseline, candidate)` returns `regressions` (pass to fail), `fixes` (fail to pass) and a `regression` boolean. | P1 | **Given** a baseline that passes `x` and a candidate that fails it, **then** `regression` is true and `regressions == ["x"]`. | `tests/test_lab.py::test_compare_reports_flags_regression` |
| FR-31 | Verifiers are pure functions of `(trace, state)`: no network, no clock, no model. Hosts register their own invariants with `register_verifier` (loaded via `check --plugin`). | P0 | **Given** the same trace checked twice, **then** the JSON reports are identical. | gap (determinism is enforced statically by FR-49) |

### 2.6 `agentcrash check` (CLI and exit codes)

`check --trace <jsonl> --invariant <id> [--invariant ...] [--plugin <module>] [--state <json>]
[--report-out <path>] [--format text|json] [--scan-pii]`. Invariants `duplicate_mutating_effect`,
`fresh_write_only`, `no_lost_update` and `required_step:<name>` are built in.

| ID | Requirement | P | Acceptance | Proof |
|---|---|---|---|---|
| FR-32 | Exit `0` when every requested invariant passes; exit `1` when any fails. Text mode prints `[PASS]`/`[FAIL] Invariant <id> ... (<evidence>)` and `verdict=<V> report=<path>`. | P0 | **Given** a trace with a duplicated `pay` call, **when** `check` runs with `duplicate_mutating_effect` and `required_step:charge`, **then** exit is 1, stdout has `[FAIL] Invariant duplicate_mutating_effect` and `[PASS] Invariant required_step:charge`. | `tests/test_generic_core.py::test_check_cli_knows_builtin_invariants` |
| FR-33 | Exit `2` (`E_CONFIG`) for usage or config errors: no `--invariant`, trace or state file not found, unimportable `--plugin`, unknown invariant id. | P0 | **Given** no `--invariant`, **when** `check` runs, **then** exit is 2 and stderr says `E_CONFIG: no invariants`. **Given** `--trace nope.jsonl --format json`, **then** exit 2 and stdout is `{"api_version": "v1", "code": "E_CONFIG", "exit": 2, "hint": ...}`. | `tests/test_gaps.py::test_check_exit_2_usage_errors` |
| FR-34 | Exit `3` (`E_TRACE_CORRUPT`) when the trace or state is not valid JSONL/JSON, is empty, or a verifier cannot judge it. A corrupt trace is never a verdict. | P0 | **Given** a trace whose first line is not JSON, **then** exit is 3 and the message names `line 1: invalid JSON`. | `tests/test_gaps.py::test_check_exit_3_corrupt_inputs` |
| FR-35 | `--scan-pii` scans the trace (and `--state`) after judging and exits `1` with code `E_PII_LEAK` on any raw email or card-like run; otherwise prints `PII scan: clean`. | P1 | **Given** a trace containing `jane@corp.com`, **then** exit is 1 and the hit location is printed. | `tests/test_gaps.py::test_scan_pii_flags_raw_pii_and_passes_redacted` |
| FR-36 | `check` writes the report JSON (`api_version`, `results`, `run_id`, `verdict`; sorted keys) to `--report-out`, default `reports/<run_id>.verdict.json`. With `--format json`, prose goes to stderr and the report is printed to stdout. | P0 | **Given** `--report-out v.json`, **then** the file exists and holds the same `verdict` as the exit code implies. | `tests/test_generic_core.py::test_check_cli_knows_builtin_invariants` (file write), `tests/test_gaps.py::test_check_exit_3_corrupt_inputs` (JSON error document) |
| FR-37 | The CLI surface is exactly `check`, `serve` and `--version`. `--help` documents every flag; an unknown command exits `2` with `unknown command`. `python -m agentcrash` is equivalent to the console script. | P0 | **Given** `agentcrash frobnicate`, **then** exit is 2. **Given** `check --help` / `serve --help`, **then** every flag in the table above appears. | `tests/test_cli_help.py` (4 cases) |

Exit codes `4` (`EXIT_POLICY_REJECTED`) and `5` (`EXIT_RUNABORTED`) are defined in `cli.py` for
host CLIs that reuse the helpers; `check` and `serve` never return them.

### 2.7 Control plane (`server.py`)

`agentcrash serve [--port 8901] [--bind 127.0.0.1]` starts a stdlib loopback server. A lazy
FastAPI app factory (`create_fastapi_app`, needs the `server` extra) exposes the same store.

| ID | Requirement | P | Acceptance | Proof |
|---|---|---|---|---|
| FR-38 | `POST /v1/events` with `{"events": [...]}` is idempotent on `event_id`. The response is `{accepted, duplicate, invalid, schema_version}`. Runs list events in `seq` order. A body whose `events` is not a list gets 400. | P0 | **Given** a batch already ingested, **when** it is posted again, **then** `accepted == 0` and `duplicate == len(batch)`. | `tests/test_server_ingestion.py::test_idempotent_ingestion_and_run_detail`, `::test_http_roundtrip_multi_project` |
| FR-39 | Events that are not objects, lack `event_id`, or fail `validate_event` are counted in `invalid` and not stored. | P0 | **Given** `[{"event_id": "e1"}]` (missing keys), **then** `invalid == 1` and nothing is stored. | gap |
| FR-40 | Auth: when `AGENTCRASH_API_TOKENS` (comma-separated) is non-empty, every `/v1/*` call needs `Authorization: Bearer <token>` from the list, else 401. Unset means loopback mode (all local traffic accepted). Lab static assets stay public. | P0 | **Given** `AGENTCRASH_API_TOKENS=s3cret`, **then** `/v1/projects` returns 401 with no or a wrong token, 200 with `s3cret`, and `/` returns 200 with no token. | `tests/test_server_ingestion.py::test_auth_allowlist_gates_api_but_not_lab` (FastAPI path: gap) |
| FR-41 | Endpoints: `POST /v1/projects` (needs `project`, else 400), `GET /v1/runs[?project=]`, `GET /v1/runs/{id}` (404 `run not found`), `GET /v1/reliability/{project}`, `POST /v1/evaluations` (needs `run_id`; malformed rows give 400), `GET /v1/runs/{id}/evaluation` (404 `no evaluation for run` until one is stored), `POST /v1/admin/prune`. The stdlib and FastAPI servers expose the same routes and the same `GET /v1/runs` row shape (`run_id`, `project`, `workflow`, `events`). | P0 | **Given** a registered project and ingested run, **then** `GET /v1/runs/{id}` returns the project and ordered events and `GET /v1/reliability/{project}` returns `runs == 1`. | `tests/test_server_ingestion.py::test_http_roundtrip_multi_project`, `tests/test_lab.py::test_evaluation_submit_and_run_detail`, `::test_malformed_evaluation_is_client_error` (function-level `TypeError`; the HTTP 400 mapping is untested), `::test_lab_static_and_evaluation_roundtrip`, `tests/test_server_ingestion.py::test_stdlib_and_fastapi_expose_the_same_read_routes` (route parity; skips the FastAPI half when the extra is absent) |
| FR-42 | Evaluation storage is idempotent per invariant id: resubmitting overwrites; the summary verdict is PASS only if all stored verdicts pass. | P1 | **Given** two submissions for the same id, **then** the count of invariants does not grow. | `tests/test_lab.py::test_evaluation_submit_and_run_detail` |
| FR-43 | Retention: `prune_runs(project, keep_last_n, older_than_days)` and `POST /v1/admin/prune`. Keep-last-N uses ingestion order; `older_than_days` drops runs created at or before the cutoff even if keep-last-N would keep them. At least one criterion is required; negative values are refused. Pruning removes the dropped runs' events and evaluations. | P0 | **Given** 3 runs and `keep_last_n=2`, **then** result is `{"project": "p", "dropped": 1, "kept": 2}` and the oldest run is gone. **Given** no criterion or `keep_last_n=-1`, **then** `ValueError` (HTTP 400). | `tests/test_server_ingestion.py::test_prune_enforces_retention`, `::test_prune_over_http_requires_fields` |
| FR-44 | `serve` binds `127.0.0.1` by default. Lab assets are served only for flat `.html`, `.js`, `.css` names; traversal (`..`, `\`, nested `/`) returns 404. The Lab ships no baked demo bundle. | P0 | **Given** `GET /lab/../x`, **then** 404. | `tests/test_no_static_bundle.py` (3 tests); traversal: gap |
| FR-45 | Importing the core pulls in no domain stack, and a second app can integrate with stdlib plus `agentcrash` only. | P1 | **Given** `examples/minimal_agent/app.py`, **then** it imports nothing outside stdlib and `agentcrash`. | `tests/test_lab.py::test_core_import_pulls_no_domain_stacks`, `tests/test_second_app.py` |

Store is in-memory per process; there is no persistence (see §6).

### 2.8 Policy and patch validation

| ID | Requirement | P | Acceptance | Proof |
|---|---|---|---|---|
| FR-46 | `load_policy` / `validate_policy_data` accept only `schema_version: policy/v1` documents that satisfy `schemas/policy.json`. Rules need `rule` (`on_tool_timeout`, `on_mutating_retry`, `on_untrusted_content`, `on_stale_read`), `tool`, `action` (`fail`, `retry_same`, `check_then_act`, `require_confirmation`); `retry.max_attempts` is 0 to 3. Failures raise `PolicyError`. | P0 | **Given** a rule with `rule: bogus`, **then** `PolicyError` / `PolicyRejectedError` states the value is not one of the allowed names. | gap (confirmed by hand on 0.3.0) |
| FR-47 | `validate_patch_data` / `load_patch_doc` accept only `policy_patch/v1` documents with `schema_version`, `source_run_id`, `rationale`, `patch` (object). Anything else raises `PolicyRejectedError` (exit code 4 in host CLIs) before anything runs. | P0 | **Given** a patch without `source_run_id`, **then** `PolicyRejectedError: ... 'source_run_id' is a required property`. | `tests/test_gaps.py::test_malformed_patch_documents_are_rejected`, `::test_patch_file_errors_are_rejections` |
| FR-48 | `apply_patch(base, patch_doc)` applies `patch` as an RFC 7396 JSON Merge Patch (arrays replace, `null` deletes), normalizes shorthand rule keys (`max_retries` 0 to 3 to `retry.max_attempts`; `retry_with_idempotency_key` to `retry.require_idempotency_key`; action aliases `check_transaction_status` to `check_then_act`, `confirm_with_user` to `require_confirmation`, `abort` to `fail`), then re-validates the merged policy. Returns `(new_policy, canonical_yaml)`; the input policy is not mutated. | P0 | **Given** a base `{"schema_version": "policy/v1", "rules": []}` and a patch adding `on_tool_timeout` for `payments.charge` with `max_retries: 2`, **then** the result has `retry.max_attempts == 2`. **Given** a patch yielding an invalid rule name, **then** `PolicyRejectedError`. | `tests/test_gaps.py::test_apply_patch_merges_and_normalizes_shorthand`, `::test_patches_that_break_the_policy_schema_are_rejected` |
| FR-49 | `diff_policies(old, new)` renders one line per added (`+`), removed (`-`) or changed (`~`) rule field, for human review before a patch is accepted. | P1 | **Given** two policies differing in `retry.max_attempts`, **then** one `~ rule/tool: retry.max_attempts a -> b` line appears. | gap |
| FR-50 | Live patch proposal is opt-in: `fetch_live_patch(...)` returns `None` unless `AGENTCRASH_LLM_BASE_URL` is set (key from `AGENTCRASH_LLM_API_KEY`). It speaks to any OpenAI-compatible `/chat/completions` endpoint. Transport failure returns `None`; a 200 response that is not a valid `policy_patch/v1` raises `PolicyRejectedError`. The model never edits a policy file and never decides a verdict. | P0 | **Given** the base URL variable is unset, **when** `fetch_live_patch` is called, **then** it returns `None` and makes no request. | `tests/test_generic_core.py::test_live_patch_is_opt_in` (unparsable-output path: gap) |

### 2.9 Determinism and core boundary

| ID | Requirement | P | Acceptance | Proof |
|---|---|---|---|---|
| FR-51 | `clock.py`, `tracer.py`, `ledger.py`, `injector.py` and `verifiers.py` do not import `time`, `datetime` or `random` (ACR001 lint, static AST check). | P0 | **Given** the repo, **when** `pytest tests/test_acr001.py` runs, **then** it passes. | `tests/test_acr001.py::test_no_wall_clock_or_global_random` |
| FR-52 | No domain vocabulary in `src/agentcrash` (ADR-009). | P0 | **Given** the source tree, **then** the banned-term scan finds no hits. | `tests/test_generic_core.py::test_core_source_has_no_domain_vocabulary` |
| FR-53 | `Ledger` (synthetic versioned store) seeds records at version 1, bumps the version on a fresh write, flags `version_conflict` on a stale write (still committing it), applies rival updates, and has a stable `ledger_hash`. It is a test double, not a product surface. | P1 | **Given** a stale `expected_version`, **when** written, **then** the write commits and `version_conflict` is true. | `tests/test_ledger.py` (6 tests) |

## 3. External interface requirements

- CLI: `agentcrash check|serve`, `--version`; exit codes in FR-32 to FR-34 (0 pass, 1 fail or PII leak, 2 usage/config, 3 corrupt trace).
- Python API: `agentcrash.__all__` exports `AgentCrash`, `WorkflowConfig`, `FaultPolicy`, `RunContext`, `RequiredStep`, `DuplicateMutatingEffect`, `evaluate_trace`, `compare_reports`, `make_event`, `validate_event`, `SCHEMA_VERSION`, and the control-plane functions.
- HTTP: `/v1/projects`, `/v1/events`, `/v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/evaluation`, `/v1/evaluations`, `/v1/reliability/{project}`, `/v1/admin/prune`; Lab UI at `/` and `/lab`.
- Environment variables: `AGENTCRASH_DISABLED`, `AGENTCRASH_API_TOKENS`, `AGENTCRASH_LLM_BASE_URL`, `AGENTCRASH_LLM_API_KEY`.
- Files: SDK-schema JSONL traces (input to `check`), optional state JSON, `policy/v1` YAML, `policy_patch/v1` JSON, verdict report JSON. Schemas ship in `src/agentcrash/schemas/`.

## 4. Data requirements

- Created: SDK events (in memory, optionally exported), TraceWriter JSONL files, verdict reports, in-memory control-plane store (projects, events, runs, evaluations).
- Source of truth for a verdict: the trace plus optional state snapshot passed to `check`. Verifiers never read a live environment.
- Retention: control-plane data is process-local and pruned through `prune_runs` / `/v1/admin/prune` (FR-43). Trace files on disk are the caller's to delete.
- Classification: traces are test data. They may hold synthetic PII-shaped values but must carry no real secrets; FR-15 to FR-18 enforce this.

## 5. Quality requirements (traceability to 12-NFR)

| NFR | Target summary | Covering FRs |
|---|---|---|
| NFR-DET-01…03 (same input gives byte-identical output) | static lint plus repeat-run comparison | FR-20, FR-21, FR-23, FR-31, FR-51 |
| NFR-PERF-01/02 (SDK overhead, trace size) | `bench/run_overhead.py` | FR-01, FR-19, FR-20 |
| NFR-REL-01…03 (no false verdicts, fail-closed errors) | tests + exit-code contract | FR-29, FR-32 to FR-34, FR-47 |
| NFR-SEC-01 (no secrets or PII in artifacts) | redaction tests + `--scan-pii` | FR-15 to FR-18, FR-35 |
| NFR-PORT-01/02 (Python 3.11 to 3.13) | CI matrix | FR-37 |

## 6. Constraints and assumptions

Constraints: (a) deterministic invariants own verdicts; models only propose schema-valid patches (FR-50). (b) The core never imports a domain or host stack (FR-45, FR-52). (c) No `time`, `datetime` or `random` imports in the deterministic modules listed in FR-51. (d) The hash chain is not tamper-proof; there is no signing in v1.

Known limitations, stated so nobody relies on them:

- SDK call counts are per boundary per run and live in the `RunContext`. Async or threaded hosts that interleave calls to one boundary should pass `call_index` themselves for a stable count.
- The control-plane store is in-memory, with no persistence and no request-size limit.

Assumptions: one operator reviews each policy patch before it is applied; hosts that need `run` / `diagnose` / `replay` supply their own runner (R6).

## 7. Acceptance and traceability

| Requirement IDs | Goal | Design element | Verification | Status |
|---|---|---|---|---|
| FR-01…FR-08 | Capture without owning the loop | SDK | `tests/test_sdk.py`, `tests/test_generic_core.py` | Covered; error and `enabled=False` paths gap |
| FR-09…FR-14 | Deterministic fault triggers | Injector | `tests/test_generic_core.py`, `tests/test_sdk.py` | FR-14 covered; FR-12, FR-13 gap (roadmap R2) |
| FR-15…FR-18 | No secrets or PII in traces | Redaction, `scan_pii.py` | `tests/test_tracer.py`, `tests/test_sdk.py`, `tests/test_gaps.py` | Covered |
| FR-19…FR-24 | Verifiable evidence | Tracer, SDK chain | `tests/test_tracer.py`, `tests/test_clock.py` | Covered |
| FR-25…FR-31 | Deterministic verdicts | Invariants, registry | `tests/test_generic_core.py`, `tests/test_lab.py` | Partly covered; verifier error paths gap (R3) |
| FR-32…FR-37 | CI-usable `check` | `cli.py` | `tests/test_generic_core.py`, `tests/test_cli_help.py` | Exit 0/1/2/3 and JSON error document covered (`tests/test_gaps.py`) |
| FR-38…FR-45 | Ingest, query, retain | `server.py` | `tests/test_server_ingestion.py`, `tests/test_lab.py` | Covered for stdlib server; FastAPI routes covered by parity test only |
| FR-46…FR-50 | Schema-gated policy patches | `policy.py` | `tests/test_generic_core.py`, `tests/test_gaps.py` | Patch validation and merge covered; `load_policy` file path gap |
| FR-51…FR-53 | Determinism and domain-free core | ACR001 lint, guard test | `tests/test_acr001.py`, `tests/test_generic_core.py`, `tests/test_ledger.py` | Covered |

Total suite: 56 offline tests (`pytest -q`), plus ruff, black (line length 100) and the ACR001 lint in CI on Python 3.11 to 3.13.

Open questions: OQ-01 SRS sign-off owner is the Founder.

## 8. Change history

| Milestone | Change | Author |
|---|---|---|
| Pre-M0 draft | SRS skeleton with one placeholder FR | Pre-M0 skeleton (author unrecorded) |
| M0 draft | Scenario-runner SRS, FR-01…FR-25 | Group B |
| 0.3.0 rewrite | FRs rewritten to the real surface (SDK, faults, redaction, hash chain, invariants, `check`, control plane, policy); each FR names its proof test or `gap`; removed-feature scope dropped per ADR-009 | Docs rewrite |
