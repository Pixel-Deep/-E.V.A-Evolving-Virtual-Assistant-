"""Regression tests for selectively integrated OpenCode-inspired browser contracts."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eva.automation.engine.models import AutomationError
from eva.automation.service import AutomationService
from eva.automation.task_planner import _tool_rows
from eva.automation.tools.browser import BrowserSession, _action_locator, _find_system_chromium_executable


_INTERACTIVE = "button, a, input, textarea, select, [role=button], [role=link], [role=checkbox], [role=radio], [contenteditable=true], [tabindex]:not([tabindex='-1'])"


class FakeElementLocator:
    def __init__(self, page, index=None, selector=""):
        self.page = page
        self.index = index
        self.selector = selector

    def count(self):
        if self.index is None:
            return len(self.page.elements)
        return len(self.page.elements)

    def nth(self, index):
        return FakeElementLocator(self.page, index, self.selector)

    def is_visible(self, timeout=None):
        return bool(self.page.elements[self.index].get("visible", True))

    def is_enabled(self, timeout=None):
        return bool(self.page.elements[self.index].get("enabled", True))

    def evaluate(self, script):
        if "tagName.toLowerCase()" in script and "const clean" not in script:
            return self.page.elements[self.index]["descriptor"]["tag"]
        return dict(self.page.elements[self.index]["descriptor"])

    def click(self, timeout=None):
        self.page.clicked = self.index

    def fill(self, value, timeout=None):
        self.page.filled = (self.index, value)

    def inner_text(self, timeout=None):
        return self.page.elements[self.index]["descriptor"].get("text", "")

    def aria_snapshot(self, timeout=None):
        return "- button \"Sign in\"\n- textbox \"Email\""


class FakePage:
    def __init__(self):
        self.url = "https://example.com/login"
        self.title_value = "Example Login"
        self.elements = [
            {"visible": True, "enabled": True, "descriptor": {
                "tag": "button", "role": "button", "name": "Sign in", "text": "Sign in",
                "type": "", "href": "", "placeholder": ""}},
            {"visible": True, "enabled": True, "descriptor": {
                "tag": "input", "role": "textbox", "name": "Email", "text": "",
                "type": "email", "href": "", "placeholder": "Email address"}},
        ]
        self.clicked = None
        self.filled = None

    def is_closed(self):
        return False

    def title(self):
        return self.title_value

    def locator(self, selector):
        if selector == "body":
            return FakeElementLocator(self, 0, selector)
        return FakeElementLocator(self, None, selector)


class BrowserIntegrationTests(unittest.TestCase):
    def test_public_browser_tools_are_registered_in_evas_existing_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ("Desktop", "Documents", "Downloads"):
                (root / folder).mkdir()
            service = AutomationService(allowed_roots=[root], audit_path=None,
                                        workflows_dir=root / "workflows",
                                        capability_gaps_path=root / "state" / "gaps.json",
                                        browser_headless=True)
            try:
                names = set(service.registry.names())
                expected = {
                    "browser.snapshot", "browser.click_element", "browser.fill_element",
                    "browser.click_by_role", "browser.fill_by_label", "browser.find_text", "browser.press", "browser.hover", "browser.select_option",
                    "browser.set_checked", "browser.scroll", "browser.wait_for", "browser.back",
                    "browser.forward", "browser.reload", "browser.tabs.list", "browser.tabs.open",
                    "browser.tabs.focus", "browser.tabs.close", "browser.frames.list", "browser.diagnostics",
                }
                self.assertTrue(expected.issubset(names), sorted(expected - names))
                self.assertEqual(service.registry.get("browser.click_element").spec.risk.value, "high")
                self.assertTrue(service.registry.get("browser.click_element").spec.requires_approval)
                self.assertTrue(service.registry.get("browser.tabs.close").spec.requires_approval)
                denied_click = service.execute_tool("browser.click_element", {"snapshot_id": 1, "ref": "s1-e1"})
                denied_close = service.execute_tool("browser.tabs.close", {"tab_id": "tab-1"})
                self.assertEqual(denied_click.error_code, "APPROVAL_REQUIRED")
                self.assertEqual(denied_close.error_code, "APPROVAL_REQUIRED")
            finally:
                service.close()

    def test_planner_shortlists_snapshot_and_grounded_actions_for_page_interaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AutomationService(allowed_roots=[root], audit_path=None,
                                        workflows_dir=root / "workflows",
                                        capability_gaps_path=root / "state" / "gaps.json",
                                        browser_headless=True)
            try:
                rows = _tool_rows(service, "Inspect the website and click the sign in button")
                names = {item["name"] for item in rows}
                self.assertIn("browser.snapshot", names)
                self.assertIn("browser.click_element", names)
                self.assertIn("browser.click_by_role", names)
                self.assertIn("browser.fill_by_label", names)
                self.assertIn("browser.wait_for", names)
                self.assertLessEqual(len(rows), 16)
            finally:
                service.close()

    def test_snapshot_refs_work_only_while_snapshot_descriptor_is_still_current(self):
        session = BrowserSession(headless=True)
        page = FakePage()
        session._ensure_page = lambda: page
        session._page = page
        try:
            snapshot = session.snapshot()
            self.assertIn("Sign in", snapshot["accessibility_tree"])
            self.assertEqual(snapshot["element_count"], 2)
            button = next(item for item in snapshot["elements"] if item["name"] == "Sign in")
            result = session.click_element(button["ref"], snapshot["snapshot_id"])
            self.assertTrue(result["ok"])
            self.assertEqual(page.clicked, 0)
            with self.assertRaises(AutomationError) as stale:
                session.click_element(button["ref"], snapshot["snapshot_id"])
            self.assertEqual(stale.exception.code, "STALE_ELEMENT_REF")

            snapshot2 = session.snapshot()
            email = next(item for item in snapshot2["elements"] if item["name"] == "Email")
            page.elements[1]["descriptor"]["name"] = "Password"
            with self.assertRaises(AutomationError) as changed:
                session.fill_element(email["ref"], snapshot2["snapshot_id"], "example@example.com")
            self.assertEqual(changed.exception.code, "STALE_ELEMENT_REF")
        finally:
            session.close()

    def test_private_and_unresolvable_browser_destinations_are_blocked_by_default(self):
        session = BrowserSession(headless=True)
        try:
            for url in ("http://127.0.0.1:8000", "http://localhost/admin", "https://router.local/"):
                with self.subTest(url=url), self.assertRaises(AutomationError) as raised:
                    session._validate_destination(url)
                self.assertEqual(raised.exception.code, "UNSAFE_URL")
            with patch("eva.automation.tools.browser._host_is_public", return_value=False):
                with self.assertRaises(AutomationError):
                    session._validate_destination("https://internal.example.invalid/")
            local_session = BrowserSession(allow_local_urls=True)
            try:
                self.assertEqual(local_session._validate_destination("http://127.0.0.1:8000"), "http://127.0.0.1:8000")
            finally:
                local_session.close()
        finally:
            session.close()


    def test_selector_based_actions_reject_ambiguous_targets(self):
        class ManyMatches:
            def count(self): return 3
            @property
            def first(self): return self
        class FakePageForLocator:
            def locator(self, selector): return ManyMatches()
        with self.assertRaises(AutomationError) as raised:
            _action_locator(FakePageForLocator(), "button", "click")
        self.assertEqual(raised.exception.code, "AMBIGUOUS_LOCATOR")
        self.assertIn("3 elements", raised.exception.message)

    def test_selector_with_no_matches_fails_fast(self):
        class NoMatches:
            def count(self): return 0
            @property
            def first(self): return self
        class FakePageForLocator:
            def locator(self, selector): return NoMatches()
        with self.assertRaises(AutomationError) as raised:
            _action_locator(FakePageForLocator(), "button[data-missing]", "click")
        self.assertEqual(raised.exception.code, "ELEMENT_NOT_FOUND")

    def test_selector_query_errors_fail_fast_instead_of_waiting_for_action_timeout(self):
        class BrokenLocator:
            def count(self):
                raise ValueError("Unexpected token in selector")
        class FakePageForLocator:
            def locator(self, selector): return BrokenLocator()
        with self.assertRaises(AutomationError) as raised:
            _action_locator(FakePageForLocator(), "button[", "click")
        self.assertEqual(raised.exception.code, "LOCATOR_QUERY_FAILED")
        self.assertIn("Unexpected token", raised.exception.message)

    def test_malformed_and_out_of_range_ports_return_structured_argument_errors(self):
        session = BrowserSession(headless=True)
        try:
            for url in ("https://example.com:abc/", "https://example.com:99999/", "https://example.com:0/"):
                with self.subTest(url=url), self.assertRaises(AutomationError) as raised:
                    session._validate_destination(url)
                self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
        finally:
            session.close()

    def test_system_browser_fallback_finds_an_installed_chromium_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "chromium"
            executable.write_text("placeholder", encoding="utf-8")
            with patch("eva.automation.tools.browser.shutil.which",
                       side_effect=lambda name: str(executable) if name == "chromium" else None):
                self.assertEqual(_find_system_chromium_executable(), str(executable))

    def test_invalid_navigation_port_returns_tool_error_without_starting_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AutomationService(allowed_roots=[root], audit_path=None,
                                        workflows_dir=root / "workflows",
                                        capability_gaps_path=root / "state" / "gaps.json",
                                        browser_headless=True)
            try:
                with patch.object(service.browser_session, "_ensure_page", side_effect=AssertionError("browser should not start")):
                    result = service.execute_tool("browser.navigate", {"url": "https://example.com:99999/"})
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "INVALID_ARGUMENT")
            finally:
                service.close()

    def test_diagnostics_does_not_create_a_blank_page_or_claim_no_errors(self):
        session = BrowserSession(headless=True)
        try:
            result = session.diagnostics()
            self.assertFalse(result["page_loaded"])
            self.assertFalse(result["diagnostics_available"])
            self.assertIsNone(result["url"])
            self.assertIn("No active page", result["message"])
            self.assertIsNone(session._page, "Read-only diagnostics must not launch a browser or create about:blank.")
        finally:
            session.close()

    def test_default_browser_url_launch_uses_the_same_private_destination_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AutomationService(allowed_roots=[root], audit_path=None,
                                        workflows_dir=root / "workflows",
                                        capability_gaps_path=root / "state" / "gaps.json",
                                        browser_headless=True)
            try:
                with patch("eva.automation.service.webbrowser.open") as open_browser:
                    result = service.execute_tool("desktop.open_url", {"url": "http://127.0.0.1:8000/admin"})
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "UNSAFE_URL")
                open_browser.assert_not_called()
            finally:
                service.close()

    def test_request_guard_aborts_private_redirect_or_subresource_before_continuing(self):
        class FakeRoute:
            def __init__(self): self.aborted = None; self.continued = False
            def abort(self, reason=None): self.aborted = reason
            def continue_(self): self.continued = True
        class FakeRequest:
            url = "http://127.0.0.1:8000/private"
        session = BrowserSession(headless=True)
        route = FakeRoute()
        try:
            session._guard_route(route, FakeRequest())
            self.assertEqual(route.aborted, "blockedbyclient")
            self.assertFalse(route.continued)
            diagnostic = session.diagnostics.__name__
            self.assertEqual(diagnostic, "diagnostics")
            self.assertTrue(any(item["kind"] == "blocked_request" for item in session._observations))
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
