"""API-free public browsing and latest user-reported regressions."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eva.automation.content_contracts import build_content_brief, validate_generated_content
from eva.automation.service import AutomationService
from eva.automation.task_planner import TaskPlanner, _extract_web_browse_request, _requested_filename
from eva.core.intent_understanding import analyze_intent
from eva.core.task_router import TaskRouter
from eva.tools.web_search import search_web_results


class SequencedProvider:
    def __init__(self, *outputs: str):
        self.outputs = list(outputs)
        self.text_calls = 0
        self.plan_calls = 0

    def generate_text(self, system_prompt, user_prompt, *, timeout=45, num_predict=1200):
        self.text_calls += 1
        if self.outputs:
            return self.outputs.pop(0)
        return ""

    def generate_project_json(self, *args, **kwargs):
        self.plan_calls += 1
        return '{"goal":"unused","steps":[],"needs_input":[],"missing_capabilities":[]}'


class FakeHTMLResponse:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit=-1): return self.body if limit < 0 else self.body[:limit]


class ApiFreeBrowsingAndRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-web-browse-regression-")
        self.root = Path(self.temp.name).resolve()
        for name in ("Desktop", "Documents", "Downloads"):
            (self.root / name).mkdir()
        self.service = AutomationService(
            allowed_roots=[self.root], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "state" / "gaps.json",
            browser_headless=True,
        )
        self.planner = TaskPlanner(self.service, None, self.service.capability_gaps)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_public_html_search_uses_normal_html_page_not_external_api(self):
        body = '<div class="result"><a class="result__a" href="https://example.com/python">Python downloads</a><a class="result__snippet">Official release information and downloads.</a></div>'
        with patch("eva.tools.web_search.urllib.request.urlopen", return_value=FakeHTMLResponse(body)) as fetch:
            result = search_web_results("latest stable Python release", max_results=3)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_mode"], "public_html_search")
        self.assertEqual(result["results"][0]["url"], "https://example.com/python")
        requested_url = fetch.call_args.args[0].full_url
        self.assertIn("https://html.duckduckgo.com/html/?", requested_url)
        self.assertNotIn("/api/", requested_url.casefold())

    def test_public_html_search_falls_back_to_second_html_engine_without_api(self):
        body = '<li class="b_algo"><h2><a href="https://www.python.org/downloads/">Python Downloads</a></h2></li>'
        def fetch(request, timeout=0):
            url = request.full_url
            if "html.duckduckgo.com" in url:
                raise OSError("primary search unavailable")
            if "bing.com/search" in url:
                return FakeHTMLResponse(body)
            raise AssertionError("Unexpected search source: " + url)
        with patch("eva.tools.web_search.urllib.request.urlopen", side_effect=fetch) as mocked:
            result = search_web_results("Python downloads", max_results=3)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_mode"], "fallback_html_search")
        self.assertEqual(result["results"][0]["url"], "https://www.python.org/downloads/")
        self.assertTrue(result.get("warning"))
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(all("/api/" not in call.args[0].full_url.casefold() for call in mocked.call_args_list))

    def test_public_browse_tool_reads_real_page_text_from_browser_tool_results(self):
        def fake_search(query, max_results=5):
            return {"ok": True, "query": query, "source_mode": "public_html_search",
                    "search_url": "https://html.duckduckgo.com/html/?q=sample",
                    "results": [{"title": "Sample page", "url": "https://example.com/sample",
                                 "snippet": "A sample result."}]}
        def fake_browser(method, *args, **kwargs):
            if method == "navigate":
                return {"ok": True, "url": "https://example.com/sample", "title": "Sample page", "http_status": 200}
            if method == "text":
                return {"ok": True, "url": "https://example.com/sample", "title": "Sample page",
                        "text": "This is the actual readable page body.", "truncated": False}
            if method == "headings":
                return {"ok": True, "headings": ["Sample heading"]}
            raise AssertionError(method)
        with patch("eva.tools.web_search.search_web_results", side_effect=fake_search), \
             patch.object(self.service.browser_session, "run_serialized", side_effect=fake_browser) as browser_call:
            result = self.service.execute_tool("web.browse", {"query": "sample topic", "max_pages": 1,
                                                               "max_chars_per_page": 1000})
        self.assertTrue(result.ok, result.as_dict())
        self.assertTrue(result.verified)
        self.assertIn("actual readable page body", result.data["research_text"])
        self.assertEqual(result.data["page_count"], 1)
        self.assertEqual(result.data["pages"][0]["headings"], ["Sample heading"])
        self.assertEqual([c.args[0] for c in browser_call.call_args_list], ["navigate", "text", "headings"])

    def test_browse_summarize_and_save_runs_end_to_end_without_external_api(self):
        def fake_search(query, max_results=5):
            return {"ok": True, "query": query, "source_mode": "public_html_search",
                    "search_url": "https://html.duckduckgo.com/html/?q=shortcuts",
                    "results": [{"title": "Official keyboard shortcuts", "url": "https://example.com/shortcuts",
                                 "snippet": "Official shortcut list."}]}
        def fake_browser(method, *args, **kwargs):
            if method == "navigate":
                return {"ok": True, "url": "https://example.com/shortcuts", "title": "Official keyboard shortcuts", "http_status": 200}
            if method == "text":
                return {"ok": True, "url": "https://example.com/shortcuts", "title": "Official keyboard shortcuts",
                        "text": "Ctrl+C copies. Ctrl+V pastes. Ctrl+Z undoes the last action.", "truncated": False}
            if method == "headings":
                return {"ok": True, "headings": ["Keyboard shortcuts"]}
            raise AssertionError(method)
        summary = ("The page identifies Ctrl+C as copy, Ctrl+V as paste, and Ctrl+Z as undo. "
                   "Source: https://example.com/shortcuts")
        provider = SequencedProvider(summary)
        prompt = ("Browse the web for official Windows keyboard shortcuts and summarize it "
                  "to a file named windows-shortcuts-research.txt on Desktop")
        with patch("eva.tools.web_search.search_web_results", side_effect=fake_search), \
             patch.object(self.service.browser_session, "run_serialized", side_effect=fake_browser):
            result = self.service.plan_and_execute(prompt, provider)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(provider.text_calls, 1)
        target = self.root / "Desktop" / "windows-shortcuts-research.txt"
        self.assertTrue(target.is_file())
        saved = target.read_text(encoding="utf-8")
        self.assertEqual(saved, summary)
        self.assertIn("https://example.com/shortcuts", provider.outputs[0] if provider.outputs else saved)
        steps = result["execution"]["steps"]
        self.assertEqual([step["tool"] for step in steps], ["web.browse", "content.generate_text", "files.create_unique_text_file"])
        self.assertTrue(all(step["status"] == "succeeded" for step in steps))

    def test_browse_summarize_and_save_uses_honest_extractive_fallback_when_local_model_unavailable(self):
        def fake_search(query, max_results=5):
            return {"ok": True, "query": query, "source_mode": "public_html_search",
                    "search_url": "https://html.duckduckgo.com/html/?q=python",
                    "results": [{"title": "Python guide", "url": "https://example.com/python-guide",
                                 "snippet": "A guide to Python."}]}
        def fake_browser(method, *args, **kwargs):
            if method == "navigate":
                return {"ok": True, "url": "https://example.com/python-guide", "title": "Python guide", "http_status": 200}
            if method == "text":
                return {"ok": True, "url": "https://example.com/python-guide", "title": "Python guide",
                        "text": "Python is a programming language used for many tasks. It has a readable syntax that helps beginners learn. The guide covers functions, modules, and file input and output.",
                        "truncated": False}
            if method == "headings":
                return {"ok": True, "headings": ["Python basics", "Functions", "Files"]}
            raise AssertionError(method)
        prompt = ("Search the web for an introduction to Python, open a useful result, "
                  "extract its main headings and useful information, then save a short report "
                  "to a file named python-intro-report.txt on Desktop")
        with patch("eva.tools.web_search.search_web_results", side_effect=fake_search), \
             patch.object(self.service.browser_session, "run_serialized", side_effect=fake_browser):
            result = self.service.plan_and_execute(prompt, provider=None)
        self.assertTrue(result.get("ok"), result)
        target = self.root / "Desktop" / "python-intro-report.txt"
        self.assertTrue(target.is_file())
        saved = target.read_text(encoding="utf-8")
        self.assertIn("Offline extractive report", saved)
        self.assertIn("Python basics", saved)
        self.assertIn("https://example.com/python-guide", saved)
        steps = result["execution"]["steps"]
        self.assertEqual([step["tool"] for step in steps], [
            "web.browse", "content.generate_text", "files.create_unique_text_file"
        ])
        summary_step = next(step for step in steps if step["tool"] == "content.generate_text")
        self.assertTrue(summary_step["result"]["data"].get("model_used") is False)

    def test_web_browse_rejects_private_and_local_urls_before_browser(self):
        for url in ("http://127.0.0.1/admin", "http://localhost:8080", "file:///C:/secret.txt", "https://user:pass@example.com"):
            with self.subTest(url=url), patch.object(self.service.browser_session, "run_serialized") as browser:
                result = self.service.execute_tool("web.browse", {"url": url})
                self.assertFalse(result.ok, result.as_dict())
                self.assertFalse(browser.called)

    def test_registered_browse_tool_is_api_key_free_and_has_usage_contract(self):
        tool_names = set(self.service.registry.names())
        self.assertIn("web.browse", tool_names)
        self.assertIn("web.search", tool_names)
        self.assertNotIn("network.public_ip", tool_names)
        spec = self.service.registry.get("web.browse").spec
        self.assertIn("no external api", spec.description.casefold())
        self.assertIn("query", spec.input_schema["properties"])
        self.assertIn("url", spec.input_schema["properties"])

    def test_browse_then_summarize_and_save_has_real_dependencies_and_exact_filename(self):
        prompt = "Browse the web for the latest stable Python release and summarize it to a file named python-research.txt"
        parsed = _extract_web_browse_request(prompt)
        self.assertEqual(parsed["query"], "latest stable Python release")
        self.assertTrue(parsed["wants_summary"])
        self.assertTrue(parsed["wants_save"])
        self.assertEqual(parsed["output_name"], "python-research.txt")
        plan = self.planner._deterministic_plan(prompt)
        self.assertEqual([step["tool"] for step in plan["steps"]], [
            "web.browse", "content.generate_text", "files.create_unique_text_file"
        ])
        steps = {step["id"]: step for step in plan["steps"]}
        self.assertEqual(steps["summary"]["depends_on"], ["browse"])
        self.assertEqual(steps["save_research"]["depends_on"], ["summary"])
        self.assertEqual(steps["save_research"]["args"]["filename"], "python-research.txt")
        self.assertEqual(steps["save_research"]["args"]["content"], "${steps.summary.data.text}")

    def test_direct_url_read_and_summary_uses_browse_not_search_api(self):
        prompt = "Open https://www.python.org/downloads/ and summarize this page"
        plan = self.planner._deterministic_plan(prompt)
        self.assertEqual(plan["steps"][0]["tool"], "web.browse")
        self.assertEqual(plan["steps"][0]["args"]["url"], "https://www.python.org/downloads/")
        self.assertEqual(plan["steps"][1]["tool"], "content.generate_text")
        self.assertIn("${steps.browse.data.research_text}", plan["steps"][1]["args"]["prompt"])

    def test_research_intent_routes_to_browse_orchestration(self):
        for prompt in (
            "Please research online the latest Python stable release.",
            "Browse the web for official Windows keyboard shortcuts.",
            "Read https://example.com and summarize the page.",
            "Find the official Microsoft page for Windows keyboard shortcuts.",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(analyze_intent(prompt).intent, "web_browsing")
                self.assertEqual(TaskRouter().route(prompt).handler, "desktop_automation")

    def test_spelling_recovery_and_one_bounded_content_repair(self):
        prompt = "open notepad and write a story about a school bou"
        brief = build_content_brief(prompt)
        self.assertEqual(brief["output_kind"], "story")
        self.assertEqual(brief["topic"], "a school boy")
        bad = "A strange School Bou was a magical bouquet that made a girl happy."
        good = "A schoolboy named Ravi studied hard, helped his classmates, and won the school science fair."
        provider = SequencedProvider(bad, good)
        calls: list[tuple[str, str, dict]] = []
        def desktop_execute(action, value="", **kwargs):
            calls.append((action, value, kwargs))
            return {"ok": True, "verified": True, "message": "mocked UI result"}
        with patch("eva.automation.desktop.execute", side_effect=desktop_execute):
            result = self.service.handle_text(prompt, provider=provider)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(provider.text_calls, 2)
        typed = [call[1] for call in calls if call[0] == "type"]
        self.assertEqual(typed, [good])
        self.assertNotIn("School Bou Adventure", typed[0])

    def test_school_boy_story_validator_accepts_closed_compound(self):
        good = "A schoolboy named Ravi studied hard and helped his classmates."
        self.assertIsNone(validate_generated_content(good, "story", "a school boy"))
        bad = "A magical bouquet of flowers made everyone happy."
        self.assertIsNotNone(validate_generated_content(bad, "story", "a school boy"))

    def test_pptx_read_request_uses_deterministic_filename_and_reader(self):
        prompt = "open IT.pptx from my desktop and read the file"
        self.assertEqual(_requested_filename(prompt), "IT.pptx")
        plan = self.planner._deterministic_plan(prompt)
        self.assertIsNotNone(plan)
        self.assertEqual([step["tool"] for step in plan["steps"]], ["files.resolve_by_name", "files.read_document"])
        self.assertEqual(plan["steps"][0]["args"]["filename"], "IT.pptx")
        self.assertEqual(plan["steps"][1]["args"]["path"], "${steps.find_file.data.path}")

    def test_shopping_list_request_never_emits_unresolved_python_expression(self):
        prompt = "note notepad and list the items i have to buy are apples and mango"
        plan = self.planner._deterministic_plan(prompt)
        self.assertEqual([step["tool"] for step in plan["steps"]], [
            "desktop.open_target", "desktop.new_notepad_document", "desktop.type_notepad"
        ])
        self.assertEqual(plan["steps"][2]["args"]["value"], "Items to buy:\n- apples\n- mango")
        self.assertEqual(TaskPlanner._find_unsupported_references(plan), [])
        self.assertNotIn("${user_goal.split(' ')[-3]}", str(plan))

    def test_integrated_web_browse_audit_requires_verified_real_page_result(self):
        from types import SimpleNamespace
        from scripts import eva_runtime_tool_audit as audit

        good_result = SimpleNamespace(
            ok=True, verified=True, data={
                "page_count": 1,
                "research_text": "SOURCE: Python downloads\nURL: https://www.python.org/downloads/\nPage text",
                "pages": [{"title": "Python downloads", "url": "https://www.python.org/downloads/", "text": "Page text"}],
                "source_mode": "public_html_search",
            }, message="read page", error_code=None,
        )
        with patch.object(audit.AutomationService, "execute_tool", return_value=good_result) as execute:
            result = audit._probe_web_browse()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(execute.call_args.args[0], "web.browse")
        self.assertFalse(result["uses_external_search_api"])

        bad_result = SimpleNamespace(ok=True, verified=False, data={"page_count": 0, "research_text": "", "pages": []},
                                     message="no verified page", error_code="WEB_PAGE_READ_FAILED")
        with patch.object(audit.AutomationService, "execute_tool", return_value=bad_result):
            result = audit._probe_web_browse()
        self.assertEqual(result["status"], "FAIL", result)

    def test_planner_strips_only_known_metadata_and_rejects_executable_templates(self):
        payload = {"goal": "read presentation", "steps": [], "needs_input": [], "missing_capabilities": [],
                   "approval": "low", "name": "read", "output": {}, "params": {}, "purpose": "test",
                   "required": [], "risk": "low", "side_effects": "none"}
        normalized = TaskPlanner._normalize_plan_envelope(payload)
        self.assertEqual(set(normalized), {"goal", "steps", "needs_input", "missing_capabilities"})
        self.assertEqual(TaskPlanner._find_unsupported_references({"value": "${user_goal.split(' ')[-3]}"}),
                         ["${user_goal.split(' ')[-3]}"])

    def test_public_browsing_does_not_depend_on_external_api_credentials_or_sdk(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2] / "eva"
        source = "\n".join(path.read_text(encoding="utf-8", errors="ignore")
                           for path in root.rglob("*.py") if "__pycache__" not in path.parts)
        forbidden = (
            "api64.ipify.org", "api.open-meteo.com", "api.spotify.com",
            "accounts.spotify.com/api", "checkip.amazonaws.com",
        )
        for endpoint in forbidden:
            self.assertNotIn(endpoint, source)
        audit_source = (Path(__file__).resolve().parents[2] / "scripts" / "eva_runtime_tool_audit.py").read_text(encoding="utf-8")
        self.assertNotIn("spotify_api_credentials_configured", audit_source)
        self.assertIn("--probe-web-browse", audit_source)

    def test_network_url_check_blocks_private_targets_and_pins_dns(self):
        from eva.automation.engine.models import AutomationError
        for private_url in ("http://127.0.0.1:8080", "http://localhost:3000", "http://192.168.1.1/admin"):
            with self.subTest(url=private_url):
                res = self.service.execute_tool("network.url_check", {"url": private_url})
                self.assertFalse(res.ok)
                self.assertEqual(res.error_code, "URL_BLOCKED")

