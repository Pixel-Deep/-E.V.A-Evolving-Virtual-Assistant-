"""CLI for EVA's model-independent coding engine."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from eva.coding.engine import CodingEngine
from eva.coding.construction import scaffold, available_templates
from eva.coding.repair import suggest_repairs
from eva.coding.patches import propose_text_patch, save_patch_proposal, apply_patch_proposal
from eva.coding.algorithms import ALGORITHM_METADATA
from eva.coding.blueprint import load_blueprint, build_blueprint
from eva.coding.doctor import run_doctor
import inspect
import eva.coding.algorithms as algorithm_module


def _engine(args) -> CodingEngine:
    data = args.data_dir or os.environ.get("EVA_CODING_DATA", str(Path.home() / ".eva" / "coding"))
    return CodingEngine(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="EVA deterministic Coding Engine (no LLM required)")
    parser.add_argument("--data-dir", help="Directory for persistent SQLite knowledge and experience")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inspect", help="Read-only workspace inventory")
    p.add_argument("workspace")
    p = sub.add_parser("validate", help="Static validation; does not run the target file")
    p.add_argument("file")
    p = sub.add_parser("plan", help="Build a deterministic task plan")
    p.add_argument("workspace")
    p.add_argument("request")
    p = sub.add_parser("ingest", help="Index local code/docs as unverified knowledge; never executes files")
    p.add_argument("workspace")
    p.add_argument("--max-files", type=int, default=500)
    p = sub.add_parser("knowledge-add", help="Store knowledge as unverified by default")
    p.add_argument("title"); p.add_argument("body_file"); p.add_argument("--language", default="general")
    p.add_argument("--category", default="reference"); p.add_argument("--source", default="local")
    p.add_argument("--license", default="unknown")
    p = sub.add_parser("search", help="Search indexed knowledge")
    p.add_argument("query"); p.add_argument("--language"); p.add_argument("--verified-only", action="store_true")
    p = sub.add_parser("failure", help="Record a failure/experience")
    p.add_argument("task"); p.add_argument("evidence"); p.add_argument("--language", default="unknown")
    p.add_argument("--category", default="unknown"); p.add_argument("--outcome", choices=["failure", "partial", "unresolved", "success"], default="failure")
    p = sub.add_parser("seed", help="Load bundled coding references into the local database as unverified knowledge")
    p = sub.add_parser("templates", help="List built-in deterministic project templates")
    p = sub.add_parser("scaffold", help="Create a new project from a deterministic template; never overwrites")
    p.add_argument("template", choices=["python-cli", "java-console", "web-static"])
    p.add_argument("target")
    p.add_argument("--name", required=True)
    sub.add_parser("algorithms", help="List bundled, unit-tested deterministic algorithms")
    p = sub.add_parser("algorithm-export", help="Export a bundled algorithm as a new Python file; refuses overwrite")
    p.add_argument("name", choices=sorted(ALGORITHM_METADATA))
    p.add_argument("target")
    p = sub.add_parser("diagnose", help="Suggest evidence-based repair steps from a diagnostic")
    p.add_argument("diagnostic", help="Quote the full diagnostic")
    p = sub.add_parser("patch-propose", help="Create a reviewable patch proposal from a replacement file")
    p.add_argument("target")
    p.add_argument("replacement_file")
    p.add_argument("proposal_file")
    p = sub.add_parser("patch-apply", help="Apply a previously reviewed patch proposal after explicit confirmation")
    p.add_argument("proposal_file")
    p = sub.add_parser("blueprint-check", help="Validate a general multi-file JSON project blueprint without writing files")
    p.add_argument("blueprint")
    p = sub.add_parser("blueprint-build", help="Create a multi-file project from a JSON blueprint; never overwrites")
    p.add_argument("blueprint")
    p.add_argument("target")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("doctor", help="Read-only self-diagnostics and optional toolchain discovery")
    sub.add_parser("stats", help="Show knowledge and experience counts")
    p = sub.add_parser("tests", help="Run tests only after interactive confirmation")
    p.add_argument("workspace", nargs="?", default="."); p.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    engine = _engine(args)
    try:
        if args.command == "inspect":
            result = engine.inspect(args.workspace)
        elif args.command == "validate":
            result = engine.validate(args.file)
        elif args.command == "plan":
            result = engine.plan(args.request, args.workspace)
        elif args.command == "ingest":
            result = engine.ingest_workspace(args.workspace, max_files=args.max_files)
        elif args.command == "knowledge-add":
            body_path = Path(args.body_file).resolve(strict=True)
            body = body_path.read_text(encoding="utf-8")
            result = {"id": engine.store.add_knowledge(args.title, body, language=args.language,
                       category=args.category, source=args.source, license=args.license, status="unverified"),
                      "status": "unverified", "message": "Stored; not trusted until separately validated."}
        elif args.command == "search":
            result = engine.store.search_knowledge(args.query, language=args.language, include_unverified=not args.verified_only)
        elif args.command == "failure":
            result = {"experience_id": engine.store.record_experience(args.task, language=args.language,
                      outcome=args.outcome, category=args.category, evidence=args.evidence)}
        elif args.command == "seed":
            pack = Path(__file__).with_name("knowledge_pack")
            added = 0
            for item in sorted(pack.rglob("*.md")):
                body = item.read_text(encoding="utf-8")
                rel = item.relative_to(pack).as_posix()
                engine.store.add_knowledge(item.stem.replace("_", " ").title(), body,
                    language="general", category=rel.split("/", 1)[0], source=f"bundled:{rel}",
                    license="EVA bundled reference; review before redistribution", status="unverified")
                added += 1
            result = {"status": "loaded", "items": added, "trust": "unverified", "message": "Bundled references are searchable but are not executable code or automatically trusted."}
        elif args.command == "templates":
            result = available_templates()
        elif args.command == "scaffold":
            result = scaffold(args.template, args.target, args.name)
        elif args.command == "algorithms":
            result = [{"name": name, **meta} for name, meta in sorted(ALGORITHM_METADATA.items())]
        elif args.command == "algorithm-export":
            target = Path(args.target).expanduser().resolve()
            if target.exists():
                raise FileExistsError("Target already exists; EVA will not overwrite it.")
            target.parent.mkdir(parents=True, exist_ok=True)
            function = getattr(algorithm_module, args.name)
            preludes = {
                "binary_search": "from collections.abc import Sequence\nfrom typing import TypeVar\nT = TypeVar('T')\n\n",
                "merge_sort": "from collections.abc import Callable, Iterable\nfrom typing import TypeVar\nT = TypeVar('T')\n\n",
                "breadth_first_search": "from collections import deque\nfrom collections.abc import Hashable, Iterable, Mapping\nfrom typing import TypeVar\nV = TypeVar('V', bound=Hashable)\n\n",
                "dijkstra": "import heapq\nfrom collections.abc import Hashable, Iterable, Mapping\nfrom typing import TypeVar\nV = TypeVar('V', bound=Hashable)\n\n",
            }
            target.write_text("from __future__ import annotations\n" + preludes[args.name] + inspect.getsource(function), encoding="utf-8")
            result = {"status": "exported", "algorithm": args.name, "target": str(target), "note": "Review and test in your project before use."}
        elif args.command == "diagnose":
            result = suggest_repairs(args.diagnostic)
        elif args.command == "patch-propose":
            replacement = Path(args.replacement_file).resolve(strict=True).read_text(encoding="utf-8")
            proposal = propose_text_patch(args.target, replacement)
            proposal_path = save_patch_proposal(proposal, args.proposal_file)
            result = {"status": "review_required", "proposal_file": str(proposal_path), "diff": proposal["diff"], "changed": proposal["changed"]}
        elif args.command == "patch-apply":
            proposal_path = Path(args.proposal_file).resolve(strict=True)
            print("Review the proposal diff and ensure the target is safe to modify.")
            answer = input("Type APPLY PATCH to approve the exact proposal: ").strip()
            result = apply_patch_proposal(proposal_path, approved=(answer == "APPLY PATCH"))
        elif args.command == "blueprint-check":
            spec = load_blueprint(args.blueprint)
            result = {"status": "valid", "name": spec["name"], "file_count": len(spec["files"]),
                      "total_bytes": spec["total_bytes"],
                      "files": [{"path": f["path"], "bytes": f["bytes"], "sha256": f["sha256"]} for f in spec["files"]],
                      "executed_code": False, "written_files": False}
        elif args.command == "blueprint-build":
            result = build_blueprint(load_blueprint(args.blueprint), args.target, dry_run=args.dry_run)
        elif args.command == "doctor":
            result = run_doctor()
        elif args.command == "stats":
            result = engine.store.stats()
        elif args.command == "tests":
            root = Path(args.workspace).resolve(strict=True)
            if not root.is_dir():
                raise ValueError("Workspace must be a directory")
            print(f"About to execute Python unittest discovery in: {root}")
            print("Tests are code and may have side effects. Review the project before approving.")
            answer = input("Type RUN TESTS to continue: ").strip()
            if answer != "RUN TESTS":
                print("Cancelled; no tests executed.")
                return 2
            try:
                completed = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"],
                    cwd=str(root), capture_output=True, text=True, timeout=max(1, min(args.timeout, 600)), shell=False)
                result = {"returncode": completed.returncode, "stdout": completed.stdout[-12000:],
                          "stderr": completed.stderr[-12000:], "executed_tests": True,
                          "note": "This is confirmation-gated execution, not an OS sandbox."}
                engine.store.record_experience(f"unittest discovery in {root}", language="python",
                    outcome="success" if completed.returncode == 0 else "failure", category="test_run",
                    evidence=(completed.stdout + "\n" + completed.stderr)[-8000:],
                    context={"returncode": completed.returncode})
            except subprocess.TimeoutExpired as exc:
                result = {"returncode": None, "status": "timeout", "executed_tests": True,
                          "message": f"Test process exceeded {args.timeout}s"}
        else:
            parser.error("Unknown command")
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        if args.command == "validate" and result["status"] == "failed":
            return 1
        if args.command == "tests" and result.get("returncode", 0) not in (0, None):
            return 1
        return 0
    except (OSError, ValueError, UnicodeError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
