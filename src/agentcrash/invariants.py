"""Domain-agnostic trace invariants for the AgentCrash core.

These checks reason only about the versioned SDK event schema
(``step`` / ``tool`` / ``kind`` / ``status``); they never import domain
stacks (ODRL, SHACL, DSPy). Host and extension packages register
domain-specific invariants alongside these via
:mod:`agentcrash._registry`.
"""

from __future__ import annotations

import json
from typing import Any

from agentcrash._registry import Verdict, VerifierError


class RequiredStep:
    """A named step must appear in the trace (missing tool call).

    Passes when at least one event carries ``step == <name>``;
    otherwise the workflow never executed the required step.
    """

    def __init__(self, step: str) -> None:
        self.step = step
        self.id = f"required_step:{step}"

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict:
        del state
        if not isinstance(trace, list):
            raise VerifierError(f"trace must be a list of events, got {type(trace).__name__}")
        hits = [
            event for event in trace if isinstance(event, dict) and event.get("step") == self.step
        ]
        if hits:
            return Verdict(self.id, True, f"step {self.step!r} present ({len(hits)} event(s))")
        return Verdict(self.id, False, f"step {self.step!r} missing from trace")


class DuplicateMutatingEffect:
    """No mutating tool effect may commit twice.

    Two successful ``tool`` executions of the same tool with identical
    inputs count as a duplicate effect, regardless of domain. Traces
    without repeated successful tool calls pass vacuously.
    """

    id = "duplicate_mutating_effect"

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict:
        del state
        if not isinstance(trace, list):
            raise VerifierError(f"trace must be a list of events, got {type(trace).__name__}")
        seen: dict[str, dict[str, Any]] = {}
        dupes: list[str] = []
        for event in trace:
            if not isinstance(event, dict):
                raise VerifierError("trace contains a non-object event")
            if event.get("kind") != "tool" or event.get("status") != "ok":
                continue
            tool = event.get("tool")
            if not tool:
                continue
            key = json.dumps(
                {"tool": tool, "inputs": event.get("inputs")}, sort_keys=True, default=str
            )
            first = seen.setdefault(key, event)
            if first is not event:
                dupes.append(f"{tool} at steps {first.get('step')} and {event.get('step')}")
        if not dupes:
            return Verdict(self.id, True, "no duplicate mutating tool effects")
        return Verdict(self.id, False, "duplicate effect(s): " + "; ".join(sorted(set(dupes))))
