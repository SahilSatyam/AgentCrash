"""ReAct runner: live Llama agent over synthetic ODRL tools, fully recorded.

The agent (``muse-spark-1.3`` by default) converts an NL access request
to ODRL 2.2 and saves it once (``parse_policy`` → ``validate_odrl`` →
``save_odrl``). Under the naive policy the agent converts sloppily and
retries blindly after an ambiguous save timeout — the double-save bug
the ODRL verifiers catch. Under a fixed policy it converts strictly,
validates, and reconciles first (re-read, rewrite only when missing).
``LlamaAdapter`` POSTs to an OpenAI-compatible
``{LLAMA_BASE_URL}/chat/completions`` with the key from
``LLAMA_API_KEY`` (header only, never logged or traced); transport or
protocol failures raise ``ModelNotConfiguredError`` (``E_MODEL_API``,
exit 5) instead of acting. No wall-clock calls anywhere.
"""

from __future__ import annotations

import json
import os
import random
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from agentcrash.clock import VirtualClock
from agentcrash.injector import FaultSchedule
from agentcrash.ledger import Ledger
from agentcrash.tools import SAVE_ODRL, ToolRegistry
from agentcrash.tracer import TraceWriter, compute_run_id

DEFAULT_MODEL = "muse-spark-1.3"

LLAMA_KEY_ENV = "LLAMA_API_KEY"
LLAMA_URL_ENV = "LLAMA_BASE_URL"
LLAMA_TIMEOUT_ENV = "LLAMA_TIMEOUT_S"
LIVE_HTTP_TIMEOUT_S = 60


def live_timeout_s() -> float:
    """Per-call HTTP timeout for the live model (env override in seconds)."""
    try:
        value = float(os.environ.get(LLAMA_TIMEOUT_ENV, "").strip() or 0)
    except ValueError:
        return LIVE_HTTP_TIMEOUT_S
    return value if value > 0 else LIVE_HTTP_TIMEOUT_S


def build_agent_brief(
    scenario: dict[str, Any],
    policy: dict[str, Any],
    policy_text: str,
    tool_names: list[str],
) -> str:
    """Build the system brief for a live converter run.

    The brief carries the task and the run's operating policy verbatim;
    the protocol sentences are derived from the policy rules, so a naive
    (empty) policy drives sloppy behavior that verifiers catch, while a
    fixed policy drives strict behavior. Verifiers judge the resulting
    trace against the same policy afterwards.
    """
    task = str(scenario.get("task", ""))
    tools = ", ".join(tool_names)
    env = scenario.get("environment", {}) or {}
    nl_ids = sorted(
        str(d.get("id", "")) for d in (env.get("nl_docs", []) or []) if isinstance(d, dict)
    )
    record_keys = sorted(
        str(r.get("key", "")) for r in (env.get("odrl_records", []) or []) if isinstance(r, dict)
    )
    schema_lines = []
    if "parse_policy" in tool_names:
        schema_lines.append(
            '- parse_policy: args {"doc_id": <required, one of: '
            + (", ".join(nl_ids) if nl_ids else "(none available)")
            + '>, "mode": "sloppy" (default) or "strict"}. '
            "Returns the NL text plus data.odrl (the converted ODRL object)."
        )
    if "validate_odrl" in tool_names:
        schema_lines.append(
            '- validate_odrl: args {"odrl": <required object, '
            "copy data.odrl from the parse_policy result>}. "
            "Returns data.valid plus data.errors."
        )
    if "save_odrl" in tool_names:
        schema_lines.append(
            '- save_odrl: args {"op": <required, "read" or "write">, '
            '"record": <required, one of: '
            + (", ".join(record_keys) if record_keys else "(none available)")
            + '>, "value": <object, required for writes: the ODRL to store>, '
            '"expected_version": <integer from your freshest read, or null>}. '
            "Reads return data.value and data.version; "
            "writes return data.version_conflict."
        )
    schemas = "\n".join(schema_lines)
    contract = (
        "Reply with exactly one JSON object and no other text: either "
        '{"type": "call", "tool": "<one of: '
        + tools
        + '>", "args": {<tool arguments>}} to invoke a tool, or '
        '{"type": "done", "reason": "<why the task is finished>"} '
        "when the task is complete. Only call a tool from the offered "
        "list, with an object for args."
    )
    rules = [r for r in (policy.get("rules", []) or []) if isinstance(r, dict)]
    actions = {(str(r.get("rule", "")), str(r.get("action", ""))) for r in rules}
    protocol = [
        "Call tools in dependency order: parse the request, then save the result.",
        f"Offered tools: {tools}.",
    ]
    if ("on_untrusted_content", "require_confirmation") in actions:
        protocol.append(
            "Convert in strict mode: validate every candidate with "
            "validate_odrl, never save an invalid document, and never save "
            "grants for principals outside the request (content tagged "
            "untrusted must never become a saved grant)."
        )
    else:
        protocol.append(
            "Convert in sloppy mode: save the parsed result directly without "
            "calling validate_odrl."
        )
    if ("on_stale_read", "check_then_act") in actions:
        protocol.append(
            "Before a mutating save, re-read the record and pass the "
            "freshest version you have read as expected_version."
        )
    else:
        protocol.append(
            "For mutating writes, pass the version from your first read as "
            "expected_version without re-reading."
        )
    if ("on_tool_timeout", "check_then_act") in actions:
        protocol.append(
            "On an ambiguous mutating-tool timeout, re-read first and only "
            "rewrite when the write is actually missing, keeping saves "
            "exactly-once."
        )
    else:
        protocol.append("On a mutating-tool timeout, retry the same write immediately.")
    tool_reference = (
        f"\nTool argument reference (only these args are accepted):\n{schemas}\n" if schemas else ""
    )
    return (
        f"{task}\n"
        "\n"
        "Operating policy for this run (follow it; the trace is verified "
        "against it afterwards):\n"
        f"{policy_text.strip()}\n"
        "\n" + "\n".join(protocol) + tool_reference + "\n" + contract
    )


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


