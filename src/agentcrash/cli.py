"""AgentCrash command-line entry point (v0: run lands in Phase 2)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from agentcrash import __version__
from agentcrash.adapter import DEFAULT_MODEL, run_react_loop
from agentcrash.injector import FaultSchedule, UnknownFaultError
from agentcrash.tracer import compute_run_id

EXIT_OK = 0
EXIT_USAGE = 2
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


def main(argv: list[str] | None = None) -> int:
    """Dispatch subcommands; `run` is live in Phase 2, the rest follow."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--version" in raw:
        print(f"agentcrash {__version__}")
        return EXIT_OK
    if not raw or raw[0] in ("-h", "--help"):
        print("usage: agentcrash [--version] {run} ...")
        print("  run: execute an agent against a chaos scenario (offline, stub/fake)")
        print("  check/diagnose/replay: land in later M0 phases")
        return EXIT_OK if raw else EXIT_USAGE
    command, rest = raw[0], raw[1:]
    if command == "run":
        return cmd_run(rest)
    if command in ("check", "diagnose", "replay"):
        print(
            f"agentcrash {command}: not implemented yet "
            "(lands in a later M0 phase; 'run' is available).",
            file=sys.stderr,
        )
        return 1
    print(f"agentcrash: unknown command {command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
