"""Bounded filesystem tools restricted to explicitly approved roots."""
from __future__ import annotations

import errno
import fnmatch
import hashlib
import html
import os
import re
import zipfile
from datetime import datetime
from itertools import chain, islice
from pathlib import Path
from time import monotonic
from typing import Any

from eva.automation.dates import date_slug
from eva.automation.engine.models import AutomationError, ExecutionContext, RiskLevel, ToolResult, ToolSpec
from eva.automation.engine.registry import ToolRegistry

_SKIPPED_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules", "$recycle.bin", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", "target", "tmp", "temp"}
_SENSITIVE_NAMES = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".pki", "credentials",
    "login data", "cookies", "wallet.dat", "id_rsa", "id_ed25519", ".pypirc",
}
_SENSITIVE_SUFFIXES = {".env", ".pem", ".key", ".p12", ".pfx"}
_MAX_TEXT_BYTES = 1_048_576
_MAX_COPY_BYTES = 536_870_912
_MAX_TRAVERSED_DIRS = 5000
_MAX_TRAVERSAL_SECONDS = 15.0
_MAX_NONRECURSIVE_ENTRIES = 20_000


def _is_sensitive(path: Path) -> bool:
    for part in path.parts:
        lowered = part.lower()
        if lowered in _SENSITIVE_NAMES or lowered.endswith(tuple(_SENSITIVE_SUFFIXES)):
            return True
        if lowered.startswith(".env."):
            return True
    return False


def _resolve_safe_path(raw: str, roots: tuple[Path, ...], *, purpose: str = "access") -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise AutomationError("INVALID_ARGUMENT", "A non-empty path is required.")
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise AutomationError("INVALID_PATH", f"Could not resolve path: {exc}") from exc
    allowed = False
    for root in roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise AutomationError("PATH_OUTSIDE_ALLOWED_ROOTS", "This path is outside EVA's approved automation folders.")
    if _is_sensitive(resolved):
        raise AutomationError("SENSITIVE_PATH_BLOCKED", "EVA's filesystem tools do not access credential or private-key paths.")
    # Protect against traversing through a symlink that resolves outside a root.
    if purpose in {"write", "copy", "move"} and resolved.exists() and resolved.is_dir() and resolved.is_symlink():
        raise AutomationError("INVALID_PATH", "Writing through a directory symlink is not allowed.")
    return resolved


def _root_paths(allowed_roots: list[str | Path] | tuple[str | Path, ...] | None) -> tuple[Path, ...]:
    values = [Path.home()] if allowed_roots is None else allowed_roots
    roots: list[Path] = []
    for value in values:
        root = Path(value).expanduser().resolve(strict=False)
        if root not in roots:
            roots.append(root)
    if not roots:
        raise ValueError("At least one filesystem allow-root is required.")
    return tuple(roots)


