"""Loopback web API for interactive NL-to-ODRL conversion, backed by SQLite.

Endpoints (same server that serves ``ui/`` statically):

- ``GET /api/faults`` lists the generalized fault catalog.
- ``POST /api/convert`` with JSON ``{nl_text, faults|fault,
  policy_variant, seed, max_steps}`` starts a live agent run in a
  background thread and returns ``{run_key, run_id, status}``.
- ``GET /api/runs`` lists recorded runs (filters: scenario, verdict,
  status, limit); ``GET /api/runs/<run_key>`` returns one full snapshot.
- ``GET /api/odrl`` lists NL-to-ODRL pairs for Policy Studio.

Finished runs are recorded in ``agentcrash.db`` (stdlib sqlite3); the
trace JSONL files remain the immutable artifact layer.
"""

from __future__ import annotations

import copy
import functools
import http.server
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from agentcrash import db as db_module
from agentcrash.adapter import (
    ModelNotConfiguredError,
    run_react_loop,
)
from agentcrash.injector import (
    FAULT_CATALOG,
    UnknownFaultError,
    catalog_entry,
    validate_fault_entry,
)
from agentcrash.odrl import contains_attacker_grant, validate_odrl_subset
from agentcrash.policy import PolicyRejectedError, reference_patch_for, validate_patch_data
from agentcrash.tracer import compute_run_id, verify_chain
from agentcrash.verifiers import (
    TraceCorruptError,
    UnknownVerifierError,
    VerifierError,
    load_state_snapshot,
    run_verifiers,
)

INLINE_DOC_ID = "pasted-request-1"

INLINE_GUIDANCE = (
    "\n\nThe request to convert is pasted inline as document "
    f"'{INLINE_DOC_ID}'. Its parse result carries no candidate ODRL "
    "(empty object): author the ODRL 2.2 JSON-LD document yourself "
    "from the request text, then follow the policy protocol below."
)

BASE_SCENARIO = "odrl_convert"
CONCURRENT_SCENARIO = "odrl_convert_concurrent"

NAIVE_POLICY = "policies/default.yaml"
FIXED_POLICIES = {
    "odrl_convert": "policies/fixed-odrl-convert.yaml",
    "odrl_convert_timeout": "policies/fixed-odrl-timeout.yaml",
    "odrl_convert_concurrent": "policies/fixed-odrl-convert.yaml",
}

PRESETS = {
    "none": [],
    "timeout": [{"kind": "timeout_after_commit"}],
    "concurrent": [{"kind": "concurrent_update"}],
}

MAX_NL_CHARS = 4000
MAX_ACTIVE_RUNS = 2


class ConvertError(ValueError):
    """Bad convert request (HTTP 400)."""


def find_repo_root(start: Path | None = None) -> Path | None:
    """Locate the checkout root by searching for ``ui/index.html`` upward."""
    here = start or Path.cwd()
    for candidate in [here, *list(here.parents)[:3]]:
        if (candidate / "ui" / "index.html").is_file():
            return candidate
    return None


def load_yaml(path: Path) -> tuple[dict[str, Any], str]:
    """Parse a YAML file, returning (data, raw text)."""
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}, text


def fault_catalog() -> list[dict[str, Any]]:
    """Return the generalized fault catalog for the UI picker."""
    return [
        {
            "kind": entry["kind"],
            "description": entry["description"],
            "effect": entry["effect"],
            "inline_supported": entry["inline_supported"],
            "params_schema": entry["params_schema"],
            "default_params": entry["default_params"],
        }
        for entry in FAULT_CATALOG
    ]


