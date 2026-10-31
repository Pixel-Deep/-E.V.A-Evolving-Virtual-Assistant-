"""Request-derived writing briefs and output-contract checks.

This module deliberately contains no genre-specific *content templates*. It only
extracts what the user requested so the writing provider cannot confuse an
operation such as "open Notepad" with the content to be written.
"""
from __future__ import annotations

import re
from typing import Any


_GENRES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.I), kind)
    for pattern, kind in [
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|brief\s+|simple\s+|small\s+|detailed\s+|long\s+|interesting\s+|informative\s+|educational\s+)?paragraph\b", "paragraph"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|brief\s+|simple\s+|detailed\s+|long\s+)?essay\b", "essay"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|brief\s+|simple\s+|funny\s+|rhyming\s+)?poem\b", "poem"),
        (r"^(?:a\s+|an\s+|one\s+)?short\s+story\b", "story"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|brief\s+|simple\s+|funny\s+)?story\b", "story"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|brief\s+|informative\s+)?article\b", "article"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|brief\s+|motivational\s+)?speech\b", "speech"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|brief\s+|slideshow\s+)?(?:presentation|slideshow|slide deck)\b", "presentation"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:formal\s+)?letter\b", "letter"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:(?:polite|respectful|formal|school)\s+)*(?:leave\s+|formal\s+|school\s+)?application\b", "application"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:formal\s+|professional\s+|thank[- ]you\s+)?e-?mail\b", "email"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|friendly\s+|thank[- ]you\s+)?message\b", "message"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|thank[- ]you\s+)?note\b", "note"),
        (r"^(?:a\s+|an\s+|one\s+)?reminder\b", "reminder"),
        (r"^(?:a\s+|an\s+|one\s+)?affirmation\b", "affirmation"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:motivational\s+)?quote\b", "quote"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:shopping|grocery|packing|to[- ]do|task)\s+list\b", "list"),
        (r"^(?:exactly\s+)?(?:one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})\s+(?:grocery|shopping|packing)\s+items?\b", "list"),
        (r"^(?:a\s+|an\s+|one\s+)?to[- ]do\s+list\b", "list"),
        (r"^(?:a\s+|an\s+|one\s+)?list\b", "list"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:packing\s+)?checklist\b", "checklist"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:study\s+)?(?:time)?table\b", "timetable"),
        (r"^(?:a\s+|an\s+|one\s+)?study\s+plan\b", "timetable"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|concise\s+|detailed\s+)?report\b", "report"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|concise\s+|five[- ]point\s+)?summary\b", "summary"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:short\s+|brief\s+|simple\s+|concise\s+|quick\s+)?(?:introduction|intro)\b", "paragraph"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:beginner'?s?\s+)?(?:guide|tutorial)\b", "article"),
        (r"^(?:a\s+|an\s+|one\s+)?comparison\b", "report"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:brief\s+|concise\s+|general\s+)?overview\b", "summary"),
        (r"^(?:a\s+|an\s+|one\s+)?(?:detailed\s+|brief\s+)?research\b", "report"),
    ]
)

_OPERATION = re.compile(r"\b(?:write|draft|compose|prepare|create|type|make|generate|research|summari[sz]e|compare|explain|save)\b\s+", re.I)
_CONTENT_OPERATION = re.compile(r"\b(?:write|draft|compose|prepare|create|type|make|generate|research|summari[sz]e|compare|explain)\b\s+", re.I)
_APP_CLAUSE = re.compile(
    r"\b(?:in|into|on|to)\s+(?:my\s+|the\s+)?(?:not\s*epad|note\s*pad|microsoft\s+word|word\s+document|desktop|documents|downloads)\b.*$",
    re.I,
)
_TRAILING_ACTION = re.compile(
    r"\b(?:and\s+(?:then\s+)?)?(?:then|after\s+that|afterwards)\s+(?:save|open|launch|show|display|create)\b.*$|"
    r"\b(?:and\s+)?(?:save|export)\s+(?:the\s+result|the\s+output|it|this|that)?\s*(?:as|to|into|on|in)\b.*$",
    re.I,
)
_STOP_WORDS = {
    "a", "an", "the", "about", "on", "of", "for", "to", "and", "or", "with",
    "in", "into", "my", "our", "your", "their", "this", "that", "some", "any",
    "please", "write", "paragraph", "essay", "poem", "story", "article", "speech",
    "i", "me", "my", "we", "our", "you", "he", "she", "they", "have", "has", "had",
    "am", "is", "are", "was", "were", "be", "been", "being", "because", "due", "reason",
}


def _clean(value: str) -> str:
    value = value.strip().strip(" \t\r\n\"'`.,;:!?—-")
    value = re.sub(r"\s+", " ", value)
    return value


def _normalize_common_transcription_errors(value: str) -> str:
    """Conservatively fix known speech/typing variants without network calls.

    Corrections are context-bound so a short ambiguous token is not globally
    rewritten. This is deliberately small and deterministic; uncertain terms are
    preserved rather than sent to an online spell-check API.
    """
    value = re.sub(r"\bbenifits\b", "benefits", value, flags=re.I)
    value = re.sub(r"\bcalulate\b", "calculate", value, flags=re.I)
    value = re.sub(r"\bschool\s+bou\b", "school boy", value, flags=re.I)
    value = re.sub(r"\bschool\s+boi\b", "school boy", value, flags=re.I)
    return value


def build_content_brief(request: str) -> dict[str, str]:
    """Extract the requested content genre, subject, and constraints.

    This is a request parser, not a content template library. It describes what
    the configured writing provider should generate and never stores canned
    paragraphs, letters, poems, or factual lists.
    """
    source = _normalize_common_transcription_errors(re.sub(r"\s+", " ", (request or "").strip()))
    # In file-oriented phrasing, the actual content request may be a noun clause
    # rather than a second verb: "create report.txt with a paragraph about X".
    # Extract only that declared content clause for the writer; the file planner
    # still uses the original command for destination/name/action semantics.
    embedded_content = re.search(
        r"\b(?:with|containing|include|including)\s+(?:the following\s+)?(?:(?:a|an|the|some|exact)\s+)?"
        r"(?:(?:short|brief|simple|informative|detailed|interesting|educational|funny|rhyming|concise)\s+)*"
        r"(?:paragraph|essay|poem|story|article|speech|letter|application|e-?mail|message|note|reminder|affirmation|quote|checklist|timetable|study plan|report|summary|(?:shopping|grocery|packing|to-?do) list|(?:benefits|advantages|disadvantages|uses|causes|effects|features|characteristics|reasons|steps|ways|tips)\s+(?:of|for|to)|importance\s+of)\b.*$",
        source, re.I,
    )
    if embedded_content:
        content_clause = source[embedded_content.start():]
        content_clause = re.sub(r"^\b(?:with|containing|include|including)\s+", "", content_clause, flags=re.I)
        content_clause = re.split(r"\b(?:on|in|to)\s+(?:my\s+)?(?:desktop|documents|downloads)\b", content_clause, maxsplit=1, flags=re.I)[0]
        content_clause = re.split(r"\b(?:and then|then|after that)\s+(?:open|launch|save|show|display)\b", content_clause, maxsplit=1, flags=re.I)[0]
        source = "write " + content_clause.strip()
    writing_surface_named = bool(re.search(
        r"\b(?:not\s*epad|note\s*pad|microsoft\s+word|word\s+document)\b", source, re.I
    ))
    if writing_surface_named:
        source = re.sub(r"\b(?:on|in|into)\s+(?:it|there)\b(?=\s*(?:[.!?]|$))", "", source, flags=re.I)

    # Normalize a known speech-to-text spelling variant in the parsed content
    # brief. The original user command is still retained by the caller/log.
    source = _normalize_common_transcription_errors(source)
    actions = list(_OPERATION.finditer(source))
    content_actions = list(_CONTENT_OPERATION.finditer(source))
    action = content_actions[0] if content_actions else (actions[0] if actions else None)
    for candidate in actions:
        candidate_tail = source[candidate.end():].strip()
        candidate_tail = re.sub(r"^(?:me\s+)?(?:a|an|the|some)\s+", "", candidate_tail, flags=re.I)
        candidate_tail = _APP_CLAUSE.sub("", candidate_tail)
        candidate_tail = _TRAILING_ACTION.sub("", candidate_tail)
        if (any(pattern.match(candidate_tail) for pattern, _ in _GENRES)
                or re.match(r"^(?:the\s+)?(?:benefits|advantages|disadvantages|uses|causes|effects|features|characteristics|reasons|steps|ways|tips|importance)\s+(?:of|for|to)\b", candidate_tail, re.I)):
            action = candidate
            break

    composition = source[action.end():].strip() if action else source
    composition = re.sub(r"^(?:me\s+)?(?:a|an|the|some)\s+", "", composition, flags=re.I)
    composition = _APP_CLAUSE.sub("", composition)
    composition = _TRAILING_ACTION.sub("", composition)
    composition = _clean(composition) or _clean(source)

    kind = "other"
    tail = composition
    topic = ""
    form_heading = ""

    heading_match = re.match(
        r"^(?:the\s+)?(benefits|advantages|disadvantages|uses|causes|effects|features|characteristics|reasons|steps|ways|tips)\s+(of|for|to)\s+(.+)$",
        composition, re.I,
    )
    importance_match = re.match(r"^(?:the\s+)?importance\s+of\s+(.+)$", composition, re.I)
    adv_match = re.search(r"\b(?:the\s+)?(?:advantages\s+and\s+disadvantages|pros\s+and\s+cons|benefits\s+and\s+drawbacks)\s+(?:of\s+)?(.+)$", composition, re.I)
    adv_prefix_match = re.search(r"^([A-Za-z0-9_.-]+)\s+(?:advantages\s+and\s+disadvantages|pros\s+and\s+cons)\b", composition, re.I)
    if heading_match:
        kind = "list"
        form_heading = heading_match.group(1).casefold()
        topic = _clean(heading_match.group(3))
        tail = topic
    elif importance_match:
        kind = "paragraph"
        form_heading = "importance"
        topic = _clean(importance_match.group(1))
        tail = topic
    elif adv_match:
        kind = "report"
        topic = _clean(adv_match.group(1))
        tail = topic
    elif adv_prefix_match:
        kind = "report"
        topic = _clean(adv_prefix_match.group(1))
        tail = topic
    else:
        for pattern, candidate_kind in _GENRES:
            match = pattern.match(composition)
            if match:
                kind = candidate_kind
                tail = composition[match.end():].strip()
                break

    tail = _APP_CLAUSE.sub("", tail)
    tail = _TRAILING_ACTION.sub("", tail)
    tail = _clean(tail)
    if not topic and kind in {"paragraph", "essay", "poem", "story", "article", "speech", "report", "summary"}:
        topic_match = re.match(r"^(?:about|on|regarding|concerning|to|of|between)\s+(.+)$", tail, re.I)
        if topic_match:
            topic = _clean(topic_match.group(1))
        elif re.match(r"^.+?\s+(?:and|versus|vs)\s+.+$", tail, re.I):
            topic = _clean(tail)
    elif not topic and kind in {"list", "checklist", "timetable"}:
        topic_match = re.match(r"^(?:for|about|on|regarding|of|to)\s+(.+)$", tail, re.I)
        if topic_match:
            topic = _clean(topic_match.group(1))

    if topic:
        topic = re.split(r"\b(?:in|into|on)\s+(?:my\s+)?(?:desktop|documents|downloads|notepad|note\s*pad|a\s+file)\b", topic, maxsplit=1, flags=re.I)[0]
        topic = re.split(r"\b(?:and\s+then|then|after\s+that|save\s+as|save\s+it\s+as)\b", topic, maxsplit=1, flags=re.I)[0]
        topic = re.split(r"\b(?:in|inside)\s+(?:it|the\s+file|this\s+file|that\s+file)\b.*$", topic, maxsplit=1, flags=re.I)[0]
        topic = _clean(re.sub(r"\s+(?:and|or|then)$", "", topic, flags=re.I))

    # The reason is a content constraint for formal correspondence, not an
    # invitation to invent an alternative explanation.
    if not topic and kind in {"letter", "application", "email", "message", "note", "reminder"}:
        reason_match = re.search(
            r"\b(?:because\s+of|because|due\s+to|owing\s+to|on\s+account\s+of|reason(?:\s+is)?\s*[:=]?)\s+(.+?)(?=[.!?;]|$)",
            source, re.I,
        )
        if reason_match:
            topic = _clean(reason_match.group(1))
            topic = re.split(r"\b(?:and\s+then|then|save\s+as|save\s+it\s+as|on\s+my\s+desktop|in\s+my\s+documents)\b", topic, maxsplit=1, flags=re.I)[0]
            topic = _clean(topic)

    # Routine school leave applications have a conventional genre and a neutral
    # fallback when no personal reason is supplied. Do not turn "polite leave
    # application for a student" into an underspecified generic writing prompt.
    is_leave_application = kind == "application" and bool(re.search(r"\bleave\s+application\b", composition, re.I))
    if is_leave_application:
        topic = "leave application"

    if kind == "list" and form_heading:
        prep = "of" if form_heading in {"benefits", "advantages", "disadvantages", "uses", "causes", "effects", "features", "characteristics", "reasons"} else "to"
        prompt = (
            f"Create an accurate, useful list of the {form_heading} {prep} {topic}. "
            f"Start with the heading '{form_heading.title()} of {topic}', then provide 4–6 distinct concise items, one per line. "
            "Each item must be specific to the requested subject. Do not write a letter, application, email, or unrelated paragraphs. "
            "Return only the heading and list items."
        )
    elif kind == "paragraph" and form_heading == "importance":
        prompt = (
            f"Write exactly one coherent informative paragraph about the importance of {topic}. "
            "Stay focused on the subject and do not turn it into a letter, application, email, or list. "
            "Return only the paragraph, without a heading or commentary."
        )
    elif kind == "paragraph":
        prompt = (
            f"Write exactly one coherent paragraph about {topic}. Use clear, natural language suitable for a general student reader. "
            "Stay focused on this topic. Do not write a letter, application, email, list, or unrelated content. "
            "Return only the paragraph, with no title, preface, or explanation."
        ) if topic else (
            f"Write exactly one coherent paragraph based on this requested subject: {composition}. "
            "Do not turn it into a letter, application, email, list, or another genre. Return only the paragraph."
        )
    elif kind == "poem" and not topic:
        prompt = (
            "Write a short, original poem on a simple, general everyday theme of your choice. "
            "The user requested a poem but did not specify its subject; choose a harmless, broadly relatable subject. "
            "Do not write a letter, application, email, or discuss the writing application. "
            "Return only the poem, without a title, preface, or commentary."
        )
    elif kind in {"essay", "poem", "story", "article", "speech", "report", "summary"}:
        prompt = (f"Write the requested {kind} about {topic}. Stay focused on that topic and use a suitable level of detail. Return only the {kind}, without commentary."
                  if topic else f"Write the requested {kind} based on this request: {composition}. Preserve the user's topic and constraints. Return only the {kind}, without commentary.")
    elif kind == "list":
        subject = f" about {topic}" if topic else f" matching this request: {composition}"
        prompt = f"Create a useful list{subject}. Preserve any explicit items, count, and ordering. Return only list items, one item per line, without unrelated letter/email text."
    elif kind == "checklist":
        subject = f" for {topic}" if topic else f" matching this request: {composition}"
        prompt = f"Create a practical checklist{subject}. Include concrete, task-relevant checkable items in a useful order. Do not substitute a generic article or letter. Return only the checklist."
    elif kind == "timetable":
        subject = f" for {topic}" if topic else f" matching this request: {composition}"
        prompt = f"Create a clear, usable timetable{subject}. Use a table-like plain-text layout with labels and time slots where relevant. Mark invented schedules as samples, not the user's real schedule. Return only the timetable."
    elif is_leave_application:
        prompt = (
            "Write a complete, polite school leave application from a student to their teacher or principal. "
            "The user did not supply a specific reason, duration, date, or personal name: do not invent any of those. "
            "Use the neutral phrase 'for personal reasons' for the reason, and use clear placeholders only for the student's name, class, and date where needed. "
            "Include a suitable subject, respectful greeting, concise request, and respectful closing. "
            "Return only the finished leave application; do not ask the user for more details."
        )
    elif kind in {"letter", "application", "email", "message", "note", "reminder", "affirmation", "quote", "checklist", "timetable"}:
        prompt = (
            f"Create the requested {kind} described here: {composition}. Follow its purpose, recipients, topic, and every explicit detail. "
            f"Do not replace the requested kind of content with another genre. Return only the finished {kind}, without commentary."
        )
    else:
        # A natural "write about X" prompt without an explicit genre is most
        # usefully interpreted as a short informational paragraph. It is not
        # forced into an unrelated template and remains topic-bound.
        generic_about = re.match(r"^(?:about|on|regarding|concerning)\s+(.+)$", composition, re.I)
        if generic_about:
            topic = _clean(generic_about.group(1))
            kind = "paragraph"
            prompt = f"Write exactly one concise informative paragraph about {topic}. Stay on topic; do not turn it into correspondence. Return only the paragraph."
        else:
            prompt = (
                f"Fulfil this writing request exactly: {composition}. Keep the requested topic, content type, and stated details. "
                "Do not transform it into a different kind of document. Return only the finished content, without commentary."
            )

    style = {
        "paragraph": "one focused factual paragraph; clear, natural, student-friendly language",
        "essay": "essay matching the requested topic and detail level",
        "poem": "poem matching the requested topic and tone",
        "story": "story matching the requested topic and tone",
        "article": "article matching the requested subject",
        "speech": "speech matching the stated audience and subject",
        "timetable": "practical timetable respecting explicitly requested times and priorities",
        "checklist": "useful checklist matching the requested task",
        "list": "list matching the requested topic and preserving supplied items",
    }.get(kind, "clear, natural writing matching the request")
    return {"prompt": prompt, "output_kind": kind, "topic": topic, "style": style,
            "composition": composition, "form_heading": form_heading}

def validate_generated_content(text: str, output_kind: str = "", expected_topic: str = "", expected_count: int | None = None) -> str | None:
    """Return a mismatch message when generated text violates an explicit brief.

    This is a fail-closed guard: invalid content is never passed to a desktop or
    filesystem step. It intentionally checks clear contract violations rather
    than trying to judge style or factual correctness with another model call.
    """
    value = (text or "").strip()
    if not value:
        return "The writing provider returned empty content."
    # A frequent local-model failure is a conversational clarification request
    # being returned as if it were the requested artifact. Reject it before it can
    # be pasted into Notepad, copied to the clipboard, or written to a file.
    refusal_start = re.match(
        r"^\s*(?:i(?:'m| am)\s+sorry|i\s+apologize|i\s+(?:can(?:'t|not)|am\s+unable\s+to)\s+(?:assist|help|fulfill|provide|complete)|as\s+an\s+ai)\b",
        value, re.I
    )
    clarification_start = re.match(
        r"^(?:sure[,! ]*\s*)?(?:please\s+provide|provide\s+me\s+with|could\s+you\s+(?:please\s+)?(?:provide|clarify|tell)|"
        r"can\s+you\s+(?:please\s+)?(?:provide|clarify|tell)|tell\s+me\s+(?:what|the)|"
        r"i\s+(?:need|require)\s+(?:more\s+)?(?:details|instructions|information)|"
        r"what\s+exactly\s+(?:would|do)\s+you\s+like)", value, re.I
    )
    clarification_anywhere = re.search(
        r"(?:i(?:'m| am) sorry.{0,180}(?:need more information|cannot fulfill|cannot help|cannot assist|can't assist|can't help|unable to|who .{0,30} refers|provide more context)|"
        r"could you (?:please )?(?:specify|provide more context|give me more context)|"
        r"without additional information.{0,180}(?:who|what|details)|"
        r"please let me know how i can assist)", value[:900], re.I | re.S
    )
    meta_instruction = re.search(
        r"(?:when asked.{0,100}respond|when asked.{0,100}say|message draft is now available on your clipboard|"
        r"here(?:'s| is) (?:the )?(?:message|draft)\s*:\s*)", value, re.I
    )
    if refusal_start or clarification_start or clarification_anywhere or meta_instruction:
        return "The writing provider returned a clarification prompt, refusal, or assistant-side instruction instead of only the requested content. No text should be handed to a destination."
    kind = (output_kind or "").strip().casefold()
    if kind == "summary":
        # Research models sometimes return the webpage's navigation/infobox verbatim
        # and nevertheless satisfy the topic-token check. That output must never be
        # typed into Notepad or saved as if it were a summary; the caller will use a
        # clean source-grounded fallback or stop without writing anything.
        page_shell_markers = (
            r"\bjump to content\b", r"\bskip to content\b", r"\bsearch search\b",
            r"\b\d{1,3}\s+languages\b", r"\bmain headings:\s*\n",
            r"\buseful source excerpts:\b", r"\bsource excerpt:", r"\bsource urls:\s*\n",
            r"\bfor other uses, see\b", r"\bpersonal details\b.{0,180}\b(?:born|died|resting place)\b",
            r"\bofficial portrait\b.{0,180}\bin office\b", r"\bno readable source excerpts\b",
        )
        if any(re.search(pattern, value, re.I | re.S) for pattern in page_shell_markers):
            return "The generated summary appears to contain webpage navigation, infobox metadata, or extraction diagnostics instead of an actual summary. No content was sent to the destination."
    if kind == "paragraph":
        if re.match(r"^(?:dear\b|subject\s*:|to whom it may concern\b|respected (?:sir|madam)\b)", value, re.I) or re.search(
            r"\b(?:yours faithfully|yours sincerely|school leave application|request for leave)\b", value, re.I
        ):
            return "The generated content looks like a letter/application, but the request requires a paragraph. No content was sent to the destination."
        paragraphs = [part for part in re.split(r"\n\s*\n", value) if part.strip()]
        if len(paragraphs) > 1:
            return "The generated content contains multiple paragraphs, but the request requires one paragraph. No content was sent to the destination."
    if kind in {"paragraph", "essay", "poem", "story", "article", "speech", "list", "checklist", "timetable", "report", "summary"}:
        if re.match(r"^(?:dear\b|subject\s*:|to whom it may concern\b|respected (?:sir|madam)\b)", value, re.I):
            return f"The generated content looks like correspondence, but the request requires {kind} content. No content was sent to the destination."
    if kind == "list":
        list_lines = [line.strip() for line in value.splitlines() if line.strip()]
        if len(list_lines) < 2:
            return "The generated list did not contain at least two separate list items. No content was sent to the destination."
        if expected_count is not None and len(list_lines) != expected_count:
            return f"The generated list contained {len(list_lines)} items, but exactly {expected_count} were requested. No content was sent to the destination."
    if kind == "presentation":
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if len(lines) < 2:
            return "The generated presentation must contain a title and at least one slide section. No file was created."
        if not re.search(r"(?m)^\s*---SLIDE---\s*$", value):
            return "The generated presentation omitted the required ---SLIDE--- delimiters, so slide boundaries cannot be verified. No file was created."
        # The first non-empty line is presentation metadata/title. Everything after
        # it is split into actual slide sections by the explicit delimiter.
        first_line_end = value.find(lines[0]) + len(lines[0])
        slide_body = value[first_line_end:]
        sections = [part for part in re.split(r"(?m)^\s*---SLIDE---\s*$", slide_body) if part.strip()]
        if expected_count is not None and len(sections) != expected_count:
            return f"The generated presentation contained {len(sections)} slide sections, but exactly {expected_count} were requested. No file was created."
    if expected_topic:
        tokens = [token.casefold() for token in re.findall(r"[A-Za-z0-9]+", expected_topic)
                  if token.casefold() not in _STOP_WORDS and len(token) > 1]
        if tokens:
            rendered = " " + " ".join(re.findall(r"[A-Za-z0-9]+", value.casefold())) + " "
            # Require at least one salient topic term. For a one-word topic (e.g. cow),
            # this prevents a completely unrelated letter/draft from being accepted.
            variants = set(tokens)
            variants.update(token + "s" for token in tokens)
            matched = {token for token in variants if re.search(r"\b" + re.escape(token) + r"\b", rendered)}
            # Treat common closed compounds as equivalent to their explicitly
            # requested component words (e.g. schoolboy vs. school boy), without
            # relaxing topical validation for unrelated content.
            compact_rendered = "".join(re.findall(r"[a-z0-9]+", value.casefold()))
            if len(tokens) > 1 and "".join(tokens) in compact_rendered:
                matched.update(tokens)
            for token in tokens:
                if token in compact_rendered:
                    matched.add(token)
            synonyms = {
                "cybersecurity": ["cyber", "security", "infosec"],
                "ai": ["artificial", "intelligence"],
                "ml": ["machine", "learning"],
                "dev": ["development", "developer"],
            }
            for token in tokens:
                for syn in synonyms.get(token, []):
                    if re.search(r"\b" + re.escape(syn) + r"\b", rendered):
                        matched.add(token)
            required_hits = len(tokens) if len(tokens) <= 2 else max(2, len(tokens) - 1)
            if len(matched) < required_hits:
                return "The generated content does not sufficiently match the requested topic or reason. No content was sent to the destination."
    return None
