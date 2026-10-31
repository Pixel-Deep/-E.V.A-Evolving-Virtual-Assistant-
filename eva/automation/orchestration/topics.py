"""Canonical topic aliases shared by analysis, planning, and verification.

The mapping is intentionally tiny and evidence-based. A canonical URL is only a
locator: every summary still has to be grounded in page text actually returned by
the browser tool.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class KnownTopic:
    name: str
    url: str


_KNOWN_TOPICS: tuple[tuple[KnownTopic, tuple[str, ...]], ...] = (
    (
        KnownTopic("Jawaharlal Nehru", "https://en.wikipedia.org/wiki/Jawaharlal_Nehru"),
        (
            "jawaharlal nehru", "jawahar lal nehru", "pandit jawaharlal nehru",
            "nehru", "chacha nehru", "jawaharlal",
        ),
    ),
    (
        KnownTopic("A. P. J. Abdul Kalam", "https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam"),
        (
            "dr apj abdul kalam", "apj abdul kalam", "a p j abdul kalam",
            "drapkj abdul kalam", "dr a p j abdul kalam", "abdul kalam", "kalam",
        ),
    ),
    (
        KnownTopic("Internet service provider", "https://en.wikipedia.org/wiki/Internet_service_provider"),
        ("isp", "internet service provider", "internet service providers"),
    ),
)

# A stable public page for the Python downloads summary workflow.
PYTHON_DOWNLOADS_URL = "https://www.python.org/downloads/"


def _normalize_topic(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def find_known_topic(value: str) -> KnownTopic | None:
    normalized = _normalize_topic(value)
    if not normalized:
        return None
    for topic, aliases in _KNOWN_TOPICS:
        if normalized in aliases:
            return topic
    return None


def _alias_pattern(alias: str) -> str:
    words = re.findall(r"[a-z0-9]+", alias.casefold())
    return r"\b" + r"[\s.\-_]*".join(re.escape(word) for word in words) + r"\b"


def find_known_topic_in_text(text: str) -> KnownTopic | None:
    """Find the longest canonical alias occurring in free text.

    Longest-first matching prevents ``abdul kalam`` from winning over
    ``dr apj abdul kalam`` and keeps the canonical name stable for planning and
    verification. Matching ignores punctuation between initials.
    """
    source = (text or "").replace("\u2019", "'")
    candidates: list[tuple[int, KnownTopic]] = []
    for topic, aliases in _KNOWN_TOPICS:
        for alias in aliases:
            pattern = _alias_pattern(alias)
            match = re.search(pattern, source, re.I)
            if match:
                candidates.append((len(match.group(0)), topic))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]



def canonical_url_for(value: str) -> str | None:
    topic = find_known_topic(value)
    return topic.url if topic is not None else None


# Clause boundaries after which a Wikipedia topic description stops being the
# topic. These are action/deliverable words that commonly follow "on Wikipedia".
_TOPIC_END = (
    r"(?=\s*(?:,|;|\.\s|\.$|\band\b|\bthen\b|\bwritten\b|\btyped\b|\bput\b|"
    r"\bwrite\b|\btype\b|\bsave\b|\bsummari[sz]e\b|\bopen\b|\bread\b|$))"
)

_WIKIPEDIA_TOPIC_PATTERNS = (
    # "Find Jawaharlal Nehru's Wikipedia page"
    r"(?P<topic>[A-Za-z0-9][A-Za-z0-9 ._'\-]{1,80}?)['\u2019]s\s+wikipedia\s+(?:page|article|entry)" + _TOPIC_END,
    # "I need a brief Wikipedia-based summary of Jawaharlal Nehru"
    r"wikipedia(?:-|\s+)based\s+(?:summary|overview|information|article|page|entry)"
    r"\s+(?:of|about|on)\s+(?P<topic>[A-Za-z0-9][A-Za-z0-9 ._'\-]{1,100}?)" + _TOPIC_END,
    # "Search Wikipedia for Jawaharlal Nehru"
    r"\b(?:search|look\s+up|find|read|open|visit|research)\s+wikipedia\s+"
    r"(?:for|about|on)?\s*(?P<topic>[A-Za-z0-9][A-Za-z0-9 ._'\-]{1,100}?)" + _TOPIC_END,
    # "information about Nehru on Wikipedia"
    r"\b(?:information|details|facts|summary|article|page|entry)\s+(?:about|on|of)\s+"
    r"(?P<topic>[A-Za-z0-9][A-Za-z0-9 ._'\-]{1,100}?)\s+(?:on|from|in)\s+wikipedia\b",
    # "Open the Wikipedia page about artificial intelligence"
    r"wikipedia\s+(?:page|article|entry)\s+(?:about|on|for)\s+"
    r"(?P<topic>[A-Za-z0-9][A-Za-z0-9 ._'\-]{1,100}?)" + _TOPIC_END,
    # "Search about Jawaharlal Nehru on Wikipedia"
    r"\b(?:search|look\s+up|find|read|open|visit|research)\s+(?:about\s+|for\s+)?"
    r"(?P<topic>[A-Za-z0-9][A-Za-z0-9 ._'\-]{1,100}?)\s+(?:on|from|in)\s+wikipedia\b",
)


def clean_topic(value: str) -> str:
    """Return a bounded topic phrase without surrounding action clauses."""
    topic = re.sub(r"\s+", " ", (value or "").replace("\u2019", "'")).strip(" .,!?:;\"'`\u201c\u201d")
    topic = re.split(
        r"\s*(?:,|;|\band\b|\bthen\b|\bwritten\b|\btyped\b|\bput\b|\bwrite\b|"
        r"\btype\b|\bsave\b|\bsummari[sz]e\b|\bopen\b|\bread\b)\s*",
        topic, maxsplit=1, flags=re.I,
    )[0]
    topic = re.sub(r"\s+(?:on|from|in)\s+wikipedia.*$", "", topic, flags=re.I)
    # Possessive phrasing ("Find X's Wikipedia page") can otherwise leave the
    # leading action verb inside the extracted topic because the regex starts at
    # the beginning of the clause.
    topic = re.sub(r"^(?:find|search|look\s+up|read|open|visit|research)\s+", "", topic, flags=re.I)
    topic = re.sub(r"^(?:the|a|an|brief|short|quick|detailed)\s+", "", topic, flags=re.I)
    topic = re.sub(r"^(?:the|a|an|brief|short|quick|detailed)\s+", "", topic, flags=re.I)
    return topic.strip(" .,!?:;\"'`\u201c\u201d")


def extract_wikipedia_topic(text: str) -> str:
    """Extract the encyclopaedia topic from common Wikipedia request phrasing."""
    source = (text or "").replace("\u2019", "'")
    for pattern in _WIKIPEDIA_TOPIC_PATTERNS:
        match = re.search(pattern, source, re.I)
        if not match:
            continue
        topic = clean_topic(match.group("topic"))
        if 2 <= len(topic) <= 100:
            return topic
    return ""


def wikipedia_url_for(topic: str) -> str | None:
    """Return the canonical article URL when the topic is known, else ``None``."""
    return canonical_url_for(topic)
