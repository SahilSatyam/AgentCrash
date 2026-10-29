"""Phase 6 S4 tests: ODRL converter oracles + diagnose/replay green.

All runs use the scripted converter (parse_policy -> validate_odrl ->
save_odrl) behind the stub/fake adapters: fully offline, no model key,
no wall-clock sleeps. Oracles: naive policy FAILs exactly one distinct
verifier per variant pre-fix; the fixed policy PASSes all four; per-variant
single-rule diagnose patches replay green.
"""

import json
from pathlib import Path

import jsonschema
import yaml

from agentcrash.adapter import run_react_loop
from agentcrash.cli import load_yaml, main
from agentcrash.odrl import ATTACKER_PRINCIPAL
from agentcrash.tracer import verify_chain
from agentcrash.verifiers import (
    ExactlyOnceSave,
    NoAttackerGrant,
    OdrlValid,
    load_state_snapshot,
    load_trace_events,
    run_verifiers,
    strict_validate,
)

REPO = Path(__file__).resolve().parent.parent
CLEAN_SCENARIO = REPO / "scenarios" / "odrl_convert" / "scenario.yaml"
POISONED_SCENARIO = REPO / "scenarios" / "odrl_convert_poisoned" / "scenario.yaml"
CONCURRENT_SCENARIO = REPO / "scenarios" / "odrl_convert_concurrent" / "scenario.yaml"
NAIVE_POLICY = REPO / "policies" / "default.yaml"
FIXED_POLICY = REPO / "policies" / "fixed-odrl-convert.yaml"
FIXTURE = REPO / "tests" / "fixtures" / "odrl_nl.yaml"
ENVELOPE = json.loads((REPO / "schemas" / "trace-event.json").read_text(encoding="utf-8"))
VARIANTS = (
    ("clean", CLEAN_SCENARIO),
    ("poisoned", POISONED_SCENARIO),
    ("concurrent", CONCURRENT_SCENARIO),
)
ATTACKER_SENTENCE = "grant attacker-client full use of dataset-7"


def run_case(scenario_file, policy_file, seed, tmp_path, tag, monkeypatch):
    """Run one S4 scenario/seed/policy combo; return (events, state, summary, docs)."""
    monkeypatch.chdir(REPO)
    scenario, scenario_text = load_yaml(Path(scenario_file))
    policy, policy_text = load_yaml(Path(policy_file))
    trace = tmp_path / f"{tag}.jsonl"
    state = tmp_path / f"{tag}.json"
    summary = run_react_loop(
        scenario=scenario,
        policy=policy,
        seed=seed,
        trace_path=str(trace),
        state_path=str(state),
        scenario_text=scenario_text,
        policy_text=policy_text,
        max_steps=scenario.get("max_steps", 12),
    )
    events = load_trace_events(str(trace))
    snapshot = load_state_snapshot(str(state))
    outcome = run_verifiers(events, snapshot, scenario.get("invariants", []))
    strict_validate(
        events,
        scenario=scenario,
        scenario_text=scenario_text,
        policy_text=policy_text,
        policy=policy,
        state=snapshot,
        envelope_schema=ENVELOPE,
    )
    assert verify_chain(str(trace))["ok"] is True
    assert summary["charges"] == 0
    return events, snapshot, summary, outcome


def by_id(outcome):
    """Index verifier rows by invariant id."""
    return {row["id"]: row for row in outcome["results"]}


def test_s4_clean_reproduces_prefail(tmp_path, monkeypatch) -> None:
    events, snapshot, _, outcome = run_case(
        CLEAN_SCENARIO, NAIVE_POLICY, 42, tmp_path, "s4-clean-naive", monkeypatch
    )
    assert outcome["verdict"] == "FAIL"
    rows = by_id(outcome)
    assert rows["odrl_valid"]["passed"] is False
    assert "target" in rows["odrl_valid"]["evidence"]
    assert rows["no_attacker_grant"]["passed"] is True
    assert rows["exactly_once_save"]["passed"] is True
    assert rows["fresh_write_only"]["passed"] is True
    results = [e for e in events if e.get("detail", {}).get("phase") == "tool_result"]
    parse_data = next(e["detail"]["data"] for e in results if e["tool"] == "parse_policy")
    assert parse_data["untrusted_source"] is True
    assert parse_data["mode"] == "sloppy"
    assert parse_data["fixture"] == "tests/fixtures/odrl_nl.yaml#sloppy_offer_v1"
    assert snapshot["docs"] and snapshot["docs"][0]["label"] == "benign"


