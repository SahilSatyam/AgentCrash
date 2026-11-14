# 12 — Non-Functional Requirements (NFRs)

**Status:** Current for `agentcrash-core` 0.3.0 · **Owner:** Founder (scope sign-off) · **Milestone scheme:** M0–M3 (ADR-006)

> ADLC lens: these targets keep a laptop-run, zero-infrastructure library honest. Every NFR
> names a metric, a target, the workload it holds under and a verification command or test.
> An NFR with no way to verify it is not listed. "Gap" marks a target that is enforced by
> hand or not yet by an automated test; the gap and its roadmap item are in §8. Traceability
> to functional requirements in [03](03-requirements-and-srs.md) is explicit. No day-wise
> timeline is used; NFRs gate milestones.

## 1. Determinism and reproducibility

| ID | Metric | Target | Workload / conditions | Verification | Priority |
|---|---|---|---|---|---|
| NFR-DET-01 | Deterministic modules read no wall-clock or global RNG | `clock.py`, `tracer.py`, `ledger.py`, `injector.py`, `verifiers.py` import none of `time`, `datetime`, `random` | Source tree | `pytest tests/test_acr001.py` (AST check; also its own CI step). Covers FR-51 | P0 |
| NFR-DET-02 | Verdict report stability | Two `check` runs on the same trace, invariants and state write byte-identical report files (sorted keys, no timestamps) | Any SDK-schema trace | Run `check --report-out a.json` twice and compare file hashes (checked by hand on 0.3.0; automated test is a gap). Covers FR-31, FR-36 | P0 |
| NFR-DET-03 | Trace evidence is order-stable | `TraceWriter` output has sorted keys, gapless `event_id`, monotonic `ts_virtual`; `verify_chain` returns `ok` on any untouched file | 200-event writer run | `tests/test_tracer.py`, `tests/test_generic_core.py::test_tracer_never_redacts_its_own_hash_fields`. Covers FR-20, FR-21 | P0 |
| NFR-DET-04 | Virtual-clock purity | Synthetic time advances only through `VirtualClock.sleep`; negative inputs refused | `clock.py` | `tests/test_clock.py`. Covers FR-24 | P1 |

Principle kept from the original design: one seeded source of entropy and one virtual clock,
so a seed reproduces a fault schedule. SDK `run_id` values are random (`uuid4`) by design;
byte-equality claims apply to verdict reports and `TraceWriter` output, not to SDK run ids.

## 2. Performance budgets

Method (`bench/run_overhead.py`, result in `docs/phase7-evidence/bench.json`): a bare 12-step
loop and the same loop instrumented through the real SDK path (`run`, `step`, `maybe_fault`,
`record_tool`, so three hash-chained, redacted events per step) are run 30 times,
interleaved so machine drift hits both equally, after one warm-up each. Each stub step does
2 ms of deterministic busy work standing in for a tool or model call. That is a harsh floor:
networked tool calls typically take 10 ms or more and model calls 100 ms or more, so real
overhead is smaller. The script compares medians and exits non-zero on a budget miss.
Absolute per-event costs do not depend on the 2 ms assumption.

| ID | Metric | Target | Measured (`bench.json`) | Workload / conditions | Verification | Priority |
|---|---|---|---|---|---|---|
| NFR-PERF-01 | SDK overhead vs bare loop, relative | Median instrumented run ≤ 10% slower than median bare run | 5.7% (`overhead_ratio` 0.057; 25.402 ms vs 24.031 ms) | 12 steps, 2 ms stub step, 30 interleaved runs, medians | `python bench\run_overhead.py` (`overhead_pass`). Covers FR-01, FR-19 | P0 |
| NFR-PERF-02 | Trace size | A 100-step trace ≤ 2 MiB (2,097,152 bytes) | 37,797 bytes (`hundred_step_bytes`), about 380 bytes per event | 100 `TraceWriter` events with a small `args`/`detail` payload | `python bench\run_overhead.py` (`trace_size_pass`). Covers FR-20 | P0 |
| NFR-PERF-03 | Tracer cost per event | `TraceWriter` ≤ 60 µs per event, flushed per line | 30.7 µs | 2000 events, best of 5 | `python bench\run_overhead.py` (`tracer_pass`) | P0 |
| NFR-PERF-04 | SDK cost per event (absolute) | Reported, not gated | 14.0 µs per event; 114.2 µs added per step (`overhead_us_per_step`) | 2000 `record_tool` events, best of 5 | `bench.json` | P1 |

