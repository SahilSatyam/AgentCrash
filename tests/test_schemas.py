"""Hand-built synthetic fixtures validate against every v1 schema."""

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

TRACE_EVENT = {
    "event_id": "evt_ea7d6c8084456233_0004",
    "prev_hash": "GENESIS",
    "ts_virtual": 140,
    "run_id": "ea7d6c8084456233",
    "scenario": "odrl_convert_timeout",
    "step": 4,
    "tool": "save_odrl",
    "args": {"op": "write", "record": "odrl-policy-1", "expected_version": 1},
    "outcome": "timeout",
    "latency": 2000,
    "injector_event": {
        "fault": "timeout_after_commit",
        "target_tool": "save_odrl",
        "target_step": 4,
        "committed": True,
    },
    "policy_decision": {
        "rule": "on_tool_timeout",
        "action": "retry_same",
        "detail": "blind retry, no reconcile",
    },
    "detail": {"phase": "tool_result"},
}

SCENARIO = {
    "schema_version": "scenario/v1",
    "scenario": "odrl_convert_timeout",
    "task": "Convert the access request to ODRL 2.2 JSON-LD and save it once",
    "seed": 42,
    "tools": [
        {
            "name": "parse_policy",
            "latency_ms": 80,
            "timeout_ms": 1000,
            "mutating": False,
            "idempotency_key_arg": None,
        },
        {
            "name": "save_odrl",
            "latency_ms": 60,
            "timeout_ms": 1000,
            "mutating": True,
            "idempotency_key_arg": None,
        },
    ],
    "faults": [
        {
            "at_step": None,
            "kind": "timeout_after_commit",
            "params": {"tool": "save_odrl", "op": "write", "occurrence": 1},
        }
    ],
    "invariants": [
        {
            "id": "exactly_once_save",
            "check": "count(saves['save_odrl']) == 1",
            "description": "The converter commits exactly one save.",
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
            "tool": "save_odrl",
            "action": "retry_same",
            "retry": {"max_attempts": 2, "require_idempotency_key": False},
        }
    ],
}

POLICY_PATCH = {
    "schema_version": "policy_patch/v1",
    "source_run_id": "ea7d6c8084456233",
    "rationale": "Re-read the record version first and rewrite only when missing.",
    "patch": {
        "rules": [
            {
                "rule": "on_tool_timeout",
                "tool": "save_odrl",
                "action": "check_then_act",
                "check": {"tool": "save_odrl", "by": "version"},
                "retry": {"max_attempts": 1, "require_idempotency_key": False},
            }
        ]
    },
}

ENV_STATE = {
    "schema_version": "env_state/v1",
    "run_id": "ea7d6c8084456233",
    "docs": [],
    "record_versions": [{"record": "odrl-policy-1", "value": {"type": "Offer"}, "version": 3}],
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
