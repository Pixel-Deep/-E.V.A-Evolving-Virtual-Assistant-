"""Release packaging script with an explicit allow-list.

Creates a clean distribution archive without Git history, cache files,
runtime test artifacts, scratch scripts, or local secret files (.env).
"""
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files and directories to include in clean release
ALLOW_TOP_FILES = [
    "main.py",
    "requirements.txt",
    "README.md",
    ".env.example",
    "pytest.ini",
]

ALLOW_DIRS = [
    "eva",
    "config",
    "tests",
    "scripts",
    "docs",
]

EXCLUDE_PATTERNS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".vscode",
    ".idea",
    ".DS_Store",
    "Thumbs.db",
    "work",
    "scratch",
    "Documents",
    ".eva",
}

EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".log",
    ".jsonl",
    ".tmp",
    ".exe",
    ".obj",
    ".o",
}


def should_include_file(path: Path) -> bool:
    for part in path.parts:
        if part in EXCLUDE_PATTERNS:
            return False
    if path.suffix.lower() in EXCLUDE_EXTENSIONS:
        return False
    # Never package actual .env files
    if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
        return False
    return True


def build_release_zip(output_path: Path | None = None) -> Path:
    dist_dir = ROOT / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        output_path = dist_dir / "EVA-release.zip"

    print(f"Building clean release archive at {output_path}...")
    included_files: list[tuple[Path, str]] = []

    # 1. Top-level files
    for fname in ALLOW_TOP_FILES:
        fpath = ROOT / fname
        if fpath.is_file() and should_include_file(fpath):
            included_files.append((fpath, fname))

    # 2. Allowed directories
    for dname in ALLOW_DIRS:
        dpath = ROOT / dname
        if not dpath.is_dir():
            continue
        for root, dirs, files in os.walk(dpath):
            # Prune excluded directories in-place
            dirs[:] = [d for d in dirs if d not in EXCLUDE_PATTERNS]
            for file in files:
                fpath = Path(root) / file
                if should_include_file(fpath):
                    rel_path = fpath.relative_to(ROOT)
                    included_files.append((fpath, str(rel_path).replace("\\", "/")))

    # 3. Create zip
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fpath, arcname in sorted(included_files, key=lambda x: x[1]):
            zf.write(fpath, arcname)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"Package successfully created: {len(included_files)} files, {size_mb:.2f} MiB")
    return output_path


if __name__ == "__main__":
    out = build_release_zip()
    print(f"Release package ready: {out}")
