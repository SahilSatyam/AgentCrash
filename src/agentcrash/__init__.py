"""AgentCrash — installable reliability framework for agentic workflows.

Core package: SDK instrumentation, versioned events, generic invariants,
fault policies, and the control-plane server. Domain logic (ODRL, DSPy,
SHACL) lives in host apps / extensions, never in core imports.
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

__version__ = "0.2.0"

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
