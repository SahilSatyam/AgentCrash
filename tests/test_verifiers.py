"""Phase 3 verifier tests: positive+negative fixture per verifier, no false PASS.

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
    committed_charge_steps,
    get_verifier,
    register_verifier,
    run_verifiers,
    strict_validate,
    verifier_ids,
)

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "timeout_retry" / "scenario.yaml"
POLICY_FILE = REPO / "policies" / "default.yaml"
FIXED_TRACE = REPO / "traces" / "golden" / "timeout_retry-fixed-seed43.jsonl"
FIXED_STATE = REPO / "traces" / "golden" / "timeout_retry-fixed-seed43.state.json"


def charge_result(step, outcome="success", charge_id="ch_001", committed=False):
    """Build one charge_card tool_result event."""
    injector = (
        {
            "fault": "timeout_after_commit",
            "target_tool": "charge_card",
            "target_step": step,
            "simulated_latency_ms": 2000,
            "committed": True,
        }
        if committed
        else None
    )
    return {
        "tool": "charge_card",
        "step": step,
        "outcome": outcome,
        "injector_event": injector,
        "detail": {"phase": "tool_result"},
    }


def paid_state(n_charges, status="PAID"):
    """Build a state snapshot with n single-key charges."""
    charges = [
        {
            "charge_id": f"ch_{i + 1:03d}",
            "invoice_id": 9821,
            "amount": 150,
            "idempotency_key": "inv-9821-a3f" if i == 0 else f"inv-9821-a3f-r{i}",
        }
        for i in range(n_charges)
    ]
    return {
        "schema_version": "env_state/v1",
        "run_id": "r",
        "invoices": [{"id": 9821, "vendor": "Acme Corp", "amount": 150, "status": status}],
        "charges": charges,
        "ledger_hash": "sha256:x",
    }


def test_registry_holds_v0_verifiers() -> None:
    assert set(verifier_ids()) == {"exactly_once_mutation", "task_completion"}
    assert get_verifier("exactly_once_mutation").id == "exactly_once_mutation"


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


def test_exactly_once_positive_single_charge() -> None:
    trace = [charge_result(3, "success", "ch_001")]
    verdict = get_verifier("exactly_once_mutation").check(trace, paid_state(1))
    assert verdict.passed is True
    assert "ch_001" in verdict.evidence


def test_exactly_once_negative_double_charge_cites_steps() -> None:
    trace = [charge_result(3, "timeout", "ch_001", committed=True), charge_result(4)]
    verdict = get_verifier("exactly_once_mutation").check(trace, paid_state(2))
    assert verdict.passed is False
    assert "2 charges" in verdict.evidence
    assert "3" in verdict.evidence and "4" in verdict.evidence


def test_exactly_once_negative_zero_charges() -> None:
    verdict = get_verifier("exactly_once_mutation").check([], paid_state(0))
    assert verdict.passed is False
    assert "0 charges" in verdict.evidence


def test_task_completion_positive_paid() -> None:
    verdict = get_verifier("task_completion").check([], paid_state(1))
    assert verdict.passed is True
    assert "PAID" in verdict.evidence


def test_task_completion_negative_unpaid() -> None:
    verdict = get_verifier("task_completion").check([], paid_state(1, status="UNPAID"))
    assert verdict.passed is False
    assert "UNPAID" in verdict.evidence


def test_malformed_state_never_passes() -> None:
    exactly_once = get_verifier("exactly_once_mutation")
    task_completion = get_verifier("task_completion")
    with pytest.raises(VerifierError):
        exactly_once.check([], {})
    with pytest.raises(VerifierError):
        exactly_once.check([], {"charges": "nope"})
    with pytest.raises(VerifierError):
        task_completion.check([], {"invoices": []})
    with pytest.raises(VerifierError):
        task_completion.check([], {})
    with pytest.raises(VerifierError):
        run_verifiers("not-a-list", paid_state(1), ["exactly_once_mutation"])
    with pytest.raises(VerifierError):
        run_verifiers([], paid_state(1), [{"id": None}])


def test_run_verifiers_unknown_id_raises_config() -> None:
    with pytest.raises(UnknownVerifierError):
        run_verifiers([], paid_state(1), ["exactly_once_mutation", "nope"])


def test_run_verifiers_overall_fail_on_any_violation() -> None:
    trace = [charge_result(3, committed=True), charge_result(4)]
    outcome = run_verifiers(trace, paid_state(2), ["exactly_once_mutation", "task_completion"])
    assert outcome["verdict"] == "FAIL"
    rows = {row["id"]: row for row in outcome["results"]}
    assert rows["exactly_once_mutation"]["passed"] is False
    assert rows["task_completion"]["passed"] is True


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
    policy = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8"))
    policy_text = POLICY_FILE.read_text(encoding="utf-8")
    return scenario, scenario_text, policy, policy_text


def test_strict_rule4_key_required_and_missing_key_rejected() -> None:
    scenario, scenario_text, _, policy_text = _scenario_and_policy()
    events = [json.loads(line) for line in FIXED_TRACE.read_text(encoding="utf-8").splitlines()]
    state = json.loads(FIXED_STATE.read_text(encoding="utf-8"))
    strict_policy = {
        "schema_version": "policy/v1",
        "rules": [
            {
                "rule": "on_tool_timeout",
                "tool": "charge_card",
                "action": "retry_same",
                "retry": {"max_attempts": 1, "require_idempotency_key": True},
            }
        ],
    }
    seed = strict_validate(
        events,
        scenario=scenario,
        scenario_text=scenario_text,
        policy_text=policy_text,
        policy=strict_policy,
        state=state,
    )
    assert seed == 43
    broken = json.loads(json.dumps(events))
    for event in broken:
        detail = event.get("detail") or {}
        if detail.get("phase") == "tool_call" and event.get("tool") == "charge_card":
            event["args"] = dict(event.get("args", {}))
            event["args"]["idempotency_key"] = None
            break
    _rechain(broken)
    with pytest.raises(TraceCorruptError, match="idempotency_key"):
        strict_validate(
            broken,
            scenario=scenario,
            scenario_text=scenario_text,
            policy_text=policy_text,
            policy=strict_policy,
            state=state,
        )


def test_strict_rule2_broken_chain_rejected() -> None:
    scenario, scenario_text, policy, policy_text = _scenario_and_policy()
    events = [json.loads(line) for line in FIXED_TRACE.read_text(encoding="utf-8").splitlines()]
    state = json.loads(FIXED_STATE.read_text(encoding="utf-8"))
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
    events = [json.loads(line) for line in FIXED_TRACE.read_text(encoding="utf-8").splitlines()]
    state = json.loads(FIXED_STATE.read_text(encoding="utf-8"))
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


def test_committed_steps_counts_success_and_after_commit_timeout() -> None:
    trace = [
        charge_result(3, "timeout", committed=True),
        charge_result(4, "timeout", committed=False),
        charge_result(5),
    ]
    assert committed_charge_steps(trace) == [3, 5]


def test_run_id_formula_matches_golden() -> None:
    scenario_text = SCENARIO_FILE.read_text(encoding="utf-8")
    policy_text = POLICY_FILE.read_text(encoding="utf-8")
    assert compute_run_id(scenario_text, policy_text, 42) == "43e3064f37f779fe"
