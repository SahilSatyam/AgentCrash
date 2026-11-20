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
import hmac
import json
import os
import threading
import time
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self
from uuid import uuid4

from agentcrash.events import make_event
from agentcrash.tracer import key_bytes, redact

#: Built once: ``json.dumps(..., sort_keys=True)`` builds a new encoder per
#: call. Same output format, so existing chains still verify.
#: ``default=str`` keeps non-JSON host values (objects, datetimes) from
#: ever raising into host code.
_HASH_ENCODER = json.JSONEncoder(sort_keys=True, check_circular=False, default=str)
GENESIS = "GENESIS"


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
    signing_key: str | bytes | None = None


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

    def __post_init__(self) -> None:
        import random

        self._rng = random.Random(self.seed) if self.seed is not None else None

    @classmethod
    def from_scenario(cls, scenario: dict[str, Any]) -> FaultPolicy:
        """Build FaultPolicy from scenario mapping containing a 'faults' list."""
        faults_map: dict[str, dict[str, Any]] = {}
        seed = scenario.get("seed")
        for f in scenario.get("faults", []):
            if isinstance(f, dict):
                params = dict(f.get("params") or {})
                tool = params.get("tool") or f.get("boundary")
                if tool:
                    faults_map[tool] = {"kind": f.get("kind"), **params}
        return cls(faults=faults_map, seed=seed)

    def should_fire(self, boundary: str, call_index: int = 1) -> dict[str, Any] | None:
        """Return the fault descriptor if it fires here, else None."""
        spec = self.faults.get(boundary)
        if not spec:
            return None
        want = spec.get("occurrence", 1)
        if want == "all":
            return dict(spec)
        try:
            want_int = int(want)
        except (ValueError, TypeError):
            return None
        if call_index != want_int:
            return None
        return dict(spec)