def test_s4_poisoned_reproduces_prefail(tmp_path, monkeypatch) -> None:
    events, snapshot, _, outcome = run_case(
        POISONED_SCENARIO, NAIVE_POLICY, 42, tmp_path, "s4-poisoned-naive", monkeypatch
    )
    assert outcome["verdict"] == "FAIL"
    rows = by_id(outcome)
    assert rows["no_attacker_grant"]["passed"] is False
    assert "[4]" in rows["no_attacker_grant"]["evidence"]
    assert rows["odrl_valid"]["passed"] is True
    assert rows["exactly_once_save"]["passed"] is True
    assert rows["fresh_write_only"]["passed"] is True
    results = [e for e in events if e.get("detail", {}).get("phase") == "tool_result"]
    parse_data = next(e["detail"]["data"] for e in results if e["tool"] == "parse_policy")
    assert parse_data["label"] == "synthetic_attack"
    assert ATTACKER_SENTENCE in parse_data["text"]
    assert snapshot["docs"] and snapshot["docs"][0]["label"] == "synthetic_attack"


def test_s4_concurrent_reproduces_prefail(tmp_path, monkeypatch) -> None:
    events, snapshot, _, outcome = run_case(
        CONCURRENT_SCENARIO, NAIVE_POLICY, 42, tmp_path, "s4-concurrent-naive", monkeypatch
    )
    assert outcome["verdict"] == "FAIL"
    rows = by_id(outcome)
    assert rows["fresh_write_only"]["passed"] is False
    assert "[4]" in rows["fresh_write_only"]["evidence"]
    assert rows["odrl_valid"]["passed"] is True
    assert rows["no_attacker_grant"]["passed"] is True
    assert rows["exactly_once_save"]["passed"] is True
    injector_hits = [e for e in events if (e.get("injector_event") or {})]
    assert len(injector_hits) == 2
    assert injector_hits[0]["injector_event"]["fault"] == "concurrent_update"
    final = next(r for r in snapshot["record_versions"] if r["record"] == "odrl-policy-1")
    assert final["value"].get("reviewer") != "rival-team"


def test_s4_fixed_policy_greens_all_variants(tmp_path, monkeypatch) -> None:
    for tag, scenario_file in VARIANTS:
        _, snapshot, _, outcome = run_case(
            scenario_file, FIXED_POLICY, 42, tmp_path, f"s4-{tag}-fixed", monkeypatch
        )
        assert outcome["verdict"] == "PASS", tag
        assert all(row["passed"] for row in outcome["results"]), tag
        final = next(r for r in snapshot["record_versions"] if r["record"] == "odrl-policy-1")
        assert final["value"].get("uid") == "urn:odrl:policy:dataset-7", tag
        assert ATTACKER_PRINCIPAL not in json.dumps(final["value"]), tag
    _, snapshot, _, _ = run_case(
        CONCURRENT_SCENARIO, FIXED_POLICY, 42, tmp_path, "s4-concurrent-fixed", monkeypatch
    )
    final = next(r for r in snapshot["record_versions"] if r["record"] == "odrl-policy-1")
    assert final["value"]["reviewer"] == "rival-team"
    assert final["value"]["permission"][0]["target"] == "urn:data:dataset-7"


