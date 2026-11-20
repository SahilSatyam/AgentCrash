# AgentCrash Production Upgrades — Phased Implementation Plan

Date: 2026-11-20
Source: `agentcrash_technical_analysis_and_state_of_the_art_upgrade_roadmap.html` plus workspace evidence in `src/agentcrash/`
Base: `agentcrash-core v0.3.0`, Python 3.11+, `hatchling`, core deps `pyyaml` + `jsonschema` only (`pyproject.toml`)

## Goal

Implement the five roadmap upgrades in three phases, turning AgentCrash from a deterministic prototype into a production chaos platform, while keeping `agentcrash-core` zero-dependency and verifiers pure.

## Success Criteria

- Phase 1: no caller-thread HTTP stall under async load; `flush()` drains the full queue; dropped events are counted; runs and idempotency dedupe survive server restart.
- Phase 2: all 4 fault kinds fire deterministically from a seed; `no_lost_update` catches injected version drift in CI; card false positives drop with no missed obvious secrets in existing fixtures.
- Phase 3: chaos runs correlate in an OTel backend when the extra is enabled; signed chains verify; `check --fuzz` emits a minimal failing seed plus shrunken trace.

## Approach

Preserve the split the roadmap praises: thin runtime instrumentation that never raises, and pure offline verification. Add durability and async behind interfaces rather than rewrites. Keep `AGENTCRASH_DISABLED=1` as a no-op passthrough, keep `server` and observability pieces optional, and reuse `FaultSchedule`, `redact()`, `verify_sdk_chain`, `evaluate_trace`, and `compare_reports` instead of adding frameworks.

Applicable workflow per project skills: plan via `planning-and-task-breakdown`, then build with `incremental-implementation` plus `test-driven-development`, and review with `code-review-and-quality` before any publish.

Assumptions (reversible): SQLite WAL becomes the default durable store; PostgreSQL, OTel export, and Ed25519 signing stay optional extras; exact OTel span field names are pinned at Phase 3 kickoff from the specs named in the roadmap.

## Steps

### Phase 1 — P0 correctness: non-blocking export + durable store

1. Exporter worker in `src/agentcrash/sdk.py` (`_Exporter.emit`, `_flush_best_effort`, `flush`, `drain`, `BATCH_SIZE`, `sent`/`failed`/`dropped`).
   - Move HTTP send off the caller thread: daemon worker thread, or an asyncio background task when running inside an event loop.
   - Keep bounded ring-buffer policy: drop oldest, increment `dropped`.
   - Change `flush()` from one batch to loop-until-empty with a configurable shutdown deadline; expose counters for backpressure.
   - Compatibility: no new required dep; `endpoint=None` path unchanged; never raise.
   - Tests: extend `tests/test_sdk.py` with async stall test, full-drain test with >50 events, counter test.

2. Storage protocol plus SQLite backend in `src/agentcrash/server.py` (`_STORE`, `_LOCK`, `reset_store`, `register_project`, `ingest_events`, stdlib `serve()`, FastAPI factory).
   - Introduce a small `Storage` protocol covering current dict operations: project register, idempotent event ingest on `event_id`, run save, evaluation save, Lab listing.
   - Ship in-memory (tests) plus SQLite WAL (default local/CI) behind it; preserve `event_id` dedupe and `seq` order.
   - Migrate Lab UI history and run listing to the same interface; handle first-run file creation and restart recovery.
   - Tests: extend `tests/test_server_ingestion.py` and `tests/test_lab.py` with restart-persistence and duplicate-replay cases.

### Phase 2 — P1 chaos surface: active faults + seeded RNG + PII

3. Activate `poisoned_doc` and `concurrent_update` in `src/agentcrash/injector.py` (`KNOWN_FAULT_KINDS`, `FAULT_CATALOG`, `FaultSchedule.fault_for` / `fault_for_call`) plus the SDK tool boundary.
   - `poisoned_doc`: intercept tool read outputs and inject the configured payload from fixture params (`doc_id` / `fixture_key`).
   - `concurrent_update`: mutate simulated version / ETag between read and write so `NoLostUpdate` fires; uses existing `key` + `updates` params.
   - Wire `FaultPolicy.seed` / scenario `seed` into an isolated `random.Random(seed)` threaded through `FaultSchedule`; keep fixed `at_step` / `occurrence` triggers working.
   - Tests: deterministic seed replay (same seed, same schedule), occurrence matching, CLI refusal of unknown kinds.

4. Harden redaction in `src/agentcrash/tracer.py` (`_CARD_RE`, `_scrub_str`, `redact`) and `src/agentcrash/scan_pii.py`.
   - Add Luhn checksum to card matches; add prefix plus Shannon-entropy scan for JWT / cloud API tokens; keep cheap prechecks and the `idempotency_key` exemption.
   - Tests: extend `tests/test_tracer.py` and `tests/test_gaps.py` with false-positive numeric IDs, valid cards, and JWT fixtures.

### Phase 3 — P2 scale and discovery: OTel + Postgres + signing + fuzz

5. Optional OTel export in `tracer.py` / `sdk.py`: map `run_id`, `step`, tool boundaries, and `ts_virtual` onto agent spans with `traceparent` propagation. OTel deps live in an extra; core stays dependency-free.
6. Tamper-evident chains: optional HMAC-SHA256 signing over the `prev_hash` chain (Ed25519 only if a vetted lib is already approved). Extend `verify_sdk_chain` and `check --verify-chain` to verify when a key is configured and pass through when not.
7. PostgreSQL adapter behind the Phase 1 `Storage` protocol for multi-worker FastAPI. SQLite stays the default. Document connection and migration notes.
8. Schedule fuzzer plus shrinker in `src/agentcrash/cli.py` (`check --fuzz`) and `src/agentcrash/evaluate.py`: explore combinations of timeout / concurrent-update / poisoned-doc over multi-step graphs with deterministic seeds. On invariant failure run delta-debugging to emit the smallest schedule plus seed and failing trace. Cap combinations and add `--max-cases` / timeout guards.
   - Tests: golden failing seed shrinks to a minimal case; `--fuzz` respects budget flags; `compare_reports` flags the regression.

## Validation Plan

- Per phase: `python -m pytest tests/test_sdk.py tests/test_server_ingestion.py tests/test_lab.py -q`, then `tests/test_tracer.py tests/test_gaps.py tests/test_gaps_2.py -q`, then full `python -m pytest -q`.
- Lint and format gates: `ruff check .` and `black --check .` per `pyproject.toml` config.
- Manual checks: async load run with endpoint latency injected (no loop stall); kill-and-restart server (history intact); same-seed rerun diff (byte-identical schedule); OTel backend shows correlated spans; fuzz run shrinks to a one- or two-fault minimal case.
- Highest-risk validation: Phase 1 async export under load plus restart recovery. If either flakes, stop before Phase 2.

## Risks / Open Questions

- Backpressure tuning: worker queue size and shutdown deadline need load numbers. Default to current `BATCH_SIZE` / `max_queue` behavior and expose metrics first.
- SQLite WAL fixes single-node restart, not horizontal scale. That is why Postgres waits for Phase 3. In-memory backend stays for tests, so rollback is safe.
- Entropy scanning risks false positives. Keep it behind a flag if CI fixtures disagree.
- Fuzzer state explosion. Mitigated by deterministic seeds, combination caps, and shrinking.
- Non-goals: no new required core dependencies, no domain demo code, no Lab redesign, no change to pure-verifier semantics.
- Open questions: none that block planning. Pin OTel field names and Postgres connection defaults at Phase 3 kickoff.
