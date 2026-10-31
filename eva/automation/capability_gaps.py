"""Persistent registry of automation capabilities EVA could not satisfy.

Records are engineering telemetry, not instructions. They are written atomically
as versioned JSON and never executed or imported as code.
"""
from __future__ import annotations

import json
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA_VERSION = 1
_MAX_GAPS = 500
_MAX_EXAMPLES = 8
_MAX_TEXT = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: Any, limit: int = _MAX_TEXT) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()
    return text[:limit]


def _gap_id(name: str, category: str) -> str:
    clean_name = re.sub(r"[^a-z0-9_.-]+", "_", name.lower()).strip("_.-")
    clean_category = re.sub(r"[^a-z0-9_-]+", "_", category.lower()).strip("_-") or "general"
    return f"{clean_category}:{clean_name or 'unknown'}"


def _safe_example(prompt: str) -> str:
    """Keep a short reproducible example without storing obvious personal secrets."""
    value = _clean(prompt, 1000)
    value = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]", value, flags=re.I)
    value = re.sub(r"https?://\S+", "[url]", value, flags=re.I)
    value = re.sub(r"(?i)\b(password|passcode|token|api[_ -]?key|secret)\s*[:=]\s*\S+", r"\1=[redacted]", value)
    value = re.sub(r"(?i)\bC:\\Users\\[^\s\"']+", "[path]", value)
    value = re.sub(r"(?i)(?:/home/|/users/)[^/\s]+(?:/[^\s]+)*", "[path]", value)
    # Don't save long quoted body/text payloads in the engineering gap catalogue.
    return value[:400]


class CapabilityGapStore:
    """Thread-safe, duplicate-aware JSON store for genuinely missing capabilities."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": _SCHEMA_VERSION, "updated_at": _now(), "gaps": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            # Preserve the bad file for diagnosis instead of silently overwriting it.
            raise RuntimeError(f"Capability-gap file is unreadable: {self.path}")
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION or not isinstance(payload.get("gaps"), list):
            raise RuntimeError(f"Capability-gap file has an unsupported schema: {self.path}")
        payload.setdefault("updated_at", _now())
        return payload

    def record(
        self,
        name: str,
        *,
        category: str = "general",
        description: str = "",
        reason: str = "No registered tool currently satisfies this capability.",
        prompt: str = "",
        risk: str = "unknown",
        required_inputs: list[str] | tuple[str, ...] = (),
        dependencies: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        name = _clean(name, 160) or "unknown"
        category = _clean(category, 80) or "general"
        gap_id = _gap_id(name, category)
        example = _safe_example(prompt)
        clean_desc = _clean(_safe_example(description))
        clean_reason = _clean(_safe_example(reason))
        now = _now()
        with self._lock:
            payload = self._read()
            rows = payload["gaps"]
            item = next((row for row in rows if isinstance(row, dict) and row.get("id") == gap_id), None)
            if item is None:
                item = {
                    "id": gap_id,
                    "name": name,
                    "category": category,
                    "description": clean_desc,
                    "reason": clean_reason,
                    "risk": _clean(risk, 40) or "unknown",
                    "required_inputs": sorted({_clean(x, 120) for x in required_inputs if _clean(x, 120)}),
                    "dependencies": sorted({_clean(x, 160) for x in dependencies if _clean(x, 160)}),
                    "status": "missing",
                    "occurrences": 0,
                    "first_seen": now,
                    "last_seen": now,
                    "examples": [],
                }
                rows.append(item)
            else:
                # Refresh informative fields while preserving the original first_seen.
                item["description"] = clean_desc or item.get("description", "")
                item["reason"] = clean_reason or item.get("reason", "")
                item["risk"] = _clean(risk, 40) or item.get("risk", "unknown")
                item["required_inputs"] = sorted(set(item.get("required_inputs", [])) | {_clean(x, 120) for x in required_inputs if _clean(x, 120)})
                item["dependencies"] = sorted(set(item.get("dependencies", [])) | {_clean(x, 160) for x in dependencies if _clean(x, 160)})
                item["status"] = item.get("status", "missing")
                item["last_seen"] = now
            item["occurrences"] = int(item.get("occurrences", 0)) + 1
            item["last_seen"] = now
            examples = item.setdefault("examples", [])
            if example and example not in examples:
                examples.append(example)
                del examples[:-_MAX_EXAMPLES]
            # Bound long-running usage and avoid uncontrolled file growth.
            payload["gaps"] = rows[-_MAX_GAPS:]
            payload["updated_at"] = now
            self._write_atomic(payload)
            return dict(item)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._read()["gaps"] if isinstance(item, dict)]

    def _write_atomic(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with open(fd, "w", encoding="utf-8", newline="\n", closefd=True) as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                import os
                os.fsync(stream.fileno())
            Path(temp_name).replace(self.path)
        except Exception:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
            raise
