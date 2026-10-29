"""Fault injector: declarative schedule loaded from scenario.yaml.

Supported faults: ``timeout_after_commit`` (commit, then report TIMEOUT
with the simulated latency) and ``timeout_before_commit`` (report TIMEOUT
without committing). A fault fires either at a loop step (``at_step``,
1-based) or on the Nth matching tool call (``at_step: null`` with
``params`` ``tool``/``op``/``occurrence``); step indices are fragile under
live agents, so call-count triggers are preferred for new scenarios. An
optional ``params.tool`` constrains the fault to one tool.
Schema-valid but inert kinds (``poisoned_doc``, ``concurrent_update``)
parse and never fire as timeouts. Anything else raises
:class:`UnknownFaultError` so the CLI can refuse the run before writing
any trace. No wall-clock, no network, no model calls.
"""

from __future__ import annotations

from typing import Any

#: Fault kinds the injector recognizes (superset of what v0 executes).
KNOWN_FAULT_KINDS = (
    "timeout_after_commit",
    "timeout_before_commit",
    "poisoned_doc",
    "concurrent_update",
)

#: Kinds that change tool execution in v0.
TIMEOUT_KINDS = ("timeout_after_commit", "timeout_before_commit")


class UnknownFaultError(ValueError):
    """Raised when scenario.yaml declares a fault kind the injector cannot run."""


class FaultSchedule:
    """Declarative fault program for one run.

    ``faults`` are the ``faults`` entries from scenario.yaml
    (at_step/kind/params). ``rng`` is the run's seeded PRNG, threaded
    through for future probabilistic params; v0 firing is fully
    deterministic.
    """

    def __init__(self, faults: list[dict[str, Any]], rng: Any | None = None) -> None:
        self._faults = [dict(fault) for fault in faults]
        self._rng = rng

    @classmethod
    def from_scenario(cls, scenario: dict[str, Any], rng: Any | None = None) -> FaultSchedule:
        """Build a schedule, refusing unknown fault kinds (FR-07/FR-08)."""
        faults = scenario.get("faults", [])
        if not isinstance(faults, list):
            raise UnknownFaultError("scenario 'faults' must be a list")
        for fault in faults:
            kind = fault.get("kind") if isinstance(fault, dict) else None
            if kind not in KNOWN_FAULT_KINDS:
                raise UnknownFaultError(
                    f"unknown fault kind {kind!r} at step "
                    f"{fault.get('at_step') if isinstance(fault, dict) else '?'}; "
                    f"known kinds: {', '.join(KNOWN_FAULT_KINDS)}"
                )
        return cls(faults, rng)

    def fault_for(self, step: int, tool: str) -> dict[str, Any] | None:
        """Return the fault firing at ``step`` for ``tool``, if any."""
        for fault in self._faults:
            if fault.get("at_step") != step:
                continue
            params = fault.get("params", {}) or {}
            target = params.get("tool")
            if target is not None and target != tool:
                continue
            if fault.get("kind") not in TIMEOUT_KINDS:
                continue
            return fault
        return None

    @staticmethod
    def _call_matches(fault: dict[str, Any], tool: str, op: str | None, occurrence: int) -> bool:
        """True when a call-count fault fires on this tool call."""
        if fault.get("at_step") is not None:
            return False
        params = fault.get("params", {}) or {}
        target = params.get("tool")
        if target is not None and target != tool:
            return False
        want_op = params.get("op")
        if want_op is not None and want_op != op:
            return False
        want_n = params.get("occurrence", 1)
        return want_n == occurrence

    def fault_for_call(self, tool: str, op: str | None, occurrence: int) -> dict[str, Any] | None:
        """Return the timeout fault firing on this tool call, if any."""
        for fault in self._faults:
            if fault.get("kind") not in TIMEOUT_KINDS:
                continue
            if self._call_matches(fault, tool, op, occurrence):
                return fault
        return None

    def concurrent_for(self, step: int, tool: str) -> dict[str, Any] | None:
        """Return the ``concurrent_update`` fault firing at ``step``, if any.

        Unlike timeout faults this fault commits a rival write directly to
        the store (it never alters the tool result itself); the runner
        applies it before executing the agent's call at that step.
        """
        for fault in self._faults:
            if fault.get("at_step") != step:
                continue
            if fault.get("kind") != "concurrent_update":
                continue
            params = fault.get("params", {}) or {}
            target = params.get("tool")
            if target is not None and target != tool:
                continue
            return fault
        return None

    def concurrent_for_call(
        self, tool: str, op: str | None, occurrence: int
    ) -> dict[str, Any] | None:
        """Return the rival-write fault firing before this tool call, if any."""
        for fault in self._faults:
            if fault.get("kind") != "concurrent_update":
                continue
            if self._call_matches(fault, tool, op, occurrence):
                return fault
        return None
