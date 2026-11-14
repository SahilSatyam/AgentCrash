"""Domain-free verifiers: pure ``(trace, state) -> Verdict`` checks.

Core keeps only the checks that need no domain stack: the verifier
registry (re-exported from :mod:`agentcrash._registry`), the generic
write-freshness and lost-update invariants, and the trace/state loading
plus ``run_verifiers`` helpers. Domain-specific invariants live in host
or plugin packages, which import :exc:`VerifierError` and the base
checks from here and register their own ids. Core imports no domain
stack.

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

__all__ = [
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
    "tool_results",
    "verifier_ids",
]


def _events(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(trace, list):
        raise VerifierError(f"trace must be a list of events, got {type(trace).__name__}")
    for event in trace:
        if not isinstance(event, dict):
            raise VerifierError("trace contains a non-object event")
    return trace


def _is_sdk_event(event: dict[str, Any]) -> bool:
    """SDK events (``events.py``) carry ``kind`` + ``seq``; envelopes do not."""
    return "kind" in event and "seq" in event


def _step_label(event: dict[str, Any]) -> Any:
    """Evidence label: SDK ``seq`` (unique, ordered) or envelope ``step``."""
    if _is_sdk_event(event):
        return f"seq {event.get('seq')} ({event.get('step')})"
    return event.get("step")


def tool_results(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize executed tool calls from either trace format.

    Returns ``{"label", "tool", "op", "expected_version", "conflict"}`` per
    successful call:

    - ``TraceWriter`` envelope: ``detail.phase == "tool_result"`` and
      ``outcome != "error"``; ``op`` from ``args``; versions from
      ``detail.data``.
    - SDK event: ``kind == "tool"`` and ``status == "ok"``; ``op`` from
      ``inputs``; ``expected_version`` from ``inputs`` (else ``outputs``);
      the conflict flag from ``outputs.version_conflict`` or
      ``outputs.conflict`` (what :meth:`Ledger.kv_write` returns).
    """
    calls: list[dict[str, Any]] = []
    for event in _events(trace):
        if _is_sdk_event(event):
            if event.get("kind") != "tool" or event.get("status") != "ok":
                continue
            inputs = event.get("inputs") or {}
            outputs = event.get("outputs") or {}
            expected = inputs.get("expected_version", outputs.get("expected_version"))
            conflict = outputs.get("version_conflict", outputs.get("conflict"))
            op = inputs.get("op")
        else:
            if (event.get("detail") or {}).get("phase") != "tool_result":
                continue
            if event.get("outcome") == "error":
                continue
            data = (event.get("detail") or {}).get("data") or {}
            expected = data.get("expected_version")
            conflict = data.get("version_conflict")
            op = (event.get("args") or {}).get("op")
        calls.append(
            {
                "label": _step_label(event),
                "tool": event.get("tool"),
                "op": op,
                "expected_version": expected,
                "conflict": conflict,
            }
        )
    return calls


class FreshWriteOnly:
    """Every write to a versioned store carries a freshness proof.

    A call to one of ``tools`` counts as a write when its ``op`` is
    ``"write"``, or when it has no ``op`` at all (a dedicated write tool).
    A write passes only when it sent an ``expected_version`` and the store
    reported no version conflict. Stale or version-less writes fail, even
    though the store commits them. Works on SDK traces and ``TraceWriter``
    envelopes (see :func:`tool_results`).

    ``tools`` defaults to ``("key_value_store",)``. On the CLI, name your
    own: ``--invariant fresh_write_only:orders_db,crm.update``.
    """

    id = "fresh_write_only"

    #: Default versioned-store tools whose writes need a freshness proof.
    VERSIONED_TOOLS: tuple[str, ...] = ("key_value_store",)

    def __init__(
        self, tools: tuple[str, ...] | list[str] | None = None, *, id: str | None = None
    ) -> None:
        if tools is not None:
            self.VERSIONED_TOOLS = tuple(tools)
        if id is not None:
            self.id = id

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict:
        del state
        writes = [
            call
            for call in tool_results(trace)
            if call["tool"] in self.VERSIONED_TOOLS and call["op"] in (None, "write")
        ]
        tools = ", ".join(self.VERSIONED_TOOLS)
        if not writes:
            return Verdict(
                self.id, True, f"no writes to [{tools}] in trace; nothing to judge (vacuous)"
            )
        stale = [
            call["label"]
            for call in writes
            if call["conflict"] is True or call["expected_version"] is None
        ]
        if not stale:
            return Verdict(self.id, True, f"{len(writes)} write(s) carried the fresh version")
        return Verdict(self.id, False, f"stale write(s) without freshness proof at {stale}")


class NoLostUpdate:
    """The final state keeps every concurrent update.

    Rival writes come from ``concurrent_update`` faults in either format:
    an envelope ``injector_event`` (``fault``, ``record``, ``updates``) or
    an SDK ``fault`` event whose descriptor has ``kind``, ``key`` (or
    ``record``) and ``updates``. Every rival field must still hold the
    rival value in ``state["record_versions"]`` (an ``env_state/v1``
    snapshot, e.g. :meth:`Ledger.snapshot`).

    Traces without a rival writer pass vacuously. A trace with a rival
    writer but no ``record_versions`` in the state raises
    :exc:`VerifierError`: it cannot be judged, so it is never a PASS.
    """

    id = "no_lost_update"

    @staticmethod
    def _rival(event: dict[str, Any]) -> dict[str, Any] | None:
        if _is_sdk_event(event):
            fault = event.get("fault") if event.get("kind") == "fault" else None
            if isinstance(fault, dict) and fault.get("kind") == "concurrent_update":
                return {"record": fault.get("key", fault.get("record")), **fault}
            return None
        injector = event.get("injector_event") or {}
        if injector.get("fault") == "concurrent_update":
            return injector
        return None

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict:
        events = _events(trace)
        if not isinstance(state, dict):
            raise VerifierError(f"state must be an object, got {type(state).__name__}")
        rivals = [(event, rival) for event in events if (rival := self._rival(event))]
        if not rivals:
            return Verdict(self.id, True, "no concurrent writer in trace; nothing to preserve")
        if not isinstance(state.get("record_versions"), list):
            raise VerifierError(
                "no_lost_update needs a state snapshot with 'record_versions' "
                "(pass --state); the trace has a concurrent writer"
            )
        final = {
            rec.get("record"): rec.get("value", {})
            for rec in state["record_versions"]
            if isinstance(rec, dict)
        }
        lost: list[str] = []
        for event, rival in rivals:
            record = rival.get("record")
            current = final.get(record, {})
            for field, want in (rival.get("updates") or {}).items():
                if not isinstance(current, dict) or current.get(field) != want:
                    lost.append(f"{_step_label(event)}: {record}.{field} (rival {want!r} lost)")
        if not lost:
            return Verdict(
                self.id, True, f"final state preserves all {len(rivals)} rival update(s)"
            )
        return Verdict(self.id, False, "; ".join(lost))


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
