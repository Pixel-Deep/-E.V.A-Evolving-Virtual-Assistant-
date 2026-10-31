"""Single authoritative orchestration facade for compound EVA requests."""
from __future__ import annotations

from eva.automation.orchestration.analyzer import analyze_request, analyze_request_dict
from eva.automation.orchestration.models import (
    OutcomeCheck,
    RequestAnalysis,
    VerificationReport,
    WorkflowKind,
)
from eva.automation.orchestration.planner import build_plan
from eva.automation.orchestration.verifier import evaluate_plan_coverage, verify_execution

__all__ = [
    "OutcomeCheck",
    "RequestAnalysis",
    "VerificationReport",
    "WorkflowKind",
    "analyze_request",
    "analyze_request_dict",
    "build_plan",
    "evaluate_plan_coverage",
    "verify_execution",
]
