# 10 - Testing strategy

**Status:** Revised for 0.3.0 · **Owner:** Tech lead · **Last reviewed:** 2026-11-14

> How AgentCrash proves it catches real agent failures without crying wolf.
> Every test earns its runtime: the pyramid favors fast, deterministic,
> offline checks. No test needs a model, a network, or a key.

## 1. Test pyramid

The suite is offline pytest tests under `tests/`. Ruff, Black (line 100)
and the ACR001 determinism lint run beside it in CI.

| Level | What it covers | Real test files | Exit criteria |
|---|---|---|---|
| Unit | Clock, tracer, redaction, hash chain, ledger test double, fault catalog validation | `test_clock.py`, `test_tracer.py`, `test_ledger.py`, `test_skeleton.py` | Zero flakes; deterministic output |
| Component | SDK capture, fault firing, bounded queue, disabled mode, dead-endpoint tolerance, verifiers, evaluation and `compare_reports` | `test_sdk.py`, `test_lab.py`, `test_generic_core.py` | Host is never broken by SDK failure; verdicts reproducible |
| Integration | Control plane ingestion, auth, retention, Lab static assets, CLI `check` end to end | `test_server_ingestion.py`, `test_lab.py`, `test_cli_help.py`, `test_generic_core.py::test_check_cli_knows_builtin_invariants` | Idempotent ingestion; exit codes as documented |
| Boundary / guard | Core stays domain-free; a second host app uses only stdlib plus `agentcrash`; no baked demo bundle; wall-clock and global-random ban | `test_generic_core.py`, `test_second_app.py`, `test_no_static_bundle.py`, `test_acr001.py` | Any hit fails the build |
| Perf | NFR-PERF-01 overhead, tracer cost, trace size | `bench/run_overhead.py` (manual, evidence in `docs/phase7-evidence/bench.json`) | Section 6 numbers |

Manual testing is limited to the demo loop in doc 14 and readability of
`check` output. There is no browser test harness and no staging environment.

Removed in 0.3.0 (ADR-009): scenario tests, golden-trace suites, seed
matrices over a live model, and diagnoser-quality evaluation. They return
only with host-supplied runners (roadmap R6) and belong to the host's own
test suite.

## 2. Unit tests

### 2.1 Tracer and clock (`test_tracer.py`, `test_clock.py`)

- Envelope fields present, keys sorted.
- Event ids gapless; `ts_virtual` monotonic, and a non-monotonic write is
  rejected.
- `verify_chain` passes on a good file and names the first bad link on a
  tampered one.
- Redaction strips `*_token` / `*secret*` / `*key*` values but keeps
  `idempotency_key`; `detail` over 64 KB gets a truncation marker.
- `compute_run_id` follows the authority rule.
- `VirtualClock` defaults to tick 50 ms and t=0; `sleep` advances; `deadline`
  does not; negative inputs are rejected.

### 2.2 Ledger test double (`test_ledger.py`)

Versioned reads and writes: seeded record at version 1, fresh write bumps the
version, a stale write still commits but flags a conflict, a concurrent
update is preserved in the snapshot, and the ledger hash is stable.

### 2.3 Fault catalog (`test_generic_core.py`)

- No catalog entry has a default `tool`.
- `validate_fault_entry("timeout_after_commit", {"tool": "payments.charge"})`
  keeps the user's tool and defaults `occurrence` to 1.
- `concurrent_update` without `key` and `updates` raises `UnknownFaultError`.

## 3. Component tests

### 3.1 SDK (`test_sdk.py`)

Sync and async `@ac.workflow` capture a run; a configured fault fires
deterministically and redaction applies; `AGENTCRASH_DISABLED=1` is a
passthrough; the bounded queue drops oldest; a dead `endpoint` never breaks
the host. `test_generic_core.py` adds thread-safe hash chains (8 threads, 50
events each, no drops) and acceptance of non-JSON tool outputs.

### 3.2 Verifiers and evaluation

- `FreshWriteOnly(tools=[...])` fails on a write without a fresh version for
  a user-named store tool, and the evidence names the step.
- `duplicate_mutating_effect` and `required_step:<name>` are exercised
  through `check` (section 4). `test_lab.py` covers evaluation storage and
  read-back (`/v1/evaluations`, `/v1/runs/{id}/evaluation`).
- `compare_reports` flags a regression between two reports.
- Malformed evaluation payloads are client errors, never a pass.
- Verifier tests run fully offline. A test that needs a key is deleted and
  rewritten.

## 4. Integration tests

