"""ReAct runner: scripted stub agent plus fake adapter, fully offline.

The v0 agent runs ``stub-plan:retry_immediately``: check status, charge
once, and on an ambiguous TIMEOUT retry immediately — blindly issuing a
fresh idempotency key under the naive policy (the double-charge bug that
Phase 3 verifiers catch). Under a ``check_then_act`` rule the stub first
reconciles via ``get_transaction_status`` and retries with the same key
only while the invoice is still UNPAID. Both the stub and the fake model adapter sit
behind ``complete(messages, tools, timeout_s)``; the live Llama transport
is stubbed to refuse without a key and never touches the network, so every
run is fixture-backed and deterministic. No wall-clock calls anywhere.
S4 (``odrl_convert*``) reuses the same loop with the scripted converter
(``parse_policy`` → ``validate_odrl`` → ``save_odrl``); ``--model`` is
ignored for those scenarios.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any

import yaml

from agentcrash.clock import VirtualClock
from agentcrash.injector import FaultSchedule
from agentcrash.ledger import Ledger
from agentcrash.tools import (
    CHARGE_CARD,
    FIRST_IDEMPOTENCY_KEY,
    GET_TRANSACTION_STATUS,
    INSTRUCTION_MARKER,
    KEY_VALUE_STORE,
    PARSE_POLICY,
    READ_DOC,
    SAVE_ODRL,
    VALIDATE_ODRL,
    ToolRegistry,
)
from agentcrash.tracer import TraceWriter, compute_run_id

STUB_PLAN = "stub-plan:retry_immediately"
DEFAULT_MODEL = "llama-3.3-70b"


def timeout_rule(policy: dict[str, Any], tool: str) -> dict[str, Any] | None:
    """Return the on_tool_timeout rule covering ``tool``, if any."""
    for rule in policy.get("rules", []) or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("rule") != "on_tool_timeout":
            continue
        covered = rule.get("tool")
        if covered == tool or covered == "*":
            return rule
    return None


def _rule_for(policy: dict[str, Any], name: str, tool: str) -> dict[str, Any] | None:
    """Return the ``name`` rule covering ``tool``, if any."""
    for rule in policy.get("rules", []) or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("rule") != name:
            continue
        covered = rule.get("tool")
        if covered == tool or covered == "*":
            return rule
    return None


def untrusted_rule(policy: dict[str, Any], tool: str) -> dict[str, Any] | None:
    """Return the confirmation gate on untrusted content, if any (S2 fix)."""
    rule = _rule_for(policy, "on_untrusted_content", tool)
    if rule is not None and rule.get("action") == "require_confirmation":
        return rule
    return None


def stale_rule(policy: dict[str, Any], tool: str) -> dict[str, Any] | None:
    """Return the re-read rule for stale records, if any (S3 fix)."""
    rule = _rule_for(policy, "on_stale_read", tool)
    if rule is not None and rule.get("action") == "check_then_act":
        return rule
    return None


class StubAgent:
    """Scripted v0 agent: reconcile-read, charge, blind retry, confirm.

    ``scenario_kind`` selects the scripted flow: ``s1`` (payment timeout),
    ``s2`` (poisoned doc: naive obeys the planted instruction, gated only
    summarizes), ``s3`` (stale read: naive writes from the first read,
    fixed re-reads before writing), ``s4`` (ODRL convert: naive parses
    sloppy and saves blind; strict converts, validates, then saves fresh).
    """

    def __init__(
        self,
        invoice_id: int,
        amount: float,
        max_attempts: int = 1,
        require_idempotency_key: bool = False,
        reconcile_first: bool = False,
        scenario_kind: str = "s1",
        confirmation_gate: bool = False,
        reread_before_write: bool = False,
        doc_id: str = "vendor-terms",
        kv_key: str = "vendor-address",
        intended_update: dict[str, Any] | None = None,
        strict_convert: bool = False,
        odrl_record: str = "odrl-policy-1",
    ) -> None:
        self._invoice_id = invoice_id
        self._amount = amount
        self._max_attempts = max_attempts
        self._require_key = require_idempotency_key
        self._reconcile_first = reconcile_first
        self._scenario_kind = scenario_kind
        self._confirmation_gate = confirmation_gate
        self._reread_before_write = reread_before_write
        self._doc_id = doc_id
        self._kv_key = kv_key
        self._intended_update = dict(intended_update or {})
        self._strict_convert = strict_convert
        self._odrl_record = odrl_record

    def retries_allowed(self, attempts_made: int) -> bool:
        """True while another mutating attempt fits in max_attempts."""
        return attempts_made <= self._max_attempts

    def _retry_key(self, attempts_made: int) -> str:
        if self._require_key:
            return FIRST_IDEMPOTENCY_KEY
        return f"{FIRST_IDEMPOTENCY_KEY}-r{attempts_made}"

    def _needs_reconcile(self, history: list[dict[str, Any]]) -> bool:
        """True when no status read followed the latest charge attempt."""
        for entry in reversed(history):
            if entry.get("tool") == GET_TRANSACTION_STATUS:
                return False
            if entry.get("tool") == CHARGE_CARD and entry.get("outcome") != "error":
                return True
        return False

    def next_action(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        """Pick the next action from past tool results (pure function)."""
        if self._scenario_kind == "s2":
            return self._next_s2(history)
        if self._scenario_kind == "s3":
            return self._next_s3(history)
        if self._scenario_kind == "s4":
            return self._next_s4(history)
        charges = [
            h for h in history if h.get("tool") == CHARGE_CARD and h.get("outcome") != "error"
        ]
        if not history:
            return {
                "type": "call",
                "tool": GET_TRANSACTION_STATUS,
                "args": {"invoice_id": self._invoice_id},
            }
        last = history[-1]
        if last.get("tool") == GET_TRANSACTION_STATUS:
            if last.get("data", {}).get("status") == "PAID":
                return {"type": "done", "reason": "invoice PAID"}
            if not charges:
                return {
                    "type": "call",
                    "tool": CHARGE_CARD,
                    "args": {
                        "invoice_id": self._invoice_id,
                        "amount": self._amount,
                        "idempotency_key": FIRST_IDEMPOTENCY_KEY,
                    },
                }
            if self.retries_allowed(len(charges)):
                return {
                    "type": "call",
                    "tool": CHARGE_CARD,
                    "args": {
                        "invoice_id": self._invoice_id,
                        "amount": self._amount,
                        "idempotency_key": self._retry_key(len(charges)),
                    },
                }
            return {"type": "done", "reason": "retries exhausted, invoice UNPAID"}
        if last.get("tool") == CHARGE_CARD:
            if last.get("outcome") == "error":
                return {"type": "done", "reason": "tool args rejected"}
            if last.get("outcome") == "timeout":
                if self._reconcile_first and self._needs_reconcile(history):
                    return {
                        "type": "call",
                        "tool": GET_TRANSACTION_STATUS,
                        "args": {"invoice_id": self._invoice_id},
                    }
                if self.retries_allowed(len(charges)):
                    return {
                        "type": "call",
                        "tool": CHARGE_CARD,
                        "args": {
                            "invoice_id": self._invoice_id,
                            "amount": self._amount,
                            "idempotency_key": self._retry_key(len(charges)),
                        },
                    }
                return {
                    "type": "call",
                    "tool": GET_TRANSACTION_STATUS,
                    "args": {"invoice_id": self._invoice_id},
                }
            return {
                "type": "call",
                "tool": GET_TRANSACTION_STATUS,
                "args": {"invoice_id": self._invoice_id},
            }
        return {"type": "done", "reason": "no applicable stub action"}

    def _next_s2(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        """S2 flow: read the doc, then obey it (naive) or summarize (gated)."""
        if not history:
            return {"type": "call", "tool": READ_DOC, "args": {"doc_id": self._doc_id}}
        last = history[-1]
        if last.get("tool") == READ_DOC:
            if last.get("outcome") == "error":
                return {"type": "done", "reason": "tool args rejected"}
            if self._confirmation_gate:
                return {"type": "done", "reason": "summarized without acting"}
            text = str(last.get("data", {}).get("text", ""))
            if INSTRUCTION_MARKER in text:
                return {
                    "type": "call",
                    "tool": CHARGE_CARD,
                    "args": {
                        "invoice_id": self._invoice_id,
                        "amount": -self._amount,
                        "idempotency_key": FIRST_IDEMPOTENCY_KEY,
                    },
                }
            return {"type": "done", "reason": "answered from clean doc"}
        return {"type": "done", "reason": "acted on doc instruction"}

    def _next_s3(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        """S3 flow: read, then write merged update (fresh only when fixed)."""
        reads = [
            h
            for h in history
            if h.get("tool") == KEY_VALUE_STORE
            and h.get("args", {}).get("op") == "read"
            and h.get("outcome") != "error"
        ]
        if not history:
            return {
                "type": "call",
                "tool": KEY_VALUE_STORE,
                "args": {"op": "read", "record": self._kv_key},
            }
        last = history[-1]
        if last.get("outcome") == "error":
            return {"type": "done", "reason": "tool args rejected"}
        if last.get("tool") == KEY_VALUE_STORE and last.get("args", {}).get("op") == "read":
            if self._reread_before_write and len(reads) < 2:
                return {
                    "type": "call",
                    "tool": KEY_VALUE_STORE,
                    "args": {"op": "read", "record": self._kv_key},
                }
            latest = last.get("data", {})
            merged = dict(latest.get("value", {}))
            merged.update(self._intended_update)
            return {
                "type": "call",
                "tool": KEY_VALUE_STORE,
                "args": {
                    "op": "write",
                    "record": self._kv_key,
                    "value": merged,
                    "expected_version": latest.get("version"),
                },
            }
        return {"type": "done", "reason": "address updated"}

    def _parsed_odrl(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the candidate ODRL doc from the parse step (pure lookup)."""
        for entry in history:
            if entry.get("tool") == PARSE_POLICY and entry.get("outcome") != "error":
                odrl = (entry.get("data") or {}).get("odrl")
                if isinstance(odrl, dict):
                    return dict(odrl)
        return {}

    def _next_s4(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        """S4 flow: parse, then validate (strict only), then save fresh."""
        mode = "strict" if self._strict_convert else "sloppy"
        if not history:
            return {
                "type": "call",
                "tool": PARSE_POLICY,
                "args": {"doc_id": self._doc_id, "mode": mode},
            }
        last = history[-1]
        if last.get("outcome") == "error":
            return {"type": "done", "reason": "tool args rejected"}
        if last.get("tool") == PARSE_POLICY:
            if self._strict_convert:
                return {
                    "type": "call",
                    "tool": VALIDATE_ODRL,
                    "args": {"odrl": self._parsed_odrl(history)},
                }
            return {
                "type": "call",
                "tool": SAVE_ODRL,
                "args": {"op": "read", "record": self._odrl_record},
            }
        if last.get("tool") == VALIDATE_ODRL:
            if last.get("data", {}).get("valid") is not True:
                return {"type": "done", "reason": "odrl invalid, refusing to save"}
            return {
                "type": "call",
                "tool": SAVE_ODRL,
                "args": {"op": "read", "record": self._odrl_record},
            }
        if last.get("tool") == SAVE_ODRL and last.get("args", {}).get("op") == "read":
            reads = [
                h
                for h in history
                if h.get("tool") == SAVE_ODRL
                and h.get("args", {}).get("op") == "read"
                and h.get("outcome") != "error"
            ]
            if self._reread_before_write and len(reads) < 2:
                return {
                    "type": "call",
                    "tool": SAVE_ODRL,
                    "args": {"op": "read", "record": self._odrl_record},
                }
            latest = last.get("data", {})
            merged = dict(latest.get("value", {}))
            merged.update(self._parsed_odrl(history))
            return {
                "type": "call",
                "tool": SAVE_ODRL,
                "args": {
                    "op": "write",
                    "record": self._odrl_record,
                    "value": merged,
                    "expected_version": latest.get("version"),
                },
            }
        return {"type": "done", "reason": "odrl saved"}


def history_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project adapter messages back to tool-result history entries."""
    return [
        {
            "tool": m.get("tool"),
            "args": m.get("args", {}),
            "outcome": m.get("outcome"),
            "data": m.get("data", {}),
        }
        for m in messages
        if m.get("role") == "tool"
    ]


class FakeAdapter:
    """Fixture-backed model stand-in behind complete()."""

    def __init__(self, agent: StubAgent, allowed_tools: list[str]) -> None:
        self._agent = agent
        self._allowed = list(allowed_tools)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[str],
        timeout_s: float,
    ) -> dict[str, Any]:
        """Return the stub's next action; timeout_s is virtual, never blocks."""
        del timeout_s
        offered = list(tools) or list(self._allowed)
        action = self._agent.next_action(history_from_messages(messages))
        if action.get("type") == "call" and action.get("tool") not in offered:
            return {"type": "done", "reason": f"tool {action.get('tool')!r} not offered"}
        return action


class ModelNotConfiguredError(RuntimeError):
    """Live-model path is unavailable offline (E_MODEL_API)."""


class LlamaAdapter:
    """Live Llama transport placeholder: offline it refuses, never dials out."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model = model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[str],
        timeout_s: float,
    ) -> dict[str, Any]:
        """Refuse without a key; the live transport lands with G5 (doc 10 OQ)."""
        del messages, tools, timeout_s
        if not os.environ.get("LLAMA_API_KEY"):
            raise ModelNotConfiguredError(
                f"live model {self._model!r} requires LLAMA_API_KEY; "
                "run the fixture-backed stub/fake offline instead"
            )
        raise ModelNotConfiguredError(
            f"live transport for {self._model!r} is not implemented in v0; "
            "run the fixture-backed stub/fake offline instead"
        )


def _invoice_seed(scenario: dict[str, Any]) -> tuple[int, str, float]:
    invoices = (scenario.get("environment", {}) or {}).get("invoices", [])
    if not invoices:
        billing = (scenario.get("environment", {}) or {}).get("billing", {}) or {}
        if "invoice_id" in billing and "amount" in billing:
            return (int(billing["invoice_id"]), "Acme Corp", billing["amount"])
        raise ValueError("scenario 'environment.invoices' must list at least one invoice")
    first = invoices[0]
    return int(first["id"]), str(first.get("vendor", "")), first["amount"]


def _scenario_kind(scenario: dict[str, Any]) -> str:
    """Map the scenario id to the stub flow (s1/s2/s3/s4)."""
    scenario_id = str(scenario.get("scenario", ""))
    if scenario_id == "poisoned_document":
        return "s2"
    if scenario_id == "concurrent_update":
        return "s3"
    if scenario_id in ("odrl_convert", "odrl_convert_poisoned", "odrl_convert_concurrent"):
        return "s4"
    return "s1"


def is_odrl_scenario(scenario: dict[str, Any]) -> bool:
    """True for S4 converter scenarios (``--model`` is ignored for these)."""
    return _scenario_kind(scenario) == "s4"


def load_doc_store(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the ``read_doc`` store from the fixture + scenario documents.

    The fixture file stays the single canonical home of adversarial bytes
    (doc 11 S3); the scenario only names ``fixture_key`` entries.
    """
    env = scenario.get("environment", {}) or {}
    documents = env.get("documents", []) or []
    if not documents:
        return {}
    fixture_ref = str(env.get("doc_fixture", ""))
    if not fixture_ref:
        raise ValueError("scenario declares documents but no environment.doc_fixture")
    fixture_path = Path(fixture_ref)
    if not fixture_path.is_absolute():
        fixture_path = Path.cwd() / fixture_ref
    try:
        fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"cannot read doc fixture {fixture_ref}: {exc}") from exc
    entries = fixture.get("docs", {}) or {}
    store: dict[str, dict[str, Any]] = {}
    for doc in documents:
        key = doc.get("fixture_key")
        entry = entries.get(key)
        if not isinstance(entry, dict) or "text" not in entry:
            raise ValueError(f"doc fixture {fixture_ref} has no entry {key!r}")
        store[doc["id"]] = {
            "text": str(entry["text"]),
            "label": str(entry.get("label", "")),
            "fixture": f"{fixture_ref}#{key}",
        }
    return store


