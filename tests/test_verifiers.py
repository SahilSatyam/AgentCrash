"""ODRL verifier tests: positive+negative fixture per verifier, no false PASS.

Verifiers are pure functions of (trace, state): malformed inputs raise
VerifierError (judged as corruption, exit 3) and unknown ids raise
UnknownVerifierError (E_CONFIG, exit 2) — neither ever reports PASS.
"""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from agentcrash.tracer import compute_run_id
from agentcrash.verifiers import (
    TraceCorruptError,
    UnknownVerifierError,
    Verdict,
    VerifierError,
    get_verifier,
    register_verifier,
    run_verifiers,
    strict_validate,
    verifier_ids,
)

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "odrl_convert_timeout" / "scenario.yaml"
POLICY_FILE = REPO / "policies" / "default.yaml"
FIXED_POLICY_FILE = REPO / "policies" / "fixed-odrl-timeout.yaml"
TRACE_DIR = REPO / "tests" / "fixtures" / "traces"
NAIVE_TRACE = TRACE_DIR / "odrl_convert_timeout-naive-seed42.jsonl"
NAIVE_STATE = TRACE_DIR / "odrl_convert_timeout-naive-seed42.state.json"
FIXED_TRACE = TRACE_DIR / "odrl_convert_timeout-fixed-seed42.jsonl"
FIXED_STATE = TRACE_DIR / "odrl_convert_timeout-fixed-seed42.state.json"

VALID_ODRL = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "assigner": "data-steward",
    "permission": [
        {
            "action": "use",
            "target": "urn:data:dataset-7",
            "assignee": "research-team",
        }
    ],
}

ATTACKER_ODRL = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "assigner": "data-steward",
    "permission": [
        {
            "action": "use",
            "target": "urn:data:dataset-7",
            "assignee": "attacker-client",
        }
    ],
}

SLOPPY_ODRL = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "permission": [{"action": "use", "assignee": "research-team"}],
}


def save_result(step, value, outcome="success", version_conflict=False):
    """Build one save_odrl tool_result event."""
    injector = (
        {
            "fault": "timeout_after_commit",
            "target_tool": "save_odrl",
            "target_step": step,
            "simulated_latency_ms": 2000,
            "committed": True,
        }
        if outcome == "timeout"
        else None
    )
    return {
        "tool": "save_odrl",
        "step": step,
        "outcome": outcome,
        "injector_event": injector,
        "args": {
            "op": "write",
            "record": "odrl-policy-1",
            "value": value,
            "expected_version": 1,
        },
        "detail": {
            "phase": "tool_result",
            "data": {
                "value": value,
                "version_conflict": version_conflict,
                "expected_version": 1,
            },
        },
    }


def odrl_state():
    """Build a minimal consistent state snapshot for ODRL checks."""
    return {
        "schema_version": "env_state/v1",
        "run_id": "r",
        "record_versions": [],
        "ledger_hash": "sha256:x",
    }


def test_registry_holds_odrl_verifiers() -> None:
    assert {
        "odrl_valid",
        "no_attacker_grant",
        "exactly_once_save",
        "fresh_write_only",
    } <= set(verifier_ids())
    assert get_verifier("exactly_once_save").id == "exactly_once_save"


def test_get_unknown_verifier_raises_config() -> None:
    with pytest.raises(UnknownVerifierError):
        get_verifier("no_such_invariant")


def test_register_and_get_roundtrip() -> None:
    class Dummy:
        id = "dummy_probe"

        def check(self, trace, state):
            del trace, state
            return Verdict(self.id, True, "probe ok")

    register_verifier(Dummy())
    try:
        assert get_verifier("dummy_probe").check([], {}).passed is True
    finally:
        import agentcrash.verifiers as verifiers_mod

        del verifiers_mod._REGISTRY["dummy_probe"]


def test_exactly_once_positive_single_save() -> None:
    trace = [save_result(4, VALID_ODRL)]
    verdict = get_verifier("exactly_once_save").check(trace, odrl_state())
    assert verdict.passed is True
    assert "step 4" in verdict.evidence


