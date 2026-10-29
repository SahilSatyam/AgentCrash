"""Append-only JSONL trace writer with hash chain and write-time redaction.

Canonical envelope (doc 06 S5, ADR-006): ``event_id prev_hash ts_virtual
run_id scenario step tool args outcome latency injector_event
policy_decision detail`` with sorted keys. Determinism: no wall-clock,
no network, no model calls.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from typing import Any, Self

DETAIL_MAX_BYTES = 65536
TRUNCATION_MARKER = "...[TRUNCATED detail]"
REDACTED = "[REDACTED]"
REDACTED_EMAIL = "redacted@example.com"
REDACTED_CARD = "4111111111111111"

_SENSITIVE_PATTERNS = ("*_token", "*secret*", "*key*")
_EXEMPT_KEYS = {"idempotency_key"}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CARD_RE = re.compile(r"\b\d(?:[ \-]?\d){12,18}\b")

OUTCOMES = {"success", "timeout", "error", "blocked", "info"}


def _is_sensitive_key(key: str) -> bool:
    if key in _EXEMPT_KEYS:
        return False
    lowered = key.lower()
    return any(fnmatch.fnmatchcase(lowered, pat) for pat in _SENSITIVE_PATTERNS)


def _scrub_str(value: str) -> str:
    value = _EMAIL_RE.sub(REDACTED_EMAIL, value)
    value = _CARD_RE.sub(REDACTED_CARD, value)
    return value


def redact(obj: Any) -> Any:
    """Recursively redact secrets/PII-shaped values in ``obj``.

    Rules (docs 11 S5-S6): mapping keys matching ``*_token`` / ``*secret*``
    / ``*key*`` (except ``idempotency_key``) have their value replaced;
    email shapes and card-like digit runs inside strings are replaced with
    fixed synthetic constants.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                out[key] = REDACTED
            else:
                out[key] = redact(value)
        return out
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    if isinstance(obj, str):
        return _scrub_str(obj)
    return obj


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _truncate_detail(detail: Any) -> Any:
    raw = _canonical(detail)
    if len(raw.encode("utf-8")) <= DETAIL_MAX_BYTES:
        return detail
    return {
        "_truncated": True,
        "original_bytes": len(raw.encode("utf-8")),
        "preview": raw[:DETAIL_MAX_BYTES] + TRUNCATION_MARKER,
    }


def compute_run_id(scenario_text: str, policy_text: str, seed: int) -> str:
    """Derive ``run_id = sha256(scenario_file + policy_file + seed)[:16]``."""
    digest = hashlib.sha256(f"{scenario_text}{policy_text}{seed}".encode())
    return digest.hexdigest()[:16]


class TraceWriter:
    """Write gapless, hash-chained, redacted trace events to a JSONL file."""

    def __init__(self, run_id: str, scenario: str, path: str) -> None:
        self.run_id = run_id
        self.scenario = scenario
        self.path = path
        self._seq = 0
        self._t = 0
        self._prev_hash = "GENESIS"
        self._fh = open(path, "w", encoding="utf-8")  # noqa: SIM115

    def write(
        self,
        *,
        step: int,
        tool: str | None = None,
        args: dict[str, Any] | None = None,
        outcome: str = "info",
        latency: int = 0,
        injector_event: dict[str, Any] | None = None,
        policy_decision: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        ts_virtual: int | None = None,
    ) -> dict[str, Any]:
        """Append one envelope event; returns the written (redacted) event."""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        if step < 0:
            raise ValueError("step must be non-negative")
        if latency < 0:
            raise ValueError("latency must be non-negative")
        if ts_virtual is None:
            ts_virtual = self._t + latency
        if ts_virtual < self._t:
            raise ValueError("ts_virtual must be monotonic non-decreasing")
        self._t = ts_virtual
        self._seq += 1
        event: dict[str, Any] = {
            "event_id": f"evt_{self.run_id}_{self._seq:04d}",
            "prev_hash": self._prev_hash,
            "ts_virtual": ts_virtual,
            "run_id": self.run_id,
            "scenario": self.scenario,
            "step": step,
            "tool": tool,
            "args": args or {},
            "outcome": outcome,
            "latency": latency,
            "injector_event": injector_event,
            "policy_decision": policy_decision,
            "detail": detail if detail is not None else {},
        }
        event = redact(event)
        event["detail"] = _truncate_detail(event["detail"])
        line = _canonical(event)
        self._fh.write(line + "\n")
        self._fh.flush()
        self._prev_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return event

    def close(self) -> None:
        """Close the trace file."""
        self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def verify_chain(path: str) -> dict[str, Any]:
    """Verify gapless ids, monotonic ts, and the prev_hash chain of a trace.

    Returns ``{"ok": True, ...}`` or ``{"ok": False, "first_bad": event_id,
    "reason": ...}`` naming the first bad link.
    """
    prev_hash = "GENESIS"
    expected_seq = 1
    prev_ts: int | None = None
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return {"ok": False, "first_bad": f"line-{lineno}", "reason": "invalid JSON"}
            event_id = event.get("event_id", f"line-{lineno}")
            want_id_prefix = f"_{expected_seq:04d}"
            if not str(event_id).endswith(want_id_prefix):
                return {"ok": False, "first_bad": event_id, "reason": "gapless event_id broken"}
            if event.get("prev_hash") != prev_hash:
                return {"ok": False, "first_bad": event_id, "reason": "prev_hash mismatch"}
            ts = event.get("ts_virtual")
            if prev_ts is not None and ts < prev_ts:
                return {"ok": False, "first_bad": event_id, "reason": "ts_virtual decreased"}
            prev_ts = ts
            prev_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
            expected_seq += 1
    return {"ok": True, "first_bad": None, "reason": None}
