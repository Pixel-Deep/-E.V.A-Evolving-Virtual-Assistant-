"""Run EVA's repeatable local release checks from the repository root.

This script deliberately avoids external-network and live-desktop probes. Those
are opt-in because they depend on the user's machine, browser, model, and network.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = [
    ("Python compilation", [sys.executable, "-m", "compileall", "-q", "eva", "main.py", "tests", "scripts"]),
    ("Complete test suite", [sys.executable, "-m", "pytest", "-q"]),
    ("Deterministic runtime audit", [sys.executable, "scripts/eva_runtime_tool_audit.py"]),
    ("One-by-one tool/orchestration contract audit", [sys.executable, "scripts/audit_tool_orchestration.py", "--check-only"]),
]


def main() -> int:
    failures: list[str] = []
    for label, command in CHECKS:
        print(f"\n=== {label} ===", flush=True)
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode != 0:
            failures.append(f"{label} (exit {completed.returncode})")
    print("\n=== Release validation summary ===")
    if failures:
        print("FAIL: " + "; ".join(failures))
        return 1
    print("PASS: compilation, all configured tests, deterministic tool/workflow audit, and one-by-one registry/shortlist audit.")
    print("Note: local-model, live Windows UI, and external-network probes are not included.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
