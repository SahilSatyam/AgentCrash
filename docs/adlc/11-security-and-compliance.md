# 11 - Security and compliance notes

**Status:** Revised for 0.3.0 · **Owner:** Tech lead · **Last reviewed:** 2026-11-14

> Project-specific security requirements for AgentCrash. A working note, not a
> compliance certification. Protect keys, keep the blast radius to the host's
> own test environment, and write down just enough to earn trust.

## 1. Assets and trust boundaries

| Asset | Why it matters | Trust boundary |
|---|---|---|
| Model API key (only if `fetch_live_patch` is used) | Billed identity; a leak spends money | Process environment (`AGENTCRASH_LLM_API_KEY`) only; never repo, flag, or trace |
| Control-plane bearer tokens | Gate `/v1/*` | `AGENTCRASH_API_TOKENS` env var on the server; `api_key` in the host's `WorkflowConfig` |
| Traces and reports | May embed tool inputs, outputs, PII-shaped values | Redacted at write time; local files are gitignored (`reports/`, `traces/*`) |
| Policy files and patches | Control the host's recovery behavior | Schema-gated (`policy/v1`, `policy_patch/v1`); a model-proposed patch is never auto-applied |
| Control-plane store | Holds ingested events and evaluations | Loopback by default; in-process data, pruned with `/v1/admin/prune` |
| Source | Project IP | Standard repo access control |

Roles: contributor (code and docs), reviewer (approves schema, redaction and
guard-test changes), operator (runs `serve`, holds tokens and any model key).
There are no end-user accounts and no admin console. The only privileged
operations are `/v1/admin/prune` and the token allowlist.

## 2. Threat model

| Threat | Impact | Mitigation and detection | Status |
|---|---|---|---|
| Secret exfiltration through a trace | A key or token lands in a shareable file | Write-time redaction by key name (`*_token`, `*secret*`, `*key*`); tests assert redaction; `check --scan-pii` scans raw files | Mitigated; name-based, so a secret under an innocuous key name is not caught (host must name keys sensibly) |
| PII in traces | Privacy exposure on share | Write-time redaction of email shapes and card-like digit runs; `--scan-pii` gate exits 1 on a hit | Mitigated for those two shapes only; other PII (names, addresses) is not detected |
| Unauthenticated control-plane access | Read or write of events, pruning | Binds to `127.0.0.1` by default; `AGENTCRASH_API_TOKENS` bearer allowlist returns 401 otherwise | Mitigated; see section 4 for non-loopback use |
| Trace tampering | Verdict truth silently changed | `prev_hash` chain and `verify_chain` give an integrity signal | Open by design: the chain is NOT tamper-proof (an attacker can recompute it) |
| Duplicate side effects after an ambiguous timeout | Duplicate payment or update in the tested host | `duplicate_mutating_effect`; `timeout_after_commit` fault to reproduce it | This is the failure AgentCrash exists to detect |
| Stale read leading to a lost update | Silent data corruption pattern | `FreshWriteOnly`, `NoLostUpdate`; `concurrent_update` fault | Detected by verifiers when the host records versions |
| Poisoned document drives an agent action | Unauthorized side effect in the tested host | `poisoned_doc` fault injects an inert marker only; the host's own defenses are what is under test | Inert by design: no adversarial text ships in core |
| Model-proposed patch steers policy | Attacker-crafted trace influences a patch | Opt-in transport that fails closed; patch must validate against `policy_patch/v1`; human review before applying | Mitigated; patches are proposals, never verdicts |
| Malicious dependency | Supply-chain compromise | Two runtime deps (`pyyaml`, `jsonschema`); server extras (`fastapi`, `uvicorn`) are optional | Open: no lockfile-audit job in CI yet (roadmap R7) |
| Malicious plugin passed to `--plugin` | Arbitrary code runs in `check` | `--plugin` imports a dotted module you name; only pass code you trust | Accepted; documented |
| Corrupt or hostile trace file | Crash or false pass | Malformed traces exit 3 and never pass; `verify_chain` names the first bad link | Mitigated |

## 3. Secrets handling

- Secrets come from the environment only. The variables the package reads
  are `AGENTCRASH_API_TOKENS` (server), `AGENTCRASH_LLM_BASE_URL` and
  `AGENTCRASH_LLM_API_KEY` (opt-in patch proposals), and
  `AGENTCRASH_DISABLED` (a switch, not a secret).
- No key is accepted as a CLI flag. `check` and `serve` have no key options.
- No key, token, or fixture credential is committed. `.gitignore` excludes
  `.env`, `*.pem`, `*.key`.
- Without `AGENTCRASH_LLM_BASE_URL`, `fetch_live_patch` returns `None` and
  makes no network call (asserted in `tests/test_generic_core.py`).
- Tests and CI use no real keys and need none.
- Logs and error messages should name the tool and arg keys, not values.

Incident rule: on a suspected key or PII leak, rotate the credential, delete
the exposed trace or report, and add the lesson to the runbook (doc 14).

## 4. Control plane: loopback default and token auth

- `python -m agentcrash serve` binds `--bind 127.0.0.1` by default
  (`--port` defaults to 8901).
  Keep it on loopback unless a reverse proxy with TLS sits in front.
- If `AGENTCRASH_API_TOKENS` is set (comma-separated), every `/v1/*` request
  needs `Authorization: Bearer <token>` from that list; otherwise it gets
  `401`. The Lab static assets stay public so the page can load. If the
  variable is unset, the API is open: acceptable on loopback only.
