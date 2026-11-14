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
import threading
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self
from uuid import uuid4

from agentcrash.events import make_event
from agentcrash.tracer import redact

#: Built once: ``json.dumps(..., sort_keys=True)`` builds a new encoder per
#: call. Same output format, so existing chains still verify.
#: ``default=str`` keeps non-JSON host values (objects, datetimes) from
#: ever raising into host code.
_HASH_ENCODER = json.JSONEncoder(sort_keys=True, check_circular=False, default=str)


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
    ``{"payments.charge": {"kind": "timeout_after_commit"}}``. Decisions are
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
        # ``max_queue <= 0`` means unbounded (same as ``queue.Queue``).
        self._max = config.max_queue if config.max_queue > 0 else None
        self._buf: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self.dropped = 0
        self.sent = 0
        self.failed = 0

    def emit(self, event: dict[str, Any]) -> bool:
        """Enqueue, dropping the oldest when full; never raises, never blocks.

        Duplicate ``event_id`` values are not filtered here (the SDK never
        emits one twice, and the server dedupes on ingestion), so memory
        stays bounded by ``max_queue``.
        """
        with self._lock:
            if self._max is not None and len(self._buf) >= self._max:
                self._buf.popleft()
                self.dropped += 1
            self._buf.append(event)
            flush_now = bool(self._config.endpoint) and len(self._buf) >= self.BATCH_SIZE
        if flush_now:
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
        with self._lock:
            items = list(self._buf)
            self._buf.clear()
        return items

    def _flush_best_effort(self) -> None:
        endpoint = self._config.endpoint
        if not endpoint:
            return
        with self._lock:
            n = min(self.BATCH_SIZE, len(self._buf))
            batch = [self._buf.popleft() for _ in range(n)]
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
            with self._lock:  # requeue at the front, in order, best-effort
                for event in reversed(batch):
                    if self._max is not None and len(self._buf) >= self._max:
                        self.dropped += 1
                        continue
                    self._buf.appendleft(event)


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
        self._calls: dict[str, int] = {}

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

    def maybe_fault(self, boundary: str, call_index: int | None = None) -> dict[str, Any] | None:
        """Deterministic fault hook; records injections as events.

        Each call counts as one call of ``boundary`` in this run, so a fault
        with ``occurrence: N`` fires on exactly the Nth call. Pass
        ``call_index`` to supply your own count instead.
        """
        if call_index is None:
            call_index = self._calls.get(boundary, 0) + 1
            self._calls[boundary] = call_index
        fault = self._sdk.faults.should_fire(boundary, call_index) if self._sdk.faults else None
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
            event = self._record_locked(
                run,
                kind=kind,
                step=step,
                status=status,
                tool=tool,
                inputs=inputs,
                outputs=outputs,
                error=error,
                fault=fault,
            )
        # Outside the lock: a batch flush may touch the network.
        self.exporter.emit(event)
        return event

    def _record_locked(
        self,
        run: RunContext,
        *,
        kind: str,
        step: str | None,
        status: str,
        tool: str | None,
        inputs: Any,
        outputs: Any,
        error: str | None,
        fault: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build, chain and capture one event (caller holds ``self._lock``).

        Sequence number and ``prev_hash`` advance together under the lock,
        so concurrent steps can never fork the chain.
        """
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
            inputs=redact(inputs) if inputs else {},
            outputs=redact(outputs) if outputs else {},
            error=error,
            fault=fault,
        )
        # No per-event validate_event(): make_event builds valid events by
        # construction (pinned by tests), and the server re-validates every
        # event on ingestion. This keeps the hot path cheap.
        line = _HASH_ENCODER.encode(event)
        digest = hashlib.sha256(f"{self._prev_hash}:{line}".encode()).hexdigest()
        self._prev_hash = digest
        event["prev_hash"] = digest
        self.traces.setdefault(run.run_id, []).append(event)
        return event

    def trace(self, run_id: str) -> list[dict[str, Any]]:
        """Return captured events for a run (local capture path)."""
        return list(self.traces.get(run_id, []))
