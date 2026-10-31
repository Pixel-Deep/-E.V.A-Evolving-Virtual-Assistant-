"""Cross-component regressions for EVA's recurring automation failures.

These tests focus on preserving the user's entire requested outcome before any
side effect, not merely producing a schema-valid or locally successful plan.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eva.automation.service import (
    AutomationService,
    _extractive_summary_from_source,
    _has_wikipedia_article_prose,
    _offline_research_summary,
)
from eva.automation.content_contracts import validate_generated_content
from eva.automation.engine import ExecutionContext
from eva.automation.task_planner import (
    TaskPlanner,
    _extract_ranked_search_task,
    _extract_web_browse_request,
    _request_outcome_coverage_issues,
)
from eva.automation.tools.browser import BrowserSession
from eva.core.intent_understanding import is_compound_web_task
from eva.core.task_router import TaskRouter


class _FakeResultLink:
    def __init__(self, href: str, title: str, visible: bool = True):
        self.href = href
        self.title = title
        self.visible = visible

    def is_visible(self, timeout: int = 0) -> bool:
        return self.visible

    def get_attribute(self, name: str):
        return self.href if name == "href" else None

    def inner_text(self, timeout: int = 0) -> str:
        return self.title


class _FakeLocator:
    def __init__(self, items):
        self.items = items

    def count(self) -> int:
        return len(self.items)

    def nth(self, index: int):
        return self.items[index]


class _FakePage:
    def __init__(self, url: str, links: dict[str, list[_FakeResultLink]]):
        self.url = url
        self.links = links

    def locator(self, selector: str):
        return _FakeLocator(self.links.get(selector, []))




class _FakeResponse:
    def __init__(self, status=200):
        self.status = status


class _FakeNavigationPage:
    def __init__(self):
        self.url = "about:blank"
        self._title = ""
        self._body = ""
        self.goto_calls = []
        self.results = [_FakeResultLink("https://example.org/result", "Result")]

    def goto(self, url: str, **kwargs):
        self.goto_calls.append(url)
        if "google.com/search" in url:
            self.url = "https://www.google.com/sorry/index?continue=" + url
            self._title = "Unusual traffic"
            self._body = "Our systems have detected unusual traffic from your computer network."
        else:
            self.url = url
            self._title = "DuckDuckGo Search"
            self._body = "Search results for the requested query."
        return _FakeResponse(200)

    def title(self):
        return self._title

    def locator(self, selector: str):
        if selector == "body":
            return _FakeBodyLocator(self._body)
        if selector == "a.result__a, a.result-link":
            return _FakeLocator(self.results)
        return _FakeLocator([])


class _FakeBodyLocator:
    def __init__(self, text: str):
        self._text = text

    def inner_text(self, timeout: int = 0) -> str:
        return self._text


class AutomationRootCauseRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-root-cause-regressions-")
        self.root = Path(self.temp.name).resolve()
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()
        self.service = AutomationService(
            allowed_roots=[self.desktop], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "state" / "gaps.json",
            browser_headless=True,
        )
        self.planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_search_then_open_first_result_routes_to_orchestrator(self):
        prompt = "search google for cat videos and then open the first result"
        self.assertTrue(is_compound_web_task(prompt))
        self.assertEqual(TaskRouter().route(prompt).handler, "desktop_automation")
        self.assertTrue(self.service._should_use_task_planner(prompt))

    def test_search_then_open_first_result_builds_grounded_two_step_plan(self):
        prompt = "search google for cat videos and then open the first result"
        plan = self.planner._deterministic_plan(prompt)
        self.assertIsNotNone(plan)
        self.assertEqual([step["tool"] for step in plan["steps"]], [
            "browser.navigate", "browser.open_first_result"
        ])
        self.assertIn("google.com/search?q=cat+videos", plan["steps"][0]["args"]["url"])
        self.assertEqual(plan["steps"][1]["depends_on"], ["open_search_results"])
        self.assertEqual(_request_outcome_coverage_issues(prompt, plan["steps"], []), [])
        self.assertEqual(self.planner._draft_plan_issues(plan, prompt), [])

    def test_supported_search_engines_get_concrete_search_urls_and_complete_plans(self):
        cases = [
            ("search youtube for beginner Java tutorials then open the first result", "youtube.com/results?search_query=beginner+Java+tutorials"),
            ("search bing for free typing practice then open the first result", "bing.com/search?q=free+typing+practice"),
            ("search duck duck go for python tutorial and open first result", "duckduckgo.com/?q=python+tutorial"),
        ]
        for prompt, expected_url in cases:
            with self.subTest(prompt=prompt):
                plan = self.planner._deterministic_plan(prompt)
                self.assertIsNotNone(plan)
                self.assertEqual([step["tool"] for step in plan["steps"]], [
                    "browser.navigate", "browser.open_first_result"
                ])
                self.assertIn(expected_url, plan["steps"][0]["args"]["url"])
                self.assertEqual(self.planner._draft_plan_issues(plan, prompt), [])

    def test_generic_search_then_open_first_result_uses_single_verified_page_read(self):
        prompt = "search about Jane Goodall and open the first result"
        plan = self.planner._deterministic_plan(prompt)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(plan["steps"][0]["tool"], "web.browse")
        self.assertEqual(plan["steps"][0]["args"]["query"], "Jane Goodall")
        self.assertEqual(plan["steps"][0]["args"]["max_pages"], 1)
        self.assertEqual(_request_outcome_coverage_issues(prompt, plan["steps"], []), [])
        self.assertEqual(self.planner._draft_plan_issues(plan, prompt), [])

    def test_wikipedia_summary_in_notepad_accepts_write_the_summary_phrasing(self):
        prompt = "search about jawaharlal nehru on wikipedia and write the summary in a notepad"
        parsed = _extract_web_browse_request(prompt)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["url"], "https://en.wikipedia.org/wiki/Jawaharlal_Nehru")
        self.assertEqual(parsed["query"], "")
        self.assertTrue(parsed["wants_summary"])
        self.assertTrue(parsed["wants_notepad"])
        plan = self.planner._deterministic_plan(prompt)
        self.assertIsNotNone(plan)
        self.assertEqual([step["tool"] for step in plan["steps"]], [
            "web.browse", "content.generate_text", "desktop.open_target",
            "desktop.new_notepad_document", "desktop.type_notepad",
        ])
        by_id = {step["id"]: step for step in plan["steps"]}
        self.assertIn("${steps.browse.data.research_text}", by_id["summary"]["args"]["prompt"])
        self.assertEqual(by_id["summary"]["depends_on"], ["browse"])
        self.assertEqual(by_id["type_summary"]["args"]["value"], "${steps.summary.data.text}")
        self.assertIn("summary", by_id["type_summary"]["depends_on"])
        self.assertIn("new_notepad_document", by_id["type_summary"]["depends_on"])
        self.assertEqual(_request_outcome_coverage_issues(prompt, plan["steps"], []), [])
        self.assertEqual(self.planner._draft_plan_issues(plan, prompt), [])

    def test_noncanonical_wikipedia_topic_query_drops_about_prefix_and_follow_up_actions(self):
        parsed = _extract_web_browse_request(
            "search about Jane Goodall on wikipedia and write the summary in a notepad"
        )
        self.assertIsNotNone(parsed)
        self.assertIn("Jane Goodall site:en.wikipedia.org", parsed["query"])
        self.assertNotIn("about Jane Goodall", parsed["query"])
        self.assertNotIn("write the summary", parsed["query"].casefold())
        self.assertTrue(parsed["wants_summary"])
        self.assertTrue(parsed["wants_notepad"])

    def test_existing_apj_abdul_kalam_notepad_prompt_stays_complete(self):
        prompt = "search about dr apj abdul kalam and summarise the data and write about him on notepad"
        parsed = _extract_web_browse_request(prompt)
        self.assertEqual(parsed["url"], "https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam")
        self.assertTrue(parsed["wants_summary"])
        self.assertTrue(parsed["wants_notepad"])
        plan = self.planner._deterministic_plan(prompt)
        self.assertEqual(len(plan["steps"]), 5)
        self.assertEqual(self.planner._draft_plan_issues(plan, prompt), [])

    def test_shared_gate_rejects_browse_only_plan_for_summary_and_notepad_request(self):
        prompt = "search about jawaharlal nehru on wikipedia and write the summary in a notepad"
        incomplete = [{
            "id": "browse", "tool": "web.browse", "purpose": "Search Wikipedia for Jawaharlal Nehru",
            "args": {"query": "jawaharlal nehru site:en.wikipedia.org"}, "depends_on": [],
        }]
        issues = _request_outcome_coverage_issues(prompt, incomplete, [])
        joined = " ".join(issues).casefold()
        self.assertIn("summary", joined)
        self.assertIn("notepad", joined)

    def test_invalid_incomplete_deterministic_fallback_never_executes(self):
        prompt = "search about jawaharlal nehru on wikipedia and write the summary in a notepad"
        incomplete = {
            "goal": prompt,
            "steps": [{
                "id": "browse", "tool": "web.browse", "purpose": "Search for Jawaharlal Nehru on Wikipedia",
                "args": {"query": "jawaharlal nehru site:en.wikipedia.org", "max_pages": 2},
                "depends_on": [], "verify": "The page was read.",
            }],
            "needs_input": [], "missing_capabilities": [],
        }
        with patch.object(self.planner, "_deterministic_plan", return_value=incomplete), \
             patch.object(self.service, "_execute_planned_workflow") as execute:
            result = self.planner.run(prompt)
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "invalid_plan")
        self.assertIn("No steps were executed", result["message"])
        self.assertIn("does not cover the full request", result["error"])
        execute.assert_not_called()

    def test_open_first_result_extracts_live_visible_href_and_verifies_navigation(self):
        page = _FakePage(
            "https://www.google.com/search?q=cat+videos",
            {"a:has(h3)": [_FakeResultLink("https://example.org/cats", "Cat videos") ]},
        )
        session = object.__new__(BrowserSession)
        session.timeout_ms = 1000
        with patch.object(session, "_ensure_page", return_value=page), \
             patch.object(session, "_validate_destination", side_effect=lambda value: value), \
             patch.object(session, "navigate", return_value={
                 "ok": True, "url": "https://example.org/cats", "title": "Cat videos", "http_status": 200,
             }) as navigate:
            result = session.open_first_result()
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["result_url"], "https://example.org/cats")
        self.assertEqual(result["opened_url"], "https://example.org/cats")
        navigate.assert_called_once_with("https://example.org/cats")

    def test_open_first_result_unwraps_google_redirect_before_navigation(self):
        page = _FakePage(
            "https://www.google.com/search?q=cat+videos",
            {"a:has(h3)": [_FakeResultLink("/url?q=https%3A%2F%2Fexample.org%2Fcats", "Cat videos")]},
        )
        session = object.__new__(BrowserSession)
        session.timeout_ms = 1000
        with patch.object(session, "_ensure_page", return_value=page), \
             patch.object(session, "_validate_destination", side_effect=lambda value: value), \
             patch.object(session, "navigate", return_value={
                 "ok": True, "url": "https://example.org/cats", "title": "Cat videos", "http_status": 200,
             }) as navigate:
            result = session.open_first_result()
        self.assertEqual(result["result_url"], "https://example.org/cats")
        navigate.assert_called_once_with("https://example.org/cats")

    def test_open_result_rejects_lookalike_search_engine_domains(self):
        from eva.automation.engine.models import AutomationError
        for url in (
            "https://notyoutube.com/results?search_query=cat",
            "https://evilgoogle.com/search?q=cat",
            "https://duckduckgo.com.attacker.example/html/?q=cat",
        ):
            with self.subTest(url=url):
                page = _FakePage(url, {})
                session = object.__new__(BrowserSession)
                session.timeout_ms = 1000
                with patch.object(session, "_ensure_page", return_value=page), \
                     patch.object(session, "navigate") as navigate:
                    with self.assertRaises(AutomationError) as raised:
                        session.open_result(index=1)
                self.assertEqual(raised.exception.code, "NOT_SEARCH_RESULTS")
                navigate.assert_not_called()

    def test_open_first_result_rejects_non_search_page_without_navigation(self):
        page = _FakePage("https://example.org/article", {})
        session = object.__new__(BrowserSession)
        session.timeout_ms = 1000
        with patch.object(session, "_ensure_page", return_value=page), \
             patch.object(session, "navigate") as navigate:
            from eva.automation.engine.models import AutomationError
            with self.assertRaises(AutomationError) as raised:
                session.open_first_result()
        self.assertEqual(raised.exception.code, "NOT_SEARCH_RESULTS")
        navigate.assert_not_called()


    def test_search_topic_on_youtube_then_play_second_result_routes_to_planner(self):
        prompts = [
            "search Mr beast on youtube and play the second result",
            "search youtube for Mr beast and play the second result",
            "search YouTube for Mr Beast then play the 2nd video",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(is_compound_web_task(prompt))
                self.assertEqual(TaskRouter().route(prompt).handler, "desktop_automation")
                parsed = _extract_ranked_search_task(prompt)
                self.assertEqual(parsed, {"engine": "youtube", "query": "Mr beast" if "2nd video" not in prompt else "Mr Beast",
                                          "index": 2, "play": True, "action": "play"})
                plan = self.planner._deterministic_plan(prompt)
                self.assertIsNotNone(plan)
                self.assertEqual([step["tool"] for step in plan["steps"]], ["browser.navigate", "browser.open_result"])
                self.assertIn("youtube.com/results?search_query=Mr+beast" if "2nd video" not in prompt else "youtube.com/results?search_query=Mr+Beast",
                              plan["steps"][0]["args"]["url"])
                self.assertEqual(plan["steps"][1]["args"], {"index": 2, "play": True})
                self.assertEqual(plan["steps"][1]["depends_on"], ["open_search_results"])
                self.assertEqual(self.planner._draft_plan_issues(plan, prompt), [])

    def test_ranked_result_coverage_rejects_search_only_or_wrong_ordinal(self):
        prompt = "search youtube for Mr beast and play the second result"
        search_only = [{"id": "open_search_results", "tool": "browser.navigate",
                        "args": {"url": "https://www.youtube.com/results?search_query=Mr+beast"},
                        "depends_on": []}]
        issues = " ".join(_request_outcome_coverage_issues(prompt, search_only, [])).casefold()
        self.assertIn("result 2", issues)
        wrong_rank = [
            search_only[0],
            {"id": "open_ranked_result", "tool": "browser.open_result", "args": {"index": 1, "play": True},
             "depends_on": ["open_search_results"]},
        ]
        issues = " ".join(_request_outcome_coverage_issues(prompt, wrong_rank, [])).casefold()
        self.assertIn("result 2", issues)

    def test_open_result_selects_exact_second_visible_href(self):
        page = _FakePage(
            "https://www.google.com/search?q=cat+videos",
            {"a:has(h3)": [
                _FakeResultLink("https://example.org/cats-1", "First cat result"),
                _FakeResultLink("https://example.org/cats-2", "Second cat result"),
            ]},
        )
        session = object.__new__(BrowserSession)
        session.timeout_ms = 1000
        with patch.object(session, "_ensure_page", return_value=page), \
             patch.object(session, "_validate_destination", side_effect=lambda value: value), \
             patch.object(session, "navigate", return_value={
                 "ok": True, "verified": True, "url": "https://example.org/cats-2", "title": "Second cat result", "http_status": 200,
             }) as navigate:
            result = session.open_result(index=2, play=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["result_index"], 2)
        self.assertEqual(result["result_url"], "https://example.org/cats-2")
        navigate.assert_called_once_with("https://example.org/cats-2")

    def test_play_result_is_not_success_without_observed_playback(self):
        page = _FakePage(
            "https://www.youtube.com/results?search_query=Mr+beast",
            {"ytd-video-renderer a#video-title": [
                _FakeResultLink("https://www.youtube.com/watch?v=first", "First video"),
                _FakeResultLink("https://www.youtube.com/watch?v=second", "Second video"),
            ]},
        )
        session = object.__new__(BrowserSession)
        session.timeout_ms = 1000
        with patch.object(session, "_ensure_page", return_value=page), \
             patch.object(session, "_validate_destination", side_effect=lambda value: value), \
             patch.object(session, "navigate", return_value={
                 "ok": True, "verified": True, "url": "https://www.youtube.com/watch?v=second", "title": "Second video", "http_status": 200,
             }), \
             patch.object(session, "_attempt_video_playback", return_value={"playing": False, "paused": True}):
            result = session.open_result(index=2, play=True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["error_code"], "PLAYBACK_NOT_CONFIRMED")
        self.assertEqual(result["opened_url"], "https://www.youtube.com/watch?v=second")

    def test_google_challenge_falls_back_to_html_search_and_reports_it(self):
        page = _FakeNavigationPage()
        session = object.__new__(BrowserSession)
        session.timeout_ms = 1000
        session._invalidate_element_refs = lambda: None
        with patch.object(session, "_ensure_page", return_value=page), \
             patch.object(session, "_validate_destination", side_effect=lambda value: value):
            result = session.navigate("https://www.google.com/search?q=cat+videos")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_engine"], "DuckDuckGo")
        self.assertIn("google.com/search?q=cat+videos", page.goto_calls[0])
        self.assertIn("html.duckduckgo.com/html/?q=cat+videos", page.goto_calls[1])
        self.assertTrue(result["url"].startswith("https://html.duckduckgo.com/html/"))
        self.assertEqual(len(page.goto_calls), 2)

    def test_summary_contract_rejects_wikipedia_page_shell_echo(self):
        pasted_page_shell = (
            "A. P. J. Abdul Kalam - Wikipedia Jump to content Search Search Avul Pakir Jainulabdeen Abdul Kalam "
            "Official portrait, c. 2002 President of India In office 25 July 2002 – 25 July 2007 "
            "Personal details Born (1931-10-15) 15 October 1931 Rameswaram, India."
        )
        mismatch = validate_generated_content(pasted_page_shell, "summary", "APJ Abdul Kalam")
        self.assertIsNotNone(mismatch)
        self.assertIn("webpage navigation", mismatch.casefold())
        self.assertFalse(_has_wikipedia_article_prose(pasted_page_shell))

    def test_offline_wikipedia_summary_ignores_page_shell_and_uses_article_sentences(self):
        prompt = """Requested output style: summary
