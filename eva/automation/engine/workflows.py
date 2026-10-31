"""Validated, deterministic DAG workflows; expressions are references, never code."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from .executor import ToolExecutor
from .models import AutomationError, ExecutionContext, ToolResult, validate_arguments
from .registry import ToolRegistry

_REFERENCE = re.compile(r"\$\{(inputs|steps)\.([A-Za-z0-9_-]+)((?:\.[A-Za-z0-9_-]+(?:\[\d{1,4}\])?)*)\}")
_MAX_STEPS = 100
_MAX_WORKFLOW_SECONDS = 600.0


@dataclass
class WorkflowResult:
    ok: bool
    name: str
    run_id: str
    steps: list[dict[str, Any]]
    error_code: str | None = None
    message: str = ""
    verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "name": self.name, "run_id": self.run_id,
                "steps": self.steps, "error_code": self.error_code, "message": self.message,
                "verified": self.verified}


def load_workflow(path: str | Path) -> dict[str, Any]:
    """Load JSON or YAML workflow files without evaluating Python or templates."""
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if target.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise AutomationError("DEPENDENCY_MISSING", "PyYAML is required to load YAML workflows; install requirements.txt.") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise AutomationError("INVALID_WORKFLOW", "A workflow must be a mapping/object.")
    return payload


class WorkflowEngine:
    def __init__(self, registry: ToolRegistry, executor: ToolExecutor):
        self.registry = registry
        self.executor = executor

    def validate(self, workflow: dict[str, Any], inputs: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not isinstance(workflow, dict):
            raise AutomationError("INVALID_WORKFLOW", "Workflow must be an object.")
        # Fail closed on misspelled or unsupported workflow fields. Silently
        # ignoring a model-generated field such as "approval_required" or a
        # step-level "shell" command makes a plan look accepted when its
        # semantics were not actually implemented.
        allowed_workflow_keys = {"name", "version", "description", "inputs", "steps", "timeout_seconds"}
        unknown_workflow_keys = sorted(set(workflow) - allowed_workflow_keys)
        if unknown_workflow_keys:
            raise AutomationError("INVALID_WORKFLOW", "Unsupported workflow field(s): " + ", ".join(unknown_workflow_keys))
        name = workflow.get("name")
        steps = workflow.get("steps")
        if not isinstance(name, str) or not name.strip():
            raise AutomationError("INVALID_WORKFLOW", "Workflow needs a non-empty name.")
        if not isinstance(steps, list) or not steps or len(steps) > _MAX_STEPS:
            raise AutomationError("INVALID_WORKFLOW", f"Workflow must contain between 1 and {_MAX_STEPS} steps.")
        timeout_seconds = workflow.get("timeout_seconds", _MAX_WORKFLOW_SECONDS)
        if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
                or timeout_seconds <= 0 or timeout_seconds > _MAX_WORKFLOW_SECONDS):
            raise AutomationError(
                "INVALID_WORKFLOW",
                f"timeout_seconds must be greater than 0 and at most {_MAX_WORKFLOW_SECONDS:g}.",
            )
        inputs_schema = workflow.get("inputs", {})
        if not isinstance(inputs_schema, dict):
            raise AutomationError("INVALID_WORKFLOW", "Workflow inputs must be a mapping of names to schemas.")
        input_values = {} if inputs is None else inputs
        if not isinstance(input_values, dict):
            raise AutomationError("INVALID_ARGUMENT", "Workflow inputs must be an object.")
        clean_input_schemas: dict[str, dict[str, Any]] = {}
        required_inputs: list[str] = []
        for key, rule in inputs_schema.items():
            if not isinstance(rule, dict):
                raise AutomationError("INVALID_WORKFLOW", f"Workflow input schema for '{key}' must be an object.")
            clean_input_schemas[key] = {k: v for k, v in rule.items() if k not in {"required", "description"}}
            if rule.get("required"):
                required_inputs.append(key)
        validate_arguments({"type": "object", "properties": clean_input_schemas,
                            "required": required_inputs, "additionalProperties": False}, input_values)

        by_id: dict[str, dict[str, Any]] = {}
        for step in steps:
            if not isinstance(step, dict):
                raise AutomationError("INVALID_WORKFLOW", "Every workflow step must be an object.")
            allowed_step_keys = {"id", "tool", "args", "depends_on", "purpose", "goal", "verify", "require_verification"}
            unknown_step_keys = sorted(set(step) - allowed_step_keys)
            if unknown_step_keys:
                raise AutomationError("INVALID_WORKFLOW", "Unsupported field(s) in workflow step: " + ", ".join(unknown_step_keys))
            step_id, tool = step.get("id"), step.get("tool")
            if not isinstance(step_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", step_id):
                raise AutomationError("INVALID_WORKFLOW", "Every step needs a safe id (letters, digits, _ or -).")
            if step_id in by_id:
                raise AutomationError("INVALID_WORKFLOW", f"Duplicate workflow step id: {step_id}")
            if not isinstance(tool, str) or self.registry.get(tool) is None:
                raise AutomationError("TOOL_NOT_FOUND", f"Workflow step '{step_id}' refers to an unavailable tool: {tool}")
            args = step.get("args", {})
            if not isinstance(args, dict):
                raise AutomationError("INVALID_WORKFLOW", f"Arguments for step '{step_id}' must be an object.")
            deps = step.get("depends_on", [])
            if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
                raise AutomationError("INVALID_WORKFLOW", f"depends_on for step '{step_id}' must be a list of step ids.")
            by_id[step_id] = {**step, "depends_on": deps, "args": args}

        for step_id, step in by_id.items():
            unknown = set(step["depends_on"]) - set(by_id)
            if unknown:
                raise AutomationError("INVALID_WORKFLOW", f"Step '{step_id}' depends on unknown step(s): {', '.join(sorted(unknown))}")
            if step_id in step["depends_on"]:
                raise AutomationError("INVALID_WORKFLOW", f"Step '{step_id}' cannot depend on itself.")

        ordered: list[dict[str, Any]] = []
        pending = dict(by_id)
        complete: set[str] = set()
        while pending:
            ready = [sid for sid, step in pending.items() if set(step["depends_on"]).issubset(complete)]
            if not ready:
                raise AutomationError("INVALID_WORKFLOW", "Workflow dependencies contain a cycle.")
            for sid in ready:
                ordered.append(pending.pop(sid))
                complete.add(sid)
        # Validate every plan step before executing any of them. Template values
        # are dynamic, but their surrounding argument structure, required keys,
        # extra keys, static sibling values, reference syntax, and dependencies
        # are all knowable at preview time and must fail closed here.
        for step in ordered:
            spec = self.registry.require(step["tool"]).spec
            args = step["args"]
            _validate_arguments_with_templates(spec.input_schema, args, step["id"])
            for expression, group, root_name in _template_references(args):
                if group == "inputs":
                    if root_name not in clean_input_schemas:
                        raise AutomationError("INVALID_WORKFLOW_REFERENCE",
                            f"Workflow reference '{expression}' uses unknown input '{root_name}'.")
                else:
                    if root_name not in by_id:
                        raise AutomationError("INVALID_WORKFLOW_REFERENCE",
                            f"Workflow reference '{expression}' points to unknown step '{root_name}'.")
                    if root_name not in step["depends_on"]:
                        raise AutomationError("INVALID_WORKFLOW_REFERENCE",
                            f"Step '{step['id']}' must declare referenced step '{root_name}' in depends_on.")
        return ordered, input_values

    def preview(self, workflow: dict[str, Any], inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        ordered, input_values = self.validate(workflow, inputs)
        return {
            "ok": True,
            "name": workflow["name"],
            "dry_run": True,
            "inputs": input_values,
            "steps": [{"id": step["id"], "tool": step["tool"], "depends_on": step["depends_on"], "args": step["args"]} for step in ordered],
            "message": "Workflow validated; no actions were executed.",
        }

    def execute(self, workflow: dict[str, Any], inputs: dict[str, Any] | None = None, *, context: ExecutionContext | None = None) -> WorkflowResult:
        context = context or ExecutionContext()
        try:
            ordered, input_values = self.validate(workflow, inputs)
        except AutomationError as exc:
            return WorkflowResult(False, str(workflow.get("name", "<invalid>")) if isinstance(workflow, dict) else "<invalid>", context.run_id, [], exc.code, exc.message)
        # Whole-workflow permission preflight: do not execute an earlier safe
        # prefix only to discover that a later step is categorically forbidden.
        # Approval-gated tools are handled at execution/approval time; this pass
        # checks hard permission policy only.
        try:
            for step in ordered:
                spec = self.registry.require(step["tool"]).spec
                self.executor.policy.authorize(spec, context, check_approval=False)
        except AutomationError as exc:
            return WorkflowResult(False, workflow["name"], context.run_id, [], exc.code,
                                  f"Workflow blocked before execution: {exc.message}")

        completed: dict[str, ToolResult] = {}
        report: list[dict[str, Any]] = []
        failure = None
        workflow_timeout = float(workflow.get("timeout_seconds", _MAX_WORKFLOW_SECONDS))
        deadline = monotonic() + workflow_timeout
        context.deadline = deadline
        for step in ordered:
            step_id = step["id"]
            if monotonic() >= deadline:
                context.cancel()
                failure = ("WORKFLOW_TIMEOUT", f"Workflow exceeded its {workflow_timeout:g}s total execution budget.")
                report.append({"id": step_id, "tool": step["tool"], "status": "skipped", "error_code": "WORKFLOW_TIMEOUT"})
                break
            if context.cancelled:
                failure = failure or ("CANCELLED", "Workflow was cancelled.")
                report.append({"id": step_id, "tool": step["tool"], "status": "skipped", "error_code": "CANCELLED"})
                continue
            completed_statuses = {r["id"]: r.get("status") for r in report}
            failed_deps = [dep for dep in step["depends_on"] if dep not in completed or not completed[dep].ok or completed_statuses.get(dep) != "succeeded"]
            if failed_deps:
                report.append({"id": step_id, "tool": step["tool"], "status": "skipped", "error_code": "DEPENDENCY_FAILED", "depends_on": failed_deps})
                failure = failure or ("DEPENDENCY_FAILED", f"Step '{step_id}' was skipped because a dependency failed.")
                continue
            try:
                args = _resolve_templates(step["args"], input_values, completed)
                spec = self.registry.require(step["tool"]).spec
                validate_arguments(spec.input_schema, args)
                result = self.executor.execute(step["tool"], args, context=context)
            except AutomationError as exc:
                result = ToolResult(False, step["tool"], error_code=exc.code, message=exc.message)
            completed[step_id] = result
            if not result.ok:
                failure = failure or (result.error_code or "TOOL_ERROR", result.message or f"Step '{step_id}' failed.")
            elif step.get("require_verification") and not result.verified:
                failure = failure or ("VERIFICATION_FAILED", f"Step '{step_id}' succeeded without required postcondition verification ({step.get('verify', 'required')}).")
            step_succeeded = result.ok and not (step.get("require_verification") and not result.verified)
            report.append({"id": step_id, "tool": step["tool"], "status": "succeeded" if step_succeeded else "failed", "result": result.as_dict()})
            if not result.ok and isinstance(result.data, dict) and result.data.get("side_effects_uncertain"):
                context.cancel()
            if not result.ok and (result.error_code == "TIMEOUT" or monotonic() >= deadline):
                context.cancel()
                if monotonic() >= deadline or (isinstance(result.data, dict) and result.data.get("deadline_exceeded")):
                    failure = ("WORKFLOW_TIMEOUT", f"Workflow exceeded its {workflow_timeout:g}s total execution budget during step '{step_id}'.")
                    break
            if monotonic() >= deadline:
                # The action may already have happened. Preserve its truthful step
                # status but report that the total workflow budget was exhausted.
                context.cancel()
                failure = ("WORKFLOW_TIMEOUT", f"Workflow exceeded its {workflow_timeout:g}s total execution budget after step '{step_id}'.")
                break
        ok = failure is None and len(report) == len(ordered) and all(item.get("status") == "succeeded" for item in report)
        all_verified = ok and all(bool(item.get("result", {}).get("verified")) for item in report if item.get("status") == "succeeded")
        return WorkflowResult(ok, workflow["name"], context.run_id, report,
                              None if ok else (failure[0] if failure else "WORKFLOW_FAILED"),
                              "Workflow completed." if ok else (failure[1] if failure else "Workflow stopped before all steps completed."),
                              verified=all_verified)


def _has_template(value: Any) -> bool:
    if isinstance(value, str):
        return "${" in value
    if isinstance(value, dict):
        return any(_has_template(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_template(item) for item in value)
    return False


def _template_references(value: Any):
    """Yield syntactically valid references, rejecting malformed ${...} syntax."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _template_references(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _template_references(item)
        return
    if not isinstance(value, str) or "${" not in value:
        return
    position = 0
    while True:
        start = value.find("${", position)
        if start < 0:
            return
        end = value.find("}", start + 2)
        if end < 0:
            raise AutomationError("INVALID_WORKFLOW_REFERENCE", "A workflow reference is missing its closing brace.")
        expression = value[start:end + 1]
        match = _REFERENCE.fullmatch(expression)
        if not match:
            raise AutomationError("INVALID_WORKFLOW_REFERENCE", f"Malformed workflow reference: {expression[:120]}.")
        group, root_name, _path = match.groups()
        yield expression, group, root_name
        position = end + 1


