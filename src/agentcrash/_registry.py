"""Domain-free invariant registry: verdict type, plugin protocol, lookup.

Split out of :mod:`agentcrash.verifiers` so the installable core can
register and evaluate invariants without importing ODRL/SHACL stacks.
``verifiers.py`` re-exports every name defined here for backward
compatibility.
"""

from __future__ import annotations

from typing import Any, Protocol


class VerifierError(Exception):
    """Harness bug: the trace/state cannot be judged (exit 3, never a verdict)."""


class UnknownVerifierError(ValueError):
    """Unknown invariant id requested (``E_CONFIG``, exit 2)."""


class TraceCorruptError(ValueError):
    """Structural trace/state problem found by strict validation (exit 3)."""


class Verdict:
    """One invariant judgement with human-readable evidence."""

    def __init__(self, invariant_id: str, passed: bool, evidence: str) -> None:
        self.invariant_id = invariant_id
        self.passed = passed
        self.evidence = evidence

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable verdict row."""
        return {"id": self.invariant_id, "passed": self.passed, "evidence": self.evidence}


class Verifier(Protocol):
    """Plugin interface: pure check over ``(trace, state)``."""

    id: str

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict: ...


_REGISTRY: dict[str, Verifier] = {}


def register_verifier(verifier: Verifier) -> None:
    """Register a verifier under its invariant id."""
    _REGISTRY[verifier.id] = verifier


def get_verifier(invariant_id: str) -> Verifier:
    """Return the verifier for ``invariant_id`` or raise ``E_CONFIG``."""
    try:
        return _REGISTRY[invariant_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise UnknownVerifierError(
            f"unknown invariant {invariant_id!r}; known verifiers: {known}"
        ) from None


def verifier_ids() -> list[str]:
    """Return the registered invariant ids in registration order."""
    return list(_REGISTRY)
