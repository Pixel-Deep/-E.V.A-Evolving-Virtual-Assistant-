"""Read-only self-diagnostics for EVA's coding toolkit."""
from __future__ import annotations
import importlib.util
import platform
import shutil
import sys
from pathlib import Path


def run_doctor() -> dict:
    checks = []
    def add(name: str, ok: bool, detail: str, level: str = "info"):
        checks.append({"name": name, "status": "passed" if ok else level, "detail": detail})
    add("python_version", sys.version_info >= (3, 10), platform.python_version(), "warning")
    add("sqlite", importlib.util.find_spec("sqlite3") is not None, "SQLite is part of the standard library.", "error")
    add("json", importlib.util.find_spec("json") is not None, "JSON support is part of the standard library.", "error")
    for exe in ("python", "git", "node", "npm", "javac", "java", "gcc", "g++", "dotnet", "go", "rustc"):
        path = shutil.which(exe)
        add(f"tool_{exe}", path is not None, path or "Not installed or not on PATH.", "optional")
    try:
        from eva.coding.algorithms import binary_search, merge_sort, breadth_first_search, dijkstra
        add("algorithm_imports", True, "Bundled algorithm module imports.")
        add("algorithm_smoke", binary_search([1, 3, 5], 3) == 1 and merge_sort([3, 1, 2]) == [1, 2, 3]
            and breadth_first_search({"a": ["b"], "b": []}, "a") == ["a", "b"]
            and dijkstra({"a": [("b", 2)], "b": []}, "a")["b"] == 2,
            "Small deterministic algorithm sanity checks.", "error")
    except Exception as exc:
        add("algorithm_imports", False, f"{type(exc).__name__}: {exc}", "error")
    try:
        from eva.coding.blueprint import validate_blueprint
        result = validate_blueprint({"schema_version": 1, "name": "doctor", "files": [{"path": "main.py", "content": "print('ok')\n"}]})
        add("blueprint_engine", len(result["files"]) == 1, "Multi-file blueprint validation available.", "error")
    except Exception as exc:
        add("blueprint_engine", False, f"{type(exc).__name__}: {exc}", "error")
    return {"engine": "EVA Coding Engine", "mode": "read_only", "python": sys.version,
            "platform": platform.platform(), "checks": checks,
            "summary": {"passed": sum(c["status"] == "passed" for c in checks),
                        "optional_missing": sum(c["status"] == "optional" for c in checks),
                        "warnings": sum(c["status"] == "warning" for c in checks),
                        "errors": sum(c["status"] == "error" for c in checks)},
            "limitations": ["Doctor checks availability and small sanity cases, not full correctness.",
                            "No code is executed by this diagnostic."]}
