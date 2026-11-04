"""Domain-free verifiers: pure ``(trace, state) -> Verdict`` checks.

Core keeps only the checks that need no domain stack: the verifier
registry (re-exported from :mod:`agentcrash._registry`), the generic
write-freshness and lost-update invariants, and the trace/state loading
plus ``run_verifiers`` helpers. ODRL-specific invariants live in the
host/extension package (``odrl_agent.verifiers``), which imports
:exc:`VerifierError`, :class:`FreshWriteOnly` and :class:`NoLostUpdate`
from here. Core never imports ODRL/SHACL/DSPy stacks.

Rules: verifiers read only the trace events and the state snapshot — no
network, no wall-clock, no model calls. A violated invariant becomes
``Verdict(passed=False)`` with step-level evidence; a harness problem
(malformed trace/state the verifier cannot judge) raises
:exc:`VerifierError` so callers report corruption instead of a false
verdict. Unknown invariant ids raise :exc:`UnknownVerifierError`.
"""

from __future__ import annotations

import json
from typing import Any

from agentcrash._registry import (
    TraceCorruptError,
    UnknownVerifierError,
    Verdict,
    Verifier,
    VerifierError,
    get_verifier,
    register_verifier,
    verifier_ids,
)

#: Tool name for ODRL saves, kept as a plain string so the generic
#: freshness check covers versioned ODRL writes without importing them.
SAVE_ODRL = "save_odrl"

__all__ = [
    "SAVE_ODRL",
    "FreshWriteOnly",
    "NoLostUpdate",
    "TraceCorruptError",
    "UnknownVerifierError",
    "Verdict",
    "Verifier",
    "VerifierError",
    "get_verifier",
    "load_state_snapshot",
    "load_trace_events",
    "register_verifier",
    "run_verifiers",
    "verifier_ids",
]


def _mutating_calls(trace: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    """Return executed (non-error) ``tool`` results from the trace."""
    if not isinstance(trace, list):
        raise VerifierError(f"trace must be a list of events, got {type(trace).__name__}")
    calls: list[dict[str, Any]] = []
    for event in trace:
        if not isinstance(event, dict):
            raise VerifierError("trace contains a non-object event")
        if event.get("tool") != tool:
            continue
        if event.get("detail", {}).get("phase") != "tool_result":
            continue
        if event.get("outcome") == "error":
            continue
        calls.append(event)
    return calls


class FreshWriteOnly:
    """Every mutating write carries a freshness proof.

    A ``key_value_store`` or ``save_odrl`` write passes only when its
    ``expected_version`` matched the store version (``version_conflict``
    false). Stale or version-less writes fail, even though the store
    commits them.
    """

    id = "fresh_write_only"

    #: Versioned-store tools whose writes need a freshness proof.
    VERSIONED_TOOLS = ("key_value_store", SAVE_ODRL)

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict:
        del state
        writes: list[dict[str, Any]] = []
        for tool in self.VERSIONED_TOOLS:
            writes.extend(
                event
                for event in _mutating_calls(trace, tool)
                if (event.get("args") or {}).get("op") == "write"
            )
        stale = sorted(
            event.get("step")
            for event in writes
            if (event.get("detail") or {}).get("data", {}).get("version_conflict") is True
            or (event.get("detail") or {}).get("data", {}).get("expected_version") is None
        )
        if not stale:
            return Verdict(self.id, True, f"{len(writes)} write(s) carried the fresh version")
        return Verdict(
            self.id,
            False,
            f"stale write(s) without freshness proof at steps {stale}",
        )


class NoLostUpdate:
    """The final state keeps every concurrent update.

    For each ``concurrent_update`` injector event, every applied field
    must still hold the rival value in the final snapshot. Traces without
    a concurrent writer pass vacuously.
    """

    id = "no_lost_update"

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict:
        if not isinstance(trace, list):
            raise VerifierError(f"trace must be a list of events, got {type(trace).__name__}")
        if not isinstance(state, dict):
            raise VerifierError(f"state must be an object, got {type(state).__name__}")
        final = {
            rec.get("record"): rec.get("value", {})
            for rec in state.get("record_versions", [])
            if isinstance(rec, dict)
        }
        lost: list[str] = []
        rivals = 0
        for event in trace:
            injector = event.get("injector_event") or {}
            if injector.get("fault") != "concurrent_update":
                continue
            rivals += 1
            record = injector.get("record")
            updates = injector.get("updates") or {}
            current = final.get(record, {})
            for field, want in updates.items():
                if not isinstance(current, dict) or current.get(field) != want:
                    lost.append(
                        f"step {event.get('step')}: {record}.{field} " f"(rival {want!r} lost)"
                    )
        if not rivals:
            return Verdict(self.id, True, "no concurrent writer in trace; nothing to preserve")
        if not lost:
            return Verdict(self.id, True, "final state preserves all rival update(s)")
        return Verdict(self.id, False, "; ".join(sorted(lost)))


def run_verifiers(
    trace: list[dict[str, Any]],
    state: dict[str, Any],
    invariants: list[str | dict[str, Any]],
) -> dict[str, Any]:
    """Run verifiers for ``invariants`` (ids or ``{"id": ...}`` mappings).

    Returns ``{"verdict": "PASS"|"FAIL", "results": [...]}``. Unknown ids
    raise :exc:`UnknownVerifierError`; malformed inputs raise
    :exc:`VerifierError` — neither is ever reported as a passing verdict.
    """
    results: list[dict[str, Any]] = []
    for invariant in invariants:
        invariant_id = invariant.get("id") if isinstance(invariant, dict) else invariant
        if not isinstance(invariant_id, str):
            raise VerifierError(f"invariant entries must name an id string, got {invariant!r}")
        verifier = get_verifier(invariant_id)
        verdict = verifier.check(trace, state)
        if not isinstance(verdict, Verdict) or verdict.invariant_id != invariant_id:
            raise VerifierError(
                f"verifier {invariant_id!r} returned a bad verdict object "
                f"({verdict!r}); refusing to report it"
            )
        results.append(verdict.to_dict())
    overall = "PASS" if all(row["passed"] for row in results) else "FAIL"
    return {"verdict": overall, "results": results}


def load_trace_events(path: str) -> list[dict[str, Any]]:
    """Parse a JSONL trace; structural problems raise TraceCorruptError."""
    try:
        text = open(path, encoding="utf-8").read()  # noqa: SIM115
    except OSError as exc:
        raise TraceCorruptError(f"cannot read trace {path}: {exc}") from exc
    events: list[dict[str, Any]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TraceCorruptError(f"line {lineno}: invalid JSON ({exc})") from exc
        if not isinstance(event, dict):
            raise TraceCorruptError(f"line {lineno}: event must be an object")
        events.append(event)
    if not events:
        raise TraceCorruptError("trace has no events")
    return events


def load_state_snapshot(path: str) -> dict[str, Any]:
    """Parse a state snapshot; structural problems raise TraceCorruptError."""
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except OSError as exc:
        raise TraceCorruptError(f"cannot read state {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TraceCorruptError(f"state {path} is not valid JSON ({exc})") from exc
    if not isinstance(state, dict):
        raise TraceCorruptError(f"state {path} must contain an object")
    return state