def normalize_faults(raw: Any) -> list[dict[str, Any]]:
    """Validate user-supplied faults against the catalog (generalized)."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ConvertError("faults must be a list of {kind, params}")
    normalized = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConvertError("each fault must be an object with kind/params")
        try:
            entry = validate_fault_entry(item.get("kind"), item.get("params", {}))
        except UnknownFaultError as exc:
            raise ConvertError(str(exc)) from exc
        catalog = catalog_entry(str(entry["kind"]))
        if not catalog.get("inline_supported"):
            raise ConvertError(
                f"fault {entry['kind']!r} needs fixture bytes and cannot run on pasted text"
            )
        normalized.append(entry)
    return normalized


def validate_convert(body: Any) -> tuple[str, list[dict[str, Any]], str, int, int]:
    """Return (nl_text, fault_entries, policy_variant, seed, max_steps)."""
    if not isinstance(body, dict):
        raise ConvertError("request body must be a JSON object")
    nl_text = body.get("nl_text", "")
    if not isinstance(nl_text, str) or not nl_text.strip():
        raise ConvertError("nl_text must be a non-empty string")
    if len(nl_text) > MAX_NL_CHARS:
        raise ConvertError(f"nl_text exceeds {MAX_NL_CHARS} characters")
    if "faults" in body:
        fault_entries = normalize_faults(body.get("faults"))
    else:
        preset = body.get("fault", "none")
        if preset not in PRESETS:
            raise ConvertError(f"fault must be one of {sorted(PRESETS)}")
        fault_entries = normalize_faults(PRESETS[preset])
    variant = body.get("policy_variant", "naive")
    if variant not in ("naive", "fixed"):
        raise ConvertError('policy_variant must be "naive" or "fixed"')
    seed = body.get("seed", 42)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise ConvertError("seed must be an integer in 0..2147483647")
    max_steps = body.get("max_steps", 12)
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 24:
        raise ConvertError("max_steps must be an integer in 1..24")
    return nl_text.strip(), fault_entries, variant, seed, max_steps


def _patch_key_for(kinds: set[str]) -> str:
    """Pick the reference-patch scenario for a fault set (primary first)."""
    if "timeout_after_commit" in kinds or "timeout_before_commit" in kinds:
        return "odrl_convert_timeout"
    if "concurrent_update" in kinds:
        return "odrl_convert_concurrent"
    return "odrl_convert"


def _fixed_policy_for(kinds: set[str]) -> str:
    if "timeout_after_commit" in kinds or "timeout_before_commit" in kinds:
        return FIXED_POLICIES["odrl_convert_timeout"]
    if "concurrent_update" in kinds:
        return FIXED_POLICIES["odrl_convert_concurrent"]
    return FIXED_POLICIES["odrl_convert"]


def _lost_update_invariant(root: Path) -> dict[str, Any] | None:
    path = root / "scenarios" / CONCURRENT_SCENARIO / "scenario.yaml"
    data, _ = load_yaml(path)
    for invariant in data.get("invariants", []):
        if isinstance(invariant, dict) and invariant.get("id") == "no_lost_update":
            return copy.deepcopy(invariant)
    return None


def build_inline_run(
    nl_text: str,
    fault_entries: list[dict[str, Any]],
    variant: str,
    seed: int,
    max_steps: int,
    root: Path,
    attempt: int,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the scenario/policy/paths for one pasted-NL run."""
    kinds = {str(f["kind"]) for f in fault_entries}
    scenario_path = root / "scenarios" / BASE_SCENARIO / "scenario.yaml"
    policy_rel = NAIVE_POLICY if variant == "naive" else _fixed_policy_for(kinds)
    policy_path = root / policy_rel
    scenario, scenario_text = load_yaml(scenario_path)
    policy, policy_text = load_yaml(policy_path)
    scenario = copy.deepcopy(scenario)
    scenario["task"] = str(scenario.get("task", "")) + INLINE_GUIDANCE
    env = scenario.setdefault("environment", {})
    env["nl_docs"] = [{"id": INLINE_DOC_ID, "text": nl_text, "label": "user_supplied"}]
    scenario["faults"] = [
        {"at_step": f.get("at_step"), "kind": f["kind"], "params": dict(f.get("params", {}))}
        for f in fault_entries
    ]
    if "concurrent_update" in kinds and not any(
        i.get("id") == "no_lost_update" for i in scenario.get("invariants", [])
    ):
        invariant = _lost_update_invariant(root)
        if invariant is not None:
            scenario.setdefault("invariants", []).append(invariant)
    out = out_dir or root
    faults_hash = json.dumps(fault_entries, sort_keys=True)
    run_id = compute_run_id(
        f"{scenario_text}\nui_attempt: {attempt}\nfaults: {faults_hash}", policy_text, seed
    )
    return {
        "scenario": scenario,
        "scenario_text": scenario_text,
        "policy": policy,
        "policy_text": policy_text,
        "scenario_id": BASE_SCENARIO,
        "patch_scenario_id": _patch_key_for(kinds),
        "policy_file": policy_rel,
        "faults": fault_entries,
        "variant": variant,
        "seed": seed,
        "max_steps": max_steps,
        "run_id": run_id,
        "nl_text": nl_text,
        "nl_label": "user_supplied",
        "trace_path": str(out / "traces" / f"{run_id}.jsonl"),
        "state_path": str(out / "reports" / f"{run_id}.state.json"),
        "db_path": str(out / "agentcrash.db"),
    }