def _validate_arguments_with_templates(schema: dict[str, Any], arguments: dict[str, Any], step_id: str) -> None:
    """Validate all static fields before execution while deferring only reference values.

    This prevents a late bad argument (for example an extra `shell` key) from
    being discovered only after an earlier browser or file action already ran.
    A full-reference value is type-checked again after it resolves. Interpolated
    references are strings and are rejected for non-string schema fields.
    """
    if not isinstance(arguments, dict):
        raise AutomationError("INVALID_ARGUMENT", f"Arguments for step '{step_id}' must be an object.")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [name for name in required if name not in arguments]
    if missing:
        raise AutomationError("INVALID_ARGUMENT", f"Step '{step_id}' is missing required argument(s): {', '.join(missing)}.")
    if schema.get("additionalProperties", False) is False:
        extra = sorted(set(arguments) - set(properties))
        if extra:
            raise AutomationError("INVALID_ARGUMENT", f"Step '{step_id}' has unknown argument(s): {', '.join(extra)}.")

    for name, value in arguments.items():
        rule = properties.get(name)
        if rule is None:
            continue
        if _has_template(value):
            if isinstance(value, str):
                full_reference = _REFERENCE.fullmatch(value) is not None
                expected = rule.get("type")
                accepted = expected if isinstance(expected, list) else [expected]
                if not full_reference and expected and "string" not in accepted:
                    raise AutomationError("INVALID_WORKFLOW_REFERENCE",
                        f"Step '{step_id}' interpolates a reference into non-string argument '{name}'.")
            continue
        validate_arguments({"type": "object", "properties": {name: rule},
                            "additionalProperties": False}, {name: value})


