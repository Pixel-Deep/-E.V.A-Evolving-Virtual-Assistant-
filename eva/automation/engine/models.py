"""Typed contracts shared by EVA's deterministic automation engine."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from time import monotonic
from typing import Any
from uuid import uuid4


class AutomationError(Exception):
    """An expected automation error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RiskLevel(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class ToolSpec:
    """Public, serializable declaration of an automation tool."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False
    })
    permissions: tuple[str, ...] = ()
    category: str = "general"
    version: str = "1.0.0"
    timeout_seconds: float = 15.0
    idempotent: bool = True
    risk: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    aliases: tuple[str, ...] = ()
    when_to_use: tuple[str, ...] = ()
    output_description: str = ""
    side_effects: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "version": self.version,
            "permissions": list(self.permissions),
            "timeout_seconds": self.timeout_seconds,
            "idempotent": self.idempotent,
            "risk": self.risk.value,
            "requires_approval": self.requires_approval,
            "input_schema": self.input_schema,
            "aliases": list(self.aliases),
            "when_to_use": list(self.when_to_use),
            "output_description": self.output_description,
            "side_effects": self.side_effects,
        }


@dataclass
class ToolResult:
    """Consistent result envelope for tools, workflows, and the EVA UI."""

    ok: bool
    tool: str
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    error_code: str | None = None
    verified: bool = False
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "data": self.data,
            "message": self.message,
            "error_code": self.error_code,
            "verified": self.verified,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class ExecutionContext:
    """Per-run state; no conversation memory is stored here."""

    run_id: str = field(default_factory=lambda: uuid4().hex)
    dry_run: bool = False
    cancel_event: Event = field(default_factory=Event, repr=False)
    approved_actions: frozenset[str] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=monotonic)
    parent_context: ExecutionContext | None = None
    deadline: float | None = None
    _cancel_callbacks: list[Any] = field(default_factory=list, repr=False)
    _active_processes: list[Any] = field(default_factory=list, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set() or (self.parent_context is not None and self.parent_context.cancelled)

    def remaining_seconds(self) -> float | None:
        """Seconds remaining before execution deadline, or None if unconstrained."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - monotonic())

    def register_process(self, process: Any) -> None:
        """Register an active subprocess so cancellation terminates it."""
        if process not in self._active_processes:
            self._active_processes.append(process)

    def unregister_process(self, process: Any) -> None:
        if process in self._active_processes:
            self._active_processes.remove(process)

    def add_cancel_callback(self, callback: Any) -> None:
        """Register a cleanup/cancellation action invoked when cancellation is requested."""
        if callback not in self._cancel_callbacks:
            self._cancel_callbacks.append(callback)

    def cancel(self) -> None:
        self.cancel_event.set()
        # Actively terminate any registered active processes
        for proc in list(self._active_processes):
            try:
                if hasattr(proc, "poll") and proc.poll() is None:
                    if hasattr(proc, "terminate"):
                        proc.terminate()
                    try:
                        proc.wait(timeout=0.1)
                    except Exception:
                        if hasattr(proc, "kill"):
                            proc.kill()
            except Exception:
                pass
        self._active_processes.clear()

        # Execute registered cancellation callbacks
        for cb in list(self._cancel_callbacks):
            try:
                cb()
            except Exception:
                pass
        self._cancel_callbacks.clear()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise AutomationError("CANCELLED", "The automation run was cancelled.")

    def child_context(self) -> ExecutionContext:
        """Create a scoped child context for an individual tool/step execution.

        Cancelling the child signals cooperative cancellation to that specific tool handler
        without cancelling the parent workflow context.
        Cancelling the parent cancels all child contexts.
        """
        child = ExecutionContext(
            run_id=self.run_id,
            dry_run=self.dry_run,
            approved_actions=self.approved_actions,
            metadata=dict(self.metadata),
            created_at=self.created_at,
            parent_context=self,
            deadline=self.deadline,
        )
        self.add_cancel_callback(child.cancel)
        return child


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _validate_schema_value(value: Any, rule: dict[str, Any], path: str) -> None:
    """Validate a bounded JSON-Schema subset recursively with precise paths."""
    expected = rule.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    if expected and not any(_schema_type_matches(value, kind) for kind in expected_types):
        kinds = " or ".join(str(kind) for kind in expected_types)
        raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' must be {kinds}.")
    if value is None:
        return
    if "enum" in rule and value not in rule["enum"]:
        raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' is not an allowed value.")
    if isinstance(value, str):
        if len(value) < rule.get("minLength", 0):
            raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' is too short.")
        if len(value) > rule.get("maxLength", 100_000):
            raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' is too long.")
        if "pattern" in rule:
            import re
            if not re.fullmatch(rule["pattern"], value):
                raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' has an invalid format.")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' is below the minimum.")
        if "maximum" in rule and value > rule["maximum"]:
            raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' is above the maximum.")
    if isinstance(value, (list, tuple)):
        if len(value) < rule.get("minItems", 0) or len(value) > rule.get("maxItems", 10_000):
            raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' has an invalid number of items.")
        if rule.get("uniqueItems"):
            seen = set()
            for item in value:
                try:
                    marker = json.dumps(item, sort_keys=True, separators=(",", ":"))
                except Exception:
                    marker = (type(item).__name__, repr(item))
                if marker in seen:
                    raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' must contain unique items.")
                seen.add(marker)
        item_rule = rule.get("items")
        if isinstance(item_rule, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, item_rule, f"{path}[{index}]")
    if isinstance(value, dict):
        properties = rule.get("properties", {})
        required = rule.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' is missing required field(s): {', '.join(missing)}.")
        if rule.get("additionalProperties", True) is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise AutomationError("INVALID_ARGUMENT", f"Argument '{path}' contains unknown field(s): {', '.join(extra)}.")
        for name, item in value.items():
            item_rule = properties.get(name)
            if isinstance(item_rule, dict):
                _validate_schema_value(item, item_rule, f"{path}.{name}")


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Validate the safe JSON-Schema subset used by EVA tools.

    Nested arrays/objects are checked recursively. This matters for tools such as
    files.find_one/files.find_latest whose exclude_directories parameter is a list
    of paths: accepting a list with non-string members would defer a routing error
    until inside a tool handler.
    """
    if not isinstance(arguments, dict):
        raise AutomationError("INVALID_ARGUMENT", "Tool arguments must be an object.")
    if schema.get("type", "object") != "object":
        raise AutomationError("INVALID_SCHEMA", "Only object input schemas are supported.")
    _validate_schema_value(arguments, {**schema, "type": "object"}, "args")