def test_exactly_once_negative_double_save_cites_steps() -> None:
    trace = [save_result(4, VALID_ODRL, outcome="timeout"), save_result(5, VALID_ODRL)]
    verdict = get_verifier("exactly_once_save").check(trace, odrl_state())
    assert verdict.passed is False
    assert "2 committed" in verdict.evidence
    assert "4" in verdict.evidence and "5" in verdict.evidence


def test_exactly_once_negative_zero_saves() -> None:
    verdict = get_verifier("exactly_once_save").check([], odrl_state())
    assert verdict.passed is False
    assert "no save_odrl" in verdict.evidence


def test_odrl_valid_positive() -> None:
    verdict = get_verifier("odrl_valid").check([save_result(4, VALID_ODRL)], odrl_state())
    assert verdict.passed is True
    assert "odrl-subset-frozen-v1" in verdict.evidence


def test_odrl_valid_negative_sloppy_save() -> None:
    verdict = get_verifier("odrl_valid").check([save_result(4, SLOPPY_ODRL)], odrl_state())
    assert verdict.passed is False
    assert "step 4" in verdict.evidence


def test_odrl_valid_negative_no_saves() -> None:
    verdict = get_verifier("odrl_valid").check([], odrl_state())
    assert verdict.passed is False


def test_no_attacker_grant_positive() -> None:
    verdict = get_verifier("no_attacker_grant").check([save_result(4, VALID_ODRL)], odrl_state())
    assert verdict.passed is True


def test_no_attacker_grant_negative() -> None:
    verdict = get_verifier("no_attacker_grant").check([save_result(4, ATTACKER_ODRL)], odrl_state())
    assert verdict.passed is False
    assert "attacker-client" in verdict.evidence


def test_fresh_write_only_positive() -> None:
    verdict = get_verifier("fresh_write_only").check([save_result(4, VALID_ODRL)], odrl_state())
    assert verdict.passed is True


def test_fresh_write_only_negative_conflict() -> None:
    trace = [save_result(4, VALID_ODRL, version_conflict=True)]
    verdict = get_verifier("fresh_write_only").check(trace, odrl_state())
    assert verdict.passed is False
    assert "4" in verdict.evidence


def test_malformed_inputs_never_pass() -> None:
    exactly_once = get_verifier("exactly_once_save")
    with pytest.raises(VerifierError):
        exactly_once.check("not-a-list", odrl_state())
    with pytest.raises(VerifierError):
        exactly_once.check(["not-an-event"], odrl_state())
    with pytest.raises(VerifierError):
        run_verifiers("not-a-list", odrl_state(), ["exactly_once_save"])
    with pytest.raises(VerifierError):
        run_verifiers([], odrl_state(), [{"id": None}])


def test_run_verifiers_unknown_id_raises_config() -> None:
    with pytest.raises(UnknownVerifierError):
        run_verifiers([], odrl_state(), ["exactly_once_save", "nope"])


def test_run_verifiers_overall_fail_on_any_violation() -> None:
    trace = [save_result(4, VALID_ODRL, outcome="timeout"), save_result(5, VALID_ODRL)]
    outcome = run_verifiers(trace, odrl_state(), ["exactly_once_save", "odrl_valid"])
    assert outcome["verdict"] == "FAIL"
    rows = {row["id"]: row for row in outcome["results"]}
    assert rows["exactly_once_save"]["passed"] is False
    assert rows["odrl_valid"]["passed"] is True


def test_recorded_live_traces_judge_as_expected() -> None:
    naive_events = [json.loads(line) for line in NAIVE_TRACE.read_text().splitlines()]
    naive_state = json.loads(NAIVE_STATE.read_text())
    naive = run_verifiers(
        naive_events,
        naive_state,
        ["odrl_valid", "no_attacker_grant", "exactly_once_save", "fresh_write_only"],
    )
    assert naive["verdict"] == "FAIL"
    fixed_events = [json.loads(line) for line in FIXED_TRACE.read_text().splitlines()]
    fixed_state = json.loads(FIXED_STATE.read_text())
    fixed = run_verifiers(
        fixed_events,
        fixed_state,
        ["odrl_valid", "no_attacker_grant", "exactly_once_save", "fresh_write_only"],
    )
    assert fixed["verdict"] == "PASS"


