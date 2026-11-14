# 06 -- Data design (AgentCrash)

**Status:** Draft -- **Owner:** Startup CTO (interim) -- **Last reviewed:** Phase-gated (no calendar date)
**Describes:** `agentcrash-core` 0.3.0. The code is the source of truth. Every field below is taken from `src/agentcrash/`.

## 1. Data principles

1. **JSON everywhere, files first.** Traces are JSONL, reports and state are JSON, policies are YAML. There is no database. The control plane keeps an in-memory store.
2. **Versioned schemas.** Each artifact carries a version tag: event `schema_version: 1`, `policy/v1`, `policy_patch/v1`, `env_state/v1`, verdict `api_version: "v1"`.
3. **Redact at write time.** Secrets and PII-shaped values are replaced before anything is stored, exported or hashed.
4. **Determinism.** Canonical JSON (sorted keys), integer virtual time, no wall-clock in judged data.
5. **Integrity signal, not tamper resistance.** Hash chains detect accidental loss or reordering. There is no signing in v1.

## 2. Data inventory

| Artifact | Format | Producer | Consumer | Sensitivity |
|---|---|---|---|---|
| SDK event stream | JSON events (schema v1) | `AgentCrash` (`sdk.py`, `events.py`) | `POST /v1/events`, `check`, host code | Redacted. Host inputs and outputs may still hold business data. |
| Trace file | JSONL, one `TraceWriter` envelope per line | `TraceWriter` (`tracer.py`) | `check`, `verify_chain` | Redacted. |
| Policy | YAML, `policy/v1` | Host | Host runner, `policy.py` | Low. |
| Policy patch | JSON, `policy_patch/v1` | Host or `fetch_live_patch` | `apply_patch` | Low. Model-proposed, schema-gated. |
| State snapshot | JSON, `env_state/v1` | `Ledger.snapshot` | `check --state` | Synthetic. |
| Verdict report | JSON | `check` | CI, humans, `POST /v1/evaluations` | Evidence strings may quote trace steps. |
| Control-plane store | In-memory dicts | `server.py` | Lab, `/v1` API | Lost on restart. |

## 3. Scenario schema

Removed in 0.3.0. The core no longer reads a `scenario.yaml`. The host names fault boundaries in code (`ac.configure_faults`) or builds a `FaultSchedule` from its own fault list, and passes invariant ids to `check --invariant`. `FaultSchedule.from_scenario` and `validate_fault_entry` remain as helpers for hosts that keep their own scenario files. Both reject unknown fault kinds with `UnknownFaultError`.

### 3.1 Fault descriptors

SDK form, keyed by boundary name:

```python
ac.configure_faults({"payments.charge": {"kind": "timeout_after_commit", "occurrence": 1}})
```

`FaultPolicy.should_fire(boundary, call_index)` fires when `int(spec.get("occurrence", 1)) == call_index`. `RunContext.maybe_fault` counts calls per boundary per run, so `occurrence: N` fires on exactly the Nth call.

Catalog (`injector.py`, `FAULT_CATALOG`):

| kind | Effect | Params (`required` marked) | Defaults |
|---|---|---|---|
| `timeout_after_commit` | Write commits, then the tool reports TIMEOUT | `tool`, `op`, `occurrence`, `simulated_latency_ms` | `occurrence=1`, `simulated_latency_ms=2000` |
| `timeout_before_commit` | TIMEOUT without a commit | same as above | same as above |
| `concurrent_update` | Rival writer updates the record first | `tool`, `op`, `occurrence`, `key` (required), `updates` (required, object) | `occurrence=1` |
| `poisoned_doc` | Inert marker. The retrieved fixture bytes carry the injection | `tool`, `doc_id`, `fixture_key` | none |

`validate_fault_entry(kind, params)` returns `{"kind", "at_step": None, "params": <defaults + params>}`. It rejects non-string kinds, unknown kinds, non-object params, unknown param names and missing required params. Triggers: by `tool`/`op`/`occurrence` call count, or by `at_step` (1-based loop step). No fault has a default tool target.

## 4. Policy schema (`policy/v1`) and patch (`policy_patch/v1`)

Schemas live in `src/agentcrash/schemas/policy.json` and `policy-patch.json` (JSON Schema draft-07).

### 4.1 `policy/v1` (YAML or JSON)

```yaml
schema_version: policy/v1
rules:
  - rule: on_tool_timeout
    tool: payments.charge
    action: check_then_act
    check: {tool: payments.status, by: reference}
    retry: {max_attempts: 1, require_idempotency_key: true}
```

