# Phase 7 cost sheet (M3 ship prep)

All verification ran offline on the `fake` adapter. No live model key exists,
so live-model paths are implemented but never exercised against the network.

| Item | Observed |
|---|---|
| Live model calls | 0 |
| Live tokens in/out | 0 / 0 |
| Live model spend | $0.00 |
| Infrastructure | $0 (local WSL run, no cloud resources) |
| CI minutes used | 0 (matrix defined in `.github/workflows/ci.yml`, not yet executed remotely) |
| Benchmark | `bench/run_overhead.py` → `bench.json` (this directory) |

## Benchmarks (observed 2026-10-29, local WSL)

From `bench.json`:

- Harness vs bare flushed-JSONL loop over 2000 envelope events (best of 5):
  overhead ratio **4.8553** vs target <= 0.10 — **MISS**. The per-event delta
  (~33 us: write-time redaction walk + canonical re-serialization + SHA-256
  chain link) dominates this serialization-only micro-benchmark. End-to-end
  `run` wall time stays in seconds (faulted seed-42 run completes < 5 s wall),
  so this is a micro-benchmark gap, not a demo blocker. Follow-up: batch
  hashing / single-pass redact+serialize before optimizing.
- 100-step trace size **40197 bytes** vs 2 MB budget — **PASS** (~2% of budget).

Fixture-backed defaults everywhere: stub/fake adapters, synthetic NL/ODRL
fixtures, golden traces. Nothing in this milestone required paid inference.
