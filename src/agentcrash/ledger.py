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

    def snapshot(self, run_id: str) -> dict[str, Any]:
        """Return the ``env_state/v1`` snapshot dict including ``ledger_hash``."""
        body: dict[str, Any] = {
            "schema_version": "env_state/v1",
            "run_id": run_id,
            "invoices": sorted(self.invoices.values(), key=lambda inv: inv["id"]),
            "charges": list(self.charges),
            "docs": [],
            "record_versions": [],
        }
        body["ledger_hash"] = "sha256:" + hashlib.sha256(_canonical(body).encode()).hexdigest()
        return body

    @staticmethod
    def ledger_hash(snapshot: dict[str, Any]) -> str:
        """Recompute the snapshot hash over all fields except ``ledger_hash``."""
        body = {k: v for k, v in snapshot.items() if k != "ledger_hash"}
        return "sha256:" + hashlib.sha256(_canonical(body).encode()).hexdigest()
