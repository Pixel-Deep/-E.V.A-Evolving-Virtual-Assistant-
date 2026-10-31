"""Optional Playwright browser controls; imported lazily and never plan tasks."""
from __future__ import annotations

import os
import re
import ipaddress
import shutil
import socket
import time
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from threading import get_ident
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from eva.automation.engine.models import AutomationError, ExecutionContext, RiskLevel, ToolResult, ToolSpec
from eva.automation.engine.registry import ToolRegistry
from eva.automation.tools.filesystem import _resolve_safe_path, _root_paths


def validate_browser_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise AutomationError("INVALID_ARGUMENT", "A URL from 1 to 4096 characters is required.")
    url = value.strip()
    if any(ord(char) < 32 for char in url):
        raise AutomationError("INVALID_ARGUMENT", "URLs cannot contain control characters.")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise AutomationError("INVALID_ARGUMENT", "Only absolute HTTP and HTTPS URLs are supported.")
    # Accessing SplitResult.port raises ValueError for malformed or out-of-range
    # ports. Convert that parser exception into a structured tool error so an
    # invalid URL cannot escape the browser tool's normal failure contract.
    try:
        port = parsed.port
    except ValueError as exc:
        raise AutomationError("INVALID_ARGUMENT", "The URL port must be an integer from 1 to 65535.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise AutomationError("INVALID_ARGUMENT", "The URL port must be an integer from 1 to 65535.")
    if parsed.username is not None or parsed.password is not None:
        raise AutomationError("INVALID_ARGUMENT", "URLs containing embedded credentials are not supported.")
    return url


def _find_system_chromium_executable() -> str | None:
    """Find an installed Chromium-family browser when Playwright's bundle is absent.

    Playwright's managed Chromium remains the preferred runtime. This fallback is
    useful on machines that already have Chrome/Edge/Chromium installed but cannot
    download Playwright's separate browser package. EVA still creates its own
    isolated browser context; this does not attach to the user's active profile.
    """
    command_names = (
        "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
        "chrome", "chrome.exe", "msedge", "msedge.exe", "microsoft-edge",
        "brave-browser", "brave", "brave.exe",
    )
    candidates: list[str] = []
    for name in command_names:
        try:
            found = shutil.which(name)
        except Exception:
            found = None
        if found:
            candidates.append(found)

    # Standard install paths cover common Windows and macOS installations where
    # the executable is not on PATH. Nonexistent paths are harmless to check.
    program_files = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")]
    for base in filter(None, program_files):
        candidates.extend([
            str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            str(Path(base) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"),
            str(Path(base) / "Chromium" / "Application" / "chrome.exe"),
        ])
    candidates.extend([
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable", "/snap/bin/chromium",
    ])

    seen: set[str] = set()
    for candidate in candidates:
        try:
            path = str(Path(candidate).expanduser())
            if path in seen:
                continue
            seen.add(path)
            if Path(path).is_file():
                return path
        except (OSError, ValueError):
            continue
    return None


def _is_domain_or_subdomain(host: str, domain: str) -> bool:
    """Match a domain boundary, never a lookalike suffix such as ``notyoutube.com``."""
    candidate = (host or "").rstrip(".").casefold()
    base = (domain or "").rstrip(".").casefold()
    return bool(base and (candidate == base or candidate.endswith("." + base)))


def _host_is_public(host: str, port: int) -> bool:
    """Resolve a host and fail closed if any address is local/private/reserved."""
    hostname = (host or "").rstrip(".").casefold()
    if not hostname or hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local", ".internal", ".home", ".lan", ".test")):
        return False
    try:
        try:
            addresses = {ipaddress.ip_address(hostname)}
        except ValueError:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)}
    except (OSError, ValueError):
        return False
    return bool(addresses) and all(address.is_global for address in addresses)