| Test | Assertion |
|---|---|
| `test_check_cli_knows_builtin_invariants` | A run with `timeout_after_commit` and a blind retry exits 1; output has `[FAIL] Invariant duplicate_mutating_effect` and `[PASS] Invariant required_step:charge` |
| `test_idempotent_ingestion_and_run_detail` | Re-posting the same `event_id` does not duplicate events |
| `test_http_roundtrip_multi_project` | Projects stay isolated over HTTP |
| `test_prune_enforces_retention`, `test_prune_over_http_requires_fields` | `/v1/admin/prune` keeps last N and/or drops runs older than N days; missing criteria is rejected |
| `test_auth_allowlist_gates_api_but_not_lab` | With `AGENTCRASH_API_TOKENS` set, `/v1/*` returns 401 without a listed bearer; Lab static assets stay public |
| `test_lab_static_and_evaluation_roundtrip` | Lab UI served; evaluation submit and read-back work |
| `test_help_documents_every_flag`, `test_top_level_help_lists_all_commands`, `test_bogus_command_exits_nonzero` | CLI help covers every flag; unknown commands exit non-zero |
| `test_minimal_agent_end_to_end` | `examples/minimal_agent/app.py` runs against the SDK |

## 5. Determinism and guard tests

| Rule | Test | Failure means |
|---|---|---|
| No wall-clock or global random in deterministic modules (`clock.py`, `tracer.py`, `ledger.py`, `injector.py`, `verifiers.py`, ...) | `test_acr001.py::test_no_wall_clock_or_global_random` | A banned import in a deterministic module |
| Core is domain-free | `test_generic_core.py::test_core_source_has_no_domain_vocabulary` | Banned vocabulary under `src/agentcrash/` |
| Core import pulls no domain stacks | `test_lab.py::test_core_import_pulls_no_domain_stacks` | A heavy or domain dependency crept into import time |
| A second host works on stdlib plus `agentcrash` only | `test_second_app.py` | The core leaked host assumptions |
| No baked demo bundle in the Lab | `test_no_static_bundle.py` | Demo content shipped in core |
| Hash fields are never redacted; chains verify over 200 writes | `test_tracer_never_redacts_its_own_hash_fields` | Redaction corrupted the chain |
| Same input produces the same verdict | `check` run twice on one trace (manual, doc 14) | A verifier reads time, randomness or the network |

## 6. Perf evidence (NFR-PERF-01)

`bench/run_overhead.py` measures per-run overhead against a bare 12-step loop
with 2 ms stub steps. Latest numbers are in
`docs/phase7-evidence/bench.json`:

| Metric | Measured | Gate |
|---|---|---|
| Per-run overhead vs bare loop | 5.7% (24.0 ms bare, 25.4 ms instrumented, median of 30 runs) | <= 10% |
| Absolute overhead | ~114 us per step | reported |
| SDK cost | ~14 us per event | reported |
| Tracer cost | ~31 us per event | <= 60 us |
| 100-step trace size | ~38 KB (37,797 bytes) | <= 2 MB |

Re-run it after any change to `sdk.py`, `tracer.py` or `events.py`.

## 7. CI gates

`.github/workflows/ci.yml` runs on every push and pull request, on a
py3.11, 3.12, 3.13 matrix (ubuntu-latest, `fail-fast: false`):

| Gate | CI step | Command | Blocks on |
|---|---|---|---|
| G0 install | Install (with dev extras) | `uv sync --extra dev` | Resolution failure |
| G1 lint | Lint | `uv run --no-sync ruff check .` | Any finding |
| G2 format | Format | `uv run --no-sync black --check .` | Any diff |
| G3 tests | Tests | `uv run --no-sync pytest -q` | Any failure |
| G4 determinism | Determinism lint (ACR001) | `uv run --no-sync pytest tests/test_acr001.py -q` | Banned wall-clock or random use |

Local equivalent on Windows (repo root):

```powershell
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m black --check .
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m pytest tests/test_acr001.py -q
```

CI does not run `bench/run_overhead.py` and does not run the demo loop;
both are manual evidence (docs 12 and 14).

Milestone mapping (M0-M3 per ADR-006) and roadmap items:

| Milestone | Test additions |
|---|---|
| M0 | Current suite: tracer, SDK, verifiers, control plane, guard tests |
| M1 | Fault catalog v2 and invariant library v2 fixtures (R2, R3) |
| M2 | Reliability metrics tests (R4); standard-trace conversion tests (R5) |
| M3 | Host-supplied runner contract tests for diagnose and replay (R6); release checks (R7) |

## 8. Test data and fixtures

- Tests build traces in code through the SDK or `TraceWriter`, in `tmp_path`.
  There are no committed golden traces.
- All values are synthetic: neutral tool names (`payments.charge`,
  `crm.update`, `kv`), `a@b.co`-style emails, fixed card-like constants.
- Redaction canaries (planted secret-shaped keys and emails) must never
  appear raw in any emitted trace or report. `check --scan-pii` scans a trace
  and state file for emails and card-like digit runs; the redaction
  placeholders `redacted@example.com` and `4111111111111111` are exempt.
- `fetch_live_patch` is tested only for its opt-in default (no base URL
  returns `None`); no test calls a model.

## 9. Open Questions

| Question | Owner | Notes |
|---|---|---|
| Coverage threshold gate (none today) | Tech lead | Add a number only once it can be met without padding |
| Run `bench/run_overhead.py` in CI as a smoke gate | Tech lead | Timing noise on shared runners; decide with R7 |
| Windows runner in the CI matrix | Tech lead | Development is on Windows; CI is ubuntu-only today |
