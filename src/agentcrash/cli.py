"""AgentCrash command-line entry point (v0: run lands in Phase 2)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from agentcrash import __version__
from agentcrash.adapter import DEFAULT_MODEL, run_react_loop
from agentcrash.injector import FaultSchedule, UnknownFaultError
from agentcrash.policy import (
    PolicyError,
    PolicyRejectedError,
    apply_patch,
    diff_policies,
    fetch_live_patch,
    load_patch_doc,
    load_policy,
    reference_patch,
    validate_patch_data,
)
from agentcrash.tracer import compute_run_id, verify_chain
from agentcrash.verifiers import (
    TraceCorruptError,
    UnknownVerifierError,
    VerifierError,
    load_state_snapshot,
    load_trace_events,
    run_verifiers,
    strict_validate,
)

EXIT_OK = 0
EXIT_VERDICT_FAIL = 1
EXIT_USAGE = 2
EXIT_CORRUPT = 3
EXIT_POLICY_REJECTED = 4
EXIT_RUNABORTED = 5


class ConfigError(ValueError):
    """Bad flags, missing files, or invalid scenario/policy (E_CONFIG/E_SCHEMA)."""


def resolve_scenario(ref: str) -> Path:
    """Resolve a scenario id or path to its scenario.yaml file."""
    direct = Path(ref)
    if direct.is_file():
        return direct
    candidates = [
        Path("scenarios") / ref / "scenario.yaml",
        Path("scenarios") / f"{ref}.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"unknown --scenario {ref!r}: tried {direct}, " + ", ".join(str(c) for c in candidates)
    )


def resolve_policy(ref: str | None, scenario_path: Path, scenario: dict[str, Any]) -> Path:
    """Resolve the policy override, the scenario default, or fail."""
    raw = ref or str(scenario.get("policy", ""))
    if not raw:
        raise ConfigError("no policy: pass --policy or set scenario 'policy'")
    direct = Path(raw)
    if direct.is_file():
        return direct
    sibling = scenario_path.parent / raw
    if sibling.is_file():
        return sibling
    raise ConfigError(f"policy file not found: {raw!r}")


def load_yaml(path: Path) -> tuple[dict[str, Any], str]:
    """Parse a YAML mapping file; return (data, raw text)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data, text


def validate_run_inputs(scenario: dict[str, Any], policy: dict[str, Any]) -> tuple[int, int]:
    """Light structural validation; returns (seed, max_steps)."""
    for key in ("scenario", "task", "tools", "policy"):
        if key not in scenario:
            raise ConfigError(f"scenario is missing required key {key!r}")
    if not isinstance(scenario["tools"], list) or not scenario["tools"]:
        raise ConfigError("scenario 'tools' must be a non-empty list")
    if policy.get("schema_version") != "policy/v1":
        raise ConfigError(
            f"policy schema_version must be 'policy/v1' (got {policy.get('schema_version')!r})"
        )
    try:
        FaultSchedule.from_scenario(scenario)
    except UnknownFaultError as exc:
        raise ConfigError(str(exc)) from exc
    seed = scenario.get("seed", 42)
    max_steps = scenario.get("max_steps", 12)
    if type(seed) is not int or type(max_steps) is not int:
        raise ConfigError("scenario 'seed' and 'max_steps' must be integers")
    return seed, max_steps


