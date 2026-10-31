"""Objective-level verification for EVA's analyzed compound workflows.

A tool's ``ok`` flag means the call returned without an exception. This module
checks the requested outcome instead: was a real source page read, did the
summary contain the requested topic, was Notepad text read back exactly, was the
selected search result actually opened, and does a dated copy exist with the
same bytes/digest as its source?

Launch-only actions are reported separately. An accepted OS launch is allowed to
have ``ok=True`` and ``verified=False``; file existence, browser navigation,
typed-text readback, and copy hashes are strict proof points.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from eva.automation.orchestration.models import (
    OutcomeCheck,
    RequestAnalysis,
    VerificationReport,
    WorkflowKind,
)
from eva.automation.orchestration.topics import clean_topic

_LAUNCH_TOOLS = {
    "desktop.open_target",
    "desktop.open_url",
    "desktop.open_file",
    "desktop.open_text_file",
}
_OPEN_COPY_TOOLS = {"desktop.open_file", "desktop.open_text_file"}
_MAX_HASH_BYTES = 134_217_728  # Independent hash checks stay bounded at 128 MiB.
_TOPIC_STOP = {
    "a", "an", "the", "of", "and", "or", "on", "in", "for", "to", "from",
    "dr", "mr", "mrs", "ms", "p", "j",
}


def _steps_by_id(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for step in (payload or {}).get("steps", []) or []:
        if isinstance(step, dict):
            result[str(step.get("id") or "")] = step
    return result


def _find_step(steps: list[dict[str, Any]], tool: str,
               step_id: str = "") -> dict[str, Any] | None:
    if step_id:
        for step in steps:
            if str(step.get("id") or "") == step_id:
                return step
    for step in steps:
        if str(step.get("tool") or "") == tool:
            return step
    return None


def _result(step: dict[str, Any] | None) -> dict[str, Any]:
    value = (step or {}).get("result", {})
    return value if isinstance(value, dict) else {}


def _data(step: dict[str, Any] | None) -> dict[str, Any]:
    value = _result(step).get("data", {})
    return value if isinstance(value, dict) else {}


def _step_ok(step: dict[str, Any] | None) -> bool:
    return bool(step and step.get("status") == "succeeded" and _result(step).get("ok") is True)


def _step_verified(step: dict[str, Any] | None) -> bool:
    return bool(_step_ok(step) and _result(step).get("verified") is True)


def _normalize_url(value: Any) -> str:
    raw = unquote(str(value or "")).strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    host = (parts.hostname or "").casefold().removeprefix("www.")
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{query}"


def _topic_tokens(topic: str) -> list[str]:
    return [
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9]+", topic or "")
        if len(token) > 1 and token.casefold() not in _TOPIC_STOP
    ]


def _has_topic(value: str, topic: str) -> bool:
    tokens = _topic_tokens(topic)
    if not tokens:
        return True
    rendered = " ".join(re.findall(r"[A-Za-z0-9]+", (value or "").casefold()))
    return any(re.search(r"\b" + re.escape(token) + r"\b", rendered) for token in tokens)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _created_epoch(stat: Any) -> int | None:
    birth = getattr(stat, "st_birthtime", None)
    if birth is not None:
        return int(birth)
    if hasattr(stat, "st_ctime"):
        return int(stat.st_ctime)
    return None


def _calendar_date(epoch: int | None) -> str:
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(epoch).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _check(name: str, step: dict[str, Any] | None, *,
           strict: bool = True, launch_only: bool = False,
           detail: str = "") -> OutcomeCheck:
    ok = _step_ok(step)
    verified = _step_verified(step)
    if not step:
        detail = detail or "The required step is missing from the execution report."
    elif not ok:
        detail = detail or str(_result(step).get("message") or "The required step failed.")
    elif strict and not verified:
        detail = detail or "The tool returned success without independent verification."
    return OutcomeCheck(
        name=name,
        step_id=str((step or {}).get("id") or ""),
        tool=str((step or {}).get("tool") or ""),
        ok=ok,
        verified=verified,
        launch_only=launch_only,
        detail=detail,
    )


def _browse_source_check(step: dict[str, Any] | None, analysis: RequestAnalysis) -> tuple[OutcomeCheck, str]:
    check = _check("source_page_read", step)
    if not check.ok:
        return check, check.detail or "The requested source page was not read."
    data = _data(step)
    pages = [page for page in data.get("pages", []) or [] if isinstance(page, dict)]
    research_text = str(data.get("research_text") or "")
    readable = [page for page in pages if str(page.get("url") or "").strip()
                and len(str(page.get("text") or "").strip()) >= 20]
    if not readable or len(research_text.strip()) < 40:
        check.ok = False
        check.verified = False
        check.detail = "Browser success was reported without substantive page text and a source URL."
        return check, check.detail
    if not check.verified:
        check.detail = "A page was returned, but the browser did not mark the observation verified."
        return check, check.detail
    if analysis.canonical_url:
        expected = _normalize_url(analysis.canonical_url)
        observed = {_normalize_url(page.get("url")) for page in readable}
        if expected not in observed:
            check.ok = False
            check.verified = False
            check.detail = (f"The page actually read was not the expected source. Expected "
                            f"{analysis.canonical_url}; observed {sorted(observed)[:3]}.")
            return check, check.detail
    check.detail = f"Read {len(readable)} source page(s) with visible text and URL evidence."
    return check, check.detail


def _extractive_fallback_summary(source_text: str, topic: str, max_sentences: int = 3) -> str:
    """Extract topic-relevant, factual sentences directly from source text."""
    if not source_text:
        return ""
    paragraphs = [p.strip() for p in source_text.split("\n\n") if p.strip()]
    topic_clean = clean_topic(topic).lower() if topic else ""
    topic_tokens = set(re.findall(r"[a-z0-9]+", topic_clean)) - _TOPIC_STOP
    scored_sentences: list[tuple[int, str]] = []
    for para in paragraphs:
        for sent in re.split(r"(?<=[.!?])\s+", para):
            s = sent.strip()
            if 30 <= len(s) <= 280:
                words = set(re.findall(r"[a-z0-9]+", s.lower()))
                overlap = len(words & topic_tokens)
                if overlap > 0:
                    scored_sentences.append((overlap, s))
    scored_sentences.sort(key=lambda x: x[0], reverse=True)
    selected = [s for _, s in scored_sentences[:max_sentences]]
    return " ".join(selected) if selected else (source_text[:300].strip() + "...")


def _summary_check(step: dict[str, Any] | None, analysis: RequestAnalysis,
                   plan_steps: list[dict[str, Any]],
                   source_step: dict[str, Any] | None = None) -> tuple[OutcomeCheck, str]:
    check = _check("summary_generated", step)
    if not check.ok:
        return check, check.detail or "No summary was generated."
    data = _data(step)
    text = str(data.get("text") or "").strip()
    if len(text) < 30:
        check.ok = False
        check.verified = False
        check.detail = "The generated summary was too short to be a usable result."
        return check, check.detail
    if not _has_topic(text, analysis.topic):
        check.ok = False
        check.verified = False
        check.detail = f"The generated summary did not mention the requested topic ({analysis.topic})."
        return check, check.detail
    if not check.verified:
        check.detail = "A summary was returned, but its content contract was not verified."
        return check, check.detail
    plan_summary = _find_step(plan_steps, "content.generate_text", "summary")
    if plan_summary is not None:
        prompt = str((plan_summary.get("args") or {}).get("prompt") or "")
        if "${steps.browse.data.research_text}" not in prompt:
            check.ok = False
            check.verified = False
            check.detail = "The plan's summary prompt was not grounded in actual browser output."
            return check, check.detail
        if "browse" not in (plan_summary.get("depends_on") or []):
            check.ok = False
            check.verified = False
            check.detail = "The summary step did not declare its dependency on the browser read."
            return check, check.detail

    # Evidence-based factual grounding check against actual source text
    source_data = _data(source_step) if source_step else {}
    source_text = str(source_data.get("research_text") or "")
    if not source_text and isinstance(source_data.get("pages"), list):
        source_text = "\n\n".join(str(p.get("text") or "") for p in source_data["pages"] if isinstance(p, dict))
    source_urls = [str(p.get("url") or "") for p in source_data.get("pages", []) if isinstance(p, dict) and p.get("url")]

    if source_text and len(source_text.strip()) > 50:
        source_lower = source_text.lower()
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 15]
        grounded_count = 0
        for sent in sentences:
            sent_lower = sent.lower()
            # Extract key factual units: numbers, dates, capitalized entity tokens, key non-stop words
            factual_tokens = [w for w in re.findall(r"\b[a-z0-9]{4,}\b", sent_lower) if w not in _TOPIC_STOP]
            numbers = re.findall(r"\b\d+(?:,\d+)*(?:\.\d+)?\b", sent)
            if numbers:
                # If sentence mentions numbers/dates, ensure at least one number matches source
                has_num_support = any(num in source_text for num in numbers)
                num_overlap = sum(1 for tok in factual_tokens if tok in source_lower)
                if has_num_support or (factual_tokens and num_overlap / len(factual_tokens) >= 0.4):
                    grounded_count += 1
            else:
                tok_overlap = sum(1 for tok in factual_tokens if tok in source_lower)
                if factual_tokens and (tok_overlap / len(factual_tokens) >= 0.35):
                    grounded_count += 1

        grounding_score = grounded_count / max(1, len(sentences))
        factually_grounded = grounding_score >= 0.4
        data["handoff_verified"] = True
        data["factually_grounded"] = factually_grounded
        data["grounding_score"] = round(grounding_score, 2)
        data["source_urls"] = source_urls

        if not factually_grounded:
            check.verified = False
            fallback = _extractive_fallback_summary(source_text, analysis.topic)
            data["extractive_fallback"] = fallback
            check.detail = (
                f"Data handoff succeeded, but factual grounding was weak ({grounding_score:.0%}). "
                "One or more claims lack direct evidence in source text; extractive fallback prepared."
            )
            return check, check.detail

    check.detail = "The summary is non-empty, topic-relevant, and evidenced by the source text."
    return check, check.detail


def _typed_notepad_check(step: dict[str, Any] | None,
                         plan_steps: list[dict[str, Any]]) -> tuple[OutcomeCheck, str]:
    check = _check("summary_typed_and_read_back", step)
    if not check.ok:
        return check, check.detail or "The summary was not typed into Notepad."
    readback = _data(step).get("readback")
    if isinstance(readback, dict) and readback.get("verified") is False:
        check.ok = False
        check.verified = False
        check.detail = str(readback.get("message") or "Notepad read-back did not match the summary.")
        return check, check.detail
    if not check.verified:
        check.detail = "Notepad accepted the text, but no exact read-back verified it."
        return check, check.detail
    plan_type = _find_step(plan_steps, "desktop.type_notepad", "type_summary")
    if plan_type is not None:
        value = (plan_type.get("args") or {}).get("value")
        if value != "${steps.summary.data.text}":
            check.ok = False
            check.verified = False
            check.detail = "The typed value was not the generated summary hand-off."
            return check, check.detail
    check.detail = "Notepad text was read back and exactly matched the generated summary."
    return check, check.detail


def _verify_web_summary(analysis: RequestAnalysis, plan_steps: list[dict[str, Any]],
                        executed: list[dict[str, Any]]) -> VerificationReport:
    checks: list[OutcomeCheck] = []
    issues: list[str] = []

    browse = _find_step(executed, "web.browse", "browse")
    source_check, source_detail = _browse_source_check(browse, analysis)
    checks.append(source_check)

    summary = _find_step(executed, "content.generate_text", "summary")
    summary_check, summary_detail = _summary_check(summary, analysis, plan_steps, source_step=browse)
    checks.append(summary_check)

    launch = _find_step(executed, "desktop.open_target", "open_notepad")
    launch_check = _check("notepad_launch_requested", launch, strict=False, launch_only=True)
    checks.append(launch_check)

    prepared = _find_step(executed, "desktop.new_notepad_document", "new_notepad_document")
    prepared_check = _check("document_prepared", prepared, strict=False)
    if prepared_check.ok and not prepared_check.verified:
        prepared_check.detail = "The blank-document state was not independently verified; exact typed-text readback is the deliverable proof."
    checks.append(prepared_check)

    typed = _find_step(executed, "desktop.type_notepad", "type_summary")
    typed_check, typed_detail = _typed_notepad_check(typed, plan_steps)
    checks.append(typed_check)

    for check, detail in ((source_check, source_detail), (summary_check, summary_detail),
                          (launch_check, launch_check.detail), (prepared_check, prepared_check.detail),
                          (typed_check, typed_detail)):
        if not check.ok:
            issues.append(f"{check.name}: {detail}")
    return _report(checks, issues, executed)


def _verify_ranked(analysis: RequestAnalysis, plan_steps: list[dict[str, Any]],
                   executed: list[dict[str, Any]]) -> VerificationReport:
    checks: list[OutcomeCheck] = []
    issues: list[str] = []

    navigation = _find_step(executed, "browser.navigate", "open_search_results")
    nav_check = _check("search_results_opened", navigation)
    if nav_check.ok:
        data = _data(navigation)
        if not str(data.get("url") or "").strip():
            nav_check.ok = False
            nav_check.verified = False
            nav_check.detail = "Navigation returned without an observed final URL."
    checks.append(nav_check)

    opened = _find_step(executed, "browser.open_result", "open_ranked_result")
    opened_check = _check("ranked_result_opened", opened)
    if opened_check.ok:
        data = _data(opened)
        index = data.get("result_index")
        if index != analysis.index:
            opened_check.ok = False
            opened_check.verified = False
            opened_check.detail = f"The opened result index was {index!r}, not {analysis.index}."
        elif not str(data.get("opened_url") or "").strip():
            opened_check.ok = False
            opened_check.verified = False
            opened_check.detail = "The result tool did not report the final opened URL."
        elif analysis.play:
            playback = data.get("playback_state")
            if not isinstance(playback, dict) or playback.get("playing") is not True:
                opened_check.ok = False
                opened_check.verified = False
                opened_check.detail = "Video playback was requested but not confirmed as playing."
            elif not opened_check.verified:
                opened_check.ok = False
                opened_check.verified = False
                opened_check.detail = "The result page opened, but playback verification was not marked verified."
    checks.append(opened_check)

    for check in checks:
        if not check.ok:
            issues.append(f"{check.name}: {check.detail}")
    return _report(checks, issues, executed)


def _verify_first_result(analysis: RequestAnalysis, plan_steps: list[dict[str, Any]],
                         executed: list[dict[str, Any]]) -> VerificationReport:
    checks: list[OutcomeCheck] = []
    issues: list[str] = []

    first = _find_step(executed, "browser.open_first_result", "open_first_result")
    if first is not None:
        navigation = _find_step(executed, "browser.navigate", "open_search_results")
        nav_check = _check("search_results_opened", navigation)
        checks.append(nav_check)
        first_check = _check("first_result_opened", first)
        if first_check.ok:
            data = _data(first)
            index = data.get("result_index")
            if index not in (None, 1):
                first_check.ok = False
                first_check.verified = False
                first_check.detail = f"The opened result index was {index!r}, not the first result."
            elif not str(data.get("opened_url") or "").strip():
                first_check.ok = False
                first_check.verified = False
                first_check.detail = "The first-result tool did not report the final opened URL."
        checks.append(first_check)
    else:
        browse = _find_step(executed, "web.browse", "browse")
        source_check, source_detail = _browse_source_check(browse, analysis)
        if source_check.ok:
            data = _data(browse)
            results = [item for item in data.get("results", []) or [] if isinstance(item, dict)]
            pages = [item for item in data.get("pages", []) or [] if isinstance(item, dict)]
            if not results:
                source_check.ok = False
                source_check.verified = False
                source_check.detail = "The single-page browse returned no ranked search results."
            elif not pages:
                source_check.ok = False
                source_check.verified = False
                source_check.detail = "No result page was actually read."
            else:
                first_url = _normalize_url(results[0].get("url"))
                opened_url = _normalize_url(pages[0].get("url"))
                if not first_url or first_url != opened_url:
                    source_check.ok = False
                    source_check.verified = False
                    source_check.detail = (f"The page read ({opened_url or 'unknown'}) was not the first "
                                           f"search result ({first_url or 'unknown'}).")
        checks.append(source_check)

    for check in checks:
        if not check.ok:
            issues.append(f"{check.name}: {check.detail}")
    return _report(checks, issues, executed)


def _file_date(path: Path, field: str) -> tuple[str, str]:
    try:
        stat = path.stat()
    except OSError:
        return "", ""
    modified = _calendar_date(int(stat.st_mtime))
    created_epoch = _created_epoch(stat)
    created = _calendar_date(created_epoch)
    if field == "modified":
        return modified, created
    if field == "created":
        return created, modified
    return modified, created


def _verify_dated_copy(analysis: RequestAnalysis, plan_steps: list[dict[str, Any]],
                       executed: list[dict[str, Any]]) -> VerificationReport:
    checks: list[OutcomeCheck] = []
    issues: list[str] = []

    find_step = _find_step(executed, "files.find_by_date", "find_report")
    if find_step is not None:
        find_check = _check("dated_source_found", find_step)
        if find_check.ok:
            data = _data(find_step)
            raw_path = str(data.get("path") or "").strip()
            if not raw_path:
                find_check.ok = False
                find_check.verified = False
                find_check.detail = "The date search returned no file path."
            else:
                path = Path(raw_path)
                if not path.is_file():
                    find_check.ok = False
                    find_check.verified = False
                    find_check.detail = "The reported source file no longer exists."
                else:
                    modified, created = _file_date(path, analysis.source_field)
                    matched = {
                        "modified": modified == analysis.source_date,
                        "created": created == analysis.source_date,
                        "any": analysis.source_date in {modified, created},
                    }[analysis.source_field]
                    if not matched:
                        find_check.ok = False
                        find_check.verified = False
                        find_check.detail = (
                            f"The source date did not match {analysis.source_date} "
                            f"({analysis.source_field}; modified={modified or 'unknown'}, "
                            f"created={created or 'unknown'})."
                        )
                    else:
                        find_check.detail = f"Exactly one dated source matched local date {analysis.source_date}."
        checks.append(find_check)

    copy_step = _find_step(executed, "files.copy_with_date", "copy_report")
    copy_check = _check("dated_copy_created", copy_step)
    if copy_check.ok:
        data = _data(copy_step)
        source = str(data.get("source") or "").strip()
        destination = str(data.get("path") or data.get("destination") or "").strip()
        if not source or not destination:
            copy_check.ok = False
            copy_check.verified = False
            copy_check.detail = "The copy result did not report both source and destination paths."
        else:
            source_path, destination_path = Path(source), Path(destination)
            if source_path == destination_path:
                copy_check.ok = False
                copy_check.verified = False
                copy_check.detail = "The copy destination is the same as the source."
            elif not destination_path.is_file():
                copy_check.ok = False
                copy_check.verified = False
                copy_check.detail = "The reported copy does not exist on disk."
            elif analysis.copy_date not in destination_path.name:
                copy_check.ok = False
                copy_check.verified = False
                copy_check.detail = f"The copy filename does not contain the requested date {analysis.copy_date}."
            else:
                try:
                    size = source_path.stat().st_size
                except OSError:
                    size = -1
                if size >= 0 and size <= _MAX_HASH_BYTES:
                    try:
                        source_hash = _hash_file(source_path)
                        destination_hash = _hash_file(destination_path)
                    except OSError:
                        source_hash = destination_hash = ""
                    if not source_hash or source_hash != destination_hash:
                        copy_check.ok = False
                        copy_check.verified = False
                        copy_check.detail = "The copied file's SHA-256 digest does not match the source."
                    else:
                        copy_check.detail = "The copy exists with the requested date in its name and an exact SHA-256 match."
                else:
                    copy_check.detail = "The copy exists with the requested date in its name; hash comparison was skipped because the file exceeded the independent-check bound."
        # A tool may return a hash even when independent hashing was skipped.
        reported_hash = str(data.get("sha256") or "")
        reported_source_hash = str(data.get("source_sha256") or "")
        if copy_check.ok and reported_hash and reported_source_hash and reported_hash != reported_source_hash:
            copy_check.ok = False
            copy_check.verified = False
            copy_check.detail = "The copy tool reported different source and destination digests."
    checks.append(copy_check)

    if analysis.open_copy:
        open_step = next((step for step in executed
                          if str(step.get("tool") or "") in _OPEN_COPY_TOOLS), None)
        open_check = _check("copy_open_requested", open_step, strict=False, launch_only=True)
        checks.append(open_check)

    for check in checks:
        if not check.ok:
            issues.append(f"{check.name}: {check.detail}")
    return _report(checks, issues, executed)


def _report(checks: list[OutcomeCheck], issues: list[str],
            executed: list[dict[str, Any]]) -> VerificationReport:
    execution_ok = True
    for step in executed:
        status = str(step.get("status") or "")
        if status and status not in {"succeeded", "missing_capability", "rejected_before_execution"}:
            execution_ok = False
            break
    required = [check for check in checks if not check.launch_only and check.name != "document_prepared"]
    unverified_steps: list[str] = []
    for check in required:
        if check.ok and not check.verified:
            unverified_steps.append(check.step_id or check.tool or check.name)
    launch_only_steps = [
        check.step_id or check.tool or check.name
        for check in checks
        if check.launch_only and check.ok
    ]
    required_ok = all(check.ok for check in checks)
    required_verified = all(check.verified for check in required)
    ok = bool(execution_ok and required_ok and required_verified)
    if not execution_ok and not issues:
        issues = ["The workflow execution did not complete successfully."]
    if unverified_steps and not issues:
        issues = ["Required outcome(s) completed without independent verification: "
                  + ", ".join(unverified_steps) + "."]
    verified = bool(ok and not unverified_steps)
    return VerificationReport(
        ok=ok,
        verified=verified,
        checks=checks,
        issues=issues,
        unverified_steps=unverified_steps,
        launch_only_steps=launch_only_steps,
    )


def verify_execution(analysis: RequestAnalysis | None,
                     plan_steps: list[dict[str, Any]] | None,
                     execution_payload: dict[str, Any] | None) -> VerificationReport | None:
    """Verify an executed workflow against the original analyzed objective."""
    if analysis is None:
        return None
    plan = [step for step in (plan_steps or []) if isinstance(step, dict)]
    executed = _steps_by_id(execution_payload)
    ordered = [executed[key] for key in executed]

    if analysis.kind in {WorkflowKind.WEB_SUMMARY_NOTEPAD, WorkflowKind.PYTHON_DOWNLOADS_SUMMARY}:
        return _verify_web_summary(analysis, plan, ordered)
    if analysis.kind == WorkflowKind.RANKED_RESULT:
        return _verify_ranked(analysis, plan, ordered)
    if analysis.kind == WorkflowKind.FIRST_RESULT:
        return _verify_first_result(analysis, plan, ordered)
    if analysis.kind == WorkflowKind.DATED_FILE_COPY_OPEN:
        return _verify_dated_copy(analysis, plan, ordered)
    return None


def evaluate_plan_coverage(
    request: str,
    steps: list[dict[str, Any]] | Any,
    gaps: list[dict[str, Any]] | Any = None,
) -> list[str]:
    """Unified pre-execution coverage check across compound clauses, deliverables, and outcomes.

    Fails closed when a proposed plan drops an explicit action clause, misses an
    intermediate step (such as opening a ranked search result), or omits an
    observable deliverable.
    """
    from eva.automation.agent_runtime import _plan_coverage_issues
    from eva.automation.task_planner import _request_outcome_coverage_issues

    issues: list[str] = []
    normalized_steps = [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []
    normalized_gaps: list[dict[str, Any]] = []
    if isinstance(gaps, list):
        for g in gaps:
            if isinstance(g, dict):
                normalized_gaps.append(g)
            elif isinstance(g, str):
                normalized_gaps.append({"name": g, "goal": g, "description": g})

    issues.extend(_plan_coverage_issues(request, normalized_steps, normalized_gaps))
    issues.extend(_request_outcome_coverage_issues(request, steps, gaps))

    seen: set[str] = set()
    deduped: list[str] = []
    for issue in issues:
        clean = issue.strip()
        if clean and clean not in seen:
            seen.add(clean)
            deduped.append(clean)
    return deduped