def _list_files(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    base = _resolve_safe_path(args["path"], roots)
    if not base.exists() or not base.is_dir():
        raise AutomationError("RESOURCE_NOT_FOUND", "The requested directory does not exist.")
    limit = args.get("limit", 200)
    recursive = args.get("recursive", False)
    entries: list[dict[str, Any]] = []
    truncated = False
    deadline = monotonic() + _MAX_TRAVERSAL_SECONDS
    visited_dirs = 0
    if recursive:
        for current, dirs, files in os.walk(base, followlinks=False):
            context.check_cancelled()
            if monotonic() >= deadline or visited_dirs >= _MAX_TRAVERSED_DIRS:
                truncated = True
                break
            visited_dirs += 1
            current_path = Path(current)
            dirs[:] = [d for d in dirs if d.lower() not in _SKIPPED_DIRS and not _is_sensitive(current_path / d)]
            available_count = len(dirs) + len(files)
            available_names = list(islice(chain(dirs, files), _MAX_NONRECURSIVE_ENTRIES))
            if available_count > _MAX_NONRECURSIVE_ENTRIES:
                truncated = True
            for name in sorted(available_names, key=str.lower):
                item = current_path / name
                if item.is_symlink() or _is_sensitive(item):
                    continue
                entries.append(_entry(item))
                if len(entries) >= limit:
                    truncated = True
                    break
            if truncated:
                break
    else:
        # Cap before sorting so a directory with millions of entries cannot make
        # the sorting step itself unbounded.
        limited_items = []
        with os.scandir(base) as scan:
            for index, item in enumerate(scan):
                context.check_cancelled()
                if monotonic() >= deadline or index >= _MAX_NONRECURSIVE_ENTRIES:
                    truncated = True
                    break
                limited_items.append(Path(item.path))
        for item in sorted(limited_items, key=lambda p: (not p.is_dir(), p.name.lower())):
            context.check_cancelled()
            if item.is_symlink() or _is_sensitive(item):
                continue
            entries.append(_entry(item))
            if len(entries) >= limit:
                truncated = True
                break
    return ToolResult(True, "files.list", {
        "path": str(base), "entries": entries, "count": len(entries), "truncated": truncated,
        "visited_directories": visited_dirs if recursive else 0,
    }, "Directory listing completed within traversal limits.", verified=True)


def _entry(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        return {"name": path.name, "path": str(path), "is_directory": path.is_dir(),
                "size_bytes": None if path.is_dir() else stat.st_size,
                "modified_epoch": int(stat.st_mtime)}
    except OSError:
        return {"name": path.name, "path": str(path), "is_directory": path.is_dir(), "size_bytes": None, "modified_epoch": None}


def _search_files(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    base = _resolve_safe_path(args["path"], roots)
    pattern = args["pattern"].strip()
    if not pattern or len(pattern) > 128 or pattern in {".", ".."} or "/" in pattern or "\\" in pattern:
        raise AutomationError("INVALID_ARGUMENT", "pattern must be a filename or glob pattern, not a path.")
    if not base.exists() or not base.is_dir():
        raise AutomationError("RESOURCE_NOT_FOUND", "The requested search directory does not exist.")
    recursive = args.get("recursive", True)
    limit = args.get("limit", 200)
    excluded_directories = []
    for raw_excluded in args.get("exclude_directories", []):
        try:
            excluded_directories.append(_resolve_safe_path(str(raw_excluded), roots).resolve(strict=False))
        except AutomationError:
            # Invalid exclusions fail closed: do not silently ignore a requested
            # boundary and broaden the search. The schema and planner use approved paths.
            raise

    def is_excluded(candidate: Path) -> bool:
        resolved_candidate = candidate.resolve(strict=False)
        for excluded in excluded_directories:
            if resolved_candidate == excluded or excluded in resolved_candidate.parents:
                return True
        return False

    matches: list[dict[str, Any]] = []
    truncated = False
    deadline = monotonic() + _MAX_TRAVERSAL_SECONDS
    visited_dirs = 0
    if recursive:
        for current, dirs, files in os.walk(base, followlinks=False):
            context.check_cancelled()
            if monotonic() >= deadline or visited_dirs >= _MAX_TRAVERSED_DIRS:
                truncated = True
                break
            visited_dirs += 1
            here = Path(current)
            if is_excluded(here):
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d.lower() not in _SKIPPED_DIRS
                       and not _is_sensitive(here / d) and not is_excluded(here / d)]
            available_count = len(files) + len(dirs)
            names = list(islice(chain(files, dirs), _MAX_NONRECURSIVE_ENTRIES))
            if available_count > _MAX_NONRECURSIVE_ENTRIES:
                truncated = True
            for name in names:
                context.check_cancelled()
                candidate = here / name
                if candidate.is_symlink() or _is_sensitive(candidate) or is_excluded(candidate):
                    continue
                if fnmatch.fnmatch(name.lower(), pattern.lower()):
                    matches.append(_entry(candidate))
                    if len(matches) >= limit:
                        truncated = True
                        break
            if truncated:
                break
    else:
        with os.scandir(base) as scan:
            for index, item in enumerate(scan):
                context.check_cancelled()
                if monotonic() >= deadline or index >= _MAX_NONRECURSIVE_ENTRIES:
                    truncated = True
                    break
                candidate = Path(item.path)
                if not candidate.is_symlink() and not _is_sensitive(candidate) and not is_excluded(candidate) and fnmatch.fnmatch(candidate.name.lower(), pattern.lower()):
                    matches.append(_entry(candidate))
                    if len(matches) >= limit:
                        truncated = True
                        break
    return ToolResult(True, "files.search", {
        "path": str(base), "pattern": pattern, "matches": matches, "count": len(matches),
        "truncated": truncated, "visited_directories": visited_dirs if recursive else 0,
    }, "File search completed within traversal limits.", verified=True)


def _find_one(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Find exactly one matching file; never silently pick among duplicates."""
    search = _search_files(context, {
        "path": args["path"], "pattern": args["pattern"],
        "recursive": args.get("recursive", True), "limit": 2,
        "exclude_directories": args.get("exclude_directories", []),
    }, roots)
    if not search.ok:
        return ToolResult(False, "files.find_one", search.data, search.message, search.error_code, False)
    matches = search.data.get("matches", [])
    if len(matches) > 1:
        return ToolResult(False, "files.find_one", {
            "matches": matches[:2], "at_least": 2, "truncated": bool(search.data.get("truncated")),
        }, "More than one file matched. Narrow the filename or search folder; no file was opened or read.",
        "AMBIGUOUS_MATCH", False)
    if search.data.get("truncated"):
        return ToolResult(False, "files.find_one", {"matches": matches, "truncated": True},
                          "The bounded search was incomplete, so uniqueness could not be verified. Narrow the search path.",
                          "SEARCH_INCOMPLETE", False)
    if not matches:
        return ToolResult(False, "files.find_one", {"matches": [], "truncated": False},
                          "No file matched the requested name in the approved search directory.",
                          "RESOURCE_NOT_FOUND", False)
    match = matches[0]
    return ToolResult(True, "files.find_one", {"path": match["path"], "match": match},
                      "Exactly one matching file was found.", verified=True)


def _find_latest(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Return the uniquely newest matching regular file; never guess on ties/incomplete scans."""
    search = _search_files(context, {
        "path": args["path"], "pattern": args["pattern"],
        "recursive": args.get("recursive", True), "limit": args.get("limit", 1000),
        "exclude_directories": args.get("exclude_directories", []),
    }, roots)
    if not search.ok:
        return ToolResult(False, "files.find_latest", search.data, search.message, search.error_code, False)
    excluded = str(args.get("exclude_name", "")).strip().casefold()
    matches = [item for item in search.data.get("matches", [])
               if not item.get("is_directory") and Path(str(item.get("path", ""))).name.casefold() != excluded]
    if search.data.get("truncated"):
        return ToolResult(False, "files.find_latest", {"path": search.data.get("path"),
                          "pattern": search.data.get("pattern"), "candidates": matches[:10],
                          "truncated": True},
                          "The bounded search was incomplete, so EVA cannot safely claim which file is newest. Narrow the search folder or filename.",
                          "SEARCH_INCOMPLETE", False)
    if not matches:
        return ToolResult(False, "files.find_latest", {"path": search.data.get("path"),
                          "pattern": search.data.get("pattern"), "matches_count": 0},
                          "No matching regular files were found in the approved search directory.",
                          "RESOURCE_NOT_FOUND", False)
    matches.sort(key=lambda item: (int(item.get("modified_epoch") or 0), str(item.get("path", "")).casefold()), reverse=True)
    newest_time = int(matches[0].get("modified_epoch") or 0)
    newest = [item for item in matches if int(item.get("modified_epoch") or 0) == newest_time]
    if len(newest) != 1:
        return ToolResult(False, "files.find_latest", {"path": search.data.get("path"),
                          "pattern": search.data.get("pattern"), "matches_count": len(matches),
                          "tied_candidates": newest[:10]},
                          "Multiple matching files share the newest modification timestamp. EVA will not arbitrarily choose one; narrow the search or select a file.",
                          "AMBIGUOUS_LATEST", False)
    return ToolResult(True, "files.find_latest", {"path": newest[0]["path"], "match": newest[0],
                      "matches_count": len(matches), "pattern": search.data.get("pattern"),
                      "search_path": search.data.get("path")},
                      "The uniquely newest matching file was found by modification time.", verified=True)


def _birth_epoch(stat: Any) -> tuple[int | None, str]:
    """Return a creation-like timestamp and an honest basis label."""
    birth = getattr(stat, "st_birthtime", None)
    if birth is not None:
        return int(birth), "birth_time"
    if os.name == "nt":
        return int(stat.st_ctime), "creation_time"
    # POSIX st_ctime is inode metadata-change time, not creation time. The caller
    # exposes this label so a "created" query cannot silently claim otherwise.
    return int(stat.st_ctime), "metadata_change_time"


def _local_date_from_epoch(value: int | None) -> str:
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(value).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _find_by_date(context: ExecutionContext, args: dict[str, Any],
                  roots: tuple[Path, ...]) -> ToolResult:
    """Find exactly one regular file matching a local calendar date and subject."""
    base = _resolve_safe_path(args["path"], roots)
    if not base.exists() or not base.is_dir():
        raise AutomationError("RESOURCE_NOT_FOUND", "The requested search directory does not exist.")
    pattern = str(args.get("pattern") or "*").strip()
    if not pattern or len(pattern) > 128 or pattern in {".", ".."} or "/" in pattern or "\\" in pattern:
        raise AutomationError("INVALID_ARGUMENT", "pattern must be a filename or glob pattern, not a path.")
    field = str(args.get("field") or "any").strip().casefold()
    if field not in {"any", "modified", "created"}:
        raise AutomationError("INVALID_ARGUMENT", "field must be one of: any, modified, created.")
    try:
        requested_date = date_slug(args.get("date"))
    except ValueError as exc:
        raise AutomationError("INVALID_ARGUMENT", str(exc)) from exc
    terms = [
        str(term).strip().casefold()
        for term in args.get("name_terms", []) or []
        if str(term).strip()
    ][:10]
    search = _search_files(context, {
        "path": str(base), "pattern": pattern,
        "recursive": bool(args.get("recursive", True)),
        "limit": int(args.get("limit", 1000)),
        "exclude_directories": args.get("exclude_directories", []),
    }, roots)
    if not search.ok:
        return ToolResult(False, "files.find_by_date", search.data, search.message,
                          search.error_code, False)
    if search.data.get("truncated"):
        return ToolResult(False, "files.find_by_date", {
            "search_path": str(base), "pattern": pattern, "requested_date": requested_date,
            "field": field, "truncated": True, "matches": [],
        }, "The bounded search was incomplete, so EVA cannot safely claim which file matches the date. Narrow the folder, pattern, or subject.",
            "SEARCH_INCOMPLETE", False)

    matches: list[dict[str, Any]] = []
    for item in search.data.get("matches", []):
        if not isinstance(item, dict) or item.get("is_directory"):
            continue
        path = Path(str(item.get("path") or ""))
        name = path.name.casefold()
        if terms and not any(term in name for term in terms):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        modified_epoch = int(stat.st_mtime)
        created_epoch, creation_basis = _birth_epoch(stat)
        modified_date = _local_date_from_epoch(modified_epoch)
        created_date = _local_date_from_epoch(created_epoch)
        matched_field = ""
        if field == "modified" and modified_date == requested_date:
            matched_field = "modified"
        elif field == "created" and created_date == requested_date:
            matched_field = "created"
        elif field == "any":
            if modified_date == requested_date:
                matched_field = "modified"
            elif created_date == requested_date:
                matched_field = "created"
        if not matched_field:
            continue
        matches.append({
            "name": path.name, "path": str(path), "size_bytes": int(stat.st_size),
            "modified_epoch": modified_epoch, "created_epoch": created_epoch,
            "modified_date": modified_date, "created_date": created_date,
            "creation_basis": creation_basis, "matched_field": matched_field,
        })

    matches.sort(key=lambda item: (item["modified_epoch"], item["path"].casefold()), reverse=True)
    data = {
        "search_path": str(base), "pattern": pattern, "field": field,
        "requested_date": requested_date, "matches_count": len(matches),
        "truncated": False,
    }
    if not matches:
        data["matches"] = []
        return ToolResult(False, "files.find_by_date", data,
                          f"No regular file matched the requested {field} date {requested_date} and subject filter.",
                          "RESOURCE_NOT_FOUND", False)
    if len(matches) > 1:
        data["matches"] = matches[:10]
        data["at_least"] = len(matches)
        return ToolResult(False, "files.find_by_date", data,
                          "More than one file matched the requested date and subject. EVA will not choose arbitrarily; narrow the search or select a file.",
                          "AMBIGUOUS_MATCH", False)
    match = matches[0]
    data.update({
        "path": match["path"], "match": match, "matched_date": requested_date,
        "matched_field": match["matched_field"], "creation_basis": match["creation_basis"],
    })
    return ToolResult(True, "files.find_by_date", data,
                      f"Exactly one file matched the local {match['matched_field']} date {requested_date}.",
                      verified=True)


def _sha256_path(context: ExecutionContext, path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            context.check_cancelled()
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_with_date(context: ExecutionContext, args: dict[str, Any],
                    roots: tuple[Path, ...]) -> ToolResult:
    """Copy a file with a local ISO date in its name, never overwriting."""
    source = _resolve_safe_path(args["source"], roots)
    if source.is_symlink() or not source.is_file():
        raise AutomationError("RESOURCE_NOT_FOUND", "Source must be an existing regular file (not a symlink).")
    if source.stat().st_size > _MAX_COPY_BYTES:
        raise AutomationError("FILE_TOO_LARGE", f"Copies are limited to {_MAX_COPY_BYTES} bytes.")
    directory = _resolve_safe_path(str(args.get("directory") or source.parent), roots, purpose="copy")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        dated = date_slug(args.get("date"))
    except ValueError as exc:
        raise AutomationError("INVALID_ARGUMENT", str(exc)) from exc

    stem = re.sub(r"(?:[-_ ]\d{4}-\d{2}-\d{2})+$", "", source.stem).strip(" -_") or "copy"
    suffix = source.suffix
    source_hash: str | None = None
    for index in range(1, 1001):
        context.check_cancelled()
        name = (f"{stem}-{dated}{suffix}" if index == 1
                else f"{stem}-{dated}-{index}{suffix}")
        destination = directory / name
        try:
            copied = _copy_file(context, {"source": str(source), "destination": str(destination)}, roots)
            if source_hash is None:
                source_hash = _sha256_path(context, source)
            destination_hash = _sha256_path(context, destination)
            if source_hash != destination_hash:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
                raise AutomationError("VERIFICATION_FAILED",
                                      "The dated copy was created, but its SHA-256 digest did not match the source. The copy was removed.")
            data = dict(copied.data)
            data.update({
                "path": str(destination), "destination": str(destination),
                "date": dated, "original_filename": source.name, "dated_name": name,
                "sha256": destination_hash, "source_sha256": source_hash,
                "filename_contains_date": dated in name,
            })
            return ToolResult(True, "files.copy_with_date", data,
                              "Created a new copy with the requested date in its filename and an exact SHA-256 match.",
                              verified=True)
        except AutomationError as exc:
            if exc.code == "ALREADY_EXISTS":
                continue
            raise
    raise AutomationError("NAME_COLLISION_LIMIT", "Could not find a free dated filename after 1000 attempts.")


def _create_docx(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Create a minimal valid DOCX document with one paragraph per input line."""
    target = _resolve_safe_path(args["path"], roots, purpose="write")
    if target.suffix.casefold() != ".docx":
        raise AutomationError("INVALID_ARGUMENT", "files.create_docx requires a .docx destination path.")
    content = args["content"]
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_TEXT_BYTES:
        raise AutomationError("INPUT_TOO_LARGE", f"Document content is limited to {_MAX_TEXT_BYTES} bytes.")
    target.parent.mkdir(parents=True, exist_ok=True)
    paragraphs = []
    for line in content.split("\n"):
        if line:
            paragraphs.append("<w:p><w:r><w:t xml:space=\"preserve\">" + html.escape(line, quote=False) + "</w:t></w:r></w:p>")
        else:
            paragraphs.append("<w:p/>")
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + "".join(paragraphs) +
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
        '</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    context.check_cancelled()
    created = False
    try:
        # Exclusive creation prevents overwriting a user's existing document.
        with target.open("xb"):
            created = True
        with zipfile.ZipFile(target, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", root_rels)
            archive.writestr("word/document.xml", document_xml)
        with zipfile.ZipFile(target, mode="r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None or "word/document.xml" not in archive.namelist():
                raise OSError("DOCX package verification failed.")
            actual_xml = archive.read("word/document.xml").decode("utf-8")
            for line in content.split("\n"):
                if line and html.escape(line, quote=False) not in actual_xml:
                    raise OSError("DOCX content verification failed.")
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "The destination already exists; EVA did not overwrite it.") from exc
    except Exception as exc:
        if created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, AutomationError):
            raise
        raise AutomationError("DOCX_WRITE_FAILED", f"Could not create or verify the DOCX document: {type(exc).__name__}: {exc}") from exc
    return ToolResult(True, "files.create_docx", {"path": str(target), "size_bytes": target.stat().st_size,
                      "characters": len(content), "format": "docx"},
                      "A valid DOCX file was created without overwriting an existing document.", verified=True)



def _create_pptx(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Create a genuine PowerPoint package; plain text is never given a .pptx suffix."""
    target = _resolve_safe_path(args["path"], roots, purpose="write")
    if target.suffix.casefold() != ".pptx":
        raise AutomationError("INVALID_ARGUMENT", "files.create_pptx requires a .pptx destination path.")
    content = str(args.get("content", ""))
    if len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise AutomationError("INPUT_TOO_LARGE", f"Presentation content is limited to {_MAX_TEXT_BYTES} bytes.")
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
    except ImportError as exc:
        raise AutomationError("DEPENDENCY_MISSING", "PowerPoint creation requires python-pptx. Install the updated requirements.txt and retry.") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        context.check_cancelled()
        # Exclusive reservation means a pre-existing document is never overwritten.
        with target.open("xb"):
            created = True
        presentation = Presentation()
        content_lines = [line.rstrip() for line in content.splitlines()]
        nonempty = [line.strip() for line in content_lines if line.strip()]
        has_slide_delimiters = bool(re.search(r"(?m)^\s*---SLIDE---\s*$", content))
        expected_text_lines: list[str] = []
        if content.strip() and has_slide_delimiters:
            # Contract: first non-empty line is presentation metadata/title; the
            # remaining body is split into real slides by a standalone delimiter.
            first_index = next(index for index, line in enumerate(content_lines) if line.strip())
            presentation_title = content_lines[first_index].strip()
            presentation.core_properties.title = presentation_title[:255]
            body_content = "\n".join(content_lines[first_index + 1:])
            sections = re.split(r"(?m)^\s*---SLIDE---\s*$", body_content)
            for section in sections:
                slide_lines = [line.strip() for line in section.splitlines() if line.strip()]
                if not slide_lines:
                    continue
                title_line = re.sub(r"^slide\s+\d+\s*:\s*", "", slide_lines[0], flags=re.I).strip()
                slide = presentation.slides.add_slide(presentation.slide_layouts[1])
                slide.shapes.title.text = (title_line or target.stem)[:240]
                body = slide.placeholders[1].text_frame
                body.clear()
                for line in slide_lines[1:]:
                    para = body.paragraphs[0] if not body.paragraphs[0].text else body.add_paragraph()
                    para.text = line
                    para.font.size = Pt(20)
                if len(slide_lines) == 1:
                    body.text = title_line or target.stem
                expected_text_lines.extend([
                    re.sub(r"^slide\s+\d+\s*:\s*", "", line, flags=re.I).strip()
                    for line in slide_lines
                ])
        elif content.strip():
            # Backward-compatible single-slide format for simple direct callers.
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = nonempty[0][:240] if nonempty else target.stem
            body = slide.placeholders[1].text_frame
            body.clear()
            for line in (nonempty[1:] if len(nonempty) > 1 else []):
                para = body.paragraphs[0] if not body.paragraphs[0].text else body.add_paragraph()
                para.text = line
                para.font.size = Pt(20)
            if len(nonempty) == 1:
                body.text = nonempty[0]
            expected_text_lines = nonempty
            presentation.core_properties.title = target.stem[:255]
        else:
            # A blank, one-slide presentation is a valid answer to "create an
            # empty .pptx file"; it is not a ZIP shell or renamed text file.
            presentation.slides.add_slide(presentation.slide_layouts[6])
            presentation.core_properties.title = target.stem[:255]
        context.check_cancelled()
        presentation.save(str(target))
        reopened = Presentation(str(target))
        if len(reopened.slides) < 1 or target.stat().st_size < 1000:
            raise OSError("PowerPoint package did not pass reopen/integrity validation.")
        rendered = "\n".join(shape.text for slide in reopened.slides for shape in slide.shapes if getattr(shape, "has_text_frame", False))
        if has_slide_delimiters and reopened.core_properties.title != presentation_title:
            raise OSError("PowerPoint presentation title metadata did not survive reopen validation.")
        for line in expected_text_lines:
            if line and line not in rendered:
                raise OSError(f"PowerPoint text verification failed for line: {line[:80]!r}")
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "The destination already exists; EVA did not overwrite it.") from exc
    except Exception as exc:
        if created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, AutomationError):
            raise
        raise AutomationError("PPTX_WRITE_FAILED", f"Could not create or verify the PowerPoint file: {type(exc).__name__}: {exc}") from exc
    return ToolResult(True, "files.create_pptx", {"path": str(target), "size_bytes": target.stat().st_size,
                      "characters": len(content), "format": "pptx", "slides": len(reopened.slides)},
                      "A valid PowerPoint presentation was created and reopened without overwriting an existing file.", verified=True)


def _create_xlsx(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Create a real Excel workbook; optional text is placed into cells as data, not formulas."""
    target = _resolve_safe_path(args["path"], roots, purpose="write")
    if target.suffix.casefold() != ".xlsx":
        raise AutomationError("INVALID_ARGUMENT", "files.create_xlsx requires an .xlsx destination path.")
    content = str(args.get("content", ""))
    if len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise AutomationError("INPUT_TOO_LARGE", f"Workbook content is limited to {_MAX_TEXT_BYTES} bytes.")
    try:
        from openpyxl import Workbook, load_workbook
    except ImportError as exc:
        raise AutomationError("DEPENDENCY_MISSING", "Excel workbook creation requires openpyxl. Install the updated requirements.txt and retry.") from exc
    import csv
    from io import StringIO
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        context.check_cancelled()
        with target.open("xb"):
            created = True
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "EVA"
        expected_rows: list[list[object]] = []
        if content:
            delimiter = "\t" if "\t" in content else ("," if "," in content else None)
            if delimiter:
                expected_rows = [next(csv.reader([line], delimiter=delimiter)) for line in content.splitlines() if line.strip()]
            else:
                expected_rows = [[line] for line in content.splitlines() if line.strip()]
            if not expected_rows:
                expected_rows = [[content]]
            for row in expected_rows:
                safe_row = []
                for value in row:
                    value = str(value)
                    # Prevent user/model-provided strings from becoming active
                    # spreadsheet formulas when somebody opens the workbook.
                    if value.startswith("=") or re_formula_prefix(value):
                        value = "'" + value
                    safe_row.append(value)
                sheet.append(safe_row)
        workbook.properties.title = target.stem[:255]
        context.check_cancelled()
        workbook.save(str(target))
        verified_book = load_workbook(str(target), read_only=False, data_only=False)
        verified_sheet = verified_book["EVA"]
        if verified_book.sheetnames != ["EVA"]:
            raise OSError("Workbook structure verification failed.")
        if expected_rows:
            actual = [[verified_sheet.cell(row=r, column=c).value for c in range(1, len(row) + 1)]
                      for r, row in enumerate(expected_rows, 1)]
            expected_safe = [[("'" + str(value)) if (str(value).startswith("=") or re_formula_prefix(str(value))) else str(value)
                              for value in row] for row in expected_rows]
            if actual != expected_safe:
                raise OSError("Workbook cell content verification failed.")
        verified_book.close()
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "The destination already exists; EVA did not overwrite it.") from exc
    except Exception as exc:
        if created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, AutomationError):
            raise
        raise AutomationError("XLSX_WRITE_FAILED", f"Could not create or verify the Excel workbook: {type(exc).__name__}: {exc}") from exc
    return ToolResult(True, "files.create_xlsx", {"path": str(target), "size_bytes": target.stat().st_size,
                      "characters": len(content), "format": "xlsx", "rows": len(expected_rows)},
                      "A valid Excel workbook was created and reopened without overwriting an existing file.", verified=True)


def re_formula_prefix(value: str) -> bool:
    """Return true for spreadsheet formula-injection prefixes other than plain text dashes."""
    stripped = value.lstrip()
    return bool(stripped.startswith(("+", "@")) or (stripped.startswith("-") and len(stripped) > 1 and stripped[1:2] not in {" ", "\t"}))


def _create_pdf(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Create a valid PDF containing the supplied text, or one blank page if content is omitted."""
    target = _resolve_safe_path(args["path"], roots, purpose="write")
    if target.suffix.casefold() != ".pdf":
        raise AutomationError("INVALID_ARGUMENT", "files.create_pdf requires a .pdf destination path.")
    content = str(args.get("content", ""))
    if len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise AutomationError("INPUT_TOO_LARGE", f"PDF content is limited to {_MAX_TEXT_BYTES} bytes.")
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        from xml.sax.saxutils import escape
    except ImportError as exc:
        raise AutomationError("DEPENDENCY_MISSING", "PDF creation requires ReportLab. Install the updated requirements.txt and retry.") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        context.check_cancelled()
        with target.open("xb"):
            created = True
        document = SimpleDocTemplate(str(target), pagesize=letter, rightMargin=0.7 * inch,
                                     leftMargin=0.7 * inch, topMargin=0.65 * inch, bottomMargin=0.65 * inch)
        styles = getSampleStyleSheet()
        story = []
        if content.strip():
            for block in re.split(r"\n\s*\n", content):
                if not block.strip():
                    continue
                markup = "<br/>".join(escape(line) for line in block.splitlines())
                story.extend((Paragraph(markup, styles["BodyText"]), Spacer(1, 8)))
        else:
            story.append(Spacer(1, 1))
        context.check_cancelled()
        document.build(story)
        raw = target.read_bytes()
        if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-1024:] or len(raw) < 100:
            raise OSError("PDF header/trailer integrity verification failed.")
        if content:
            try:
                from pypdf import PdfReader
                extracted = "\n".join(page.extract_text() or "" for page in PdfReader(str(target)).pages)
                # PDF text extraction often normalizes whitespace; verify each
                # alphanumeric source line as a substring after normalization.
                norm_extracted = " ".join(extracted.split())
                for line in content.splitlines():
                    line = line.strip()
                    if line and len(line) > 4 and not all(ch in "=-*•" for ch in line):
                        probe = " ".join(line.split())
                        if probe not in norm_extracted and not probe[:35] in norm_extracted:
                            raise OSError(f"PDF text verification failed for line: {line[:80]!r}")
            except ImportError:
                # Header/trailer integrity is still checked; install pypdf for
                # stronger text extraction verification.
                pass
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "The destination already exists; EVA did not overwrite it.") from exc
    except Exception as exc:
        if created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, AutomationError):
            raise
        raise AutomationError("PDF_WRITE_FAILED", f"Could not create or verify the PDF: {type(exc).__name__}: {exc}") from exc
    return ToolResult(True, "files.create_pdf", {"path": str(target), "size_bytes": target.stat().st_size,
                      "characters": len(content), "format": "pdf"},
                      "A valid PDF was created and its package header/trailer was verified without overwriting an existing file.", verified=True)



def _rtf_escape_text(value: str) -> str:
    pieces: list[str] = []
    for char in value:
        code = ord(char)
        if char == "\\":
            pieces.append(r"\textbackslash ")
        elif char in "{}":
            pieces.append("\\" + char)
        elif char == "\n":
            pieces.append(r"\par " + "\n")
        elif char == "\r":
            continue
        elif 32 <= code < 127:
            pieces.append(char)
        elif code <= 0xFFFF:
            signed = code if code < 0x8000 else code - 0x10000
            pieces.append(f"\\u{signed}?")
        else:
            adjusted = code - 0x10000
            for surrogate in (0xD800 + (adjusted >> 10), 0xDC00 + (adjusted & 0x3FF)):
                signed = surrogate if surrogate < 0x8000 else surrogate - 0x10000
                pieces.append(f"\\u{signed}?")
    return "".join(pieces)


def _create_rtf(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Create a valid Rich Text Format document using RTF control syntax."""
    target = _resolve_safe_path(args["path"], roots, purpose="write")
    if target.suffix.casefold() != ".rtf":
        raise AutomationError("INVALID_ARGUMENT", "files.create_rtf requires an .rtf destination path.")
    content = str(args.get("content", ""))
    if len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise AutomationError("INPUT_TOO_LARGE", f"RTF content is limited to {_MAX_TEXT_BYTES} bytes.")
    body = _rtf_escape_text(content)
    document = r"{\rtf1\ansi\deff0{\fonttbl{\f0 Calibri;}}\viewkind4\uc1\f0\fs22 " + body + "}"
    target.parent.mkdir(parents=True, exist_ok=True)
    context.check_cancelled()
    try:
        with target.open("xb") as stream:
            stream.write(document.encode("ascii", errors="strict"))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "The destination already exists; EVA did not overwrite it.") from exc
    except OSError as exc:
        raise AutomationError("RTF_WRITE_FAILED", f"Could not create the RTF document: {exc}") from exc
    actual = target.read_bytes().decode("ascii", errors="strict")
    if not actual.startswith(r"{\rtf1") or not actual.endswith("}") or body not in actual:
        target.unlink(missing_ok=True)
        raise AutomationError("RTF_VERIFY_FAILED", "The RTF header/body verification failed.")
    return ToolResult(True, "files.create_rtf", {"path": str(target), "size_bytes": target.stat().st_size,
                      "characters": len(content), "format": "rtf"},
                      "A valid RTF document was created and verified without overwriting an existing file.", verified=True)


def _create_odt(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Create a minimal OpenDocument Text package containing text paragraphs."""
    target = _resolve_safe_path(args["path"], roots, purpose="write")
    if target.suffix.casefold() != ".odt":
        raise AutomationError("INVALID_ARGUMENT", "files.create_odt requires an .odt destination path.")
    content = str(args.get("content", ""))
    if len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise AutomationError("INPUT_TOO_LARGE", f"ODT content is limited to {_MAX_TEXT_BYTES} bytes.")
    office_ns = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    text_ns = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    style_ns = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
    paragraphs = [f'<text:p xml:space="preserve">{html.escape(line, quote=False)}</text:p>' for line in content.split("\n")]
    content_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-content xmlns:office="{office_ns}" xmlns:text="{text_ns}" xmlns:style="{style_ns}" office:version="1.2">'
        '<office:automatic-styles/><office:body><office:text>' + "".join(paragraphs) + '</office:text></office:body></office:document-content>')
    styles_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-styles xmlns:office="{office_ns}" xmlns:style="{style_ns}" office:version="1.2">'
        '<office:styles/><office:automatic-styles/><office:master-styles/></office:document-styles>')
    meta_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-meta xmlns:office="{office_ns}" office:version="1.2"><office:meta/></office:document-meta>')
    settings_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-settings xmlns:office="{office_ns}" office:version="1.2"><office:settings/></office:document-settings>')
    manifest_ns = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
    manifest_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
        f'<manifest:manifest xmlns:manifest="{manifest_ns}" manifest:version="1.2">'
        '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="settings.xml" manifest:media-type="text/xml"/>'
        '</manifest:manifest>')
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        context.check_cancelled()
        with target.open("xb"):
            created = True
        with zipfile.ZipFile(target, "w") as archive:
            mime_info = zipfile.ZipInfo("mimetype")
            mime_info.compress_type = zipfile.ZIP_STORED
            archive.writestr(mime_info, "application/vnd.oasis.opendocument.text")
            archive.writestr("META-INF/manifest.xml", manifest_xml)
            archive.writestr("content.xml", content_xml)
            archive.writestr("styles.xml", styles_xml)
            archive.writestr("meta.xml", meta_xml)
            archive.writestr("settings.xml", settings_xml)
        context.check_cancelled()
        with zipfile.ZipFile(target) as archive:
            if archive.testzip() is not None or archive.namelist()[0] != "mimetype":
                raise OSError("ODT package integrity failed.")
            stored_content = archive.read("content.xml").decode("utf-8")
            for line in content.split("\n"):
                if html.escape(line, quote=False) not in stored_content:
                    raise OSError("ODT content verification failed.")
        import xml.etree.ElementTree as ET
        ET.fromstring(content_xml)
        ET.fromstring(manifest_xml)
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "The destination already exists; EVA did not overwrite it.") from exc
    except Exception as exc:
        if created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, AutomationError):
            raise
        raise AutomationError("ODT_WRITE_FAILED", f"Could not create or verify the ODT document: {type(exc).__name__}: {exc}") from exc
    return ToolResult(True, "files.create_odt", {"path": str(target), "size_bytes": target.stat().st_size,
                      "characters": len(content), "format": "odt"},
                      "An OpenDocument Text package was created and verified without overwriting an existing file.", verified=True)


