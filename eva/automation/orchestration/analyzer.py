"""Deterministic semantic analysis for EVA's compound automation requests.

Analysis is deliberately bounded: it recognises a small set of high-confidence
workflow shapes whose requested outcomes can be verified independently. Anything
outside those shapes returns ``None`` and keeps the existing routing/planning
behaviour. No model is consulted here, so analysis cannot invent a workflow.
"""
from __future__ import annotations

import re
from typing import Any

from eva.automation.dates import date_phrase_in, date_slug
from eva.automation.orchestration.models import RequestAnalysis, WorkflowKind
from eva.automation.orchestration.topics import (
    PYTHON_DOWNLOADS_URL,
    canonical_url_for,
    clean_topic,
    extract_wikipedia_topic,
    find_known_topic_in_text,
)

_NOTEPAD = re.compile(r"\b(?:not\s*epad|note\s*pad)\b", re.I)
_SUMMARY = re.compile(
    r"\b(?:summari[sz]e|summary|overview|key\s+points|main\s+points|"
    r"write\s+about\s+(?:him|her|them|it)|information\s+about)\b", re.I,
)
_NOTEPAD_DELIVERABLE_ACTION = re.compile(
    r"\b(?:write|type|enter|paste|put|draft|compose|prepare|save|saved|"
    r"written|summari[sz]e|summary)\b", re.I,
)
_DATE_VALUE = (
    r"(?:today(?:'?s)?|yesterday(?:'?s)?|tomorrow(?:'?s)?|"
    r"current\s+date|\d{4}-\d{1,2}-\d{1,2})"
)


def _normalize_date_phrase(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip().casefold())
    text = text.replace("\u2019", "'")
    text = re.sub(r"'s$", "", text)
    if text in {"current date", "current"}:
        return "today"
    return text


def _notepad_deliverable(low: str) -> bool:
    return bool(_NOTEPAD.search(low)) and bool(_NOTEPAD_DELIVERABLE_ACTION.search(low))


def _wikipedia_summary_analysis(text: str, low: str) -> RequestAnalysis | None:
    if not _SUMMARY.search(low) or not _notepad_deliverable(low):
        return None
    if "wikipedia" in low:
        topic = clean_topic(extract_wikipedia_topic(text))
        if not topic:
            return None
        canonical = canonical_url_for(topic) or ""
        if not canonical:
            # Extraction can retain surrounding action phrasing. Recover a known
            # canonical article from any alias still present in the topic text.
            known_in_topic = find_known_topic_in_text(topic)
            if known_in_topic is not None:
                topic = known_in_topic.name
                canonical = known_in_topic.url
        return RequestAnalysis(
            kind=WorkflowKind.WEB_SUMMARY_NOTEPAD,
            request=text,
            topic=topic,
            canonical_url=canonical,
            plan_owned=True,
        )
    # Known public figures are accepted without the word "Wikipedia" when the
    # request clearly asks for a researched summary plus a Notepad deliverable.
    # The canonical article is still only a locator; verification requires the
    # page text actually returned by web.browse.
    known = find_known_topic_in_text(text)
    if known is None:
        return None
    if not re.search(r"\b(?:search|look\s+up|research|find|summari[sz]e|summary|information)\b", low):
        return None
    return RequestAnalysis(
        kind=WorkflowKind.WEB_SUMMARY_NOTEPAD,
        request=text,
        topic=known.name,
        canonical_url=known.url,
        plan_owned=True,
    )


def _python_downloads_analysis(text: str, low: str) -> RequestAnalysis | None:
    if not re.search(r"\bpython\b", low):
        return None
    if not re.search(r"\bdownloads?\b", low):
        return None
    if not _SUMMARY.search(low) or not _notepad_deliverable(low):
        return None
    return RequestAnalysis(
        kind=WorkflowKind.PYTHON_DOWNLOADS_SUMMARY,
        request=text,
        topic="Python downloads",
        canonical_url=PYTHON_DOWNLOADS_URL,
        plan_owned=True,
    )