def cmd_run(argv: list[str]) -> int:
    """Execute `agentcrash run` (exit 0 completed, 2 usage/config, 5 runaway)."""
    parser = argparse.ArgumentParser(prog="agentcrash run")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--policy", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--trace-out", default=None)
    parser.add_argument("--state-out", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    def fail(code: str, hint: str, exit_code: int) -> int:
        payload = {"code": code, "exit": exit_code, "hint": hint, "command": "run"}
        if args.json:
            print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        else:
            print(f"agentcrash run: {code}: {hint}", file=sys.stderr)
        return exit_code

    try:
        scenario_path = resolve_scenario(args.scenario)
        scenario, scenario_text = load_yaml(scenario_path)
        policy_path = resolve_policy(args.policy, scenario_path, scenario)
        policy, policy_text = load_yaml(policy_path)
        file_seed, file_steps = validate_run_inputs(scenario, policy)
    except ConfigError as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    seed = args.seed if args.seed is not None else file_seed
    max_steps = args.max_steps if args.max_steps is not None else file_steps
    run_id = compute_run_id(scenario_text, policy_text, seed)
    trace_out = args.trace_out or str(Path("traces") / f"{run_id}.jsonl")
    state_out = args.state_out or str(Path("reports") / f"{run_id}.state.json")

    if args.dry_run:
        plan = {
            "run_id": run_id,
            "scenario": scenario.get("scenario"),
            "task": scenario.get("task"),
            "seed": seed,
            "max_steps": max_steps,
            "model": args.model,
            "adapter": "fake (offline fixture-backed)",
            "tools": [t.get("name") for t in scenario.get("tools", [])],
            "faults": scenario.get("faults", []),
            "policy": str(policy_path),
            "trace_out": trace_out,
        }
        if args.json:
            print(json.dumps(plan, sort_keys=True, indent=2))
        else:
            print(f"run_id={run_id} scenario={plan['scenario']} seed={seed} steps={max_steps}")
            print(f"task: {plan['task']}")
            print(f"tools: {', '.join(str(t) for t in plan['tools'])}")
            print(f"faults: {plan['faults']}")
            print(f"policy: {plan['policy']} model: {args.model} (fake offline)")
            print(f"trace: {trace_out} (dry-run: nothing written)")
        return EXIT_OK

    try:
        Path(trace_out).parent.mkdir(parents=True, exist_ok=True)
        Path(state_out).parent.mkdir(parents=True, exist_ok=True)
        summary = run_react_loop(
            scenario=scenario,
            policy=policy,
            seed=seed,
            trace_path=trace_out,
            state_path=state_out,
            scenario_text=scenario_text,
            policy_text=policy_text,
            max_steps=max_steps,
            model=args.model,
        )
    except UnknownFaultError as exc:
        return fail("E_SCHEMA", str(exc), EXIT_USAGE)
    except (ValueError, OSError) as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)

    if args.json:
        print(
            json.dumps(
                {
                    "api_version": "v1",
                    "trace": summary["trace_path"],
                    "status": summary["status"],
                    "steps": summary["steps"],
                    "scenario": summary["scenario"],
                    "run_id": summary["run_id"],
                    "ledger_hash": summary["ledger_hash"],
                },
                sort_keys=True,
            )
        )
    else:
        print(
            f"run_id={summary['run_id']} scenario={summary['scenario']} "
            f"steps={summary['steps']} trace={summary['trace_path']}"
        )
        print(
            f"status={summary['status']} "
            f"ledger_hash={summary['ledger_hash']} charges={summary['charges']}"
        )
    return EXIT_RUNABORTED if summary["status"] == "runaway" else EXIT_OK


def _infer_scenario(trace_scenario: str) -> Path:
    """Find the scenario.yaml whose id matches the trace scenario."""
    for candidate in sorted(Path("scenarios").glob("*/scenario.yaml")):
        try:
            data, _ = load_yaml(candidate)
        except ConfigError:
            continue
        if data.get("scenario") == trace_scenario:
            return candidate
    raise ConfigError(
        f"cannot infer --scenario for trace scenario {trace_scenario!r}; pass --scenario"
    )


