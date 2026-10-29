"""Deterministic synthetic tools: charge_card + get_transaction_status.

Seeded virtual latencies come from the scenario YAML tool specs; every call
advances only the harness VirtualClock (ADR-003, ADR-006). Unknown tools and
unexpected args are rejected and recorded (``outcome: error``) — never
executed. No wall-clock, no network, no model calls.
"""

from __future__ import annotations

from typing import Any

from agentcrash.clock import VirtualClock
from agentcrash.ledger import Ledger

CHARGE_CARD = "charge_card"
GET_TRANSACTION_STATUS = "get_transaction_status"

FIRST_IDEMPOTENCY_KEY = "inv-9821-a3f"

_IMPLEMENTED = (CHARGE_CARD, GET_TRANSACTION_STATUS)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_args(name: str, args: dict[str, Any]) -> str | None:
    """Return an error string for bad args, or None when valid."""
    if name == CHARGE_CARD:
        allowed = {"invoice_id", "amount", "idempotency_key"}
        unknown = sorted(set(args) - allowed)
        if unknown:
            return f"unknown arg(s) for charge_card: {', '.join(unknown)}"
        if type(args.get("invoice_id")) is not int:
            return "charge_card requires integer invoice_id"
        if "amount" not in args or not _is_number(args["amount"]):
            return "charge_card requires numeric amount"
        key = args.get("idempotency_key")
        if key is not None and not isinstance(key, str):
            return "charge_card idempotency_key must be a string or null"
        return None
    if name == GET_TRANSACTION_STATUS:
        unknown = sorted(set(args) - {"invoice_id"})
        if unknown:
            return f"unknown arg(s) for get_transaction_status: {', '.join(unknown)}"
        if type(args.get("invoice_id")) is not int:
            return "get_transaction_status requires integer invoice_id"
        return None
    return f"tool {name!r} is not implemented in v0"


class ToolRegistry:
    """Execute synthetic tools against a Ledger on virtual time.

    ``tool_specs`` are the ``tools`` entries from scenario.yaml
    (name/latency_ms/timeout_ms/mutating). ``rng`` is the run's seeded
    ``random.Random`` threaded through for future probabilistic params;
    nominal latencies stay exact per ADR-006.
    """

    def __init__(
        self,
        tool_specs: list[dict[str, Any]],
        ledger: Ledger,
        rng: Any | None = None,
    ) -> None:
        self._specs = {spec["name"]: dict(spec) for spec in tool_specs}
        self._ledger = ledger
        self._rng = rng

    @classmethod
    def from_scenario(
        cls, scenario: dict[str, Any], ledger: Ledger, rng: Any | None = None
    ) -> ToolRegistry:
        """Build a registry from a parsed scenario.yaml dict."""
        return cls(scenario.get("tools", []), ledger, rng)

    @property
    def tool_names(self) -> list[str]:
        """Sorted names declared by the scenario."""
        return sorted(self._specs)

    def spec(self, name: str) -> dict[str, Any] | None:
        """Return the scenario spec for ``name``, or None when undeclared."""
        return self._specs.get(name)

    def call(self, name: str, args: dict[str, Any], clock: VirtualClock) -> dict[str, Any]:
        """Execute ``name``; always returns a result dict, never raises.

        Rejections (unknown tool, bad args, unimplemented tool) return
        ``outcome: error`` with ``latency_ms: 0``: the clock is untouched
        and the ledger is unchanged.
        """
        spec = self._specs.get(name)
        if spec is None:
            return {
                "outcome": "error",
                "latency_ms": 0,
                "error": f"unknown tool {name!r}",
                "data": {},
            }
        if not isinstance(args, dict):
            return {
                "outcome": "error",
                "latency_ms": 0,
                "error": f"args for {name!r} must be an object",
                "data": {},
            }
        problem = _validate_args(name, args)
        if problem is not None:
            return {"outcome": "error", "latency_ms": 0, "error": problem, "data": {}}
        latency_ms = int(spec["latency_ms"])
        timeout_ms = int(spec["timeout_ms"])
        if name == CHARGE_CARD:
            charge = self._ledger.charge(
                args["invoice_id"], args["amount"], args.get("idempotency_key")
            )
            clock.sleep(latency_ms)
            outcome = "timeout" if latency_ms > timeout_ms else "success"
            return {
                "outcome": outcome,
                "latency_ms": latency_ms,
                "error": None,
                "data": {
                    "charge_id": charge["charge_id"],
                    "invoice_id": charge["invoice_id"],
                    "idempotency_key": charge["idempotency_key"],
                },
            }
        if name == GET_TRANSACTION_STATUS:
            clock.sleep(latency_ms)
            invoice = self._ledger.invoices.get(args["invoice_id"])
            status = invoice["status"] if invoice is not None else "UNKNOWN"
            amount = invoice["amount"] if invoice is not None else 0
            outcome = "timeout" if latency_ms > timeout_ms else "success"
            return {
                "outcome": outcome,
                "latency_ms": latency_ms,
                "error": None,
                "data": {
                    "invoice_id": args["invoice_id"],
                    "status": status,
                    "amount": amount,
                },
            }
        return {
            "outcome": "error",
            "latency_ms": 0,
            "error": f"tool {name!r} is declared but not implemented in v0",
            "data": {},
        }