def _rechain(events):
    """Repair prev_hash links after an in-memory mutation (mirrors TraceWriter)."""
    prev = "GENESIS"
    for event in events:
        event["prev_hash"] = prev
        line = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        prev = hashlib.sha256(line.encode("utf-8")).hexdigest()


def _scenario_and_policy():
    scenario = yaml.safe_load(SCENARIO_FILE.read_text(encoding="utf-8"))
    scenario_text = SCENARIO_FILE.read_text(encoding="utf-8")
    policy = yaml.safe_load(FIXED_POLICY_FILE.read_text(encoding="utf-8"))
    policy_text = FIXED_POLICY_FILE.read_text(encoding="utf-8")
    return scenario, scenario_text, policy, policy_text


def test_strict_fixed_trace_validates_and_returns_seed() -> None:
    scenario, scenario_text, policy, policy_text = _scenario_and_policy()
    events = [json.loads(line) for line in FIXED_TRACE.read_text().splitlines()]
    state = json.loads(FIXED_STATE.read_text())
    assert (
        strict_validate(
            events,
            scenario=scenario,
            scenario_text=scenario_text,
            policy_text=policy_text,
            policy=policy,
            state=state,
        )
        == 42
    )


def test_strict_rule4_key_required_and_missing_key_rejected() -> None:
    scenario, scenario_text, _, policy_text = _scenario_and_policy()
    events = [json.loads(line) for line in FIXED_TRACE.read_text().splitlines()]
    state = json.loads(FIXED_STATE.read_text())
    strict_policy = {
        "schema_version": "policy/v1",
        "rules": [
            {
                "rule": "on_tool_timeout",
                "tool": "save_odrl",
                "action": "retry_same",
                "retry": {"max_attempts": 1, "require_idempotency_key": True},
            }
        ],
    }
    with pytest.raises(TraceCorruptError, match="idempotency_key"):
        strict_validate(
            events,
            scenario=scenario,
            scenario_text=scenario_text,
            policy_text=policy_text,
            policy=strict_policy,
            state=state,
        )


def test_strict_rule2_broken_chain_rejected() -> None:
    scenario, scenario_text, policy, policy_text = _scenario_and_policy()
    events = [json.loads(line) for line in FIXED_TRACE.read_text().splitlines()]
    state = json.loads(FIXED_STATE.read_text())
    events[2]["prev_hash"] = "tampered"
    with pytest.raises(TraceCorruptError, match="prev_hash"):
        strict_validate(
            events,
            scenario=scenario,
            scenario_text=scenario_text,
            policy_text=policy_text,
            policy=policy,
            state=state,
        )


def test_strict_rule5_ledger_mismatch_rejected() -> None:
    scenario, scenario_text, policy, policy_text = _scenario_and_policy()
    events = [json.loads(line) for line in FIXED_TRACE.read_text().splitlines()]
    state = json.loads(FIXED_STATE.read_text())
    state["ledger_hash"] = "sha256:" + "0" * 64
    with pytest.raises(TraceCorruptError, match="ledger_hash"):
        strict_validate(
            events,
            scenario=scenario,
            scenario_text=scenario_text,
            policy_text=policy_text,
            policy=policy,
            state=state,
        )


def test_committed_saves_counts_success_and_after_commit_timeout() -> None:
    trace = [
        save_result(3, VALID_ODRL, outcome="timeout"),
        save_result(5, VALID_ODRL),
    ]
    steps = sorted(e.get("step") for e in trace if e["outcome"] != "error")
    assert steps == [3, 5]
    assert get_verifier("exactly_once_save").check(trace, odrl_state()).passed is False


def test_run_id_formula_matches_recorded_naive() -> None:
    scenario_text = SCENARIO_FILE.read_text(encoding="utf-8")
    policy_text = POLICY_FILE.read_text(encoding="utf-8")
    assert compute_run_id(scenario_text, policy_text, 42) == "ea7d6c8084456233"
