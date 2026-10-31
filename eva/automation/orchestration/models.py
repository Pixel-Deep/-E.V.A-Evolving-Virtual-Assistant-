"""Structured contracts for EVA's compound automation orchestration."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class WorkflowKind(str, Enum):
    """Bounded, high-confidence compound workflows owned by the orchestrator."""

    WEB_SUMMARY_NOTEPAD = "web_summary_notepad"
    PYTHON_DOWNLOADS_SUMMARY = "python_downloads_summary"
    RANKED_RESULT = "ranked_result"
    FIRST_RESULT = "first_result"
    DATED_FILE_COPY_OPEN = "dated_file_copy_open"


@dataclass(frozen=True)
class RequestAnalysis:
    """A deterministic interpretation of a compound request.

    ``plan_owned`` records whether the orchestration planner must build the
    plan itself. Keeping analysis separate from plan ownership allows the same
    verifier to check legacy-compatible plans without replacing them.
    """

    kind: WorkflowKind
    request: str
    topic: str = ""
    canonical_url: str = ""
    engine: str = ""
    query: str = ""
    index: int = 0
    play: bool = False
    source_date: str = ""
    source_field: str = "any"
    copy_date: str = ""
    source_phrase: str = ""
    copy_phrase: str = ""
    target_name: str = ""
    open_copy: bool = False
    plan_owned: bool = True

    @property
    def is_orchestrated(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "topic": self.topic,
            "canonical_url": self.canonical_url,
            "engine": self.engine,
            "query": self.query,
            "index": self.index,
            "play": self.play,
            "source_date": self.source_date,
            "source_field": self.source_field,
            "copy_date": self.copy_date,
            "source_phrase": self.source_phrase,
            "copy_phrase": self.copy_phrase,
            "target_name": self.target_name,
            "open_copy": self.open_copy,
            "plan_owned": self.plan_owned,
        }


@dataclass
class OutcomeCheck:
    """One independently checked outcome in an executed compound task."""

    name: str
    step_id: str = ""
    tool: str = ""
    ok: bool = False
    verified: bool = False
    launch_only: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "step_id": self.step_id,
            "tool": self.tool,
            "ok": self.ok,
            "verified": self.verified,
            "launch_only": self.launch_only,
            "detail": self.detail,
        }


@dataclass
class VerificationReport:
    """Objective-level verification result for one executed workflow."""

    ok: bool
    verified: bool
    checks: list[OutcomeCheck] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    unverified_steps: list[str] = field(default_factory=list)
    launch_only_steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verified": self.verified,
            "checks": [check.as_dict() for check in self.checks],
            "issues": list(self.issues),
            "unverified_steps": list(self.unverified_steps),
            "launch_only_steps": list(self.launch_only_steps),
        }
