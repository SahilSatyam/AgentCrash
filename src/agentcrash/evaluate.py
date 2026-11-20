"""Reliability evaluation over SDK traces.

Framework-neutral: callers pass explicit invariant objects with ``.id``
and ``.check(trace, state)`` (see :mod:`agentcrash.verifiers` for the
protocol). Produces machine-readable reports and baseline-vs-fault
comparisons for regression tracking.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from typing import Any, Protocol


class Invariant(Protocol):
    """Structural contract for an invariant evaluator."""

    id: str

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Any: ...


def evaluate_trace(
    trace: list[dict[str, Any]],
    invariants: list[Invariant],
    state: dict[str, Any] | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    """Run invariants over one trace; returns a machine-readable report."""
    state = state or {}
    results = []
    for invariant in invariants:
        verdict = invariant.check(trace, state)
        results.append(verdict.to_dict() if hasattr(verdict, "to_dict") else dict(verdict))
    passed = sum(1 for r in results if r.get("passed"))
    return {
        "run_id": run_id,
        "invariants": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "verdict": "PASS" if passed == len(results) else "FAIL",
        "results": results,
    }


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Diff two evaluation reports; flags regressions and fixes."""
    base = {r["id"]: r.get("passed") for r in baseline.get("results", [])}
    cand = {r["id"]: r.get("passed") for r in candidate.get("results", [])}
    regressions = sorted(i for i, ok in cand.items() if ok is False and base.get(i) is True)
    fixes = sorted(i for i, ok in cand.items() if ok is True and base.get(i) is False)
    return {
        "baseline": baseline.get("verdict"),
        "candidate": candidate.get("verdict"),
        "regressions": regressions,
        "fixes": fixes,
        "regression": bool(regressions),
    }


def fuzz_schedules(
    boundaries: list[str],
    *,
    fault_kinds: list[str] | None = None,
    seed: int = 42,
    max_cases: int = 20,
) -> list[dict[str, dict[str, Any]]]:
    """Generate deterministic fault schedule combinations for multi-step boundaries."""
    if not boundaries:
        return []
    rng = random.Random(seed)
    kinds = fault_kinds or ["timeout_after_commit", "concurrent_update", "poisoned_doc"]
    schedules: list[dict[str, dict[str, Any]]] = []
    seen_keys: set[str] = set()

    def _make_fault(boundary: str, kind: str) -> dict[str, Any]:
        if kind == "timeout_after_commit":
            return {"kind": "timeout_after_commit", "occurrence": rng.choice([1, 2])}
        elif kind == "concurrent_update":
            return {
                "kind": "concurrent_update",
                "key": f"key_{boundary}",
                "updates": {"status": "stale", "version": rng.randint(2, 5)},
            }
        elif kind == "poisoned_doc":
            return {
                "kind": "poisoned_doc",
                "doc_id": f"doc_{boundary}",
                "payload": "[INJECTED_OVERRIDE_INSTRUCTION]",
            }
        return {"kind": kind}

    # Priority 1: single-fault schedules for each boundary and kind
    for b in boundaries:
        for k in kinds:
            sched = {b: _make_fault(b, k)}
            rep = json.dumps(sched, sort_keys=True)
            if rep not in seen_keys:
                seen_keys.add(rep)
                schedules.append(sched)
                if len(schedules) >= max_cases:
                    return schedules

    # Priority 2: combinations
    while len(schedules) < max_cases and len(boundaries) >= 2:
        pair_b = rng.sample(boundaries, min(2, len(boundaries)))
        sched = {b: _make_fault(b, rng.choice(kinds)) for b in pair_b}
        rep = json.dumps(sched, sort_keys=True)
        if rep not in seen_keys:
            seen_keys.add(rep)
            schedules.append(sched)

    return schedules[:max_cases]


def shrink_schedule(
    schedule: dict[str, dict[str, Any]],
    test_fn: Callable[[dict[str, dict[str, Any]]], bool],
) -> dict[str, dict[str, Any]]:
    """Delta-debug a failing fault schedule to the minimal failing subset.

    ``test_fn(candidate_schedule)`` returns True if the invariant failure
    reproduces, False otherwise.

    .. note:: Minimization Bound
       ``shrink_schedule`` employs a greedy single-removal minimization strategy
       rather than recursive subset partitioning (full $DDmin$). It meets the
       target of isolating 1-to-2 fault regressions without state explosion.
    """
    if len(schedule) <= 1:
        return schedule

    # Step 1: Check if any 1-fault subset reproduces the failure
    for b, fault in schedule.items():
        single = {b: fault}
        if test_fn(single):
            return single

    # Step 2: Try removing one fault at a time
    current = dict(schedule)
    changed = True
    while changed and len(current) > 1:
        changed = False
        for b in list(current.keys()):
            candidate = {k: v for k, v in current.items() if k != b}
            if test_fn(candidate):
                current = candidate
                changed = True
                break

    return current


