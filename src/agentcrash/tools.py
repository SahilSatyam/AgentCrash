"""Deterministic synthetic tools: payments, docs, and versioned records.

Seeded virtual latencies come from the scenario YAML tool specs; every call
advances only the harness VirtualClock (ADR-003, ADR-006). Unknown tools and
unexpected args are rejected and recorded (``outcome: error``) — never
executed. ``read_doc`` returns seeded fixture bytes tagged
``untrusted_source`` (doc 11 S3); ``key_value_store`` does versioned
reads/writes. No wall-clock, no network, no model calls.
"""

from __future__ import annotations

from typing import Any

from agentcrash.clock import VirtualClock
from agentcrash.ledger import Ledger

CHARGE_CARD = "charge_card"
GET_TRANSACTION_STATUS = "get_transaction_status"
READ_DOC = "read_doc"
KEY_VALUE_STORE = "key_value_store"

FIRST_IDEMPOTENCY_KEY = "inv-9821-a3f"

#: Marker an obedient-scripted attacker instruction carries in S2 fixtures.
INSTRUCTION_MARKER = "INSTRUCTION:"

_IMPLEMENTED = (CHARGE_CARD, GET_TRANSACTION_STATUS)


def render_untrusted(text: str, source: str) -> str:
    """Wrap retrieved doc bytes in labeled delimiters (doc 11 S3).

    The trace keeps raw bytes for evidence; every CLI rendering goes
    through this helper so operators never mistake test content for
    product guidance.
    """
    return f'<untrusted_content source="{source}">\n{text}\n</untrusted_content>'


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
    if name == READ_DOC:
        unknown = sorted(set(args) - {"doc_id"})
        if unknown:
            return f"unknown arg(s) for read_doc: {', '.join(unknown)}"
        if not isinstance(args.get("doc_id"), str) or not args["doc_id"]:
            return "read_doc requires a non-empty string doc_id"
        return None
    if name == KEY_VALUE_STORE:
        unknown = sorted(set(args) - {"op", "record", "value", "expected_version"})
        if unknown:
            return f"unknown arg(s) for key_value_store: {', '.join(unknown)}"
        if args.get("op") not in ("read", "write"):
            return "key_value_store requires op 'read' or 'write'"
        if not isinstance(args.get("record"), str) or not args["record"]:
            return "key_value_store requires a non-empty string record"
        if args["op"] == "write":
            if not isinstance(args.get("value"), dict):
                return "key_value_store write requires an object value"
            expected = args.get("expected_version")
            if expected is not None and type(expected) is not int:
                return "key_value_store expected_version must be an integer or null"
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
        doc_store: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._specs = {spec["name"]: dict(spec) for spec in tool_specs}
        self._ledger = ledger
        self._rng = rng
        self._docs = dict(doc_store or {})

    @classmethod
    def from_scenario(
        cls,
        scenario: dict[str, Any],
        ledger: Ledger,
        rng: Any | None = None,
        doc_store: dict[str, dict[str, Any]] | None = None,
    ) -> ToolRegistry:
        """Build a registry from a parsed scenario.yaml dict."""
        return cls(scenario.get("tools", []), ledger, rng, doc_store)

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
        if name == READ_DOC:
            doc = self._docs.get(args["doc_id"])
            if doc is None:
                return {
                    "outcome": "error",
                    "latency_ms": 0,
                    "error": f"unknown doc_id {args['doc_id']!r}",
                    "data": {},
                }
            clock.sleep(latency_ms)
            text = str(doc.get("text", ""))
            self._ledger.record_doc(
                args["doc_id"], str(doc.get("fixture", "")), str(doc.get("label", ""))
            )
            outcome = "timeout" if latency_ms > timeout_ms else "success"
            return {
                "outcome": outcome,
                "latency_ms": latency_ms,
                "error": None,
                "data": {
                    "doc_id": args["doc_id"],
                    "text": text,
                    "bytes": len(text.encode("utf-8")),
                    "untrusted_source": True,
                    "fixture": str(doc.get("fixture", "")),
                    "label": str(doc.get("label", "")),
                    "has_instruction": INSTRUCTION_MARKER in text,
                },
            }
        if name == KEY_VALUE_STORE:
            name_ = args["record"]
            if args["op"] == "read":
                record = self._ledger.kv_read(name_)
                if record is None:
                    return {
                        "outcome": "error",
                        "latency_ms": 0,
                        "error": f"unknown record {name_!r}",
                        "data": {},
                    }
                clock.sleep(latency_ms)
                outcome = "timeout" if latency_ms > timeout_ms else "success"
                return {
                    "outcome": outcome,
                    "latency_ms": latency_ms,
                    "error": None,
                    "data": {
                        "record": name_,
                        "value": record["value"],
                        "version": record["version"],
                    },
                }
            written = self._ledger.kv_write(name_, args["value"], args.get("expected_version"))
            clock.sleep(latency_ms)
            outcome = "timeout" if latency_ms > timeout_ms else "success"
            return {
                "outcome": outcome,
                "latency_ms": latency_ms,
                "error": None,
                "data": {
                    "record": name_,
                    "value": dict(args["value"]),
                    "version": written["version"],
                    "expected_version": args.get("expected_version"),
                    "version_conflict": written["conflict"],
                },
            }
        return {
            "outcome": "error",
            "latency_ms": 0,
            "error": f"tool {name!r} is declared but not implemented in v0",
            "data": {},
        }