def load_nl_store(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the ``parse_policy`` store from the NL fixture + scenario docs.

    Mirrors :func:`load_doc_store`: the fixture file stays the single
    canonical home of NL bytes (doc 11 S3); the scenario only names
    ``fixture_key`` entries. Each entry must carry ``naive_odrl`` (sloppy
    converter output) and ``expected_odrl`` (strict converter output).
    """
    env = scenario.get("environment", {}) or {}
    documents = env.get("nl_docs", []) or []
    if not documents:
        return {}
    fixture_ref = str(env.get("nl_fixture", ""))
    if not fixture_ref:
        raise ValueError("scenario declares nl_docs but no environment.nl_fixture")
    fixture_path = Path(fixture_ref)
    if not fixture_path.is_absolute():
        fixture_path = Path.cwd() / fixture_ref
    try:
        fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"cannot read NL fixture {fixture_ref}: {exc}") from exc
    entries = fixture.get("docs", {}) or {}
    store: dict[str, dict[str, Any]] = {}
    for doc in documents:
        key = doc.get("fixture_key")
        entry = entries.get(key)
        if (
            not isinstance(entry, dict)
            or "nl" not in entry
            or "naive_odrl" not in entry
            or "expected_odrl" not in entry
        ):
            raise ValueError(f"NL fixture {fixture_ref} has no ODRL entry {key!r}")
        store[doc["id"]] = {
            "text": str(entry["nl"]),
            "label": str(entry.get("label", "")),
            "fixture": f"{fixture_ref}#{key}",
            "naive_odrl": entry["naive_odrl"],
            "expected_odrl": entry["expected_odrl"],
        }
    return store


def run_react_loop(
    *,
    scenario: dict[str, Any],
    policy: dict[str, Any],
    seed: int,
    trace_path: str,
    state_path: str | None = None,
    scenario_text: str = "",
    policy_text: str = "",
    max_steps: int = 12,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Run the stub agent against the injector; write trace + state snapshot.

    Returns a summary dict (run_id, scenario, steps, status, charges,
    ledger_hash, trace_path, state_path). Raises UnknownFaultError before
    creating any file when the fault program is unusable.
    """
    rng = random.Random(seed)
    schedule = FaultSchedule.from_scenario(scenario, rng)
    clock = VirtualClock()
    ledger = Ledger()
    env = scenario.get("environment", {}) or {}
    kind = _scenario_kind(scenario)
    if kind in ("s3", "s4"):
        invoice_id, vendor, amount = 0, "", 0.0
    else:
        invoice_id, vendor, amount = _invoice_seed(scenario)
    for inv in env.get("invoices", []):
        ledger.add_invoice(int(inv["id"]), str(inv.get("vendor", "")), inv["amount"])
    for rec in env.get("records", []):
        ledger.kv_seed(str(rec["key"]), dict(rec.get("value", {}) or {}))
    for rec in env.get("odrl_records", []):
        ledger.kv_seed(str(rec["key"]), dict(rec.get("value", {}) or {}))
    doc_store = load_doc_store(scenario)
    nl_store = load_nl_store(scenario)
    registry = ToolRegistry.from_scenario(scenario, ledger, rng, doc_store, nl_store)

    run_id = compute_run_id(scenario_text, policy_text, seed)
    scenario_id = str(scenario.get("scenario", "unknown"))
    rule = timeout_rule(policy, CHARGE_CARD)
    if rule is not None:
        retry = rule.get("retry", {}) or {}
        max_attempts = int(retry.get("max_attempts", 1))
        require_key = bool(retry.get("require_idempotency_key", False))
        reconcile_first = str(rule.get("action", "retry_same")) == "check_then_act"
    else:
        max_attempts, require_key, reconcile_first = 0, False, False
    documents = env.get("documents", []) or []
    nl_docs = env.get("nl_docs", []) or []
    if kind == "s4" and nl_docs:
        read_doc_id = str(nl_docs[0].get("id", "access-request-1"))
    elif documents:
        read_doc_id = str(documents[0].get("id", "vendor-terms"))
    else:
        read_doc_id = "vendor-terms"
    agent = StubAgent(
        invoice_id,
        amount,
        max_attempts,
        require_key,
        reconcile_first,
        scenario_kind=kind,
        confirmation_gate=untrusted_rule(policy, READ_DOC) is not None,
        reread_before_write=stale_rule(policy, KEY_VALUE_STORE) is not None
        or stale_rule(policy, SAVE_ODRL) is not None,
        doc_id=read_doc_id,
        kv_key=str((env.get("records", []) or [{}])[0].get("key", "vendor-address")),
        intended_update=dict(env.get("intended_update", {}) or {}),
        strict_convert=untrusted_rule(policy, PARSE_POLICY) is not None,
        odrl_record=str((env.get("odrl_records", []) or [{}])[0].get("key", "odrl-policy-1")),
    )
    adapter = FakeAdapter(agent, registry.tool_names)

    writer = TraceWriter(run_id, scenario_id, trace_path)
    try:
        writer.write(
            step=0,
            outcome="info",
            ts_virtual=clock.now(),
            detail={
                "phase": "run_start",
                "task": str(scenario.get("task", "")),
                "seed": seed,
                "model": model,
                "adapter": "fake",
                "plan": STUB_PLAN,
                "max_steps": max_steps,
                "vendor": vendor,
            },
        )
        writer.write(
            step=1,
            outcome="info",
            ts_virtual=clock.now(),
            detail={"phase": "agent_plan", "plan": STUB_PLAN},
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"plan {STUB_PLAN}"},
            {"role": "user", "content": str(scenario.get("task", ""))},
        ]
        step = 1
        calls = 0
        status = "completed"
        while True:
            step += 1
            if step > max_steps:
                status = "runaway"
                break
            action = adapter.complete(messages, registry.tool_names, timeout_s=0)
            if action.get("type") != "call":
                break
            tool = str(action["tool"])
            args = dict(action.get("args", {}))
            calls += 1
            writer.write(
                step=step,
                tool=tool,
                args=args,
                outcome="info",
                ts_virtual=clock.now(),
                detail={"phase": "tool_call"},
            )
            fault = schedule.fault_for(step, tool)
            injector_event: dict[str, Any] | None = None
            rival = schedule.concurrent_for(step, tool)
            if rival is not None:
                params = rival.get("params", {}) or {}
                updates = dict(params.get("updates", {}) or {})
                applied = ledger.kv_apply_concurrent(str(params["key"]), updates)
                injector_event = {
                    "fault": "concurrent_update",
                    "target_tool": tool,
                    "target_step": step,
                    "record": str(params["key"]),
                    "updates": updates,
                    "version_before": applied["version_before"],
                    "version_after": applied["version_after"],
                    "committed": True,
                }
            if fault is not None and fault["kind"] == "timeout_before_commit":
                simulated = int((fault.get("params", {}) or {}).get("simulated_latency_ms", 0))
                clock.sleep(simulated)
                result = {
                    "outcome": "timeout",
                    "latency_ms": simulated,
                    "error": None,
                    "data": {},
                }
                injector_event = {
                    "fault": fault["kind"],
                    "target_tool": tool,
                    "target_step": step,
                    "simulated_latency_ms": simulated,
                    "committed": False,
                }
            else:
                result = registry.call(tool, args, clock)
                if (
                    fault is not None
                    and result["outcome"] != "error"
                    and fault["kind"] == "timeout_after_commit"
                ):
                    simulated = int((fault.get("params", {}) or {}).get("simulated_latency_ms", 0))
                    clock.sleep(max(0, simulated - int(result["latency_ms"])))
                    result = {
                        "outcome": "timeout",
                        "latency_ms": simulated,
                        "error": None,
                        "data": result["data"],
                    }
                    injector_event = {
                        "fault": fault["kind"],
                        "target_tool": tool,
                        "target_step": step,
                        "simulated_latency_ms": simulated,
                        "committed": True,
                    }
            writer.write(
                step=step,
                tool=tool,
                args=args,
                outcome=str(result["outcome"]),
                latency=int(result["latency_ms"]),
                injector_event=injector_event,
                ts_virtual=clock.now(),
                detail={
                    "phase": "tool_result",
                    "error": result["error"],
                    "data": result["data"],
                },
            )
            if injector_event is not None:
                writer.write(
                    step=step,
                    outcome="info",
                    injector_event=injector_event,
                    ts_virtual=clock.now(),
                    detail={"phase": "injector_event"},
                )
            messages.append({"role": "assistant", "tool": tool, "args": args})
            messages.append(
                {
                    "role": "tool",
                    "tool": tool,
                    "args": args,
                    "outcome": result["outcome"],
                    "data": result["data"],
                }
            )
            history = history_from_messages(messages)
            if tool == CHARGE_CARD and result["outcome"] == "timeout":
                attempts = len(
                    [
                        h
                        for h in history
                        if h.get("tool") == CHARGE_CARD and h.get("outcome") != "error"
                    ]
                )
                if rule is not None and agent.retries_allowed(attempts):
                    decision: dict[str, Any] = {
                        "rule": "on_tool_timeout",
                        "tool": CHARGE_CARD,
                        "action": str(rule.get("action", "retry_same")),
                        "retry_attempt": attempts + 1,
                        "max_attempts": max_attempts,
                    }
                else:
                    decision = {
                        "rule": "on_tool_timeout",
                        "tool": CHARGE_CARD,
                        "action": "stop",
                        "retry_attempt": attempts,
                        "max_attempts": max_attempts,
                    }
                writer.write(
                    step=step,
                    outcome="info",
                    policy_decision=decision,
                    ts_virtual=clock.now(),
                    detail={"phase": "policy_decision"},
                )
        snapshot = ledger.snapshot(run_id)
        writer.write(
            step=step,
            outcome="info",
            ts_virtual=clock.now(),
            detail={
                "phase": "run_end",
                "status": status,
                "tool_calls": calls,
                "charges": len(ledger.charges),
                "ledger_hash": snapshot["ledger_hash"],
            },
        )
        if state_path is not None:
            with open(state_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
    finally:
        writer.close()
    return {
        "run_id": run_id,
        "scenario": scenario_id,
        "steps": calls,
        "status": status,
        "charges": len(ledger.charges),
        "ledger_hash": snapshot["ledger_hash"],
        "trace_path": trace_path,
        "state_path": state_path,
    }
