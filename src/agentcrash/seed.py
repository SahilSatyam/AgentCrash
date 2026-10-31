"""Seed the UI database with scripted fixture-backed runs (offline).

Replays the naive/fixed oracle pairs from tests/test_scenarios_odrl.py
through the real ``run_react_loop`` + verifier + patch path and records
every run in ``agentcrash.db``. No network, no model key. The oracle
tests stay the contract; this module only reuses their action scripts.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from agentcrash.adapter import load_nl_store
from agentcrash.cli import load_yaml
from agentcrash.tracer import compute_run_id
from agentcrash.web import run_and_store

REPO = Path(__file__).resolve().parent.parent

STRICT_ODRL = {
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

SLOPPY_ODRL = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "permission": [{"action": "use", "assignee": "research-team"}],
}


def _parse(doc_id="access-request-1", mode="sloppy"):
    return {"type": "call", "tool": "parse_policy", "args": {"doc_id": doc_id, "mode": mode}}


def _validate(odrl):
    return {"type": "call", "tool": "validate_odrl", "args": {"odrl": odrl}}


def _read():
    return {"type": "call", "tool": "save_odrl", "args": {"op": "read", "record": "odrl-policy-1"}}


def _write(value, expected_version=None):
    args = {"op": "write", "record": "odrl-policy-1", "value": value}
    if expected_version is not None:
        args["expected_version"] = expected_version
    return {"type": "call", "tool": "save_odrl", "args": args}


DONE = {"type": "done", "reason": "saved"}

SCRIPTS = {
    ("odrl_convert", "naive"): [_parse(), _write(SLOPPY_ODRL), DONE],
    ("odrl_convert", "fixed"): [
        _parse(mode="strict"),
        _validate(STRICT_ODRL),
        _read(),
        _write(STRICT_ODRL, 1),
        DONE,
    ],
    ("odrl_convert_timeout", "naive"): [
        _parse(),
        _write(SLOPPY_ODRL),
        _write(SLOPPY_ODRL),
        DONE,
    ],
    ("odrl_convert_timeout", "fixed"): [
        _parse(mode="strict"),
        _validate(STRICT_ODRL),
        _read(),
        _write(STRICT_ODRL, 1),
        DONE,
    ],
    ("odrl_convert_poisoned", "naive"): [_parse(), _write("POISONED_NAIVE"), DONE],
    ("odrl_convert_poisoned", "fixed"): [
        _parse(mode="strict"),
        _validate(STRICT_ODRL),
        _read(),
        _write(STRICT_ODRL, 1),
        DONE,
    ],
    ("odrl_convert_concurrent", "naive"): [_parse(), _read(), _write(SLOPPY_ODRL, 1), DONE],
    ("odrl_convert_concurrent", "fixed"): [
        _parse(mode="strict"),
        _validate(STRICT_ODRL),
        _read(),
        _write({**STRICT_ODRL, "reviewer": "rival-team"}, 2),
        DONE,
    ],
}

FIXED_POLICY = {
    "odrl_convert": "policies/fixed-odrl-convert.yaml",
    "odrl_convert_poisoned": "policies/fixed-odrl-convert.yaml",
    "odrl_convert_concurrent": "policies/fixed-odrl-convert.yaml",
    "odrl_convert_timeout": "policies/fixed-odrl-timeout.yaml",
}

CASES = [
    ("odrl_convert", "naive"),
    ("odrl_convert", "fixed"),
    ("odrl_convert_timeout", "naive"),
    ("odrl_convert_timeout", "fixed"),
    ("odrl_convert_poisoned", "naive"),
    ("odrl_convert_poisoned", "fixed"),
    ("odrl_convert_concurrent", "naive"),
    ("odrl_convert_concurrent", "fixed"),
]


def _resolve_poisoned(actions: list[dict[str, Any]], scenario: dict[str, Any]) -> list[dict]:
    store = load_nl_store(scenario)
    doc_id = scenario["environment"]["nl_docs"][0]["id"]
    resolved = []
    for action in actions:
        if (
            isinstance(action, dict)
            and action.get("tool") == "save_odrl"
            and action.get("args", {}).get("value") == "POISONED_NAIVE"
        ):
            action = copy.deepcopy(action)
            action["args"] = dict(action["args"], value=store[doc_id]["naive_odrl"])
        resolved.append(copy.deepcopy(action))
    return resolved


def _seed_spec(
    scenario_id: str, variant: str, seed: int, root: Path, out_dir: Path | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scenario_path = root / "scenarios" / scenario_id / "scenario.yaml"
    policy_rel = FIXED_POLICY[scenario_id] if variant == "fixed" else "policies/default.yaml"
    policy_path = root / policy_rel
    scenario, scenario_text = load_yaml(scenario_path)
    policy, policy_text = load_yaml(policy_path)
    scenario = copy.deepcopy(scenario)
    faults = copy.deepcopy(scenario.get("faults", []))
    store = load_nl_store(scenario)
    doc_id = scenario["environment"]["nl_docs"][0]["id"]
    entry = store[doc_id]
    run_id = compute_run_id(f"{scenario_text}\nseed-demo: {variant}", policy_text, seed)
    out = out_dir or root
    actions = _resolve_poisoned(copy.deepcopy(SCRIPTS[(scenario_id, variant)]), scenario)
    spec = {
        "scenario": scenario,
        "scenario_text": scenario_text,
        "policy": policy,
        "policy_text": policy_text,
        "scenario_id": scenario_id,
        "patch_scenario_id": scenario_id,
        "policy_file": policy_rel,
        "faults": faults,
        "variant": variant,
        "seed": seed,
        "max_steps": scenario.get("max_steps", 12),
        "run_id": run_id,
        "nl_text": entry["text"],
        "nl_label": entry.get("label", ""),
        "trace_path": str(out / "traces" / f"{run_id}.jsonl"),
        "state_path": str(out / "reports" / f"{run_id}.state.json"),
        "db_path": str(out / "agentcrash.db"),
    }
    return spec, actions


def seed_database(root: Path, out_dir: Path | None = None, seed: int = 42) -> list[str]:
    """Replay all oracle combos scripted; record every run. Returns run keys."""
    keys = []
    for scenario_id, variant in CASES:
        spec, actions = _seed_spec(scenario_id, variant, seed, root, out_dir)
        run_key = f"{spec['run_id']}-seed"
        run_and_store(spec, run_key, actions)
        keys.append(run_key)
    return keys
