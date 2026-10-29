"""ReAct runner: scripted stub agent plus fake adapter, fully offline.

The v0 agent runs ``stub-plan:retry_immediately``: check status, charge
once, and on an ambiguous TIMEOUT retry immediately — blindly issuing a
fresh idempotency key under the naive policy (the double-charge bug that
Phase 3 verifiers catch). Both the stub and the fake model adapter sit
behind ``complete(messages, tools, timeout_s)``; the live Llama transport
is stubbed to refuse without a key and never touches the network, so every
run is fixture-backed and deterministic. No wall-clock calls anywhere.
"""

from __future__ import annotations

import json
import os
import random
from typing import Any

from agentcrash.clock import VirtualClock
from agentcrash.injector import FaultSchedule
from agentcrash.ledger import Ledger
from agentcrash.tools import (
    CHARGE_CARD,
    FIRST_IDEMPOTENCY_KEY,
    GET_TRANSACTION_STATUS,
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


class StubAgent:
    """Scripted v0 agent: reconcile-read, charge, blind retry, confirm."""

    def __init__(
        self,
        invoice_id: int,
        amount: float,
        max_attempts: int = 1,
        require_idempotency_key: bool = False,
    ) -> None:
        self._invoice_id = invoice_id
        self._amount = amount
        self._max_attempts = max_attempts
        self._require_key = require_idempotency_key

    def retries_allowed(self, attempts_made: int) -> bool:
        """True while another mutating attempt fits in max_attempts."""
        return attempts_made <= self._max_attempts

    def _retry_key(self, attempts_made: int) -> str:
        if self._require_key:
            return FIRST_IDEMPOTENCY_KEY
        return f"{FIRST_IDEMPOTENCY_KEY}-r{attempts_made}"

    def next_action(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        """Pick the next action from past tool results (pure function)."""
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
        raise ValueError("scenario 'environment.invoices' must list at least one invoice")
    first = invoices[0]
    return int(first["id"]), str(first.get("vendor", "")), first["amount"]


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
    invoice_id, vendor, amount = _invoice_seed(scenario)
    for inv in (scenario.get("environment", {}) or {}).get("invoices", []):
        ledger.add_invoice(int(inv["id"]), str(inv.get("vendor", "")), inv["amount"])
    registry = ToolRegistry.from_scenario(scenario, ledger, rng)

    run_id = compute_run_id(scenario_text, policy_text, seed)
    scenario_id = str(scenario.get("scenario", "unknown"))
    rule = timeout_rule(policy, CHARGE_CARD)
    if rule is not None:
        retry = rule.get("retry", {}) or {}
        max_attempts = int(retry.get("max_attempts", 1))
        require_key = bool(retry.get("require_idempotency_key", False))
    else:
        max_attempts, require_key = 0, False
    agent = StubAgent(invoice_id, amount, max_attempts, require_key)
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
