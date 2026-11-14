"""Harness overhead + trace-size budgets (doc 12 NFR-PERF-01/02).

NFR-PERF-01 is "<= 10% added wall-clock per run vs the bare agent loop".
This script runs a 12-step stub agent loop twice: bare, and instrumented
the way a host integrates (SDK run/step/record_tool/maybe_fault: three
hash-chained, redacted events per step). Each stub step does a fixed
``STEP_COST_MS`` (2 ms) of deterministic busy work standing in for the
tool or model call. That is a deliberately harsh floor: networked tool
calls take 10 ms or more and model calls 100 ms or more, so real
overhead is far smaller than reported here. ``overhead_us_per_step`` is
the absolute added cost and does not depend on that assumption.

It also reports absolute per-event costs (SDK, and the standalone JSONL
``TraceWriter`` flushed per line, gated at ``TRACER_US_TARGET``), which
do not depend on the step-cost assumption, and the
size of a 100-step trace (NFR-PERF-02, <= 2 MB). Prints a JSON result
and exits non-zero on a budget miss.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentcrash.sdk import AgentCrash, WorkflowConfig
from agentcrash.tracer import TraceWriter

OVERHEAD_TARGET = 0.10
TRACE_SIZE_TARGET_BYTES = 2 * 1024 * 1024
STEPS = 12
STEP_COST_MS = 2.0
RUNS = 30
MICRO_EVENTS = 2000
#: Per-event budget for the standalone JSONL tracer (flushed per line).
TRACER_US_TARGET = 60.0


def _stub_tool(i: int) -> dict:
    """Deterministic busy work standing in for one tool/model call."""
    deadline = time.perf_counter() + STEP_COST_MS / 1000.0
    n = 0
    while time.perf_counter() < deadline:
        n += 1
    return {"record": "order-1", "version": i + 2, "spins": n > 0}


def _write(writer: TraceWriter, i: int, args: dict, data: dict) -> None:
    writer.write(
        step=i,
        tool="write_record",
        args=args,
        outcome="success",
        latency=50,
        detail={"phase": "tool_result", "data": data},
    )


def _bare_run() -> float:
    start = time.perf_counter()
    for i in range(STEPS):
        _stub_tool(i)
    return time.perf_counter() - start


def _instrumented_run() -> float:
    """The real integration path: SDK run/step/record_tool/maybe_fault.

    Every step emits three hash-chained, redacted, validated events
    (step_start, tool, step_end) into the bounded export queue.
    """
    ac = AgentCrash(WorkflowConfig(project="bench", fault_injection=True, seed=42))
    start = time.perf_counter()
    with ac.run(name="bench") as run:
        for i in range(STEPS):
            with run.step(f"step-{i}") as step:
                args = {"record": "order-1", "expected_version": i}
                out = _stub_tool(i)
                run.maybe_fault("write_record")
                step.record_tool("write_record", inputs=args, outputs=out)
    return time.perf_counter() - start


def _tracer_us_per_event(tmp: Path) -> float:
    best = float("inf")
    for r in range(5):
        with TraceWriter(run_id="bench", scenario="bench", path=str(tmp / f"m{r}.jsonl")) as w:
            start = time.perf_counter()
            for i in range(MICRO_EVENTS):
                _write(w, i, {"record": "order-1", "expected_version": i}, {"version": i + 2})
            best = min(best, time.perf_counter() - start)
    return best / MICRO_EVENTS * 1e6


def _sdk_us_per_event() -> float:
    best = float("inf")
    for _ in range(5):
        ac = AgentCrash(WorkflowConfig(project="bench", max_queue=MICRO_EVENTS + 10))
        with ac.run(name="bench") as run, run.step("s") as step:
            start = time.perf_counter()
            for i in range(MICRO_EVENTS):
                step.record_tool("write_record", inputs={"expected_version": i}, outputs={"v": i})
            best = min(best, time.perf_counter() - start)
    return best / MICRO_EVENTS * 1e6


def _hundred_step_bytes(tmp: Path) -> int:
    target = tmp / "hundred.jsonl"
    with TraceWriter(run_id="bench", scenario="bench", path=str(target)) as writer:
        for i in range(100):
            _write(writer, i, {"record": "order-1", "expected_version": i}, {"version": i + 2})
    return target.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/phase7-evidence/bench.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="agentcrash-bench-") as raw:
        tmp = Path(raw)
        _bare_run(), _instrumented_run()  # warm-up
        bare, inst = [], []
        for _ in range(RUNS):  # interleave so drift hits both equally
            bare.append(_bare_run())
            inst.append(_instrumented_run())
        tracer_us = _tracer_us_per_event(tmp)
        sdk_us = _sdk_us_per_event()
        size = _hundred_step_bytes(tmp)
    bare_mean, inst_mean = statistics.median(bare), statistics.median(inst)
    overhead = (inst_mean - bare_mean) / bare_mean if bare_mean > 0 else 0.0
    result = {
        "steps": STEPS,
        "runs": RUNS,
        "step_cost_ms": STEP_COST_MS,
        "bare_run_ms_median": round(bare_mean * 1000, 3),
        "instrumented_run_ms_median": round(inst_mean * 1000, 3),
        "overhead_ratio": round(overhead, 4),
        "overhead_us_per_step": round((inst_mean - bare_mean) / STEPS * 1e6, 1),
        "overhead_target": OVERHEAD_TARGET,
        "overhead_pass": overhead <= OVERHEAD_TARGET,
        "tracer_us_per_event": round(tracer_us, 1),
        "tracer_us_target": TRACER_US_TARGET,
        "tracer_pass": tracer_us <= TRACER_US_TARGET,
        "sdk_us_per_event": round(sdk_us, 1),
        "hundred_step_bytes": size,
        "trace_size_target_bytes": TRACE_SIZE_TARGET_BYTES,
        "trace_size_pass": size <= TRACE_SIZE_TARGET_BYTES,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True, indent=2))
    ok = result["overhead_pass"] and result["trace_size_pass"] and result["tracer_pass"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
