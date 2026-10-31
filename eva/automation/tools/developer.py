"""Read-only developer inspection helpers; no shell interpolation or source execution."""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any

from eva.automation.engine.models import AutomationError, ExecutionContext, ToolResult, ToolSpec
from eva.automation.engine.registry import ToolRegistry
from eva.automation.tools.filesystem import _is_sensitive, _resolve_safe_path, _root_paths

_MAX_FILES = 1500
_MAX_FILE_BYTES = 2 * 1024 * 1024
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".tox"}


def _git_status(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    requested = _resolve_safe_path(args["path"], roots)
    if not requested.exists() or not requested.is_dir():
        raise AutomationError("RESOURCE_NOT_FOUND", "Project path must be an existing directory within an approved root.")
    try:
        root_result = subprocess.run(["git", "-C", str(requested), "rev-parse", "--show-toplevel"],
                                     capture_output=True, text=True, timeout=8, check=False)
    except FileNotFoundError as exc:
        raise AutomationError("DEPENDENCY_MISSING", "Git executable was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AutomationError("TIMEOUT", "Git repository discovery exceeded 8 seconds.") from exc
    if root_result.returncode != 0:
        detail = (root_result.stderr or root_result.stdout).strip()[:1000]
        raise AutomationError("NOT_A_GIT_REPOSITORY", detail or "The selected directory is not inside a Git repository.")
    repo = Path(root_result.stdout.strip()).resolve(strict=False)
    # Resolve and re-check the repository root; a directory's .git can be a redirect.
    _resolve_safe_path(str(repo), roots)
    outputs: dict[str, str] = {"repository": str(repo)}
    commands = {
        "branch": ["branch", "--show-current"],
        "status": ["status", "--short", "--branch"],
        "diff_stat": ["diff", "--stat"],
    }
    for key, tail in commands.items():
        context.check_cancelled()
        try:
            result = subprocess.run(["git", "-C", str(repo), *tail], capture_output=True,
                                    text=True, timeout=8, check=False)
        except subprocess.TimeoutExpired as exc:
            raise AutomationError("TIMEOUT", f"git {' '.join(tail)} exceeded 8 seconds.") from exc
        if result.returncode != 0:
            raise AutomationError("GIT_COMMAND_FAILED", f"git {' '.join(tail)} failed: {(result.stderr or result.stdout).strip()[:1000]}")
        outputs[key] = result.stdout.strip()[:20_000]
    return ToolResult(True, "developer.git_status", outputs,
                      "Git branch, working-tree status, and diff summary read successfully; no files changed.", verified=True)


def _inspect_project(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    base = _resolve_safe_path(args["path"], roots)
    if not base.exists() or not base.is_dir():
        raise AutomationError("RESOURCE_NOT_FOUND", "Project path must be an existing directory within an approved root.")
    counts: dict[str, int] = {}
    python_issues: list[dict[str, Any]] = []
    total_files = 0
    skipped_sensitive = 0
    truncated = False
    tree_entries: list[str] = []
    entry_points: list[dict[str, str]] = []
    max_tree_entries = 160
    entry_point_names = {
        "main.py": "Common Python application entry point",
        "app.py": "Common Python application entry point",
        "__main__.py": "Python package/module entry point",
        "server.py": "Common Python server entry point",
        "main.js": "Common JavaScript application entry point",
        "index.js": "Common JavaScript application entry point",
        "index.ts": "Common TypeScript application entry point",
        "main.ts": "Common TypeScript application entry point",
        "program.cs": "Common .NET application entry point",
        "main.go": "Common Go application entry point",
        "main.rs": "Common Rust application entry point",
        "pom.xml": "Java/Maven project descriptor; inspect configured main class",
        "package.json": "JavaScript/TypeScript project manifest; inspect scripts/main field",
        "pyproject.toml": "Python project manifest; inspect script/entry-point configuration",
        "Cargo.toml": "Rust project manifest; inspect src/main.rs or bin targets",
    }
    for current, dirs, files in __import__("os").walk(base, followlinks=False):
        context.check_cancelled()
        here = Path(current)
        dirs[:] = [d for d in dirs if d.lower() not in _SKIP and not _is_sensitive(here / d)
                   and not (here / d).is_symlink()]
        if len(tree_entries) < max_tree_entries:
            for dirname in sorted(dirs, key=str.casefold):
                rel = (here / dirname).relative_to(base).as_posix() + "/"
                if len(tree_entries) < max_tree_entries:
                    tree_entries.append(rel)
        for name in files:
            context.check_cancelled()
            candidate = here / name
            if candidate.is_symlink() or _is_sensitive(candidate):
                skipped_sensitive += 1
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            total_files += 1
            if len(tree_entries) < max_tree_entries:
                tree_entries.append(candidate.relative_to(base).as_posix())
            if name.casefold() in entry_point_names:
                entry_points.append({"path": str(candidate), "relative_path": candidate.relative_to(base).as_posix(),
                                     "reason": entry_point_names[name.casefold()]})
            if total_files > _MAX_FILES:
                truncated = True
                break
            suffix = candidate.suffix.lower() or "[no extension]"
            counts[suffix] = counts.get(suffix, 0) + 1
            if suffix == ".py" and size <= _MAX_FILE_BYTES:
                try:
                    source = candidate.read_text(encoding="utf-8")
                    ast.parse(source, filename=str(candidate))
                except (OSError, UnicodeDecodeError) as exc:
                    python_issues.append({"path": str(candidate), "issue": f"Could not read as UTF-8: {exc}"})
                except SyntaxError as exc:
                    python_issues.append({"path": str(candidate), "line": exc.lineno,
                                          "column": exc.offset, "issue": exc.msg})
        if truncated:
            break
    priority = {"main.py": 0, "app.py": 1, "__main__.py": 2, "main.js": 3,
                "index.js": 4, "index.ts": 5, "main.ts": 6, "server.py": 7,
                "program.cs": 8, "main.go": 9, "main.rs": 10}
    entry_points.sort(key=lambda item: (priority.get(Path(item["path"]).name.casefold(), 50), item["relative_path"].casefold()))
    return ToolResult(True, "developer.inspect_project", {
        "path": str(base), "files_inspected": min(total_files, _MAX_FILES),
        "extension_counts": dict(sorted(counts.items())), "python_syntax_issues": python_issues,
        "python_files_with_syntax_issues": len(python_issues), "skipped_sensitive_paths": skipped_sensitive,
        "tree": tree_entries, "tree_entries_returned": len(tree_entries),
        "tree_truncated": len(tree_entries) >= max_tree_entries or truncated,
        "entry_point_candidates": entry_points[:50],
        "truncated": truncated, "tests_executed": False, "source_executed": False,
    }, "Read-only project inspection completed. Folder structure and likely entry-point candidates are included; Python files were parsed but no source code or tests were executed.", verified=True)


def register_developer_tools(registry: ToolRegistry, allowed_roots: list[str | Path] | tuple[str | Path, ...] | None = None) -> None:
    roots = _root_paths(allowed_roots)
    project_schema = {"type": "object", "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["path"], "additionalProperties": False}
    registry.register(ToolSpec(
        "developer.git_status", "Read Git repository root, current branch, working-tree status, and diff statistics without modifying files.",
        project_schema, ("developer.read",), "developer", timeout_seconds=30,
        aliases=("git status", "git diff summary", "current branch")),
        lambda ctx, args: _git_status(ctx, args, roots))
    registry.register(ToolSpec(
        "developer.inspect_project", "Read-only project inventory and Python syntax inspection; does not execute source code or tests.",
        project_schema, ("developer.read",), "developer", timeout_seconds=30,
        aliases=("project health report", "inspect project")),
        lambda ctx, args: _inspect_project(ctx, args, roots))