class _Exporter:
    """Bounded, non-blocking event exporter with graceful degradation.

    Events batch in memory and are sent by a background daemon worker
    thread when an endpoint is configured. Network failures are swallowed
    and counted, never raised into host code.
    """

    BATCH_SIZE = 50

    def __init__(self, config: WorkflowConfig) -> None:
        self._config = config
        # ``max_queue <= 0`` means unbounded (same as ``queue.Queue``).
        self._max = config.max_queue if config.max_queue > 0 else None
        self._buf: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._send_lock = threading.Lock()
        self.dropped = 0
        self.sent = 0
        self.failed = 0
        self._in_flight = 0
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        if self._config.enabled and self._config.endpoint:
            self._start_worker()

    def _start_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._stop_event.clear()
                self._worker = threading.Thread(
                    target=self._worker_loop,
                    daemon=True,
                    name="agentcrash-exporter",
                )
                self._worker.start()

    @property
    def queued(self) -> int:
        """Number of events currently waiting in the in-memory buffer."""
        with self._lock:
            return len(self._buf)

    @property
    def in_flight(self) -> int:
        """Number of events currently detached from buffer and transmitting over HTTP."""
        with self._lock:
            return self._in_flight

    def emit(self, event: dict[str, Any]) -> bool:
        """Enqueue, dropping the oldest when full; never raises, never blocks."""
        with self._cond:
            if self._max is not None and len(self._buf) >= self._max:
                self._buf.popleft()
                self.dropped += 1
            self._buf.append(event)
            if self._config.endpoint and len(self._buf) >= self.BATCH_SIZE:
                self._cond.notify()
        if self._config.endpoint and (self._worker is None or not self._worker.is_alive()):
            self._start_worker()
        return True

    def _send_batch(self, batch: list[dict[str, Any]]) -> bool:
        """Send a single batch to the endpoint. Caller must NOT hold self._lock."""
        endpoint = self._config.endpoint
        if not endpoint:
            return False
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
                pass
            with self._lock:
                self.sent += len(batch)
            return True
        except Exception:  # noqa: BLE001 -- graceful degradation by design
            with self._lock:
                self.failed += len(batch)
                for event in reversed(batch):
                    if self._max is not None and len(self._buf) >= self._max:
                        self.dropped += 1
                        continue
                    self._buf.appendleft(event)
            return False

    def _pop_and_send_batch(self, max_batch: int | None = None) -> bool | None:
        """Pop up to batch_size events and send them.

        Returns True on send success, False on network failure, or None when
        the buffer was empty. Manages _in_flight counter and condition notifications.
        """
        limit = max_batch or self.BATCH_SIZE
        with self._cond:
            if not self._buf:
                return None
            n = min(limit, len(self._buf))
            batch = [self._buf.popleft() for _ in range(n)]
            self._in_flight += len(batch)
        try:
            with self._send_lock:
                return self._send_batch(batch)
        finally:
            with self._cond:
                self._in_flight -= len(batch)
                self._cond.notify_all()

    def _worker_loop(self) -> None:
        """Background daemon thread to export batches without stalling caller."""
        while not self._stop_event.is_set():
            with self._cond:
                while not self._stop_event.is_set() and len(self._buf) < self.BATCH_SIZE:
                    self._cond.wait(timeout=0.2)
                if self._stop_event.is_set() and not self._buf:
                    break
                if len(self._buf) < self.BATCH_SIZE:
                    continue
            self._pop_and_send_batch()

    def _flush_best_effort(self) -> None:
        """Push up to BATCH_SIZE events to the endpoint. Never raises."""
        if not self._config.enabled or not self._config.endpoint:
            return
        self._pop_and_send_batch()

    def flush(self, timeout: float | None = 5.0) -> None:
        """Loop-until-empty with a configurable shutdown deadline. Never raises.

        Flush is best-effort: it loops until all buffered events are drained and
        all in-flight network sends conclude, or until the deadline expires.
        If an HTTP send fails, the batch is returned to the queue, the failure is
        recorded in counters, and flush terminates early to avoid stalling callers
        on dead endpoints.
        """
        if not self._config.enabled or not self._config.endpoint:
            return
        deadline = time.time() + (timeout if timeout is not None else 5.0)
        while time.time() < deadline:
            with self._cond:
                if not self._buf and self._in_flight == 0:
                    break
                if not self._buf:
                    # Wait for in-flight worker to finish
                    self._cond.wait(timeout=0.1)
                    continue
            ok = self._pop_and_send_batch()
            if ok is False:
                # On failure, do not spin repeatedly; abort this flush pass.
                break

    def drain(self) -> list[dict[str, Any]]:
        """Remove and return all buffered events (local capture path).

        Clears the in-memory buffer (`queued`). Note that any events already
        `in_flight` (being transmitted by the background worker) are not returned.
        """
        with self._lock:
            items = list(self._buf)
            self._buf.clear()
        return items

    def close(self, timeout: float = 2.5) -> None:
        """Stop background worker.

        Signals the worker thread to stop and waits up to ``timeout`` seconds for
        any in-flight socket request (HTTP socket timeout is 2.0s) to finish.
        The worker thread is a daemon thread, guaranteeing no thread leaks on shutdown.
        """
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=timeout)


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
        # Per-run chain: each run's trace verifies on its own
        # (seq starts at 1, prev_hash starts at GENESIS).
        self._seq = 0
        self._prev_hash = GENESIS

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

    def intercept_output(self, tool: str, output: Any, call_index: int | None = None) -> Any:
        """Intercept tool outputs to inject active faults (poisoned_doc, concurrent_update)."""
        fault = self.maybe_fault(tool, call_index=call_index)
        if not fault:
            return output
        kind = fault.get("kind")
        if kind == "poisoned_doc":
            payload = (
                fault.get("payload")
                or fault.get("doc_id")
                or fault.get("fixture_key")
                or "[INJECTED_INSTRUCTION]"
            )
            if isinstance(output, dict):
                output = dict(output)
                if "text" in output:
                    output["text"] = payload
                elif "content" in output:
                    output["content"] = payload
                else:
                    output["payload"] = payload
                output["poisoned"] = True
                return output
            return payload
        elif kind == "concurrent_update":
            if isinstance(output, dict):
                output = dict(output)
                updates = fault.get("updates", {})
                if isinstance(updates, dict):
                    output.update(updates)
                if "version" in output:
                    output["version"] = int(output["version"]) + 1
                    output["conflict"] = True
                return output
        return output


