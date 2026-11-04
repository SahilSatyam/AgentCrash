"""Versioned SDK-to-server event schema (v1).

At-least-once delivery with idempotent ingestion: the server treats
``event_id`` as the idempotency key and dedupes. Ordering per run is by
``seq``; ``ts_virtual`` is a host-controlled virtual clock (monotonic per
run), never wall-clock.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1

REQUIRED_KEYS = (
    "schema_version",
    "event_id",
    "run_id",
    "project",
    "workflow",
    "step",
    "kind",
    "status",
    "seq",
)


def make_event(
    *,
    event_id: str,
    run_id: str,
    project: str,
    workflow: str,
    step: str | None = None,
    kind: str,
    status: str = "ok",
    seq: int,
    tool: str | None = None,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    error: str | None = None,
    fault: dict[str, Any] | None = None,
    parent_step: str | None = None,
) -> dict[str, Any]:
    """Build a v1 event dict (JSON-serializable)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "run_id": run_id,
        "project": project,
        "workflow": workflow,
        "step": step,
        "parent_step": parent_step,
        "kind": kind,
        "status": status,
        "seq": seq,
        "tool": tool,
        "inputs": inputs or {},
        "outputs": outputs or {},
        "error": error,
        "fault": fault,
    }


def validate_event(event: dict[str, Any]) -> list[str]:
    """Return a list of problems; empty means valid."""
    problems: list[str] = []
    for key in REQUIRED_KEYS:
        if key not in event:
            problems.append(f"missing key {key!r}")
    if event.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {event.get('schema_version')!r}")
    seq = event.get("seq")
    if not isinstance(seq, int) or seq < 1:
        problems.append("seq must be a positive int")
    return problems
