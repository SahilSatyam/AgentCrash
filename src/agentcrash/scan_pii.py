"""Offline PII guardrail: scan trace/state/report files for leaked PII shapes.

Mirrors the write-time redaction shapes in :mod:`agentcrash.tracer`
(emails + card-like digit runs) so CI can prove redacted artifacts carry
no raw PII. The canonical redaction placeholders (``redacted@example.com``
and ``4111111111111111``) are exempt: flagging them would punish the
redactor for doing its job. No network, no clock, no model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agentcrash.tracer import REDACTED_CARD, REDACTED_EMAIL

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CARD_RE = re.compile(r"\b\d(?:[ \-]?\d){12,18}\b")


@dataclass(frozen=True)
class PiiHit:
    """One raw-PII sighting: file, 1-based line, matched kind, matched text."""

    location: str
    kind: str
    sample: str


def _is_placeholder(kind: str, text: str) -> bool:
    if kind == "email":
        return text == REDACTED_EMAIL
    digits = re.sub(r"[ \-]", "", text)
    return digits == REDACTED_CARD


def scan_text(text: str, label: str) -> list[PiiHit]:
    """Scan ``text`` line by line; return hits (placeholders exempt)."""
    hits: list[PiiHit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in EMAIL_RE.finditer(line):
            if not _is_placeholder("email", match.group(0)):
                hits.append(PiiHit(f"{label}:{lineno}", "email", match.group(0)))
        for match in CARD_RE.finditer(line):
            if not _is_placeholder("card", match.group(0)):
                hits.append(PiiHit(f"{label}:{lineno}", "card", match.group(0)))
    return hits


def scan_files(paths: list[str | Path]) -> list[PiiHit]:
    """Scan raw file bytes (trace + state/report) for PII shapes."""
    hits: list[PiiHit] = []
    for raw in paths:
        path = Path(raw)
        hits.extend(scan_text(path.read_text(encoding="utf-8"), str(path)))
    return hits


def format_hits(hits: list[PiiHit]) -> str:
    """Render hits as ``location kind sample`` lines for CLI output."""
    return "\n".join(f"{hit.location} {hit.kind} {hit.sample}" for hit in hits)