def read_events(trace_path: str) -> list[dict[str, Any]]:
    """Read trace events tolerantly (the file grows while a run is live)."""
    events: list[dict[str, Any]] = []
    try:
        with open(trace_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def sum_usage(events: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
    """Return (usage totals, model call count) from tool_call details."""
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    calls = 0
    for event in events:
        usage = (event.get("detail") or {}).get("model_usage")
        if isinstance(usage, dict):
            calls += 1
            for key in total:
                value = usage.get(key, 0)
                if isinstance(value, int):
                    total[key] += value
    return total, calls


def summarize_saves(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per committed save: value, frozen-subset validity, attacker flag."""
    saves = []
    for event in events:
        if event.get("tool") != "save_odrl":
            continue
        if event.get("detail", {}).get("phase") != "tool_result":
            continue
        if event.get("outcome") == "error":
            continue
        if (event.get("args") or {}).get("op") != "write":
            continue
        value = (event.get("detail") or {}).get("data", {}).get("value")
        valid, errors = validate_odrl_subset(value)
        saves.append(
            {
                "step": event.get("step"),
                "outcome": event.get("outcome"),
                "value": value,
                "valid": valid,
                "errors": errors,
                "attacker": bool(contains_attacker_grant(value)),
            }
        )
    return saves


def _snapshot_from_parts(
    run_key: str,
    spec: dict[str, Any],
    status: str,
    events: list[dict[str, Any]],
    outcome: dict[str, Any] | None,
    patch_doc: dict[str, Any] | None,
    chain_ok: bool | None,
    wall_seconds: float,
    error: dict[str, str] | None,
) -> dict[str, Any]:
    usage_total, model_calls = sum_usage(events)
    virtual_ms = events[-1].get("ts_virtual", 0) if events else 0
    results = (outcome or {}).get("results", [])
    return {
        "run_key": run_key,
        "status": status,
        "spec": {
            "scenario_id": spec["scenario_id"],
            "faults": spec["faults"],
            "policy_variant": spec["variant"],
            "seed": spec["seed"],
            "run_id": spec["run_id"],
        },
        "fault_summary": [str(f.get("kind")) for f in spec["faults"]],
        "nl_text": spec["nl_text"],
        "nl_label": spec.get("nl_label", "user_supplied"),
        "events": events,
        "usage_total": usage_total,
        "model_calls": model_calls,
        "verdict": {"verdict": outcome["verdict"], "results": results} if outcome else None,
        "patch": patch_doc,
        "saves": summarize_saves(events),
        "chain_ok": chain_ok,
        "wall_seconds": wall_seconds,
        "error": error,
        "_virtual_ms": virtual_ms,
    }


def _record_snapshot(
    db_path: str, run_key: str, snap: dict[str, Any], spec: dict[str, Any]
) -> None:
    conn = db_module.connect(Path(db_path))
    try:
        db_module.migrate(conn)
        results = (snap["verdict"] or {}).get("results", [])
        db_module.insert_run(
            conn,
            {
                "run_key": run_key,
                "run_id": spec["run_id"],
                "scenario_id": spec["scenario_id"],
                "faults": spec["faults"],
                "policy_variant": spec["variant"],
                "policy_file": spec["policy_file"],
                "seed": spec["seed"],
                "max_steps": spec["max_steps"],
                "nl_text": spec["nl_text"],
                "nl_label": spec.get("nl_label", "user_supplied"),
                "status": snap["status"],
                "verdict": (snap["verdict"] or {}).get("verdict"),
                "model_calls": snap["model_calls"],
                "prompt_tokens": snap["usage_total"]["prompt_tokens"],
                "completion_tokens": snap["usage_total"]["completion_tokens"],
                "total_tokens": snap["usage_total"]["total_tokens"],
                "virtual_ms": snap["_virtual_ms"],
                "wall_seconds": snap["wall_seconds"],
                "ledger_hash": snap.get("ledger_hash"),
                "chain_ok": snap["chain_ok"],
                "trace_path": spec["trace_path"],
                "state_path": spec["state_path"],
                "error_code": (snap["error"] or {}).get("code"),
                "error_hint": (snap["error"] or {}).get("hint"),
                "created_at": spec.get("created_at", db_module.utcnow()),
                "completed_at": db_module.utcnow(),
                "events": snap["events"],
                "verdict_rows": results,
                "patch": snap["patch"],
                "saves": snap["saves"],
            },
        )
    finally:
        conn.close()


def _scripted_transport(actions: list[dict[str, Any]]):
    """Fake urlopen serving canned chat-completions actions (offline runs)."""

    class _FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    state = {"i": 0}

    def _fake_urlopen(request: Any, timeout: Any = None) -> Any:
        idx = state["i"]
        action = actions[idx] if idx < len(actions) else {"type": "done", "reason": "end"}
        state["i"] += 1
        return _FakeResponse({"choices": [{"message": {"content": json.dumps(action)}}]})

    return _fake_urlopen


def run_and_store(
    spec: dict[str, Any],
    run_key: str,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute one spec (live, or scripted when actions given); record to DB."""
    started = time.monotonic()
    created = db_module.utcnow()
    spec = dict(spec, created_at=created)
    old_key = os.environ.get("LLAMA_API_KEY")
    old_url = os.environ.get("LLAMA_BASE_URL")
    old_urlopen = urllib.request.urlopen
    scripted = actions is not None
    if scripted:
        os.environ["LLAMA_API_KEY"] = "seed-key"
        os.environ["LLAMA_BASE_URL"] = "https://seed.example/v1"
        urllib.request.urlopen = _scripted_transport(actions or [])
    try:
        Path(spec["trace_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(spec["state_path"]).parent.mkdir(parents=True, exist_ok=True)
        run_react_loop(
            scenario=spec["scenario"],
            policy=spec["policy"],
            seed=spec["seed"],
            trace_path=spec["trace_path"],
            state_path=spec["state_path"],
            scenario_text=spec["scenario_text"],
            policy_text=spec["policy_text"],
            max_steps=spec["max_steps"],
        )
    except ModelNotConfiguredError as exc:
        events = read_events(spec["trace_path"])
        snap = _snapshot_from_parts(
            run_key,
            spec,
            "error",
            events,
            None,
            None,
            None,
            time.monotonic() - started,
            {"code": "E_MODEL_API", "hint": f"{exc}. Set the model key and URL."},
        )
        snap["ledger_hash"] = None
        _record_snapshot(spec["db_path"], run_key, snap, spec)
        return _public_snapshot(snap)
    except (UnknownFaultError, VerifierError, TraceCorruptError, UnknownVerifierError) as exc:
        events = read_events(spec["trace_path"])
        snap = _snapshot_from_parts(
            run_key,
            spec,
            "error",
            events,
            None,
            None,
            None,
            time.monotonic() - started,
            {"code": "E_RUN", "hint": f"{type(exc).__name__}: {exc}"},
        )
        snap["ledger_hash"] = None
        _record_snapshot(spec["db_path"], run_key, snap, spec)
        return _public_snapshot(snap)
    finally:
        if scripted:
            if old_key is None:
                os.environ.pop("LLAMA_API_KEY", None)
            else:
                os.environ["LLAMA_API_KEY"] = old_key
            if old_url is None:
                os.environ.pop("LLAMA_BASE_URL", None)
            else:
                os.environ["LLAMA_BASE_URL"] = old_url
            urllib.request.urlopen = old_urlopen
    events = read_events(spec["trace_path"])
    state = load_state_snapshot(spec["state_path"])
    outcome = run_verifiers(events, state, spec["scenario"].get("invariants", []))
    patch_doc = None
    if outcome["verdict"] == "FAIL":
        try:
            candidate = reference_patch_for(spec["patch_scenario_id"], spec["run_id"])
            validate_patch_data(candidate)
            patch_doc = candidate
        except (ValueError, PolicyRejectedError):
            patch_doc = None
    chain_ok = bool(verify_chain(spec["trace_path"]).get("ok"))
    snap = _snapshot_from_parts(
        run_key,
        spec,
        "done",
        events,
        outcome,
        patch_doc,
        chain_ok,
        time.monotonic() - started,
        None,
    )
    snap["ledger_hash"] = state.get("ledger_hash")
    _record_snapshot(spec["db_path"], run_key, snap, spec)
    return _public_snapshot(snap)


def _public_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    """Strip internal bookkeeping before serving."""
    public = dict(snap)
    public.pop("_virtual_ms", None)
    return public


def _db_conn(root: Path, out_dir: Path | None = None) -> Any:
    out = out_dir or root
    conn = db_module.connect(out / "agentcrash.db")
    db_module.migrate(conn)
    return conn


class RunRegistry:
    """Track background convert runs; at most MAX_ACTIVE_RUNS at once."""

    def __init__(self, root: Path, out_dir: Path | None = None) -> None:
        self._root = root
        self._out_dir = out_dir
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._active = 0

    def start(
        self, nl_text: str, faults: list[dict[str, Any]], variant: str, seed: int, max_steps: int
    ) -> dict[str, Any]:
        """Launch a run in a background thread; return its public record."""
        with self._lock:
            if self._active >= MAX_ACTIVE_RUNS:
                raise ConvertError("a conversion is already running; try again shortly")
            self._seq += 1
            attempt = self._seq
            self._active += 1
        spec = build_inline_run(
            nl_text, faults, variant, seed, max_steps, self._root, attempt, self._out_dir
        )
        run_key = f"{spec['run_id']}-{attempt}"
        record: dict[str, Any] = {
            "run_key": run_key,
            "status": "running",
            "spec": spec,
            "started_at": time.monotonic(),
            "snapshot": None,
        }
        with self._lock:
            self._runs[run_key] = record
        thread = threading.Thread(target=self._execute, args=(run_key, spec), daemon=True)
        thread.start()
        return {"run_key": run_key, "run_id": spec["run_id"], "status": "running"}

    def _execute(self, run_key: str, spec: dict[str, Any]) -> None:
        try:
            snap = run_and_store(spec, run_key)
        except Exception as exc:  # noqa: BLE001 - record, never drop a run
            snap = _public_snapshot(
                _snapshot_from_parts(
                    run_key,
                    spec,
                    "error",
                    read_events(spec["trace_path"]),
                    None,
                    None,
                    None,
                    0.0,
                    {"code": "E_RUN", "hint": f"{type(exc).__name__}: {exc}"},
                )
            )
        with self._lock:
            record = self._runs[run_key]
            record["status"] = snap["status"]
            record["snapshot"] = snap
            self._active -= 1

    def snapshot(self, run_key: str) -> dict[str, Any] | None:
        """Live snapshot while running, DB snapshot once finished."""
        with self._lock:
            record = self._runs.get(run_key)
        if record is not None and record["status"] == "running":
            spec = record["spec"]
            events = read_events(spec["trace_path"])
            usage_total, model_calls = sum_usage(events)
            return {
                "run_key": run_key,
                "status": "running",
                "spec": {
                    "scenario_id": spec["scenario_id"],
                    "faults": spec["faults"],
                    "policy_variant": spec["variant"],
                    "seed": spec["seed"],
                    "run_id": spec["run_id"],
                },
                "fault_summary": [str(f.get("kind")) for f in spec["faults"]],
                "nl_text": spec["nl_text"],
                "nl_label": spec.get("nl_label", "user_supplied"),
                "events": events,
                "usage_total": usage_total,
                "model_calls": model_calls,
                "verdict": None,
                "patch": None,
                "saves": summarize_saves(events),
                "chain_ok": None,
                "wall_seconds": time.monotonic() - record["started_at"],
                "error": None,
            }
        if record is not None and record["snapshot"] is not None:
            return record["snapshot"]
        conn = _db_conn(self._root, self._out_dir)
        try:
            return db_module.get_run(conn, run_key)
        finally:
            conn.close()

    def wait(self, run_key: str, timeout: float = 120.0) -> dict[str, Any] | None:
        """Block until a run leaves 'running' (test helper); else None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.snapshot(run_key)
            if snapshot is None or snapshot["status"] != "running":
                return snapshot
            time.sleep(0.05)
        return self.snapshot(run_key)


def _query_flags(query: dict[str, list[str]]) -> dict[str, Any]:
    def first(name: str) -> str | None:
        values = query.get(name)
        return values[0] if values else None

    def flag(name: str) -> bool | None:
        value = first(name)
        if value is None or value == "":
            return None
        return value.lower() in ("1", "true", "yes")

    limit_raw = first("limit")
    try:
        limit = max(1, min(200, int(limit_raw))) if limit_raw else 100
    except ValueError:
        limit = 100
    return {
        "scenario": first("scenario") or None,
        "verdict": first("verdict") or None,
        "status": first("status") or None,
        "valid": flag("valid"),
        "attacker": flag("attacker"),
        "limit": limit,
    }


class ConvertHandler(http.server.SimpleHTTPRequestHandler):
    """Serve ui/ statically plus the convert/runs/odrl/faults API."""

    registry: RunRegistry | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send_json(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _db(self) -> Any:
        assert ConvertHandler.registry is not None
        return _db_conn(ConvertHandler.registry._root, ConvertHandler.registry._out_dir)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/convert":
            self._send_json({"error": "E_CONFIG", "hint": "unknown endpoint"}, 404)
            return
        assert ConvertHandler.registry is not None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self._send_json({"error": "E_CONFIG", "hint": "body must be 1..65536 bytes"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "E_CONFIG", "hint": "body must be JSON"}, 400)
            return
        try:
            nl_text, faults, variant, seed, max_steps = validate_convert(body)
        except ConvertError as exc:
            self._send_json({"error": "E_CONFIG", "hint": str(exc)}, 400)
            return
        try:
            record = ConvertHandler.registry.start(nl_text, faults, variant, seed, max_steps)
        except ConvertError as exc:
            self._send_json({"error": "E_BUSY", "hint": str(exc)}, 429)
            return
        self._send_json(record)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/faults":
            self._send_json({"faults": fault_catalog()})
            return
        if parsed.path == "/api/runs":
            flags = _query_flags(urllib.parse.parse_qs(parsed.query))
            conn = self._db()
            try:
                runs = db_module.list_runs(
                    conn,
                    scenario=flags["scenario"],
                    verdict=flags["verdict"],
                    status=flags["status"],
                    limit=flags["limit"],
                )
            finally:
                conn.close()
            self._send_json({"runs": runs})
            return
        if parsed.path.startswith("/api/runs/"):
            assert ConvertHandler.registry is not None
            run_key = parsed.path[len("/api/runs/") :]
            snapshot = ConvertHandler.registry.snapshot(run_key)
            if snapshot is None:
                self._send_json({"error": "E_CONFIG", "hint": "unknown run_key"}, 404)
                return
            self._send_json(snapshot)
            return
        if parsed.path == "/api/odrl":
            flags = _query_flags(urllib.parse.parse_qs(parsed.query))
            conn = self._db()
            try:
                pairs = db_module.list_odrl_docs(
                    conn,
                    scenario=flags["scenario"],
                    valid=flags["valid"],
                    attacker=flags["attacker"],
                    limit=flags["limit"],
                )
            finally:
                conn.close()
            self._send_json({"pairs": pairs})
            return
        super().do_GET()


def serve_ui(port: int, bind: str, ui_dir: Path) -> int:
    """Serve the UI directory plus the API; migrate the DB; block on Ctrl+C."""
    from agentcrash.cli import EXIT_OK

    root = ui_dir.parent
    conn = _db_conn(root)
    conn.close()
    handler = functools.partial(ConvertHandler, directory=str(ui_dir))
    ConvertHandler.registry = RunRegistry(root)
    server = http.server.ThreadingHTTPServer((bind, port), handler)
    print(f"AgentCrash UI: http://{bind}:{port}/ (serving {ui_dir}, Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK
