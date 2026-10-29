"""Deterministic synthetic tools for the NL→ODRL converter loop.

Seeded virtual latencies come from the scenario YAML tool specs; every call
advances only the harness VirtualClock (ADR-003, ADR-006). Unknown tools and
unexpected args are rejected and recorded (``outcome: error``) — never
executed. ``parse_policy`` returns fixture-backed NL→ODRL (sloppy vs strict
mode) tagged ``untrusted_source``; ``validate_odrl`` checks the frozen ODRL
subset via :mod:`agentcrash.odrl`; ``save_odrl`` and ``key_value_store`` do
versioned reads/writes on the record store. No wall-clock, no network,
no model calls.
"""

from __future__ import annotations

from typing import Any

from agentcrash.clock import VirtualClock
from agentcrash.ledger import Ledger
from agentcrash.odrl import ODRL_SUBSET_ID, validate_odrl_subset

KEY_VALUE_STORE = "key_value_store"
PARSE_POLICY = "parse_policy"
VALIDATE_ODRL = "validate_odrl"
SAVE_ODRL = "save_odrl"

#: Marker an attacker instruction carries in poisoned NL fixtures.
INSTRUCTION_MARKER = "INSTRUCTION:"


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
    if name == PARSE_POLICY:
        unknown = sorted(set(args) - {"doc_id", "mode"})
        if unknown:
            return f"unknown arg(s) for parse_policy: {', '.join(unknown)}"
        if not isinstance(args.get("doc_id"), str) or not args["doc_id"]:
            return "parse_policy requires a non-empty string doc_id"
        if args.get("mode", "sloppy") not in ("sloppy", "strict"):
            return "parse_policy mode must be 'sloppy' or 'strict'"
        return None
    if name == VALIDATE_ODRL:
        unknown = sorted(set(args) - {"odrl"})
        if unknown:
            return f"unknown arg(s) for validate_odrl: {', '.join(unknown)}"
        if not isinstance(args.get("odrl"), dict):
            return "validate_odrl requires an object odrl"
        return None
    if name in (KEY_VALUE_STORE, SAVE_ODRL):
        unknown = sorted(set(args) - {"op", "record", "value", "expected_version"})
        if unknown:
            return f"unknown arg(s) for {name}: {', '.join(unknown)}"
        if args.get("op") not in ("read", "write"):
            return f"{name} requires op 'read' or 'write'"
        if not isinstance(args.get("record"), str) or not args["record"]:
            return f"{name} requires a non-empty string record"
        if args["op"] == "write":
            if not isinstance(args.get("value"), dict):
                return f"{name} write requires an object value"
            expected = args.get("expected_version")
            if expected is not None and type(expected) is not int:
                return f"{name} expected_version must be an integer or null"
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
        nl_store: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._specs = {spec["name"]: dict(spec) for spec in tool_specs}
        self._ledger = ledger
        self._rng = rng
        self._docs = dict(doc_store or {})
        self._nl_docs = dict(nl_store or {})

    @classmethod
    def from_scenario(
        cls,
        scenario: dict[str, Any],
        ledger: Ledger,
        rng: Any | None = None,
        doc_store: dict[str, dict[str, Any]] | None = None,
        nl_store: dict[str, dict[str, Any]] | None = None,
    ) -> ToolRegistry:
        """Build a registry from a parsed scenario.yaml dict."""
        return cls(scenario.get("tools", []), ledger, rng, doc_store, nl_store)

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
        if name == PARSE_POLICY:
            entry = self._nl_docs.get(args["doc_id"])
            if entry is None:
                return {
                    "outcome": "error",
                    "latency_ms": 0,
                    "error": f"unknown doc_id {args['doc_id']!r}",
                    "data": {},
                }
            clock.sleep(latency_ms)
            mode = str(args.get("mode", "sloppy"))
            odrl = dict(entry["expected_odrl"] if mode == "strict" else entry["naive_odrl"])
            text = str(entry.get("text", ""))
            self._ledger.record_doc(
                args["doc_id"], str(entry.get("fixture", "")), str(entry.get("label", ""))
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
                    "fixture": str(entry.get("fixture", "")),
                    "label": str(entry.get("label", "")),
                    "has_instruction": INSTRUCTION_MARKER in text,
                    "mode": mode,
                    "odrl": odrl,
                },
            }
        if name == VALIDATE_ODRL:
            valid, errors = validate_odrl_subset(args["odrl"])
            clock.sleep(latency_ms)
            outcome = "timeout" if latency_ms > timeout_ms else "success"
            return {
                "outcome": outcome,
                "latency_ms": latency_ms,
                "error": None,
                "data": {
                    "valid": valid,
                    "errors": errors,
                    "checked": ODRL_SUBSET_ID,
                },
            }
        if name in (KEY_VALUE_STORE, SAVE_ODRL):
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