Rationale: the harness must be near-free next to tool and model time. Model latency is
excluded from every budget; only harness time counts. Removed in 0.3.0: the old
`check` latency and `replay` cost budgets, which measured commands that no longer exist.

## 3. Reliability (correctness of the harness itself)

| ID | Metric | Target | Workload / conditions | Verification | Priority |
|---|---|---|---|---|---|
| NFR-REL-01 | SDK never breaks the host | No exception from capture or export reaches host code; non-JSON values are stringified | Dead endpoint; datetime and custom-object outputs | `tests/test_sdk.py::test_dead_endpoint_never_breaks_host`, `tests/test_generic_core.py::test_sdk_accepts_non_json_tool_outputs`. Covers FR-06, FR-07 | P0 |
| NFR-REL-02 | Bounded memory | Export queue never exceeds `max_queue`; overflow drops oldest and is counted | `max_queue=4`, 10 steps | `tests/test_sdk.py::test_bounded_queue_drops_oldest`. Covers FR-05 | P0 |
| NFR-REL-03 | Chain integrity under concurrency | `seq` is gapless and every `prev_hash` recomputes with 8 threads | 400 tool events | `tests/test_generic_core.py::test_sdk_chain_is_consistent_under_threads`. Covers FR-19 | P0 |
| NFR-REL-04 | Idempotent ingestion | Re-posting a batch stores nothing new | Same batch twice | `tests/test_server_ingestion.py::test_idempotent_ingestion_and_run_detail`. Covers FR-38 | P0 |
| NFR-REL-05 | Fail closed | A corrupt trace exits 3, a usage or config error exits 2, and neither is ever reported as PASS; a verifier returning a bad verdict object aborts instead of passing | `check` with a non-JSON trace, no `--invariant`, a missing file | Exit codes confirmed by hand on 0.3.0; automated tests are a gap. Covers FR-29, FR-33, FR-34 | P0 |
| NFR-REL-06 | Patch gate | An invalid `policy/v1` or `policy_patch/v1` document raises before anything is applied | Unknown rule name; patch missing `source_run_id` | Confirmed by hand on 0.3.0; automated tests are a gap (roadmap R6). Covers FR-46 to FR-48 | P0 |

## 4. Portability and compatibility

| ID | Metric | Target | Workload / conditions | Verification | Priority |
|---|---|---|---|---|---|
| NFR-PORT-01 | Python support | 3.11, 3.12, 3.13 with no code changes; `requires-python = ">=3.11"` | CI matrix | `.github/workflows/ci.yml` runs ruff, black `--check`, `pytest -q` and the ACR001 test on each version | P0 |
| NFR-PORT-02 | Dependency footprint | 2 runtime dependencies (`pyyaml`, `jsonschema`); server stack only behind the `server` extra | Fresh `pip install agentcrash-core` | `pyproject.toml` review; `tests/test_second_app.py` pins a second app to stdlib plus `agentcrash` | P0 |
| NFR-PORT-03 | Offline operation | The full test suite needs no network, model or API key | `pytest -q` with no credentials set | 56 offline tests pass in CI | P0 |
| NFR-PORT-04 | OS support | CI runs on Linux (ubuntu-latest). Windows 11 is the maintainer's development platform and is exercised manually; macOS is untested | `pytest -q` | CI log (Linux); manual run (Windows). Windows with Smart App Control: use `python -m agentcrash` | P1 |

## 5. Observability (every verdict explainable)