- The SDK sends its `api_key` from `WorkflowConfig` as the bearer token. The
  SDK flush is best-effort and never raises into the host.
- Ingestion is idempotent on `event_id`. `/v1/admin/prune` enforces
  retention (`keep_last_n` and/or `older_than_days`).
- If you bind a non-loopback address, you own TLS, network ACLs, and token
  rotation. Binding publicly without tokens is unsupported.

## 5. Tool and fault safety

- Faults are applied only at boundaries the host names in `configure_faults`
  and only when `WorkflowConfig(fault_injection=True)`. Unknown fault kinds
  are refused. The catalog (`timeout_after_commit`, `timeout_before_commit`,
  `concurrent_update`, `poisoned_doc`) has no default tool target, so nothing
  is hit by accident.
- `maybe_fault` returns a signal to the host; the host decides what its tool
  does. AgentCrash does not wrap, execute, or sandbox host tools.
- Run chaos only against test or staging resources. Pointing a fault-injected
  workflow at production payments or real customer records is the operator's
  risk to avoid; core cannot prevent it.
- `ledger.py` is a synthetic store test double and holds no real data.

## 6. Trace redaction (matches `tracer.py`)

Redaction is applied at write time (`redact()` in `tracer.py`; the SDK calls
the same function on `inputs` and `outputs`), so raw values never rest on
disk.

| Rule | Behavior |
|---|---|
| Sensitive key names | A mapping key matching `*_token`, `*secret*` or `*key*` (case-insensitive) has its value replaced with `[REDACTED]` |
| Exempt key | `idempotency_key` is never redacted, because the duplicate-effect check needs it |
| Email shapes | Inside any string, replaced with `redacted@example.com` |
| Card-like digit runs | 13 to 19 digits, optional single space or hyphen separators, replaced with `4111111111111111` |
| Recursion | Applied through nested dicts and lists |
| Harness-owned fields | `event_id`, `prev_hash`, `ts_virtual`, `run_id` and similar fields are not redacted, so the chain stays valid |
| Size cap | `detail` over 64 KB (65,536 bytes) is replaced by a preview with a truncation marker |

Limits to state plainly: redaction is pattern-based. It does not detect
names, addresses, or secrets stored under innocuous key names. Review sample
traces before sharing them.

`python -m agentcrash check --trace run.jsonl --invariant required_step:charge --scan-pii`
re-scans the raw trace (and `--state` file) for emails and card-like runs and
exits 1 on any hit. The redaction placeholders are exempt. `scan_pii.py` uses
the same regexes as `tracer.py`; change them together.

Sharing rule: builder traces with real-ish data stay local. To share a trace,
re-run it with synthetic inputs or confirm `--scan-pii` is clean.

## 7. Supply chain

- Runtime dependencies are `pyyaml` and `jsonschema`. Extra `server` adds
  `fastapi` and `uvicorn`; extra `dev` adds `pytest`, `ruff`, `black`.
- Each new dependency names the capability it buys in the PR. The standard
  stdlib `serve()` path keeps the control plane usable without FastAPI.
- Scenario-like inputs are data: policies and patches are validated against
  bundled JSON schemas and never evaluated as code. The one code-loading path
  is the explicit `--plugin` option (section 2).
- Release hygiene (pinning, audit, signing) is roadmap R7.

## 8. Safety rules

1. Test environments only. No live payments, refunds, or real customer
   records under fault injection.
2. No production credentials in the repo, fixtures, traces, or reports.
3. A model may only propose a schema-valid policy patch; deterministic
   invariants own every verdict (ADR-001, ADR-004).
4. Proposed patches are reviewed by a human and re-checked before they become
   policy.
5. The core never imports host or domain stacks (ADR-009, guard test in doc 09).

## 9. Compliance notes

- Data retention: traces and reports are working data and may be deleted at
  any time. For a running control plane, enforce a policy with
  `/v1/admin/prune` (`keep_last_n`, `older_than_days`).
- Disclosure: demos and posts state that AgentCrash tests agents against
  injected faults and that a PASS describes the checked invariants, not the
  overall safety of an agent or a model.
- Regulatory posture: the package processes only what a host sends it. It
  offers no regulated function. Revisit before any hosted service or any
  customer-data ingestion.
- Reviewer: one accountable reviewer for security notes, reviewed each
  milestone (M0-M3, ADR-006).

## 10. Worked example: redaction and PII gate

A host records a tool call whose inputs contain a customer email, an
`api_key`, and an `idempotency_key`:

1. `record_tool("payments.charge", inputs={"email": "pat@corp.example", "api_key": "sk-123", "idempotency_key": "o-1"}, ...)`.
2. The stored event has `email` equal to `redacted@example.com`, `api_key`
   equal to `[REDACTED]`, and `idempotency_key` still `o-1`.
3. `python -m agentcrash check --trace run.jsonl --invariant duplicate_mutating_effect --scan-pii`
   passes the scan; the placeholders are exempt.
4. If a raw email were present in a hand-written trace file, the same command
   exits 1 and names the file and line.

Review checklist before sharing a trace:

- No live-looking emails, card numbers, names, or keys in the file.
- `--scan-pii` exits 0.
- Verdicts reproduce offline with the network disabled.

## 11. Open Questions

| Question | Owner | Notes |
|---|---|---|
| Secret-scanning hook for commits (pre-commit versus CI) | Tech lead | Decide with R7; traces and reports are already gitignored |
| Configurable redaction key patterns | Tech lead | Proposal: tracer-owned config so redaction cannot be policy-patched away |
| Dependency audit job in CI | Tech lead | Roadmap R7 |