Current task:
Summarize APJ Abdul Kalam from the source.

Actual browser results:
SOURCE: A. P. J. Abdul Kalam - Wikipedia
URL: https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam
HEADINGS:
- Personal life
- Scientific career
PAGE TEXT (untrusted source content):
A. P. J. Abdul Kalam - Wikipedia Jump to content Search Search 155 languages. For other uses, see Nehru (disambiguation).
Avul Pakir Jainulabdeen Abdul Kalam was an Indian aerospace engineer and science administrator who served as president of India from 2002 to 2007.
He was awarded the Bharat Ratna in 1997 for his contribution to scientific research and the modernisation of defence technology in India.
"""
        report = _offline_research_summary(prompt)
        self.assertIn("Offline extractive report", report)
        self.assertIn("served as president of India", report)
        self.assertIn("Bharat Ratna in 1997", report)
        self.assertNotIn("Jump to content", report)
        self.assertNotIn("Main headings:", report)
        self.assertNotIn("Personal life", report)
        self.assertIsNone(validate_generated_content(report, "summary", "APJ Abdul Kalam"))

    def test_invalid_model_page_shell_is_replaced_by_clean_source_extracts(self):
        class PageShellProvider:
            def generate_text(self, system_prompt, user_prompt, **kwargs):
                return ("A. P. J. Abdul Kalam - Wikipedia Jump to content Search Search "
                        "Official portrait, c. 2002 President of India In office 25 July 2002 – 25 July 2007 "
                        "Personal details Born 15 October 1931.")

        prompt = """Requested output style: summary