def simulate_trace_faults(
    trace: list[dict[str, Any]],
    schedule: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Simulate fault injections over an existing trace and return (mutated_trace, state).

    .. note:: Occurrence Semantics
       Faults fire on the exact call specified by ``occurrence`` (defaulting to the
       first call: ``1``). If ``occurrence == "all"``, the fault fires on every invocation.
    """
    mutated: list[dict[str, Any]] = []
    state: dict[str, Any] = {"record_versions": []}
    call_counts: dict[str, int] = {}
    seq = len(trace)

    for event in trace:
        ev = dict(event)
        tool = ev.get("tool") or ev.get("step")
        if tool and tool in schedule:
            fault = schedule[tool]
            call_counts[tool] = call_counts.get(tool, 0) + 1
            occ = fault.get("occurrence", 1)
            if call_counts[tool] == occ or occ == "all":
                kind = fault.get("kind")
                ev["fault"] = fault
                if kind == "timeout_after_commit":
                    ev["status"] = "error"
                    ev["error"] = "TimeoutError: simulated timeout after commit"
                    mutated.append(ev)
                    # Retry duplicates tool call
                    seq += 1
                    retry_ev = dict(ev)
                    retry_ev["seq"] = seq
                    retry_ev["event_id"] = f"{ev.get('event_id', 'evt')}_retry"
                    retry_ev["status"] = "ok"
                    retry_ev["error"] = None
                    mutated.append(retry_ev)
                    continue
                elif kind == "concurrent_update":
                    seq += 1
                    key = fault.get("key", f"key_{tool}")
                    fault_ev = {
                        "schema_version": 1,
                        "event_id": f"evt_fault_{seq}",
                        "run_id": ev.get("run_id", "run"),
                        "kind": "fault",
                        "status": "ok",
                        "seq": seq,
                        "tool": tool,
                        "fault": fault,
                    }
                    mutated.append(fault_ev)
                    outputs = (
                        dict(ev.get("outputs", {})) if isinstance(ev.get("outputs"), dict) else {}
                    )
                    outputs.update(fault.get("updates", {}))
                    outputs["conflict"] = True
                    outputs["version"] = int(outputs.get("version", 1)) + 1
                    ev["outputs"] = outputs
                    state["record_versions"].append(
                        {
                            "record": key,
                            "value": {"status": "stale_overwritten"},
                        }
                    )
                elif kind == "poisoned_doc":
                    outputs = (
                        dict(ev.get("outputs", {})) if isinstance(ev.get("outputs"), dict) else {}
                    )
                    outputs["poisoned"] = True
                    outputs["payload"] = fault.get("payload", "[INJECTED_PAYLOAD]")
                    ev["outputs"] = outputs
        mutated.append(ev)
    return mutated, state


def fuzz_and_shrink(
    runner_fn: Callable[[dict[str, dict[str, Any]]], tuple[list[dict[str, Any]], dict[str, Any]]],
    invariants: list[Invariant],
    *,
    boundaries: list[str],
    seed: int = 42,
    max_cases: int = 20,
    fault_kinds: list[str] | None = None,
) -> dict[str, Any]:
    """Fuzz combinations of faults, and shrink failing cases to the minimal schedule."""
    schedules = fuzz_schedules(
        boundaries,
        fault_kinds=fault_kinds,
        seed=seed,
        max_cases=max_cases,
    )

    cases_tested = 0
    for sched in schedules:
        cases_tested += 1
        trace, state = runner_fn(sched)
        report = evaluate_trace(trace, invariants, state)
        if report.get("verdict") == "FAIL":
            # Found a failing schedule! Delta-debug / shrink it.
            def test_fn(cand: dict[str, dict[str, Any]]) -> bool:
                t, s = runner_fn(cand)
                r = evaluate_trace(t, invariants, s)
                return r.get("verdict") == "FAIL"

            minimal_sched = shrink_schedule(sched, test_fn)
            min_trace, min_state = runner_fn(minimal_sched)
            min_report = evaluate_trace(min_trace, invariants, min_state)

            return {
                "cases_tested": cases_tested,
                "failed": True,
                "seed": seed,
                "original_schedule": sched,
                "minimal_schedule": minimal_sched,
                "minimal_trace": min_trace,
                "report": min_report,
            }

    return {
        "cases_tested": cases_tested,
        "failed": False,
        "seed": seed,
        "original_schedule": None,
        "minimal_schedule": None,
        "minimal_trace": None,
        "report": None,
    }
