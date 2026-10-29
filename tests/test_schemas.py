"""Hand-built synthetic fixtures validate against every v1 schema."""

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

TRACE_EVENT = {
    "event_id": "evt_a3f9c1d2e4b50607_0003",
    "prev_hash": "GENESIS",
    "ts_virtual": 2300,
    "run_id": "a3f9c1d2e4b50607",
    "scenario": "payment_gateway_timeout",
    "step": 3,
    "tool": "charge_card",
    "args": {"amount": 150.0, "invoice_id": 9821, "idempotency_key": None},
    "outcome": "timeout",
    "latency": 2000,
    "injector_event": {
        "fault": "timeout_after_commit",
        "at_step": 3,
        "params": {"simulated_latency_ms": 2000},
    },
    "policy_decision": {
        "rule": "on_tool_timeout",
        "action": "retry_same",
        "detail": "blind retry, no idempotency key",
    },
    "detail": {"attempt": 1},
}

SCENARIO = {
    "schema_version": "scenario/v1",
    "scenario": "payment_gateway_timeout",
    "task": "Pay vendor $150 for invoice #9821",
    "seed": 42,
    "tools": [
        {
            "name": "charge_card",
            "latency_ms": 300,
            "timeout_ms": 1500,
            "mutating": True,
            "idempotency_key_arg": "idempotency_key",
        },
        {
            "name": "get_transaction_status",
            "latency_ms": 120,
            "timeout_ms": 1000,
            "mutating": False,
            "idempotency_key_arg": None,
        },
    ],
    "faults": [
        {
            "at_step": 3,
            "kind": "timeout_after_commit",
            "params": {"tool": "charge_card", "simulated_latency_ms": 2000},
        }
    ],
    "invariants": [
        {
            "id": "exactly_once_mutation",
            "check": "count(calls['charge_card']) == 1",
            "description": "The payment tool must commit exactly once.",
        }
    ],
    "policy": "policies/default.yaml",
    "max_steps": 12,
}

POLICY = {
    "schema_version": "policy/v1",
    "rules": [
        {
            "rule": "on_tool_timeout",
            "tool": "charge_card",
            "action": "retry_same",
            "retry": {"max_attempts": 2, "require_idempotency_key": False},
        }
    ],
}

POLICY_PATCH = {
    "schema_version": "policy_patch/v1",
    "source_run_id": "a3f9c1d2e4b50607",
    "rationale": "Force status check first and require idempotency keys on retry.",
    "patch": {
        "rules": [
            {
                "rule": "on_tool_timeout",
                "tool": "charge_card",
                "action": "check_then_act",
                "check": {"tool": "get_transaction_status", "by": "idempotency_key"},
                "retry": {"max_attempts": 2, "require_idempotency_key": True},
            }
        ]
    },
}

ENV_STATE = {
    "schema_version": "env_state/v1",
    "run_id": "a3f9c1d2e4b50607",
    "invoices": [{"id": 9821, "vendor": "Acme Paper Co", "amount": 150.0, "status": "PAID"}],
    "charges": [
        {
            "charge_id": "ch_001",
            "invoice_id": 9821,
            "amount": 150.0,
            "idempotency_key": None,
        }
    ],
    "docs": [],
    "record_versions": [],
    "ledger_hash": "sha256:" + "7f" * 32,
}

CASES = {
    "trace-event": TRACE_EVENT,
    "scenario": SCENARIO,
    "policy": POLICY,
    "policy-patch": POLICY_PATCH,
    "env-state": ENV_STATE,
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_synthetic_fixture_validates(name: str) -> None:
    schema = json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))
    jsonschema.validate(CASES[name], schema)


def test_unknown_schema_version_rejected() -> None:
    schema = json.loads((SCHEMA_DIR / "scenario.json").read_text(encoding="utf-8"))
    bad = dict(SCENARIO, schema_version="scenario/v9")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
