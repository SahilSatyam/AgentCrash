"""Synthetic per-run store: versioned records plus retrieved-doc spans.

Versioned records back ``key_value_store`` and ``save_odrl`` (seeded
version 1; writes bump the version and flag stale ``expected_version``
with ``conflict: True`` so verifiers can judge freshness). Determinism:
integer versions, canonical JSON hashing; no wall-clock, no network,
no model calls.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Ledger:
    """In-memory versioned-record store for one run."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.doc_spans: list[dict[str, Any]] = []

    def kv_seed(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        """Seed a versioned record at version 1 (S3 setup, per-run store)."""
        record = {"value": dict(value), "version": 1}
        self.records[key] = record
        return record

    def kv_read(self, key: str) -> dict[str, Any] | None:
        """Return ``{"value", "version"}`` for ``key``, or None when absent."""
        record = self.records.get(key)
        if record is None:
            return None
        return {"value": dict(record["value"]), "version": record["version"]}

    def kv_write(
        self, key: str, value: dict[str, Any], expected_version: int | None
    ) -> dict[str, Any]:
        """Write ``value`` under ``key`` and bump the version.

        Overwrite semantics: a stale (or missing) ``expected_version`` still
        commits — modelling a store without optimistic concurrency — but the
        result flags ``conflict: True`` so verifiers can judge freshness.
        """
        record = self.records.get(key)
        if record is None:
            record = {"value": {}, "version": 0}
            self.records[key] = record
        conflict = expected_version != record["version"]
        record["value"] = dict(value)
        record["version"] += 1
        return {"version": record["version"], "conflict": conflict}

    def kv_apply_concurrent(self, key: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply a concurrent writer's partial ``updates``; bump the version."""
        record = self.records.get(key)
        if record is None:
            record = {"value": {}, "version": 0}
            self.records[key] = record
        before = record["version"]
        record["value"].update(dict(updates))
        record["version"] += 1
        return {"version_before": before, "version_after": record["version"]}

    def record_doc(self, doc_id: str, fixture: str, label: str) -> None:
        """Remember one retrieved NL span for the state snapshot."""
        self.doc_spans.append({"doc_id": doc_id, "fixture": fixture, "label": label})

    def snapshot(self, run_id: str) -> dict[str, Any]:
        """Return the ``env_state/v1`` snapshot dict including ``ledger_hash``."""
        body: dict[str, Any] = {
            "schema_version": "env_state/v1",
            "run_id": run_id,
            "docs": list(self.doc_spans),
            "record_versions": [
                {"record": key, "value": rec["value"], "version": rec["version"]}
                for key, rec in sorted(self.records.items())
            ],
        }
        body["ledger_hash"] = "sha256:" + hashlib.sha256(_canonical(body).encode()).hexdigest()
        return body

    @staticmethod
    def ledger_hash(snapshot: dict[str, Any]) -> str:
        """Recompute the snapshot hash over all fields except ``ledger_hash``."""
        body = {k: v for k, v in snapshot.items() if k != "ledger_hash"}
        return "sha256:" + hashlib.sha256(_canonical(body).encode()).hexdigest()
