"""High-level deterministic coding workflows."""
from __future__ import annotations
from pathlib import Path
from eva.coding.diagnostics import classify_diagnostic
from eva.coding.store import CodingStore
from eva.coding.validators import validate_file
from eva.coding.workspace import inspect_workspace, LANGUAGE_EXTENSIONS


class CodingEngine:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = CodingStore(self.data_dir / "coding_knowledge.db")

    def inspect(self, workspace: str | Path) -> dict:
        report = inspect_workspace(workspace)
        self.store.record_experience(
            f"Inspect workspace: {report['root']}", outcome="success", category="workspace_inspection",
            evidence=f"files={report['file_count']}; languages={report['languages']}"
        )
        return report

    def validate(self, file_path: str | Path) -> dict:
        result = validate_file(file_path)
        outcome = "success" if result["status"] == "passed" else "failure" if result["status"] == "failed" else "partial"
        self.store.record_experience(f"Validate {result['path']}", language=result["language"], outcome=outcome,
                                     category="static_validation", evidence=result["message"],
                                     context={"status": result["status"], "command": result.get("command")})
        if result["status"] == "failed":
            result["diagnosis"] = classify_diagnostic(result["message"])
        return result


    def ingest_workspace(self, workspace: str | Path, *, max_files: int = 500, max_file_bytes: int = 256_000) -> dict:
        """Index readable project code/docs as unverified knowledge; never execute it."""
        root = Path(workspace).expanduser().resolve(strict=True)
        report = inspect_workspace(root, max_files=max(1, min(max_files, 5000)), max_file_bytes=max_file_bytes)
        indexed, skipped = 0, 0
        allowed_docs = {".md", ".rst", ".txt", ".toml", ".json", ".yaml", ".yml"}
        allowed_code = set(LANGUAGE_EXTENSIONS)
        for entry in report["files"]:
            rel = entry["path"]
            path = root / rel
            ext = path.suffix.lower()
            if ext not in allowed_code | allowed_docs or entry["size_bytes"] > max_file_bytes:
                skipped += 1
                continue
            try:
                body = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                skipped += 1
                continue
            if not body.strip():
                skipped += 1
                continue
            language = LANGUAGE_EXTENSIONS.get(ext, "general").lower().replace("javascript", "javascript")
            category = "code_sample" if ext in allowed_code else "documentation"
            self.store.add_knowledge(rel, body, language=language, category=category,
                                     source=f"local:{rel}", license="unknown", status="unverified")
            indexed += 1
        return {"workspace": str(root), "indexed": indexed, "skipped": skipped,
                "status": "unverified", "executed_files": False,
                "message": "Imported content is searchable but not trusted or executed. Review source and license before reuse."}

    def plan(self, request: str, workspace: str | Path) -> dict:
        """Produce a deterministic work plan; this does not claim semantic understanding."""
        text = request.strip()
        if not text:
            raise ValueError("Request must not be empty.")
        report = inspect_workspace(workspace)
        results = self.store.search_knowledge(text, limit=8)
        prior_cases = self.store.search_experiences(text, limit=5)
        verified_rules = self.store.search_verified_rules(text, limit=5)
        lowered = text.lower()
        action = "inspect_and_validate"
        if any(word in lowered for word in ("debug", "fix", "repair", "error", "bug")):
            action = "diagnose_then_validate"
        elif any(word in lowered for word in ("test", "tests", "benchmark")):
            action = "identify_tests_then_validate"
        elif any(word in lowered for word in ("create", "build", "generate", "implement", "write")):
            action = "specify_then_construct_then_validate"
        return {"request": text, "mode": "deterministic_plan", "action": action,
                "workspace": report["root"], "languages": report["languages"],
                "candidate_knowledge": [{"id": x["id"], "title": x["title"], "language": x["language"],
                                          "status": x["status"], "score": x["score"], "source": x["source"]} for x in results],
                "prior_experience": [{"id": x["id"], "task": x["task"], "outcome": x["outcome"], "category": x["category"], "score": x["score"]} for x in prior_cases],
                "verified_rules": [{"id": x["id"], "title": x["title"], "language": x["language"], "score": x["score"]} for x in verified_rules],
                "steps": ["Clarify measurable requirements", "Inspect relevant files and dependencies",
                          "Retrieve compatible knowledge and prior verified fixes", "Prepare a reviewable change",
                          "Run static checks and approved tests", "Record evidence and unresolved limitations"],
                "limitations": ["This planner uses deterministic keyword and metadata rules; it does not infer arbitrary intent.",
                                "It does not automatically edit files or execute project code."]}
