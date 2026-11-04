"""Reliability evaluation over SDK traces.

Framework-neutral: callers pass explicit invariant objects with ``.id``
and ``.check(trace, state)`` (see :mod:`agentcrash.verifiers` for the
protocol). Produces machine-readable reports and baseline-vs-fault
comparisons for regression tracking.
"""

from __future__ import annotations

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
