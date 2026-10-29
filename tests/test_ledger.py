"""Ledger: versioned writes, stale-conflict flags, hash stability."""

from agentcrash.ledger import Ledger


def test_seeded_record_reads_at_version_1() -> None:
    ledger = Ledger()
    ledger.kv_seed("odrl-policy-1", {})
    assert ledger.kv_read("odrl-policy-1") == {"value": {}, "version": 1}


def test_fresh_write_bumps_version_without_conflict() -> None:
    ledger = Ledger()
    ledger.kv_seed("odrl-policy-1", {})
    written = ledger.kv_write("odrl-policy-1", {"type": "Offer"}, 1)
    assert written == {"version": 2, "conflict": False}
    assert ledger.kv_read("odrl-policy-1")["version"] == 2


def test_stale_write_still_commits_but_flags_conflict() -> None:
    ledger = Ledger()
    ledger.kv_seed("odrl-policy-1", {})
    ledger.kv_write("odrl-policy-1", {"type": "Offer"}, 1)
    stale = ledger.kv_write("odrl-policy-1", {"type": "Offer"}, 1)
    assert stale == {"version": 3, "conflict": True}
    assert ledger.kv_read("odrl-policy-1")["version"] == 3


def test_concurrent_update_preserved_in_snapshot() -> None:
    ledger = Ledger()
    ledger.kv_seed("odrl-policy-1", {})
    applied = ledger.kv_apply_concurrent("odrl-policy-1", {"note": "rival"})
    assert applied["version_after"] == 2
    snapshot = ledger.snapshot("r")
    assert snapshot["record_versions"][0]["value"] == {"note": "rival"}


def test_record_doc_tracks_retrieved_bytes() -> None:
    ledger = Ledger()
    ledger.record_doc("access-request-1", "tests/fixtures/odrl_nl.yaml#sloppy_offer_v1", "benign")
    snapshot = ledger.snapshot("r")
    assert snapshot["docs"][0]["doc_id"] == "access-request-1"


def test_ledger_hash_stable_and_matches_snapshot() -> None:
    run_id = "ea7d6c8084456233"

    def build() -> Ledger:
        ledger = Ledger()
        ledger.kv_seed("odrl-policy-1", {})
        ledger.kv_write("odrl-policy-1", {"type": "Offer"}, 1)
        return ledger

    first = build().snapshot(run_id)
    second = build().snapshot(run_id)
    assert first["ledger_hash"] == second["ledger_hash"]
    assert first["ledger_hash"] == Ledger.ledger_hash(first)
    assert first["schema_version"] == "env_state/v1"
