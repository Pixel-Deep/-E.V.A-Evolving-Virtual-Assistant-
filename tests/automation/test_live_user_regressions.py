"""Regression tests derived from EVA's real interactive smoke-test transcript."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eva.automation.content_contracts import validate_generated_content
from eva.automation.engine import ExecutionContext
from eva.automation.service import AutomationService
from eva.automation.task_planner import TaskPlanner
from eva.core.task_router import TaskRouter


class FixedProvider:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def generate_text(self, system_prompt, user_prompt, **kwargs):
        self.calls += 1
        return self.text

    def generate_project_json(self, *args, **kwargs):
        raise AssertionError("These high-confidence requests must not be replanned by the model")




class FailingProvider:
    def generate_text(self, system_prompt, user_prompt, **kwargs):
        raise RuntimeError("Ollama is not running")

    def generate_project_json(self, *args, **kwargs):
        raise RuntimeError("Ollama is not running")


class RecordingProvider:
    def __init__(self, text: str):
        self.text = text
        self.user_prompt = ""

    def generate_text(self, system_prompt, user_prompt, **kwargs):
        self.user_prompt = user_prompt
        return self.text


class LiveUserRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-live-regressions-")
        self.root = Path(self.temp.name).resolve()
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()
        self.service = AutomationService(
            allowed_roots=[self.desktop], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "state" / "gaps.json",
            browser_headless=True,
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_search_about_apj_abdul_kalam_routes_to_web_research_and_notepad(self):
        from eva.automation.task_planner import _extract_web_browse_request
        prompt = "search about dr apj abdul kalam and summarise the data and write about him on notepad"
        parsed = _extract_web_browse_request(prompt)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["url"], "https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam")
        self.assertEqual(parsed["query"], "")
        self.assertTrue(parsed["wants_summary"])
        plan = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)._deterministic_plan(prompt)
        self.assertEqual(plan["steps"][0]["tool"], "web.browse")
        self.assertEqual(plan["steps"][0]["args"]["url"], "https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam")
        self.assertNotIn("query", plan["steps"][0]["args"])
        self.assertEqual(plan["steps"][1]["tool"], "content.generate_text")
        self.assertEqual(plan["steps"][2]["tool"], "desktop.open_target")
        self.assertEqual(plan["steps"][-1]["tool"], "desktop.type_notepad")

    def test_common_misrecognition_of_apj_abdul_kalam_is_normalized(self):
        from eva.automation.task_planner import _extract_web_browse_request
        parsed = _extract_web_browse_request(
            "search about drapkj abdul kalam and summarise the data and write about him on notepad"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["url"], "https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam")
        self.assertEqual(parsed["query"], "")

    def test_known_apj_reference_reads_page_without_using_html_search(self):
        page_url = "https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam"

        def fake_browser(method, *args, **kwargs):
            if method == "navigate":
                return {"ok": True, "url": page_url, "title": "A. P. J. Abdul Kalam", "http_status": 200}
            if method == "text":
                return {"ok": True, "url": page_url, "title": "A. P. J. Abdul Kalam",
                        "text": "A. P. J. Abdul Kalam was an Indian aerospace scientist and the 11th President of India from 2002 to 2007.",
                        "truncated": False}
            if method == "headings":
                return {"ok": True, "headings": ["Early life", "Presidency"]}
            raise AssertionError(method)

        with patch("eva.tools.web_search.search_web_results", side_effect=AssertionError("Search HTML should not be called for this canonical topic")), \
             patch.object(self.service.browser_session, "run_serialized", side_effect=fake_browser):
            result = self.service.execute_tool("web.browse", {"url": page_url, "max_pages": 1, "max_chars_per_page": 1000})
        self.assertTrue(result.ok, result.as_dict())
        self.assertIn("11th President of India", result.data["research_text"])
        self.assertEqual(result.data["pages"][0]["url"], page_url)

    def test_wikipedia_reader_targets_article_content_before_page_shell(self):
        page_url = "https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam"
        article_text = (
            "Avul Pakir Jainulabdeen Abdul Kalam was an Indian aerospace scientist and statesman "
            "who served as the 11th president of India from 2002 to 2007. "
            "He was born on 15 October 1931 in Rameswaram, Tamil Nadu, and studied physics and aerospace engineering."
        )

        def fake_browser(method, *args, **kwargs):
            if method == "navigate":
                return {"ok": True, "url": page_url, "title": "A. P. J. Abdul Kalam", "http_status": 200}
            if method == "text":
                selector = args[0]
                if selector == "#mw-content-text .mw-parser-output":
                    return {"ok": True, "url": page_url, "title": "A. P. J. Abdul Kalam",
                            "selector": selector, "text": article_text, "truncated": False}
                return {"ok": True, "url": page_url, "title": "A. P. J. Abdul Kalam",
                        "selector": selector, "text": "Contents\nEarly life and education\nCareer as a scientist\nPresidency\nAwards and honours", "truncated": False}
            if method == "headings":
                return {"ok": True, "headings": ["Early life and education", "Presidency"]}
            raise AssertionError(method)

        with patch.object(self.service.browser_session, "run_serialized", side_effect=fake_browser):
            result = self.service.execute_tool("web.browse", {
                "url": page_url, "max_pages": 1, "max_chars_per_page": 5000,
            })
        self.assertTrue(result.ok, result.as_dict())
        self.assertIn("served as the 11th president", result.data["research_text"])
        self.assertNotIn("Contents\nEarly life", result.data["research_text"])
        self.assertEqual(result.data["pages"][0]["content_selector"], "#mw-content-text .mw-parser-output")

    def test_wikipedia_headings_only_is_not_a_successful_browse(self):
        page_url = "https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam"

        def fake_browser(method, *args, **kwargs):
            if method == "navigate":
                return {"ok": True, "url": page_url, "title": "A. P. J. Abdul Kalam", "http_status": 200}
            if method == "text":
                return {"ok": True, "url": page_url, "title": "A. P. J. Abdul Kalam",
                        "selector": args[0],
                        "text": "Contents\nEarly life and education\nCareer as a scientist\nPresidency\nAwards and honours",
                        "truncated": False}
            if method == "headings":
                return {"ok": True, "headings": ["Early life and education", "Presidency"]}
            raise AssertionError(method)

        with patch.object(self.service.browser_session, "run_serialized", side_effect=fake_browser), \
             patch("eva.automation.service._read_public_html_page", side_effect=OSError("fallback unavailable")):
            result = self.service.execute_tool("web.browse", {
                "url": page_url, "max_pages": 1, "max_chars_per_page": 5000,
            })
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "WEB_PAGE_READ_FAILED")
        self.assertEqual(result.data.get("page_count"), 0)
        self.assertIn("navigation/headings only", result.data.get("errors", [""])[0])

    def test_offline_summary_refuses_to_pass_headings_only_as_content(self):
        prompt = (
            "Summarize actual source evidence.\n\nActual browser results:\n"
            "SOURCE: A. P. J. Abdul Kalam - Wikipedia\n"
            "URL: https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam\n"
            "HEADINGS:\n- Early life and education\n- Presidency\n"
            "PAGE TEXT (untrusted source content):\nContents\nEarly life and education\nPresidency"
        )
        result = self.service.execute_tool("content.generate_text", {
            "prompt": prompt, "output_kind": "summary", "expected_topic": "APJ Abdul Kalam",
        })
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INSUFFICIENT_SOURCE_TEXT")
        self.assertTrue(result.data.get("insufficient_source_text"))
        self.assertIn("did not invent a summary", result.message)

    def test_model_refusal_is_never_reported_as_a_successful_factual_draft(self):
        refusal = ("I'm sorry, but I cannot fulfill this request without additional information about "
                   "who 'him' refers to. Could you please provide more context?")
        self.assertIsNotNone(validate_generated_content(refusal, "summary", "APJ Abdul Kalam"))
        self.assertIsNotNone(validate_generated_content(
            "A good message.\n\nWhen asked \"Tell me when it is ready,\" respond: \"Message draft is now available on your clipboard.\"",
            "message", "meeting"))

    def test_invalid_meeting_model_output_uses_clean_offline_template(self):
        prompt = "Draft a message asking for a meeting, copy it to the clipboard, and tell me when it is ready."
        provider = FixedProvider(
            "Subject: Meeting\n\nHello, could we meet?\n\nWhen asked tell me when it is ready, respond: Message draft is now available on your clipboard."
        )
        result = self.service.executor.execute(
            "content.generate_text",
            {"prompt": prompt, "output_kind": "message", "expected_topic": "meeting"},
            context=ExecutionContext(metadata={"llm_provider": provider}),
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data["mode"], "offline_template_fallback")
        self.assertNotIn("When asked", result.data["text"])
        self.assertIn("meeting", result.data["text"].casefold())

    def test_explanation_about_python_file_search_is_not_desktop_action(self):
        route = TaskRouter().route("Explain how Python searches for files.")
        self.assertEqual(route.category, "EXPLANATION")
        self.assertEqual(route.handler, "ollama")

    def test_capability_question_uses_registered_automation_inventory(self):
        route = TaskRouter().route("What types of automation can you perform?")
        self.assertEqual(route.category, "AUTOMATION_CAPABILITIES")
        self.assertEqual(route.handler, "desktop_automation")
        result = self.service.handle_text("What types of automation can you perform?")
        self.assertTrue(result.get("ok"), result)
        self.assertIn("registered_tool_count", result)

    def test_exact_notepad_text_is_literal_and_never_sent_to_writing_model(self):
        planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)
        plan = planner._deterministic_plan("Open Notepad and write the words 'calculate 5 + 8'")
        self.assertIsNotNone(plan)
        content_steps = [s for s in plan["steps"] if s["tool"] in {"content.generate_text", "content.literal_text"}]
        self.assertEqual(len(content_steps), 1)
        self.assertEqual(content_steps[0]["tool"], "content.literal_text")
        self.assertEqual(content_steps[0]["args"]["text"], "calculate 5 + 8")
        type_step = next(s for s in plan["steps"] if s["tool"] == "desktop.type_notepad")
        self.assertEqual(type_step["args"]["value"], "${steps.literal_content.data.text}")

    def test_named_text_file_preserves_literal_after_containing(self):
        planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)
        plan = planner._deterministic_plan(
            "Create a text file named test.txt on my Desktop containing 'EVA is running'"
        )
        self.assertIsNotNone(plan)
        create = next(step for step in plan["steps"] if step["tool"] == "files.create_text_file")
        self.assertEqual(Path(create["args"]["path"]), self.desktop / "test.txt")
        self.assertEqual(create["args"]["content"], "EVA is running")

    def test_notepad_save_as_uses_requested_filename_and_approved_desktop_root(self):
        planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)
        plan = planner._deterministic_plan("Open Notepad, type 'Step one complete', then save it as eva_test.txt on my Desktop.")
        self.assertIsNotNone(plan)
        create = next(s for s in plan["steps"] if s["tool"] == "files.create_text_file")
        self.assertEqual(Path(create["args"]["path"]), self.desktop / "eva_test.txt")
        self.assertEqual(create["args"]["content"], "Step one complete")

    def test_folder_and_child_file_workflow_uses_approved_desktop_root(self):
        prompt = ("Create a folder named StudyPlan on my Desktop, then create a file inside it "
                  "named timetable.txt and create a demo timetable in the txt file")
        provider = FixedProvider("SAMPLE WEEKLY TIMETABLE\nMonday: Mathematics, English, Computer Science\nTuesday: Commerce, Accountancy, Hindi\nWednesday: Mathematics, English, Physical Education\nThursday: Computer Science, Commerce, Accountancy\nFriday: Hindi, Mathematics, Revision")
        result = self.service.handle_text(prompt, provider=provider)
        self.assertTrue(result.get("ok"), result)
        folder = self.desktop / "StudyPlan"
        target = folder / "timetable.txt"
        self.assertTrue(folder.is_dir())
        self.assertTrue(target.is_file())
        self.assertTrue(target.read_text(encoding="utf-8").strip())
        self.assertIn("sample", target.read_text(encoding="utf-8").casefold())

    def test_clipboard_receives_generated_message_not_original_prompt(self):
        prompt = "Draft a message asking for a meeting, copy it to the clipboard, and tell me when it is ready."
        draft = "Hello, could we arrange a meeting at a time convenient for you? Thank you."
        provider = FixedProvider(draft)
        with patch("eva.automation.desktop.execute", return_value={"ok": True, "verified": True, "characters": len(draft)}) as desktop_execute:
            result = self.service.handle_text(prompt, provider=provider)
        self.assertTrue(result.get("ok"), result)
        clipboard_calls = [c for c in desktop_execute.call_args_list if c.args and c.args[0] == "clipboard_set"]
        self.assertEqual(len(clipboard_calls), 1)
        self.assertEqual(clipboard_calls[0].args[1], draft)
        self.assertNotEqual(clipboard_calls[0].args[1], prompt)

    def test_long_browser_evidence_is_compacted_before_local_model_call(self):
        evidence = (
            "SOURCE: Internet service provider\n"
            "URL: https://en.wikipedia.org/wiki/Internet_service_provider\n"
            "HTTP status: 200\n"
            "HEADINGS:\n- Overview\n- Access technologies\n"
            "PAGE TEXT (untrusted source content):\n" + ("An internet service provider supplies internet access. " * 260)
        )
        prompt = (
            "Summarize the actual browser page safely and include the source URL.\n\n"
            "Actual browser results:\n" + evidence
        )
        self.assertGreater(len(prompt), 4000)
        provider = RecordingProvider(
            "An internet service provider supplies access to the internet and related connectivity services. "
            "Source: https://en.wikipedia.org/wiki/Internet_service_provider"
        )
        context = ExecutionContext(metadata={"llm_provider": provider})
        result = self.service.executor.execute(
            "content.generate_text",
            {"prompt": prompt, "output_kind": "summary", "expected_topic": "internet service provider"},
            context=context,
        )
        self.assertTrue(result.ok, result.message)
        self.assertLessEqual(len(provider.user_prompt), 7000)
        self.assertIn("https://en.wikipedia.org/wiki/Internet_service_provider", provider.user_prompt)

    def test_web_summary_uses_observed_source_excerpts_when_local_model_is_unavailable(self):
        prompt = (
            "Summarize only the actual page and retain its source URL.\n\n"
            "Actual browser results:\n"
            "SOURCE: Internet service provider\n"
            "URL: https://en.wikipedia.org/wiki/Internet_service_provider\n"
            "HTTP status: 200\n"
            "HEADINGS:\n- Overview\n- Access technologies\n"
            "PAGE TEXT (untrusted source content):\n"
            "An internet service provider (ISP) is an organization that provides services for accessing, using, "
            "managing, or participating in the internet. ISPs can provide a variety of services."
        )
        result = self.service.executor.execute(
            "content.generate_text",
            {"prompt": prompt, "output_kind": "summary", "expected_topic": "internet service provider"},
            context=ExecutionContext(metadata={"llm_provider": FailingProvider()}),
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data.get("mode"), "offline_extractive_fallback")
        self.assertIn("https://en.wikipedia.org/wiki/Internet_service_provider", result.data["text"])
        self.assertIn("Internet service provider", result.data["text"])

    def test_meeting_message_falls_back_safely_when_local_model_is_unavailable(self):
        prompt = "Draft a message asking for a meeting, copy it to the clipboard, and tell me when it is ready."
        expected_prefix = "Hello,\n\nI hope you are doing well."
        with patch("eva.automation.desktop.execute", return_value={"ok": True, "verified": True}) as desktop_execute:
            result = self.service.handle_text(prompt, provider=FailingProvider())
        self.assertTrue(result.get("ok"), result)
        clipboard_calls = [c for c in desktop_execute.call_args_list if c.args and c.args[0] == "clipboard_set"]
        self.assertEqual(len(clipboard_calls), 1)
        copied = clipboard_calls[0].args[1]
        self.assertTrue(copied.startswith(expected_prefix), copied)
        self.assertIn("meeting", copied.casefold())
        self.assertNotIn("Friday", copied)
        self.assertNotEqual(copied, prompt)

    def test_generic_model_clarification_is_rejected_before_destination_handoff(self):
        bad = "Sure! Please provide me with the specific writing request so I can fulfill it."
        self.assertIsNotNone(validate_generated_content(bad, "summary"))
        self.assertIsNotNone(validate_generated_content(bad, "application", "leave application"))

    def test_bad_leave_application_draft_does_not_open_or_type_into_notepad(self):
        bad = "Sure! Please provide me with the specific instructions you would like to follow."
        provider = FixedProvider(bad)
        with patch("eva.automation.desktop.execute", return_value={"ok": True, "verified": True}) as desktop_execute:
            result = self.service.handle_text(
                "Open Notepad and write a polite leave application for a student.", provider=provider
            )
        self.assertFalse(result.get("ok"), result)
        self.assertIn("clarification prompt", result.get("message", "").casefold())
        self.assertEqual(desktop_execute.call_count, 0, "Invalid draft must not reach desktop actions")
        execution_steps = result.get("execution", {}).get("steps", [])
        self.assertTrue(execution_steps)
        self.assertEqual(execution_steps[0].get("tool"), "content.generate_text")
        self.assertEqual(execution_steps[0].get("status"), "failed")

    def test_generic_leave_application_has_a_default_plan_not_a_clarification_question(self):
        planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)
        plan = planner._deterministic_plan("Open Notepad and write a polite leave application for a student.")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.get("needs_input"), [])
        draft = next(step for step in plan["steps"] if step["tool"] == "content.generate_text")
        self.assertEqual(draft["args"]["output_kind"], "application")
        self.assertEqual(draft["args"]["expected_topic"], "leave application")
        self.assertIn("for personal reasons", draft["args"]["prompt"].casefold())

    def test_research_summary_plan_hands_actual_page_text_to_writer_then_notepad(self):
        planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)
        plan = planner._deterministic_plan("open isp on wikipedia summarise the data and write it on notepad")
        self.assertIsNotNone(plan)
        tools = [s["tool"] for s in plan["steps"]]
        self.assertEqual(tools, ["web.browse", "content.generate_text", "desktop.open_target", "desktop.new_notepad_document", "desktop.type_notepad"])
        summary = plan["steps"][1]
        self.assertIn("${steps.browse.data.research_text}", summary["args"]["prompt"])
        typing = plan["steps"][-1]
        self.assertEqual(typing["args"]["value"], "${steps.summary.data.text}")


if __name__ == "__main__":
    unittest.main()