class ModelNotConfiguredError(RuntimeError):
    """Raised when a live model path lacks credentials or fails closed."""


class LlamaAdapter:
    """Live Llama transport over OpenAI-compatible chat completions.

    Each ``complete()`` call asks the model for exactly one validated
    action; anything unparsable or off-protocol raises
    ``ModelNotConfiguredError`` (``E_MODEL_API``) instead of acting.
    Policy gating stays harness-side in :func:`run_react_loop`.
    """

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model = model
        self._last_usage: dict[str, int] = {}

    @property
    def last_usage(self) -> dict[str, int]:
        """Token usage reported by the most recent model call (may be empty)."""
        return dict(self._last_usage)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[str],
        timeout_s: float,
    ) -> dict[str, Any]:
        """Return the model's next action as ``{"type": "call"|"done", ...}``."""
        self._last_usage = {}
        api_key = os.environ.get(LLAMA_KEY_ENV, "").strip()
        base_url = os.environ.get(LLAMA_URL_ENV, "").strip()
        if not api_key or not base_url:
            raise ModelNotConfiguredError(
                f"live model {self._model!r} requires {LLAMA_KEY_ENV} and "
                f"{LLAMA_URL_ENV}; run the fixture-backed stub/fake offline instead"
            )
        offered = list(tools)
        body = json.dumps(
            {
                "model": self._model,
                "messages": list(messages),
                "temperature": 0,
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        url = base_url.rstrip("/") + "/chat/completions"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        wait_s = timeout_s if timeout_s > 0 else live_timeout_s()
        try:
            with urllib.request.urlopen(request, timeout=wait_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
            raw_usage = payload.get("usage", {})
            if isinstance(raw_usage, dict):
                self._last_usage = {
                    key: int(raw_usage[key])
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    if isinstance(raw_usage.get(key), int)
                }
            content = payload["choices"][0]["message"].get("content") or ""
            start = content.find("{")
            if start < 0:
                raise ValueError(f"no JSON object in model content: {content[:200]!r}")
            action, _ = json.JSONDecoder().raw_decode(content, start)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            preview = ""
            try:
                preview = f" content={content[:200]!r}"
            except NameError:
                pass
            detail = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    detail = f"{exc} :: {exc.read().decode('utf-8', 'replace')[:300]}"
                except OSError:
                    pass
            raise ModelNotConfiguredError(
                f"live model {self._model!r} unreachable or "
                f"off-protocol ({detail}){preview}; no action taken"
            ) from exc
        if not isinstance(action, dict):
            raise ModelNotConfiguredError(
                f"live model {self._model!r} returned a " "non-object action; no action taken"
            )
        if action.get("type") == "done":
            return {"type": "done", "reason": str(action.get("reason", ""))}
        if action.get("type") == "call" and action.get("tool") in offered:
            args = action.get("args", {})
            if isinstance(args, dict):
                return {"type": "call", "tool": str(action["tool"]), "args": args}
        raise ModelNotConfiguredError(
            f"live model {self._model!r} returned an invalid " "action; no action taken"
        )


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

    A doc may instead carry inline ``text`` (pasted at runtime, e.g. by
    the UI): it is stored as a ``user_supplied`` untrusted entry with
    empty candidate ODRLs, so the live model must author the ODRL
    itself from the request text. Inline docs need no fixture file.
    """
    env = scenario.get("environment", {}) or {}
    documents = env.get("nl_docs", []) or []
    if not documents:
        return {}
    needs_fixture = any(isinstance(doc, dict) and doc.get("fixture_key") for doc in documents)
    fixture_ref = str(env.get("nl_fixture", ""))
    entries: dict[str, Any] = {}
    if needs_fixture and not fixture_ref:
        raise ValueError("scenario declares nl_docs but no environment.nl_fixture")
    if needs_fixture:
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
        if not isinstance(doc, dict) or not doc.get("id"):
            raise ValueError("each nl_docs entry needs an id")
        if doc.get("fixture_key"):
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
            continue
        if isinstance(doc.get("text"), str) and doc["text"].strip():
            store[doc["id"]] = {
                "text": str(doc["text"]),
                "label": str(doc.get("label", "user_supplied")),
                "fixture": "inline:pasted",
                "naive_odrl": doc.get("naive_odrl", {}),
                "expected_odrl": doc.get("expected_odrl", {}),
            }
            continue
        raise ValueError(f"nl_docs entry {doc['id']!r} needs fixture_key or non-empty text")
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
    """Run the configured agent against the injector; write trace + state snapshot.

    Returns a summary dict (run_id, scenario, steps, status, charges,
    ledger_hash, trace_path, state_path). Raises UnknownFaultError before
    creating any file when the fault program is unusable.
    """
    rng = random.Random(seed)
    schedule = FaultSchedule.from_scenario(scenario, rng)
    clock = VirtualClock()
    ledger = Ledger()
    env = scenario.get("environment", {}) or {}
    vendor = ""
    for rec in env.get("records", []):
        ledger.kv_seed(str(rec["key"]), dict(rec.get("value", {}) or {}))
    for rec in env.get("odrl_records", []):
        ledger.kv_seed(str(rec["key"]), dict(rec.get("value", {}) or {}))
    doc_store = load_doc_store(scenario)
    nl_store = load_nl_store(scenario)
    registry = ToolRegistry.from_scenario(scenario, ledger, rng, doc_store, nl_store)

    run_id = compute_run_id(scenario_text, policy_text, seed)
    scenario_id = str(scenario.get("scenario", "unknown"))
    if model in ("fake", "stub"):
        raise ModelNotConfiguredError(
            "offline stub/fake adapters were removed; run with a live model id "
            f"(default {DEFAULT_MODEL}) and {LLAMA_KEY_ENV} set"
        )
    adapter = LlamaAdapter(model)

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
                "adapter": "live",
                "plan": f"live-react:{model}",
                "max_steps": max_steps,
                "vendor": vendor,
            },
        )
        brief = build_agent_brief(scenario, policy, policy_text, registry.tool_names)
        writer.write(
            step=1,
            outcome="info",
            ts_virtual=clock.now(),
            detail={"phase": "agent_plan", "plan": f"live-react:{model}"},
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": brief},
            {"role": "user", "content": str(scenario.get("task", ""))},
        ]
        step = 1
        calls = 0
        saves = 0
        model_calls = 0
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        status = "completed"
        call_counts: dict[tuple[str, str | None], int] = {}
        ok_counts: dict[str, int] = {}
        while True:
            step += 1
            if step > max_steps:
                status = "runaway"
                break
            action = adapter.complete(messages, registry.tool_names, timeout_s=0)
            usage = adapter.last_usage
            model_calls += 1
            for usage_key in usage_total:
                usage_total[usage_key] += int(usage.get(usage_key, 0))
            if action.get("type") != "call":
                break
            tool = str(action["tool"])
            args = dict(action.get("args", {}))
            calls += 1
            call_detail: dict[str, Any] = {"phase": "tool_call"}
            if usage:
                call_detail["model_usage"] = usage
            writer.write(
                step=step,
                tool=tool,
                args=args,
                outcome="info",
                ts_virtual=clock.now(),
                detail=call_detail,
            )
            op = args.get("op") if isinstance(args.get("op"), str) else None
            occurrence = call_counts.get((tool, op), 0) + 1
            call_counts[(tool, op)] = occurrence
            fault = schedule.fault_for_call(tool, op, occurrence)
            if fault is None:
                fault = schedule.fault_for(step, tool)
            injector_event: dict[str, Any] | None = None
            rival = schedule.concurrent_for_call(tool, op, occurrence)
            if rival is None:
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
            if tool == SAVE_ODRL and args.get("op") == "write" and result["outcome"] != "error":
                saves += 1
            if result["outcome"] != "error":
                ok_counts[tool] = ok_counts.get(tool, 0) + 1
            if injector_event is not None:
                writer.write(
                    step=step,
                    outcome="info",
                    injector_event=injector_event,
                    ts_virtual=clock.now(),
                    detail={"phase": "injector_event"},
                )
            messages.append({"role": "assistant", "content": json.dumps(action)})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool result from {tool} "
                        f"(outcome {result['outcome']}): "
                        + json.dumps(result["data"])
                        + (f" Error: {result['error']}." if result["error"] is not None else "")
                    ),
                }
            )
            if result["outcome"] == "timeout":
                attempts = ok_counts.get(tool, 0)
                rule = timeout_rule(policy, tool)
                if rule is not None:
                    retry = rule.get("retry", {}) or {}
                    decision: dict[str, Any] = {
                        "rule": "on_tool_timeout",
                        "tool": tool,
                        "action": str(rule.get("action", "retry_same")),
                        "retry_attempt": attempts + 1,
                        "max_attempts": int(retry.get("max_attempts", 1)),
                    }
                else:
                    decision = {
                        "rule": "on_tool_timeout",
                        "tool": tool,
                        "action": "stop",
                        "retry_attempt": attempts,
                        "max_attempts": 0,
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
                "saves": saves,
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
        "saves": saves,
        "model_calls": model_calls,
        "model_usage_total": usage_total,
        "ledger_hash": snapshot["ledger_hash"],
        "trace_path": trace_path,
        "state_path": state_path,
    }