class AgentCrash:
    """Installable reliability SDK. Observes; never orchestrates."""

    def __init__(self, config: WorkflowConfig | None = None, **kwargs: Any) -> None:
        self.config = config or WorkflowConfig(**kwargs)
        if os.environ.get("AGENTCRASH_DISABLED") == "1":
            self.config.enabled = False
        self.faults = FaultPolicy(seed=self.config.seed)
        self.exporter = _Exporter(self.config)
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

    def configure_faults_from_scenario(self, scenario: dict[str, Any]) -> None:
        """Configure fault injection from a scenario mapping."""
        self.faults = FaultPolicy.from_scenario(scenario)

    def close(self) -> None:
        """Close the SDK and stop the background exporter worker."""
        self.exporter.close()

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
        run._seq += 1
        seq = run._seq
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
        digest = _chain_hash(run._prev_hash, line)
        run._prev_hash = digest
        event["prev_hash"] = digest
        kb = key_bytes(self.config.signing_key)
        if kb is not None:
            # Note: SDK signs the new digest (incorporating current event line).
            event["sig"] = hmac.new(kb, digest.encode(), hashlib.sha256).hexdigest()
        self.traces.setdefault(run.run_id, []).append(event)
        return event

    def trace(self, run_id: str) -> list[dict[str, Any]]:
        """Return captured events for a run (local capture path)."""
        return list(self.traces.get(run_id, []))


def _chain_hash(prev: str, line: str) -> str:
    return hashlib.sha256(f"{prev}:{line}".encode()).hexdigest()


def verify_sdk_chain(
    events: list[dict[str, Any]],
    signing_key: str | bytes | None = None,
) -> dict[str, Any]:
    """Verify one run's SDK events on their own.

    Checks, in ``seq`` order: a single ``run_id``, gapless ``seq`` from 1,
    and the ``prev_hash`` chain, where each event's ``prev_hash`` is
    ``sha256(<previous value>:<canonical JSON of the event without
    prev_hash>)`` starting from ``GENESIS``. When ``signing_key`` is supplied,
    verifies HMAC-SHA256 signatures stored in ``sig``.

    .. note:: Signing Split
       SDK events sign the newly computed event digest (incorporating the event's
       canonical payload), while TraceWriter signs the old ``prev_hash``.
       Each verification function matches its respective writer format.
    """
    if not events:
        return {"ok": False, "first_bad": None, "reason": "no events", "events": 0}
    run_ids = {event.get("run_id") for event in events}
    if len(run_ids) != 1:
        return {
            "ok": False,
            "first_bad": None,
            "reason": f"trace mixes {len(run_ids)} runs; verify one run at a time",
            "events": len(events),
        }
    kb = key_bytes(signing_key)
    prev = GENESIS
    ordered = sorted(events, key=lambda e: e.get("seq") if isinstance(e.get("seq"), int) else -1)
    for expected, event in enumerate(ordered, start=1):
        event_id = event.get("event_id")
        if event.get("seq") != expected:
            return {"ok": False, "first_bad": event_id, "reason": "seq gap", "events": len(events)}
        body = {key: value for key, value in event.items() if key not in ("prev_hash", "sig")}
        prev = _chain_hash(prev, _HASH_ENCODER.encode(body))
        if event.get("prev_hash") != prev:
            return {
                "ok": False,
                "first_bad": event_id,
                "reason": "prev_hash mismatch",
                "events": len(events),
            }
        if kb is not None:
            expected_sig = hmac.new(kb, prev.encode(), hashlib.sha256).hexdigest()
            if event.get("sig") != expected_sig:
                return {
                    "ok": False,
                    "first_bad": event_id,
                    "reason": "signature mismatch",
                    "events": len(events),
                }
    return {"ok": True, "first_bad": None, "reason": None, "events": len(events)}


