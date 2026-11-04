"""AgentCrash SDK runtime: instrument host workflows without owning them.

Framework-neutral, minimal overhead, graceful degradation:

- Disabled mode (``AGENTCRASH_DISABLED=1`` or ``enabled=False``) makes every
  API a no-op passthrough; host logic keeps working.
- Offline mode (no endpoint) captures traces locally; nothing blocks.
- Buffered exporter: bounded in-memory queue, drop-oldest with a counter,
  batched flush (batch threshold or run end, never per step) when an
  endpoint is configured; network failures are swallowed and counted,
  never raised into host code.
- Delivery is at-least-once; the server dedupes on ``event_id``.
- Hash chain (``prev_hash`` over canonical JSON) is an integrity signal for
  accidental loss/reordering, NOT tamper resistance (no signing in v1).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import queue
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self
from uuid import uuid4

from agentcrash.events import make_event, validate_event
from agentcrash.tracer import redact


@dataclass
class WorkflowConfig:
    """Host-supplied SDK configuration."""

    project: str = "default"
    environment: str = "development"
    fault_injection: bool = False
    enabled: bool = True
    endpoint: str | None = None
    api_key: str | None = None
    max_queue: int = 1000
    seed: int | None = None


@dataclass
class FaultPolicy:
    """Deterministic fault decisions for named boundaries.

    ``faults`` maps boundary name -> fault descriptor, e.g.
    ``{"save_odrl": {"kind": "timeout_after_commit"}}``. Decisions are
    seeded and reproducible; faults only fire when the SDK was created
    with ``fault_injection=True``.
    """

    faults: dict[str, dict[str, Any]] = field(default_factory=dict)
    seed: int | None = None

    def should_fire(self, boundary: str, call_index: int = 1) -> dict[str, Any] | None:
        """Return the fault descriptor if it fires here, else None."""
        spec = self.faults.get(boundary)
        if not spec:
            return None
        want = int(spec.get("occurrence", 1))
        if call_index != want:
            return None
        return dict(spec)


class _Exporter:
    """Bounded, non-blocking event exporter with graceful degradation.

    No network I/O happens per step: events batch in memory and flush
    only when the batch threshold is reached or the run ends.
    """

    BATCH_SIZE = 50

    def __init__(self, config: WorkflowConfig) -> None:
        self._config = config
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=config.max_queue)
        self.dropped = 0
        self.sent = 0
        self.failed = 0
        self._seen_ids: set[str] = set()

    def emit(self, event: dict[str, Any]) -> bool:
        """Enqueue; False when dropped (full) — never raises, never blocks."""
        if event.get("event_id") in self._seen_ids:
            return True  # idempotent: already captured
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                return False
        self._seen_ids.add(event["event_id"])
        if self._config.endpoint and self._queue.qsize() >= self.BATCH_SIZE:
            self._flush_best_effort()
        return True

    def flush(self) -> None:
        """Push one batch to the endpoint, if configured. Never raises."""
        if not self._config.enabled:
            return
        try:
            self._flush_best_effort()
        except Exception:  # noqa: BLE001 -- flush must never break the host
            self.failed += 1

    def drain(self) -> list[dict[str, Any]]:
        """Remove and return all buffered events (local capture path)."""
        items: list[dict[str, Any]] = []
        try:
            while True:
                items.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        return items

    def _flush_best_effort(self) -> None:
        endpoint = self._config.endpoint
        if not endpoint:
            return
        batch: list[dict[str, Any]] = []
        try:
            while len(batch) < 50:
                batch.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        if not batch:
            return
        try:
            body = json.dumps({"events": batch}).encode()
            req = urllib.request.Request(
                endpoint.rstrip("/") + "/v1/events",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            if self._config.api_key:
                req.add_header("Authorization", f"Bearer {self._config.api_key}")
            with urllib.request.urlopen(req, timeout=2):
                self.sent += len(batch)
        # Best-effort flush: network failures are counted, never raised.
        except Exception:  # noqa: BLE001 -- graceful degradation by design
            self.failed += len(batch)
            for event in batch:  # requeue best-effort
                try:
                    self._queue.put_nowait(event)
                except queue.Full:
                    self.dropped += 1


class _StepContext:
    def __init__(self, run: RunContext, name: str) -> None:
        self._run = run
        self.name = name

    def __enter__(self) -> Self:
        self._run._sdk._record(self._run, kind="step_start", step=self.name, status="ok")
        return self

    def __exit__(self, *exc: object) -> None:
        status = "error" if exc[0] is not None else "ok"
        error = f"{exc[0].__name__}: {exc[1]}" if exc[0] is not None else None
        self._run._sdk._record(
            self._run, kind="step_end", step=self.name, status=status, error=error
        )

    def record_tool(
        self,
        tool: str,
        *,
        inputs: Any = None,
        outputs: Any = None,
        status: str = "ok",
        error: str | None = None,
        fault: dict[str, Any] | None = None,
    ) -> None:
        """Record a tool invocation inside this step."""
        self._run._sdk._record(
            self._run,
            kind="tool",
            step=self.name,
            status=status,
            tool=tool,
            inputs=inputs,
            outputs=outputs,
            error=error,
            fault=fault,
        )


class RunContext:
    """One instrumented workflow execution (sync CM; async via ``arun``)."""

    def __init__(self, sdk: AgentCrash, name: str) -> None:
        self._sdk = sdk
        self.name = name
        self.run_id = f"run_{uuid4().hex[:12]}"
        self.faults_fired: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        self._sdk._record(self, kind="run_start", status="ok")
        return self

    def __exit__(self, *exc: object) -> None:
        status = "error" if exc[0] is not None else "ok"
        error = f"{exc[0].__name__}: {exc[1]}" if exc[0] is not None else None
        self._sdk._record(self, kind="run_end", status=status, error=error)
        self._sdk.exporter.flush()

    def step(self, name: str) -> _StepContext:
        """Open a child step context manager."""
        return _StepContext(self, name)

    def maybe_fault(self, boundary: str) -> dict[str, Any] | None:
        """Deterministic fault hook; records injections as events."""
        fault = self._sdk.faults.should_fire(boundary) if self._sdk.faults else None
        if fault is None or not self._sdk._faults_enabled():
            return None
        self.faults_fired.append({"boundary": boundary, **fault})
        self._sdk._record(self, kind="fault", step=boundary, status="ok", fault=fault)
        return fault


class AgentCrash:
    """Installable reliability SDK. Observes; never orchestrates."""

    def __init__(self, config: WorkflowConfig | None = None, **kwargs: Any) -> None:
        self.config = config or WorkflowConfig(**kwargs)
        if os.environ.get("AGENTCRASH_DISABLED") == "1":
            self.config.enabled = False
        self.faults = FaultPolicy(seed=self.config.seed)
        self.exporter = _Exporter(self.config)
        self._seq = 0
        self._prev_hash = "GENESIS"
        self._lock = threading.Lock()
        self.traces: dict[str, list[dict[str, Any]]] = {}

    # -- configuration -------------------------------------------------
    def _active(self) -> bool:
        return bool(self.config.enabled)

    def _faults_enabled(self) -> bool:
        return bool(self.config.enabled and self.config.fault_injection)

    def configure_faults(self, faults: dict[str, dict[str, Any]]) -> None:
        """Set deterministic fault descriptors per boundary."""
        self.faults = FaultPolicy(faults=faults, seed=self.config.seed)

    # -- integration APIs ----------------------------------------------
    def workflow(self, name: str | None = None) -> Callable:
        """Decorator: trace a host-owned function as one run."""

        def deco(fn: Callable) -> Callable:
            wf_name = name or fn.__name__

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not self._active():
                    return fn(*args, **kwargs)
                # Errors propagate through RunContext.__exit__, which records
                # the run_end error — no catch-all here by design.
                with self.run(wf_name) as run:
                    out = fn(*args, **kwargs)
                    self._record(
                        run, kind="result", status="ok", outputs={"value": str(out)[:2000]}
                    )
                    return out

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not self._active():
                    return await fn(*args, **kwargs)
                with self.run(wf_name) as run:
                    out = await fn(*args, **kwargs)
                    self._record(
                        run, kind="result", status="ok", outputs={"value": str(out)[:2000]}
                    )
                    return out

            if asyncio.iscoroutinefunction(fn):
                return async_wrapper
            return sync_wrapper

        return deco

    def run(self, name: str) -> RunContext:
        """Open an explicit run context (no-op passthrough when disabled)."""
        run = RunContext(self, name)
        if not self._active():
            run.run_id = f"disabled_{uuid4().hex[:8]}"
        return run

    # -- capture --------------------------------------------------------
    def _record(
        self,
        run: RunContext,
        *,
        kind: str,
        step: str | None = None,
        status: str = "ok",
        tool: str | None = None,
        inputs: Any = None,
        outputs: Any = None,
        error: str | None = None,
        fault: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self._active():
            return None
        with self._lock:
            self._seq += 1
            seq = self._seq
        event = make_event(
            event_id=f"evt_{run.run_id}_{seq:04d}",
            run_id=run.run_id,
            project=self.config.project,
            workflow=run.name,
            step=step,
            kind=kind,
            status=status,
            seq=seq,
            tool=tool,
            inputs=redact(inputs or {}),
            outputs=redact(outputs or {}),
            error=error,
            fault=fault,
        )
        problems = validate_event(event)
        assert not problems, f"SDK emitted invalid event: {problems}"
        line = json.dumps(event, sort_keys=True)
        digest = hashlib.sha256(f"{self._prev_hash}:{line}".encode()).hexdigest()
        self._prev_hash = digest
        event["prev_hash"] = digest
        self.traces.setdefault(run.run_id, []).append(event)
        self.exporter.emit(event)
        return event

    def trace(self, run_id: str) -> list[dict[str, Any]]:
        """Return captured events for a run (local capture path)."""
        return list(self.traces.get(run_id, []))
