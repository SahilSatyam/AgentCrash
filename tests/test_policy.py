"""Phase 4 policy-engine tests: load/validate, merge-patch, diff rendering."""

import json
from pathlib import Path

import pytest
import yaml

from agentcrash.policy import (
    PolicyError,
    PolicyRejectedError,
    apply_patch,
    diff_policies,
    load_patch_doc,
    load_policy,
    merge_patch,
    normalize_rule,
    reference_patch_for,
    validate_patch_data,
    validate_policy_data,
)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = REPO / "policies" / "default.yaml"


def test_load_default_policy_validates() -> None:
    data, text = load_policy(DEFAULT_POLICY)
    assert data["schema_version"] == "policy/v1"
    assert text == DEFAULT_POLICY.read_text(encoding="utf-8")
    assert data["rules"] == []


def test_load_policy_rejects_bad_version(tmp_path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("schema_version: policy/v9\nrules: []\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="policy/v1"):
        load_policy(bad)


def test_load_policy_rejects_missing_file(tmp_path) -> None:
    with pytest.raises(PolicyError, match="cannot read"):
        load_policy(tmp_path / "nope.yaml")


def test_validate_policy_data_rejects_bad_retry() -> None:
    with pytest.raises(PolicyError, match="max_attempts|violates"):
        validate_policy_data(
            {
                "schema_version": "policy/v1",
                "rules": [
                    {
                        "rule": "on_tool_timeout",
                        "tool": "save_odrl",
                        "action": "retry_same",
                        "retry": {"max_attempts": 9, "require_idempotency_key": False},
                    }
                ],
            }
        )


def test_reference_patch_envelope_is_schema_valid() -> None:
    patch = reference_patch_for("odrl_convert_timeout", "abc123")
    validate_patch_data(patch)
    assert patch["schema_version"] == "policy_patch/v1"
    assert patch["source_run_id"] == "abc123"
    assert len(patch["patch"]["rules"]) == 1


def test_reference_patch_carries_reconcile_fix() -> None:
    rule = reference_patch_for("odrl_convert_timeout", "abc123")["patch"]["rules"][0]
    assert (rule["rule"], rule["tool"]) == ("on_tool_timeout", "save_odrl")
    assert rule["action"] == "check_then_act"
    assert rule["check"] == {"tool": "save_odrl", "by": "version"}
    assert rule["retry"] == {"max_attempts": 1, "require_idempotency_key": False}


def test_reference_patch_variants_cover_odrl_scenarios() -> None:
    validate_rule = reference_patch_for("odrl_convert", "abc123")["patch"]["rules"][0]
    assert (validate_rule["rule"], validate_rule["tool"]) == (
        "on_untrusted_content",
        "parse_policy",
    )
    fresh_rule = reference_patch_for("odrl_convert_concurrent", "abc123")["patch"]["rules"][0]
    assert (fresh_rule["rule"], fresh_rule["tool"]) == ("on_stale_read", "save_odrl")


def test_reference_patch_for_unknown_scenario_rejected() -> None:
    with pytest.raises(ValueError, match="no reference patch"):
        reference_patch_for("payment_gateway_timeout", "abc123")


def test_apply_reference_patch_yields_fixed_policy(tmp_path) -> None:
    base, _ = load_policy(DEFAULT_POLICY)
    new_policy, new_text = apply_patch(base, reference_patch_for("odrl_convert_timeout", "abc123"))
    assert new_policy["schema_version"] == "policy/v1"
    rule = new_policy["rules"][0]
    assert rule["action"] == "check_then_act"
    assert rule["check"] == {"tool": "save_odrl", "by": "version"}
    assert rule["retry"] == {"max_attempts": 1, "require_idempotency_key": False}
    assert "retry_with_idempotency_key" not in rule and "max_retries" not in rule
    reparsed = yaml.safe_load(new_text)
    assert reparsed == new_policy


def test_merge_patch_replaces_arrays_and_deletes_nulls() -> None:
    assert merge_patch({"a": {"b": 1, "c": 2}}, {"a": {"b": 9, "c": None}}) == {"a": {"b": 9}}
    assert merge_patch({"rules": [1]}, {"rules": [2]}) == {"rules": [2]}


def test_apply_patch_rejects_time_travel_action() -> None:
    base, _ = load_policy(DEFAULT_POLICY)
    patch = reference_patch_for("odrl_convert_timeout", "abc123")
    patch["patch"]["rules"][0]["action"] = "time_travel"
    with pytest.raises(PolicyRejectedError):
        apply_patch(base, patch)


def test_apply_patch_rejects_out_of_range_retries() -> None:
    base, _ = load_policy(DEFAULT_POLICY)
    patch = reference_patch_for("odrl_convert_timeout", "abc123")
    patch["patch"]["rules"][0]["max_retries"] = 7
    with pytest.raises(PolicyRejectedError):
        apply_patch(base, patch)


def test_normalize_rule_maps_adr004_action_aliases() -> None:
    rule = normalize_rule(
        {"rule": "on_tool_timeout", "tool": "*", "action": "check_transaction_status"}
    )
    assert rule["action"] == "check_then_act"
    assert normalize_rule({"rule": "r", "tool": "t", "action": "abort"})["action"] == "fail"


def test_load_patch_doc_rejects_free_form_text(tmp_path) -> None:
    bad = tmp_path / "patch.json"
    bad.write_text(json.dumps({"notes": "just retry it harder"}), encoding="utf-8")
    with pytest.raises(PolicyRejectedError, match="policy_patch/v1"):
        load_patch_doc(bad)


def test_diff_policies_renders_review_lines() -> None:
    base, _ = load_policy(DEFAULT_POLICY)
    new_policy, _ = apply_patch(base, reference_patch_for("odrl_convert_timeout", "abc123"))
    diff = diff_policies(base, new_policy)
    assert "on_tool_timeout/save_odrl" in diff
    assert "check_then_act" in diff
    assert "0 rule(s)" in diff_policies(base, base)