def _dated_file_analysis(text: str, low: str) -> RequestAnalysis | None:
    copy_requested = bool(re.search(r"\b(?:copy|duplicate)\b", low))
    if not copy_requested:
        return None
    # A directory name containing a date is a different workflow; do not turn it
    # into a dated filename copy.
    if re.search(r"\b(?:folder|directory)\b.{0,60}\b(?:today|yesterday|tomorrow|date)\b", low):
        return None
    subject_match = re.search(
        r"\b(?:the\s+)?(report|document|assignment|notes?|spreadsheet|workbook|"
        r"letter|resume|cv|file)\b",
        low,
    )
    if not subject_match:
        return None

    source_match = re.search(
        rf"\b(?P<verb>created|made|modified|saved|written|dated|from|on)\s+(?:on\s+)?"
        rf"(?P<date>{_DATE_VALUE})\b",
        low,
    )
    if not source_match:
        source_match = re.search(
            rf"(?P<date>{_DATE_VALUE})\s+(?:report|document|assignment|notes?|file)\b",
            low,
        )
    if not source_match:
        return None

    copy_match = re.search(
        rf"\b(?:with|using|bearing|including|containing|dated|add|include)\s+"
        rf"(?:the\s+)?(?P<date>{_DATE_VALUE})\s+(?:date\s+)?"
        rf"(?:in|on|as|to)?\s*(?:its|the)?\s*(?:file\s*name|filename|name)?\b",
        low,
    )
    if not copy_match:
        copy_match = re.search(
            rf"(?P<date>{_DATE_VALUE})\s+(?:date\s+)?(?:in|on|as)\s+"
            rf"(?:its|the)?\s*(?:file\s*name|filename)",
            low,
        )
    if not copy_match:
        # "copy it with today's date" is an explicit request to date the copy.
        # Without an explicit copy date we fail closed instead of assuming today.
        return None
    source_phrase = _normalize_date_phrase(source_match.group("date"))
    copy_phrase = _normalize_date_phrase(copy_match.group("date"))
    try:
        source_date = date_slug(source_phrase)
        copy_date = date_slug(copy_phrase)
    except ValueError:
        return None

    source_verb = str(source_match.groupdict().get("verb") or "").casefold()
    source_field = ("created" if source_verb in {"created", "made", "written"}
                    else "modified" if source_verb in {"modified", "saved"}
                    else "any")
    open_copy = bool(re.search(r"\bopen\b", low)) and low.rfind("open") > low.find("copy")
    return RequestAnalysis(
        kind=WorkflowKind.DATED_FILE_COPY_OPEN,
        request=text,
        source_date=source_date,
        source_field=source_field,
        copy_date=copy_date,
        source_phrase=source_phrase,
        copy_phrase=copy_phrase,
        target_name=subject_match.group(1),
        open_copy=open_copy or bool(re.search(r"\bopen\s+(?:the\s+|its\s+|new\s+)?copy\b", low)),
        plan_owned=True,
    )


def _ranked_result_analysis(text: str, low: str) -> RequestAnalysis | None:
    # Import lazily: task_planner imports this package, and a module-level import
    # here would create a cycle during startup.
    from eva.automation.task_planner import _extract_ranked_search_task

    parsed = _extract_ranked_search_task(text)
    if not parsed:
        return None
    # The first-result workflow has its own plan/verifier. The ranked plan owns
    # only explicit non-first selections and playback requests.
    if int(parsed["index"]) <= 1 and not parsed["play"]:
        return None
    return RequestAnalysis(
        kind=WorkflowKind.RANKED_RESULT,
        request=text,
        engine=parsed["engine"],
        query=parsed["query"],
        index=int(parsed["index"]),
        play=bool(parsed["play"]),
        plan_owned=False,
    )


def _first_result_analysis(text: str, low: str) -> RequestAnalysis | None:
    if not re.search(
        r"\b(?:open|visit|navigate\s+to|go\s+to)\s+(?:the\s+)?(?:first|top|1st)\s+"
        r"(?:search\s+)?(?:result|link|page)\b",
        low,
    ):
        return None
    if not re.search(r"\b(?:search|look\s+up|find|google|youtube|bing|duckduckgo)\b", low):
        return None
    engine_match = re.search(r"\b(google|youtube|bing|duck\s*duck\s*go|duckduckgo)\b", low)
    engine = re.sub(r"\s+", "", engine_match.group(1)) if engine_match else ""
    query_match = re.search(
        r"\bsearch(?:\s+on)?\s+(?:the\s+)?(?:" + r"google|youtube|bing|duck\s*duck\s*go|duckduckgo" + r")?"
        r"\s*(?:for|about)?\s*(.+?)\s*(?:(?:and\s+then|then|and)\s+)?(?:please\s+)?"
        r"(?:open|visit|navigate\s+to|go\s+to)\s+(?:the\s+)?(?:first|top|1st)\s+"
        r"(?:search\s+)?(?:result|link|page)\b",
        text, re.I,
    )
    query = clean_topic(query_match.group(1)) if query_match else ""
    return RequestAnalysis(
        kind=WorkflowKind.FIRST_RESULT,
        request=text,
        engine=engine,
        query=query,
        index=1,
        plan_owned=False,
    )


def analyze_request(text: str) -> RequestAnalysis | None:
    """Return a high-confidence analysis, or ``None`` for legacy handling."""
    source = (text or "").strip()
    if not source:
        return None
    low = re.sub(r"\s+", " ", source.casefold())

    analysis = _wikipedia_summary_analysis(source, low)
    if analysis is not None:
        return analysis
    analysis = _python_downloads_analysis(source, low)
    if analysis is not None:
        return analysis
    analysis = _dated_file_analysis(source, low)
    if analysis is not None:
        return analysis
    analysis = _ranked_result_analysis(source, low)
    if analysis is not None:
        return analysis
    analysis = _first_result_analysis(source, low)
    if analysis is not None:
        return analysis
    return None


def analyze_request_dict(text: str) -> dict[str, Any] | None:
    """JSON-friendly wrapper used by diagnostics and reports."""
    analysis = analyze_request(text)
    return analysis.as_dict() if analysis is not None else None
