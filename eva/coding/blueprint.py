"""General multi-file project blueprints with strict path and overwrite guards.

Blueprints are explicit JSON authored by a human or another trusted process. This
module does not infer code from natural language and never executes generated code.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

MAX_BLUEPRINT_BYTES = 10 * 1024 * 1024
MAX_FILES = 1000
MAX_FILE_BYTES = 2 * 1024 * 1024
_RESERVED = {".env", ".env.local", "id_rsa", "id_ed25519", "credentials.json", "secrets.json"}


def _validate_relative_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Each blueprint file needs a non-empty relative path.")
    if "\\" in raw or "\x00" in raw:
        raise ValueError(f"Unsafe path {raw!r}: use relative POSIX paths without backslashes.")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe relative path: {raw!r}")
    if re.match(r"^[A-Za-z]:", raw) or raw.startswith("//"):
        raise ValueError(f"Absolute or drive-qualified path is not allowed: {raw!r}")
    if any(part.lower() in _RESERVED or part.lower().startswith(".env.") for part in path.parts):
        raise ValueError(f"Blueprint cannot create likely secret files: {raw!r}")
    if path.parts and path.parts[0].lower() == ".git":
        raise ValueError("Blueprint cannot write into .git.")
    return path.as_posix()


def load_blueprint(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("Blueprint path must point to a JSON file.")
    raw = source.read_bytes()
    if len(raw) > MAX_BLUEPRINT_BYTES:
        raise ValueError(f"Blueprint exceeds {MAX_BLUEPRINT_BYTES} byte limit.")
    try:
        spec = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid UTF-8 JSON blueprint: {exc}") from exc
    return validate_blueprint(spec)


def validate_blueprint(spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("Blueprint root must be a JSON object.")
    if spec.get("schema_version") != 1:
        raise ValueError("schema_version must be integer 1.")
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("name must be a non-empty string of at most 120 characters.")
    description = spec.get("description", "")
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("description must be text no longer than 4000 characters.")
    files = spec.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("files must be a non-empty array.")
    if len(files) > MAX_FILES:
        raise ValueError(f"Blueprint may contain at most {MAX_FILES} files.")
    seen: set[str] = set()
    total = 0
    clean_files = []
    for item in files:
        if not isinstance(item, dict) or set(item) - {"path", "content"}:
            raise ValueError("Each file entry must contain only path and content.")
        rel = _validate_relative_path(item.get("path"))
        key = rel.casefold()
        if key in seen:
            raise ValueError(f"Duplicate or case-colliding path: {rel!r}")
        seen.add(key)
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"Content for {rel!r} must be a string.")
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValueError(f"File {rel!r} exceeds {MAX_FILE_BYTES} bytes.")
        total += size
        clean_files.append({"path": rel, "content": content, "bytes": size,
                            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})
    if total > MAX_BLUEPRINT_BYTES:
        raise ValueError(f"Combined file content exceeds {MAX_BLUEPRINT_BYTES} bytes.")
    return {"schema_version": 1, "name": name.strip(), "description": description,
            "files": clean_files, "total_bytes": total}


def build_blueprint(spec: dict[str, Any], target: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Create a multi-file project without overwriting any existing files.

    A target may be absent or an existing empty directory. The function does not
    execute code, install dependencies, or claim that generated code is correct.
    """
    # `load_blueprint` returns normalized entries with computed metadata; strip
    # those derived fields before applying the strict public input schema again.
    raw_spec = dict(spec)
    raw_spec["files"] = [{"path": item["path"], "content": item["content"]} for item in spec.get("files", [])]
    clean = validate_blueprint(raw_spec)
    target_path = Path(target).expanduser()
    # Check the original target and existing parent components before resolving.
    try:
        st = os.lstat(target_path)
        if stat.S_ISLNK(st.st_mode) or target_path.is_symlink() or os.path.islink(target_path):
            raise FileExistsError(f"Target cannot be a symbolic link: {target_path}")
    except FileNotFoundError:
        pass

    curr = target_path
    while curr != curr.parent:
        try:
            st = os.lstat(curr)
            if stat.S_ISLNK(st.st_mode) or curr.is_symlink() or os.path.islink(curr):
                raise FileExistsError(f"Target path or parent cannot be a symbolic link: {curr}")
        except FileNotFoundError:
            pass
        except OSError:
            pass
        curr = curr.parent

    dest = target_path.resolve()
    if dest.exists():
        if dest.is_symlink() or os.path.islink(dest) or not dest.is_dir() or any(dest.iterdir()):
            raise FileExistsError("Target must be absent or an empty directory; existing data is never overwritten.")
    if dry_run:
        return {"status": "dry_run", "name": clean["name"], "target": str(dest),
                "file_count": len(clean["files"]), "total_bytes": clean["total_bytes"],
                "files": [{"path": x["path"], "bytes": x["bytes"], "sha256": x["sha256"]} for x in clean["files"]],
                "executed_code": False}
    created_root = not dest.exists()
    if created_root:
        dest.mkdir(parents=True, exist_ok=False)
    written: list[Path] = []
    try:
        for item in clean["files"]:
            out = dest.joinpath(*PurePosixPath(item["path"]).parts)
            # Re-check containment after resolving parents; reject symlinks in the tree.
            out.parent.mkdir(parents=True, exist_ok=True)
            parent_resolved = out.parent.resolve()
            if parent_resolved != dest and dest not in parent_resolved.parents:
                raise ValueError(f"Output path escaped target: {item['path']!r}")
            if out.is_symlink() or out.exists():
                raise FileExistsError(f"Refusing to overwrite existing or symlink path: {item['path']!r}")
            with out.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(item["content"])
            written.append(out)
        manifest = {"generated_by": "EVA blueprint engine", "schema_version": 1,
                    "name": clean["name"], "description": clean["description"],
                    "verification": "not_run", "executed_code": False,
                    "files": [{"path": x["path"], "bytes": x["bytes"], "sha256": x["sha256"]}
                              for x in clean["files"]]}
        manifest_path = dest / ".eva-blueprint-manifest.json"
        if manifest_path.exists():
            raise FileExistsError("Reserved manifest path already exists.")
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(manifest_path)
    except Exception:
        for path in reversed(written):
            try:
                path.unlink()
            except OSError:
                pass
        if created_root:
            # Remove only directories made empty by this operation.
            for path in sorted(dest.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if path.is_dir() and not path.is_symlink():
                    try: path.rmdir()
                    except OSError: pass
            try: dest.rmdir()
            except OSError: pass
        raise
    return {"status": "created", "name": clean["name"], "target": str(dest),
            "file_count": len(clean["files"]), "total_bytes": clean["total_bytes"],
            "files": [x["path"] for x in clean["files"]], "manifest": str(manifest_path),
            "verification": "not_run", "executed_code": False,
            "next_steps": ["Review generated files", "Run EVA static validation", "Review project before running its tests"]}
