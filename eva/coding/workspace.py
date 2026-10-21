"""Read-only project inspection with conservative directory and file limits."""
from __future__ import annotations
import hashlib
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".mypy_cache", ".pytest_cache", "target"}
SECRET_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
LANGUAGE_EXTENSIONS = {
    ".py": "Python", ".pyi": "Python", ".java": "Java", ".js": "JavaScript", ".jsx": "JavaScript/JSX",
    ".ts": "TypeScript", ".tsx": "TypeScript/TSX", ".c": "C", ".h": "C/C++ header", ".cpp": "C++",
    ".cc": "C++", ".hpp": "C++ header", ".cs": "C#", ".go": "Go", ".rs": "Rust", ".php": "PHP",
    ".rb": "Ruby", ".swift": "Swift", ".kt": "Kotlin", ".html": "HTML", ".css": "CSS", ".sql": "SQL",
}


def inspect_workspace(root: str | Path, *, max_files: int = 5000, max_file_bytes: int = 1_000_000) -> dict:
    base = Path(root).expanduser().resolve(strict=True)
    if not base.is_dir():
        raise ValueError("Workspace must be a directory.")
    files, languages, manifests, tests, total_bytes = [], {}, [], [], 0
    for path in base.rglob("*"):
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if any(part.lower() in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.lower() in SECRET_NAMES or path.name.lower().startswith(".env."):
            continue
        if len(files) >= max_files:
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_file_bytes:
            continue
        ext = path.suffix.lower()
        lang = LANGUAGE_EXTENSIONS.get(ext)
        if lang:
            languages[lang] = languages.get(lang, 0) + 1
        name = path.name.lower()
        if name in {"pyproject.toml", "requirements.txt", "package.json", "pom.xml", "build.gradle", "cargo.toml", "go.mod", "makefile", "cmakelists.txt"}:
            manifests.append(rel.as_posix())
        if name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{rel.as_posix().lower()}/" or "/test/" in f"/{rel.as_posix().lower()}/":
            tests.append(rel.as_posix())
        files.append({"path": rel.as_posix(), "size_bytes": size, "language": lang or "Other"})
        total_bytes += size
    return {"root": str(base), "file_count": len(files), "total_bytes": total_bytes,
            "languages": dict(sorted(languages.items())), "manifests": sorted(manifests),
            "tests": sorted(tests), "files": files, "truncated": len(files) >= max_files}


def sha256_file(path: str | Path, *, max_bytes: int = 1_000_000) -> str:
    p = Path(path)
    data = p.read_bytes()
    if len(data) > max_bytes:
        raise ValueError("File exceeds hashing size limit.")
    return hashlib.sha256(data).hexdigest()
