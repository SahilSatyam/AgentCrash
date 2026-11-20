"""AgentCrash control plane: projects, idempotent ingestion, runs, Lab API.

Transport: versioned ``/v1/*`` JSON over HTTP. Delivery is at-least-once;
ingestion is idempotent on ``event_id`` (replays are deduped, original
order by ``seq`` preserved). Auth: optional bearer token (``api_key``);
without one the loopback server accepts local traffic and marks the
deployment ``untrusted-local`` in responses.

Two servings (same store):
- stdlib ``serve()`` — zero-dependency loopback, used by tests and Lab dev.
- ``create_fastapi_app()`` — FastAPI factory; imports fastapi lazily so
  core installs never require it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agentcrash.events import SCHEMA_VERSION, validate_event

LAB_DIR = Path(__file__).resolve().parent / "lab"


def _validate_ingest_event(event: Any) -> tuple[bool, str | None]:
    """Validate event structural shape and v1 schema; returns (is_valid, event_id)."""
    if not isinstance(event, dict) or not event.get("event_id"):
        return False, None
    if validate_event(event):
        return False, None
    return True, str(event["event_id"])


@runtime_checkable
class Storage(Protocol):
    """Protocol for AgentCrash control-plane storage backends."""

    def register_project(
        self, project: str, environment: str = "development"
    ) -> dict[str, Any]: ...

    def ingest_events(self, events: list[dict[str, Any]]) -> dict[str, Any]: ...

    def submit_evaluation(self, run_id: str, results: list[dict[str, Any]]) -> dict[str, Any]: ...

    def evaluation_summary(self, run_id: str) -> dict[str, Any] | None: ...

    def run_detail(self, run_id: str) -> dict[str, Any] | None: ...

    def list_runs(self, project: str = "") -> list[dict[str, Any]]: ...

    def prune_runs(
        self,
        project: str,
        keep_last_n: int | None = None,
        older_than_days: float | None = None,
    ) -> dict[str, Any]: ...

    def reliability_summary(self, project: str) -> dict[str, Any]: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


class MemoryStorage:
    """Thread-safe in-memory store for tests and fast ephemeral runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._projects: dict[str, Any] = {}
        self._events: dict[str, Any] = {}
        self._runs: dict[str, Any] = {}
        self._evaluations: dict[str, Any] = {}

    def reset(self) -> None:
        with self._lock:
            self._projects.clear()
            self._events.clear()
            self._runs.clear()
            self._evaluations.clear()

    def close(self) -> None:
        pass

    def register_project(self, project: str, environment: str = "development") -> dict[str, Any]:
        with self._lock:
            existing = self._projects.get(project)
            if existing is None:
                existing = {"project": project, "environment": environment, "runs": 0}
                self._projects[project] = existing
            return dict(existing)

    def ingest_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        accepted = 0
        duplicate = 0
        invalid = 0
        with self._lock:
            for event in events:
                ok, eid = _validate_ingest_event(event)
                if not ok or eid is None:
                    invalid += 1
                    continue
                if eid in self._events:
                    duplicate += 1
                    continue
                self._events[eid] = event
                run_id = event.get("run_id", "?")
                run = self._runs.setdefault(
                    run_id,
                    {
                        "run_id": run_id,
                        "project": event.get("project"),
                        "workflow": event.get("workflow"),
                        "events": [],
                        "created_at": time.time(),
                    },
                )
                run["events"].append(eid)
                accepted += 1
        return {
            "accepted": accepted,
            "duplicate": duplicate,
            "invalid": invalid,
            "schema_version": SCHEMA_VERSION,
        }

    def submit_evaluation(self, run_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(results, list):
            raise TypeError("results must be a list")
        clean: dict[str, dict[str, Any]] = {}
        for row in results:
            if not isinstance(row, dict) or not row.get("id"):
                raise ValueError("each result needs an 'id'")
            clean[str(row["id"])] = {
                "id": str(row["id"]),
                "passed": bool(row.get("passed")),
                "evidence": str(row.get("evidence", "")),
            }
        with self._lock:
            existing = self._evaluations.setdefault(run_id, {})
            existing.update(clean)
            verdicts = list(existing.values())
        passed = sum(1 for v in verdicts if v["passed"])
        return {
            "run_id": run_id,
            "invariants": len(verdicts),
            "passed": passed,
            "failed": len(verdicts) - passed,
            "verdict": "PASS" if passed == len(verdicts) else "FAIL",
        }

    def evaluation_summary(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            verdicts = self._evaluations.get(run_id)
            if not verdicts:
                return None
            rows = list(verdicts.values())
        passed = sum(1 for v in rows if v["passed"])
        return {
            "run_id": run_id,
            "results": rows,
            "verdict": "PASS" if passed == len(rows) else "FAIL",
        }

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            ordered = sorted(
                (self._events[eid] for eid in run["events"] if eid in self._events),
                key=lambda e: e.get("seq", 0),
            )
            evaluation = self._evaluations.get(run_id)
            return {
                "run_id": run_id,
                "project": run["project"],
                "workflow": run["workflow"],
                "faults": [e.get("fault") for e in ordered if e.get("fault")],
                "events": ordered,
                "evaluation": (list(evaluation.values()) if evaluation else None),
            }

    def list_runs(self, project: str = "") -> list[dict[str, Any]]:
        with self._lock:
            runs = [r for r in self._runs.values() if not project or r.get("project") == project]
            return [_run_row(r) for r in runs]

    def prune_runs(
        self,
        project: str,
        keep_last_n: int | None = None,
        older_than_days: float | None = None,
    ) -> dict[str, Any]:
        if keep_last_n is None and older_than_days is None:
            raise ValueError("prune_runs needs keep_last_n and/or older_than_days")
        if keep_last_n is not None and (not isinstance(keep_last_n, int) or keep_last_n < 0):
            raise ValueError("keep_last_n must be a non-negative int")
        if older_than_days is not None and (
            not isinstance(older_than_days, (int, float)) or older_than_days < 0
        ):
            raise ValueError("older_than_days must be a non-negative number")
        cutoff = time.time() - float(older_than_days or 0.0) * 86400.0
        with self._lock:
            ordered = [r for r in self._runs.values() if r.get("project") == project]
            drop_ids: set[str] = set()
            if older_than_days is not None:
                drop_ids.update(r["run_id"] for r in ordered if r.get("created_at", 0) <= cutoff)
            survivors = [r["run_id"] for r in ordered if r["run_id"] not in drop_ids]
            if keep_last_n is not None:
                drop_ids.update(survivors[: max(0, len(survivors) - keep_last_n)])
            for run_id in drop_ids:
                run = self._runs.pop(run_id)
                for eid in run["events"]:
                    self._events.pop(eid, None)
                self._evaluations.pop(run_id, None)
            kept = sum(1 for r in self._runs.values() if r.get("project") == project)
        return {"project": project, "dropped": len(drop_ids), "kept": kept}

    def reliability_summary(self, project: str) -> dict[str, Any]:
        with self._lock:
            runs = [r for r in self._runs.values() if r.get("project") == project]
            fault_runs = 0
            for run in runs:
                for eid in run["events"]:
                    ev = self._events.get(eid)
                    if ev and ev.get("fault"):
                        fault_runs += 1
                        break
            return {
                "project": project,
                "runs": len(runs),
                "fault_runs": fault_runs,
                "delivery": "at-least-once/idempotent",
            }


class SQLiteStorage:
    """Persistent SQLite store with WAL mode for local/CI control-plane durability."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = os.environ.get("AGENTCRASH_DB_PATH", "agentcrash.db")
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            p = Path(self.db_path)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project TEXT PRIMARY KEY,
                    environment TEXT,
                    runs INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    project TEXT,
                    workflow TEXT,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    project TEXT,
                    workflow TEXT,
                    seq INTEGER,
                    data TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    run_id TEXT,
                    invariant_id TEXT,
                    passed INTEGER,
                    evidence TEXT,
                    PRIMARY KEY (run_id, invariant_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_events_run_id_seq ON events(run_id, seq);
                CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project, created_at);
                """
            )

    def reset(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM evaluations;")
            self._conn.execute("DELETE FROM events;")
            self._conn.execute("DELETE FROM runs;")
            self._conn.execute("DELETE FROM projects;")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def register_project(self, project: str, environment: str = "development") -> dict[str, Any]:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO projects (project, environment, runs) VALUES (?, ?, 0) "
                "ON CONFLICT(project) DO NOTHING;",
                (project, environment),
            )
            row = self._conn.execute(
                "SELECT project, environment, runs FROM projects WHERE project = ?;",
                (project,),
            ).fetchone()
            return {"project": row[0], "environment": row[1], "runs": row[2]}

    def ingest_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        accepted = 0
        duplicate = 0
        invalid = 0
        with self._lock, self._conn:
            for event in events:
                ok, eid = _validate_ingest_event(event)
                if not ok or eid is None:
                    invalid += 1
                    continue
                cur = self._conn.execute("SELECT 1 FROM events WHERE event_id = ?;", (eid,))
                if cur.fetchone():
                    duplicate += 1
                    continue
                run_id = event.get("run_id", "?")
                self._conn.execute(
                    "INSERT INTO runs (run_id, project, workflow, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET "
                    "project = COALESCE(runs.project, excluded.project), "
                    "workflow = COALESCE(runs.workflow, excluded.workflow);",
                    (run_id, event.get("project"), event.get("workflow"), time.time()),
                )
                self._conn.execute(
                    "INSERT INTO events (event_id, run_id, project, workflow, seq, data) VALUES (?, ?, ?, ?, ?, ?);",
                    (
                        eid,
                        run_id,
                        event.get("project"),
                        event.get("workflow"),
                        event.get("seq", 0),
                        json.dumps(event),
                    ),
                )
                accepted += 1
        return {
            "accepted": accepted,
            "duplicate": duplicate,
            "invalid": invalid,
            "schema_version": SCHEMA_VERSION,
        }

    def submit_evaluation(self, run_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(results, list):
            raise TypeError("results must be a list")
        for row in results:
            if not isinstance(row, dict) or not row.get("id"):
                raise ValueError("each result needs an 'id'")
        with self._lock, self._conn:
            for row in results:
                self._conn.execute(
                    "INSERT INTO evaluations (run_id, invariant_id, passed, evidence) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(run_id, invariant_id) DO UPDATE SET passed=excluded.passed, evidence=excluded.evidence;",
                    (
                        run_id,
                        str(row["id"]),
                        1 if row.get("passed") else 0,
                        str(row.get("evidence", "")),
                    ),
                )
            rows = self._conn.execute(
                "SELECT invariant_id, passed, evidence FROM evaluations WHERE run_id = ?;",
                (run_id,),
            ).fetchall()
        passed = sum(1 for r in rows if r[1])
        return {
            "run_id": run_id,
            "invariants": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "verdict": "PASS" if passed == len(rows) else "FAIL",
        }

    def evaluation_summary(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT invariant_id, passed, evidence FROM evaluations WHERE run_id = ?;",
                (run_id,),
            ).fetchall()
        if not rows:
            return None
        clean = [{"id": r[0], "passed": bool(r[1]), "evidence": r[2]} for r in rows]
        passed = sum(1 for r in clean if r["passed"])
        return {
            "run_id": run_id,
            "results": clean,
            "verdict": "PASS" if passed == len(clean) else "FAIL",
        }

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            run_row = self._conn.execute(
                "SELECT project, workflow FROM runs WHERE run_id = ?;",
                (run_id,),
            ).fetchone()
            if run_row is None:
                return None
            event_rows = self._conn.execute(
                "SELECT data FROM events WHERE run_id = ? ORDER BY seq ASC;",
                (run_id,),
            ).fetchall()
            eval_rows = self._conn.execute(
                "SELECT invariant_id, passed, evidence FROM evaluations WHERE run_id = ?;",
                (run_id,),
            ).fetchall()
        ordered = [json.loads(d[0]) for d in event_rows]
        evaluation = (
            [{"id": r[0], "passed": bool(r[1]), "evidence": r[2]} for r in eval_rows]
            if eval_rows
            else None
        )
        return {
            "run_id": run_id,
            "project": run_row[0],
            "workflow": run_row[1],
            "faults": [e.get("fault") for e in ordered if e.get("fault")],
            "events": ordered,
            "evaluation": evaluation,
        }

    def list_runs(self, project: str = "") -> list[dict[str, Any]]:
        with self._lock:
            if project:
                runs = self._conn.execute(
                    "SELECT run_id, project, workflow FROM runs WHERE project = ? ORDER BY created_at ASC, rowid ASC;",
                    (project,),
                ).fetchall()
            else:
                runs = self._conn.execute(
                    "SELECT run_id, project, workflow FROM runs ORDER BY created_at ASC, rowid ASC;"
                ).fetchall()
            result = []
            for r in runs:
                cnt = self._conn.execute(
                    "SELECT COUNT(*) FROM events WHERE run_id = ?;", (r[0],)
                ).fetchone()[0]
                result.append(
                    {
                        "run_id": r[0],
                        "project": r[1],
                        "workflow": r[2],
                        "events": cnt,
                    }
                )
            return result

    def prune_runs(
        self,
        project: str,
        keep_last_n: int | None = None,
        older_than_days: float | None = None,
    ) -> dict[str, Any]:
        if keep_last_n is None and older_than_days is None:
            raise ValueError("prune_runs needs keep_last_n and/or older_than_days")
        if keep_last_n is not None and (not isinstance(keep_last_n, int) or keep_last_n < 0):
            raise ValueError("keep_last_n must be a non-negative int")
        if older_than_days is not None and (
            not isinstance(older_than_days, (int, float)) or older_than_days < 0
        ):
            raise ValueError("older_than_days must be a non-negative number")
        cutoff = time.time() - float(older_than_days or 0.0) * 86400.0
        with self._lock, self._conn:
            ordered = self._conn.execute(
                "SELECT run_id, created_at FROM runs WHERE project = ? ORDER BY created_at ASC, rowid ASC;",
                (project,),
            ).fetchall()
            drop_ids: set[str] = set()
            if older_than_days is not None:
                drop_ids.update(r[0] for r in ordered if (r[1] or 0.0) <= cutoff)
            survivors = [r[0] for r in ordered if r[0] not in drop_ids]
            if keep_last_n is not None:
                drop_ids.update(survivors[: max(0, len(survivors) - keep_last_n)])
            for rid in drop_ids:
                self._conn.execute("DELETE FROM events WHERE run_id = ?;", (rid,))
                self._conn.execute("DELETE FROM evaluations WHERE run_id = ?;", (rid,))
                self._conn.execute("DELETE FROM runs WHERE run_id = ?;", (rid,))
            kept = self._conn.execute(
                "SELECT COUNT(*) FROM runs WHERE project = ?;", (project,)
            ).fetchone()[0]
        return {"project": project, "dropped": len(drop_ids), "kept": kept}

    def reliability_summary(self, project: str) -> dict[str, Any]:
        with self._lock:
            runs = self._conn.execute(
                "SELECT run_id FROM runs WHERE project = ?;", (project,)
            ).fetchall()
            fault_runs = 0
            for (rid,) in runs:
                events = self._conn.execute(
                    "SELECT data FROM events WHERE run_id = ?;", (rid,)
                ).fetchall()
                for (d_str,) in events:
                    ev = json.loads(d_str)
                    if ev.get("fault"):
                        fault_runs += 1
                        break
        return {
            "project": project,
            "runs": len(runs),
            "fault_runs": fault_runs,
            "delivery": "at-least-once/idempotent",
        }


class PostgresStorage:
    """PostgreSQL storage backend for multi-worker FastAPI control plane."""

    def __init__(self, db_url: str | None = None, conn: Any = None) -> None:
        self.db_url = db_url or os.environ.get(
            "AGENTCRASH_DB_URL", "postgresql://localhost:5432/agentcrash"
        )
        self._lock = threading.Lock()
        if conn is not None:
            self._conn = conn
        else:
            try:
                import psycopg

                self._conn = psycopg.connect(self.db_url, autocommit=True)
            except ImportError:
                try:
                    import psycopg2

                    self._conn = psycopg2.connect(self.db_url)
                    self._conn.autocommit = True
                except ImportError as exc:
                    raise ImportError(
                        "PostgresStorage requires 'psycopg' or 'psycopg2'. "
                        "Install with 'pip install psycopg[binary]' or 'pip install agentcrash[postgres]'."
                    ) from exc
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project TEXT PRIMARY KEY,
                    environment TEXT,
                    runs INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    project TEXT,
                    workflow TEXT,
                    created_at DOUBLE PRECISION
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    project TEXT,
                    workflow TEXT,
                    seq INTEGER,
                    data TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    run_id TEXT,
                    invariant_id TEXT,
                    passed INTEGER,
                    evidence TEXT,
                    PRIMARY KEY (run_id, invariant_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_events_run_id_seq ON events(run_id, seq);
                CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project, created_at);
                """
            )

    def reset(self) -> None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("DELETE FROM evaluations;")
            cur.execute("DELETE FROM events;")
            cur.execute("DELETE FROM runs;")
            cur.execute("DELETE FROM projects;")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def register_project(self, project: str, environment: str = "development") -> dict[str, Any]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (project, environment, runs) VALUES (%s, %s, 0) "
                "ON CONFLICT(project) DO NOTHING;",
                (project, environment),
            )
            cur.execute(
                "SELECT project, environment, runs FROM projects WHERE project = %s;",
                (project,),
            )
            row = cur.fetchone()
            return {"project": row[0], "environment": row[1], "runs": row[2]}

    def ingest_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        accepted = 0
        duplicate = 0
        invalid = 0
        with self._lock, self._conn.cursor() as cur:
            for event in events:
                ok, eid = _validate_ingest_event(event)
                if not ok or eid is None:
                    invalid += 1
                    continue
                cur.execute("SELECT 1 FROM events WHERE event_id = %s;", (eid,))
                if cur.fetchone():
                    duplicate += 1
                    continue
                run_id = event.get("run_id", "?")
                cur.execute(
                    "INSERT INTO runs (run_id, project, workflow, created_at) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT(run_id) DO UPDATE SET "
                    "project = COALESCE(runs.project, EXCLUDED.project), "
                    "workflow = COALESCE(runs.workflow, EXCLUDED.workflow);",
                    (run_id, event.get("project"), event.get("workflow"), time.time()),
                )
                cur.execute(
                    "INSERT INTO events (event_id, run_id, project, workflow, seq, data) VALUES (%s, %s, %s, %s, %s, %s);",
                    (
                        eid,
                        run_id,
                        event.get("project"),
                        event.get("workflow"),
                        event.get("seq", 0),
                        json.dumps(event),
                    ),
                )
                accepted += 1
        return {
            "accepted": accepted,
            "duplicate": duplicate,
            "invalid": invalid,
            "schema_version": SCHEMA_VERSION,
        }

    def submit_evaluation(self, run_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(results, list):
            raise TypeError("results must be a list")
        for row in results:
            if not isinstance(row, dict) or not row.get("id"):
                raise ValueError("each result needs an 'id'")
        with self._lock, self._conn.cursor() as cur:
            for row in results:
                cur.execute(
                    "INSERT INTO evaluations (run_id, invariant_id, passed, evidence) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT(run_id, invariant_id) DO UPDATE SET passed=EXCLUDED.passed, evidence=EXCLUDED.evidence;",
                    (
                        run_id,
                        str(row["id"]),
                        1 if row.get("passed") else 0,
                        str(row.get("evidence", "")),
                    ),
                )
            cur.execute(
                "SELECT invariant_id, passed, evidence FROM evaluations WHERE run_id = %s;",
                (run_id,),
            )
            rows = cur.fetchall()
        passed = sum(1 for r in rows if r[1])
        return {
            "run_id": run_id,
            "invariants": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "verdict": "PASS" if passed == len(rows) else "FAIL",
        }

    def evaluation_summary(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT invariant_id, passed, evidence FROM evaluations WHERE run_id = %s;",
                (run_id,),
            )
            rows = cur.fetchall()
            if not rows:
                return None
            results = [{"id": r[0], "passed": bool(r[1]), "evidence": r[2]} for r in rows]
            passed = sum(1 for r in results if r["passed"])
            return {
                "run_id": run_id,
                "results": results,
                "verdict": "PASS" if passed == len(results) else "FAIL",
            }

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT project, workflow FROM runs WHERE run_id = %s;", (run_id,))
            run_row = cur.fetchone()
            if not run_row:
                return None
            cur.execute(
                "SELECT data FROM events WHERE run_id = %s ORDER BY seq ASC;",
                (run_id,),
            )
            ev_rows = cur.fetchall()
            ordered = [json.loads(r[0]) for r in ev_rows]

            cur.execute(
                "SELECT invariant_id, passed, evidence FROM evaluations WHERE run_id = %s;",
                (run_id,),
            )
            eval_rows = cur.fetchall()
            evaluation = (
                [{"id": r[0], "passed": bool(r[1]), "evidence": r[2]} for r in eval_rows]
                if eval_rows
                else None
            )

            return {
                "run_id": run_id,
                "project": run_row[0],
                "workflow": run_row[1],
                "faults": [e.get("fault") for e in ordered if e.get("fault")],
                "events": ordered,
                "evaluation": evaluation,
            }

    def list_runs(self, project: str = "") -> list[dict[str, Any]]:
        with self._lock, self._conn.cursor() as cur:
            if project:
                cur.execute(
                    "SELECT run_id, project, workflow, created_at FROM runs WHERE project = %s ORDER BY created_at DESC;",
                    (project,),
                )
            else:
                cur.execute(
                    "SELECT run_id, project, workflow, created_at FROM runs ORDER BY created_at DESC;"
                )
            runs = cur.fetchall()
            out = []
            for r in runs:
                rid = r[0]
                cur.execute("SELECT COUNT(*) FROM events WHERE run_id = %s;", (rid,))
                ev_count = cur.fetchone()[0]
                cur.execute("SELECT passed FROM evaluations WHERE run_id = %s;", (rid,))
                eval_rows = cur.fetchall()
                if not eval_rows:
                    verdict = None
                else:
                    passed = all(ev[0] for ev in eval_rows)
                    verdict = "PASS" if passed else "FAIL"
                out.append(
                    {
                        "run_id": rid,
                        "project": r[1],
                        "workflow": r[2],
                        "created_at": r[3],
                        "events": ev_count,
                        "verdict": verdict,
                    }
                )
            return out

    def prune_runs(
        self,
        project: str,
        keep_last_n: int | None = None,
        older_than_days: float | None = None,
    ) -> dict[str, Any]:
        if keep_last_n is None and older_than_days is None:
            raise ValueError("prune_runs needs keep_last_n and/or older_than_days")
        if keep_last_n is not None and (not isinstance(keep_last_n, int) or keep_last_n < 0):
            raise ValueError("keep_last_n must be a non-negative int")
        if older_than_days is not None and (
            not isinstance(older_than_days, (int, float)) or older_than_days < 0
        ):
            raise ValueError("older_than_days must be a non-negative number")
        cutoff = time.time() - float(older_than_days or 0.0) * 86400.0
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, created_at FROM runs WHERE project = %s ORDER BY created_at ASC;",
                (project,),
            )
            rows = cur.fetchall()
            drop_ids: set[str] = set()
            if older_than_days is not None:
                drop_ids.update(r[0] for r in rows if r[1] <= cutoff)
            survivors = [r[0] for r in rows if r[0] not in drop_ids]
            if keep_last_n is not None:
                drop_ids.update(survivors[: max(0, len(survivors) - keep_last_n)])
            for rid in drop_ids:
                cur.execute("DELETE FROM evaluations WHERE run_id = %s;", (rid,))
                cur.execute("DELETE FROM events WHERE run_id = %s;", (rid,))
                cur.execute("DELETE FROM runs WHERE run_id = %s;", (rid,))
            cur.execute("SELECT COUNT(*) FROM runs WHERE project = %s;", (project,))
            kept = cur.fetchone()[0]
        return {"project": project, "dropped": len(drop_ids), "kept": kept}

    def reliability_summary(self, project: str) -> dict[str, Any]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT run_id FROM runs WHERE project = %s;", (project,))
            runs = cur.fetchall()
            fault_runs = 0
            for (rid,) in runs:
                cur.execute("SELECT data FROM events WHERE run_id = %s;", (rid,))
                events = cur.fetchall()
                for (d_str,) in events:
                    ev = json.loads(d_str)
                    if ev.get("fault"):
                        fault_runs += 1
                        break
        return {
            "project": project,
            "runs": len(runs),
            "fault_runs": fault_runs,
            "delivery": "at-least-once/idempotent",
        }


_STORAGE: Storage | None = None


def get_storage() -> Storage:
    """Return the active storage backend (defaults to SQLite WAL)."""
    global _STORAGE
    if _STORAGE is None:
        backend = os.environ.get("AGENTCRASH_STORAGE", "sqlite").lower()
        if backend == "memory":
            _STORAGE = MemoryStorage()
        elif backend in ("postgres", "postgresql") or os.environ.get(
            "AGENTCRASH_DB_URL", ""
        ).startswith(("postgres://", "postgresql://")):
            _STORAGE = PostgresStorage()
        else:
            db_path = os.environ.get("AGENTCRASH_DB_PATH", "agentcrash.db")
            _STORAGE = SQLiteStorage(db_path)
    return _STORAGE


def set_storage(storage: Storage | None) -> None:
    """Explicitly set or reset the active storage backend."""
    global _STORAGE
    _STORAGE = storage


def _allowed_bearers() -> set[str]:
    """Allowlist from ``AGENTCRASH_API_TOKENS`` (comma-separated).

    Empty means loopback mode: all local traffic accepted, deployments
    marked ``untrusted-local``. Non-empty means every ``/v1/*`` API call
    must carry an ``Authorization: Bearer`` credential from the list,
    otherwise it gets 401. Lab static assets stay public so the
    dashboard stays shareable.
    """
    raw = os.environ.get("AGENTCRASH_API_TOKENS", "")
    return {piece.strip() for piece in raw.split(",") if piece.strip()}


def _bearer_ok(header: str | None) -> bool:
    allowed = _allowed_bearers()
    if not allowed:
        return True
    if not header or not header.startswith("Bearer "):
        return False
    return header[len("Bearer ") :].strip() in allowed


def reset_store() -> None:
    """Clear all control-plane state (tests only)."""
    get_storage().reset()


def register_project(project: str, environment: str = "development") -> dict[str, Any]:
    """Idempotent project registration."""
    return get_storage().register_project(project, environment)


def ingest_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Idempotent ingestion: dedupe on ``event_id``; returns accepted/dupes."""
    return get_storage().ingest_events(events)


def submit_evaluation(run_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """Store deterministic invariant verdicts for a run (idempotent).

    Each result is ``{"id": ..., "passed": bool, "evidence": str}``.
    Re-submitting the same invariant ids overwrites them; reporting the
    same verdict twice is a no-op for pass/fail counts.
    """
    return get_storage().submit_evaluation(run_id, results)


def evaluation_summary(run_id: str) -> dict[str, Any] | None:
    """Stored verdicts for a run, or None when never evaluated."""
    return get_storage().evaluation_summary(run_id)


def run_detail(run_id: str) -> dict[str, Any] | None:
    """Full run snapshot: ordered events for Lab display."""
    return get_storage().run_detail(run_id)


def list_runs(project: str = "") -> list[dict[str, Any]]:
    """List runs for Lab or API display."""
    return get_storage().list_runs(project)


def prune_runs(
    project: str,
    keep_last_n: int | None = None,
    older_than_days: float | None = None,
) -> dict[str, Any]:
    """Data-retention enforcement: keep-last-N and/or max-age pruning.

    Order for keep-last-N is ingestion order. Removes the dropped runs'
    events and evaluations. ``keep_last_n`` must be a non-negative int
    (0 drops every run); ``older_than_days`` must be a non-negative
    number of days (runs created at or before the cutoff are dropped,
    even when ``keep_last_n`` would keep them). At least one criterion
    is required.
    """
    return get_storage().prune_runs(project, keep_last_n, older_than_days)


def reliability_summary(project: str) -> dict[str, Any]:
    """Runs, fault runs, and per-workflow counts for one project."""
    return get_storage().reliability_summary(project)


def _run_row(run: dict[str, Any]) -> dict[str, Any]:
    """Slim run summary shared by both servers' ``GET /v1/runs``."""
    return {
        "run_id": run["run_id"],
        "project": run["project"],
        "workflow": run["workflow"],
        "events": len(run["events"]) if isinstance(run["events"], list) else run["events"],
    }


class _Handler(BaseHTTPRequestHandler):
    @property
    def storage(self) -> Storage:
        return getattr(self.server, "storage", None) or get_storage()

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            return None

    def _require_auth(self) -> bool:
        """401 unless the credential check passes."""
        if _bearer_ok(self.headers.get("Authorization")):
            return True
        # Drain the unread body first; replying early makes some TCP
        # stacks (notably Windows) reset the connection mid-request.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._send(401, {"error": "unauthorized"})
        return False

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/v1/") and not self._require_auth():
            return
        if parsed.path == "/v1/projects":
            body = self._read_json()
            if not isinstance(body, dict) or not body.get("project"):
                self._send(400, {"error": "project is required"})
                return
            self._send(
                200,
                self.storage.register_project(
                    body["project"], body.get("environment", "development")
                ),
            )
        elif parsed.path == "/v1/events":
            body = self._read_json()
            events = body.get("events") if isinstance(body, dict) else None
            if not isinstance(events, list):
                self._send(400, {"error": "events must be a list"})
                return
            self._send(200, self.storage.ingest_events(events))
        elif parsed.path == "/v1/evaluations":
            body = self._read_json()
            if not isinstance(body, dict) or not body.get("run_id"):
                self._send(400, {"error": "run_id and results are required"})
                return
            try:
                self._send(
                    200, self.storage.submit_evaluation(body["run_id"], body.get("results", []))
                )
            except (TypeError, ValueError) as exc:
                self._send(400, {"error": str(exc)})
        elif parsed.path == "/v1/admin/prune":
            body = self._read_json()
            if (
                not isinstance(body, dict)
                or not body.get("project")
                or ("keep_last_n" not in body and "older_than_days" not in body)
            ):
                self._send(
                    400,
                    {"error": "project and keep_last_n and/or older_than_days required"},
                )
                return
            try:
                self._send(
                    200,
                    self.storage.prune_runs(
                        body["project"],
                        body.get("keep_last_n"),
                        body.get("older_than_days"),
                    ),
                )
            except (TypeError, ValueError) as exc:
                self._send(400, {"error": str(exc)})
        else:
            self._send(404, {"error": "not found"})

    def _send_lab(self, name: str) -> None:
        """Serve a bundled Lab asset; guards traversal and suffixes."""
        safe = name.strip("/")
        if not safe or ".." in safe or "\\" in safe:
            self._send(404, {"error": "not found"})
            return
        if "/" in safe:
            self._send(404, {"error": "not found"})
            return
        if not safe.endswith((".html", ".js", ".css")):
            self._send(404, {"error": "not found"})
            return
        path = LAB_DIR / safe
        if not path.is_file():
            self._send(404, {"error": "not found"})
            return
        ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[
            safe.rsplit(".", 1)[1]
        ]
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/lab":
            self._send_lab("index.html")
            return
        if parsed.path == "/lab/app.js" or parsed.path == "/lab/styles.css":
            self._send_lab(parsed.path.rsplit("/", 1)[1])
            return
        if parsed.path.startswith("/v1/") and not self._require_auth():
            return
        parts = parsed.path.strip("/").split("/")
        if parts == ["v1", "runs"] or (
            len(parts) == 3 and parts[:2] == ["v1", "runs"] and not parts[2]
        ):
            project = urllib.parse.parse_qs(parsed.query).get("project", [""])[0]
            self._send(200, {"runs": self.storage.list_runs(project)})
        elif len(parts) == 3 and parts[:2] == ["v1", "runs"]:
            detail = self.storage.run_detail(parts[2])
            if detail is None:
                self._send(404, {"error": "run not found"})
            else:
                self._send(200, detail)
        elif len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "evaluation":
            summary = self.storage.evaluation_summary(parts[2])
            if summary is None:
                self._send(404, {"error": "no evaluation for run"})
            else:
                self._send(200, summary)
        elif len(parts) == 3 and parts[:2] == ["v1", "reliability"]:
            self._send(200, self.storage.reliability_summary(parts[2]))
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args: object) -> None:
        pass


def serve(
    host: str = "127.0.0.1",
    port: int = 0,
    storage: Storage | None = None,
) -> tuple[HTTPServer, int]:
    """Start the loopback control plane; returns (server, port)."""
    server = HTTPServer((host, port), _Handler)
    server.storage = storage or get_storage()  # type: ignore[attr-defined]
    return server, server.server_address[1]


def _fastapi_auth(authorization):
    """401 unless the credential check passes."""
    from fastapi import HTTPException as _E  # type: ignore

    if not _bearer_ok(authorization):
        raise _E(401, "unauthorized")


def create_fastapi_app(storage: Storage | None = None) -> Any:
    """FastAPI factory (requires ``pip install agentcrash[server]``)."""
    try:
        from fastapi import FastAPI, Header, HTTPException  # type: ignore
    except ImportError as exc:
        raise ImportError("fastapi is not installed; pip install agentcrash[server]") from exc

    st = storage or get_storage()
    app = FastAPI(title="AgentCrash Control Plane", version="1")

    @app.post("/v1/projects")
    def create_project(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        if not body.get("project"):
            raise HTTPException(400, "project is required")
        return st.register_project(body["project"], body.get("environment", "development"))

    @app.post("/v1/events")
    def post_events(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        events = body.get("events")
        if not isinstance(events, list):
            raise HTTPException(400, "events must be a list")
        return st.ingest_events(events)

    @app.get("/v1/runs")
    def list_runs_route(
        project: str = "", authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        return {"runs": st.list_runs(project)}

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _fastapi_auth(authorization)
        detail = st.run_detail(run_id)
        if detail is None:
            raise HTTPException(404, "run not found")
        return detail

    @app.post("/v1/admin/prune")
    def admin_prune(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        if (
            not isinstance(body, dict)
            or not body.get("project")
            or ("keep_last_n" not in body and "older_than_days" not in body)
        ):
            raise HTTPException(400, "project and keep_last_n and/or older_than_days required")
        try:
            return st.prune_runs(
                body["project"], body.get("keep_last_n"), body.get("older_than_days")
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/evaluations")
    def post_evaluation(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        if not body.get("run_id"):
            raise HTTPException(400, "run_id and results are required")
        try:
            return st.submit_evaluation(body["run_id"], body.get("results", []))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/v1/runs/{run_id}/evaluation")
    def get_evaluation(
        run_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        summary = st.evaluation_summary(run_id)
        if summary is None:
            raise HTTPException(404, "no evaluation for run")
        return summary

    @app.get("/v1/reliability/{project}")
    def get_reliability(
        project: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        return st.reliability_summary(project)

    try:
        from fastapi.staticfiles import StaticFiles  # type: ignore

        if LAB_DIR.is_dir():
            app.mount("/lab", StaticFiles(directory=str(LAB_DIR), html=True), name="lab")
    except ImportError:
        pass

    return app

    try:
        from fastapi.staticfiles import StaticFiles  # type: ignore

        if LAB_DIR.is_dir():
            app.mount("/lab", StaticFiles(directory=str(LAB_DIR), html=True), name="lab")
    except ImportError:
        pass

    return app
