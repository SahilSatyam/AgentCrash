"""Ledger: idempotency dedup collapse, keyless-duplicate bug, hash stability."""

from agentcrash.ledger import Ledger


def test_keyed_retry_collapses_to_one_row() -> None:
    ledger = Ledger()
    ledger.add_invoice(9821, "Acme Paper Co", 150.0)
    first = ledger.charge(9821, 150.0, idempotency_key="inv-9821-a3f")
    second = ledger.charge(9821, 150.0, idempotency_key="inv-9821-a3f")
    assert first == second
    assert len(ledger.charges) == 1
    assert ledger.invoices[9821]["status"] == "PAID"


def test_keyless_retry_creates_duplicate_rows_v0_bug() -> None:
    ledger = Ledger()
    ledger.add_invoice(9821, "Acme Paper Co", 150.0)
    ledger.charge(9821, 150.0, idempotency_key=None)
    ledger.charge(9821, 150.0, idempotency_key=None)
    assert len(ledger.charges) == 2
    assert ledger.charges[0]["charge_id"] != ledger.charges[1]["charge_id"]


def test_different_keys_do_not_collapse() -> None:
    ledger = Ledger()
    ledger.add_invoice(9821, "Acme Paper Co", 150.0)
    ledger.charge(9821, 150.0, idempotency_key="inv-9821-a3f")
    ledger.charge(9821, 150.0, idempotency_key="inv-9821-b71")
    assert len(ledger.charges) == 2


def test_ledger_hash_stable_and_matches_snapshot() -> None:
    run_id = "a3f9c1d2e4b50607"

    def build() -> Ledger:
        ledger = Ledger()
        ledger.add_invoice(9821, "Acme Paper Co", 150.0)
        ledger.charge(9821, 150.0, idempotency_key="inv-9821-a3f")
        return ledger

    first = build().snapshot(run_id)
    second = build().snapshot(run_id)
    assert first["ledger_hash"] == second["ledger_hash"]
    assert first["ledger_hash"] == Ledger.ledger_hash(first)
    assert first["schema_version"] == "env_state/v1"
