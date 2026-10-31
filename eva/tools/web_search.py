"""API-free public web search using ordinary HTML pages.

EVA fetches DuckDuckGo's public HTML results page with urllib; it does not use a
search-provider API, API key, paid search service, or structured forecast API.
Returned snippets are untrusted discovery aids. Use the controlled Playwright
browser to open public pages and read their visible contents when needed.
"""
from __future__ import annotations

import html
import ipaddress
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

_SEARCH_LOCK = threading.Lock()
_LAST_SEARCH_TIME = 0.0
_MIN_SEARCH_INTERVAL = 0.5  # Minimum seconds between outgoing search engine requests
_SEARCH_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 120.0  # 2-minute cache for identical queries

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
)
TIMEOUT_SECONDS = 12
MAX_QUERY_LENGTH = 300
MAX_RESULTS_LIMIT = 5

OFFICIAL_DOMAINS = {
    "python": ["python.org"],
    "windows": ["microsoft.com"],
    "microsoft": ["microsoft.com"],
    "ollama": ["ollama.com", "github.com"],
    "openai": ["openai.com"],
    "india": ["india.gov.in", "pib.gov.in"],
}


class _SearchParser(HTMLParser):
    """Extract result titles, result URLs, and nearby result snippets."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._active_title: dict[str, str] | None = None
        self._active_snippet: dict[str, str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            self._active_title = {"title": "", "url": attrs.get("href", ""), "snippet": ""}
            self.results.append(self._active_title)
        elif "result__snippet" in classes and self.results:
            self._active_snippet = self.results[-1]

    def handle_data(self, data):
        if self._active_title is not None:
            self._active_title["title"] += data
        if self._active_snippet is not None:
            self._active_snippet["snippet"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._active_title is not None:
            self._active_title = None
        if self._active_snippet is not None:
            self._active_snippet = None



class _FallbackSearchParser(HTMLParser):
    """Conservative extractor for ordinary search-engine HTML pages.

    It deliberately accepts only external public HTTP(S) links and ignores
    engine navigation, ads, and common utility links. This is a fallback for
    markup changes, not a guarantee that every search engine will allow scraping.
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._anchor: dict[str, str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        href = attrs.get("href", "")
        classes = set(attrs.get("class", "").split())
        # Bing result links commonly live inside b_algo; DuckDuckGo lite uses
        # result-link. Generic external anchors are a final conservative fallback.
        if "result-link" in classes or "b_algo" in attrs.get("class", "") or href.startswith(("http://", "https://")):
            self._anchor = {"title": "", "url": href, "snippet": ""}

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["title"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            title = _clean_text(self._anchor.get("title"))
            url = _normalize_url(self._anchor.get("url", ""))
            if title and url:
                self.results.append({"title": title, "url": url, "snippet": "No search snippet was provided."})
            self._anchor = None


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _is_public_web_url(raw_url: str) -> bool:
    """Reject non-web schemes, embedded credentials, and obvious private targets."""
    parsed = urllib.parse.urlparse((raw_url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.casefold().rstrip(".")
    if (host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home"))
            or host in {"0.0.0.0", "::", "::1"}):
        return False
    try:
        address = ipaddress.ip_address(host)
        return bool(address.is_global)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        for item in infos:
            addr = ipaddress.ip_address(item[4][0])
            if not addr.is_global:
                return False
    except (OSError, ValueError):
        pass
    return True


def _normalize_url(raw_url: str) -> str | None:
    """Unwrap DuckDuckGo redirects and accept only public HTTP(S) destinations."""
    raw_url = (raw_url or "").strip()
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.hostname and parsed.hostname.casefold().endswith("duckduckgo.com"):
        redirect = urllib.parse.parse_qs(parsed.query).get("uddg")
        if redirect:
            raw_url = redirect[0]
    if not _is_public_web_url(raw_url):
        return None
    parsed = urllib.parse.urlparse(raw_url)
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def _official_domain_priority(query: str, url: str) -> int:
    query_lower = query.casefold()
    host = (urllib.parse.urlparse(url).hostname or "").casefold()
    preferred = [domain for topic, domains in OFFICIAL_DOMAINS.items() if topic in query_lower for domain in domains]
    return int(any(host == domain or host.endswith("." + domain) for domain in preferred))


def _known_topic_fallback(query: str) -> list[dict[str, str]]:
    """Small offline fallback catalogue for typing practice and standard releases, without API calls."""
    low = query.casefold()
    if "typing" in low and re.search(r"\b(?:learn|learning|free|websites?)\b", low):
        return [
            {"title": "TypingClub", "snippet": "Free touch-typing lessons for individuals and schools.", "url": "https://www.typingclub.com/"},
            {"title": "Typing.com", "snippet": "Typing lessons, practice, and tests.", "url": "https://www.typing.com/"},
            {"title": "Keybr", "snippet": "Adaptive touch-typing practice.", "url": "https://www.keybr.com/"},
        ]
    if "python" in low and ("release" in low or "version" in low or "download" in low or "stable" in low):
        return [
            {"title": "Download Python (Official Downloads Directory) | Python.org",
             "snippet": "Official Python downloads directory for stable releases. (Offline fallback: live verification unavailable; check the official site for current latest release).",
             "url": "https://www.python.org/downloads/"},
            {"title": "Python Source Releases and Documentation | Python.org",
             "snippet": "Historical archive: Python 3.13 series observed in 2025. Current latest stable version must be verified online at https://www.python.org/downloads/.",
             "url": "https://www.python.org/downloads/source/"},
        ]
    return []


def search_web_results(query: str, max_results: int = 5) -> dict:
    """Return structured results obtained from public HTML search pages (not an API)."""
    global _LAST_SEARCH_TIME
    query = _clean_text(query)
    if not query:
        return {"ok": False, "query": query, "results": [], "error": "Please provide a search query."}
    if len(query) > MAX_QUERY_LENGTH:
        return {"ok": False, "query": query, "results": [], "error": "The search query is too long."}
    try:
        max_results = max(1, min(int(max_results), MAX_RESULTS_LIMIT))
    except (TypeError, ValueError):
        max_results = 5

    # Check cache to prevent repetitive requests
    cache_key = f"{query.casefold()}::{max_results}"
    now = time.monotonic()
    with _SEARCH_LOCK:
        if cache_key in _SEARCH_CACHE:
            ts, cached = _SEARCH_CACHE[cache_key]
            if now - ts < _CACHE_TTL:
                return dict(cached)

    engines = [
        ("public_html_search", "https://html.duckduckgo.com/html/?"),
        ("fallback_html_search", "https://www.bing.com/search?"),
        ("fallback_html_search", "https://lite.duckduckgo.com/lite/?"),
    ]
    errors: list[str] = []
    last_url = ""
    for engine_index, (source_mode, base_url) in enumerate(engines):
        search_url = base_url + urllib.parse.urlencode({"q": query})
        last_url = search_url
        request = urllib.request.Request(search_url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
        })
        try:
            with _SEARCH_LOCK:
                elapsed = time.monotonic() - _LAST_SEARCH_TIME
                if elapsed < _MIN_SEARCH_INTERVAL:
                    time.sleep(_MIN_SEARCH_INTERVAL - elapsed)
                _LAST_SEARCH_TIME = time.monotonic()

            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                content_type = response.headers.get("Content-Type", "")
                if content_type and "html" not in content_type.casefold():
                    errors.append(f"{base_url}: unexpected non-HTML response")
                    continue
                body = response.read(2_000_000).decode("utf-8", errors="replace")
            parser = _SearchParser() if engine_index == 0 else _FallbackSearchParser()
            parser.feed(body)
            cleaned: list[dict[str, str]] = []
            seen: set[str] = set()
            for item in parser.results:
                title = _clean_text(item.get("title"))
                url = _normalize_url(item.get("url", ""))
                snippet = _clean_text(item.get("snippet"))
                if not title or not url:
                    continue
                host = (urllib.parse.urlparse(url).hostname or "").casefold()
                if host in {"bing.com", "www.bing.com", "duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"}:
                    continue
                key = url.rstrip("/").casefold()
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({"title": title, "url": url,
                                "snippet": snippet or "No search snippet was provided."})
            cleaned.sort(key=lambda item: -_official_domain_priority(query, item["url"]))
            if cleaned:
                result = {"ok": True, "query": query, "results": cleaned[:max_results],
                          "search_url": search_url, "source_mode": source_mode}
                if engine_index:
                    result["warning"] = "Primary HTML search was unavailable or returned no usable results; a fallback public HTML search page was used."
                with _SEARCH_LOCK:
                    _SEARCH_CACHE[cache_key] = (time.monotonic(), dict(result))
                    if len(_SEARCH_CACHE) > 200:
                        oldest_key = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
                        _SEARCH_CACHE.pop(oldest_key, None)
                return result
            errors.append(f"{base_url}: no usable results extracted")
        except urllib.error.HTTPError as error:
            errors.append(f"{base_url}: HTTP {error.code}")
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
            errors.append(f"{base_url}: {type(error).__name__}")
        except Exception as error:
            errors.append(f"{base_url}: {type(error).__name__}")

    fallback = _known_topic_fallback(query)
    if fallback:
        return {"ok": True, "query": query, "results": fallback[:max_results],
                "search_url": last_url, "source_mode": "bundled_public_site_catalogue",
                "warning": "Live public HTML search engines were unavailable; these are offline fallback suggestions, not live search results."}
    return {"ok": False, "query": query, "results": [], "search_url": last_url,
            "error": "Public HTML search failed across available sources. " + "; ".join(errors) + ". Check internet access or retry later.",
            "attempted_sources": [base for _, base in engines]}


def search_web(query: str, max_results: int = 5) -> str:
    """Search the public web and return a readable, bounded source list."""
    result = search_web_results(query, max_results)
    if not result.get("ok"):
        return str(result.get("error") or "Web search failed.")
    query = result["query"]
    output = [f"Search results for: {query}"]
    for index, item in enumerate(result.get("results", []), start=1):
        output.append(f"\n{index}. {item['title']}\n   {item.get('snippet') or 'No search snippet was provided.'}\n   Source: {item['url']}")
    if result.get("warning"):
        output.append("\nNote: " + result["warning"])
    output.append("\nReliability note: Search snippets are discovery aids, not verified facts. Open source pages and check dates and context before relying on claims.")
    return "\n".join(output)