def to_otel_spans(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map AgentCrash trace events to OpenTelemetry-compatible span dicts with W3C traceparent.

    Zero dependency: produces pure dictionaries following OpenTelemetry semantic
    conventions for AI agents, with deterministic trace_id and span_id derived
    from run_id and sequence.

    .. note:: Virtual Clock Semantics
       ``ts_virtual`` travels as the span attribute ``agentcrash.ts_virtual``.
       Spans receive provider wall-clock times; consumers should rely on
       ``agentcrash.ts_virtual`` rather than span start/end wall-clock timestamps
       for deterministic virtual duration analysis.
    """
    if not events:
        return []
    run_id = str(events[0].get("run_id", "default_run"))
    trace_id = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
    spans: list[dict[str, Any]] = []

    step_spans: dict[str, str] = {}
    run_span_id = hashlib.sha256(f"{run_id}:root".encode()).hexdigest()[:16]

    for i, event in enumerate(events, start=1):
        seq = event.get("seq", i)
        kind = event.get("kind", "info")
        step = event.get("step")
        tool = event.get("tool")
        status = event.get("status", "ok")
        error = event.get("error")
        fault = event.get("fault")

        span_id = hashlib.sha256(f"{run_id}:{seq}:{kind}:{step}:{tool}".encode()).hexdigest()[:16]

        if kind in ("step_start", "step_end") and step:
            span_name = f"step:{step}"
            parent_id = run_span_id
            step_spans[step] = span_id
        elif kind == "tool" or tool:
            span_name = f"tool:{tool}" if tool else "tool"
            parent_id = step_spans.get(step, run_span_id) if step else run_span_id
        elif kind in ("run_start", "run_end"):
            span_name = f"workflow:{event.get('workflow', 'run')}"
            parent_id = None
        elif kind == "fault":
            span_name = f"fault:{fault.get('kind', 'unknown')}" if fault else "fault"
            parent_id = step_spans.get(step, run_span_id) if step else run_span_id
        else:
            span_name = f"agentcrash:{kind}"
            parent_id = run_span_id

        traceparent = f"00-{trace_id}-{span_id}-01"

        attributes: dict[str, Any] = {
            "agentcrash.run_id": run_id,
            "agentcrash.project": event.get("project", "default"),
            "agentcrash.kind": kind,
            "agentcrash.seq": seq,
            "agentcrash.status": status,
            "gen_ai.system": "agentcrash",
        }
        if step:
            attributes["agentcrash.step"] = step
        if tool:
            attributes["agentcrash.tool"] = tool
            attributes["gen_ai.tool.name"] = tool
        if error:
            attributes["agentcrash.error"] = str(error)
        if fault and isinstance(fault, dict):
            attributes["agentcrash.fault_kind"] = fault.get("kind", "unknown")
        if "ts_virtual" in event:
            attributes["agentcrash.ts_virtual"] = event["ts_virtual"]

        status_obj = {
            "code": "ERROR" if status == "error" or error else "OK",
            "description": str(error or ""),
        }

        spans.append(
            {
                "name": span_name,
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": parent_id,
                "traceparent": traceparent,
                "attributes": attributes,
                "status": status_obj,
            }
        )

    return spans


def export_otel_spans(events: list[dict[str, Any]], tracer: Any = None) -> int:
    """Export trace events to OpenTelemetry spans.

    If ``tracer`` is None, lazily imports ``opentelemetry.trace`` and creates
    spans. If OpenTelemetry is not installed, raises an informative ImportError.
    Returns the count of exported spans.

    .. note:: Virtual Clock Semantics
       ``ts_virtual`` is passed as attribute ``agentcrash.ts_virtual``; span durations
       reflect wall-clock time of the tracer provider.
    """
    spans_data = to_otel_spans(events)
    if not spans_data:
        return 0

    if tracer is None:
        try:
            from opentelemetry import trace

            tracer = trace.get_tracer("agentcrash")
        except ImportError as exc:
            raise ImportError(
                "OpenTelemetry is not installed. Install with 'pip install agentcrash[otel]' "
                "or pass an explicit tracer."
            ) from exc

    exported = 0
    for span_data in spans_data:
        if hasattr(tracer, "start_span"):
            span = tracer.start_span(
                span_data["name"],
                attributes=span_data["attributes"],
            )
            if hasattr(span, "set_status") and span_data["status"]["code"] == "ERROR":
                try:
                    from opentelemetry.trace import Status, StatusCode

                    span.set_status(Status(StatusCode.ERROR, span_data["status"]["description"]))
                except (ImportError, AttributeError, TypeError, ValueError):
                    pass
            if hasattr(span, "end"):
                span.end()
            exported += 1
        elif hasattr(tracer, "start_as_current_span"):
            with tracer.start_as_current_span(
                span_data["name"],
                attributes=span_data["attributes"],
            ):
                exported += 1
        else:
            exported += 1
    return exported
