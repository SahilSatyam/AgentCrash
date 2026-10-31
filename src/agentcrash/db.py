"""SQLite store for runs, events, verdicts, patches, and ODRL documents.

The trace JSONL files stay the immutable artifact layer; this module is
the query layer the UI reads from. Standard library only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS runs (
  run_key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scenario_id TEXT NOT NULL,
  faults_json TEXT NOT NULL,
  policy_variant TEXT NOT NULL,
  policy_file TEXT NOT NULL,
  seed INTEGER NOT NULL,
  max_steps INTEGER NOT NULL,
  nl_text TEXT NOT NULL,
  nl_label TEXT NOT NULL,
  status TEXT NOT NULL,
  verdict TEXT,
  model_calls INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  virtual_ms INTEGER NOT NULL DEFAULT 0,
  wall_seconds REAL NOT NULL DEFAULT 0,
  ledger_hash TEXT,
  chain_ok INTEGER,
  trace_path TEXT NOT NULL,
  state_path TEXT NOT NULL,
  error_code TEXT,
  error_hint TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL REFERENCES runs(run_key),
  seq INTEGER NOT NULL,
  event_id TEXT NOT NULL,
  step INTEGER NOT NULL,
  tool TEXT,
  phase TEXT,
  outcome TEXT NOT NULL,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  ts_virtual INTEGER NOT NULL DEFAULT 0,
  model_usage_json TEXT,
  injector_json TEXT,
  policy_json TEXT,
  detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_key, seq);
CREATE TABLE IF NOT EXISTS verdicts (
  run_key TEXT NOT NULL REFERENCES runs(run_key),
  invariant_id TEXT NOT NULL,
  passed INTEGER NOT NULL,
  evidence TEXT NOT NULL,
  PRIMARY KEY (run_key, invariant_id)
);
CREATE TABLE IF NOT EXISTS patches (
  run_key TEXT PRIMARY KEY REFERENCES runs(run_key),
  patch_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS odrl_docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL REFERENCES runs(run_key),
  step INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  doc_json TEXT NOT NULL,
  valid INTEGER NOT NULL,
  errors_json TEXT NOT NULL,
  attacker INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_odrl_run ON odrl_docs(run_key);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Create tables idempotently; return the schema version."""
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        return SCHEMA_VERSION
    return int(row[0])


