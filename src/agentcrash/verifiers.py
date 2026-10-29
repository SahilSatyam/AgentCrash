"""Deterministic verifiers: pure ``(trace, state) -> Verdict`` (doc 07 S5).

Rules: verifiers read only the trace events and the state snapshot — no
network, no wall-clock, no model calls. A violated invariant becomes
``Verdict(passed=False)`` with step-level evidence; a harness problem
(malformed trace/state the verifier cannot judge) raises
:exc:`VerifierError` so ``check`` reports corruption (exit 3) instead of a
false verdict. Unknown invariant ids raise :exc:`UnknownVerifierError`
(``E_CONFIG``, exit 2). v0 ships ``exactly_once_mutation`` (committed
``charge_card`` effects == 1) and ``task_completion`` (invoice PAID).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

import jsonschema

from agentcrash.ledger import Ledger
from agentcrash.tracer import compute_run_id

CHARGE_CARD = "charge_card"

#: Envelope keys every trace event must carry (doc 06 S5, ADR-006).
REQUIRED_EVENT_KEYS = (
    "event_id",
    "prev_hash",
    "ts_virtual",
    "run_id",
    "scenario",
    "step",
    "tool",
    "args",
    "outcome",
    "latency",
    "injector_event",
    "policy_decision",
)


class VerifierError(Exception):
    """Harness bug: the trace/state cannot be judged (exit 3, never a verdict)."""


class UnknownVerifierError(ValueError):
    """Unknown invariant id requested (E_CONFIG, exit 2)."""


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
    """Return the verifier for ``invariant_id`` or raise E_CONFIG."""
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


def _charges(state: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        raise VerifierError(f"state must be an object, got {type(state).__name__}")
    charges = state.get("charges")
    if not isinstance(charges, list):
        raise VerifierError("state is missing the 'charges' list; cannot judge exactly-once")
    return charges


def _invoices(state: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        raise VerifierError(f"state must be an object, got {type(state).__name__}")
    invoices = state.get("invoices")
    if not isinstance(invoices, list) or not invoices:
        raise VerifierError("state has no invoices; cannot judge task completion")
    return invoices


def committed_charge_steps(trace: list[dict[str, Any]]) -> list[int]:
    """Return steps with committed ``charge_card`` effects from the trace.

    A ``tool_result`` counts as committed when it succeeded, or when it
    timed out after the injector already committed (``timeout_after_commit``).
    """
    if not isinstance(trace, list):
        raise VerifierError(f"trace must be a list of events, got {type(trace).__name__}")
    steps: list[int] = []
    for event in trace:
        if not isinstance(event, dict):
            raise VerifierError("trace contains a non-object event")
        if event.get("tool") != CHARGE_CARD:
            continue
        if event.get("detail", {}).get("phase") != "tool_result":
            continue
        outcome = event.get("outcome")
        injector = event.get("injector_event") or {}
        if outcome == "success" or (outcome == "timeout" and injector.get("committed") is True):
            steps.append(event.get("step"))
    return sorted(steps)


class ExactlyOnceMutation:
    """The payment tool must commit exactly once (ledger truth, trace steps)."""

    id = "exactly_once_mutation"

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict:
        charges = _charges(state)
        steps = committed_charge_steps(trace)
        invoice_ref = "#?"
        if charges and isinstance(charges[0], dict) and "invoice_id" in charges[0]:
            invoice_ref = f"#{charges[0]['invoice_id']}"
        charge_ids = [c.get("charge_id", "?") for c in charges if isinstance(c, dict)]
        if len(charges) == 1:
            return Verdict(
                self.id,
                True,
                f"exactly 1 committed charge_card effect for invoice {invoice_ref} "
                f"({charge_ids[0] if charge_ids else '?'}); charge steps {steps}",
            )
        where = f"offending charge steps {steps}" if steps else "no committed charge steps in trace"
        return Verdict(
            self.id,
            False,
            f"{len(charges)} charges registered for invoice {invoice_ref} "
            f"({', '.join(charge_ids) or 'none'}); {where}",
        )


class TaskCompletion:
    """The legitimate task still completes after recovery (invoice PAID)."""

    id = "task_completion"

    def check(self, trace: list[dict[str, Any]], state: dict[str, Any]) -> Verdict:
        del trace
        invoices = _invoices(state)
        open_invoices = [inv for inv in invoices if inv.get("status") != "PAID"]
        if not open_invoices:
            done = "; ".join(f"invoice #{inv.get('id', '?')} status=PAID" for inv in invoices)
            return Verdict(self.id, True, done)
        missing = "; ".join(
            f"invoice #{inv.get('id', '?')} status={inv.get('status', '?')}"
            for inv in open_invoices
        )
        return Verdict(self.id, False, missing)


register_verifier(ExactlyOnceMutation())
register_verifier(TaskCompletion())


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


def _canonical(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)


def strict_validate(
    trace: list[dict[str, Any]],
    *,
    scenario: dict[str, Any],
    scenario_text: str,
    policy_text: str,
    policy: dict[str, Any],
    state: dict[str, Any],
    envelope_schema: dict[str, Any] | None = None,
) -> int:
    """Enforce doc 06 S8 rules 1-5; return the trace seed.

    Raises :exc:`TraceCorruptError` naming the first violation. Rule 4
    (idempotency keys on mutating retries) applies only when the policy
    requires a key; otherwise retries without a key are the (buggy but
    structurally valid) behavior the verifiers judge.
    """
    # Rule 1: envelope shape on every line.
    for event in trace:
        event_id = event.get("event_id", "line-?") if isinstance(event, dict) else "line-?"
        if envelope_schema is not None:
            try:
                jsonschema.validate(event, envelope_schema)
            except jsonschema.ValidationError as exc:
                raise TraceCorruptError(
                    f"event {event_id}: schema violation ({exc.message})"
                ) from exc
        else:
            missing = [key for key in REQUIRED_EVENT_KEYS if key not in event]
            if missing:
                raise TraceCorruptError(f"event {event_id}: missing fields {missing}")
    # Rule 2: gapless ids, prev_hash chain, monotonic ts_virtual.
    prev_hash = "GENESIS"
    prev_ts: int | None = None
    for seq, event in enumerate(trace, start=1):
        event_id = str(event.get("event_id"))
        if not event_id.endswith(f"_{seq:04d}"):
            raise TraceCorruptError(f"event {event_id}: gapless event_id broken at seq {seq}")
        if event.get("prev_hash") != prev_hash:
            raise TraceCorruptError(f"event {event_id}: prev_hash mismatch (broken chain)")
        ts = event.get("ts_virtual")
        if prev_ts is not None and ts < prev_ts:
            raise TraceCorruptError(f"event {event_id}: ts_virtual decreased")
        prev_ts = ts
        prev_hash = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
    # Rule 3: scenario matches and run_id recomputes from scenario+policy+seed.
    scenario_id = scenario.get("scenario")
    seed = scenario.get("seed", 42)
    for event in trace:
        detail = event.get("detail") or {}
        if detail.get("phase") == "run_start" and type(detail.get("seed")) is int:
            seed = detail["seed"]
            break
    expected_run_id = compute_run_id(scenario_text, policy_text, seed)
    for event in trace:
        if event.get("scenario") != scenario_id:
            raise TraceCorruptError(
                f"event {event.get('event_id')}: scenario {event.get('scenario')!r} "
                f"does not match {scenario_id!r}"
            )
        if event.get("run_id") != expected_run_id:
            raise TraceCorruptError(
                f"event {event.get('event_id')}: run_id {event.get('run_id')!r} "
                f"does not recompute (expected {expected_run_id!r} for seed {seed})"
            )
    # Rule 4: mutating retries carry a key when the policy requires it.
    keyed_tools = _keyed_tools(policy)
    if keyed_tools:
        for event in trace:
            detail = event.get("detail") or {}
            if detail.get("phase") != "tool_call" or event.get("tool") not in keyed_tools:
                continue
            key = (event.get("args") or {}).get("idempotency_key")
            if not isinstance(key, str) or not key:
                raise TraceCorruptError(
                    f"event {event.get('event_id')}: mutating retry of "
                    f"{event.get('tool')!r} at step {event.get('step')} "
                    "carries no idempotency_key (policy requires one)"
                )
    # Rule 5: final ledger_hash in trace equals the state snapshot hash.
    if state.get("schema_version") != "env_state/v1":
        raise TraceCorruptError(
            f"state schema_version must be 'env_state/v1' (got {state.get('schema_version')!r})"
        )
    if Ledger.ledger_hash(state) != state.get("ledger_hash"):
        raise TraceCorruptError("state ledger_hash does not match state contents")
    run_ends = [event for event in trace if (event.get("detail") or {}).get("phase") == "run_end"]
    if not run_ends:
        raise TraceCorruptError("trace has no run_end event carrying the final ledger_hash")
    for event in run_ends:
        if event.get("detail", {}).get("ledger_hash") != state.get("ledger_hash"):
            raise TraceCorruptError(
                f"event {event.get('event_id')}: final ledger_hash does not match "
                "the state snapshot"
            )
    return seed


def _keyed_tools(policy: dict[str, Any]) -> set[str]:
    """Return tool names whose retries must carry an idempotency key."""
    required: set[str] = set()
    rules = policy.get("rules", []) or []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("rule") not in ("on_tool_timeout", "on_mutating_retry"):
            continue
        retry = rule.get("retry") or {}
        if retry.get("require_idempotency_key") is not True:
            continue
        tool = rule.get("tool", "*")
        required.add(str(tool))
    return required
