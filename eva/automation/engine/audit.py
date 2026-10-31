"""Minimal redacted JSONL audit trail for automation executions."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SECRET_PARTS = ("password", "passwd", "token", "secret", "authorization", "api_key", "client_secret", "credential", "content", "clipboard", "typed_text", "value", "text", "query", "prompt", "body", "recipient", "email_address")
_DEFAULT_PATH = object()


def redact(value: Any, *, key: str = "") -> Any:
    lowered_key = key.lower()
    if any(part in lowered_key for part in _SECRET_PARTS):
        return "[REDACTED]"
    if lowered_key in {"url", "uri", "href"} and isinstance(value, str):
        try:
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                host = parsed.hostname or "[REDACTED_HOST]"
                try:
                    if parsed.port is not None:
                        host = f"{host}:{parsed.port}"
                except ValueError:
                    pass
                return urlunsplit((parsed.scheme, f"[REDACTED]@{host}", parsed.path,
                                   "[REDACTED_QUERY]" if parsed.query else "",
                                   "[REDACTED_FRAGMENT]" if parsed.fragment else ""))
            if parsed.query or parsed.fragment:
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                   "[REDACTED_QUERY]" if parsed.query else "",
                                   "[REDACTED_FRAGMENT]" if parsed.fragment else ""))
        except ValueError:
            return "[REDACTED_URL]"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)[:500]


class AuditLogger:
    def __init__(self, path: str | Path | None | object = _DEFAULT_PATH):
        if path is _DEFAULT_PATH:
            configured = os.environ.get("EVA_AUTOMATION_AUDIT_PATH", "").strip()
            path = Path(configured).expanduser() if configured else Path.home() / ".eva" / "logs" / "automation.jsonl"
        self.path = Path(path).expanduser() if path is not None else None
        self._lock = Lock()
        self._fallback_events: list[dict[str, Any]] = []
        self.last_write_error: OSError | None = None

    def is_writable(self) -> bool:
        """Check if the audit destination is writable or gracefully buffered."""
        if self.path is None:
            return True
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    pass
            return True
        except OSError as exc:
            self.last_write_error = exc
            return False

    def write(self, event: dict[str, Any]) -> None:
        if self.path is None:
            return
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), **redact(event)}
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Keep the on-disk audit bounded. Retain one previous segment only.
                if self.path.exists() and self.path.stat().st_size > 5 * 1024 * 1024:
                    backup = self.path.with_suffix(self.path.suffix + ".1")
                    backup.unlink(missing_ok=True)
                    self.path.replace(backup)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                self.last_write_error = None
        except OSError as exc:
            self.last_write_error = exc
            with self._lock:
                if len(self._fallback_events) >= 1000:
                    self._fallback_events.pop(0)
                self._fallback_events.append(row)
            try:
                import sys
                sys.stderr.write(f"[EVA AUDIT WARNING] Could not write audit log ({exc}); event buffered in memory fallback.\n")
            except Exception:
                pass