def cmd_check(argv: list[str]) -> int:
    """Execute `agentcrash check` (exit 0 PASS, 1 VERDICT_FAIL, 3 corrupt)."""
    parser = argparse.ArgumentParser(prog="agentcrash check")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--strict-schema", action="store_true")
    parser.add_argument("--verify-chain", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--report-out", default=None)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    as_json = args.format == "json"

    def fail(code: str, hint: str, exit_code: int) -> int:
        if as_json:
            print(
                json.dumps(
                    {"api_version": "v1", "code": code, "exit": exit_code, "hint": hint},
                    sort_keys=True,
                )
            )
        else:
            print(f"agentcrash check: {code}: {hint}", file=sys.stderr)
        return exit_code

    def emit(text: str) -> None:
        print(text, file=sys.stderr if as_json else sys.stdout)

    trace_path = Path(args.trace)
    if not trace_path.is_file():
        return fail("E_CONFIG", f"trace file not found: {args.trace!r}", EXIT_USAGE)
    try:
        events = load_trace_events(str(trace_path))
    except TraceCorruptError as exc:
        emit(f"trace {trace_path} is corrupt: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    if args.verify_chain:
        chain = verify_chain(str(trace_path))
        if not chain["ok"]:
            hint = f"broken chain at {chain['first_bad']}: {chain['reason']}"
            emit(f"trace {trace_path} is corrupt: {hint}")
            return fail("E_TRACE_CORRUPT", hint, EXIT_CORRUPT)

    trace_scenario = events[0].get("scenario", "")
    try:
        scenario_path = Path(args.scenario) if args.scenario else _infer_scenario(trace_scenario)
        scenario, scenario_text = load_yaml(scenario_path)
        policy_path = resolve_policy(None, scenario_path, scenario)
        policy, policy_text = load_yaml(policy_path)
    except ConfigError as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)

    run_id = str(events[0].get("run_id", ""))
    state_ref = args.state or str(Path("reports") / f"{run_id}.state.json")
    if not Path(state_ref).is_file():
        return fail("E_CONFIG", f"state file not found: {state_ref!r}", EXIT_USAGE)
    try:
        state = load_state_snapshot(state_ref)
    except TraceCorruptError as exc:
        emit(f"state {state_ref} is corrupt: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    seed = scenario.get("seed", 42)
    if args.strict_schema:
        try:
            schema_path = Path(__file__).resolve().parent.parent.parent / "schemas"
            envelope = json.loads((schema_path / "trace-event.json").read_text(encoding="utf-8"))
            seed = strict_validate(
                events,
                scenario=scenario,
                scenario_text=scenario_text,
                policy_text=policy_text,
                policy=policy,
                state=state,
                envelope_schema=envelope,
            )
        except TraceCorruptError as exc:
            emit(f"trace {trace_path} failed --strict-schema: {exc}")
            return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    invariants = scenario.get("invariants", [])
    if not invariants:
        return fail("E_CONFIG", f"scenario {scenario_path} declares no invariants", EXIT_USAGE)
    try:
        outcome = run_verifiers(events, state, invariants)
    except UnknownVerifierError as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    except VerifierError as exc:
        emit(f"trace {trace_path} cannot be judged: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    policy_hash = "sha256:" + hashlib.sha256(policy_text.encode()).hexdigest()
    charges = state.get("charges", [])
    task_done = all(inv.get("status") == "PAID" for inv in state.get("invoices", []))
    task_done = bool(task_done and state.get("invoices"))
    report = {
        "api_version": "v1",
        "ledger_hash": state.get("ledger_hash"),
        "policy_hash": policy_hash,
        "results": outcome["results"],
        "run_id": run_id,
        "scenario": scenario.get("scenario"),
        "seed": seed,
        "side_effect_count": len(charges) if isinstance(charges, list) else 0,
        "task_completion": task_done,
        "verdict": outcome["verdict"],
    }
    report_out = args.report_out or str(Path("reports") / f"{run_id}.verdict.json")
    Path(report_out).parent.mkdir(parents=True, exist_ok=True)
    Path(report_out).write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    for row in report["results"]:
        mark = "PASS" if row["passed"] else "FAIL"
        verb = "satisfied" if row["passed"] else "violated!"
        emit(f"[{mark}] Invariant {row['id']} {verb} ({row['evidence']})")
    emit(f"verdict={report['verdict']} report={report_out}")
    if as_json:
        print(json.dumps(report, sort_keys=True, indent=2))
    return EXIT_OK if report["verdict"] == "PASS" else EXIT_VERDICT_FAIL


def _trace_seed(events: list[dict[str, Any]], fallback: int) -> int:
    """Read the run seed from the run_start event, else use ``fallback``."""
    for event in events:
        detail = event.get("detail") or {}
        if detail.get("phase") == "run_start" and type(detail.get("seed")) is int:
            return detail["seed"]
    return fallback


def cmd_diagnose(argv: list[str]) -> int:
    """Execute `agentcrash diagnose` (exit 0 patch, 2 usage, 4 rejected).

    Fixture-backed by default: the v0 double-charge failure maps to the
    reference reconcile-then-same-key-retry patch. Passing traces are
    refused (exit 2). When LLAMA_BASE_URL is set the live
    OpenAI-compatible path is tried first (opt-in, slow); transport
    failures fall back to the fixture, but schema-invalid model output is
    rejected (exit 4). Read-only: input trace/state/policy files are never
    written.
    """
    parser = argparse.ArgumentParser(prog="agentcrash diagnose")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-rules", type=int, default=1)
    parser.add_argument("--state", default=None)
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--policy", default=None)
    parser.add_argument("--verdict", default=None)
    args = parser.parse_args(argv)

    def fail(code: str, hint: str, exit_code: int) -> int:
        print(f"agentcrash diagnose: {code}: {hint}", file=sys.stderr)
        return exit_code

    if args.max_rules is not None and args.max_rules < 1:
        return fail("E_POLICY_REJECTED", "--max-rules must be >= 1", EXIT_POLICY_REJECTED)
    trace_path = Path(args.trace)
    if not trace_path.is_file():
        return fail("E_CONFIG", f"trace file not found: {args.trace!r}", EXIT_USAGE)
    try:
        events = load_trace_events(str(trace_path))
    except TraceCorruptError as exc:
        print(f"trace {trace_path} is corrupt: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    run_id = str(events[0].get("run_id", ""))
    try:
        scenario_path = (
            Path(args.scenario) if args.scenario else _infer_scenario(events[0].get("scenario", ""))
        )
        scenario, _ = load_yaml(scenario_path)
        if args.policy:
            base_policy, _ = load_policy(args.policy)
            policy_path = Path(args.policy)
        else:
            policy_ref = resolve_policy(None, scenario_path, scenario)
            base_policy, _ = load_policy(policy_ref)
            policy_path = policy_ref
    except (ConfigError, PolicyError) as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    state_ref = args.state or str(Path("reports") / f"{run_id}.state.json")
    if not Path(state_ref).is_file():
        return fail("E_CONFIG", f"state file not found: {state_ref!r}", EXIT_USAGE)
    try:
        state = load_state_snapshot(state_ref)
    except TraceCorruptError as exc:
        print(f"state {state_ref} is corrupt: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    policy_mtime = os.path.getmtime(policy_path)
    state_digest = hashlib.sha256(Path(state_ref).read_bytes()).hexdigest()

    if args.verdict:
        try:
            verdict_doc = json.loads(Path(args.verdict).read_text(encoding="utf-8"))
            results = verdict_doc["results"]
            overall = verdict_doc.get("verdict", "FAIL")
            if not isinstance(results, list):
                raise KeyError("results")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            return fail("E_CONFIG", f"cannot use --verdict {args.verdict!r}: {exc}", EXIT_USAGE)
    else:
        invariants = scenario.get("invariants", [])
        if not invariants:
            return fail("E_CONFIG", f"scenario {scenario_path} declares no invariants", EXIT_USAGE)
        try:
            outcome = run_verifiers(events, state, invariants)
        except UnknownVerifierError as exc:
            return fail("E_CONFIG", str(exc), EXIT_USAGE)
        except VerifierError as exc:
            print(f"trace {trace_path} cannot be judged: {exc}")
            return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)
        results, overall = outcome["results"], outcome["verdict"]

    if overall == "PASS":
        return fail(
            "E_CONFIG",
            f"No invariant violations to diagnose in trace {trace_path} (verdict PASS)",
            EXIT_USAGE,
        )
    failed = sorted(row["id"] for row in results if not row.get("passed", True))

    patch_doc: dict[str, Any] | None = None
    if os.environ.get("LLAMA_BASE_URL", "").strip():
        summary = f"scenario={scenario.get('scenario')} failed={failed}"
        try:
            patch_doc = fetch_live_patch(
                trace_summary=summary,
                verdict_summary=json.dumps(results, sort_keys=True),
                base_policy=base_policy,
                model=args.model,
            )
        except PolicyRejectedError as exc:
            return fail("E_POLICY_REJECTED", str(exc), EXIT_POLICY_REJECTED)
        if patch_doc is None:
            print(
                "agentcrash diagnose: live model unreachable; "
                "using fixture-backed reference patch",
                file=sys.stderr,
            )
    if patch_doc is None:
        patch_doc = reference_patch(run_id)
    try:
        validate_patch_data(patch_doc)
    except PolicyRejectedError as exc:
        return fail("E_POLICY_REJECTED", str(exc), EXIT_POLICY_REJECTED)
    if len(patch_doc["patch"]["rules"]) > args.max_rules:
        return fail(
            "E_POLICY_REJECTED",
            f"patch proposes {len(patch_doc['patch']['rules'])} rules "
            f"(exceeds --max-rules {args.max_rules})",
            EXIT_POLICY_REJECTED,
        )
    try:
        new_policy, _ = apply_patch(base_policy, patch_doc)
    except PolicyRejectedError as exc:
        return fail("E_POLICY_REJECTED", str(exc), EXIT_POLICY_REJECTED)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(patch_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    if os.path.getmtime(policy_path) != policy_mtime or (
        hashlib.sha256(Path(state_ref).read_bytes()).hexdigest() != state_digest
    ):
        return fail("E_CONFIG", "read-only violation: inputs changed during diagnose", EXIT_USAGE)

    rule = patch_doc["patch"]["rules"][0]
    print("diagnosis: blind retry_same on ambiguous charge_card timeout caused duplicate charge")
    print(
        f"proposed: {rule.get('rule')}/{rule.get('tool')} -> {rule.get('action')}"
        "(get_transaction_status) + require idempotency key"
    )
    print(f"patch={out_path} (policy_patch/v1, {len(patch_doc['patch']['rules'])} rule)")
    print(diff_policies(base_policy, new_policy))
    print(
        f"read-only: ledger {state.get('ledger_hash')} unchanged, "
        f"policy {policy_path} mtime unchanged"
    )
    return EXIT_OK


def cmd_replay(argv: list[str]) -> int:
    """Execute `agentcrash replay` (exit 0 PASS, 1 VERDICT_FAIL, 4 rejected).

    Re-executes the source trace's scenario + seed under ``--policy`` (or
    ``--patch`` applied onto the scenario's base policy), judges the new
    run with the deterministic verifiers, and optionally re-runs every
    golden trace under the new policy as a regression suite. Invalid
    patches fail closed before any run (exit 4, no trace written).
    """
    parser = argparse.ArgumentParser(prog="agentcrash replay")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--policy", default=None)
    parser.add_argument("--patch", default=None)
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--trace-out", default=None)
    parser.add_argument("--state-out", default=None)
    parser.add_argument("--regression", default=str(Path("traces") / "golden"))
    parser.add_argument("--no-regression", action="store_true")
    parser.add_argument("--report-out", default=None)
    args = parser.parse_args(argv)

    def fail(code: str, hint: str, exit_code: int) -> int:
        print(f"agentcrash replay: {code}: {hint}", file=sys.stderr)
        return exit_code

    if bool(args.policy) == bool(args.patch):
        return fail("E_CONFIG", "pass exactly one of --policy or --patch", EXIT_USAGE)
    trace_path = Path(args.trace)
    if not trace_path.is_file():
        return fail("E_CONFIG", f"trace file not found: {args.trace!r}", EXIT_USAGE)
    try:
        events = load_trace_events(str(trace_path))
    except TraceCorruptError as exc:
        print(f"trace {trace_path} is corrupt: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)
    try:
        scenario_path = (
            Path(args.scenario) if args.scenario else _infer_scenario(events[0].get("scenario", ""))
        )
        scenario, scenario_text = load_yaml(scenario_path)
        base_ref = resolve_policy(None, scenario_path, scenario)
        base_policy, _ = load_policy(base_ref)
    except (ConfigError, PolicyError) as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)

    if args.policy:
        try:
            new_policy, new_policy_text = load_policy(args.policy)
        except PolicyError as exc:
            return fail("E_SCHEMA", str(exc), EXIT_USAGE)
        patch_ref: str | None = None
    else:
        try:
            patch_doc = load_patch_doc(args.patch)
            new_policy, new_policy_text = apply_patch(base_policy, patch_doc)
            if args.patch is not None:
                patch_ref = args.patch
            else:
                patch_ref = None
        except PolicyRejectedError as exc:
            return fail("E_POLICY_REJECTED", str(exc), EXIT_POLICY_REJECTED)

    seed = _trace_seed(events, scenario.get("seed", 42))
    max_steps = args.max_steps if args.max_steps is not None else scenario.get("max_steps", 12)
    source_run_id = str(events[0].get("run_id", ""))
    source_state_ref = args.state or str(Path("reports") / f"{source_run_id}.state.json")
    if not Path(source_state_ref).is_file():
        return fail("E_CONFIG", f"state file not found: {source_state_ref!r}", EXIT_USAGE)
    try:
        source_state = load_state_snapshot(source_state_ref)
    except TraceCorruptError as exc:
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)
    invariants = scenario.get("invariants", [])
    if not invariants:
        return fail("E_CONFIG", f"scenario {scenario_path} declares no invariants", EXIT_USAGE)
    try:
        before = run_verifiers(events, source_state, invariants)
    except UnknownVerifierError as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    except VerifierError as exc:
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    replay_id = compute_run_id(scenario_text, new_policy_text, seed)
    replay_trace = args.trace_out or str(Path("traces") / f"{replay_id}.jsonl")
    replay_state = args.state_out or str(Path("reports") / f"{replay_id}.state.json")
    Path(replay_trace).parent.mkdir(parents=True, exist_ok=True)
    Path(replay_state).parent.mkdir(parents=True, exist_ok=True)
    try:
        summary = run_react_loop(
            scenario=scenario,
            policy=new_policy,
            seed=seed,
            trace_path=replay_trace,
            state_path=replay_state,
            scenario_text=scenario_text,
            policy_text=new_policy_text,
            max_steps=max_steps,
            model=args.model,
        )
    except (ValueError, OSError) as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    new_events = load_trace_events(replay_trace)
    new_state = load_state_snapshot(replay_state)
    try:
        after = run_verifiers(new_events, new_state, invariants)
    except VerifierError as exc:
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    before_failed = {row["id"] for row in before["results"] if not row["passed"]}
    after_rows = {row["id"]: row for row in after["results"]}
    fixed = sorted(i for i in before_failed if after_rows.get(i, {}).get("passed"))
    broken = sorted(
        row["id"]
        for row in before["results"]
        if row["passed"] and not after_rows.get(row["id"], {}).get("passed")
    )
    invoices = new_state.get("invoices", [])
    task_status = invoices[0].get("status", "?") if invoices else "?"

    regression: list[dict[str, Any]] = []
    if not args.no_regression:
        reg_dir = Path(args.regression)
        if reg_dir.is_dir():
            for golden in sorted(reg_dir.glob("*.jsonl")):
                regression.append(
                    _regress_golden(
                        golden, new_policy, new_policy_text, model=args.model, max_steps=max_steps
                    )
                )

    report = {
        "api_version": "v1",
        "broken": broken,
        "fixed": fixed,
        "golden": {
            "passed": sum(1 for case in regression if case["verdict"] == "PASS"),
            "total": len(regression),
            "cases": regression,
        },
        "replay_id": replay_id,
        "scenario": scenario.get("scenario"),
        "seed": seed,
        "source_run_id": source_run_id,
        "patch": patch_ref,
        "verdict": after["verdict"],
    }
    report_out = args.report_out or str(Path("reports") / f"{replay_id}.replay.json")
    Path(report_out).parent.mkdir(parents=True, exist_ok=True)
    Path(report_out).write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"replay_id={replay_id} scenario={scenario.get('scenario')} "
        f"seed={seed} verdict={after['verdict']}"
    )
    if before["verdict"] == "FAIL" and after["verdict"] == "PASS":
        fixed_bits = ", ".join(f"{i} PASS" for i in fixed) or "invariants PASS"
        print(f"fix: VERIFIED ({fixed_bits}, task {task_status})")
    elif after["verdict"] == "PASS":
        print("fix: NOTHING_TO_FIX (source already PASS)")
    else:
        print(f"fix: NOT_FIXED (verdict {after['verdict']}, task {task_status})")
    if args.no_regression or not Path(args.regression).is_dir():
        print("regression: skipped (no golden dir)")
    else:
        passed = report["golden"]["passed"]
        total = report["golden"]["total"]
        print(f"regression: {passed}/{total} golden traces still PASS")
        for case in regression:
            print(f"  [{case['verdict']}] {case['trace']}")
    print(f"report={report_out}")
    _ = summary
    regress_ok = all(case["verdict"] == "PASS" for case in regression)
    return EXIT_OK if after["verdict"] == "PASS" and regress_ok else EXIT_VERDICT_FAIL


def _regress_golden(
    golden: Path, policy: dict[str, Any], policy_text: str, *, model: str, max_steps: int
) -> dict[str, Any]:
    """Re-run one golden trace's scenario + seed under ``policy``."""
    try:
        events = load_trace_events(str(golden))
        scenario_path = _infer_scenario(events[0].get("scenario", ""))
        scenario, scenario_text = load_yaml(scenario_path)
        seed = _trace_seed(events, scenario.get("seed", 42))
    except (TraceCorruptError, ConfigError) as exc:
        return {"trace": golden.name, "verdict": "ERROR", "evidence": str(exc)}
    with tempfile.TemporaryDirectory(prefix="agentcrash-regress-") as tmp:
        trace_out = str(Path(tmp) / "trace.jsonl")
        state_out = str(Path(tmp) / "state.json")
        try:
            run_react_loop(
                scenario=scenario,
                policy=policy,
                seed=seed,
                trace_path=trace_out,
                state_path=state_out,
                scenario_text=scenario_text,
                policy_text=policy_text,
                max_steps=max_steps,
                model=model,
            )
            new_events = load_trace_events(trace_out)
            new_state = load_state_snapshot(state_out)
            outcome = run_verifiers(new_events, new_state, scenario.get("invariants", []))
        except (ValueError, OSError, VerifierError, UnknownVerifierError) as exc:
            return {"trace": golden.name, "verdict": "ERROR", "evidence": str(exc)}
    return {"trace": golden.name, "verdict": outcome["verdict"]}


def main(argv: list[str] | None = None) -> int:
    """Dispatch subcommands: run, check, diagnose, replay."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--version" in raw:
        print(f"agentcrash {__version__}")
        return EXIT_OK
    if not raw or raw[0] in ("-h", "--help"):
        print("usage: agentcrash [--version] {run, check, diagnose, replay} ...")
        print("  run: execute an agent against a chaos scenario (offline, stub/fake)")
        print("  check: deterministically verify invariants against a trace")
        print("  diagnose: propose a policy patch for a failing trace (read-only)")
        print("  replay: re-run a trace scenario under a patched policy + regression")
        return EXIT_OK if raw else EXIT_USAGE
    command, rest = raw[0], raw[1:]
    if command == "run":
        return cmd_run(rest)
    if command == "check":
        return cmd_check(rest)
    if command == "diagnose":
        return cmd_diagnose(rest)
    if command == "replay":
        return cmd_replay(rest)
    print(f"agentcrash: unknown command {command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
