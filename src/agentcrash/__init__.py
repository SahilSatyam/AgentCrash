"""AgentCrash — chaos engineering for tool-using AI agents.

Framework-neutral core: SDK instrumentation, deterministic fault
injection, versioned events, generic invariants, recovery policies, and
the control-plane server. Domain logic lives in the host workflow or a
plugin package, never in core imports.
"""

from agentcrash.evaluate import (
    compare_reports,
    evaluate_trace,
    fuzz_and_shrink,
    fuzz_schedules,
    shrink_schedule,
)
from agentcrash.events import SCHEMA_VERSION, make_event, validate_event
from agentcrash.invariants import DuplicateMutatingEffect, RequiredStep
from agentcrash.sdk import (
    AgentCrash,
    FaultPolicy,
    RunContext,
    WorkflowConfig,
    export_otel_spans,
    to_otel_spans,
    verify_sdk_chain,
)
from agentcrash.server import (
    MemoryStorage,
    PostgresStorage,
    SQLiteStorage,
    Storage,
    evaluation_summary,
    get_storage,
    ingest_events,
    prune_runs,
    register_project,
    reliability_summary,
    reset_store,
    run_detail,
    set_storage,
    submit_evaluation,
)

__version__ = "0.3.0"

__all__ = [
    "SCHEMA_VERSION",
    "AgentCrash",
    "DuplicateMutatingEffect",
    "FaultPolicy",
    "MemoryStorage",
    "PostgresStorage",
    "RequiredStep",
    "RunContext",
    "SQLiteStorage",
    "Storage",
    "WorkflowConfig",
    "compare_reports",
    "evaluate_trace",
    "evaluation_summary",
    "export_otel_spans",
    "fuzz_and_shrink",
    "fuzz_schedules",
    "get_storage",
    "ingest_events",
    "make_event",
    "prune_runs",
    "register_project",
    "reliability_summary",
    "reset_store",
    "run_detail",
    "set_storage",
    "shrink_schedule",
    "submit_evaluation",
    "to_otel_spans",
    "validate_event",
    "verify_sdk_chain",
]
