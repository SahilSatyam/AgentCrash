"""AgentCrash — chaos engineering for tool-using AI agents.

Framework-neutral core: SDK instrumentation, deterministic fault
injection, versioned events, generic invariants, recovery policies, and
the control-plane server. Domain logic lives in the host workflow or a
plugin package, never in core imports.
"""

from agentcrash.evaluate import compare_reports, evaluate_trace
from agentcrash.events import SCHEMA_VERSION, make_event, validate_event
from agentcrash.invariants import DuplicateMutatingEffect, RequiredStep
from agentcrash.sdk import AgentCrash, FaultPolicy, RunContext, WorkflowConfig
from agentcrash.server import (
    evaluation_summary,
    ingest_events,
    prune_runs,
    register_project,
    reliability_summary,
    reset_store,
    run_detail,
    submit_evaluation,
)

__version__ = "0.3.0"

__all__ = [
    "SCHEMA_VERSION",
    "AgentCrash",
    "DuplicateMutatingEffect",
    "FaultPolicy",
    "RequiredStep",
    "RunContext",
    "WorkflowConfig",
    "compare_reports",
    "evaluate_trace",
    "evaluation_summary",
    "ingest_events",
    "make_event",
    "prune_runs",
    "register_project",
    "reliability_summary",
    "reset_store",
    "run_detail",
    "submit_evaluation",
    "validate_event",
]