class BrowserSession:
    """One visible, isolated Chromium session reused by browser tools in a run.

    It does not attach to, inspect, or steal an existing user's browser profile.
    Website sign-in state therefore belongs to this isolated session.
    """
    allow_local_urls: bool = False

    def __init__(self, *, headless: bool = False, timeout_ms: int = 35_000, allow_local_urls: bool = False):
        self.headless = headless
        self.timeout_ms = timeout_ms
        # Local development hosts are blocked by default; users may explicitly opt in
        # through config/automation.json when they intend to automate a local app.
        self.allow_local_urls = bool(allow_local_urls)
        self._playwright = None
        self._browser = None
        self._browser_executable: str | None = None
        self._context = None
        self._page = None
        self._tab_ids: dict[int, str] = {}
        self._next_tab_number = 1
        self._snapshot_number = 0
        self._active_snapshot_id: int | None = None
        self._element_refs: dict[str, dict[str, Any]] = {}
        self._observations: list[dict[str, Any]] = []
        self._last_security_block: str | None = None
        self._wired_page_ids: set[int] = set()
        self._destination_cache: dict[str, tuple[float, bool]] = {}
        # Playwright's Sync API cannot run in a thread with an active asyncio
        # loop. All registered browser-tool calls share one dedicated worker so
        # the Playwright greenlet/browser/page remain on their owning thread.
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eva-playwright")
        self._worker_thread_id: int | None = None
        self._worker_shutdown = False

    def _reset_worker(self) -> None:
        """Reset the worker thread so a clean thread/event loop is available."""
        try:
            self._worker.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eva-playwright")
        self._worker_thread_id = None
        self._page = self._context = self._browser = self._playwright = None
        self._tab_ids.clear()
        self._wired_page_ids.clear()
        self._invalidate_element_refs()

    def run_serialized(self, method_name: str, *args, **kwargs):
        """Run a browser-session operation on its single owning thread."""
        if self._worker_shutdown:
            raise AutomationError("BROWSER_SESSION_CLOSED", "The controlled browser session has been shut down.")
        if self._last_security_block:
            msg = self._last_security_block
            self._last_security_block = None
            raise AutomationError("UNSAFE_URL", msg)
        if get_ident() == self._worker_thread_id:
            res = getattr(self, method_name)(*args, **kwargs)
            if self._last_security_block:
                msg = self._last_security_block
                self._last_security_block = None
                raise AutomationError("UNSAFE_URL", msg)
            return res

        def invoke():
            self._worker_thread_id = get_ident()
            res = getattr(self, method_name)(*args, **kwargs)
            if self._last_security_block:
                msg = self._last_security_block
                self._last_security_block = None
                raise AutomationError("UNSAFE_URL", msg)
            return res

        # Individual Playwright methods have their own bounded timeouts. This
        # future wait only serializes ownership and does not change that timeout.
        try:
            return self._worker.submit(invoke).result()
        except Exception as exc:
            err_msg = str(exc).lower()
            if "event loop is closed" in err_msg or "is playwright already stopped" in err_msg or "connection closed" in err_msg:
                self._reset_worker()
                return self._worker.submit(invoke).result()
            raise

    def _ensure_page(self):
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return self._page
            except Exception:
                pass
        # A closed active tab should not tear down other live tabs in this session.
        if self._context is not None and self._browser is not None and self._playwright is not None:
            try:
                pages = [page for page in self._context.pages if not page.is_closed()]
                if pages:
                    self._invalidate_element_refs()
                    self._page = pages[-1]
                    self._wire_page(self._page)
                    return self._page
            except Exception:
                pass
        # If a browser/context died, discard the stale runtime before restarting.
        if any(item is not None for item in (self._page, self._context, self._browser, self._playwright)):
            self._close_impl()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise AutomationError("DEPENDENCY_MISSING", "Browser tools require Playwright. Install requirements.txt, then run 'python -m playwright install chromium'.") from exc
        try:
            self._playwright = sync_playwright().start()
            try:
                self._browser = self._playwright.chromium.launch(headless=self.headless)
            except Exception as bundled_exc:
                system_browser = _find_system_chromium_executable()
                if not system_browser:
                    raise AutomationError(
                        "BROWSER_START_FAILED",
                        "Playwright's Chromium is not installed and no compatible system Chrome/Edge/Chromium executable was found. Run 'python -m playwright install chromium' or install a Chromium-based browser. "
                        f"Bundled-browser error: {type(bundled_exc).__name__}: {str(bundled_exc)[:300]}",
                    ) from bundled_exc
                try:
                    self._browser = self._playwright.chromium.launch(
                        executable_path=system_browser, headless=self.headless
                    )
                    self._browser_executable = system_browser
                except Exception as system_exc:
                    raise AutomationError(
                        "BROWSER_START_FAILED",
                        "Neither Playwright's bundled Chromium nor the detected system browser could start. "
                        f"System executable: {system_browser}. Errors: bundled={type(bundled_exc).__name__}: {str(bundled_exc)[:180]}; "
                        f"system={type(system_exc).__name__}: {str(system_exc)[:240]}",
                    ) from system_exc
            self._context = self._browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            # Every HTTP(S) request (including redirects and subresources) is checked
            # before Chromium sends it. This is stricter than validating only the initial URL.
            self._context.route("**/*", self._guard_route)
            # Capture popups/new pages too, not only tabs explicitly opened by EVA.
            self._context.on("page", self._on_new_page)
            self._page = self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
            self._wire_page(self._page)
            return self._page
        except Exception as exc:
            self._close_impl()
            if isinstance(exc, AutomationError):
                raise
            raise AutomationError("BROWSER_START_FAILED", f"Could not start the controlled Chromium session: {type(exc).__name__}: {exc}") from exc

    def _validate_destination(self, url: str) -> str:
        clean = validate_browser_url(url)
        if self.allow_local_urls:
            return clean
        parsed = urlsplit(clean)
        host = (parsed.hostname or "").rstrip(".").casefold()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        is_public = _host_is_public(host, port)
        if not is_public:
            raise AutomationError("UNSAFE_URL", "This browser blocks local, private, reserved, or unresolvable destinations by default. Set allow_local_browser_urls=true only when intentionally automating a local development site.")
        return clean

    def _guard_route(self, route, request) -> None:
        url = str(getattr(request, "url", ""))
        scheme = urlsplit(url).scheme.casefold()
        if scheme in {"http", "https"}:
            try:
                self._validate_destination(url)
                # Re-validate redirect sources to prevent SSRF through redirect chains
                redirected_from = getattr(request, "redirected_from", None)
                if redirected_from is not None:
                    orig_url = str(getattr(redirected_from, "url", ""))
                    if orig_url:
                        self._validate_destination(orig_url)
            except AutomationError as exc:
                self._record_observation({"kind": "blocked_request", "url": _safe_observation_url(url), "message": exc.message[:240]})
                try:
                    route.abort("blockedbyclient")
                except Exception:
                    pass
                return
        elif scheme in {"file", "chrome", "edge", "javascript"}:
            try:
                route.abort("blockedbyclient")
            except Exception:
                pass
            return
        try:
            route.continue_()
        except Exception:
            # A page may have closed while a request was being dispatched.
            try:
                route.abort()
            except Exception:
                pass

    def local_smoke_test(self) -> dict[str, Any]:
        """Render a fixed in-memory page to validate the real browser runtime.

        This helper is used by the diagnostic audit, not exposed as a user tool.
        It intentionally makes no network request and writes no files.
        """
        page = self._ensure_page()
        page.set_content(
            "<html><head><title>EVA local browser probe</title></head>"
            "<body><main id='eva-audit'>EVA local browser probe</main>"
            "<button type='button'>Ready</button></body></html>",
            wait_until="domcontentloaded", timeout=self.timeout_ms,
        )
        content = page.locator("#eva-audit").inner_text(timeout=self.timeout_ms)
        button_count = page.get_by_role("button", name="Ready", exact=True).count()
        return {"ok": content == "EVA local browser probe" and button_count == 1,
                "title": page.title(), "content_verified": content == "EVA local browser probe",
                "semantic_target_verified": button_count == 1, "browser_version": str(self._browser.version),
                "browser_executable": self._browser_executable or "Playwright-managed Chromium"}

    def _register_tab_id(self, page) -> str:
        key = id(page)
        if key not in self._tab_ids:
            self._tab_ids[key] = f"tab-{self._next_tab_number}"
            self._next_tab_number += 1
        return self._tab_ids[key]

    def _on_new_page(self, page) -> None:
        # A popup becomes the active controlled tab, as it normally would in a
        # user-facing browser. Any refs from the previous page must expire.
        if page is not self._page:
            self._invalidate_element_refs()
        self._page = page
        self._wire_page(page)

    def _wire_page(self, page) -> None:
        self._register_tab_id(page)
        key = id(page)
        if key in self._wired_page_ids:
            return
        self._wired_page_ids.add(key)
        def _on_response(response, p=page):
            try:
                status = int(getattr(response, "status", 0) or 0)
                if status >= 400:
                    self._record_observation({
                        "kind": "http_error", "status": status,
                        "url": _safe_observation_url(getattr(response, "url", ""))
                    })
                # Enforce destination rules on the actual connected IP address to prevent DNS rebinding
                if not self.allow_local_urls:
                    server = getattr(response, "server_addr", None)
                    if callable(server):
                        info = server()
                        if isinstance(info, dict) and info.get("ipAddress"):
                            ip_str = str(info["ipAddress"]).strip()
                            try:
                                addr = ipaddress.ip_address(ip_str)
                                if not addr.is_global:
                                    self._record_observation({
                                        "kind": "blocked_connection",
                                        "ip": ip_str,
                                        "url": _safe_observation_url(getattr(response, "url", "")),
                                        "message": f"Connection to non-public IP {ip_str} was detected and blocked."
                                    })
                                    self._last_security_block = f"Connection to non-public IP {ip_str} was detected and blocked (DNS rebinding protection)."
                                    # Terminate the connected page immediately
                                    try:
                                        p.close()
                                    except Exception:
                                        pass
                                    raise AutomationError("UNSAFE_URL", self._last_security_block)
                            except ValueError:
                                pass
            except AutomationError:
                raise
            except Exception:
                pass

        try:
            page.on("console", lambda message, p=page: self._record_observation({
                "kind": "console", "level": str(getattr(message, "type", "log"))[:30],
                "text": _redact_observation_text(getattr(message, "text", "")), "url": _safe_observation_url(getattr(p, "url", ""))}))
            page.on("pageerror", lambda error, p=page: self._record_observation({
                "kind": "page_error", "text": _redact_observation_text(error), "url": _safe_observation_url(getattr(p, "url", ""))}))
            page.on("requestfailed", lambda request: self._record_observation({
                "kind": "request_failed", "url": _safe_observation_url(getattr(request, "url", "")),
                "failure": str(getattr(request, "failure", "") or "")[:300]}))
            page.on("response", _on_response)
        except Exception:
            # Some test doubles and older Playwright builds may not support event hooks.
            pass

    def _record_observation(self, item: dict[str, Any]) -> None:
        try:
            self._observations.append(dict(item))
            if len(self._observations) > 300:
                del self._observations[:-300]
        except Exception:
            pass

    def _invalidate_element_refs(self) -> None:
        self._active_snapshot_id = None
        self._element_refs.clear()

    @staticmethod
    def _describe_element(locator) -> dict[str, Any]:
        return locator.evaluate(r"""el => {
          const clean = v => String(v || '').replace(/\s+/g, ' ').trim().slice(0, 180);
          const tag = (el.tagName || '').toLowerCase();
          const defaultRoles = {button:'button', a:'link', input:({checkbox:'checkbox',radio:'radio',submit:'button',button:'button',text:'textbox',email:'textbox',search:'searchbox',password:'textbox',number:'spinbutton'}[el.type] || 'textbox'), textarea:'textbox', select:'combobox'};
          const text = clean(el.innerText || el.textContent || '');
          const role = clean(el.getAttribute('role') || defaultRoles[tag] || '');
          const name = clean(el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('alt') || text || el.getAttribute('name') || '');
          return {tag, role, name, text, type:clean(el.getAttribute('type')), href:clean(el.getAttribute('href')), placeholder:clean(el.getAttribute('placeholder'))};
        }""")

    def snapshot(self, max_elements: int = 60, max_chars: int = 12000) -> dict[str, Any]:
        page = self._ensure_page()
        self._snapshot_number += 1
        snapshot_id = self._snapshot_number
        self._active_snapshot_id = snapshot_id
        self._element_refs = {}
        try:
            aria = page.locator("body").aria_snapshot(timeout=self.timeout_ms)
        except Exception:
            aria = ""
        locator = page.locator("button, a, input, textarea, select, [role=button], [role=link], [role=checkbox], [role=radio], [contenteditable=true], [tabindex]:not([tabindex='-1'])")
        try:
            total = locator.count()
        except Exception as exc:
            raise AutomationError("BROWSER_SNAPSHOT_FAILED", f"Could not enumerate page controls: {type(exc).__name__}: {exc}") from exc
        elements = []
        scanned = min(total, max_elements)
        for index in range(scanned):
            try:
                item = locator.nth(index)
                if not item.is_visible(timeout=min(self.timeout_ms, 1500)):
                    continue
                descriptor = self._describe_element(item)
                enabled = item.is_enabled(timeout=min(self.timeout_ms, 1500))
                ref = f"s{snapshot_id}-e{index + 1}"
                self._element_refs[ref] = {"page": page, "index": index, "descriptor": descriptor, "snapshot_id": snapshot_id}
                elements.append({**descriptor, "ref": ref, "enabled": bool(enabled), "visible": True})
            except Exception:
                continue
        return {"ok": True, "url": page.url, "title": page.title(), "snapshot_id": snapshot_id,
                "accessibility_tree": str(aria or "")[:max_chars], "elements": elements,
                "element_count": len(elements), "scanned_count": scanned, "total_interactive_count": total,
                "truncated": total > scanned or len(str(aria or "")) > max_chars,
                "message": "Fresh page snapshot created. Element refs are valid only for this snapshot and active tab."}

    def _resolve_element_ref(self, ref: str, snapshot_id: int):
        entry = self._element_refs.get(ref)
        if not entry or snapshot_id != self._active_snapshot_id or entry.get("snapshot_id") != snapshot_id:
            raise AutomationError("STALE_ELEMENT_REF", "This element reference is missing or stale. Take a fresh browser.snapshot and use its snapshot_id/ref pair.")
        page = entry["page"]
        if page is not self._page:
            raise AutomationError("STALE_ELEMENT_REF", "This reference belongs to a different tab. Focus that tab and take a fresh snapshot.")
        if page.is_closed():
            self._invalidate_element_refs()
            raise AutomationError("STALE_ELEMENT_REF", "The tab containing this element is closed. Take a fresh snapshot.")
        locator = page.locator("button, a, input, textarea, select, [role=button], [role=link], [role=checkbox], [role=radio], [contenteditable=true], [tabindex]:not([tabindex='-1'])").nth(entry["index"])
        try:
            if locator.count() <= entry["index"] or self._describe_element(locator) != entry["descriptor"]:
                self._invalidate_element_refs()
                raise AutomationError("STALE_ELEMENT_REF", "The page changed after the snapshot. Take a fresh browser.snapshot before acting.")
        except AutomationError:
            raise
        except Exception as exc:
            self._invalidate_element_refs()
            raise AutomationError("STALE_ELEMENT_REF", "The referenced element could not be revalidated. Take a fresh snapshot.") from exc
        return locator, entry["descriptor"]

    def click_element(self, ref: str, snapshot_id: int) -> dict[str, Any]:
        locator, descriptor = self._resolve_element_ref(ref, snapshot_id)
        # Invalidate before any side effect so a timeout cannot be blindly replayed.
        self._invalidate_element_refs()
        try:
            locator.click(timeout=self.timeout_ms)
            return {"ok": True, "ref": ref, "target": descriptor, "url": self._page.url, "message": "Grounded element click completed; inspect the resulting page before claiming the user task is complete."}
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not click the inspected element: {type(exc).__name__}: {exc}") from exc

    def fill_element(self, ref: str, snapshot_id: int, value: str) -> dict[str, Any]:
        if not isinstance(value, str) or len(value) > 20_000:
            raise AutomationError("INVALID_ARGUMENT", "Form text must be a string no longer than 20,000 characters.")
        locator, descriptor = self._resolve_element_ref(ref, snapshot_id)
        self._invalidate_element_refs()
        try:
            locator.fill(value, timeout=self.timeout_ms)
            return {"ok": True, "ref": ref, "target": descriptor, "characters": len(value), "url": self._page.url}
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not fill the inspected input: {type(exc).__name__}: {exc}") from exc

    def click_by_role(self, role: str, name: str, exact: bool = True) -> dict[str, Any]:
        allowed_roles = {"button", "link", "checkbox", "radio", "tab", "option", "menuitem"}
        if role not in allowed_roles:
            raise AutomationError("INVALID_ARGUMENT", f"Role must be one of: {', '.join(sorted(allowed_roles))}.")
        if not isinstance(name, str) or not name.strip() or len(name) > 300:
            raise AutomationError("INVALID_ARGUMENT", "A visible accessible name between 1 and 300 characters is required.")
        if not isinstance(exact, bool):
            raise AutomationError("INVALID_ARGUMENT", "exact must be a boolean.")
        page = self._ensure_page()
        try:
            locator = page.get_by_role(role, name=name.strip(), exact=exact)
            count = locator.count()
            if count == 0:
                raise AutomationError("ELEMENT_NOT_FOUND", f"No {role} with accessible name '{name[:100]}' was found on the active page.")
            if count != 1:
                raise AutomationError("AMBIGUOUS_LOCATOR", f"Accessible role/name matched {count} elements. Use browser.snapshot to inspect the page and disambiguate the target.")
            if not locator.is_visible() or not locator.is_enabled():
                raise AutomationError("ELEMENT_NOT_ACTIONABLE", "The uniquely matched control is not visible and enabled.")
            descriptor = self._describe_element(locator)
            self._invalidate_element_refs()
            locator.click(timeout=self.timeout_ms)
            return {"ok": True, "role": role, "name": name, "target": descriptor, "url": page.url,
                    "message": "Clicked the uniquely matched accessible control. Inspect the resulting state before claiming a larger task succeeded."}
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not click the accessible control: {type(exc).__name__}: {exc}") from exc

    def fill_by_label(self, label: str, value: str, exact: bool = True) -> dict[str, Any]:
        if not isinstance(label, str) or not label.strip() or len(label) > 300:
            raise AutomationError("INVALID_ARGUMENT", "A form label between 1 and 300 characters is required.")
        if not isinstance(value, str) or len(value) > 20_000:
            raise AutomationError("INVALID_ARGUMENT", "Form text must be a string no longer than 20,000 characters.")
        if not isinstance(exact, bool):
            raise AutomationError("INVALID_ARGUMENT", "exact must be a boolean.")
        page = self._ensure_page()
        try:
            locator = page.get_by_label(label.strip(), exact=exact)
            count = locator.count()
            if count == 0:
                raise AutomationError("ELEMENT_NOT_FOUND", f"No form field with label '{label[:100]}' was found on the active page.")
            if count != 1:
                raise AutomationError("AMBIGUOUS_LOCATOR", f"Form label matched {count} fields. Use browser.snapshot to inspect the page and disambiguate.")
            if not locator.is_visible() or not locator.is_enabled():
                raise AutomationError("ELEMENT_NOT_ACTIONABLE", "The uniquely matched form field is not visible and enabled.")
            descriptor = self._describe_element(locator)
            self._invalidate_element_refs()
            locator.fill(value, timeout=self.timeout_ms)
            return {"ok": True, "label": label, "target": descriptor, "characters": len(value), "url": page.url}
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not fill the field identified by its label: {type(exc).__name__}: {exc}") from exc

    def find_text(self, query: str, max_items: int = 20) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise AutomationError("INVALID_ARGUMENT", "A search phrase between 1 and 300 characters is required.")
        page = self._ensure_page()
        try:
            found = page.get_by_text(query.strip(), exact=False)
            count = found.count()
            matches = []
            for i in range(min(count, max_items)):
                item = found.nth(i)
                if not item.is_visible(timeout=min(self.timeout_ms, 1200)):
                    continue
                text = " ".join(str(item.inner_text(timeout=min(self.timeout_ms, 1200))).split())
                matches.append({"text": text[:350], "tag": str(item.evaluate("el => el.tagName.toLowerCase()"))[:30]})
            return {"ok": True, "query": query, "url": page.url, "matches": matches, "match_count": len(matches), "total_matches": count, "truncated": count > max_items}
        except Exception as exc:
            raise AutomationError("BROWSER_READ_FAILED", f"Could not find visible page text: {type(exc).__name__}: {exc}") from exc

    def press(self, selector: str, key: str) -> dict[str, Any]:
        _validate_selector(selector)
        if not isinstance(key, str) or not key.strip() or len(key) > 40:
            raise AutomationError("INVALID_ARGUMENT", "A valid Playwright keyboard key is required.")
        self._invalidate_element_refs()
        try:
            page = self._ensure_page()
            _action_locator(page, selector, "press").press(key, timeout=self.timeout_ms)
            return {"ok": True, "selector": selector, "key": key, "url": page.url}
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not send the requested key: {type(exc).__name__}: {exc}") from exc

    def hover(self, selector: str) -> dict[str, Any]:
        _validate_selector(selector)
        self._invalidate_element_refs()
        page = self._ensure_page()
        try:
            _action_locator(page, selector, "hover").hover(timeout=self.timeout_ms)
            return {"ok": True, "selector": selector, "url": page.url}
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not hover the requested locator: {type(exc).__name__}: {exc}") from exc

    def select_option(self, selector: str, value: str | None = None, label: str | None = None) -> dict[str, Any]:
        _validate_selector(selector)
        if (value is None) == (label is None):
            raise AutomationError("INVALID_ARGUMENT", "Provide exactly one of 'value' or 'label'.")
        selected = {"value": value} if value is not None else {"label": label}
        self._invalidate_element_refs()
        page = self._ensure_page()
        try:
            chosen = _action_locator(page, selector, "select option").select_option(**selected, timeout=self.timeout_ms)
            return {"ok": True, "selector": selector, "selected_values": chosen, "url": page.url}
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not select the requested option: {type(exc).__name__}: {exc}") from exc

    def set_checked(self, selector: str, checked: bool = True) -> dict[str, Any]:
        _validate_selector(selector)
        if not isinstance(checked, bool):
            raise AutomationError("INVALID_ARGUMENT", "The checked argument must be boolean.")
        self._invalidate_element_refs()
        page = self._ensure_page()
        try:
            locator = _action_locator(page, selector, "set checked state")
            locator.check(timeout=self.timeout_ms) if checked else locator.uncheck(timeout=self.timeout_ms)
            return {"ok": True, "selector": selector, "checked": checked, "url": page.url}
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not change the checkbox/radio state: {type(exc).__name__}: {exc}") from exc

    def scroll(self, direction: str = "down", amount: int = 650) -> dict[str, Any]:
        if direction not in {"up", "down", "left", "right"}:
            raise AutomationError("INVALID_ARGUMENT", "Direction must be up, down, left, or right.")
        if not isinstance(amount, int) or amount < 50 or amount > 2000:
            raise AutomationError("INVALID_ARGUMENT", "Scroll amount must be between 50 and 2000 pixels.")
        self._invalidate_element_refs()
        page = self._ensure_page()
        delta = amount if direction in {"down", "right"} else -amount
        try:
            page.mouse.wheel(delta if direction in {"left", "right"} else 0, delta if direction in {"up", "down"} else 0)
            self._invalidate_element_refs()
            return {"ok": True, "direction": direction, "amount": amount, "url": page.url}
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not scroll the browser page: {type(exc).__name__}: {exc}") from exc

    def wait_for(self, selector: str, state: str = "visible", timeout_ms: int = 10_000) -> dict[str, Any]:
        _validate_selector(selector)
        if state not in {"attached", "detached", "visible", "hidden"}:
            raise AutomationError("INVALID_ARGUMENT", "Wait state must be attached, detached, visible, or hidden.")
        if not isinstance(timeout_ms, int) or timeout_ms < 100 or timeout_ms > 15_000:
            raise AutomationError("INVALID_ARGUMENT", "Wait timeout must be between 100 and 15000 milliseconds.")
        self._invalidate_element_refs()
        page = self._ensure_page()
        try:
            page.locator(selector).first.wait_for(state=state, timeout=timeout_ms)
            return {"ok": True, "selector": selector, "state": state, "timeout_ms": timeout_ms, "url": page.url, "verified": True}
        except Exception as exc:
            raise AutomationError("BROWSER_WAIT_FAILED", f"The requested page condition was not reached: {type(exc).__name__}: {exc}") from exc

    def _history_action(self, action: str) -> dict[str, Any]:
        page = self._ensure_page()
        self._invalidate_element_refs()
        try:
            if action == "back": response = page.go_back(wait_until="domcontentloaded", timeout=self.timeout_ms)
            elif action == "forward": response = page.go_forward(wait_until="domcontentloaded", timeout=self.timeout_ms)
            else: response = page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
            return {"ok": True, "action": action, "url": page.url, "title": page.title(), "http_status": getattr(response, "status", None)}
        except Exception as exc:
            raise AutomationError("BROWSER_NAVIGATION_FAILED", f"Browser {action} failed: {type(exc).__name__}: {exc}") from exc

    def tabs_list(self) -> dict[str, Any]:
        self._ensure_page()
        tabs = []
        for page in list(self._context.pages):
            try:
                if page.is_closed(): continue
                self._wire_page(page)
                tab_id = self._register_tab_id(page)
                tabs.append({"tab_id": tab_id, "active": page is self._page, "url": str(page.url)[:2000], "title": str(page.title())[:300]})
            except Exception:
                continue
        return {"ok": True, "tabs": tabs, "count": len(tabs), "active_tab_id": self._register_tab_id(self._page)}

    def open_tab(self, url: str | None = None) -> dict[str, Any]:
        self._ensure_page()
        self._invalidate_element_refs()
        live_pages = [p for p in self._context.pages if not p.is_closed()]
        if len(live_pages) >= 10:
            for old_p in live_pages:
                if old_p is not self._page:
                    try:
                        old_p.close()
                        break
                    except Exception:
                        pass
        try:
            clean = self._validate_destination(url) if url else None
            page = self._context.new_page()
            page.set_default_timeout(self.timeout_ms)
            self._page = page
            self._wire_page(page)
            tab_id = self._register_tab_id(page)
            result = {"ok": True, "tab_id": tab_id, "url": "about:blank", "title": ""}
            if clean:
                response = page.goto(clean, wait_until="domcontentloaded", timeout=self.timeout_ms)
                if response and not self.allow_local_urls:
                    server = getattr(response, "server_addr", None)
                    if callable(server):
                        info = server()
                        if isinstance(info, dict) and info.get("ipAddress"):
                            ip_str = str(info["ipAddress"]).strip()
                            try:
                                addr = ipaddress.ip_address(ip_str)
                                if not addr.is_global:
                                    try:
                                        page.goto("about:blank")
                                    except Exception:
                                        pass
                                    raise AutomationError("UNSAFE_URL", f"Connection to non-public IP {ip_str} was detected and blocked (DNS rebinding protection).")
                            except ValueError:
                                pass
                result.update({"url": page.url, "title": page.title(), "http_status": getattr(response, "status", None)})
            return result
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_TAB_OPEN_FAILED", f"Could not open a browser tab: {type(exc).__name__}: {exc}") from exc

    def focus_tab(self, tab_id: str) -> dict[str, Any]:
        self._ensure_page()
        target = None
        for page in self._context.pages:
            if not page.is_closed() and self._register_tab_id(page) == tab_id:
                target = page; break
        if target is None:
            raise AutomationError("TAB_NOT_FOUND", f"No open browser tab has ID '{tab_id}'.")
        self._page = target
        self._invalidate_element_refs()
        try:
            return {"ok": True, "tab_id": tab_id, "url": target.url, "title": target.title(), "message": "The controlled browser's active tab was changed."}
        except Exception as exc:
            raise AutomationError("TAB_FOCUS_FAILED", f"Could not focus the requested browser tab: {type(exc).__name__}: {exc}") from exc

    def close_tab(self, tab_id: str) -> dict[str, Any]:
        self._ensure_page()
        target = None
        for page in self._context.pages:
            if not page.is_closed() and self._register_tab_id(page) == tab_id:
                target = page; break
        if target is None:
            raise AutomationError("TAB_NOT_FOUND", f"No open browser tab has ID '{tab_id}'.")
        prior_url = str(target.url)[:2000]
        previously_active = self._page
        self._invalidate_element_refs()
        try:
            target.close()
            self._tab_ids.pop(id(target), None)
            self._wired_page_ids.discard(id(target))
            survivors = [page for page in self._context.pages if not page.is_closed()]
            if survivors:
                self._page = previously_active if previously_active in survivors else survivors[-1]
            else:
                self._page = self._context.new_page()
                self._page.set_default_timeout(self.timeout_ms)
                self._wire_page(self._page)
            return {"ok": True, "closed_tab_id": tab_id, "closed_url": prior_url, "active_tab_id": self._register_tab_id(self._page)}
        except Exception as exc:
            raise AutomationError("BROWSER_TAB_CLOSE_FAILED", f"Could not close the selected browser tab: {type(exc).__name__}: {exc}") from exc

    def frames_list(self) -> dict[str, Any]:
        page = self._ensure_page()
        rows = []
        for index, frame in enumerate(page.frames[:100]):
            try: rows.append({"index": index, "name": str(frame.name or "")[:200], "url": str(frame.url)[:2000], "main_frame": frame == page.main_frame})
            except Exception: continue
        return {"ok": True, "url": page.url, "frames": rows, "count": len(rows), "truncated": len(page.frames) > len(rows)}

    def diagnostics(self, max_items: int = 50) -> dict[str, Any]:
        if not isinstance(max_items, int) or isinstance(max_items, bool) or max_items < 1 or max_items > 100:
            raise AutomationError("INVALID_ARGUMENT", "max_items must be an integer between 1 and 100.")
        # Diagnostics must observe an existing controlled page, not create a new
        # about:blank page and then misleadingly report "no errors". A closed
        # active page may be replaced by another already-open page in the same
        # managed context, but this read-only request never launches a browser.
        page = self._page
        if (page is None or page.is_closed()) and self._context is not None:
            try:
                survivors = [candidate for candidate in self._context.pages if not candidate.is_closed()]
            except Exception:
                survivors = []
            if survivors:
                page = survivors[-1]
                self._page = page
                self._wire_page(page)
        if page is None or page.is_closed():
            return {"ok": True, "url": None, "page_loaded": False, "diagnostics_available": False,
                    "observations": [], "count": 0, "available_count": 0,
                    "message": "No active page exists in EVA's controlled browser session. Open/navigate a page first; no claim about page health was made."}
        try:
            current_url = str(page.url or "")
        except Exception:
            current_url = ""
        if not current_url or current_url.casefold() == "about:blank":
            return {"ok": True, "url": current_url or None, "page_loaded": False, "diagnostics_available": False,
                    "observations": [], "count": 0, "available_count": 0,
                    "message": "The controlled browser has no loaded page to inspect yet. Navigate to the target page before requesting diagnostics."}
        observations = self._observations[-max_items:]
        return {"ok": True, "url": current_url, "page_loaded": True, "diagnostics_available": True,
                "observations": observations, "count": len(observations), "available_count": len(self._observations),
                "message": "Bounded console/page-error/failed-request/HTTP-error observations. An empty observation list means no captured errors were found, not proof that the page is error-free. Request bodies, headers, and cookies are not collected."}

    def navigate(self, url: str) -> dict[str, Any]:
        """Navigate and report the observed final URL; recover from Google's search challenge safely.

        When Google redirects an explicit public search URL to its ``/sorry`` challenge,
        EVA does not attempt to evade the challenge. It switches to a normal HTML
        DuckDuckGo results page for the same query and reports that substitution.
        """
        requested_url = self._validate_destination(url)
        self._invalidate_element_refs()
        page = self._ensure_page()
        try:
            response = page.goto(requested_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            # A previous browser crash/window close should not poison later tasks.
            # Reset and perform one bounded retry only for a closed target.
            error_text = f"{type(exc).__name__}: {exc}"
            if "blocked" in error_text.casefold() or "blockedbyclient" in error_text.casefold():
                raise AutomationError("UNSAFE_URL", f"Navigation to {requested_url} was blocked at connection boundary: destination resolved to non-public or restricted address.") from exc
            if "closed" not in error_text.casefold() and "targetclosed" not in error_text.casefold():
                # If navigation timed out waiting for domcontentloaded, check if the page actually loaded content
                if "timeout" in error_text.casefold():
                    try:
                        cur_u = str(getattr(page, "url", "") or "")
                        if cur_u and cur_u != "about:blank" and (page.title() or page.locator("body").count() > 0):
                            response = None
                        else:
                            raise AutomationError("BROWSER_NAVIGATION_FAILED", f"Browser navigation failed: {error_text}") from exc
                    except AutomationError:
                        raise
                    except Exception:
                        raise AutomationError("BROWSER_NAVIGATION_FAILED", f"Browser navigation failed: {error_text}") from exc
                else:
                    raise AutomationError("BROWSER_NAVIGATION_FAILED", f"Browser navigation failed: {error_text}") from exc
            else:
                self._close_impl()
                try:
                    page = self._ensure_page()
                    response = page.goto(requested_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                except Exception as retry_exc:
                    retry_error = f"{type(retry_exc).__name__}: {retry_exc}"
                    if "blocked" in retry_error.casefold():
                        raise AutomationError("UNSAFE_URL", f"Navigation to {requested_url} was blocked at connection boundary: destination resolved to non-public or restricted address.") from retry_exc
                    if "timeout" in retry_error.casefold():
                        try:
                            cur_u = str(getattr(page, "url", "") or "")
                            if cur_u and cur_u != "about:blank" and (page.title() or page.locator("body").count() > 0):
                                response = None
                            else:
                                raise AutomationError("BROWSER_NAVIGATION_FAILED", f"Browser navigation failed after one recovery attempt: {retry_error}") from retry_exc
                        except AutomationError:
                            raise
                        except Exception:
                            raise AutomationError("BROWSER_NAVIGATION_FAILED", f"Browser navigation failed after one recovery attempt: {retry_error}") from retry_exc
                    else:
                        raise AutomationError("BROWSER_NAVIGATION_FAILED",
                            f"Browser navigation failed after one recovery attempt: {type(retry_exc).__name__}: {retry_exc}") from retry_exc

        if response and not self.allow_local_urls:
            server = getattr(response, "server_addr", None)
            if callable(server):
                info = server()
                if isinstance(info, dict) and info.get("ipAddress"):
                    ip_str = str(info["ipAddress"]).strip()
                    try:
                        addr = ipaddress.ip_address(ip_str)
                        if not addr.is_global:
                            self._close_impl()
                            raise AutomationError("UNSAFE_URL", f"Connection to non-public IP {ip_str} was detected and blocked (DNS rebinding protection).")
                    except ValueError:
                        pass

        final_url = str(getattr(page, "url", "") or "")
        requested_parts = urlsplit(requested_url)
        final_parts = urlsplit(final_url)
        title = ""
        try:
            title = str(page.title() or "").strip()
            if not title:
                page.wait_for_timeout(750)
                title = str(page.title() or "").strip()
        except Exception:
            title = ""
        if not title and final_url and final_url != "about:blank":
            title = (final_parts.hostname or "").removeprefix("www.") or "Web Page"

        requested_host = (requested_parts.hostname or "").casefold().removeprefix("www.")
        final_host = (final_parts.hostname or "").casefold().removeprefix("www.")
        is_requested_google_search = requested_host == "google.com" and requested_parts.path.rstrip("/") == "/search"
        is_google_challenge = (
            is_requested_google_search and final_host == "google.com"
            and (final_parts.path.startswith("/sorry") or final_parts.path.startswith("/recaptcha"))
        )
        if is_requested_google_search and not is_google_challenge:
            # Some challenge variants preserve a normal-looking URL. Inspect only
            # bounded visible body text and use the alternate engine only on explicit
            # anti-bot markers; ordinary article text containing 'captcha' is not enough.
            try:
                body = str(page.locator("body").inner_text(timeout=min(self.timeout_ms, 1000)) or "")[:6000].casefold()
                is_google_challenge = any(marker in body for marker in (
                    "our systems have detected unusual traffic",
                    "unusual traffic from your computer network",
                    "automated queries",
                    "to continue, please type the characters",
                    "recaptcha/api",
                ))
            except Exception:
                pass

        if is_google_challenge:
            query = (parse_qs(requested_parts.query).get("q") or [""])[0].strip()
            if not query:
                continued = (parse_qs(requested_parts.query).get("continue") or [""])[0]
                query = (parse_qs(urlsplit(continued).query).get("q") or [""])[0].strip()
            if not query:
                return {"ok": False, "verified": False, "requested_url": requested_url,
                        "url": final_url, "title": title,
                        "error_code": "SEARCH_ENGINE_BLOCKED",
                        "message": "Google presented an anti-bot challenge and the original search query could not be recovered. EVA did not attempt to bypass the challenge; no result was opened."}
            fallback_url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query[:300]})
            try:
                fallback_response = page.goto(fallback_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                fallback_final_url = str(getattr(page, "url", "") or "")
                fallback_title = str(page.title() or "")
                result_count = int(page.locator("a.result__a, a.result-link").count())
            except Exception as fallback_exc:
                return {"ok": False, "verified": False, "requested_url": requested_url,
                        "url": str(getattr(page, "url", "") or final_url),
                        "error_code": "SEARCH_FALLBACK_FAILED",
                        "message": f"Google blocked the search and the DuckDuckGo HTML fallback failed ({type(fallback_exc).__name__}). No result was opened."}
            fallback_parts = urlsplit(fallback_final_url)
            fallback_host = (fallback_parts.hostname or "").casefold().removeprefix("www.")
            if fallback_host != "html.duckduckgo.com" or result_count <= 0:
                return {"ok": False, "verified": False, "requested_url": requested_url,
                        "url": fallback_final_url, "title": fallback_title,
                        "http_status": getattr(fallback_response, "status", None),
                        "error_code": "SEARCH_FALLBACK_NO_RESULTS",
                        "message": "Google presented an anti-bot challenge. EVA tried DuckDuckGo's public HTML search, but could not verify a results page; no result was opened."}
            return {"ok": True, "verified": True, "requested_url": requested_url,
                    "url": fallback_final_url, "title": fallback_title,
                    "http_status": getattr(fallback_response, "status", None),
                    "fallback_used": True, "fallback_engine": "DuckDuckGo",
                    "query": query,
                    "message": "Google presented an anti-bot challenge. EVA used DuckDuckGo's public HTML results for the same query; the alternate search page and visible results were verified."}

        # Successful navigation means the final URL and page title were actually
        # observed. This verifies navigation only—not the meaning or completeness of
        # the page, which must be checked by the next task step.
        observed = bool(final_url and final_url != "about:blank" and title)
        return {"ok": observed, "verified": observed, "requested_url": requested_url,
                "url": final_url or None, "title": title,
                "http_status": getattr(response, "status", None),
                "message": ("The final browser URL and title were observed. Page-specific results or content still require verification."
                            if observed else "Browser navigation returned without a verifiable final URL and title."),
                "error_code": None if observed else "NAVIGATION_NOT_VERIFIED"}

    def open_first_result(self) -> dict[str, Any]:
        """Open the first safe, visible organic result from a supported results page."""
        return self.open_result(index=1, play=False)

    def open_result(self, index: int = 1, play: bool = False) -> dict[str, Any]:
        """Open a specified visible search result, and optionally confirm video playback.

        Result links are sourced from the active result-page DOM rather than guessed.
        Duplicate hrefs and unsafe/non-result links do not consume the requested rank.
        Playback requests are successful only when a YouTube video element reports
        that it is actively playing; opening a watch URL alone is insufficient.
        """
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 10:
            raise AutomationError("INVALID_ARGUMENT", "Result index must be an integer from 1 to 10.")
        if not isinstance(play, bool):
            raise AutomationError("INVALID_ARGUMENT", "The play flag must be a boolean.")

        page = self._ensure_page()
        source_url = str(page.url or "")
        parsed_source = urlsplit(source_url)
        source_host = (parsed_source.hostname or "").casefold().removeprefix("www.")
        if parsed_source.scheme not in {"http", "https"}:
            raise AutomationError("NOT_SEARCH_RESULTS", "The active browser page is not a public web search results page.")

        if _is_domain_or_subdomain(source_host, "google.com") and parsed_source.path.startswith("/search"):
            selectors = ["a:has(h3)"]
        elif _is_domain_or_subdomain(source_host, "youtube.com") and parsed_source.path.startswith("/results"):
            selectors = ["ytd-video-renderer a#video-title", "a#video-title"]
        elif _is_domain_or_subdomain(source_host, "bing.com") and parsed_source.path.startswith("/search"):
            selectors = ["li.b_algo h2 a", "a:has(h2)"]
        elif _is_domain_or_subdomain(source_host, "duckduckgo.com") and (parsed_source.path.startswith("/html") or "q" in parse_qs(parsed_source.query)):
            selectors = ["a.result__a", "a.result-link"]
        else:
            raise AutomationError(
                "NOT_SEARCH_RESULTS",
                "The active page is not a supported Google, YouTube, Bing, or DuckDuckGo results page. Search first; no link was opened.",
            )

        results: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        failures: list[str] = []
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(int(locator.count()), 60)
            except Exception as exc:
                failures.append(f"{selector}: {type(exc).__name__}")
                continue
            for candidate_index in range(count):
                candidate = locator.nth(candidate_index)
                try:
                    if not candidate.is_visible(timeout=min(self.timeout_ms, 1200)):
                        continue
                    href = str(candidate.get_attribute("href") or "").strip()
                    title = " ".join(str(candidate.inner_text(timeout=min(self.timeout_ms, 1200)) or "").split())[:300]
                    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                        continue
                    resolved = urljoin(source_url, href)
                    parsed_target = urlsplit(resolved)
                    target_host = (parsed_target.hostname or "").casefold().removeprefix("www.")
                    if _is_domain_or_subdomain(target_host, "duckduckgo.com") and parsed_target.path.startswith("/l/"):
                        redirect = parse_qs(parsed_target.query).get("uddg", [])
                        if redirect:
                            resolved = redirect[0]
                            parsed_target = urlsplit(resolved)
                            target_host = (parsed_target.hostname or "").casefold().removeprefix("www.")
                    elif _is_domain_or_subdomain(target_host, "google.com") and parsed_target.path == "/url":
                        query_values = parse_qs(parsed_target.query)
                        redirect = (query_values.get("q") or query_values.get("url") or [""])[0]
                        if redirect.startswith(("http://", "https://")):
                            resolved = redirect
                            parsed_target = urlsplit(resolved)
                            target_host = (parsed_target.hostname or "").casefold().removeprefix("www.")
                    if not target_host or not parsed_target.hostname:
                        continue
                    if target_host == source_host:
                        # YouTube results intentionally link to same-host /watch pages.
                        if not (_is_domain_or_subdomain(source_host, "youtube.com") and parsed_source.path.startswith("/results") and parsed_target.path.startswith("/watch")):
                            continue
                    try:
                        resolved = self._validate_destination(resolved)
                    except AutomationError as exc:
                        failures.append(exc.code)
                        continue
                    canonical = resolved.split("#", 1)[0]
                    if canonical in seen_urls:
                        continue
                    seen_urls.add(canonical)
                    results.append((resolved, title))
                    if len(results) >= index:
                        break
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    continue
            if len(results) >= index:
                break

        if len(results) < index:
            detail = (" Details: " + ", ".join(failures[:4])) if failures else ""
            raise AutomationError(
                "SEARCH_RESULT_NOT_FOUND",
                f"Only {len(results)} safe, visible result(s) could be extracted; result {index} was not found. EVA did not guess a destination." + detail,
            )

        chosen_url, chosen_title = results[index - 1]
        navigation = self.navigate(chosen_url)
        final_url = str(navigation.get("url") or "")
        # Older adapters/tests may provide only the final URL. The real navigate()
        # implementation supplies `verified`; when the field is absent, observing a
        # concrete changed URL remains the minimum compatible evidence.
        navigation_verified = navigation.get("verified", bool(final_url))
        if not navigation.get("ok") or not navigation_verified or not final_url or final_url.casefold() == source_url.casefold():
            raise AutomationError("RESULT_NOT_OPENED", f"Result {index} was selected but its final page could not be verified (URL: {final_url or 'unknown'}).")

        result: dict[str, Any] = {
            "ok": True, "verified": True, "source_url": source_url,
            "result_index": index, "result_title": chosen_title,
            "result_url": chosen_url, "opened_url": final_url,
            "title": navigation.get("title", ""),
            "http_status": navigation.get("http_status"),
            "message": f"Result {index} was extracted from the active results page, passed URL-safety validation, and was opened; the final URL was observed.",
        }
        if not play:
            return result

        final_parts = urlsplit(final_url)
        final_host = (final_parts.hostname or "").casefold().removeprefix("www.")
        if not _is_domain_or_subdomain(final_host, "youtube.com") or not final_parts.path.startswith("/watch"):
            return {**result, "ok": False, "verified": False, "error_code": "PLAYBACK_UNSUPPORTED",
                    "message": f"Result {index} opened, but it is not a YouTube watch page. EVA did not claim playback (URL: {final_url})."}
        try:
            playback = self._attempt_video_playback(page)
        except Exception as exc:
            return {**result, "ok": False, "verified": False, "error_code": "PLAYBACK_NOT_CONFIRMED",
                    "message": f"The video page opened, but playback could not be verified ({type(exc).__name__}). URL: {final_url}."}
        if not playback.get("playing"):
            return {**result, "ok": False, "verified": False, "error_code": "PLAYBACK_NOT_CONFIRMED",
                    "playback_state": playback,
                    "message": f"The video page opened, but the video is not confirmed as playing. URL: {final_url}."}
        return {**result, "verified": True, "playback_state": playback,
                "message": f"Result {index} opened and YouTube video playback was confirmed."}

    def _attempt_video_playback(self, page) -> dict[str, Any]:
        """Try the visible YouTube play controls and verify the video state."""
        last_error = ""
        for selector in ("button.ytp-large-play-button", "button.ytp-play-button"):
            try:
                locator = page.locator(selector)
                if int(locator.count()) > 0 and locator.first.is_visible(timeout=min(self.timeout_ms, 1000)):
                    locator.first.click(timeout=min(self.timeout_ms, 1500))
                    break
            except Exception as exc:
                last_error = type(exc).__name__
        try:
            page.wait_for_timeout(700)
        except Exception:
            pass
        try:
            videos = page.locator("video")
            if int(videos.count()) <= 0:
                return {"playing": False, "reason": "no_video_element", "detail": last_error or None}
            state = videos.first.evaluate("(video) => ({paused: video.paused, ended: video.ended, currentTime: video.currentTime})")
            playing = isinstance(state, dict) and not state.get("paused", True) and not state.get("ended", False)
            return {"playing": bool(playing), "paused": bool(state.get("paused", True)) if isinstance(state, dict) else True,
                    "ended": bool(state.get("ended", False)) if isinstance(state, dict) else False,
                    "current_time": state.get("currentTime") if isinstance(state, dict) else None,
                    "detail": last_error or None}
        except Exception as exc:
            return {"playing": False, "reason": "video_state_unavailable", "detail": type(exc).__name__}

    def shop_and_add_to_cart(self, query: str, site: str | None = None, url: str | None = None) -> dict[str, Any]:
        """Search for a product on an e-commerce website, open it, add to cart, and verify.

        Supports Amazon, Flipkart, eBay, Walmart, Target, Best Buy, Etsy, AliExpress,
        or any custom/local e-commerce store. Verifies the addition with page evidence
        (cart count, redirect to cart/wagon, or 'Added to Cart' banner).
        """
        clean_query = (query or "").strip().strip(" .,!?:;")
        clean_query = re.sub(r"^(?:a|an|the|one)\s+", "", clean_query, flags=re.I).strip()
        if not clean_query:
            raise AutomationError("INVALID_ARGUMENT", "A non-empty product query is required.")

        site_lower = (site or "").lower().strip()
        if not site_lower and url:
            parsed_u = urlsplit(url)
            site_lower = (parsed_u.hostname or "").lower()

        # Determine target store and initial search URL
        if not url:
            if "flipkart" in site_lower:
                base_url = "https://www.flipkart.com"
                search_url = f"{base_url}/search?" + urlencode({"q": clean_query})
                site_name = "Flipkart"
            elif "ebay" in site_lower:
                base_url = "https://www.ebay.com"
                search_url = f"{base_url}/sch/i.html?" + urlencode({"_nkw": clean_query})
                site_name = "eBay"
            elif "walmart" in site_lower:
                base_url = "https://www.walmart.com"
                search_url = f"{base_url}/search?" + urlencode({"q": clean_query})
                site_name = "Walmart"
            elif "target" in site_lower:
                base_url = "https://www.target.com"
                search_url = f"{base_url}/s?" + urlencode({"searchTerm": clean_query})
                site_name = "Target"
            elif "bestbuy" in site_lower or "best buy" in site_lower:
                base_url = "https://www.bestbuy.com"
                search_url = f"{base_url}/site/searchpage.jsp?" + urlencode({"st": clean_query})
                site_name = "Best Buy"
            elif "etsy" in site_lower:
                base_url = "https://www.etsy.com"
                search_url = f"{base_url}/search?" + urlencode({"q": clean_query})
                site_name = "Etsy"
            elif "aliexpress" in site_lower:
                base_url = "https://www.aliexpress.com"
                search_url = f"{base_url}/wholesale?" + urlencode({"SearchText": clean_query})
                site_name = "AliExpress"
            elif "." in site_lower and ("http://" in site_lower or "https://" in site_lower or "/" not in site_lower):
                target_base = site_lower if site_lower.startswith(("http://", "https://")) else f"https://{site_lower}"
                search_url = f"{target_base}/search?" + urlencode({"q": clean_query})
                site_name = site_lower
            else:
                domain = "amazon.com" if "amazon.com" in site_lower else "amazon.in"
                base_url = f"https://www.{domain}"
                search_url = f"{base_url}/s?" + urlencode({"k": clean_query})
                site_name = "Amazon"
        else:
            parsed_u = urlsplit(url)
            if parsed_u.path in ("", "/") and not parsed_u.query:
                base_u = url.rstrip("/")
                search_url = f"{base_u}/search?" + urlencode({"q": clean_query})
            else:
                search_url = url
            site_name = site or (parsed_u.hostname or "Store").removeprefix("www.")

        page = self._ensure_page()
        blocked_status: list[int] = []
        def _capture_status(res):
            try:
                if getattr(res, "status", None) in (403, 429, 435):
                    blocked_status.append(res.status)
            except Exception:
                pass
        try:
            page.on("response", _capture_status)
        except Exception:
            pass

        # Step 1: Navigate to search results
        nav = self.navigate(search_url)
        page = self._ensure_page()
        if not nav.get("ok"):
            raise AutomationError("NAVIGATION_FAILED", f"Could not navigate to {site_name} search: {nav.get('message', 'unknown error')}")

        title_lower = (page.title() or "").lower()
        if 435 in blocked_status or any(w in title_lower for w in ("robot check", "captcha", "security measure", "access denied", "blocked", "error page", "pardon our interruption")):
            raise AutomationError("BOT_DETECTION_CHALLENGE", f"{site_name} presented an anti-bot security challenge or access restriction. Could not proceed automatically.")

        # Step 2: Wait for search results
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass

        # Check if we landed on a page with a search box before product listings
        product_card_selectors = [
            "div[data-component-type='s-search-result']",
            "div.s-result-item[data-asin]",
            "div[data-id]",
            "li.s-item",
            "div.s-item__info",
            "div[data-item-id]",
            "div.product-item",
            "div.product-card",
            "div.product",
            "article.product_pod",
            "div.grid-product",
            "div[data-test*='ProductCard']",
            "div[data-test='product-grid']",
            "div[data-testid*='product']",
            "div.cPHDOP",
            "div._1AtVbE",
        ]
        try:
            cards_exist = any(page.locator(sel).count() > 0 for sel in product_card_selectors)
            if not cards_exist:
                search_inputs = page.locator("input[type='search'], input[name='q'], input[name='k'], input[name='field-keywords'], input[placeholder*='search' i], input#search-box, input#search, input[data-test*='SearchInput'], input[data-test='search-input']")
                if search_inputs.count() > 0:
                    first_input = search_inputs.first
                    if first_input.is_visible(timeout=500):
                        first_input.fill(clean_query)
                        first_input.press("Enter")
                        page.wait_for_timeout(2000)
        except Exception:
            pass

        # Step 3: Extract product cards/links
        chosen_link = None
        chosen_title = ""

        for card_sel in product_card_selectors:
            try:
                cards = page.locator(card_sel)
                card_count = cards.count()
            except Exception:
                continue
            if card_count > 0:
                for idx in range(min(card_count, 15)):
                    card = cards.nth(idx)
                    try:
                        if not card.is_visible(timeout=500):
                            continue
                        links = card.locator("a")
                        l_count = links.count()
                        for li in range(l_count):
                            l_item = links.nth(li)
                            href = str(l_item.get_attribute("href") or "").strip()
                            txt = " ".join(str(l_item.inner_text(timeout=500) or "").split())
                            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                                continue
                            if any(p in href for p in ("/gp/help/", "/customer-preferences", "privacy", "signin", "register", "cart")):
                                continue
                            if len(txt) > 3 or any(p in href for p in ("/dp/", "/p/", "/itm/", "product", "item")):
                                chosen_link = href
                                chosen_title = txt or clean_query
                                break
                        if chosen_link:
                            break
                    except Exception:
                        continue
            if chosen_link:
                break

        if not chosen_link:
            direct_selectors = [
                "h2 a[href*='/dp/']",
                "[data-cy='title-recipe'] a",
                "h2.title a",
                ".product-item a",
                "a[href*='/product/']",
                "a[href*='/item/']",
                "a[href*='/p/']",
                "a[href*='/itm/']",
                "a[data-test='product-title']",
                "[data-test='product-title'] a",
                "a.s-item__link",
                "a._1fQZEK",
                "a.s1Q9rs",
                "a.VJA3rP",
                "a:has(h2)",
                "a:has(h3)",
            ]
            for d_sel in direct_selectors:
                try:
                    loc = page.locator(d_sel)
                    l_count = loc.count()
                except Exception:
                    continue
                if l_count > 0:
                    for idx in range(min(l_count, 10)):
                        cand = loc.nth(idx)
                        try:
                            href = str(cand.get_attribute("href") or "").strip()
                            txt = " ".join(str(cand.inner_text(timeout=500) or "").split())
                            if href and not href.startswith(("#", "javascript:")):
                                chosen_link = href
                                chosen_title = txt or clean_query
                                break
                        except Exception:
                            continue
                if chosen_link:
                    break

        if not chosen_link:
            if 435 in blocked_status:
                raise AutomationError("BOT_DETECTION_CHALLENGE", f"{site_name} presented an anti-bot security challenge or regional restriction (HTTP 435). Could not proceed automatically.")
            if any(code in blocked_status for code in (403, 429)):
                raise AutomationError("BOT_DETECTION_CHALLENGE", f"{site_name} returned an access challenge or rate-limit (HTTP {blocked_status[0]}). Could not extract product listings.")
            raise AutomationError("PRODUCT_NOT_FOUND", f"Could not find matching product results for '{clean_query}' on {site_name}.")

        # Step 4: Resolve product URL and navigate
        if not chosen_link.startswith(("http://", "https://")):
            chosen_link = urljoin(page.url, chosen_link)

        self.navigate(chosen_link)
        page = self._ensure_page()
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass

        # Step 5: Locate Add to Cart button
        add_btn_selectors = [
            "#add-to-cart-button",
            "input#add-to-cart-button",
            "input[name='submit.add-to-cart']",
            "button[name='submit.add-to-cart']",
            "#add-to-cart-button-ubb",
            "#addToCart input",
            "*:text('Add to cart')",
            "*:text('Add to Cart')",
            "*:text('ADD TO CART')",
            "button:has-text('Add to cart')",
            "button:has-text('Add to Cart')",
            "button:has-text('Add to Basket')",
            "button:has-text('Add to basket')",
            "button:has-text('ADD TO CART')",
            "input[value='Add to Cart']",
            "input[value='Add to cart']",
            "[data-action='add-to-cart']",
            "button.single_add_to_cart_button",
            "button[aria-label*='Add to cart' i]",
            "button[aria-label*='Add to basket' i]",
            "button.add-to-cart",
            "button[data-testid*='add-to-cart' i]",
            "button[data-testid*='addToCart' i]",
            "a#atcBtn_btn_1",
            "a[data-testid='x-atc-action']",
            "button[data-testid='x-atc-action']",
            "#isCartBtn_btn",
            "button[data-test='chooseOptionsButton']",
            "button[data-test='shippingButton']",
            "button[id*='addToCart']",
            "div[class*='css-']:has-text('Add to cart')",
            "button._2KpZ6l._2U9uOA._3v1-ww",
            "a:has-text('Add to cart')",
            "a:has-text('Add to Cart')",
        ]
        target_btn = None
        for btn_sel in add_btn_selectors:
            try:
                btn_loc = page.locator(btn_sel)
                b_count = btn_loc.count()
            except Exception:
                continue
            if b_count > 0:
                for bi in range(b_count):
                    cand_btn = btn_loc.nth(bi)
                    try:
                        if cand_btn.is_visible(timeout=500):
                            target_btn = cand_btn
                            break
                    except Exception:
                        continue
            if target_btn is not None:
                break

        if target_btn is None:
            try:
                body_txt = (page.inner_text("body") or "").lower()
                if "currently unavailable" in body_txt or "out of stock" in body_txt:
                    raise AutomationError("PRODUCT_OUT_OF_STOCK", f"Selected product '{chosen_title}' on {site_name} is currently out of stock.")
            except AutomationError:
                raise
            except Exception:
                pass
            raise AutomationError("ADD_TO_CART_NOT_FOUND", f"Could not locate an active 'Add to Cart' button for '{chosen_title}' on {site_name}.")

        # Step 6: Click Add to Cart
        target_btn.click(timeout=self.timeout_ms)
        try:
            page.wait_for_timeout(2500)
            # Switch to new page if click opened a new tab/window
            pages = [p for p in self._context.pages if not p.is_closed()]
            if pages and pages[-1] != page:
                self._page = pages[-1]
                page = self._page
                page.set_default_timeout(self.timeout_ms)
                self._wire_page(page)
        except Exception:
            pass

        # Step 7: Verification
        final_url = page.url or ""
        url_lower = final_url.lower()

        is_cart_url = any(p in url_lower for p in ("/cart", "/wagon", "/basket", "/checkout", "viewcart", "/gp/cart"))
        has_success_heading = False
        success_locators = [
            "#NATC_SMART_WAGON_CONF_MSG_SUCCESS",
            "#sw-atc-confirmation",
            "h1:has-text('Added to Cart')",
            "h2:has-text('Added to Cart')",
            "h4:has-text('Added to Cart')",
            "div:has-text('Added to Cart')",
            ":has-text('Added to Basket')",
            ".cart-success",
            ".added-to-cart",
            ":has-text('Item added to cart')",
            ":has-text('Added to Cart!')",
            ":has-text('Added to cart')",
        ]
        for s_sel in success_locators:
            try:
                loc = page.locator(s_sel)
                if loc.count() > 0 and loc.first.is_visible(timeout=300):
                    has_success_heading = True
                    break
            except Exception:
                continue

        cart_count = None
        cart_count_selectors = [
            "#nav-cart-count", ".cart-count", "[data-cart-count]", ".badge", "span.cart-count",
            "a[href*='cart']", "a[href*='viewcart']", "[data-testid*='cart']", "[aria-label*='cart' i]",
        ]
        for c_sel in cart_count_selectors:
            try:
                elem = page.locator(c_sel)
                if elem.count() > 0:
                    text_val = elem.first.inner_text(timeout=300).strip()
                    digit_match = re.search(r"\b(\d+)\b", text_val)
                    if digit_match and int(digit_match.group(1)) >= 1:
                        cart_count = int(digit_match.group(1))
                        break
            except Exception:
                continue

        has_cart_count = cart_count is not None and cart_count >= 1
        verified = is_cart_url or has_success_heading or has_cart_count

        if not verified:
            raise AutomationError("CART_UPDATE_NOT_VERIFIED", f"Clicked 'Add to Cart' for '{chosen_title}' on {site_name}, but cart confirmation could not be verified.")

        display_count = cart_count if cart_count is not None else 1
        return {
            "ok": True,
            "verified": True,
            "cart_updated": True,
            "site": site_name,
            "query": clean_query,
            "product_title": chosen_title or clean_query,
            "product_url": chosen_link,
            "cart_count": display_count,
            "final_url": final_url,
            "message": f"Workflow done. Successfully opened {site_name}, searched for '{clean_query}', selected '{chosen_title or clean_query}', and added to cart. Verified cart update (cart count: {display_count}).",
        }

    def click(self, selector: str) -> dict[str, Any]:
        _validate_selector(selector)
        self._invalidate_element_refs()
        page = self._ensure_page()
        try:
            _action_locator(page, selector, "click").click(timeout=self.timeout_ms)
            return {"ok": True, "selector": selector, "url": page.url}
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not click the requested locator: {type(exc).__name__}: {exc}") from exc

    def fill(self, selector: str, value: str) -> dict[str, Any]:
        _validate_selector(selector)
        if not isinstance(value, str) or len(value) > 20_000:
            raise AutomationError("INVALID_ARGUMENT", "Form text must be a string no longer than 20,000 characters.")
        self._invalidate_element_refs()
        page = self._ensure_page()
        try:
            _action_locator(page, selector, "fill").fill(value, timeout=self.timeout_ms)
            return {"ok": True, "selector": selector, "characters": len(value), "url": page.url}
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_ACTION_FAILED", f"Could not fill the requested locator: {type(exc).__name__}: {exc}") from exc

    def text(self, selector: str = "body", max_chars: int = 20_000) -> dict[str, Any]:
        _validate_selector(selector)
        page = self._ensure_page()
        try:
            locator = page.locator(selector)
            # Article paragraph selectors intentionally collect several paragraphs;
            # reading only `.first` would return just the lead paragraph and can make
            # local extractive summaries too thin. Generic selectors retain their
            # historical first-match behavior to avoid concatenating repeated UI.
            collect_all = bool(re.search(r"(?:^|\s)p(?:\s|$)|>\s*p(?:\s|$)", selector))
            if collect_all:
                count = min(int(locator.count()), 30)
                values: list[str] = []
                for index in range(count):
                    value = str(locator.nth(index).inner_text(timeout=self.timeout_ms) or "").strip()
                    if value:
                        values.append(value)
                value = "\n\n".join(values)
            else:
                probe_timeout = min(self.timeout_ms, 2500) if selector in {"main", "article", "[role=main]"} else self.timeout_ms
                value = locator.first.inner_text(timeout=probe_timeout)
            value = str(value or "")
            return {"ok": True, "verified": bool(value.strip()), "url": page.url, "title": page.title(), "selector": selector,
                    "text": value[:max_chars], "truncated": len(value) > max_chars,
                    "paragraph_count": len(values) if collect_all else None}
        except Exception as exc:
            raise AutomationError("BROWSER_READ_FAILED", f"Could not read text from the requested locator: {type(exc).__name__}: {exc}") from exc

    def headings(self, max_items: int = 30) -> dict[str, Any]:
        """Extract bounded h1/h2/h3 text from the current page."""
        page = self._ensure_page()
        try:
            values = page.locator("h1, h2, h3").all_text_contents(timeout=self.timeout_ms)
            headings = []
            for value in values:
                cleaned = " ".join(str(value).split())
                if cleaned:
                    headings.append(cleaned[:300])
                if len(headings) >= max_items:
                    break
            return {"ok": True, "url": page.url, "title": page.title(),
                    "headings": headings, "heading_count": len(headings),
                    "truncated": len(values) > len(headings)}
        except Exception as exc:
            raise AutomationError("BROWSER_READ_FAILED", f"Could not extract page headings: {type(exc).__name__}: {exc}") from exc

    def download_link(self, url: str, selector: str, path: str, roots: tuple[Path, ...], context: ExecutionContext) -> dict[str, Any]:
        url = self._validate_destination(url)
        _validate_selector(selector)
        target = _resolve_safe_path(path, roots, purpose="write")
        if target.exists():
            raise AutomationError("ALREADY_EXISTS", "Download destination already exists; EVA did not overwrite it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        page = self._ensure_page()
        created_target = False
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            # A click may cause an external side effect; this entire tool requires
            # run-scoped approval before reaching the handler.
            with page.expect_download(timeout=60_000) as download_info:
                _action_locator(page, selector, "download click").click(timeout=self.timeout_ms)
            download = download_info.value
            temporary_path = download.path()
            if not temporary_path:
                raise AutomationError("DOWNLOAD_FAILED", "The browser did not provide a completed download file.")
            source = Path(temporary_path)
            size = source.stat().st_size
            if size > 512 * 1024 * 1024:
                raise AutomationError("FILE_TOO_LARGE", "Browser downloads are limited to 512 MiB.")
            with source.open("rb") as src:
                with target.open("xb") as dst:
                    created_target = True
                    copied = 0
                    while True:
                        context.check_cancelled()
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > 512 * 1024 * 1024:
                            raise AutomationError("FILE_TOO_LARGE", "Browser download exceeded 512 MiB while copying.")
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
            if copied != size:
                raise AutomationError("VERIFICATION_FAILED", "Downloaded file size changed while being saved.")
            return {"ok": True, "path": str(target), "size_bytes": size,
                    "suggested_filename": str(download.suggested_filename or "")[:250], "url": page.url}
        except AutomationError:
            if created_target:
                target.unlink(missing_ok=True)
            raise
        except Exception as exc:
            if created_target:
                target.unlink(missing_ok=True)
            raise AutomationError("BROWSER_DOWNLOAD_FAILED", f"Could not download the selected browser link: {type(exc).__name__}: {exc}") from exc

    def upload_file(self, url: str, selector: str, path: str, roots: tuple[Path, ...]) -> dict[str, Any]:
        url = self._validate_destination(url)
        _validate_selector(selector)
        source = _resolve_safe_path(path, roots)
        if source.is_symlink() or not source.is_file():
            raise AutomationError("RESOURCE_NOT_FOUND", "Upload source must be an existing regular file.")
        if source.stat().st_size > 50 * 1024 * 1024:
            raise AutomationError("FILE_TOO_LARGE", "Browser file uploads are limited to 50 MiB.")
        page = self._ensure_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            locator = _action_locator(page, selector, "file upload")
            is_file_input = locator.evaluate("element => element.tagName.toLowerCase() === 'input' && element.type === 'file'")
            if not is_file_input:
                raise AutomationError("INVALID_UPLOAD_TARGET", "The selected browser element is not an HTML file input.")
            locator.set_input_files(str(source), timeout=self.timeout_ms)
            return {"ok": True, "source": str(source), "size_bytes": source.stat().st_size,
                    "selector": selector, "url": page.url,
                    "message": "File was attached to the page; it has not been submitted."}
        except AutomationError:
            raise
        except Exception as exc:
            raise AutomationError("BROWSER_UPLOAD_FAILED", f"Could not attach the file to the browser form: {type(exc).__name__}: {exc}") from exc

    def screenshot(self, path: str, roots: tuple[Path, ...]) -> dict[str, Any]:
        target = _resolve_safe_path(path, roots, purpose="write")
        if target.exists():
            raise AutomationError("ALREADY_EXISTS", "Screenshot destination already exists; EVA did not overwrite it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        page = self._ensure_page()
        created_target = False
        try:
            # Viewport-only screenshots keep the output and browser work bounded.
            content = page.screenshot(full_page=False, timeout=self.timeout_ms)
            if len(content) > 50 * 1024 * 1024:
                raise AutomationError("FILE_TOO_LARGE", "Browser screenshots are limited to 50 MiB.")
            with target.open("xb") as stream:
                created_target = True
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            return {"ok": True, "path": str(target), "url": page.url, "size_bytes": len(content)}
        except AutomationError:
            if created_target:
                target.unlink(missing_ok=True)
            raise
        except Exception as exc:
            if created_target:
                target.unlink(missing_ok=True)
            raise AutomationError("BROWSER_SCREENSHOT_FAILED", f"Could not capture browser screenshot: {type(exc).__name__}: {exc}") from exc

    def _close_impl(self) -> None:
        """Close Playwright objects on their owning worker thread."""
        for item in (self._context, self._browser, self._playwright):
            try:
                if item is not None:
                    if item is self._playwright:
                        item.stop()
                    else:
                        item.close()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None
        self._browser_executable = None
        self._tab_ids.clear()
        self._wired_page_ids.clear()
        self._invalidate_element_refs()

    def close(self, timeout: float = 5.0) -> None:
        """Close the browser and stop its dedicated worker cleanly."""
        if self._worker_shutdown:
            return
        try:
            future = self._worker.submit(self._close_impl)
            future.result(timeout=timeout)
        except Exception:
            pass
        finally:
            self._worker_shutdown = True
            try:
                self._worker.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass


def _safe_observation_url(value: Any) -> str:
    """Keep diagnostics useful without collecting query-string tokens."""
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"[:2000]
    except Exception:
        pass
    return str(value or "")[:2000]


def _redact_observation_text(text: Any) -> str:
    """Redact obvious credentials and personal tokens from browser observations."""
    t = str(text or "")
    t = re.sub(r"(?i)\b(password|passwd|token|api[_-]?key|secret|auth(?:orization)?)\s*[:=]\s*\S+", r"\1=[redacted]", t)
    t = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]", t, flags=re.I)
    return t[:500]


def _validate_selector(selector: str) -> None:
    if not isinstance(selector, str) or not selector.strip() or len(selector) > 1000:
        raise AutomationError("INVALID_ARGUMENT", "A non-empty browser selector of at most 1000 characters is required.")


def _action_locator(page, selector: str, action: str):
    """Refuse ambiguous matches and fail fast if a selector cannot be inspected."""
    locator = page.locator(selector)
    try:
        # count() is a non-waiting query. If it throws, this normally means the
        # selector is malformed or the target page is gone; swallowing it would
        # turn an instant diagnostic into a 15-second click/fill timeout.
        count = locator.count()
    except Exception as exc:
        detail = " ".join(str(exc).split())[:240]
        raise AutomationError(
            "LOCATOR_QUERY_FAILED",
            f"Could not inspect the selector for {action}: {detail or type(exc).__name__}. Check the selector and current page, then retry.",
        ) from exc
    if count == 0:
        raise AutomationError("ELEMENT_NOT_FOUND", f"The {action} selector matched no elements on the current page. Take a fresh browser.snapshot and inspect the page before retrying.")
    if count > 1:
        raise AutomationError("AMBIGUOUS_LOCATOR", f"The {action} selector matched {count} elements. Use browser.snapshot and an inspected element ref, or narrow the selector to one target.")
    return locator.first


def register_browser_tools(registry: ToolRegistry, allowed_roots: list[str | Path] | tuple[str | Path, ...] | None = None, *, headless: bool = False, allow_local_urls: bool = False) -> BrowserSession:
    session = BrowserSession(headless=headless, allow_local_urls=allow_local_urls)
    roots = _root_paths(allowed_roots)
    registry.register(ToolSpec("browser.navigate", "Navigate the controlled Chromium session to an HTTP or HTTPS URL.", {
        "type": "object", "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["url"], "additionalProperties": False},
        ("browser.navigate",), "browser", timeout_seconds=45, idempotent=True,
        aliases=("navigate browser", "open url")),
        lambda ctx, args: session.run_serialized("navigate", args["url"]))
    registry.register(ToolSpec("browser.shop_and_add_to_cart", "Search for an item on an e-commerce shopping website (Amazon, Flipkart, eBay, Walmart, Target, Best Buy, or custom store), select a relevant product, add it to the shopping cart, and verify the cart update with evidence.", {
        "type": "object", "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "site": {"type": "string", "maxLength": 100},
            "url": {"type": "string", "maxLength": 4096}},
        "required": ["query"], "additionalProperties": False},
        ("browser.navigate", "browser.interact", "browser.read"), "browser", timeout_seconds=75, idempotent=False,
        risk=RiskLevel.MODERATE, requires_approval=False, aliases=("shop and add to cart", "add item to shopping cart", "add to cart")),
        lambda ctx, args: session.run_serialized("shop_and_add_to_cart", args["query"], args.get("site"), args.get("url")))
    registry.register(ToolSpec("browser.open_first_result", "Open the first visible organic result from a supported active Google, YouTube, Bing, or DuckDuckGo search-results page. Extracts the actual link, applies public-URL safety checks, and reports the final URL; does not guess a destination.", {
        "type": "object", "properties": {}, "additionalProperties": False},
        ("browser.navigate",), "browser", timeout_seconds=30, idempotent=False,
        aliases=("open first search result", "open first result", "visit first search result")),
        lambda ctx, args: session.run_serialized("open_first_result"))
    registry.register(ToolSpec("browser.open_result", "Open the specified visible organic result from the active supported search-results page. For YouTube playback requests, success requires observing an actively playing video element; opening a URL alone is not playback.", {
        "type": "object", "properties": {
            "index": {"type": "integer", "minimum": 1, "maximum": 10},
            "play": {"type": "boolean"}},
        "required": ["index"], "additionalProperties": False},
        ("browser.navigate", "browser.interact"), "browser", timeout_seconds=35, idempotent=False,
        aliases=("open search result", "play search result", "open result number", "play result number")),
        lambda ctx, args: session.run_serialized("open_result", args["index"], args.get("play", False)))
    registry.register(ToolSpec("browser.click", "Click exactly one element matched by a CSS or Playwright locator; fails fast for zero or multiple matches.", {
        "type": "object", "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 1000}},
        "required": ["selector"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, idempotent=False,
        risk=RiskLevel.HIGH, requires_approval=True, aliases=("click browser element",)),
        lambda ctx, args: session.run_serialized("click", args["selector"]))
    registry.register(ToolSpec("browser.fill", "Fill exactly one input matched by a CSS or Playwright locator; fails fast for zero or multiple matches.", {
        "type": "object", "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 1000},
        "value": {"type": "string", "maxLength": 20_000}}, "required": ["selector", "value"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, idempotent=True,
        aliases=("fill browser form",)),
        lambda ctx, args: session.run_serialized("fill", args["selector"], args["value"]))
    registry.register(ToolSpec("browser.text", "Read text from a locator in the controlled browser session.", {
        "type": "object", "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 1000},
        "max_chars": {"type": "integer", "minimum": 1, "maximum": 20000}},
        "additionalProperties": False}, ("browser.read",), "browser", timeout_seconds=20,
        aliases=("extract browser text",)),
        lambda ctx, args: session.run_serialized("text", args.get("selector", "body"), args.get("max_chars", 20_000)))
    registry.register(ToolSpec("browser.headings", "Extract visible H1/H2/H3 headings from the current controlled browser page.", {
        "type": "object", "properties": {"max_items": {"type": "integer", "minimum": 1, "maximum": 100}},
        "additionalProperties": False}, ("browser.read",), "browser", timeout_seconds=20,
        aliases=("extract page headings",)),
        lambda ctx, args: session.run_serialized("headings", args.get("max_items", 30)))
    registry.register(ToolSpec("browser.download_link", "Click one explicitly selected page element and save its browser download without overwriting an existing file.", {
        "type": "object", "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 4096},
        "selector": {"type": "string", "minLength": 1, "maxLength": 1000},
        "path": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["url", "selector", "path"], "additionalProperties": False},
        ("browser.navigate", "browser.interact", "filesystem.write"), "browser", timeout_seconds=90,
        idempotent=False, risk=RiskLevel.HIGH, requires_approval=True, aliases=("download browser link",)),
        lambda ctx, args: session.run_serialized("download_link", args["url"], args["selector"], args["path"], roots, ctx))
    registry.register(ToolSpec("browser.upload_file", "Attach a file to a validated HTML file input; does not submit the form.", {
        "type": "object", "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 4096},
        "selector": {"type": "string", "minLength": 1, "maxLength": 1000},
        "path": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["url", "selector", "path"], "additionalProperties": False},
        ("browser.navigate", "browser.interact", "filesystem.read"), "browser", timeout_seconds=40,
        idempotent=False, risk=RiskLevel.HIGH, requires_approval=True, aliases=("upload browser file",)),
        lambda ctx, args: session.run_serialized("upload_file", args["url"], args["selector"], args["path"], roots))
    registry.register(ToolSpec("browser.screenshot", "Capture the current controlled browser page to a new file.", {
        "type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["path"], "additionalProperties": False},
        ("browser.read", "filesystem.write"), "browser", timeout_seconds=30,
        aliases=("capture browser page",)),
        lambda ctx, args: session.run_serialized("screenshot", args["path"], roots))

    # High-value OpenCode-inspired browser contracts. These are registered in EVA's
    # existing schema/permission engine; no external browser/AI API is involved.
    registry.register(ToolSpec("browser.snapshot", "Inspect the active page's accessibility tree and visible interactive controls. Returns short-lived element refs for grounded follow-up actions.", {
        "type": "object", "properties": {"max_elements": {"type": "integer", "minimum": 1, "maximum": 100}, "max_chars": {"type": "integer", "minimum": 500, "maximum": 15000}}, "additionalProperties": False},
        ("browser.read",), "browser", timeout_seconds=25, aliases=("inspect browser page", "accessibility snapshot", "inspect webpage")),
        lambda ctx, args: session.run_serialized("snapshot", args.get("max_elements", 60), args.get("max_chars", 12000)))
    registry.register(ToolSpec("browser.click_element", "Click an element identified by the latest browser.snapshot. Requires both snapshot_id and ref; refs expire after page changes or new snapshots.", {
        "type": "object", "properties": {"snapshot_id": {"type": "integer", "minimum": 1}, "ref": {"type": "string", "minLength": 1, "maxLength": 40}}, "required": ["snapshot_id", "ref"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, idempotent=False, risk=RiskLevel.HIGH, requires_approval=True, aliases=("click inspected element",)),
        lambda ctx, args: session.run_serialized("click_element", args["ref"], args["snapshot_id"]))
    registry.register(ToolSpec("browser.fill_element", "Fill an inspected input from the latest browser.snapshot. Requires its snapshot_id and ref; refs expire after changes.", {
        "type": "object", "properties": {"snapshot_id": {"type": "integer", "minimum": 1}, "ref": {"type": "string", "minLength": 1, "maxLength": 40}, "value": {"type": "string", "maxLength": 20000}}, "required": ["snapshot_id", "ref", "value"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, aliases=("fill inspected input",)),
        lambda ctx, args: session.run_serialized("fill_element", args["ref"], args["snapshot_id"], args["value"]))
    registry.register(ToolSpec("browser.click_by_role", "Click a uniquely matched control by accessible role and accessible name, such as button named 'Sign in'. Refuses zero or multiple matches.", {
        "type": "object", "properties": {"role": {"type": "string", "enum": ["button", "link", "checkbox", "radio", "tab", "option", "menuitem"]}, "name": {"type": "string", "minLength": 1, "maxLength": 300}, "exact": {"type": "boolean"}}, "required": ["role", "name"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, idempotent=False, risk=RiskLevel.HIGH, requires_approval=True, aliases=("click button by name", "click link by name")),
        lambda ctx, args: session.run_serialized("click_by_role", args["role"], args["name"], args.get("exact", True)))
    registry.register(ToolSpec("browser.fill_by_label", "Fill exactly one form field by its accessible label. Refuses ambiguous or missing labels.", {
        "type": "object", "properties": {"label": {"type": "string", "minLength": 1, "maxLength": 300}, "value": {"type": "string", "maxLength": 20000}, "exact": {"type": "boolean"}}, "required": ["label", "value"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, aliases=("fill field by label", "enter value in labelled field")),
        lambda ctx, args: session.run_serialized("fill_by_label", args["label"], args["value"], args.get("exact", True)))
    registry.register(ToolSpec("browser.find_text", "Find visible occurrences of a phrase in the active page, returning bounded text matches for inspection.", {
        "type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 300}, "max_items": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["query"], "additionalProperties": False},
        ("browser.read",), "browser", timeout_seconds=20, aliases=("find text on page",)),
        lambda ctx, args: session.run_serialized("find_text", args["query"], args.get("max_items", 20)))
    registry.register(ToolSpec("browser.press", "Send a named Playwright keyboard key to a specific page locator. Potential submission/navigation keys require approval.", {
        "type": "object", "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 1000}, "key": {"type": "string", "minLength": 1, "maxLength": 40}}, "required": ["selector", "key"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, idempotent=False, risk=RiskLevel.HIGH, requires_approval=True, aliases=("press browser key",)),
        lambda ctx, args: session.run_serialized("press", args["selector"], args["key"]))
    registry.register(ToolSpec("browser.hover", "Hover over exactly one element matched by a CSS or Playwright locator; fails fast for zero or multiple matches.", {
        "type": "object", "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 1000}}, "required": ["selector"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, aliases=("hover browser element",)),
        lambda ctx, args: session.run_serialized("hover", args["selector"]))
    registry.register(ToolSpec("browser.select_option", "Select one option in a page select control using its exact value or visible label.", {
        "type": "object", "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 1000}, "value": {"type": "string", "maxLength": 500}, "label": {"type": "string", "maxLength": 500}}, "required": ["selector"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, aliases=("select dropdown option",)),
        lambda ctx, args: session.run_serialized("select_option", args["selector"], args.get("value"), args.get("label")))
    registry.register(ToolSpec("browser.set_checked", "Set a checkbox or radio input to a specific checked state.", {
        "type": "object", "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 1000}, "checked": {"type": "boolean"}}, "required": ["selector"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=20, aliases=("check browser option", "uncheck browser option")),
        lambda ctx, args: session.run_serialized("set_checked", args["selector"], args.get("checked", True)))
    registry.register(ToolSpec("browser.scroll", "Scroll the active page by a bounded distance and direction.", {
        "type": "object", "properties": {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "amount": {"type": "integer", "minimum": 50, "maximum": 2000}}, "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=15, aliases=("scroll webpage",)),
        lambda ctx, args: session.run_serialized("scroll", args.get("direction", "down"), args.get("amount", 650)))
    registry.register(ToolSpec("browser.wait_for", "Wait for a selector to become attached, detached, visible, or hidden. Uses a bounded condition wait instead of arbitrary sleep delays.", {
        "type": "object", "properties": {"selector": {"type": "string", "minLength": 1, "maxLength": 1000}, "state": {"type": "string", "enum": ["attached", "detached", "visible", "hidden"]}, "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 15000}}, "required": ["selector"], "additionalProperties": False},
        ("browser.read",), "browser", timeout_seconds=18, aliases=("wait for browser element",)),
        lambda ctx, args: session.run_serialized("wait_for", args["selector"], args.get("state", "visible"), args.get("timeout_ms", 10000)))
    for action in ("back", "forward", "reload"):
        registry.register(ToolSpec(f"browser.{action}", f"Navigate the active browser tab {action} one history entry or reload the current page.", {
            "type": "object", "properties": {}, "additionalProperties": False},
            ("browser.navigate",), "browser", timeout_seconds=25, aliases=((f"browser go {action}"),)),
            lambda ctx, args, act=action: session.run_serialized("_history_action", act))
    registry.register(ToolSpec("browser.tabs.list", "List tabs in EVA's isolated controlled browser session, including IDs, titles, URLs, and the active tab.", {
        "type": "object", "properties": {}, "additionalProperties": False}, ("browser.read",), "browser", timeout_seconds=15, aliases=("list browser tabs",)),
        lambda ctx, args: session.run_serialized("tabs_list"))
    registry.register(ToolSpec("browser.tabs.open", "Open a new tab in EVA's isolated controlled browser session, optionally navigating it to a public HTTP/HTTPS URL.", {
        "type": "object", "properties": {"url": {"type": "string", "maxLength": 4096}}, "additionalProperties": False},
        ("browser.navigate",), "browser", timeout_seconds=45, aliases=("open new browser tab",)),
        lambda ctx, args: session.run_serialized("open_tab", args.get("url")))
    registry.register(ToolSpec("browser.tabs.focus", "Switch the active tab in EVA's controlled browser using an ID returned by browser.tabs.list.", {
        "type": "object", "properties": {"tab_id": {"type": "string", "minLength": 1, "maxLength": 40}}, "required": ["tab_id"], "additionalProperties": False},
        ("browser.navigate",), "browser", timeout_seconds=15, aliases=("focus browser tab",)),
        lambda ctx, args: session.run_serialized("focus_tab", args["tab_id"]))
    registry.register(ToolSpec("browser.tabs.close", "Close a specific tab in EVA's controlled browser. Requires explicit approval because a tab may contain unsaved work.", {
        "type": "object", "properties": {"tab_id": {"type": "string", "minLength": 1, "maxLength": 40}}, "required": ["tab_id"], "additionalProperties": False},
        ("browser.interact",), "browser", timeout_seconds=15, idempotent=False, risk=RiskLevel.HIGH, requires_approval=True, aliases=("close browser tab",)),
        lambda ctx, args: session.run_serialized("close_tab", args["tab_id"]))
    registry.register(ToolSpec("browser.frames.list", "List the active page's frame names and URLs without reading frame contents.", {
        "type": "object", "properties": {}, "additionalProperties": False}, ("browser.read",), "browser", timeout_seconds=15, aliases=("list page frames",)),
        lambda ctx, args: session.run_serialized("frames_list"))
    registry.register(ToolSpec("browser.diagnostics", "Read bounded browser console errors, page errors, failed requests and HTTP error responses. Never captures request bodies, headers, or cookies.", {
        "type": "object", "properties": {"max_items": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False},
        ("browser.read",), "browser", timeout_seconds=15, aliases=("inspect browser errors",)),
        lambda ctx, args: session.run_serialized("diagnostics", args.get("max_items", 50)))
    return session