def _find_duplicates(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Find duplicate regular files using size prefilter + SHA-256; never mutate files."""
    base = _resolve_safe_path(args["path"], roots)
    if not base.exists() or not base.is_dir():
        raise AutomationError("RESOURCE_NOT_FOUND", "The duplicate-scan path must be an existing directory within an approved root.")
    recursive = args.get("recursive", True)
    max_files = args.get("max_files", 5000)
    max_total_bytes = args.get("max_total_bytes", 1_073_741_824)
    deadline = monotonic() + min(float(args.get("timeout_seconds", 20)), 60.0)
    by_size: dict[int, list[Path]] = {}
    scanned_files = 0
    skipped_files = 0
    scanned_bytes = 0
    truncated = False
    if recursive:
        walker = os.walk(base, followlinks=False)
    else:
        try:
            children = list(base.iterdir())
        except OSError as exc:
            raise AutomationError("DIRECTORY_READ_FAILED", f"Could not read directory: {exc}") from exc
        walker = [(str(base), [], [p.name for p in children if p.is_file()])]
    for current, dirs, names in walker:
        context.check_cancelled()
        if monotonic() > deadline or scanned_files >= max_files:
            truncated = True
            break
        here = Path(current)
        dirs[:] = [d for d in dirs if d.lower() not in _SKIPPED_DIRS and not _is_sensitive(here / d)
                   and not (here / d).is_symlink()]
        for name in names:
            context.check_cancelled()
            if monotonic() > deadline or scanned_files >= max_files:
                truncated = True
                break
            candidate = here / name
            if candidate.is_symlink() or _is_sensitive(candidate):
                skipped_files += 1
                continue
            try:
                if not candidate.is_file():
                    continue
                size = candidate.stat().st_size
            except OSError:
                skipped_files += 1
                continue
            # Empty files can be duplicates too; their hash is the SHA-256 of empty bytes.
            if scanned_bytes + size > max_total_bytes:
                truncated = True
                skipped_files += 1
                continue
            by_size.setdefault(size, []).append(candidate)
            scanned_files += 1
            scanned_bytes += size
        if truncated:
            break
    duplicate_groups = []
    hashed_files = 0
    bytes_hashed_read = 0
    for size, paths in by_size.items():
        context.check_cancelled()
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for candidate in paths:
            if monotonic() > deadline:
                truncated = True
                break
            digest = hashlib.sha256()
            try:
                with candidate.open("rb") as handle:
                    while True:
                        context.check_cancelled()
                        if monotonic() > deadline:
                            truncated = True
                            break
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        bytes_hashed_read += len(chunk)
                if truncated:
                    break
                by_hash.setdefault(digest.hexdigest(), []).append(candidate)
                hashed_files += 1
            except OSError:
                skipped_files += 1
        for sha, copies in by_hash.items():
            if len(copies) > 1:
                duplicate_groups.append({"sha256": sha, "size_bytes": size,
                                         "count": len(copies), "files": [str(p) for p in copies]})
        if truncated:
            break
    result = ToolResult(True, "files.find_duplicates", {
        "path": str(base), "recursive": recursive, "duplicate_groups": duplicate_groups,
        "duplicate_file_count": sum(g["count"] for g in duplicate_groups),
        "duplicate_group_count": len(duplicate_groups), "files_scanned": scanned_files,
        "files_hashed": hashed_files,
        "total_file_bytes_considered": scanned_bytes,
        "bytes_hashed_read": bytes_hashed_read,
        "skipped_files": skipped_files, "truncated": truncated,
        "deletions_performed": 0,
    }, "SHA-256 duplicate scan completed. No files were deleted or changed.", verified=True)
    return result


def _create_directory(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    target = _resolve_safe_path(args["path"], roots, purpose="write")
    existed = target.is_dir()
    target.mkdir(parents=True, exist_ok=True)
    return ToolResult(True, "files.create_directory", {"path": str(target), "created": not existed}, "Directory is available.", verified=target.is_dir())


def _create_text_file(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    target = _resolve_safe_path(args["path"], roots, purpose="write")
    content = args.get("content", "")
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_TEXT_BYTES:
        raise AutomationError("INPUT_TOO_LARGE", f"Text file content is limited to {_MAX_TEXT_BYTES} bytes.")
    target.parent.mkdir(parents=True, exist_ok=True)
    created_target = False
    try:
        with target.open("x", encoding="utf-8", newline="") as stream:
            created_target = True
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "The destination already exists; EVA did not overwrite it.") from exc
    except OSError as exc:
        if created_target:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise AutomationError("FILE_WRITE_FAILED", f"Could not create file: {exc}") from exc
    verified = target.is_file() and target.read_text(encoding="utf-8") == content
    return ToolResult(True, "files.create_text_file", {"path": str(target), "size_bytes": len(encoded)}, "File created without overwriting an existing file.", verified=verified)


def _read_text(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Read bounded UTF-8 text from a regular file inside approved roots."""
    path = _resolve_safe_path(args["path"], roots)
    if path.is_symlink() or not path.is_file():
        raise AutomationError("RESOURCE_NOT_FOUND", "The requested path must be an existing regular file within an approved root.")
    max_bytes = min(max(int(args.get("max_bytes", 262144)), 1), _MAX_TEXT_BYTES)
    context.check_cancelled()
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise AutomationError("FILE_READ_FAILED", f"Could not read file: {exc}") from exc
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    # Text display should still work for a file with an unusual byte at the
    # truncation boundary; callers receive a warning flag rather than a crash.
    # Normalize CRLF/LF/CR so planner prompts, test assertions, and displayed
    # content behave consistently across Windows, macOS, and Linux.
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    data = {"path": str(path), "text": text, "size_bytes": path.stat().st_size,
            "bytes_read": len(raw), "truncated": truncated}
    if not truncated:
        data["sha256"] = hashlib.sha256(raw).hexdigest()
    else:
        data["sha256_prefix"] = hashlib.sha256(raw).hexdigest()
    return ToolResult(True, "files.read_text", data,
                      "Read bounded text from the requested file.", verified=True)



def _resolve_by_name(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Resolve a user-supplied filename without asking them to identify its extension."""
    base = _resolve_safe_path(args["path"], roots)
    filename = Path(args["filename"].strip()).name
    if not filename or filename in {".", ".."} or filename != args["filename"].strip() or any(ch in filename for ch in ("/", "\\")):
        raise AutomationError("INVALID_ARGUMENT", "filename must be a filename or basename, not a path.")
    if not base.exists() or not base.is_dir():
        raise AutomationError("RESOURCE_NOT_FOUND", "The requested search directory does not exist.")

    def find_pattern(pattern: str) -> tuple[list[dict[str, Any]], bool]:
        found = _search_files(context, {"path": str(base), "pattern": pattern,
                            "recursive": args.get("recursive", True), "limit": 100}, roots)
        if not found.ok:
            raise AutomationError(found.error_code or "FILE_SEARCH_FAILED", found.message)
        return list(found.data.get("matches", [])), bool(found.data.get("truncated"))

    # Exact user spelling always wins, including nonstandard suffixes such as .docs.
    exact, exact_incomplete = find_pattern(filename)
    if exact_incomplete:
        return ToolResult(False, "files.resolve_by_name", {"filename": filename, "candidates": exact[:10], "truncated": True},
                          "The exact-name search was incomplete, so EVA cannot safely confirm a unique match.",
                          "SEARCH_INCOMPLETE", False)
    if len(exact) == 1:
        return ToolResult(True, "files.resolve_by_name", {"path": exact[0]["path"], "match": exact[0],
                          "matched_by": "exact_filename", "candidates": exact},
                          "Found the exact requested filename.", verified=True)
    if len(exact) > 1:
        return ToolResult(False, "files.resolve_by_name", {"candidates": exact[:10], "match_count": len(exact)},
                          "Multiple files have the exact requested name. No file was selected.", "AMBIGUOUS_MATCH", False)

    supplied = Path(filename)
    if supplied.suffix:
        extension_groups = {
            ".docs": {".docs", ".docx", ".doc", ".txt", ".pdf", ".rtf", ".md", ".odt"},
            ".docx": {".docx"},
            ".doc": {".doc"},
            ".rtf": {".rtf"},
            ".odt": {".odt"},
            ".xlsx": {".xlsx"},
            ".xls": {".xls"},
            ".pptx": {".pptx"},
            ".ppt": {".ppt"},
        }
        if supplied.suffix.lower() in extension_groups:
            allowed_extensions = extension_groups[supplied.suffix.lower()]
        else:
            # A specifically named, unsupported extension should not silently resolve
            # to a different file type; exact spelling was already checked above.
            return ToolResult(False, "files.resolve_by_name", {"filename": filename, "search_path": str(base)},
                              "No exact filename matched; no safe extension alternative is defined for this file type.",
                              "RESOURCE_NOT_FOUND", False)
        pattern = supplied.stem + ".*"
    else:
        allowed_extensions = None
        pattern = filename + ".*"

    fallback, fallback_incomplete = find_pattern(pattern)
    if fallback_incomplete:
        return ToolResult(False, "files.resolve_by_name", {"filename": filename, "candidates": fallback[:10], "truncated": True},
                          "The extension-fallback search was incomplete, so EVA cannot safely confirm a unique match.",
                          "SEARCH_INCOMPLETE", False)
    candidates = [item for item in fallback
                  if allowed_extensions is None or Path(str(item.get("name", ""))).suffix.lower() in allowed_extensions]
    # Deduplicate paths defensively, then select only a unique match.
    candidates_by_path = {str(item["path"]).casefold(): item for item in candidates}
    candidates = list(candidates_by_path.values())
    if len(candidates) == 1:
        candidate = candidates[0]
        return ToolResult(True, "files.resolve_by_name", {"path": candidate["path"], "match": candidate,
                          "matched_by": "basename_or_extension_fallback", "candidates": candidates},
                          "Found a file matching the supplied name using a filename/extension fallback.", verified=True)
    if len(candidates) > 1:
        return ToolResult(False, "files.resolve_by_name", {"candidates": candidates[:10], "match_count": len(candidates)},
                          "Several files match the supplied name. No file was selected.", "AMBIGUOUS_MATCH", False)
    return ToolResult(False, "files.resolve_by_name", {"filename": filename, "search_path": str(base)},
                      "No file matched the supplied name or reasonable extension variants.", "RESOURCE_NOT_FOUND", False)


def _read_document(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Read plain-text files and extract text from modern DOCX documents."""
    path = _resolve_safe_path(args["path"], roots)
    if path.is_symlink() or not path.is_file():
        raise AutomationError("RESOURCE_NOT_FOUND", "The requested document must be an existing regular file within an approved root.")
    max_bytes = min(max(int(args.get("max_bytes", 262144)), 1), _MAX_TEXT_BYTES)
    context.check_cancelled()
    suffix = path.suffix.lower()
    if suffix in {"", ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log", ".xml", ".html", ".py", ".java", ".c", ".h", ".cpp", ".js", ".jsx", ".ts", ".tsx", ".css", ".cs", ".php", ".rb", ".go", ".rs", ".swift", ".kt", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".sql", ".ini", ".toml", ".cfg", ".conf", ".properties", ".env", ".docs"}:
        result = _read_text(context, {"path": str(path), "max_bytes": max_bytes}, roots)
        result.tool = "files.read_document"
        result.data["format"] = "plain_text"
        return result
    if suffix == ".docx":
        import xml.etree.ElementTree as ET
        try:
            with zipfile.ZipFile(path, "r") as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 20 * _MAX_TEXT_BYTES:
                    raise AutomationError("INPUT_TOO_LARGE", "The DOCX document XML exceeds the extraction limit.")
                xml_bytes = archive.read("word/document.xml")
            root = ET.fromstring(xml_bytes)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paragraphs = []
            for paragraph in root.findall(".//w:p", ns):
                pieces = []
                for node in paragraph.iter():
                    if node.tag.endswith("}t") and node.text:
                        pieces.append(node.text)
                    elif node.tag.endswith("}tab"):
                        pieces.append("\t")
                line = "".join(pieces).strip()
                if line:
                    paragraphs.append(line)
            text_value = "\n".join(paragraphs)
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("DOCUMENT_READ_FAILED", f"Could not extract DOCX text: {type(exc).__name__}: {exc}") from exc
        encoded = text_value.encode("utf-8")
        truncated = len(encoded) > max_bytes
        if truncated:
            text_value = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return ToolResult(True, "files.read_document", {"path": str(path), "text": text_value,
                          "characters": len(text_value), "format": "docx", "truncated": truncated,
                          "size_bytes": path.stat().st_size},
                          "Extracted text from the DOCX document.", verified=True)
    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(str(path), read_only=True, data_only=True)
            try:
                lines = []
                byte_count = 0
                truncated = False
                for sheet in workbook.worksheets:
                    lines.append(f"[{sheet.title}]")
                    for row in sheet.iter_rows(values_only=True):
                        context.check_cancelled()
                        values = ["" if cell is None else str(cell) for cell in row]
                        while values and values[-1] == "":
                            values.pop()
                        if values:
                            line = "\t".join(values)
                            byte_count += len(line.encode("utf-8")) + 1
                            if byte_count > max_bytes:
                                truncated = True
                                break
                            lines.append(line)
                    if truncated:
                        break
                text_value = "\n".join(lines)
            finally:
                workbook.close()
        except Exception as exc:
            raise AutomationError("DOCUMENT_READ_FAILED", f"Could not extract XLSX text: {type(exc).__name__}: {exc}") from exc
        return ToolResult(True, "files.read_document", {"path": str(path), "text": text_value[:max_bytes],
                          "characters": len(text_value[:max_bytes]), "format": "xlsx", "truncated": truncated,
                          "size_bytes": path.stat().st_size}, "Extracted worksheet values from the Excel workbook.", verified=True)
    if suffix == ".pptx":
        try:
            from pptx import Presentation
            presentation = Presentation(str(path))
            slide_text = []
            for index, slide in enumerate(presentation.slides, 1):
                context.check_cancelled()
                text_items = [shape.text.strip() for shape in slide.shapes if getattr(shape, "has_text_frame", False) and shape.text.strip()]
                slide_text.append(f"[Slide {index}]\n" + "\n".join(text_items))
            text_value = "\n\n".join(slide_text)
        except Exception as exc:
            raise AutomationError("DOCUMENT_READ_FAILED", f"Could not extract PPTX text: {type(exc).__name__}: {exc}") from exc
        encoded = text_value.encode("utf-8")
        truncated = len(encoded) > max_bytes
        if truncated:
            text_value = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return ToolResult(True, "files.read_document", {"path": str(path), "text": text_value,
                          "characters": len(text_value), "format": "pptx", "truncated": truncated,
                          "size_bytes": path.stat().st_size}, "Extracted slide text from the PowerPoint presentation.", verified=True)
    if suffix == ".odt":
        try:
            import xml.etree.ElementTree as ET
            with zipfile.ZipFile(path, "r") as archive:
                info = archive.getinfo("content.xml")
                if info.file_size > 20 * _MAX_TEXT_BYTES:
                    raise AutomationError("INPUT_TOO_LARGE", "The ODT content XML exceeds the extraction limit.")
                xml_bytes = archive.read("content.xml")
            root = ET.fromstring(xml_bytes)
            ns = {"text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
            paragraphs = ["".join(p.itertext()) for p in root.findall(".//text:p", ns)]
            text_value = "\n".join(paragraphs)
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("DOCUMENT_READ_FAILED", f"Could not extract ODT text: {type(exc).__name__}: {exc}") from exc
        encoded = text_value.encode("utf-8")
        truncated = len(encoded) > max_bytes
        if truncated:
            text_value = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return ToolResult(True, "files.read_document", {"path": str(path), "text": text_value,
                          "characters": len(text_value), "format": "odt", "truncated": truncated,
                          "size_bytes": path.stat().st_size}, "Extracted text from the OpenDocument Text file.", verified=True)
    if suffix == ".rtf":
        try:
            # Do not read an unbounded RTF file into memory. Escaped text can be
            # longer than plain text, so allow a bounded multiple of the output cap.
            with path.open("rb") as stream:
                raw_bytes = stream.read(max_bytes * 4 + 1)
            truncated = len(raw_bytes) > max_bytes * 4
            raw_text = raw_bytes[:max_bytes * 4].decode("ascii", errors="replace")
            # Discard metadata destinations before stripping RTF control words;
            # otherwise font names like "Calibri" appear in extracted text.
            raw_text = re.sub(r"\\(?:fonttbl|colortbl|stylesheet|info)\b.*?\}\s*", "", raw_text, flags=re.S)
            raw_text = re.sub(r"\{\\\*\\[^{}]*\}", "", raw_text)
            raw_text = re.sub(r"\\'[0-9a-fA-F]{2}", lambda m: bytes([int(m.group(0)[2:], 16)]).decode("cp1252", errors="replace"), raw_text)
            raw_text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), raw_text)
            raw_text = re.sub(r"\\(?:par|line)\b ?", "\n", raw_text)
            raw_text = re.sub(r"\\tab\b ?", "\t", raw_text)
            raw_text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", raw_text)
            text_value = raw_text.replace(r"\{", "{").replace(r"\}", "}").replace(r"\\", "\\")
            text_value = text_value.replace("{", "").replace("}", "").strip()
        except Exception as exc:
            raise AutomationError("DOCUMENT_READ_FAILED", f"Could not extract RTF text: {type(exc).__name__}: {exc}") from exc
        encoded = text_value.encode("utf-8")
        truncated = truncated or len(encoded) > max_bytes
        if len(encoded) > max_bytes:
            text_value = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return ToolResult(True, "files.read_document", {"path": str(path), "text": text_value,
                          "characters": len(text_value), "format": "rtf", "truncated": truncated,
                          "size_bytes": path.stat().st_size}, "Extracted text from the Rich Text Format file.", verified=True)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise AutomationError("DEPENDENCY_MISSING", "PDF text extraction requires pypdf; install it to read this PDF.") from exc
        try:
            reader = PdfReader(str(path))
            chunks = []
            size = 0
            for page in reader.pages:
                context.check_cancelled()
                chunk = page.extract_text() or ""
                if size + len(chunk.encode("utf-8")) > max_bytes:
                    remaining = max_bytes - size
                    chunks.append(chunk.encode("utf-8")[:remaining].decode("utf-8", errors="ignore"))
                    break
                chunks.append(chunk)
                size += len(chunk.encode("utf-8"))
            text_value = "\n\n".join(chunks)
            return ToolResult(True, "files.read_document", {"path": str(path), "text": text_value,
                              "characters": len(text_value), "format": "pdf", "truncated": size >= max_bytes,
                              "size_bytes": path.stat().st_size}, "Extracted text from the PDF document.", verified=True)
        except Exception as exc:
            raise AutomationError("DOCUMENT_READ_FAILED", f"Could not extract PDF text: {type(exc).__name__}: {exc}") from exc
    raise AutomationError("UNSUPPORTED_DOCUMENT_FORMAT", f"Reading '{suffix or 'extensionless'}' documents is not supported by the current document reader.")

def _create_unique_text_file(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Create a uniquely named UTF-8 text file without overwriting any existing file."""
    directory = _resolve_safe_path(args["directory"], roots, purpose="write")
    filename = Path(args["filename"]).name.strip()
    if not filename or filename in {".", ".."} or filename != args["filename"].strip() or any(sep in filename for sep in ("/", "\\")):
        raise AutomationError("INVALID_ARGUMENT", "filename must be a simple filename without a directory path.")
    if len(filename) > 160 or any(ord(ch) < 32 for ch in filename):
        raise AutomationError("INVALID_ARGUMENT", "filename is too long or contains control characters.")
    content = args["content"]
    if len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise AutomationError("INPUT_TOO_LARGE", f"Text file content is limited to {_MAX_TEXT_BYTES} bytes.")
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(1, 1001):
        context.check_cancelled()
        target = candidate if index == 1 else directory / f"{stem}-{index}{suffix}"
        try:
            result = _create_text_file(context, {"path": str(target), "content": content}, roots)
            data = dict(result.data)
            data["sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            data["characters"] = len(content)
            result.data = data
            result.message = "Created a unique text file without overwriting an existing file."
            result.verified = target.is_file() and target.read_text(encoding="utf-8") == content
            return result
        except AutomationError as exc:
            if exc.code == "ALREADY_EXISTS":
                continue
            raise
    raise AutomationError("NAME_COLLISION_LIMIT", "Could not find a free filename after 1000 attempts.")


def _copy_file(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    source = _resolve_safe_path(args["source"], roots)
    destination = _resolve_safe_path(args["destination"], roots, purpose="copy")
    if source.is_symlink() or not source.is_file():
        raise AutomationError("RESOURCE_NOT_FOUND", "Source must be an existing regular file (not a symlink).")
    if source.stat().st_size > _MAX_COPY_BYTES:
        raise AutomationError("FILE_TOO_LARGE", f"Copies are limited to {_MAX_COPY_BYTES} bytes.")
    if source == destination:
        raise AutomationError("INVALID_ARGUMENT", "Source and destination must be different paths.")
    if destination.exists():
        raise AutomationError("ALREADY_EXISTS", "Destination already exists; EVA did not overwrite it.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied_bytes = 0
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            while True:
                context.check_cancelled()
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                copied_bytes += len(chunk)
                if copied_bytes > _MAX_COPY_BYTES:
                    raise AutomationError("FILE_TOO_LARGE", f"Copies are limited to {_MAX_COPY_BYTES} bytes.")
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "Destination already exists; EVA did not overwrite it.") from exc
    except AutomationError:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise AutomationError("FILE_COPY_FAILED", f"Could not copy file: {exc}") from exc
    try:
        verified = destination.is_file() and destination.stat().st_size == copied_bytes == source.stat().st_size
    except OSError:
        verified = False
    if not verified:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise AutomationError("VERIFICATION_FAILED", "The copy was created, but its final size did not match the bytes read from the source.")
    return ToolResult(True, "files.copy", {"source": str(source), "destination": str(destination), "size_bytes": copied_bytes}, "File copied and size-verified.", verified=True)


def _copy_unique_to_directory(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    """Copy a file into an approved directory, preserving its extension and never overwriting."""
    source = _resolve_safe_path(args["source"], roots)
    directory = _resolve_safe_path(args["directory"], roots, purpose="copy")
    if source.is_symlink() or not source.is_file():
        raise AutomationError("RESOURCE_NOT_FOUND", "Source must be an existing regular file (not a symlink).")
    if source.stat().st_size > _MAX_COPY_BYTES:
        raise AutomationError("FILE_TOO_LARGE", f"Copies are limited to {_MAX_COPY_BYTES} bytes.")
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = source.stem, source.suffix
    for index in range(1, 1001):
        context.check_cancelled()
        name = source.name if index == 1 else f"{stem}-copy-{index}{suffix}"
        destination = directory / name
        try:
            result = _copy_file(context, {"source": str(source), "destination": str(destination)}, roots)
            data = dict(result.data)
            data["original_filename"] = source.name
            data["filename_preserved"] = destination.suffix.lower() == source.suffix.lower()
            result.data = data
            result.message = "Created a unique verified copy, preserving the original filename extension."
            return result
        except AutomationError as exc:
            if exc.code == "ALREADY_EXISTS":
                continue
            raise
    raise AutomationError("NAME_COLLISION_LIMIT", "Could not find a free name for the copy after 1000 attempts.")


def _move_file(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    source = _resolve_safe_path(args["source"], roots, purpose="move")
    destination = _resolve_safe_path(args["destination"], roots, purpose="move")
    if source.is_symlink() or not source.is_file():
        raise AutomationError("RESOURCE_NOT_FOUND", "Source must be an existing regular file (not a symlink).")
    if source == destination:
        raise AutomationError("INVALID_ARGUMENT", "Source and destination must be different paths.")
    if destination.exists():
        raise AutomationError("ALREADY_EXISTS", "Destination already exists; EVA did not overwrite it.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Same-volume rename is atomic. Cross-volume moves use copy-then-delete only
        # after the copied file verifies, and are never attempted over an existing file.
        try:
            source.rename(destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            copied = _copy_file(context, {"source": str(source), "destination": str(destination)}, roots)
            if not copied.ok:
                raise AutomationError("FILE_MOVE_FAILED", "Cross-volume copy failed; the source file was kept.")
            try:
                source.unlink()
            except OSError as delete_error:
                raise AutomationError("PARTIAL_MOVE", f"Destination copy was created, but source removal failed: {delete_error}") from delete_error
    except OSError as exc:
        raise AutomationError("FILE_MOVE_FAILED", f"Could not move file: {exc}") from exc
    verified = destination.is_file() and not source.exists()
    return ToolResult(True, "files.move", {"source": str(source), "destination": str(destination)}, "File moved.", verified=verified)


def register_filesystem_tools(registry: ToolRegistry, allowed_roots: list[str | Path] | tuple[str | Path, ...] | None = None) -> None:
    roots = _root_paths(allowed_roots)
    list_schema = {"type": "object", "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "recursive": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 2000}},
        "required": ["path"], "additionalProperties": False}
    registry.register(ToolSpec("files.list", "List files and directories within approved roots.", list_schema,
        ("filesystem.read",), "files", timeout_seconds=20, aliases=("list files", "list folder")),
        lambda ctx, args: _list_files(ctx, args, roots))
    registry.register(ToolSpec("files.search", "Search filenames using a glob pattern within approved roots.", {
        "type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "pattern": {"type": "string", "minLength": 1, "maxLength": 128}, "recursive": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
        "required": ["path", "pattern"], "additionalProperties": False},
        ("filesystem.read",), "files", timeout_seconds=30, aliases=("find files", "search files")),
        lambda ctx, args: _search_files(ctx, args, roots))
    registry.register(ToolSpec("files.find_one", "Find exactly one matching filename within approved roots; fails safely on zero, ambiguous, or incomplete results.", {
        "type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "pattern": {"type": "string", "minLength": 1, "maxLength": 128}, "recursive": {"type": "boolean"},
        "exclude_directories": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4096}, "maxItems": 50}},
        "required": ["path", "pattern"], "additionalProperties": False},
        ("filesystem.read",), "files", timeout_seconds=30, aliases=("find one file", "find unique file")),
        lambda ctx, args: _find_one(ctx, args, roots))
    registry.register(ToolSpec("files.resolve_by_name", "Resolve a user-supplied filename: exact name first, then reasonable extension variants/basename within approved roots; fails safely on ambiguity.", {
        "type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "filename": {"type": "string", "minLength": 1, "maxLength": 255}, "recursive": {"type": "boolean"}},
        "required": ["path", "filename"], "additionalProperties": False},
        ("filesystem.read",), "files", timeout_seconds=45, aliases=("resolve filename", "find file by name"),
        when_to_use=("When the user provides a filename or basename and expects EVA to locate it without asking for its extension.",),
        output_description="Unique resolved path in data.path, or explicit missing/ambiguous status.", side_effects="Read-only."),
        lambda ctx, args: _resolve_by_name(ctx, args, roots))
    registry.register(ToolSpec("files.read_document", "Read plain-text files and extract text from supported DOCX, XLSX, PPTX, RTF, ODT, and PDF documents within approved roots.", {
        "type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "max_bytes": {"type": "integer", "minimum": 1, "maximum": _MAX_TEXT_BYTES}},
        "required": ["path"], "additionalProperties": False},
        ("filesystem.read", "document.read"), "files", timeout_seconds=25, aliases=("read document", "extract document text", "read docx"),
        when_to_use=("After locating a document whose actual content the user wants read or displayed; supports plain text, DOCX, XLSX, PPTX, RTF, ODT, and PDF when the relevant optional dependencies are installed.",),
        output_description="Extracted content in data.text with source path, format and truncation metadata.", side_effects="Read-only."),
        lambda ctx, args: _read_document(ctx, args, roots))
    registry.register(ToolSpec("files.find_latest", "Find the uniquely newest regular file matching a filename glob, based on modification time; refuses incomplete scans and timestamp ties.", {
        "type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "pattern": {"type": "string", "minLength": 1, "maxLength": 128}, "recursive": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        "exclude_name": {"type": "string", "maxLength": 160},
        "exclude_directories": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4096}, "maxItems": 50}},
        "required": ["path", "pattern"], "additionalProperties": False},
        ("filesystem.read",), "files", timeout_seconds=30, aliases=("find latest file", "newest matching file"),
        when_to_use=("When the user requests the latest/newest matching local file and the timestamp determines the intended source.",),
        output_description="Unique newest path in data.path, or explicit missing/ambiguous/incomplete result.", side_effects="Read-only."),
        lambda ctx, args: _find_latest(ctx, args, roots))
    registry.register(ToolSpec("files.find_by_date", "Find exactly one regular file whose local creation or modification date matches an explicit ISO date and optional subject term; fails safely on zero, ambiguity, or incomplete scans.", {
        "type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "date": {"type": "string", "minLength": 4, "maxLength": 32},
            "pattern": {"type": "string", "minLength": 1, "maxLength": 128},
            "name_terms": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 10},
            "field": {"type": "string", "enum": ["any", "modified", "created"]},
            "recursive": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "exclude_directories": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4096}, "maxItems": 50}},
        "required": ["path", "date"], "additionalProperties": False},
        ("filesystem.read",), "files", timeout_seconds=30, idempotent=True,
        aliases=("find file by date", "find report from yesterday", "find file created today"),
        when_to_use=("When the user identifies a file by a local calendar date (today/yesterday/ISO) and a subject such as a report, rather than by an exact filename.",),
        output_description="Unique dated path in data.path with modified/created dates and the matched field, or an explicit missing/ambiguous/incomplete result.",
        side_effects="Read-only."),
        lambda ctx, args: _find_by_date(ctx, args, roots))
    registry.register(ToolSpec("files.copy_with_date", "Copy a regular file with an explicit local ISO date in its filename, never overwriting, and verify the copy by SHA-256.", {
        "type": "object", "properties": {
            "source": {"type": "string", "minLength": 1, "maxLength": 4096},
            "date": {"type": "string", "minLength": 4, "maxLength": 32},
            "directory": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["source", "date"], "additionalProperties": False},
        ("filesystem.copy",), "files", timeout_seconds=60, idempotent=False,
        aliases=("copy with today's date", "dated copy", "save copy with date in filename"),
        when_to_use=("When the user asks to copy a selected file and include a local date in the new filename; the original file is never modified.",),
        output_description="Verified dated copy path with source and destination SHA-256 digests and the original filename.",
        side_effects="Creates a new dated copy; never overwrites an existing file."),
        lambda ctx, args: _copy_with_date(ctx, args, roots))
    registry.register(ToolSpec("files.create_docx", "Create a valid Microsoft Word DOCX document from plain text without overwriting an existing file.", {
        "type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "content": {"type": "string", "maxLength": _MAX_TEXT_BYTES}},
        "required": ["path", "content"], "additionalProperties": False},
        ("filesystem.write",), "files", timeout_seconds=20, idempotent=False,
        when_to_use=("When the user explicitly requests a .docx output; do not write plain text with a .docx extension.",),
        output_description="Verified valid DOCX path and format.", side_effects="Creates a new document; refuses to overwrite."),
        lambda ctx, args: _create_docx(ctx, args, roots))
    for tool_name, extension, creator, description, aliases in (
        ("files.create_pptx", ".pptx", _create_pptx, "Create and verify a genuine PowerPoint presentation without overwriting an existing file.", ("create powerpoint", "create pptx")),
        ("files.create_xlsx", ".xlsx", _create_xlsx, "Create and verify a genuine Excel workbook without overwriting an existing file.", ("create spreadsheet", "create xlsx")),
        ("files.create_pdf", ".pdf", _create_pdf, "Create and verify a genuine PDF document without overwriting an existing file.", ("create pdf",)),
        ("files.create_rtf", ".rtf", _create_rtf, "Create and verify a valid Rich Text Format document without overwriting an existing file.", ("create rtf",)),
        ("files.create_odt", ".odt", _create_odt, "Create and verify an OpenDocument Text document without overwriting an existing file.", ("create odt",)),
    ):
        registry.register(ToolSpec(tool_name, description, {
            "type": "object", "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                "content": {"type": "string", "maxLength": _MAX_TEXT_BYTES}},
            "required": ["path"], "additionalProperties": False},
            ("filesystem.write",), "files", timeout_seconds=30, idempotent=False,
            when_to_use=(f"When the user explicitly requests an {extension} output; never write plain text bytes with an office/document extension.",),
            output_description=f"Verified valid {extension[1:].upper()} path and format.", side_effects="Creates a new document; refuses to overwrite."),
            lambda ctx, args, creator=creator: creator(ctx, args, roots))
    registry.register(ToolSpec("files.find_duplicates", "Find duplicate regular files by size and SHA-256 within approved roots; read-only and never deletes.", {
        "type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "recursive": {"type": "boolean"},
            "max_files": {"type": "integer", "minimum": 1, "maximum": 20000},
            "max_total_bytes": {"type": "integer", "minimum": 1, "maximum": 5368709120},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60}},
        "required": ["path"], "additionalProperties": False},
        ("filesystem.read",), "files", timeout_seconds=60, aliases=("find duplicate files", "scan duplicate files")),
        lambda ctx, args: _find_duplicates(ctx, args, roots))
    registry.register(ToolSpec("files.create_directory", "Create a directory within approved roots.", {
        "type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["path"], "additionalProperties": False},
        ("filesystem.write",), "files", timeout_seconds=10, idempotent=True, aliases=("create folder",)),
        lambda ctx, args: _create_directory(ctx, args, roots))
    registry.register(ToolSpec("files.create_text_file", "Create a UTF-8 text file without overwriting existing content.", {
        "type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "content": {"type": "string", "maxLength": _MAX_TEXT_BYTES}},
        "required": ["path"], "additionalProperties": False},
        ("filesystem.write",), "files", timeout_seconds=15, idempotent=False, aliases=("create text file",)),
        lambda ctx, args: _create_text_file(ctx, args, roots))
    registry.register(ToolSpec("files.read_text", "Read a bounded amount of UTF-8 text from a regular file within approved roots; returns the actual contents for display or later steps.", {
        "type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": _MAX_TEXT_BYTES}},
        "required": ["path"], "additionalProperties": False},
        ("filesystem.read",), "files", timeout_seconds=15, aliases=("read text file", "show file contents")),
        lambda ctx, args: _read_text(ctx, args, roots))
    registry.register(ToolSpec("files.create_unique_text_file", "Create a new UTF-8 text file in an approved directory, automatically adding a numeric suffix to avoid overwriting existing files.", {
        "type": "object", "properties": {
            "directory": {"type": "string", "minLength": 1, "maxLength": 4096},
            "filename": {"type": "string", "minLength": 1, "maxLength": 160},
            "content": {"type": "string", "maxLength": _MAX_TEXT_BYTES}},
        "required": ["directory", "filename", "content"], "additionalProperties": False},
        ("filesystem.write",), "files", timeout_seconds=20, idempotent=False, aliases=("create unique text file", "save draft")),
        lambda ctx, args: _create_unique_text_file(ctx, args, roots))
    registry.register(ToolSpec("files.copy", "Copy a regular file to a new destination without overwriting.", {
        "type": "object", "properties": {"source": {"type": "string", "minLength": 1, "maxLength": 4096},
        "destination": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["source", "destination"], "additionalProperties": False},
        ("filesystem.copy",), "files", timeout_seconds=60, idempotent=False, aliases=("copy file",)),
        lambda ctx, args: _copy_file(ctx, args, roots))
    registry.register(ToolSpec("files.copy_unique_to_directory", "Copy a regular file into an approved directory, preserving its filename extension and automatically adding a suffix rather than overwriting.", {
        "type": "object", "properties": {
            "source": {"type": "string", "minLength": 1, "maxLength": 4096},
            "directory": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["source", "directory"], "additionalProperties": False},
        ("filesystem.copy",), "files", timeout_seconds=60, idempotent=False,
        aliases=("copy file preserving extension", "make a copy without overwriting"),
        when_to_use=("When the user asks to save a copy of a selected source file into a destination folder and the original extension should be preserved.",),
        output_description="Verified copy path with original filename and extension information.",
        side_effects="Creates a new copy in the requested approved directory; never overwrites."),
        lambda ctx, args: _copy_unique_to_directory(ctx, args, roots))
    registry.register(ToolSpec("files.move", "Move a regular file to a new destination without overwriting.", {
        "type": "object", "properties": {"source": {"type": "string", "minLength": 1, "maxLength": 4096},
        "destination": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["source", "destination"], "additionalProperties": False},
        ("filesystem.move",), "files", timeout_seconds=60, idempotent=False,
        risk=RiskLevel.HIGH, requires_approval=True, aliases=("move file",)),
        lambda ctx, args: _move_file(ctx, args, roots))
