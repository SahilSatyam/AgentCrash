"""AgentCrash core command-line entry point: ``check`` + ``serve``.

``check`` verifies registry invariants against an SDK-schema JSONL trace;
``serve`` runs the loopback control plane (Lab + ``/v1`` API).

Host-owned commands (``run`` / ``diagnose`` / ``replay`` / ``seed`` /
``optimize-prompts``) live in the host CLI (e.g. ``odrl_agent.cli``) and
reuse the shared helpers below (exit codes, :class:`ConfigError`,
``resolve_scenario`` / ``resolve_policy`` / ``load_yaml`` /
``validate_run_inputs``). Core never imports host or domain stacks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from agentcrash import __version__
from agentcrash.injector import FaultSchedule, UnknownFaultError
from agentcrash.verifiers import (
    TraceCorruptError,
    UnknownVerifierError,
    VerifierError,
    load_state_snapshot,
    load_trace_events,
    run_verifiers,
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


def cmd_check(argv: list[str]) -> int:
    """Execute ``agentcrash check`` (exit 0 PASS, 1 VERDICT_FAIL, 3 corrupt).

    Verifies registry invariants against an SDK-schema JSONL trace.
    Invariant ids come from ``--invariant`` (repeatable, at least one);
    hosts pass their own ids explicitly instead of via scenario files.
    """
    parser = argparse.ArgumentParser(prog="agentcrash check")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--invariant", action="append", default=[])
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        help="dotted module to import before judging (registers host "
        "invariants, e.g. odrl_agent.verifiers); repeatable",
    )
    parser.add_argument("--state", default=None)
    parser.add_argument("--report-out", default=None)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--scan-pii",
        action="store_true",
        help="scan the raw trace + state files for leaked emails/card-like "
        "runs (redaction placeholders exempt); exit non-zero on any hit",
    )
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

    if not args.invariant:
        return fail(
            "E_CONFIG",
            "no invariants: pass at least one --invariant <id>",
            EXIT_USAGE,
        )
    for plugin in args.plugin:
        try:
            __import__(plugin)
        except ImportError as exc:
            return fail("E_CONFIG", f"cannot import --plugin {plugin!r}: {exc}", EXIT_USAGE)
    trace_path = Path(args.trace)
    if not trace_path.is_file():
        return fail("E_CONFIG", f"trace file not found: {args.trace!r}", EXIT_USAGE)
    try:
        events = load_trace_events(str(trace_path))
    except TraceCorruptError as exc:
        emit(f"trace {trace_path} is corrupt: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    state: dict[str, Any] = {}
    state_ref = args.state
    if state_ref:
        if not Path(state_ref).is_file():
            return fail("E_CONFIG", f"state file not found: {state_ref!r}", EXIT_USAGE)
        try:
            state = load_state_snapshot(state_ref)
        except TraceCorruptError as exc:
            emit(f"state {state_ref} is corrupt: {exc}")
            return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    try:
        outcome = run_verifiers(events, state, args.invariant)
    except UnknownVerifierError as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    except VerifierError as exc:
        emit(f"trace {trace_path} cannot be judged: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    run_id = str(events[0].get("run_id", "core-check")) if events else "core-check"
    report = {
        "api_version": "v1",
        "results": outcome["results"],
        "run_id": run_id,
        "verdict": outcome["verdict"],
    }
    report_out = args.report_out or str(Path("reports") / f"{run_id}.verdict.json")
    Path(report_out).parent.mkdir(parents=True, exist_ok=True)
    Path(report_out).write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    if args.scan_pii:
        from agentcrash.scan_pii import format_hits, scan_files

        targets = [str(trace_path)] + ([state_ref] if state_ref else [])
        hits = scan_files(targets)
        if hits:
            emit(f"PII scan: {len(hits)} raw-PII hit(s) in trace/state:")
            emit(format_hits(hits))
            return fail(
                "E_PII_LEAK",
                f"{len(hits)} raw-PII hit(s); see locations above",
                EXIT_VERDICT_FAIL,
            )
        emit(f"PII scan: clean ({', '.join(targets)})")

    for row in report["results"]:
        mark = "PASS" if row["passed"] else "FAIL"
        verb = "satisfied" if row["passed"] else "violated!"
        emit(f"[{mark}] Invariant {row['id']} {verb} ({row['evidence']})")
    emit(f"verdict={report['verdict']} report={report_out}")
    if as_json:
        print(json.dumps(report, sort_keys=True, indent=2))
    return EXIT_OK if report["verdict"] == "PASS" else EXIT_VERDICT_FAIL


def cmd_serve(argv: list[str]) -> int:
    """Serve the AgentCrash control plane (Lab + ``/v1`` API, local only)."""
    parser = argparse.ArgumentParser(prog="agentcrash serve")
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args(argv)

    from agentcrash.server import serve

    server, port = serve(args.bind, args.port)
    print(f"AgentCrash control plane on http://{args.bind}:{port}/ (Lab + /v1 API)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Dispatch subcommands: check, serve."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--version" in raw:
        print(f"agentcrash {__version__}")
        return EXIT_OK
    if not raw or raw[0] in ("-h", "--help"):
        print("usage: agentcrash [--version] {check, serve} ...")
        print("  check: verify registry invariants against an SDK trace")
        print("  serve: run the control plane (Lab + /v1 API, local only)")
        return EXIT_OK if raw else EXIT_USAGE
    command, rest = raw[0], raw[1:]
    if command == "check":
        return cmd_check(rest)
    if command == "serve":
        return cmd_serve(rest)
    print(f"agentcrash: unknown command {command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
