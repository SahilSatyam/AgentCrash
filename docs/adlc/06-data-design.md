# 06 -- Data design (AgentCrash)

**Status:** Draft -- **Owner:** Startup CTO (interim) -- **Last reviewed:** Phase-gated (no calendar date)
**Gate:** Approved when the v0 golden trace (`timeout_retry`, seed 42) validates against every schema in this doc via `check --strict-schema`. No day-wise timeline is used anywhere -- only phase/milestone gates.

## 1. Startup lens and data principles

For a pre-revenue startup, data design must minimize storage cost and maximize inspectability. Principles: (a) everything a verdict depends on is a plain-text file in git or a local JSONL trace -- no database to operate; (b) schemas are versioned and backward-checked so design-partner traces never silently rot; (c) no real customer PII ever enters the system -- all ledgers are synthetic (invoice #9821 and friends). Research grounding: append-only JSONL event logs are the standard portable trace format for agent observability (human-readable, streamable, replayable offline); idempotency-key ledgers are the standard exactly-once pattern (at-least-once delivery + idempotent consumer, key recorded transactionally with the state change).

## 2. Data inventory

| Dataset / entity | Purpose | Source / owner | Classification | Retention / deletion |
|---|---|---|---|---|
| `scenario.yaml` | Declares task, tools, fault schedule, invariants for one chaos experiment | Authored by developer, owned by CTO | Internal, non-sensitive | Git history forever; deprecated scenarios moved to `scenarios/archive/` |
| `policy.yaml` + `policy_patch.json` | Recovery policy (retry/idempotency/confirmation) and diagnoser-proposed diffs | Authored by developer, patched by diagnoser, approved by human | Internal, non-sensitive | Git history forever; every applied patch tagged with source `run_id` |
| Trace JSONL (`traces/<run_id>.jsonl`) | Append-only evidence of one run: every tool call, result, injector event, policy decision | Written by tracer, owned by QA | Internal, synthetic-only (no PII by construction) | Local 90 days; golden traces curated into `traces/golden/` permanently |
| Environment state snapshot (`reports/<run_id>.state.json`) | Final ledger state the verifiers judged | Written by runner, owned by QA | Internal, synthetic-only | Same as parent trace |
| Verdict report (`reports/<run_id>.verdict.json`) | Machine-readable pass/fail per invariant | Written by `check`, owned by QA | Internal | Same as parent trace |

## 3. Scenario schema (`scenario.yaml`, version `scenario/v1`)

| Field | Type | Required? | Constraints / meaning |
|---|---|---|---|
| `schema_version` | string | Yes | Must equal `"scenario/v1"`; unknown versions are rejected (fail-closed) |
| `scenario` | string | Yes | Snake-case id, e.g. `payment_gateway_timeout`; matches `scenario` in every trace event |
| `task` | string | Yes | Natural-language goal given to the agent, e.g. `"Pay vendor $150 for invoice #9821"` |
| `seed` | integer | Yes | Base RNG seed (default 42); recorded in `run_started` |
| `tools` | list of tool specs | Yes | Each: `name`, `latency_ms` (virtual), `timeout_ms`, `mutating` (bool), `idempotency_key_arg` (string or null) |
| `faults` | list of fault specs | Yes | Each: `at_step`, `kind` (`timeout_after_commit`, `timeout_before_commit`, `poisoned_doc`, `concurrent_update`), `params` |
| `invariants` | list of invariant specs | Yes | Each: `id`, `check` (predicate expression), `description` |
| `policy` | string | Yes | Path to the policy file this scenario runs under, e.g. `policies/default.yaml` |
| `max_steps` | integer | No | Default 12; run aborts with `E_RUNAWAY` beyond it (see OQ-05-1) |

Full example (v0 prototype slice):

```yaml
schema_version: "scenario/v1"
scenario: payment_gateway_timeout
task: "Pay vendor $150 for invoice #9821"
seed: 42
tools:
  - name: charge_card
    latency_ms: 300
    timeout_ms: 1500
    mutating: true
    idempotency_key_arg: idempotency_key
  - name: get_transaction_status
    latency_ms: 120
    timeout_ms: 1000
    mutating: false
    idempotency_key_arg: null
  - name: read_doc
    latency_ms: 80
    timeout_ms: 1000
    mutating: false
    idempotency_key_arg: null
faults:
  - at_step: 3
    kind: timeout_after_commit
    params:
      tool: charge_card
      simulated_latency_ms: 2000
invariants:
  - id: exactly_once_mutation
    check: "count(calls['charge_card']) == 1"
    description: "The payment tool must commit exactly once."
  - id: task_completion
    check: "environment.invoice_status == 'PAID'"
    description: "The legitimate task still completes after recovery."
policy: "policies/default.yaml"
max_steps: 12
```

## 4. Policy file schema (`policy/v1`) and patch format

| Field | Type | Required? | Constraints / meaning |
|---|---|---|---|
| `schema_version` | string | Yes | `"policy/v1"` |
| `rules` | list of rule objects | Yes | Each rule: `rule` (name), `tool` (or `*`), `action`, plus rule-specific params |
| `rules[].rule` | string | Yes | One of `on_tool_timeout`, `on_mutating_retry`, `on_untrusted_content`, `on_stale_read` (last two reserved for P2 scenarios) |
| `rules[].action` | string | Yes | One of `fail`, `retry_same`, `check_then_act`, `require_confirmation` |
| `retry.max_attempts` | integer | For retry rules | 1..5; v0 default 2 |
| `retry.require_idempotency_key` | bool | For retry rules | v0 fixed `true`; retries without a key are rejected by the runner |
| `confirmation.message` | string | For confirmation rules | Prompt shown; v0 auto-denies in non-interactive runs |

Full example `policies/default.yaml` (v0 naive policy -- expected to FAIL the timeout scenario):

```yaml
schema_version: "policy/v1"
rules:
  - rule: on_tool_timeout
    tool: "charge_card"
    action: retry_same
    retry:
      max_attempts: 2
      require_idempotency_key: false
```

Diagnoser output `policy_patch.json` (JSON Merge Patch, RFC 7396 style; the ONLY format `replay --policy` accepts):

```json
{
  "schema_version": "policy_patch/v1",
  "source_run_id": "a3f9c1d2e4b50607",
  "rationale": "Timeout outcome is ambiguous; blind retry caused a duplicate charge. Force status check first and require idempotency keys on retry.",
  "patch": {
    "rules": [
      {
        "rule": "on_tool_timeout",
        "tool": "charge_card",
        "action": "check_then_act",
        "check": {"tool": "get_transaction_status", "by": "idempotency_key"},
        "retry": {"max_attempts": 2, "require_idempotency_key": true}
      }
    ]
  }
}
```

Patched `policies/fixed-timeout.yaml` (what replay actually runs):

```yaml
schema_version: "policy/v1"
rules:
  - rule: on_tool_timeout
    tool: "charge_card"
    action: check_then_act
    check: {tool: get_transaction_status, by: idempotency_key}
    retry: {max_attempts: 2, require_idempotency_key: true}
```

## 5. JSONL trace event schema (`trace/v1`)

One JSON object per line, UTF-8, sorted keys, `\n` terminated. Every event carries the full envelope; scenario-specific detail lives in `detail`.

| Field | Type | Required? | Constraints / meaning |
|---|---|---|---|
| `event_id` | string | Yes | `evt_<run_id>_<zero-padded seq>`, e.g. `evt_a3f9c1d2_0007`; strictly increasing |
| `prev_hash` | string | Yes | SHA-256 of the previous line (`"GENESIS"` for the first); tamper-evidence chain |
| `ts_virtual` | integer | Yes | Virtual-clock ms since run start; monotonic non-decreasing; ONLY time verifiers may read |
| `run_id` | string | Yes | `sha256(scenario_file + policy_file + seed)[:16]`; reruns collide by design |
| `scenario` | string | Yes | Must match the `scenario.yaml` id |
| `step` | integer | Yes | Agent-loop step number, 1-based; injector/summary events use the step they belong to |
| `tool` | string or null | Yes | Tool name for call/result events; null for run envelope and injector events |
| `args` | object | Yes | Tool arguments (empty `{}` when n/a); MUST include `idempotency_key` for mutating retries under fixed policy |
| `outcome` | string | Yes | One of `success`, `timeout`, `error`, `blocked`, `info` |
| `latency` | integer | Yes | Virtual ms consumed by this event |
| `injector_event` | object or null | Yes (null when n/a) | `{fault, at_step, params}` when the injector acted at this step |
| `policy_decision` | object or null | Yes (null when n/a) | `{rule, action, detail}` recording which policy rule fired |
| `detail` | object | No | Response payloads, error strings, ledger hashes; verifiers read `detail.ledger_hash` and tool payloads |

Full JSON examples (abridged run, seed 42):

```json
{"args": {}, "detail": {"policy": "policies/default.yaml", "seed": 42, "task": "Pay vendor $150 for invoice #9821"}, "event_id": "evt_a3f9c1d2_0001", "injector_event": null, "latency": 0, "outcome": "info", "policy_decision": null, "prev_hash": "GENESIS", "run_id": "a3f9c1d2e4b50607", "scenario": "payment_gateway_timeout", "step": 0, "tool": null, "ts_virtual": 0}
{"args": {"amount": 150.0, "idempotency_key": null, "invoice_id": 9821}, "detail": {"attempt": 1}, "event_id": "evt_a3f9c1d2_0003", "injector_event": {"at_step": 3, "fault": "timeout_after_commit", "params": {"simulated_latency_ms": 2000}}, "latency": 2000, "outcome": "timeout", "policy_decision": {"action": "retry_same", "detail": "blind retry, no idempotency key", "rule": "on_tool_timeout"}, "prev_hash": "9be27f...", "run_id": "a3f9c1d2e4b50607", "scenario": "payment_gateway_timeout", "step": 3, "tool": "charge_card", "ts_virtual": 2300}
{"args": {"amount": 150.0, "idempotency_key": null, "invoice_id": 9821}, "detail": {"charge_id": "ch_002", "ledger_charges": 2}, "event_id": "evt_a3f9c1d2_0004", "injector_event": null, "latency": 300, "outcome": "success", "policy_decision": null, "prev_hash": "c41aa0...", "run_id": "a3f9c1d2e4b50607", "scenario": "payment_gateway_timeout", "step": 4, "tool": "charge_card", "ts_virtual": 2600}
{"args": {}, "detail": {"charges": 2, "invoice_status": "PAID", "ledger_hash": "sha256:77f1..."}, "event_id": "evt_a3f9c1d2_0005", "injector_event": null, "latency": 0, "outcome": "info", "policy_decision": null, "prev_hash": "d208b3...", "run_id": "a3f9c1d2e4b50607", "scenario": "payment_gateway_timeout", "step": 4, "tool": null, "ts_virtual": 2600}
```

A FIXED-policy replay of the same seed shows the recovery shape: step 3 `timeout` is followed by `get_transaction_status` (result: `committed`, charge `ch_001` found), no second `charge_card` call, final `charges: 1`.

## 6. Environment state model (invoice ledger example)

The synthetic environment is an in-memory ledger snapshotted to `reports/<run_id>.state.json` at run end. Verifiers read the snapshot, never live objects.

| Entity | Fields | Rules |
|---|---|---|
| `invoice` | `id` (int), `vendor` (string), `amount` (number), `status` (`UNPAID`/`PAID`/`REFUNDED`) | Status flips to `PAID` only via a committed `charge_card` |
| `charge` | `charge_id` (string), `invoice_id` (int), `amount` (number), `idempotency_key` (string or null) | Duplicate `(invoice_id, idempotency_key)` pairs are collapsed by the ledger; retries WITHOUT a key create duplicate rows (this is the bug v0 demonstrates) |
| `doc` | `doc_id`, `body`, `untrusted` (bool) | Reserved for the P2 poisoned-doc scenario; untrusted bodies are wrapped in `<untrusted_content>` delimiters per prompt-injection defense research |
| `record_version` | `record`, `version` (int), `last_writer` | Reserved for the P2 stale-read scenario; writers check versions, reject stale updates |

Full example `reports/a3f9c1d2.state.json` (failing run -- two charges):

```json
{
  "schema_version": "env_state/v1",
  "run_id": "a3f9c1d2e4b50607",
  "invoices": [{"id": 9821, "vendor": "Acme Paper Co", "amount": 150.0, "status": "PAID"}],
  "charges": [
    {"charge_id": "ch_001", "invoice_id": 9821, "amount": 150.0, "idempotency_key": null},
    {"charge_id": "ch_002", "invoice_id": 9821, "amount": 150.0, "idempotency_key": null}
  ],
  "docs": [],
  "record_versions": [],
  "ledger_hash": "sha256:77f1c4a9..."
}
```

## 7. Retention, versioning, and compatibility

| Rule | Detail |
|---|---|
| Schema versions | `scenario/v1`, `policy/v1`, `policy_patch/v1`, `trace/v1`, `env_state/v1`; bumped as `v2` on any breaking field change, with a migration note in `15-roadmap-and-changelog.md` |
| Strict reading | Unknown `schema_version` => reject with `E_SCHEMA` (fail-closed); unknown EXTRA fields => warn but accept (forward-tolerant) |
| Golden traces | Curated passing AND failing traces live in `traces/golden/` permanently; CI replays them on every policy change |
| Local traces | `traces/*.jsonl` + `reports/*` kept 90 days locally, then deletable; nothing leaves the laptop in P1 |
| No PII | Synthetic vendors/amounts only; `check --scan-pii` (regex for emails, cards) runs in CI as a guardrail |
| Backup | Git is the backup for scenarios/policies/golden traces; ephemeral traces are reproducible via seed and need none |

## 8. Validation rules (enforced by `check --strict-schema`)

1. Every line parses as JSON; envelope fields all present with correct types.
2. `event_id` sequence is gapless; `ts_virtual` never decreases; `prev_hash` chain verifies.
3. `scenario` matches the invoked `scenario.yaml`; `run_id` recomputes correctly.
4. Mutating tool retries carry non-null `idempotency_key` when policy requires it.
5. Final `ledger_hash` in trace equals the hash of `reports/<run_id>.state.json`.

## 9. Open questions (genuinely undecided -- owner assigned)

| ID | Question | Owner | Resolves by gate |
|---|---|---|---|
| OQ-06-1 | Cap trace payload size per event (proposal: 64 KB, truncate `detail` with marker) | Startup CTO | P1 scope freeze |
| OQ-06-2 | Canonical predicate language for `invariants[].check` (proposal: restricted Python via `asteval` vs Rego) | ML lead (to hire) | P2 planning |
| OQ-06-3 | Whether golden traces include full LLM prompts (bigger files, better diagnoser context) | Startup CTO | P1 scope freeze |