def insert_run(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Insert or replace one finished run and all of its children."""
    conn.execute(
        """
        INSERT OR REPLACE INTO runs (
          run_key, run_id, scenario_id, faults_json, policy_variant, policy_file,
          seed, max_steps, nl_text, nl_label, status, verdict, model_calls,
          prompt_tokens, completion_tokens, total_tokens, virtual_ms, wall_seconds,
          ledger_hash, chain_ok, trace_path, state_path, error_code, error_hint,
          created_at, completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            record["run_key"],
            record["run_id"],
            record["scenario_id"],
            json.dumps(record.get("faults", []), sort_keys=True),
            record["policy_variant"],
            record["policy_file"],
            record["seed"],
            record["max_steps"],
            record["nl_text"],
            record.get("nl_label", "user_supplied"),
            record["status"],
            record.get("verdict"),
            record.get("model_calls", 0),
            record.get("prompt_tokens", 0),
            record.get("completion_tokens", 0),
            record.get("total_tokens", 0),
            record.get("virtual_ms", 0),
            record.get("wall_seconds", 0.0),
            record.get("ledger_hash"),
            record.get("chain_ok"),
            record["trace_path"],
            record["state_path"],
            record.get("error_code"),
            record.get("error_hint"),
            record.get("created_at", utcnow()),
            record.get("completed_at"),
        ),
    )
    run_key = record["run_key"]
    conn.execute("DELETE FROM events WHERE run_key = ?", (run_key,))
    for seq, event in enumerate(record.get("events", [])):
        detail = event.get("detail") or {}
        conn.execute(
            """
            INSERT INTO events (
              run_key, seq, event_id, step, tool, phase, outcome, latency_ms,
              ts_virtual, model_usage_json, injector_json, policy_json, detail_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_key,
                seq,
                event.get("event_id", ""),
                event.get("step", 0),
                event.get("tool"),
                detail.get("phase"),
                event.get("outcome", "info"),
                event.get("latency", 0),
                event.get("ts_virtual", 0),
                (
                    json.dumps(detail["model_usage"], sort_keys=True)
                    if isinstance(detail.get("model_usage"), dict)
                    else None
                ),
                (
                    json.dumps(event["injector_event"], sort_keys=True)
                    if event.get("injector_event") is not None
                    else None
                ),
                (
                    json.dumps(event["policy_decision"], sort_keys=True)
                    if event.get("policy_decision") is not None
                    else None
                ),
                json.dumps(detail, sort_keys=True),
            ),
        )
    conn.execute("DELETE FROM verdicts WHERE run_key = ?", (run_key,))
    for row in record.get("verdict_rows", []):
        conn.execute(
            "INSERT INTO verdicts (run_key, invariant_id, passed, evidence) VALUES (?,?,?,?)",
            (run_key, row["id"], 1 if row["passed"] else 0, row.get("evidence", "")),
        )
    conn.execute("DELETE FROM patches WHERE run_key = ?", (run_key,))
    if record.get("patch") is not None:
        conn.execute(
            "INSERT INTO patches (run_key, patch_json) VALUES (?,?)",
            (run_key, json.dumps(record["patch"], sort_keys=True)),
        )
    conn.execute("DELETE FROM odrl_docs WHERE run_key = ?", (run_key,))
    for doc in record.get("saves", []):
        conn.execute(
            """
            INSERT INTO odrl_docs (
              run_key, step, outcome, doc_json, valid, errors_json, attacker
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                run_key,
                doc.get("step", 0),
                doc.get("outcome", ""),
                json.dumps(doc.get("value"), sort_keys=True),
                1 if doc.get("valid") else 0,
                json.dumps(doc.get("errors", [])),
                1 if doc.get("attacker") else 0,
            ),
        )
    conn.commit()


_RUN_COLUMNS = (
    "run_key, run_id, scenario_id, faults_json, policy_variant, policy_file,"
    " seed, max_steps, nl_text, nl_label, status, verdict, model_calls,"
    " prompt_tokens, completion_tokens, total_tokens, virtual_ms, wall_seconds,"
    " ledger_hash, chain_ok, trace_path, state_path, error_code, error_hint,"
    " created_at, completed_at"
)


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["faults"] = json.loads(record.pop("faults_json") or "[]")
    record["chain_ok"] = None if record["chain_ok"] is None else bool(record["chain_ok"])
    return record