| Field | Type | Rule |
|---|---|---|
| `schema_version` | string | Required. Exactly `policy/v1`. |
| `rules` | array | Required. |
| `rules[].rule` | string | Required. One of `on_tool_timeout`, `on_mutating_retry`, `on_untrusted_content`, `on_stale_read`. |
| `rules[].tool` | string | Required. |
| `rules[].action` | string | Required. One of `fail`, `retry_same`, `check_then_act`, `require_confirmation`. |
| `rules[].check` | object | Optional. Free-form. |
| `rules[].retry.max_attempts` | integer | Optional. 0 to 3. |
| `rules[].retry.require_idempotency_key` | boolean | Optional. |
| `rules[].confirmation` | object | Optional. Free-form. |

The schema does not set `additionalProperties: false`, so extra keys pass validation. `load_policy(path)` returns `(data, raw_text)` and raises `PolicyError` on unreadable, malformed or invalid input.

### 4.2 `policy_patch/v1` (JSON)

```json
{
  "schema_version": "policy_patch/v1",
  "source_run_id": "run_ab12cd34ef56",
  "rationale": "Blind retry duplicated a commit after an ambiguous timeout.",
  "patch": {"rules": [{"rule": "on_tool_timeout", "tool": "payments.charge",
                        "action": "check_then_act", "retry": {"max_attempts": 1}}]}
}
```

| Field | Type | Rule |
|---|---|---|
| `schema_version` | string | Required. Exactly `policy_patch/v1`. |
| `source_run_id` | string | Required. `fetch_live_patch` backfills it from its `source_run_id` argument when the model omits it. |
| `rationale` | string | Required. |
| `patch` | object | Required. RFC 7396 JSON Merge Patch applied onto the base policy. |

Application (`apply_patch`): objects merge by key, arrays and scalars replace (so a patch that sets `rules` replaces the whole list), `null` deletes a key. Each rule then goes through `normalize_rule`, which maps shorthand keys: `max_retries` (int 0-3) to `retry.max_attempts`, `retry_with_idempotency_key` (bool) to `retry.require_idempotency_key`, and actions `check_transaction_status` to `check_then_act`, `confirm_with_user` to `require_confirmation`, `abort` to `fail`. The merged result is validated against `policy/v1`. Any failure raises `PolicyRejectedError`. `apply_patch` returns `(new_policy, canonical_yaml_text)`. `diff_policies(old, new)` renders `+`, `-`, `~` and `=` lines per `rule/tool` key.

## 5. Trace schemas

Two JSON shapes exist. `check` loads any JSONL file as a list of objects, and each invariant reads the fields it needs.

### 5.1 SDK event v1 (`events.py`, `make_event`)

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | `1` (`SCHEMA_VERSION`). |
| `event_id` | string | `evt_<run_id>_<seq:04d>`. Idempotency key on the server. |
| `run_id` | string | `run_<12 hex>` from `uuid4`. `disabled_<8 hex>` when the SDK is disabled. |
| `project` | string | `WorkflowConfig.project`. |
| `workflow` | string | Name passed to `ac.run(name)` or `@ac.workflow(name)`. |
| `step` | string or null | Step name. For `fault` events, the boundary name. |
| `parent_step` | string or null | Reserved. The SDK does not set it today. |
| `kind` | string | SDK emits `run_start`, `run_end`, `step_start`, `step_end`, `tool`, `fault`, `result`. |
| `status` | string | `ok` (default) or `error`. |
| `seq` | int | Positive. Counter per `AgentCrash` instance, shared across its runs. |
| `tool` | string or null | Tool name for `tool` events. |
| `inputs` | object | Redacted. `{}` when empty. |
| `outputs` | object | Redacted. `{}` when empty. `result` events hold `{"value": str(out)[:2000]}`. |
| `error` | string or null | `"<ExceptionName>: <message>"` on errors. Not redacted. |
| `fault` | object or null | The fault descriptor that fired. |
| `prev_hash` | string | Added by the SDK after `make_event`. `sha256("<previous prev_hash>:<canonical JSON of the event without prev_hash>")` as hex. First link uses `GENESIS`. `make_event` itself does not emit this key. |

Required keys checked by `validate_event`: `schema_version`, `event_id`, `run_id`, `project`, `workflow`, `step`, `kind`, `status`, `seq`. Also `schema_version == 1` and `seq` an int `>= 1`. `step` must be present but may be null.

### 5.2 `TraceWriter` envelope (`tracer.py`)

One canonical JSON line per event (sorted keys, separators `,` and `:`), written with `TraceWriter(run_id, scenario, path)`.

