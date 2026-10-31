"""Safe, bounded CSV/JSON and ZIP tools that use only the Python standard library."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from eva.automation.engine.models import AutomationError, ExecutionContext, RiskLevel, ToolResult, ToolSpec
from eva.automation.engine.registry import ToolRegistry
from eva.automation.tools.filesystem import (
    _SKIPPED_DIRS,
    _is_sensitive,
    _resolve_safe_path,
    _root_paths,
)

_MAX_DATA_FILE_BYTES = 10 * 1024 * 1024
_MAX_TABLE_ROWS = 10_000
_MAX_TABLE_COLUMNS = 256
_MAX_ARCHIVE_FILES = 2_000
_MAX_ARCHIVE_TOTAL_BYTES = 536_870_912
_MAX_ARCHIVE_MEMBER_BYTES = 134_217_728
_COPY_CHUNK_BYTES = 1024 * 1024


def _read_bounded(path: Path, max_bytes: int = _MAX_DATA_FILE_BYTES) -> bytes:
    try:
        if path.stat().st_size > max_bytes:
            raise AutomationError("FILE_TOO_LARGE", f"Input file exceeds {max_bytes} bytes.")
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except AutomationError:
        raise
    except OSError as exc:
        raise AutomationError("FILE_READ_FAILED", f"Could not read input file: {exc}") from exc
    if len(data) > max_bytes:
        raise AutomationError("FILE_TOO_LARGE", f"Input file exceeds {max_bytes} bytes.")
    return data


def _write_new_file(path: Path, payload: bytes) -> None:
    if len(payload) > _MAX_DATA_FILE_BYTES:
        raise AutomationError("OUTPUT_TOO_LARGE", f"Generated data file exceeds {_MAX_DATA_FILE_BYTES} bytes.")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "Destination already exists; EVA did not overwrite it.") from exc
    except OSError as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise AutomationError("FILE_WRITE_FAILED", f"Could not write output file: {exc}") from exc


def _resolve_records(data: Any, records_path: str | None) -> list[dict[str, Any]]:
    value = data
    if records_path:
        for segment in records_path.split("."):
            if not segment or not isinstance(value, dict) or segment not in value:
                raise AutomationError("INVALID_DATA", f"records_path segment '{segment}' is not present in the JSON document.")
            value = value[segment]
    if not isinstance(value, list):
        raise AutomationError("INVALID_DATA", "JSON input must be an array of objects (or use records_path to select one).")
    if len(value) > _MAX_TABLE_ROWS:
        raise AutomationError("INPUT_TOO_LARGE", f"Tabular conversion is limited to {_MAX_TABLE_ROWS} rows.")
    if any(not isinstance(record, dict) for record in value):
        raise AutomationError("INVALID_DATA", "Every record must be a JSON object.")
    return value


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "|")
_NUMERIC_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")


def _is_numeric(val: Any) -> bool:
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return True
    if isinstance(val, str) and _NUMERIC_RE.match(val.strip()):
        return True
    return False


def _scalar_to_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sanitize_csv_cell(value: Any) -> str:
    cell = _scalar_to_cell(value)
    if not cell:
        return ""
    stripped = cell.lstrip("\ufeff \t\r\n")
    if not stripped:
        return cell
    if stripped.startswith(_FORMULA_PREFIXES) and not _is_numeric(stripped):
        return "'" + cell
    if stripped.startswith("'"):
        inner = stripped.lstrip("'").lstrip("\ufeff \t\r\n")
        if inner and inner.startswith(_FORMULA_PREFIXES) and not _is_numeric(inner):
            return "'" + cell
    return cell


def _unescape_csv_cell(value: str) -> str:
    if isinstance(value, str) and value.startswith("'") and len(value) > 1:
        rest = value[1:]
        stripped_rest = rest.lstrip("\ufeff \t\r\n")
        if stripped_rest:
            if stripped_rest.startswith(_FORMULA_PREFIXES) and not _is_numeric(stripped_rest):
                return rest
            if stripped_rest.startswith("'"):
                inner = stripped_rest.lstrip("'").lstrip("\ufeff \t\r\n")
                if inner and inner.startswith(_FORMULA_PREFIXES) and not _is_numeric(inner):
                    return rest
    return value


def _csv_to_json(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    source = _resolve_safe_path(args["source"], roots)
    destination_value = str(args.get("destination", "")).strip()
    destination = _resolve_safe_path(destination_value, roots, purpose="write") if destination_value else source.with_suffix(".json")
    destination = _resolve_safe_path(str(destination), roots, purpose="write")
    if source.is_symlink() or not source.is_file():
        raise AutomationError("RESOURCE_NOT_FOUND", "CSV source must be an existing regular file.")
    if source == destination:
        raise AutomationError("INVALID_ARGUMENT", "Source and destination must differ.")
    raw = _read_bounded(source)
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames or any(not isinstance(name, str) or not name.strip() for name in reader.fieldnames):
            raise AutomationError("INVALID_DATA", "CSV must contain a non-empty header row.")
        if len(reader.fieldnames) > _MAX_TABLE_COLUMNS or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise AutomationError("INVALID_DATA", "CSV headers must be unique and no more than 256 columns.")
        records = []
        for index, row in enumerate(reader):
            context.check_cancelled()
            if index >= _MAX_TABLE_ROWS:
                raise AutomationError("INPUT_TOO_LARGE", f"CSV conversion is limited to {_MAX_TABLE_ROWS} rows.")
            if None in row:
                raise AutomationError("INVALID_DATA", "A CSV row has more cells than the header row.")
            records.append({_unescape_csv_cell(key): _unescape_csv_cell("" if value is None else value) for key, value in row.items()})
        payload = (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    except UnicodeDecodeError as exc:
        raise AutomationError("INVALID_DATA", "CSV must be UTF-8 text.") from exc
    except csv.Error as exc:
        raise AutomationError("INVALID_DATA", f"Could not parse CSV: {exc}") from exc
    _write_new_file(destination, payload)
    verified = json.loads(destination.read_text(encoding="utf-8")) == records
    return ToolResult(True, "data.csv_to_json", {"source": str(source), "destination": str(destination), "rows": len(records), "columns": len(reader.fieldnames)}, "CSV converted to JSON and verified.", verified=verified)


def _json_to_csv(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    source = _resolve_safe_path(args["source"], roots)
    destination = _resolve_safe_path(args["destination"], roots, purpose="write")
    if source.is_symlink() or not source.is_file():
        raise AutomationError("RESOURCE_NOT_FOUND", "JSON source must be an existing regular file.")
    if source == destination:
        raise AutomationError("INVALID_ARGUMENT", "Source and destination must differ.")
    try:
        data = json.loads(_read_bounded(source).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AutomationError("INVALID_DATA", f"Could not parse JSON input: {exc}") from exc
    records = _resolve_records(data, args.get("records_path"))
    fieldnames: list[str] = []
    for record in records:
        context.check_cancelled()
        for key in record:
            if not isinstance(key, str):
                raise AutomationError("INVALID_DATA", "JSON record keys must be strings.")
            if key not in fieldnames:
                fieldnames.append(key)
                if len(fieldnames) > _MAX_TABLE_COLUMNS:
                    raise AutomationError("INPUT_TOO_LARGE", f"Conversion is limited to {_MAX_TABLE_COLUMNS} columns.")
    sanitized_fieldnames = [_sanitize_csv_cell(key) for key in fieldnames]
    if len(set(sanitized_fieldnames)) != len(sanitized_fieldnames):
        raise AutomationError("INVALID_DATA", "Sanitized CSV column headers must remain unique.")
    text_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(text_buffer, fieldnames=sanitized_fieldnames, extrasaction="raise")
    writer.writeheader()
    for record in records:
        context.check_cancelled()
        writer.writerow({_sanitize_csv_cell(key): _sanitize_csv_cell(record.get(key)) for key in fieldnames})
    payload = text_buffer.getvalue().encode("utf-8")
    _write_new_file(destination, payload)
    try:
        with destination.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            read_fieldnames = reader.fieldnames or []
            if read_fieldnames != sanitized_fieldnames:
                raise AutomationError("VERIFICATION_FAILED", "CSV header verification failed: headers do not match expected columns.")
            for h in read_fieldnames:
                h_stripped = (h or "").lstrip("\ufeff \t\r\n")
                if h_stripped and h_stripped.startswith(_FORMULA_PREFIXES) and not _is_numeric(h_stripped):
                    raise AutomationError("VERIFICATION_FAILED", f"CSV header '{h}' contains unneutralized spreadsheet formula.")
            actual_rows = 0
            for row_idx, row in enumerate(reader):
                actual_rows += 1
                for col_name, cell_value in row.items():
                    val = cell_value or ""
                    val_stripped = val.lstrip("\ufeff \t\r\n")
                    if val_stripped and val_stripped.startswith(_FORMULA_PREFIXES) and not _is_numeric(val_stripped):
                        raise AutomationError("VERIFICATION_FAILED", f"CSV row {row_idx + 1} column '{col_name}' contains unneutralized spreadsheet formula.")
    except OSError as exc:
        raise AutomationError("VERIFICATION_FAILED", f"CSV was written but could not be verified: {exc}") from exc
    verified = actual_rows == len(records)
    if not verified:
        raise AutomationError("VERIFICATION_FAILED", "CSV output row count does not match the JSON records.")
    return ToolResult(True, "data.json_to_csv", {"source": str(source), "destination": str(destination), "rows": len(records), "columns": len(fieldnames)}, "JSON converted to CSV and row count verified.", verified=True)


def _safe_archive_name(raw_name: str) -> PurePosixPath:
    normalized = raw_name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (not normalized or normalized.startswith("/") or "\x00" in normalized
            or any(part in {".", ".."} for part in pure.parts)
            or any(":" in part for part in pure.parts)):
        raise AutomationError("UNSAFE_ARCHIVE_PATH", f"Archive member has an unsafe path: {raw_name!r}.")
    if _is_sensitive(Path(*pure.parts)):
        raise AutomationError("SENSITIVE_PATH_BLOCKED", f"Archive member targets a credential/private-key path: {raw_name!r}.")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
    for part in pure.parts:
        stem = part.rstrip(". ").split(".", 1)[0].upper()
        if stem in reserved or part.endswith((".", " ")) or any(char in '<>"|?*' for char in part):
            raise AutomationError("UNSAFE_ARCHIVE_PATH", f"Archive member contains a reserved or invalid Windows filename: {raw_name!r}.")
    return pure


def _zip_create(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    source = _resolve_safe_path(args["source"], roots)
    destination = _resolve_safe_path(args["destination"], roots, purpose="write")
    if not source.is_dir() or source.is_symlink():
        raise AutomationError("RESOURCE_NOT_FOUND", "Archive source must be an existing directory.")
    if destination.exists():
        raise AutomationError("ALREADY_EXISTS", "Archive destination already exists; EVA did not overwrite it.")
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise AutomationError("INVALID_ARGUMENT", "ZIP destination cannot be inside the source directory.")
    candidates: list[Path] = []
    total_bytes = 0
    deadline_dirs = 5000
    visited_dirs = 0
    for current, dirs, files in os.walk(source, followlinks=False):
        context.check_cancelled()
        visited_dirs += 1
        if visited_dirs > deadline_dirs:
            raise AutomationError("TRAVERSAL_LIMIT", "Source directory contains too many directories for a safe archive.")
        here = Path(current)
        dirs[:] = [name for name in dirs if name.lower() not in _SKIPPED_DIRS and not _is_sensitive(here / name) and not (here / name).is_symlink()]
        for filename in files:
            candidate = here / filename
            if candidate.is_symlink() or _is_sensitive(candidate) or not candidate.is_file():
                continue
            total_bytes += candidate.stat().st_size
            if len(candidates) >= _MAX_ARCHIVE_FILES or total_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                raise AutomationError("ARCHIVE_LIMIT", "Archive source exceeds the configured file-count or uncompressed-size limit.")
            candidates.append(candidate)
    if len(candidates) == 0:
        raise AutomationError("INVALID_ARGUMENT", "No eligible files were found to archive.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as raw:
            with zipfile.ZipFile(raw, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                actual_total = 0
                for candidate in candidates:
                    context.check_cancelled()
                    archive_name = candidate.relative_to(source).as_posix()
                    member_total = 0
                    with candidate.open("rb") as source_stream, archive.open(archive_name, "w") as member_stream:
                        while True:
                            context.check_cancelled()
                            chunk = source_stream.read(_COPY_CHUNK_BYTES)
                            if not chunk:
                                break
                            member_total += len(chunk)
                            actual_total += len(chunk)
                            if member_total > _MAX_ARCHIVE_MEMBER_BYTES or actual_total > _MAX_ARCHIVE_TOTAL_BYTES:
                                raise AutomationError("ARCHIVE_LIMIT", "Source contents changed and exceeded an archive size limit.")
                            member_stream.write(chunk)
                    if member_total != candidate.stat().st_size:
                        raise AutomationError("SOURCE_CHANGED", f"File changed while archiving: {candidate.name}")
    except FileExistsError as exc:
        raise AutomationError("ALREADY_EXISTS", "Archive destination already exists; EVA did not overwrite it.") from exc
    except AutomationError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        destination.unlink(missing_ok=True)
        raise AutomationError("ARCHIVE_CREATE_FAILED", f"Could not create ZIP archive: {exc}") from exc
    try:
        with zipfile.ZipFile(destination, "r") as archive:
            bad_member = archive.testzip()
            verified = bad_member is None and len(archive.infolist()) == len(candidates)
    except (OSError, zipfile.BadZipFile):
        verified = False
    if not verified:
        destination.unlink(missing_ok=True)
        raise AutomationError("VERIFICATION_FAILED", "Created ZIP archive did not pass its integrity check.")
    return ToolResult(True, "archive.zip_create", {"source": str(source), "destination": str(destination), "files": len(candidates), "uncompressed_bytes": actual_total}, "ZIP archive created and integrity-checked.", verified=True)


def _zip_extract(context: ExecutionContext, args: dict[str, Any], roots: tuple[Path, ...]) -> ToolResult:
    source = _resolve_safe_path(args["source"], roots)
    destination = _resolve_safe_path(args["destination"], roots, purpose="write")
    if source.is_symlink() or not source.is_file() or not zipfile.is_zipfile(source):
        raise AutomationError("RESOURCE_NOT_FOUND", "Source must be an existing regular ZIP file.")
    if destination.exists():
        raise AutomationError("ALREADY_EXISTS", "Extraction destination already exists; EVA did not merge or overwrite files.")
    if destination == source:
        raise AutomationError("INVALID_ARGUMENT", "ZIP source and destination must differ.")
    try:
        with zipfile.ZipFile(source, "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_ARCHIVE_FILES:
                raise AutomationError("ARCHIVE_LIMIT", f"ZIP must contain 1 to {_MAX_ARCHIVE_FILES} entries.")
            total_bytes = 0
            safe_items = []
            seen = set()
            for info in infos:
                context.check_cancelled()
                name = _safe_archive_name(info.filename)
                normalized = name.as_posix().rstrip("/")
                seen_key = normalized.casefold() if os.name == "nt" else normalized
                if seen_key in seen:
                    raise AutomationError("INVALID_ARCHIVE", f"ZIP contains duplicate path: {normalized}")
                seen.add(seen_key)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise AutomationError("UNSAFE_ARCHIVE_ENTRY", "ZIP symbolic links are not extracted.")
                if info.file_size < 0 or info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise AutomationError("ARCHIVE_LIMIT", f"ZIP member exceeds {_MAX_ARCHIVE_MEMBER_BYTES} bytes.")
                total_bytes += info.file_size
                if total_bytes > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise AutomationError("ARCHIVE_LIMIT", "ZIP uncompressed contents exceed the archive-size limit.")
                safe_items.append((info, name))
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".eva-extract-", dir=destination.parent))
            try:
                for info, name in safe_items:
                    context.check_cancelled()
                    target = staging.joinpath(*name.parts)
                    try:
                        target.resolve(strict=False).relative_to(staging.resolve())
                    except ValueError as exc:
                        raise AutomationError("UNSAFE_ARCHIVE_PATH", f"Archive member escapes staging directory: {info.filename!r}.") from exc
                    if info.is_dir() or info.filename.endswith(("/", "\\\\")):
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as src, target.open("xb") as dst:
                        copied = 0
                        while True:
                            context.check_cancelled()
                            chunk = src.read(_COPY_CHUNK_BYTES)
                            if not chunk:
                                break
                            copied += len(chunk)
                            if copied > _MAX_ARCHIVE_MEMBER_BYTES:
                                raise AutomationError("ARCHIVE_LIMIT", "Archive member exceeded its size limit while extracting.")
                            dst.write(chunk)
                    if copied != info.file_size:
                        raise AutomationError("VERIFICATION_FAILED", f"Extracted size mismatch for {info.filename!r}.")
                # Rename the complete staging tree into place only if no destination appeared meanwhile.
                if destination.exists():
                    raise AutomationError("ALREADY_EXISTS", "Extraction destination appeared during the operation; no merge was attempted.")
                staging.rename(destination)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
    except AutomationError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise AutomationError("ARCHIVE_EXTRACT_FAILED", f"Could not safely extract ZIP: {exc}") from exc
    files = sum(1 for path in destination.rglob("*") if path.is_file())
    return ToolResult(True, "archive.zip_extract", {"source": str(source), "destination": str(destination), "files": files, "uncompressed_bytes": total_bytes}, "ZIP extracted into a new directory and verified.", verified=files == sum(not info.is_dir() for info, _ in safe_items))


def register_data_file_tools(registry: ToolRegistry, allowed_roots: list[str | Path] | tuple[str | Path, ...] | None = None) -> None:
    roots = _root_paths(allowed_roots)
    file_schema = {"type": "object", "properties": {
        "source": {"type": "string", "minLength": 1, "maxLength": 4096},
        "destination": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["source", "destination"], "additionalProperties": False}
    csv_json_schema = {"type": "object", "properties": {
        "source": {"type": "string", "minLength": 1, "maxLength": 4096},
        "destination": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["source"], "additionalProperties": False}
    registry.register(ToolSpec("data.csv_to_json", "Convert a bounded UTF-8 CSV file into a JSON array of records. If destination is omitted, create a sibling .json file without overwriting.", csv_json_schema,
        ("filesystem.read", "filesystem.write"), "data", timeout_seconds=45, idempotent=False,
        aliases=("convert CSV to JSON",)), lambda ctx, args: _csv_to_json(ctx, args, roots))
    json_schema = {**file_schema, "properties": {**file_schema["properties"],
        "records_path": {"type": "string", "maxLength": 256}},}
    registry.register(ToolSpec("data.json_to_csv", "Convert an array of JSON objects to a CSV file without overwriting existing output.", json_schema,
        ("filesystem.read", "filesystem.write"), "data", timeout_seconds=45, idempotent=False,
        aliases=("convert JSON to CSV",)), lambda ctx, args: _json_to_csv(ctx, args, roots))
    registry.register(ToolSpec("archive.zip_create", "Create and integrity-check a ZIP archive from eligible files within a folder.", file_schema,
        ("filesystem.read", "filesystem.write"), "archive", timeout_seconds=120, idempotent=False,
        aliases=("create zip archive",)), lambda ctx, args: _zip_create(ctx, args, roots))
    registry.register(ToolSpec("archive.zip_extract", "Safely extract a ZIP archive into a new directory without overwriting files.", file_schema,
        ("filesystem.read", "filesystem.write"), "archive", timeout_seconds=120, idempotent=False,
        risk=RiskLevel.MODERATE, aliases=("extract zip archive",)), lambda ctx, args: _zip_extract(ctx, args, roots))
