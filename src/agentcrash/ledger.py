"""Synthetic invoice/charge ledger with idempotency-key dedup (doc 06 S6).

Dedup rule: charges sharing ``(invoice_id, idempotency_key)`` collapse to
one row when the key is non-null; retries WITHOUT a key always create
duplicate rows (the v0 double-charge bug verifiers must catch).
Determinism: integer seq ids, canonical JSON hashing; no wall-clock,
no network, no model calls.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Ledger:
    """In-memory invoice/charge store for one run."""

    def __init__(self) -> None:
        self.invoices: dict[int, dict[str, Any]] = {}
        self.charges: list[dict[str, Any]] = []
        self._seq = 0
        self.records: dict[str, dict[str, Any]] = {}
        self.doc_spans: list[dict[str, Any]] = []

    def add_invoice(self, invoice_id: int, vendor: str, amount: float) -> dict[str, Any]:
        """Record an UNPAID invoice."""
        invoice = {"id": invoice_id, "vendor": vendor, "amount": amount, "status": "UNPAID"}
        self.invoices[invoice_id] = invoice
        return invoice

    def charge(
        self,
        invoice_id: int,
        amount: float,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Commit a charge; dedup on ``(invoice_id, idempotency_key)``.

        A non-null key that was already used for this invoice returns the
        existing row (no duplicate). A null key always appends a new row.
        """
        if idempotency_key is not None:
            for existing in self.charges:
                if (
                    existing["invoice_id"] == invoice_id
                    and existing["idempotency_key"] == idempotency_key
                ):
                    return existing
        self._seq += 1
        charge = {
            "charge_id": f"ch_{self._seq:03d}",
            "invoice_id": invoice_id,
            "amount": amount,
            "idempotency_key": idempotency_key,
        }
        self.charges.append(charge)
        invoice = self.invoices.get(invoice_id)
        if invoice is not None:
            invoice["status"] = "PAID"
        return charge

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
        """Remember one retrieved doc span for the state snapshot (S2)."""
        self.doc_spans.append({"doc_id": doc_id, "fixture": fixture, "label": label})

    def snapshot(self, run_id: str) -> dict[str, Any]:
        """Return the ``env_state/v1`` snapshot dict including ``ledger_hash``."""
        body: dict[str, Any] = {
            "schema_version": "env_state/v1",
            "run_id": run_id,
            "invoices": sorted(self.invoices.values(), key=lambda inv: inv["id"]),
            "charges": list(self.charges),
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
