"""TraceWriter: envelope, gapless ids, chain verify, redaction, truncation."""

import hashlib
import json

import pytest

from agentcrash.tracer import DETAIL_MAX_BYTES, TraceWriter, compute_run_id, verify_chain

RUN_ID = "a3f9c1d2e4b50607"


def _writer(tmp_path, run_id=RUN_ID):
    return TraceWriter(run_id, "timeout_retry", str(tmp_path / "t.jsonl"))


def test_envelope_fields_and_sorted_keys(tmp_path) -> None:
    with _writer(tmp_path) as writer:
        event = writer.write(step=0, outcome="info", detail={"seed": 42})
    assert sorted(event.keys()) == sorted(
        [
            "event_id",
            "prev_hash",
            "ts_virtual",
            "run_id",
            "scenario",
            "step",
            "tool",
            "args",
            "outcome",
            "latency",
            "injector_event",
            "policy_decision",
            "detail",
        ]
    )
    with open(writer.path, encoding="utf-8") as fh:
        raw = fh.readline().rstrip("\n")
    assert list(json.loads(raw).keys()) == sorted(json.loads(raw).keys())
    assert event["event_id"] == f"evt_{RUN_ID}_0001"
    assert event["prev_hash"] == "GENESIS"
    assert event["ts_virtual"] == 0


def test_gapless_ids_and_monotonic_ts(tmp_path) -> None:
    with _writer(tmp_path) as writer:
        writer.write(step=1, tool="write_record", outcome="success", latency=300)
        writer.write(step=2, tool="write_record", outcome="timeout", latency=2000)
        path = writer.path
    with open(path, encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh]
    assert [e["event_id"] for e in events] == [f"evt_{RUN_ID}_{i:04d}" for i in (1, 2)]
    assert events[0]["ts_virtual"] == 300
    assert events[1]["ts_virtual"] == 2300


def test_chain_verifies_and_names_first_bad_link(tmp_path) -> None:
    with _writer(tmp_path) as writer:
        writer.write(step=0, outcome="info")
        writer.write(step=1, tool="write_record", outcome="success", latency=300)
        writer.write(step=2, tool="write_record", outcome="timeout", latency=2000)
        path = writer.path
    assert verify_chain(path)["ok"] is True
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    tampered = dict(json.loads(lines[1]))
    tampered["outcome"] = "error"
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    result = verify_chain(path)
    assert result["ok"] is False
    assert result["first_bad"] == f"evt_{RUN_ID}_0003"
    assert result["reason"] == "prev_hash mismatch"


def test_non_monotonic_ts_rejected(tmp_path) -> None:
    with _writer(tmp_path) as writer:
        writer.write(step=1, latency=300)
        with pytest.raises(ValueError):
            writer.write(step=2, latency=0, ts_virtual=100)


def test_redaction_strips_secrets_but_keeps_idempotency_key(tmp_path) -> None:
    with _writer(tmp_path) as writer:
        event = writer.write(
            step=1,
            tool="write_record",
            args={
                "record": "order-1",
                "idempotency_key": "inv-9821-a3f",
                "api_key": "sk-live-abc123",
                "auth_token": "tok-secret",
                "client_secret": "hunter2",
                "email": "buyer@acme.test",
                "card": "4111 1111 1111 1111",
            },
            outcome="success",
            latency=300,
        )
        path = writer.path
    assert event["args"]["idempotency_key"] == "inv-9821-a3f"
    assert event["args"]["api_key"] == "[REDACTED]"
    assert event["args"]["auth_token"] == "[REDACTED]"
    assert event["args"]["client_secret"] == "[REDACTED]"
    assert event["args"]["email"] == "redacted@example.com"
    assert event["args"]["card"] == "4111111111111111"
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    assert "sk-live-abc123" not in raw
    assert "buyer@acme.test" not in raw
    assert "inv-9821-a3f" in raw


def test_detail_truncation_marker(tmp_path) -> None:
    with _writer(tmp_path) as writer:
        event = writer.write(step=1, outcome="info", detail={"blob": "x" * (DETAIL_MAX_BYTES + 1)})
    assert event["detail"]["_truncated"] is True
    assert event["detail"]["original_bytes"] > DETAIL_MAX_BYTES
    assert event["detail"]["preview"].endswith("...[TRUNCATED detail]")


def test_compute_run_id_matches_authority_rule() -> None:
    want = hashlib.sha256(b"scenpol42").hexdigest()[:16]
    assert compute_run_id("scen", "pol", 42) == want


def test_luhn_rejects_numeric_ids_and_accepts_valid_cards() -> None:
    from agentcrash.tracer import REDACTED_CARD, redact

    # 16-digit order ID that fails Luhn checksum
    order_id = "1234567890123456"
    assert redact({"order": order_id})["order"] == order_id

    # Valid cards that pass Luhn checksum
    valid_card = "4242424242424242"
    assert redact({"card": valid_card})["card"] == REDACTED_CARD
    valid_card_spaced = "4111 1111 1111 1111"
    assert redact({"card": valid_card_spaced})["card"] == REDACTED_CARD


def test_jwt_and_cloud_token_redaction() -> None:
    from agentcrash.tracer import REDACTED, redact

    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
        "G3jPsmHQyGz_gqXJ1eP4Rkm4g6r_v2kCpxGq8HnC6uU"
    )
    res = redact({"session": f"Bearer {jwt}"})
    assert jwt not in res["session"]
    assert REDACTED in res["session"]

    cloud_key = "sk-proj-1234567890abcdef1234567890"
    res_cloud = redact({"api": f"Bearer {cloud_key}"})
    assert cloud_key not in res_cloud["api"]
    assert REDACTED in res_cloud["api"]

    ghp_token = "ghp_123456789012345678901234567890123456"
    res_ghp = redact({"gh": ghp_token})
    assert ghp_token not in res_ghp["gh"]
    assert REDACTED in res_ghp["gh"]