| ID | Metric | Target | Workload / conditions | Verification | Priority |
|---|---|---|---|---|---|
| NFR-OBS-01 | Verdict evidence | Every verdict row has a non-empty `evidence` string; a failing duplicate-effect verdict names the tool and both steps | `duplicate_mutating_effect`, `fresh_write_only` | `tests/test_generic_core.py::test_check_cli_knows_builtin_invariants`, `::test_fresh_write_only_accepts_user_named_store_tools`. Covers FR-26, FR-27 | P0 |
| NFR-OBS-02 | Report completeness | Report JSON always carries `api_version`, `run_id`, `verdict`, `results[]` | Any `check` run | Written by `cli.cmd_check`; `tests/test_generic_core.py::test_check_cli_knows_builtin_invariants` exercises the file write. Covers FR-36 | P0 |
| NFR-OBS-03 | Machine-readable errors | With `--format json`, a failure prints `{"api_version", "code", "exit", "hint"}` | Missing trace, no invariants | Confirmed by hand on 0.3.0; automated test is a gap. Covers FR-33 | P1 |

## 6. Usability

| ID | Metric | Target | Workload / conditions | Verification | Priority |
|---|---|---|---|---|---|
| NFR-USE-01 | CLI self-description | `check --help` and `serve --help` list every flag; an unknown command exits 2 | `check`, `serve` | `tests/test_cli_help.py`. Covers FR-37 | P0 |
| NFR-USE-02 | Error actionability | Config errors name the bad value and the fix (`no invariants: pass at least one --invariant <id>`, `unknown invariant 'x'; known verifiers: ...`) | `check` misuse | Message review plus `UnknownVerifierError` text in `_registry.py` | P1 |

## 7. Security and maintainability

| ID | Metric | Target | Workload / conditions | Verification | Priority |
|---|---|---|---|---|---|
| NFR-SEC-01 | No secrets or PII in captured artifacts | Keys matching `*_token`, `*secret*`, `*key*` (except `idempotency_key`) are `[REDACTED]`; emails and card-like runs are replaced with fixed constants | Planted `api_key` input; planted email | `tests/test_sdk.py::test_deterministic_fault_and_redaction`, `tests/test_tracer.py::test_redaction_strips_secrets_but_keeps_idempotency_key`. `check --scan-pii` is the CI guard for files (test is a gap). Covers FR-15 to FR-18 | P0 |
| NFR-SEC-02 | Control-plane auth | With `AGENTCRASH_API_TOKENS` set, every `/v1/*` call without a listed bearer token gets 401; Lab assets stay public | Stdlib server | `tests/test_server_ingestion.py::test_auth_allowlist_gates_api_but_not_lab`. Covers FR-40 | P0 |
| NFR-SEC-03 | Local by default | `agentcrash serve` binds `127.0.0.1`. Any other bind is an explicit `--bind` | `serve --help` | `cli.cmd_serve` default; `tests/test_cli_help.py` checks the flag is documented. Covers FR-44 | P0 |
| NFR-SEC-04 | Model is never a trust boundary | A model can only propose a patch; verdicts come from deterministic invariants; live proposals are opt-in and schema-gated | Env var unset; invalid model output | `tests/test_generic_core.py::test_live_patch_is_opt_in`. Covers FR-50 | P0 |
| NFR-MNT-01 | Domain-free core | No domain vocabulary in `src/`, no baked-in default tool target in the fault catalog | `src/agentcrash` | `tests/test_generic_core.py::test_core_source_has_no_domain_vocabulary`, `::test_fault_catalog_targets_no_default_tool`. Covers FR-52 | P0 |
| NFR-MNT-02 | Code hygiene | ruff clean, black clean (line length 100) | Whole repo | CI steps `ruff check .` and `black --check .` | P1 |
| NFR-MNT-03 | New invariants are additive | A host adds an invariant by registering a `Verifier`; no core file changes | Plugin loaded with `check --plugin` | `register_verifier` API; `tests/test_second_app.py` shows a second app integrating with no core edits | P1 |

Security note: the hash chain is an integrity signal against accidental loss or reordering.
It is not tamper resistance, and the control-plane store is in memory with no persistence.

## 8. Prioritization, gaps and cost ceiling

Gating set for a 0.3.0 release (R7): NFR-DET-01…03, NFR-PERF-01…03, NFR-REL-01…04,
NFR-PORT-01…03, NFR-OBS-01/02, NFR-USE-01, NFR-SEC-01…04, NFR-MNT-01.

