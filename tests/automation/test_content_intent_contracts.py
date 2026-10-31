"""Adversarial tests for preserving the requested content, names, destinations and side effects."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eva.automation.content_contracts import build_content_brief, validate_generated_content
from eva.automation.engine.models import ToolResult
from eva.automation.service import AutomationService
from eva.automation.task_planner import TaskPlanner, _requested_output_name, _explicit_file_content


class FixedTextProvider:
    def __init__(self, text: str):
        self.text = text
        self.text_calls = 0
        self.plan_calls = 0
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    def generate_text(self, system_prompt, user_prompt, *, timeout=45, num_predict=1200):
        self.text_calls += 1
        self.prompts.append(user_prompt)
        self.system_prompts.append(system_prompt)
        return self.text

    def generate_project_json(self, system_prompt, user_prompt, *, timeout=45):
        self.plan_calls += 1
        raise AssertionError("A deterministic prompt must not be replanned by the model")


class ContentIntentContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-content-contract-")
        self.root = Path(self.temp.name).resolve()
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()
        self.gaps = self.root / "state" / "capability_gaps.json"
        self.service = AutomationService(
            allowed_roots=[self.root], audit_path=None,
            workflows_dir=self.root / "workflows", capability_gaps_path=self.gaps,
            browser_headless=True,
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_paragraph_brief_extracts_topic_and_explicit_genre(self):
        brief = build_content_brief("Open Notepad and write a paragraph on cow")
        self.assertEqual(brief["output_kind"], "paragraph")
        self.assertEqual(brief["topic"], "cow")
        self.assertIn("one coherent paragraph about cow", brief["prompt"].lower())
        self.assertIn("do not write a letter", brief["prompt"].lower())

    def test_letter_template_output_is_rejected_before_notepad_is_opened_or_typed(self):
        provider = FixedTextProvider(
            "Dear Principal,\nI request leave because of an urgent family matter.\nYours faithfully, Student"
        )
        with patch("eva.automation.desktop.execute") as desktop_execute:
            out = self.service.handle_text("Open Notepad and write a paragraph on cow", provider=provider)
        self.assertFalse(out.get("ok"), out)
        rendered = json.dumps(out, ensure_ascii=False).lower()
        self.assertTrue("paragraph" in rendered or "letter/application" in rendered, rendered)
        self.assertEqual(provider.text_calls, 2, "A contract mismatch gets one bounded local-model repair attempt.")
        self.assertEqual(provider.plan_calls, 0)
        self.assertFalse(out.get("verified", True))
        self.assertFalse(out.get("gap_recorded", False), "A content mismatch is not a missing tool capability.")
        self.assertFalse(out.get("capability_gap_file"), "Do not claim a capability-gap record exists for content mismatch.")
        desktop_execute.assert_not_called()

    def test_calculator_typo_is_normalized_without_losing_the_expression(self):
        from eva.automation.engine.models import ToolResult
        from unittest.mock import Mock
        launch_result = ToolResult(True, "desktop.open_target", {"target": "calculator"}, "Calculator launch requested.", verified=True)
        calls = []
        def fake_execute(tool, args, **kwargs):
            calls.append((tool, args))
            return launch_result
        ui_result = {"ok": True, "verified": True, "result": "105", "display": "Display is 105"}
        with patch.object(self.service, "execute_tool", side_effect=fake_execute), \
             patch("eva.automation.desktop.calculate_in_windows_calculator", return_value=ui_result) as calc_ui:
            out = self.service.handle_text("open calculator and calulate 73 plus 32", provider=None)
        self.assertTrue(out.get("ok"), out)
        calc_ui.assert_called_once_with("73 plus 32")
        self.assertIn(("desktop.open_target", {"target": "calculator"}), calls)
        self.assertTrue(out.get("verified"))

    def test_calculator_ui_failure_is_reported_with_the_actual_diagnostic(self):
        from eva.automation.engine.models import ToolResult
        launch_result = ToolResult(True, "desktop.open_target", {"target": "calculator"}, "Calculator launch requested.", verified=True)
        def fake_execute(tool, args, **kwargs):
            return launch_result
        ui_failure = {"ok": False, "verified": False, "error_code": "CALCULATOR_FOCUS_FAILED",
                      "error": "Foreground window was not Calculator."}
        with patch.object(self.service, "execute_tool", side_effect=fake_execute), \
             patch("eva.automation.desktop.calculate_in_windows_calculator", return_value=ui_failure):
            out = self.service.handle_text("open calculator and calculate 73 plus 32", provider=None)
        self.assertFalse(out.get("ok"), out)
        self.assertIn("Foreground window was not Calculator", out.get("message", ""))
        self.assertFalse(out.get("verified", True))

    def test_correct_paragraph_is_typed_without_saving_an_unrequested_file(self):
        paragraph = "A cow is a domestic animal. It provides milk and eats grass, hay, and other plants."
        provider = FixedTextProvider(paragraph)
        calls = []
        def desktop_action(action, value="", **kwargs):
            calls.append((action, value, kwargs))
            return {"ok": True, "verified": True, "message": "Mocked action completed"}
        with patch("eva.automation.desktop.execute", side_effect=desktop_action):
            out = self.service.handle_text("Open Notepad and write a paragraph on cow", provider=provider)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual([call[0] for call in calls], ["app_open", "notepad_new_document", "type"])
        self.assertEqual(calls[-1][1], paragraph)
        self.assertFalse(any(path.name.endswith("-paragraph.txt") for path in self.root.rglob("*.txt")))
        self.assertEqual(provider.text_calls, 1)

    def test_bare_extension_filename_is_not_prefixed_with_the_command(self):
        self.assertEqual(_requested_output_name("create a deep.txt file on desktop"), "deep.txt")
        self.assertEqual(_requested_output_name("create a deep.docx file on desktop"), "deep.docx")

    def test_docx_creation_uses_real_docx_writer(self):
        import zipfile
        out = self.service.plan_and_execute("create a deep.docx file on desktop", provider=None)
        target = self.desktop / "deep.docx"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(target.is_file())
        self.assertTrue(zipfile.is_zipfile(target), "The .docx must be a genuine OpenXML ZIP package.")
        with zipfile.ZipFile(target) as archive:
            self.assertIn("word/document.xml", archive.namelist())
        self.assertEqual(out["execution"]["steps"][0]["tool"], "files.create_docx")

    def test_poem_on_it_means_write_into_named_notepad_not_about_pronoun_it(self):
        brief = build_content_brief("open notepad and write a poem on it")
        self.assertEqual(brief["output_kind"], "poem")
        self.assertEqual(brief["topic"], "")
        self.assertNotIn("about it", brief["prompt"].lower())
        self.assertIn("poem", brief["prompt"].lower())

    def test_natural_filename_command_creates_deep_txt_without_command_prefix(self):
        out = self.service.plan_and_execute("create a deep.txt file on desktop", provider=None)
        target = self.desktop / "deep.txt"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"")
        self.assertFalse((self.desktop / "create a deep.txt").exists())
        # A repeated request must refuse overwrite under the same exact name and
        # must not silently invent another filename from the command itself.
        again = self.service.plan_and_execute("create a deep.txt file on desktop", provider=None)
        self.assertFalse(again.get("ok"), again)
        self.assertTrue(target.is_file())
        self.assertFalse((self.desktop / "create a deep.txt").exists())

    def test_named_desktop_file_without_content_creates_empty_file_without_llm(self):
        out = self.service.plan_and_execute("create a file on my desktop and name it as testing_eva.txt", provider=None)
        target = self.desktop / "testing_eva.txt"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"")
        self.assertTrue(out["execution"]["steps"][0]["result"]["verified"])

    def test_high_level_command_creates_named_empty_file_without_calling_model(self):
        out = self.service.handle_text("create a file on my desktop and name it as high_level_test.txt", provider=None)
        target = self.desktop / "high_level_test.txt"
        self.assertEqual(out.get("action"), "task_plan", out)
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"")

    def test_successful_but_unverified_ui_step_is_reported_honestly(self):
        paragraph = "A cow is a domestic animal. It provides milk and eats grass."
        provider = FixedTextProvider(paragraph)
        def desktop_action(action, value="", **kwargs):
            if action == "app_open":
                return {"ok": True, "verified": True, "message": "Opened Notepad."}
            return {"ok": True, "verified": False, "message": "Paste shortcut dispatched; visible text was not read back."}
        with patch("eva.automation.desktop.execute", side_effect=desktop_action):
            out = self.service.handle_text("Open Notepad and write a paragraph on cow", provider=provider)
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(out.get("verified", True))
        self.assertEqual(out.get("unverified_steps"), ["new_notepad_document", "type_draft"])
        self.assertIn("without independent verification", out.get("message", "").lower())

    def test_explicit_file_content_is_preserved_exactly(self):
        prompt = 'Create a file on Desktop named note.txt with exact text "Smoke test passed".'
        self.assertEqual(_requested_output_name(prompt), "note.txt")
        self.assertEqual(_explicit_file_content(prompt), "Smoke test passed")
        out = self.service.plan_and_execute(prompt, provider=None)
        target = self.desktop / "note.txt"
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(target.read_text(encoding="utf-8"), "Smoke test passed")
        self.assertTrue(out["execution"]["steps"][0]["result"]["verified"])

    def test_explicit_list_items_are_not_replaced_or_corrupted(self):
        prompt = "Please create a text file called packing.txt on Desktop with the three items socks, charger, and ID"
        expected = "socks\ncharger\nID\n"
        self.assertEqual(_explicit_file_content(prompt), expected)
        out = self.service.plan_and_execute(prompt, provider=None)
        target = self.desktop / "packing.txt"
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(target.read_text(encoding="utf-8"), expected)

    def test_exact_count_of_generated_items_is_enforced(self):
        provider = FixedTextProvider("Rice\nBeans\nMilk\nApples\nBread")
        prompt = "Make a file named groceries.txt on Desktop with five grocery items"
        out = self.service.plan_and_execute(prompt, provider=provider)
        target = self.desktop / "groceries.txt"
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(len([line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]), 5)
        self.assertEqual(provider.plan_calls, 0)

    def test_bad_generated_item_count_fails_before_file_creation(self):
        provider = FixedTextProvider("Rice\nBeans\nMilk\nApples")
        out = self.service.plan_and_execute("Make a file named groceries.txt on Desktop with five grocery items", provider=provider)
        self.assertFalse(out.get("ok"), out)
        self.assertFalse((self.desktop / "groceries.txt").exists())
        self.assertIn("exactly 5", json.dumps(out).lower())

    def test_generated_paragraph_can_be_saved_to_exact_requested_filename(self):
        provider = FixedTextProvider("A cow is a domesticated mammal commonly raised for milk. Cows eat grass and other plant material.")
        prompt = "Create a file named cow.txt and write a paragraph about cow in it"
        out = self.service.plan_and_execute(prompt, provider=provider)
        target = self.root / "Documents" / "EVA-created-files" / "cow.txt"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(target.is_file())
        self.assertIn("cow", target.read_text(encoding="utf-8").lower())

    def test_untopiced_poem_is_composed_for_notepad_without_topic_mismatch(self):
        poem = "Morning light climbs softly,\nBirds wake the sleepy trees,\nA new day opens gently,\nAnd carries hope upon the breeze."
        provider = FixedTextProvider(poem)
        calls = []
        def desktop_action(action, value="", **kwargs):
            calls.append((action, value, kwargs))
            return {"ok": True, "verified": True, "message": "Mocked action completed"}
        with patch("eva.automation.desktop.execute", side_effect=desktop_action):
            out = self.service.handle_text("open notepad and write a poem on it", provider=provider)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(provider.text_calls, 1)
        self.assertEqual([call[0] for call in calls], ["app_open", "notepad_new_document", "type"])
        self.assertEqual(calls[-1][1], poem)
        self.assertNotIn("it", build_content_brief("open notepad and write a poem on it")["topic"].casefold())

    def test_missing_topic_triggers_question_and_no_tool_side_effect(self):
        provider = FixedTextProvider("This content should never be requested")
        with patch("eva.automation.desktop.execute") as desktop_execute:
            out = self.service.handle_text("Open Notepad and write a paragraph", provider=provider)
        self.assertFalse(out.get("ok"), out)
        self.assertEqual(out.get("action"), "need_input")
        self.assertEqual(provider.text_calls, 0)
        desktop_execute.assert_not_called()

    def test_reason_phrase_is_a_constraint_not_an_opportunity_to_invent(self):
        brief = build_content_brief("Write a letter to the principal asking for leave because of fever")
        self.assertEqual(brief["topic"], "fever")
        self.assertIsNotNone(validate_generated_content(
            "Dear Principal, please grant leave for personal work.", "letter", brief["topic"]
        ))
        self.assertIsNone(validate_generated_content(
            "Dear Principal, please grant leave because of fever.", "letter", brief["topic"]
        ))

    def test_file_creation_output_name_does_not_mistake_notepad_for_filename(self):
        self.assertIsNone(_requested_output_name("Open Notepad and write a thank-you note"))

    def test_compound_create_file_and_send_email_is_not_reduced_to_writing_email_into_file(self):
        planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)
        plan = planner._deterministic_plan("Create a file and send an email")
        self.assertIsNone(plan, plan)


if __name__ == "__main__":
    unittest.main()
