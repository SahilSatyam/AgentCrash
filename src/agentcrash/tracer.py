"""Append-only JSONL trace writer with hash chain and write-time redaction.

Canonical envelope (doc 06 S5, ADR-006): ``event_id prev_hash ts_virtual
run_id scenario step tool args outcome latency injector_event
policy_decision detail`` with sorted keys. Determinism: no wall-clock,
no network, no model calls.
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import hmac
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
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_CLOUD_TOKEN_RE = re.compile(
    r"\b(?:(?:sk|pk|glpat|xoxb|xoxp)[-_]|(?:ghp|gho)_|AKIA|ASIA|ya29)[-_A-Za-z0-9]{8,}\b"
)
_CLOUD_PREFIXES = (
    "sk-",
    "pk-",
    "ghp_",
    "gho_",
    "glpat-",
    "xoxb-",
    "xoxp-",
    "AKIA",
    "ASIA",
    "ya29",
)

OUTCOMES = {"success", "timeout", "error", "blocked", "info"}


#: The glob patterns above, compiled once (``fnmatch`` per key was the
#: tracer's main cost).
_SENSITIVE_KEY_RE = re.compile(
    "|".join(f"(?:{fnmatch.translate(pat)})" for pat in _SENSITIVE_PATTERNS)
)
_MIN_CARD_DIGITS = 13
_DROP_DIGITS = str.maketrans("", "", "0123456789")


def _luhn_checksum(digits: str) -> bool:
    clean = re.sub(r"\D", "", digits)
    if not (13 <= len(clean) <= 19):
        return False
    total = 0
    reverse_digits = clean[::-1]
    for i, c in enumerate(reverse_digits):
        d = ord(c) - ord("0")
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@functools.lru_cache(maxsize=4096)
def _is_sensitive_key(key: str) -> bool:
    if key in _EXEMPT_KEYS:
        return False
    return _SENSITIVE_KEY_RE.match(key.lower()) is not None


def _scrub_str(value: str) -> str:
    # Cheap prechecks skip heavy scans for clean strings
    if "@" in value:
        value = _EMAIL_RE.sub(REDACTED_EMAIL, value)
    if "eyJ" in value:
        value = _JWT_RE.sub(REDACTED, value)
    if any(p in value for p in _CLOUD_PREFIXES):
        value = _CLOUD_TOKEN_RE.sub(REDACTED, value)
    if (
        len(value) >= _MIN_CARD_DIGITS
        and len(value) - len(value.translate(_DROP_DIGITS)) >= _MIN_CARD_DIGITS
    ):
        def _replace_card(m: re.Match[str]) -> str:
            matched = m.group(0)
            return REDACTED_CARD if _luhn_checksum(matched) else matched

        value = _CARD_RE.sub(_replace_card, value)
    return value


def redact(obj: Any) -> Any:
    """Recursively redact secrets/PII-shaped values in ``obj``.

    Rules (docs 11 S5-S6): mapping keys matching ``*_token`` / ``*secret*``
    / ``*key*`` (except ``idempotency_key``) have their value replaced;
    email shapes and card-like digit runs inside strings are replaced with
    fixed synthetic constants.
    """
    kind = type(obj)
    if kind is str:
        return _scrub_str(obj)
    if kind is dict or isinstance(obj, dict):
        return {
            key: (REDACTED if isinstance(key, str) and _is_sensitive_key(key) else redact(value))
            for key, value in obj.items()
        }
    if kind is list or isinstance(obj, list):
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


def key_bytes(key: str | bytes | None) -> bytes | None:
    """Normalize a signing key (str or bytes) to bytes, or None."""
    if key is None:
        return None
    return key.encode() if isinstance(key, str) else key


class TraceWriter:
    """Write gapless, hash-chained, redacted trace events to a JSONL file.

    .. note:: Signing Semantics
       When ``signing_key`` is configured, ``TraceWriter`` computes HMAC-SHA256
       over the old ``prev_hash`` link and stores it in ``event["sig"]`` before
       canonicalizing and writing the line. The SHA256 of the entire line
       (including ``sig``) then becomes the next event's ``prev_hash``.
    """

    def __init__(
        self,
        run_id: str,
        scenario: str,
        path: str,
        signing_key: str | bytes | None = None,
    ) -> None:
        self.run_id = run_id
        self.scenario = scenario
        self.path = path
        self.signing_key = signing_key
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
        # Only caller-supplied fields are redacted. Harness-owned fields
        # (ids, hashes, counters) must never be rewritten: a hex hash with
        # 13+ digits in a row would otherwise look like a card number.
        event: dict[str, Any] = {
            "event_id": f"evt_{self.run_id}_{self._seq:04d}",
            "prev_hash": self._prev_hash,
            "ts_virtual": ts_virtual,
            "run_id": self.run_id,
            "scenario": self.scenario,
            "step": step,
            "tool": redact(tool),
            "args": redact(args or {}),
            "outcome": outcome,
            "latency": latency,
            "injector_event": redact(injector_event),
            "policy_decision": redact(policy_decision),
            "detail": redact(detail if detail is not None else {}),
        }
        kb = key_bytes(self.signing_key)
        if kb is not None:
            event["sig"] = hmac.new(
                kb, self._prev_hash.encode(), hashlib.sha256
            ).hexdigest()
        line = _canonical(event)
        # The detail is a slice of the line, so a small line needs no
        # separate detail size check (saves one full serialization).
        if len(line) > DETAIL_MAX_BYTES // 4 and len(line.encode("utf-8")) > DETAIL_MAX_BYTES:
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


def verify_chain(path: str, signing_key: str | bytes | None = None) -> dict[str, Any]:
    """Verify gapless ids, monotonic ts, and the prev_hash chain of a trace.

    When ``signing_key`` is supplied, HMAC-SHA256 signatures are also verified.
    Returns ``{"ok": True, ...}`` or ``{"ok": False, "first_bad": event_id,
    "reason": ...}`` naming the first bad link.
    """
    prev_hash = "GENESIS"
    expected_seq = 1
    prev_ts: int | None = None
    kb = key_bytes(signing_key)
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
            if kb is not None:
                expected_sig = hmac.new(
                    kb, prev_hash.encode(), hashlib.sha256
                ).hexdigest()
                if event.get("sig") != expected_sig:
                    return {"ok": False, "first_bad": event_id, "reason": "signature mismatch"}
            ts = event.get("ts_virtual")
            if prev_ts is not None and ts < prev_ts:
                return {"ok": False, "first_bad": event_id, "reason": "ts_virtual decreased"}
            prev_ts = ts
            prev_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
            expected_seq += 1
    return {"ok": True, "first_bad": None, "reason": None}