Current task:
Summarize APJ Abdul Kalam from the source.

Actual browser results:
SOURCE: A. P. J. Abdul Kalam - Wikipedia
URL: https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam
PAGE TEXT (untrusted source content):
A. P. J. Abdul Kalam - Wikipedia Jump to content Search Search 155 languages.
Avul Pakir Jainulabdeen Abdul Kalam was an Indian aerospace engineer and science administrator who served as president of India from 2002 to 2007.
He was awarded the Bharat Ratna in 1997 for his contribution to scientific research and the modernisation of defence technology in India.
"""
        result = self.service.executor.execute(
            "content.generate_text",
            {"prompt": prompt, "output_kind": "summary", "expected_topic": "APJ Abdul Kalam"},
            context=ExecutionContext(metadata={"llm_provider": PageShellProvider()}),
        )
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(result.data.get("mode"), "offline_extractive_fallback")
        self.assertIn("served as president of India", result.data["text"])
        self.assertIn("Bharat Ratna in 1997", result.data["text"])
        self.assertNotIn("Jump to content", result.data["text"])
        self.assertNotIn("Official portrait", result.data["text"])

    def test_offline_fallback_refuses_when_only_wikipedia_shell_exists(self):
        prompt = """Actual browser results:
SOURCE: Person - Wikipedia
URL: https://en.wikipedia.org/wiki/Person
HEADINGS:
- Early life
PAGE TEXT (untrusted source content):
Jump to content Search Search 155 languages. For other uses, see Person (disambiguation).
"""
        report = _offline_research_summary(prompt)
        self.assertIn("No readable source excerpts", report)
        self.assertIn("No summary has been invented", report)

    def test_browser_navigation_reports_observed_url_as_verified(self):
        class Page:
            def __init__(self):
                self.url = "about:blank"
            def goto(self, url, **kwargs):
                self.url = url
                return _FakeResponse(200)
            def title(self):
                return "Example Domain"
        page = Page()
        session = object.__new__(BrowserSession)
        session.timeout_ms = 1000
        session._invalidate_element_refs = lambda: None
        with patch.object(session, "_ensure_page", return_value=page), \
             patch.object(session, "_validate_destination", side_effect=lambda value: value):
            result = session.navigate("https://example.org/")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["url"], "https://example.org/")



if __name__ == "__main__":
    unittest.main()
