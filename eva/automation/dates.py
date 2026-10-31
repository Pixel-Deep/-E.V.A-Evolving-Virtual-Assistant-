"""Shared local-date parsing for automation plans and filesystem tools.

These helpers deliberately use the computer's local calendar date rather than
UTC. A user who asks for "today's date" in a document expects the date shown by
their own operating system, not the server date in another timezone.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from typing import Any

_ISO_DATE = re.compile(r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*$")


def local_today(now: datetime | None = None) -> date:
    """Return the local calendar date for ``now`` (or the current date)."""
    return (now or datetime.now()).date()


def resolve_local_date(value: Any, *, now: datetime | None = None) -> date:
    """Resolve a small, explicit set of local date phrases to a ``date``.

    Supported values are ``today``, ``yesterday``, ``tomorrow``, an ISO date
    (``YYYY-MM-DD``), and an existing ``date``/``datetime`` object. Anything
    else raises ``ValueError`` so callers fail closed instead of silently using
    the wrong day.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip().casefold()
    today = local_today(now)
    if not text or text in {"today", "todays", "today's", "current", "current date"}:
        return today
    if text in {"yesterday", "yesterdays", "yesterday's"}:
        return today - timedelta(days=1)
    if text in {"tomorrow", "tomorrows", "tomorrow's"}:
        return today + timedelta(days=1)
    match = _ISO_DATE.match(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError as exc:
            raise ValueError(f"Invalid calendar date: {value!r}") from exc
    raise ValueError(f"Unsupported local date value: {value!r}")


def date_slug(value: Any, *, now: datetime | None = None) -> str:
    """Return the ISO ``YYYY-MM-DD`` filename-safe representation."""
    return resolve_local_date(value, now=now).isoformat()


def date_phrase_in(text: str, *, now: datetime | None = None) -> str | None:
    """Find an explicit relative date phrase in user text.

    Only phrases that are unambiguous are returned. The caller remains
    responsible for checking that the phrase belongs to the intended clause.
    """
    low = (text or "").casefold()
    if re.search(r"\byesterday\b", low):
        return "yesterday"
    if re.search(r"\b(?:today|today'?s|current date)\b", low):
        return "today"
    if re.search(r"\btomorrow\b", low):
        return "tomorrow"
    match = re.search(r"\b(\d{4}-\d{1,2}-\d{1,2})\b", low)
    if match:
        try:
            resolve_local_date(match.group(1), now=now)
        except ValueError:
            return None
        return match.group(1)
    return None