def _flatten(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item)
    else:
        yield value


def _lookup_reference(expression: str, inputs: dict[str, Any], results: dict[str, ToolResult]) -> Any:
    match = _REFERENCE.fullmatch(expression)
    if not match:
        return expression
    group, root_name, path = match.groups()
    if group == "inputs":
        value: Any = inputs.get(root_name)
    else:
        result = results.get(root_name)
        if result is None or not result.ok:
            raise AutomationError("INVALID_WORKFLOW_REFERENCE", f"Workflow reference '{expression}' points to an unavailable step result.")
        value = result.as_dict()
    if path:
        segments = [part for part in path.split(".") if part]
        for segment in segments:
            segment_match = re.fullmatch(r"([A-Za-z0-9_-]+)(?:\[(\d{1,4})\])?", segment)
            if not segment_match:
                raise AutomationError("INVALID_WORKFLOW_REFERENCE", f"Workflow reference '{expression}' contains an invalid path segment.")
            key, index_text = segment_match.groups()
            if not isinstance(value, dict) or key not in value:
                raise AutomationError("INVALID_WORKFLOW_REFERENCE", f"Workflow reference '{expression}' does not exist.")
            value = value[key]
            if index_text is not None:
                if not isinstance(value, (list, tuple)):
                    raise AutomationError("INVALID_WORKFLOW_REFERENCE", f"Workflow reference '{expression}' indexes a non-list value.")
                index = int(index_text)
                if index >= len(value):
                    raise AutomationError("INVALID_WORKFLOW_REFERENCE", f"Workflow reference '{expression}' indexes outside the available list results.")
                value = value[index]
    return value


def _resolve_templates(value: Any, inputs: dict[str, Any], results: dict[str, ToolResult]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_templates(item, inputs, results) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_templates(item, inputs, results) for item in value]
    if not isinstance(value, str):
        return value
    full = _REFERENCE.fullmatch(value)
    if full:
        return _lookup_reference(value, inputs, results)
    def replace(match):
        replacement = _lookup_reference(match.group(0), inputs, results)
        if isinstance(replacement, (dict, list, tuple)):
            raise AutomationError("INVALID_WORKFLOW_REFERENCE", "Structured values must occupy the entire argument value.")
        return "" if replacement is None else str(replacement)
    return _REFERENCE.sub(replace, value)
