"""Harness overhead + trace-size budgets (doc 12: <=10% overhead, <=2MB/100 steps).

Compares N ``TraceWriter.write`` calls (hash chain + redaction + flushed
file write) against a bare loop doing the same per-line flushed JSONL
serialization without the harness, so the ratio isolates chain/redaction
cost at equal durability; then writes a synthetic 100-step trace and
checks its size. Prints a JSON result and exits non-zero on a budget miss.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentcrash.tracer import TraceWriter

OVERHEAD_TARGET = 0.10
TRACE_SIZE_TARGET_BYTES = 2 * 1024 * 1024
N_EVENTS = 2000
REPEATS = 5


def _harness_write(writer: TraceWriter, i: int) -> None:
    writer.write(
        step=i,
        tool="charge_card",
        args={"amount_cents": 10000 + i, "idempotency_key": f"inv-9821-{i:04d}"},
        outcome="success",
        latency=300,
        detail={"phase": "tool_result", "data": {"charge_id": f"ch_{i:05d}"}},
    )


def _envelope(i: int) -> dict:
    return {
        "args": {"amount_cents": 10000 + i, "idempotency_key": f"inv-9821-{i:04d}"},
        "detail": {"data": {"charge_id": f"ch_{i:05d}"}, "phase": "tool_result"},
        "event_id": f"evt_bench_{i:04d}",
        "injector_event": None,
        "latency": 300,
        "outcome": "success",
        "policy_decision": None,
        "prev_hash": "GENESIS",
        "run_id": "bench",
        "scenario": "bench",
        "step": i,
        "tool": "charge_card",
        "ts_virtual": i * 50,
    }


def _time_bare(tmp: Path) -> float:
    best = float("inf")
    for _ in range(REPEATS):
        target = tmp / "bare.jsonl"
        start = time.perf_counter()
        with target.open("w", encoding="utf-8") as handle:
            for i in range(N_EVENTS):
                handle.write(json.dumps(_envelope(i), sort_keys=True) + "\n")
                handle.flush()
        best = min(best, time.perf_counter() - start)
    return best


def _time_harness(tmp: Path) -> float:
    best = float("inf")
    for _ in range(REPEATS):
        target = tmp / "harness.jsonl"
        if target.exists():
            target.unlink()
        writer = TraceWriter(run_id="bench", scenario="bench", path=str(target))
        start = time.perf_counter()
        for i in range(N_EVENTS):
            _harness_write(writer, i)
        best = min(best, time.perf_counter() - start)
    return best


def _hundred_step_bytes(tmp: Path) -> int:
    target = tmp / "hundred.jsonl"
    if target.exists():
        target.unlink()
    writer = TraceWriter(run_id="bench", scenario="bench", path=str(target))
    for i in range(100):
        _harness_write(writer, i)
    return target.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/phase7-evidence/bench.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="agentcrash-bench-") as raw:
        tmp = Path(raw)
        bare = _time_bare(tmp)
        harness = _time_harness(tmp)
        size = _hundred_step_bytes(tmp)
    overhead = (harness - bare) / bare if bare > 0 else 0.0
    result = {
        "n_events": N_EVENTS,
        "repeats": REPEATS,
        "bare_s": round(bare, 4),
        "harness_s": round(harness, 4),
        "overhead_ratio": round(overhead, 4),
        "overhead_target": OVERHEAD_TARGET,
        "overhead_pass": overhead <= OVERHEAD_TARGET,
        "hundred_step_bytes": size,
        "trace_size_target_bytes": TRACE_SIZE_TARGET_BYTES,
        "trace_size_pass": size <= TRACE_SIZE_TARGET_BYTES,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if (result["overhead_pass"] and result["trace_size_pass"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