Known gaps (enforced by hand or not yet by a test): NFR-DET-02 report byte-equality,
NFR-REL-05 exit-code contract, NFR-REL-06 patch gate, NFR-OBS-03 JSON errors, and the PII scan
in NFR-SEC-01. Each maps to adding tests under roadmap R3 (invariants and `check`) or R6
(patch validation). Not listed because they cannot be verified today: OS coverage beyond
Linux CI and Windows manual runs, and any latency budget for commands that do not exist.

Cost ceiling: $0 infrastructure. The library and its tests run on a laptop and a free CI tier.
Live patch proposal is the only paid path and it is opt-in. Owner approving exceptions: Founder.

## 9. Worked verification plan

Commands run from the repo root in PowerShell with the project venv. Expected values are
exact; any deviation fails the gate.

1. Determinism drill (DET-01…03): `.\.venv\Scripts\python -m pytest tests/test_acr001.py tests/test_tracer.py -q` passes.
   Run `check --report-out a.json` and `--report-out b.json` on the same trace; `Get-FileHash a.json, b.json` shows equal hashes (DET-02).
2. Budget drill (PERF-01…04): `.\.venv\Scripts\python bench\run_overhead.py` prints and writes `docs/phase7-evidence/bench.json`
   (pass `--out <path>` to keep the committed file) and exits 0. Current committed values: `overhead_ratio` 0.057 against the 0.10 target
   (`overhead_pass` true), `overhead_us_per_step` 114.2, `sdk_us_per_event` 14.0, `tracer_us_per_event` 30.7 against 60.0
   (`tracer_pass` true), `hundred_step_bytes` 37,797 against 2,097,152 (`trace_size_pass` true). A run on other hardware may differ; the
   pass flags are the gate, the numbers are the evidence. Medians over 30 interleaved runs absorb most scheduler noise, but rerun once
   before filing a regression.
3. Harness-correctness drill (REL-01…04): `.\.venv\Scripts\python -m pytest tests/test_sdk.py tests/test_generic_core.py tests/test_server_ingestion.py -q` passes.
   Fail-closed spot check (REL-05): `agentcrash check --trace <non-JSON file> --invariant duplicate_mutating_effect` exits 3; `agentcrash check --trace x.jsonl` exits 2.
4. Portability drill (PORT-01…03): CI matrix (Python 3.11, 3.12, 3.13) runs ruff, black, `pytest -q` (56 passed) and the ACR001 test.
   `pyproject.toml` lists two runtime dependencies.
5. Observability and usability drill (OBS-01/02, USE-01): `.\.venv\Scripts\python -m pytest tests/test_cli_help.py -q` passes; a failing
   `check` run prints `[FAIL] Invariant <id> violated! (<evidence>)` and writes a report with `verdict: FAIL`.
6. Security drill (SEC-01…04, MNT-01): `.\.venv\Scripts\python -m pytest -q` passes (56 tests), covering redaction, auth allowlist,
   opt-in live patch and the domain-free guard.

Exit rule: all six drills green means the milestone gate passes. Any red drill keeps it open;
the founder records the exception and the compensating milestone in the exit note. No silent
waivers.

## 9a. Sizing note: why the trace budget holds

The committed 100-step trace is 37,797 bytes, about 380 bytes per event (`TraceWriter`, small
payloads). Real payloads are larger because tool outputs carry content. The 2 MiB budget for 100 events
allows about 20 KB per event, which is 50 times the measured size.

| Run length | Bytes at 380 B/event (measured) |
|---|---|
| 100 events | 38 KB |
| 1,000 events | 380 KB |
| 5,000 events | 1.9 MB |

`detail` is capped at 65,536 bytes per event by `TraceWriter`, so one event can never exceed
64 KiB plus envelope. If tool outputs routinely exceed a few KB, this table is revised before
the 2 MiB target is.

## 10. Change history

| Milestone | Change | Author |
|---|---|---|
| Pre-M0 draft | NFR skeleton with placeholder targets | Pre-M0 skeleton (author unrecorded) |
| M0 draft | Full NFR set for the scenario-runner prototype | Group B |
| 0.3.0 rewrite | PERF-01/02 reworded to the interleaved-median SDK method and numbers in `bench.json`; tracer and SDK per-event budgets added; unverifiable NFRs (golden suite, replay cost, demo time, model-backed items) removed; verification drills rewritten to commands that exist | Docs rewrite |
