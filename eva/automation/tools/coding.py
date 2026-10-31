"""Safe bridges from EVA's registered automation tools to deterministic coding utilities.

These tools never execute generated source. Project scaffolding uses fixed reviewed
local templates, and validation uses bounded syntax/static checks with subprocess
execution disabled for source files.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from eva.automation.engine import AutomationError, ExecutionContext, ToolResult
from eva.automation.engine.models import RiskLevel, ToolSpec
from eva.automation.engine.registry import ToolRegistry
from eva.coding.construction import scaffold
from eva.coding.validators import validate_file


def _safe_rooted_path(raw: str, roots: tuple[Path, ...], *, must_exist: bool) -> Path:
    try:
        path = Path(raw).expanduser().resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise AutomationError("RESOURCE_NOT_FOUND", f"Could not resolve the coding path: {exc}") from exc
    if not any(path == root or root in path.parents for root in roots):
        raise AutomationError("PATH_OUTSIDE_APPROVED_ROOT", "Coding tools may access only paths inside configured approved roots.")
    return path


def register_coding_tools(registry: ToolRegistry, allowed_roots: list[str | Path] | tuple[str | Path, ...]) -> None:
    roots = tuple(Path(root).expanduser().resolve(strict=False) for root in allowed_roots)

    def validate_handler(context: ExecutionContext, args: dict[str, Any]) -> ToolResult:
        context.check_cancelled()
        target = _safe_rooted_path(args["path"], roots, must_exist=True)
        if not target.is_file():
            raise AutomationError("INVALID_ARGUMENT", "Coding validation requires a regular source file.")
        result = validate_file(target, timeout_seconds=args.get("timeout_seconds", 10))
        status = str(result.get("status", "unknown"))
        ok = status == "passed"
        message = str(result.get("message", "Static validation returned no message."))
        return ToolResult(
            ok=ok,
            tool="coding.validate_file",
            data={**result, "source_executed": False, "static_only": True},
            message=("Static validation passed; source code was not executed." if ok else message),
            error_code=None if ok else ("CODE_VALIDATION_FAILED" if status == "failed" else "CODE_VALIDATION_" + status.upper()),
            verified=True,
        )

    registry.register(ToolSpec(
        name="coding.validate_file",
        description="Run a bounded static/syntax validation on one existing source file; never executes the source program.",
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4096,
                      "description": "Existing source-file path inside an approved root."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30,
                                "description": "Maximum compiler/checker wait in seconds; source is not executed."}},
            "required": ["path"], "additionalProperties": False},
        permissions=("developer.test", "filesystem.read"), category="coding",
        timeout_seconds=35, idempotent=True, risk=RiskLevel.LOW,
        aliases=("validate code", "check source syntax", "static code validation"),
        when_to_use=("After creating or editing a supported source file, validate syntax/static structure before claiming the code is valid.",),
        output_description="Validation status, detected language, diagnostic, and an explicit source_executed=false field.",
        side_effects="Read-only except temporary compiler outputs isolated by the validator; generated/source files are not changed and source is never executed.",
    ), validate_handler)

    def scaffold_handler(context: ExecutionContext, args: dict[str, Any]) -> ToolResult:
        context.check_cancelled()
        target = _safe_rooted_path(args["target"], roots, must_exist=False)
        try:
            result = scaffold(args["template"], target, args["name"])
        except FileExistsError as exc:
            raise AutomationError("ALREADY_EXISTS", str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise AutomationError("SCAFFOLD_FAILED", str(exc)) from exc
        files = [str(item) for item in result.get("files", [])]
        verified_files = [str((target / rel).resolve(strict=False)) for rel in files
                          if (target / rel).is_file()]
        verified = len(verified_files) == len(files) and bool(files)
        return ToolResult(
            ok=verified,
            tool="coding.scaffold_project",
            data={**result, "verified_files": verified_files, "verified": verified,
                  "source_executed": False},
            message=("Deterministic project scaffold created and all listed files were verified." if verified
                     else "Scaffold creation returned, but not every listed file could be verified."),
            error_code=None if verified else "VERIFICATION_FAILED",
            verified=verified,
        )

    registry.register(ToolSpec(
        name="coding.scaffold_project",
        description="Create a new project from a fixed local Python CLI, Java console, or static website template; refuses to overwrite non-empty targets.",
        input_schema={"type": "object", "properties": {
            "template": {"type": "string", "enum": ["python-cli", "java-console", "web-static"],
                         "description": "One of EVA's deterministic reviewed starter templates."},
            "target": {"type": "string", "minLength": 1, "maxLength": 4096,
                       "description": "New project directory inside an approved root."},
            "name": {"type": "string", "minLength": 1, "maxLength": 64,
                     "description": "Human-readable project name; not a path."}},
            "required": ["template", "target", "name"], "additionalProperties": False},
        permissions=("filesystem.write",), category="coding", timeout_seconds=25,
        idempotent=False, risk=RiskLevel.LOW,
        aliases=("scaffold project", "create starter project", "make a project template"),
        when_to_use=("When a user asks for a standard starter project matching one of the built-in templates; prefer this deterministic tool over generating a template from scratch.",),
        output_description="Template ID, target path, created file list, and verification that every listed file exists.",
        side_effects="Creates new files only within approved roots; refuses to overwrite a non-empty target; does not execute generated source.",
    ), scaffold_handler)
