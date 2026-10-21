
"""Lightweight web research for EVA using DuckDuckGo HTML."""

import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
)

TIMEOUT_SECONDS = 12
MAX_QUERY_LENGTH = 300
MAX_RESULTS_LIMIT = 5

# Domain preferences are applied only when relevant to the query.
OFFICIAL_DOMAINS = {
    "python": ["python.org"],
    "windows": ["microsoft.com"],
    "microsoft": ["microsoft.com"],
    "ollama": ["ollama.com", "github.com"],
    "openai": ["openai.com"],
    "india": ["india.gov.in", "pib.gov.in"],
}


class _SearchParser(HTMLParser):
    """Extract DuckDuckGo result titles, URLs, and snippets."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._title_result = None
        self._snippet_result = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()

        if tag == "a" and "result__a" in classes:
            self._title_result = {
                "title": "",
                "url": attrs.get("href", ""),
                "snippet": "",
            }
            self.results.append(self._title_result)

        if "result__snippet" in classes and self.results:
            self._snippet_result = self.results[-1]

    def handle_data(self, data):
        if self._title_result is not None:
            self._title_result["title"] += data

        if self._snippet_result is not None:
            self._snippet_result["snippet"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._title_result is not None:
            self._title_result = None

        if self._snippet_result is not None:
            self._snippet_result = None


def _clean_text(value):
    """Normalize whitespace in titles and snippets."""
    return re.sub(r"\s+", " ", value or "").strip()


def _normalize_url(raw_url):
    """Resolve search redirects and accept only public HTTP(S) URLs."""
    raw_url = (raw_url or "").strip()

    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url

    parsed = urllib.parse.urlparse(raw_url)

    if parsed.hostname and parsed.hostname.lower().endswith(
        "duckduckgo.com"
    ):
        redirect = urllib.parse.parse_qs(parsed.query).get("uddg")
        if redirect:
            raw_url = redirect[0]
            parsed = urllib.parse.urlparse(raw_url)

    if parsed.scheme not in ("http", "https"):
        return None

    if not parsed.hostname or parsed.username or parsed.password:
        return None

    host = parsed.hostname.lower()

    # Reject local machine names and obvious private/local destinations.
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
        or host in {"127.0.0.1", "::1", "0.0.0.0"}
        or host.startswith(("10.", "192.168.", "169.254."))
    ):
        return None

    # Remove fragments so equivalent links deduplicate.
    return urllib.parse.urlunparse(
        parsed._replace(fragment="")
    )


def _official_domain_priority(query, url):
    """Prefer relevant official domains without treating them as proof."""
    query_lower = query.lower()
    host = (urllib.parse.urlparse(url).hostname or "").lower()

    preferred = []
    for topic, domains in OFFICIAL_DOMAINS.items():
        if topic in query_lower:
            preferred.extend(domains)

    if any(host == domain or host.endswith("." + domain)
           for domain in preferred):
        return 1

    return 0


def search_web(query: str, max_results: int = 5) -> str:
    """Search the web and return a stable, deduplicated source list."""

    query = _clean_text(query)

    if not query:
        return "Please provide a search query."

    if len(query) > MAX_QUERY_LENGTH:
        return "The search query is too long."

    max_results = max(1, min(int(max_results), MAX_RESULTS_LIMIT))

    search_url = (
        "https://html.duckduckgo.com/html/?"
        + urllib.parse.urlencode({"q": query})
    )

    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
        },
    )

    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS
        ) as response:
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return "The search service returned an unexpected response."

            html = response.read(2_000_000).decode(
                "utf-8", errors="replace"
            )

        parser = _SearchParser()
        parser.feed(html)

        cleaned = []
        seen_urls = set()

        for result in parser.results:
            title = _clean_text(result.get("title"))
            snippet = _clean_text(result.get("snippet"))
            result_url = _normalize_url(result.get("url"))

            if not title or not result_url:
                continue

            dedupe_key = result_url.rstrip("/").lower()
            if dedupe_key in seen_urls:
                continue

            seen_urls.add(dedupe_key)

            cleaned.append({
                "title": title,
                "snippet": snippet or "No search snippet was provided.",
                "url": result_url,
                "official_priority": _official_domain_priority(
                    query, result_url
                ),
            })

        # Stable sorting: preferred official domains first, otherwise
        # preserve the search engine's original ranking.
        cleaned.sort(
            key=lambda item: -item["official_priority"]
        )

        cleaned = cleaned[:max_results]

        if not cleaned:
            return (
                "No usable search results could be extracted. "
                "The search page may have changed or blocked the request."
            )

        output = [f"Search results for: {query}"]

        for index, result in enumerate(cleaned, start=1):
            output.append(
                f"\n{index}. {result['title']}\n"
                f"   {result['snippet']}\n"
                f"   Source: {result['url']}"
            )

        output.append(
            "\nReliability note: These are search results, not verified "
            "facts. Check the linked pages, publication dates, and "
            "primary sources before relying on the information."
        )

        return "\n".join(output)

    except urllib.error.HTTPError as error:
        return f"Web search failed: HTTP {error.code}."

    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return (
            "Web search could not connect or timed out. "
            "Check your connection and try again."
        )

    except Exception as error:
        return (
            "Web search failed unexpectedly "
            f"({type(error).__name__})."
        )