def list_runs(
    conn: sqlite3.Connection,
    scenario: str | None = None,
    verdict: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Newest-first run summaries for selectors and galleries."""
    query = (
        "SELECT run_key, run_id, scenario_id, faults_json, policy_variant, seed,"
        " status, verdict, model_calls, total_tokens, wall_seconds, created_at,"
        " substr(nl_text, 1, 160) AS nl_preview FROM runs"
    )
    clauses: list[str] = []
    args: list[Any] = []
    if scenario:
        clauses.append("scenario_id = ?")
        args.append(scenario)
    if verdict:
        clauses.append("verdict = ?")
        args.append(verdict)
    if status:
        clauses.append("status = ?")
        args.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(limit)
    rows = []
    for row in conn.execute(query, args):
        item = dict(row)
        item["faults"] = json.loads(item.pop("faults_json") or "[]")
        rows.append(item)
    return rows


def get_run(conn: sqlite3.Connection, run_key: str) -> dict[str, Any] | None:
    """Full run snapshot shaped like the live API response."""
    row = conn.execute(f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_key = ?", (run_key,)).fetchone()
    if row is None:
        return None
    record = _run_row(row)
    events = []
    for event in conn.execute(
        "SELECT event_id, step, tool, phase, outcome, latency_ms, ts_virtual,"
        " model_usage_json, injector_json, policy_json, detail_json"
        " FROM events WHERE run_key = ? ORDER BY seq",
        (run_key,),
    ):
        item = dict(event)
        detail = json.loads(item.pop("detail_json") or "{}")
        if item.pop("model_usage_json", None):
            detail.setdefault("model_usage", json.loads(item.get("model_usage_json") or "{}"))
        injector = item.pop("injector_json", None)
        policy = item.pop("policy_json", None)
        events.append(
            {
                "event_id": item["event_id"],
                "step": item["step"],
                "tool": item["tool"],
                "outcome": item["outcome"],
                "latency": item["latency_ms"],
                "ts_virtual": item["ts_virtual"],
                "injector_event": json.loads(injector) if injector else None,
                "policy_decision": json.loads(policy) if policy else None,
                "detail": detail,
                "run_id": record["run_id"],
                "scenario": record["scenario_id"],
                "prev_hash": "",
            }
        )
    verdict_rows = [
        {"id": v["invariant_id"], "passed": bool(v["passed"]), "evidence": v["evidence"]}
        for v in conn.execute(
            "SELECT invariant_id, passed, evidence FROM verdicts WHERE run_key = ?",
            (run_key,),
        )
    ]
    patch_row = conn.execute(
        "SELECT patch_json FROM patches WHERE run_key = ?", (run_key,)
    ).fetchone()
    saves = [
        {
            "step": doc["step"],
            "outcome": doc["outcome"],
            "value": json.loads(doc["doc_json"]),
            "valid": bool(doc["valid"]),
            "errors": json.loads(doc["errors_json"] or "[]"),
            "attacker": bool(doc["attacker"]),
        }
        for doc in conn.execute(
            "SELECT step, outcome, doc_json, valid, errors_json, attacker"
            " FROM odrl_docs WHERE run_key = ? ORDER BY id",
            (run_key,),
        )
    ]
    total = {
        "prompt_tokens": record["prompt_tokens"],
        "completion_tokens": record["completion_tokens"],
        "total_tokens": record["total_tokens"],
    }
    return {
        "run_key": record["run_key"],
        "status": record["status"],
        "spec": {
            "scenario_id": record["scenario_id"],
            "faults": record["faults"],
            "policy_variant": record["policy_variant"],
            "seed": record["seed"],
            "run_id": record["run_id"],
        },
        "fault_summary": [f"{f.get('kind')}" for f in record["faults"] if isinstance(f, dict)],
        "nl_text": record["nl_text"],
        "nl_label": record["nl_label"],
        "events": events,
        "usage_total": total,
        "model_calls": record["model_calls"],
        "verdict": (
            {"verdict": record["verdict"], "results": verdict_rows} if record["verdict"] else None
        ),
        "patch": json.loads(patch_row["patch_json"]) if patch_row else None,
        "saves": saves,
        "chain_ok": record["chain_ok"],
        "wall_seconds": record["wall_seconds"],
        "ledger_hash": record["ledger_hash"],
        "error": (
            {"code": record["error_code"], "hint": record["error_hint"]}
            if record["error_code"]
            else None
        ),
    }


def list_odrl_docs(
    conn: sqlite3.Connection,
    scenario: str | None = None,
    valid: bool | None = None,
    attacker: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """NL-to-ODRL pairs for Policy Studio, newest first."""
    query = (
        "SELECT o.run_key, o.step, o.outcome, o.doc_json, o.valid, o.errors_json,"
        " o.attacker, r.scenario_id, r.policy_variant, r.verdict, r.nl_text,"
        " r.nl_label, r.created_at FROM odrl_docs o"
        " JOIN runs r ON r.run_key = o.run_key"
    )
    clauses: list[str] = []
    args: list[Any] = []
    if scenario:
        clauses.append("r.scenario_id = ?")
        args.append(scenario)
    if valid is not None:
        clauses.append("o.valid = ?")
        args.append(1 if valid else 0)
    if attacker is not None:
        clauses.append("o.attacker = ?")
        args.append(1 if attacker else 0)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY r.created_at DESC, o.id DESC LIMIT ?"
    args.append(limit)
    pairs = []
    for row in conn.execute(query, args):
        item = dict(row)
        item["doc"] = json.loads(item.pop("doc_json"))
        item["errors"] = json.loads(item.pop("errors_json") or "[]")
        item["valid"] = bool(item["valid"])
        item["attacker"] = bool(item["attacker"])
        pairs.append(item)
    return pairs
