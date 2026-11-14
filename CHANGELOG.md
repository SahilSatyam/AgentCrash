# Changelog

All notable changes to `agentcrash-core`. Versions follow [SemVer](https://semver.org/);
while the version is `0.x`, minor releases may break the API.

## 0.3.0 — unreleased

Chaos engineering for any agentic workflow: the core no longer ships domain code.

### Breaking changes and how to migrate

| Removed | Why | Do this instead |
|---|---|---|
| `agentcrash.db` | Its tables were shaped for one demo app (`nl_text`, `odrl_docs`) and nothing in core used it | Use the control plane (`agentcrash serve`, `/v1/runs`, `/v1/events`) or persist `ac.trace(run_id)` yourself |
| `agentcrash.policy.reference_patch_for` and `reference_patch_s4_*` | Hard-coded fixes for one demo's scenarios | Build a `policy_patch/v1` dict for your workflow and apply it with `policy.apply_patch`; or opt in to model-proposed patches via `fetch_live_patch` |
| `agentcrash.verifiers.SAVE_ODRL` | Demo tool name | `FreshWriteOnly(tools=["your_store_tool"])` |
| `host-odrl` extra (`pyshacl`, `dspy`, `numpy`) | Domain stacks do not belong in core | Depend on them directly in your own package |
| Fault `default_params` naming a tool | Silently targeted a demo tool | Name the tool yourself: `{"tool": "payments.charge", ...}`; `concurrent_update` now requires `key` and `updates` |
| Exporter de-duplication by `event_id` | Unbounded memory in long-running hosts | Nothing to do: the server de-duplicates on ingestion |

`LLAMA_BASE_URL` / `LLAMA_API_KEY` keep working; the preferred names are
`AGENTCRASH_LLM_BASE_URL` / `AGENTCRASH_LLM_API_KEY`.

### Added

- `.github/workflows/release.yml`: on a `v*` tag, run the gates (py3.11-3.13), check the
  tag matches the package version, build sdist + wheel, `twine check --strict`,
  smoke-install the wheel, and publish to PyPI via trusted publishing (OIDC, no token).
- `verify_sdk_chain(events)` and `agentcrash check --verify-chain` (SDK runs and
  `TraceWriter` files; a broken chain exits 3).
- `check --invariant fresh_write_only:<tool>[,<tool>]` names your store tools on the CLI.
- `agentcrash.verifiers.tool_results(trace)`: one normalized view of tool calls from
  either trace format, for writing your own invariants.
- `tests/test_gaps_2.py`: workflow error path, `enabled=False`, event validation and
  invalid ingestion, `FaultSchedule` refusal and matching, `fault_injection=False`,
  `run_verifiers` error paths, runtime verifier purity, FastAPI bearer auth, Lab
  traversal, `load_policy`, `diff_policies`, live-patch transport and output gating.
  Every FR in doc 03 now cites a test. Dev extra adds `httpx2` (FastAPI test client).
- `tests/test_gaps.py`: `--scan-pii`, card redaction, `NoLostUpdate`, `check` exit codes
  2 and 3, patch rejection, report byte-stability.
- `agentcrash check` registers the built-in invariants (`duplicate_mutating_effect`,
  `fresh_write_only`, `no_lost_update`, `required_step:<name>`), so it works without plugins.
- `FreshWriteOnly(tools=...)` for your own versioned-store tools.
- MIT `LICENSE`; PyPI classifiers, keywords and project URLs.
- `tests/test_generic_core.py`: fails if domain vocabulary returns to `src/`.

### Fixed

- `fresh_write_only` and `no_lost_update` only understood `TraceWriter` envelopes, so on
  SDK traces they passed with nothing to check. Both now read SDK events too
  (`kind: tool` / `kind: fault`); `fresh_write_only` says `nothing to judge (vacuous)`
  when no write matched, and `no_lost_update` raises (exit 3) instead of passing when a
  rival writer exists but `--state` has no `record_versions`.
- SDK `seq` and the hash chain were per `AgentCrash` instance, so one run's trace could not
  be verified alone. Both are now per run (`seq` from 1, chain from `GENESIS`).
  Behavior change: `seq` restarts at 1 in every run.
- `maybe_fault` always checked call 1, so `occurrence: 2+` never fired. It now counts
  calls per boundary per run (`occurrence: N` fires on exactly the Nth call); pass
  `call_index` to override. Behavior change: an `occurrence: 1` fault no longer fires
  on every call, only the first.
- Stdlib server lacked `GET /v1/runs/{id}/evaluation`; FastAPI app lacked
  `GET /v1/reliability/{project}` and returned a different `GET /v1/runs` row shape.
  Both servers now expose the same routes and rows.

- Tracer redacted its own `prev_hash`/`event_id` fields; a hash with 13+ digits in a row
  could be rewritten as a "card number" and break the chain.
- SDK hash chain could fork under concurrent steps (`prev_hash` updated outside the lock).
- `record_tool` raised `TypeError` into host code for non-JSON outputs (objects, dates).
- Server reset the connection on 401 before reading the body (Windows; flaky auth test).
- Bench crashed on Windows (unclosed trace files).

### Performance

- Tracer: ~75 → ~31 µs per event (compiled key matcher, cached key checks, cheap
  pre-checks before the PII regexes, one serialization per event).
- SDK: ~14 µs per event (`deque` exporter, prebuilt encoder, no redundant
  per-event validation).
- `bench/run_overhead.py` now measures NFR-PERF-01 as written (per-run overhead vs a bare
  12-step agent loop): 5.7% at a 2 ms stub step, target ≤ 10%.

## 0.2.0

First installable split of the SDK, control plane and `check` CLI.