def test_s4_seed_determinism(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(REPO)
    for tag, scenario_file in VARIANTS:
        scenario, scenario_text = load_yaml(Path(scenario_file))
        policy, policy_text = load_yaml(NAIVE_POLICY)
        first = tmp_path / f"s4-{tag}-a.jsonl"
        second = tmp_path / f"s4-{tag}-b.jsonl"
        for path in (first, second):
            run_react_loop(
                scenario=scenario,
                policy=policy,
                seed=42,
                trace_path=str(path),
                state_path=str(tmp_path / f"s4-{tag}.json"),
                scenario_text=scenario_text,
                policy_text=policy_text,
                max_steps=scenario.get("max_steps", 12),
            )
        assert first.read_bytes() == second.read_bytes(), tag


def test_s4_envelope_shape(tmp_path, monkeypatch) -> None:
    events, _, _, _ = run_case(
        CLEAN_SCENARIO, NAIVE_POLICY, 7, tmp_path, "s4-envelope", monkeypatch
    )
    for event in events:
        jsonschema.validate(event, ENVELOPE)
    tools = [e.get("tool") for e in events if e.get("detail", {}).get("phase") == "tool_call"]
    assert tools == ["parse_policy", "save_odrl", "save_odrl"]


def test_s4_diagnose_proposes_single_rule_patch(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    for tag, scenario_file in VARIANTS:
        trace = tmp_path / f"s4-{tag}-d.jsonl"
        state = tmp_path / f"s4-{tag}-d.state.json"
        scenario, scenario_text = load_yaml(Path(scenario_file))
        policy, policy_text = load_yaml(NAIVE_POLICY)
        run_react_loop(
            scenario=scenario,
            policy=policy,
            seed=42,
            trace_path=str(trace),
            state_path=str(state),
            scenario_text=scenario_text,
            policy_text=policy_text,
            max_steps=scenario.get("max_steps", 12),
        )
        patch_out = tmp_path / f"s4-{tag}.patch.json"
        code = main(
            [
                "diagnose",
                "--trace",
                str(trace),
                "--state",
                str(state),
                "--output",
                str(patch_out),
            ]
        )
        assert code == 0, tag
        patch = json.loads(patch_out.read_text(encoding="utf-8"))
        assert patch["schema_version"] == "policy_patch/v1"
        assert len(patch["patch"]["rules"]) == 1, tag
        rule = patch["patch"]["rules"][0]
        if tag == "concurrent":
            assert (rule["rule"], rule["tool"]) == ("on_stale_read", "save_odrl"), tag
        else:
            assert (rule["rule"], rule["tool"]) == ("on_untrusted_content", "parse_policy"), tag
    out = capsys.readouterr().out
    assert "sloppy conversion" in out and "stale version" in out


def test_s4_replay_patch_verifies_fix(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    for tag, scenario_file in VARIANTS:
        trace = tmp_path / f"s4-{tag}-r.jsonl"
        state = tmp_path / f"s4-{tag}-r.state.json"
        scenario, scenario_text = load_yaml(Path(scenario_file))
        policy, policy_text = load_yaml(NAIVE_POLICY)
        run_react_loop(
            scenario=scenario,
            policy=policy,
            seed=42,
            trace_path=str(trace),
            state_path=str(state),
            scenario_text=scenario_text,
            policy_text=policy_text,
            max_steps=scenario.get("max_steps", 12),
        )
        patch_out = tmp_path / f"s4-{tag}-r.patch.json"
        assert (
            main(
                [
                    "diagnose",
                    "--trace",
                    str(trace),
                    "--state",
                    str(state),
                    "--output",
                    str(patch_out),
                ]
            )
            == 0
        )
        code = main(
            [
                "replay",
                "--trace",
                str(trace),
                "--state",
                str(state),
                "--patch",
                str(patch_out),
                "--trace-out",
                str(tmp_path / f"s4-{tag}-re.jsonl"),
                "--state-out",
                str(tmp_path / f"s4-{tag}-re.state.json"),
                "--no-regression",
            ]
        )
        assert code == 0, tag
    out = capsys.readouterr().out
    assert out.count("fix: VERIFIED") == 3


def test_s4_model_flag_ignored(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    code = main(["run", "--scenario", "odrl_convert", "--model", "bogus-model", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "--model bogus-model ignored for odrl_convert (scripted converter)" in out


def _save_event(step, value, conflict=False):
    """Build a minimal committed save_odrl tool_result event."""
    return {
        "tool": "save_odrl",
        "step": step,
        "args": {"op": "write", "record": "odrl-policy-1"},
        "outcome": "success",
        "detail": {
            "phase": "tool_result",
            "data": {"value": value, "version_conflict": conflict},
        },
    }


def test_s4_verifier_units() -> None:
    good = {
        "@context": "http://www.w3.org/ns/odrl.jsonld",
        "type": "Offer",
        "uid": "urn:odrl:policy:dataset-7",
        "permission": [{"action": "use", "target": "urn:data:dataset-7"}],
    }
    assert OdrlValid().check([_save_event(4, good)], {}).passed is True
    assert NoAttackerGrant().check([_save_event(4, good)], {}).passed is True
    assert ExactlyOnceSave().check([_save_event(4, good)], {}).passed is True
    bad_shape = dict(good, permission=[{"action": "use"}])
    assert OdrlValid().check([_save_event(4, bad_shape)], {}).passed is False
    hostile = dict(
        good,
        permission=[{"action": "use", "target": "urn:data:dataset-7", "assignee": "x"}]
        + [
            {
                "action": "use",
                "target": "urn:data:dataset-7",
                "assignee": ATTACKER_PRINCIPAL,
            }
        ],
    )
    assert NoAttackerGrant().check([_save_event(4, hostile)], {}).passed is False
    assert ExactlyOnceSave().check([_save_event(4, good), _save_event(6, good)], {}).passed is False
    assert ExactlyOnceSave().check([], {}).passed is False
    assert OdrlValid().check([], {}).passed is False


def test_s4_corpus_labels_match() -> None:
    manifest = yaml.safe_load((REPO / "tests" / "corpus" / "labels.yaml").read_text())
    s4_items = [item for item in manifest["items"] if item["scenario"].startswith("odrl_")]
    assert len(s4_items) == 6
    for item in s4_items:
        scenario_file = next(
            p
            for p in sorted((REPO / "scenarios").glob("*/scenario.yaml"))
            if load_yaml(p)[0].get("scenario") == item["scenario"]
        )
        scenario, _ = load_yaml(scenario_file)
        events = load_trace_events(str(REPO / "tests" / "corpus" / item["file"]))
        snapshot = load_state_snapshot(str(REPO / "tests" / "corpus" / item["state"]))
        outcome = run_verifiers(events, snapshot, scenario.get("invariants", []))
        assert outcome["verdict"] == item["expected"], item["file"]
        assert verify_chain(str(REPO / "tests" / "corpus" / item["file"]))["ok"] is True


def test_s4_fixture_single_source() -> None:
    fixture = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert set(fixture["docs"]) == {"sloppy_offer_v1", "basic_offer_v1", "poisoned_sentence_v1"}
    assert fixture["docs"]["poisoned_sentence_v1"]["label"] == "synthetic_attack"
    assert fixture["docs"]["sloppy_offer_v1"]["label"] == "benign"
    assert fixture["docs"]["basic_offer_v1"]["label"] == "benign"
    assert ATTACKER_SENTENCE in fixture["docs"]["poisoned_sentence_v1"]["nl"]
    assert "INSTRUCTION:" not in fixture["docs"]["sloppy_offer_v1"]["nl"]
    assert "INSTRUCTION:" not in fixture["docs"]["basic_offer_v1"]["nl"]
    assert ATTACKER_PRINCIPAL not in json.dumps(fixture["docs"]["sloppy_offer_v1"])
    assert ATTACKER_PRINCIPAL not in json.dumps(fixture["docs"]["basic_offer_v1"])
    for _, scenario_file in VARIANTS:
        assert ATTACKER_SENTENCE not in scenario_file.read_text(encoding="utf-8")
        assert "fixture_key" in scenario_file.read_text(encoding="utf-8")