| Field | Type | Notes |
|---|---|---|
| `event_id` | string | `evt_<run_id>_<seq:04d>`, gapless from 0001. |
| `prev_hash` | string | `GENESIS` first, then `sha256` hex of the previous full line. |
| `ts_virtual` | int | Virtual ms. Defaults to previous `ts_virtual` + `latency`. Must not decrease. |
| `run_id` | string | Passed to the constructor. |
| `scenario` | string | Free-form label passed to the constructor. |
| `step` | int | `>= 0`. |
| `tool` | string or null | Redacted. |
| `args` | object | Redacted. |
| `outcome` | string | One of `success`, `timeout`, `error`, `blocked`, `info` (default `info`). |
| `latency` | int | `>= 0`, virtual ms. |
| `injector_event` | object or null | Redacted. For `concurrent_update`, `NoLostUpdate` reads `fault`, `record`, `updates`. |
| `policy_decision` | object or null | Redacted. |
| `detail` | object | Redacted. `FreshWriteOnly` reads `detail.phase == "tool_result"` and `detail.data.version_conflict` / `detail.data.expected_version`. |

Size cap: when a line exceeds 65,536 bytes (`DETAIL_MAX_BYTES`), `detail` is replaced by `{"_truncated": true, "original_bytes": N, "preview": "<first 65536 chars>...[TRUNCATED detail]"}`. Only caller-supplied fields are redacted. Harness fields (ids, hashes, counters) never are.

`verify_chain(path)` returns `{"ok": true, "first_bad": null, "reason": null}` or `{"ok": false, "first_bad": <event_id>, "reason": ...}` with reasons `invalid JSON`, `gapless event_id broken`, `prev_hash mismatch`, `ts_virtual decreased`.
`compute_run_id(scenario_text, policy_text, seed)` is `sha256(scenario_text + policy_text + str(seed))[:16]`.

### 5.3 Redaction (both shapes)

| Rule | Replacement |
|---|---|
| Mapping key matches `*_token`, `*secret*` or `*key*` (case-insensitive), except `idempotency_key` | Value becomes `[REDACTED]` |
| Email shape inside a string | `redacted@example.com` |
| Digit run of 13 to 19 digits (spaces or dashes allowed) | `4111111111111111` |

Redaction recurses through dicts and lists. The SDK redacts `inputs` and `outputs`. `TraceWriter` redacts `tool`, `args`, `injector_event`, `policy_decision` and `detail`.

## 6. State snapshot (`env_state/v1`)

Produced by `Ledger.snapshot(run_id)` (`ledger.py`). The ledger is a per-run, in-memory versioned key-value store for tests.

```json
{
  "schema_version": "env_state/v1",
  "run_id": "run_ab12cd34ef56",
  "docs": [{"doc_id": "d1", "fixture": "f1", "label": "retrieved"}],
  "record_versions": [{"record": "acct:1", "value": {"balance": 10}, "version": 2}],
  "ledger_hash": "sha256:<64 hex>"
}
```

| Field | Notes |
|---|---|
| `docs` | Spans recorded with `record_doc(doc_id, fixture, label)`. |
| `record_versions` | Sorted by `record`. Seeded records start at version 1. Each write or concurrent apply adds 1. |
| `ledger_hash` | `"sha256:" + sha256(canonical JSON of all other fields)`. `Ledger.ledger_hash(snapshot)` recomputes it. |

Store semantics: `kv_write(key, value, expected_version)` always commits and bumps the version. It returns `{"version", "conflict"}` with `conflict` true when `expected_version` differs from the current version (a store with no optimistic concurrency, by design). `kv_apply_concurrent(key, updates)` merges `updates` and returns `{"version_before", "version_after"}`. `kv_read` returns `{"value", "version"}` or `None`.

`check --state` only requires the file to be a JSON object. `NoLostUpdate` reads `record_versions[].record` and `.value`. Missing keys read as empty.

## 7. Verdict report JSON

Written by `check` to `--report-out` (default `reports/<run_id>.verdict.json`) with sorted keys, 2-space indent and a trailing newline. With `--format json` the same document is also printed to stdout.

```json
{
  "api_version": "v1",
  "results": [
    {"evidence": "duplicate effect(s): payments.charge at steps charge and charge",
     "id": "duplicate_mutating_effect", "passed": false}
  ],
  "run_id": "run_ab12cd34ef56",
  "verdict": "FAIL"
}
```

