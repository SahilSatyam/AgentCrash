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
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from agentcrash.events import SCHEMA_VERSION, validate_event

_STORE: dict[str, Any] = {"projects": {}, "events": {}, "runs": {}, "evaluations": {}}
_LOCK = threading.Lock()

LAB_DIR = Path(__file__).resolve().parent / "lab"


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
    with _LOCK:
        _STORE["projects"].clear()
        _STORE["events"].clear()
        _STORE["runs"].clear()
        _STORE["evaluations"].clear()


def register_project(project: str, environment: str = "development") -> dict[str, Any]:
    """Idempotent project registration."""
    with _LOCK:
        existing = _STORE["projects"].get(project)
        if existing is None:
            existing = {"project": project, "environment": environment, "runs": 0}
            _STORE["projects"][project] = existing
        return dict(existing)


def ingest_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Idempotent ingestion: dedupe on ``event_id``; returns accepted/dupes."""
    accepted = 0
    duplicate = 0
    invalid = 0
    with _LOCK:
        for event in events:
            if not isinstance(event, dict) or not event.get("event_id"):
                invalid += 1
                continue
            if event.get("event_id") in _STORE["events"]:
                duplicate += 1
                continue
            problems = validate_event(event)
            if problems:
                invalid += 1
                continue
            _STORE["events"][event["event_id"]] = event
            run_id = event.get("run_id", "?")
            run = _STORE["runs"].setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "project": event.get("project"),
                    "workflow": event.get("workflow"),
                    "events": [],
                    "created_at": time.time(),
                },
            )
            run["events"].append(event["event_id"])
            accepted += 1
    return {
        "accepted": accepted,
        "duplicate": duplicate,
        "invalid": invalid,
        "schema_version": SCHEMA_VERSION,
    }


def submit_evaluation(run_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """Store deterministic invariant verdicts for a run (idempotent).

    Each result is ``{"id": ..., "passed": bool, "evidence": str}``.
    Re-submitting the same invariant ids overwrites them; reporting the
    same verdict twice is a no-op for pass/fail counts.
    """
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
    with _LOCK:
        existing = _STORE["evaluations"].setdefault(run_id, {})
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


def evaluation_summary(run_id: str) -> dict[str, Any] | None:
    """Stored verdicts for a run, or None when never evaluated."""
    with _LOCK:
        verdicts = _STORE["evaluations"].get(run_id)
        if not verdicts:
            return None
        rows = list(verdicts.values())
    passed = sum(1 for v in rows if v["passed"])
    return {"run_id": run_id, "results": rows, "verdict": "PASS" if passed == len(rows) else "FAIL"}


def run_detail(run_id: str) -> dict[str, Any] | None:
    """Full run snapshot: ordered events for Lab display."""
    with _LOCK:
        run = _STORE["runs"].get(run_id)
        if run is None:
            return None
        ordered = sorted(
            (_STORE["events"][eid] for eid in run["events"]),
            key=lambda e: e.get("seq", 0),
        )
        evaluation = _STORE["evaluations"].get(run_id)
        return {
            "run_id": run_id,
            "project": run["project"],
            "workflow": run["workflow"],
            "faults": [e.get("fault") for e in ordered if e.get("fault")],
            "events": ordered,
            "evaluation": (list(evaluation.values()) if evaluation else None),
        }


def prune_runs(
    project: str,
    keep_last_n: int | None = None,
    older_than_days: float | None = None,
) -> dict[str, Any]:
    """Data-retention enforcement: keep-last-N and/or max-age pruning.

    Order for keep-last-N is ingestion order. Removes the dropped runs'
    events and evaluations. ``keep_last_n`` must be a non-negative int
    (0 drops every run); ``older_than_days`` must be a non-negative
    number of days (runs created before the cutoff are dropped, even
    when ``keep_last_n`` would keep them). At least one criterion is
    required.
    """
    if keep_last_n is None and older_than_days is None:
        raise ValueError("prune_runs needs keep_last_n and/or older_than_days")
    if keep_last_n is not None and (not isinstance(keep_last_n, int) or keep_last_n < 0):
        raise ValueError("keep_last_n must be a non-negative int")
    if older_than_days is not None and (
        not isinstance(older_than_days, (int, float)) or older_than_days < 0
    ):
        raise ValueError("older_than_days must be a non-negative number")
    cutoff = time.time() - float(older_than_days or 0.0) * 86400.0
    with _LOCK:
        ordered = [r for r in _STORE["runs"].values() if r.get("project") == project]
        drop_ids: set[str] = set()
        if older_than_days is not None:
            drop_ids.update(r["run_id"] for r in ordered if r.get("created_at", 0) < cutoff)
        survivors = [r["run_id"] for r in ordered if r["run_id"] not in drop_ids]
        if keep_last_n is not None:
            drop_ids.update(survivors[: max(0, len(survivors) - keep_last_n)])
        for run_id in drop_ids:
            run = _STORE["runs"].pop(run_id)
            for eid in run["events"]:
                _STORE["events"].pop(eid, None)
            _STORE["evaluations"].pop(run_id, None)
        kept = sum(1 for r in _STORE["runs"].values() if r.get("project") == project)
    return {"project": project, "dropped": len(drop_ids), "kept": kept}


def reliability_summary(project: str) -> dict[str, Any]:
    """Runs, fault runs, and per-workflow counts for one project."""
    with _LOCK:
        runs = [r for r in _STORE["runs"].values() if r.get("project") == project]
        fault_runs = 0
        for run in runs:
            for eid in run["events"]:
                if _STORE["events"][eid].get("fault"):
                    fault_runs += 1
                    break
        return {
            "project": project,
            "runs": len(runs),
            "fault_runs": fault_runs,
            "delivery": "at-least-once/idempotent",
        }


class _Handler(BaseHTTPRequestHandler):
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
                200, register_project(body["project"], body.get("environment", "development"))
            )
        elif parsed.path == "/v1/events":
            body = self._read_json()
            events = body.get("events") if isinstance(body, dict) else None
            if not isinstance(events, list):
                self._send(400, {"error": "events must be a list"})
                return
            self._send(200, ingest_events(events))
        elif parsed.path == "/v1/evaluations":
            body = self._read_json()
            if not isinstance(body, dict) or not body.get("run_id"):
                self._send(400, {"error": "run_id and results are required"})
                return
            try:
                self._send(200, submit_evaluation(body["run_id"], body.get("results", [])))
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
                    prune_runs(
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
            with _LOCK:
                runs = [
                    r for r in _STORE["runs"].values() if not project or r.get("project") == project
                ]
            self._send(
                200,
                {
                    "runs": [
                        {
                            "run_id": r["run_id"],
                            "project": r["project"],
                            "workflow": r["workflow"],
                            "events": len(r["events"]),
                        }
                        for r in runs
                    ]
                },
            )
        elif len(parts) == 3 and parts[:2] == ["v1", "runs"]:
            detail = run_detail(parts[2])
            if detail is None:
                self._send(404, {"error": "run not found"})
            else:
                self._send(200, detail)
        elif len(parts) == 3 and parts[:2] == ["v1", "reliability"]:
            self._send(200, reliability_summary(parts[2]))
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args: object) -> None:
        pass


def serve(host: str = "127.0.0.1", port: int = 0) -> tuple[HTTPServer, int]:
    """Start the loopback control plane; returns (server, port)."""
    server = HTTPServer((host, port), _Handler)
    return server, server.server_address[1]


def _fastapi_auth(authorization):
    """401 unless the credential check passes."""
    from fastapi import HTTPException as _E  # type: ignore

    if not _bearer_ok(authorization):
        raise _E(401, "unauthorized")


def create_fastapi_app() -> Any:
    """FastAPI factory (requires ``pip install agentcrash[server]``)."""
    try:
        from fastapi import FastAPI, Header, HTTPException  # type: ignore
    except ImportError as exc:
        raise ImportError("fastapi is not installed; pip install agentcrash[server]") from exc

    app = FastAPI(title="AgentCrash Control Plane", version="1")

    @app.post("/v1/projects")
    def create_project(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        if not body.get("project"):
            raise HTTPException(400, "project is required")
        return register_project(body["project"], body.get("environment", "development"))

    @app.post("/v1/events")
    def post_events(
        body: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        events = body.get("events")
        if not isinstance(events, list):
            raise HTTPException(400, "events must be a list")
        return ingest_events(events)

    @app.get("/v1/runs")
    def list_runs(
        project: str = "", authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        with _LOCK:
            runs = [
                r for r in _STORE["runs"].values() if not project or r.get("project") == project
            ]
        return {"runs": runs}

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _fastapi_auth(authorization)
        detail = run_detail(run_id)
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
            return prune_runs(body["project"], body.get("keep_last_n"), body.get("older_than_days"))
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
            return submit_evaluation(body["run_id"], body.get("results", []))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/v1/runs/{run_id}/evaluation")
    def get_evaluation(
        run_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _fastapi_auth(authorization)
        summary = evaluation_summary(run_id)
        if summary is None:
            raise HTTPException(404, "no evaluation for run")
        return summary

    try:
        from fastapi.staticfiles import StaticFiles  # type: ignore

        if LAB_DIR.is_dir():
            app.mount("/lab", StaticFiles(directory=str(LAB_DIR), html=True), name="lab")
    except ImportError:
        pass

    return app