| Field | Notes |
|---|---|
| `api_version` | `"v1"`. |
| `results[]` | One `Verdict.to_dict()` per requested invariant, in request order: `id`, `passed` (bool), `evidence` (string). |
| `run_id` | `run_id` of the first trace event, or `core-check` when absent. |
| `verdict` | `PASS` only when every result passed, else `FAIL`. |

Error document (with `--format json`, printed instead of the report): `{"api_version": "v1", "code": "<E_...>", "exit": <int>, "hint": "<text>"}`. Codes: `E_CONFIG` (exit 2), `E_TRACE_CORRUPT` (exit 3), `E_PII_LEAK` (exit 1). In text mode the error goes to stderr as `agentcrash check: <code>: <hint>`.

`evaluate_trace(trace, invariants, state=None, run_id="")` returns a different, library-level report: `run_id`, `invariants`, `passed`, `failed`, `verdict`, `results`. `compare_reports(baseline, candidate)` returns `baseline`, `candidate`, `regressions` (ids that went pass to fail), `fixes` (fail to pass), `regression` (bool).

## 8. Control-plane store (`server.py`)

In-memory, process-local, cleared by `reset_store()`.

| Collection | Key | Value |
|---|---|---|
| projects | `project` | `{project, environment, runs}` |
| events | `event_id` | the event dict as ingested |
| runs | `run_id` | `{run_id, project, workflow, events: [event_id...], created_at}` (`created_at` is server wall-clock seconds) |
| evaluations | `run_id` | `{invariant id: {id, passed, evidence}}` |

Ingestion is idempotent on `event_id`. Run detail returns events sorted by `seq`, plus `faults` (non-null `fault` fields) and `evaluation`.

## 9. Retention, versioning and compatibility

- **Retention:** `prune_runs(project, keep_last_n=None, older_than_days=None)` and `POST /v1/admin/prune`. At least one criterion is required. `keep_last_n` must be a non-negative int. `older_than_days` must be a non-negative number. Runs at or before the cutoff are dropped even if `keep_last_n` would keep them. Dropped runs lose their events and evaluations. Local trace and report files are never pruned by the core.
- **Versioning:** event schema is `1`. The server rejects other versions as `invalid`. New fields must be additive within a version. A breaking change bumps the version tag.
- **Redaction exemptions:** the `*key*` pattern also hides keys such as `api_key`. `idempotency_key` is exempt so duplicate-effect evidence stays visible.

## 10. Validation rules

| Where | Rule | Failure |
|---|---|---|
| `validate_event` | Required keys present, `schema_version == 1`, `seq` int >= 1 | Problem strings. The server counts the event as `invalid`. |
| `ingest_events` | Event is an object with a non-empty `event_id` | Counted `invalid`. |
| `load_trace_events` | Readable, each non-blank line a JSON object, at least one event | `TraceCorruptError`, exit 3. |
| `load_state_snapshot` | Readable JSON object | `TraceCorruptError`, exit 3. |
| `run_verifiers` | Each invariant id is a string or `{"id": ...}`. Known id. Verifier returns a `Verdict` whose `invariant_id` equals the requested id | `UnknownVerifierError` (exit 2) or `VerifierError` (exit 3). |
| Built-in verifiers | Trace is a list of objects. `NoLostUpdate` also needs `state` to be an object | `VerifierError`, exit 3. |
| `TraceWriter.write` | `outcome` in the allowed set, `step >= 0`, `latency >= 0`, `ts_virtual` non-decreasing | `ValueError`. |
| `validate_fault_entry` | Known kind, known params, required params present | `UnknownFaultError`. |
| `validate_policy_data` | Mapping, `schema_version == "policy/v1"`, JSON Schema | `PolicyError`. |
| `validate_patch_data` | Object, `schema_version == "policy_patch/v1"`, JSON Schema, `patch` is an object | `PolicyRejectedError`. |
| `submit_evaluation` | `results` is a list, each row has an `id` | `TypeError` or `ValueError`, HTTP 400. |
| `check --scan-pii` | No raw email or card-like digit run in the trace or state file. The two redaction placeholders are exempt | `E_PII_LEAK`, exit 1. |

## 11. Open questions

| # | Question | Owner | Resolves by |
|---|---|---|---|
| 1 | One trace shape for SDK events and `TraceWriter` lines | CTO | R5 |
| 2 | Pass `call_index` from the SDK so `occurrence > 1` fires in `maybe_fault` | CTO | R2 |
| 3 | Make `seq` per run instead of per `AgentCrash` instance | CTO | R5 |
| 4 | Persist the control-plane store | CTO | After R4 |
