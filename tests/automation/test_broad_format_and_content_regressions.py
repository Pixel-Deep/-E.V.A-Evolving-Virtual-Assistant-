"""Broad regression tests for content intent, file formats, and no-cross-route recovery."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from eva.automation.content_contracts import build_content_brief, validate_generated_content
from eva.automation import desktop
from eva.automation.service import AutomationService
from eva.automation.task_planner import TaskPlanner, _requested_output_name


class FixedProvider:
    def __init__(self, generated_text: str):
        self.generated_text = generated_text
        self.text_calls = 0
        self.plan_calls = 0

    def generate_text(self, system_prompt, user_prompt, *, timeout=45, num_predict=1200):
        self.text_calls += 1
        return self.generated_text

    def generate_project_json(self, system_prompt, user_prompt, *, timeout=45):
        self.plan_calls += 1
        raise AssertionError("Deterministic high-confidence requests must not ask the planner model.")


class BroadFormatAndContentRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-broad-regression-")
        self.root = Path(self.temp.name).resolve()
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()
        (self.root / "Documents").mkdir()
        (self.root / "Downloads").mkdir()
        self.service = AutomationService(
            allowed_roots=[self.root], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "capability_gaps.json",
            browser_headless=True,
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def _plan(self, prompt: str):
        return TaskPlanner(self.service, None, self.service.capability_gaps)._deterministic_plan(prompt)

    def test_benefits_of_water_is_a_list_and_does_not_route_to_browser(self):
        prompt = "open notepad and write the benifits of drinking water"
        brief = build_content_brief(prompt)
        self.assertEqual(brief["output_kind"], "list")
        self.assertEqual(brief["topic"], "drinking water")
        plan = self._plan(prompt)
        self.assertIsNotNone(plan)
        self.assertEqual([step["tool"] for step in plan["steps"]], [
            "content.generate_text", "desktop.open_target", "desktop.new_notepad_document", "desktop.type_notepad"
        ])
        self.assertNotIn("desktop.open_default_browser", [step["tool"] for step in plan["steps"]])
        valid = (
            "Benefits of drinking water\n"
            "- Helps prevent dehydration.\n"
            "- Supports digestion.\n"
            "- Helps regulate body temperature.\n"
            "- Supports nutrient transport.\n"
            "- Helps the kidneys remove waste."
        )
        self.assertIsNone(validate_generated_content(valid, "list", "drinking water"))

    def test_notepad_content_plan_opens_a_fresh_document_before_typing(self):
        plan = self._plan("open notepad and write a story about a school boy")
        self.assertIsNotNone(plan)
        tool_names = [step["tool"] for step in plan["steps"]]
        self.assertLess(tool_names.index("desktop.open_target"), tool_names.index("desktop.new_notepad_document"))
        self.assertLess(tool_names.index("desktop.new_notepad_document"), tool_names.index("desktop.type_notepad"))
        new_doc = next(step for step in plan["steps"] if step["tool"] == "desktop.new_notepad_document")
        self.assertEqual(new_doc["depends_on"], ["open_notepad"])

    def test_new_notepad_document_targets_focused_window_and_verifies_blank_state(self):
        calls = []
        class Window:
            def window_text(self): return "Untitled - Notepad"
            def restore(self): calls.append("restore")
            def set_focus(self): calls.append("focus")
            def type_keys(self, value, set_foreground=False): calls.append(("keys", value, set_foreground))
        window = Window()
        class FakeDesktop:
            def __init__(self, backend):
                self.backend = backend
            def windows(self, visible_only=True):
                self.assert_backend = self.backend
                return [window]
        with patch.object(desktop.platform, "system", return_value="Windows"), \
             patch.dict(sys.modules, {"pywinauto": SimpleNamespace(Desktop=FakeDesktop)}), \
             patch("eva.automation.desktop._focus_notepad_window", return_value={"ok": True, "window_title": "Untitled - Notepad"}), \
             patch("eva.automation.desktop._foreground_window_details", side_effect=[
                 {"title": "Untitled - Notepad", "process": "notepad.exe"},
                 {"title": "Untitled - Notepad", "process": "notepad.exe"},
             ]), \
             patch("eva.automation.desktop._verify_notepad_text", return_value={"readable": True, "verified": True, "characters": 0}):
            result = desktop._new_notepad_document()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["verified"], result)
        self.assertIn(("keys", "^n", True), calls)

    def test_new_notepad_document_stops_if_a_save_dialog_appears(self):
        calls = []
        class Window:
            def window_text(self): return "Untitled - Notepad"
            def restore(self): pass
            def set_focus(self): pass
            def type_keys(self, value, set_foreground=False): calls.append(value)
        window = Window()
        class FakeDesktop:
            def __init__(self, backend): pass
            def windows(self, visible_only=True): return [window]
        with patch.object(desktop.platform, "system", return_value="Windows"), \
             patch.dict(sys.modules, {"pywinauto": SimpleNamespace(Desktop=FakeDesktop)}), \
             patch("eva.automation.desktop._focus_notepad_window", return_value={"ok": True, "window_title": "Untitled - Notepad"}), \
             patch("eva.automation.desktop._foreground_window_details", side_effect=[
                 {"title": "Untitled - Notepad", "process": "notepad.exe"},
                 {"title": "Save changes - Notepad", "process": "notepad.exe"},
             ]), \
             patch("eva.automation.desktop._verify_notepad_text") as verify:
            result = desktop._new_notepad_document()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOTEPAD_SAVE_DIALOG")
        verify.assert_not_called()

    def test_bad_content_retry_keeps_the_same_notepad_intent(self):
        prompt = "open notepad and write the benifits of drinking water"
        bad = FixedProvider("Dear Principal,\nI request a leave.\nYours faithfully, Student")
        good = FixedProvider(
            "Benefits of drinking water\n"
            "- Helps prevent dehydration.\n- Supports digestion.\n"
            "- Helps regulate body temperature.\n- Supports nutrient transport.\n"
            "- Helps the kidneys remove waste."
        )
        calls = []
        def mock_desktop(action, value="", **kwargs):
            calls.append((action, value, kwargs))
            return {"ok": True, "verified": True, "message": "mocked desktop action"}
        with patch("eva.automation.desktop.execute", side_effect=mock_desktop):
            first = self.service.handle_text(prompt, provider=bad)
            second = self.service.handle_text(prompt, provider=good)
        self.assertFalse(first.get("ok"), first)
        self.assertIn("correspondence", first.get("message", "").lower())
        self.assertTrue(second.get("ok"), second)
        self.assertEqual([call[0] for call in calls], ["app_open", "notepad_new_document", "type"])
        self.assertNotIn("browser", str(calls).lower())
        self.assertEqual(good.plan_calls, 0)

    def test_named_batch_file_is_extracted_and_created_without_execution(self):
        prompt = "create a deep.bat file on desktop"
        self.assertEqual(_requested_output_name(prompt), "deep.bat")
        result = self.service.plan_and_execute(prompt, provider=None)
        target = self.desktop / "deep.bat"
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"")
        self.assertEqual(result["execution"]["steps"][0]["tool"], "files.create_text_file")
        self.assertFalse((self.desktop / "create a deep.bat").exists())

    def test_benefits_written_to_named_text_file_are_generated_not_empty(self):
        prompt = 'create a file named "water.txt" with benefits of drinking water on desktop'
        provider = FixedProvider(
            "Benefits of drinking water\n"
            "- Helps prevent dehydration.\n- Supports digestion.\n"
            "- Helps regulate body temperature.\n- Supports nutrient transport.\n"
            "- Helps the kidneys remove waste."
        )
        plan = self._plan(prompt)
        self.assertIsNotNone(plan)
        draft_plan_step = next(step for step in plan["steps"] if step["tool"] == "content.generate_text")
        self.assertEqual(draft_plan_step["args"]["output_kind"], "list")
        self.assertEqual(draft_plan_step["args"]["expected_topic"], "drinking water")
        result = self.service.plan_and_execute(prompt, provider=provider)
        target = self.desktop / "water.txt"
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(target.read_text(encoding="utf-8"), provider.generated_text)
        self.assertEqual(provider.text_calls, 1)
        self.assertIn("benefits of drinking water", provider.generated_text.lower())
        draft_step = result["execution"]["steps"][0]
        self.assertEqual(draft_step["tool"], "content.generate_text")

    def test_document_formats_create_and_read_back_content(self):
        # This exercises actual file bytes/packages and the matching reader, not schemas alone.
        formats = {
            ".txt": "files.create_text_file",
            ".bat": "files.create_text_file",
            ".docx": "files.create_docx",
            ".pptx": "files.create_pptx",
            ".xlsx": "files.create_xlsx",
            ".pdf": "files.create_pdf",
            ".rtf": "files.create_rtf",
            ".odt": "files.create_odt",
        }
        payload = "EVA cross-format round-trip phrase"
        for suffix, expected_tool in formats.items():
            with self.subTest(suffix=suffix):
                target = self.desktop / f"roundtrip{suffix}"
                result = self.service.plan_and_execute(
                    f'create a file named "{target.name}" with the exact text "{payload}" on my desktop',
                    provider=None,
                )
                self.assertTrue(result.get("ok"), (suffix, result))
                self.assertTrue(target.is_file(), suffix)
                self.assertEqual(result["execution"]["steps"][0]["tool"], expected_tool)
                if suffix in {".docx", ".pptx", ".xlsx", ".odt"}:
                    self.assertTrue(zipfile.is_zipfile(target), suffix)
                    with zipfile.ZipFile(target) as archive:
                        self.assertIsNone(archive.testzip(), suffix)
                if suffix == ".pdf":
                    self.assertTrue(target.read_bytes().startswith(b"%PDF-"))
                if suffix == ".txt" or suffix == ".bat":
                    self.assertEqual(target.read_text(encoding="utf-8"), payload)
                readback = self.service.execute_tool("files.read_document", {"path": str(target), "max_bytes": 10000})
                self.assertTrue(readback.ok, (suffix, readback.message, readback.error_code))
                normalized = str(readback.data.get("text", "")).replace("\r\n", "\n")
                self.assertIn(payload, normalized, (suffix, normalized))
                if suffix == ".rtf":
                    self.assertNotIn("Calibri", normalized)
                # Retrying a create request must not silently overwrite the first
                # file or mutate its contents, regardless of document format.
                overwrite = self.service.execute_tool(expected_tool, {"path": str(target), "content": "DO NOT OVERWRITE"})
                self.assertFalse(overwrite.ok, (suffix, overwrite.message))
                after_retry = self.service.execute_tool("files.read_document", {"path": str(target), "max_bytes": 10000})
                self.assertTrue(after_retry.ok, (suffix, after_retry.message))
                self.assertIn(payload, str(after_retry.data.get("text", "")), suffix)
                self.assertNotIn("DO NOT OVERWRITE", str(after_retry.data.get("text", "")), suffix)

    def test_notepad_type_result_becomes_verified_only_after_exact_readback(self):
        with patch("eva.automation.desktop.platform.system", return_value="Windows"), \
             patch("eva.automation.desktop._focus_notepad_window", return_value={"ok": True, "verified": True}), \
             patch("eva.automation.desktop._windows", return_value={"ok": True, "verified": False}), \
             patch("eva.automation.desktop._verify_notepad_text", return_value={
                 "readable": True, "verified": True, "characters": 13,
                 "message": "Notepad document text was read back and exactly matched the requested text."
             }):
            result = desktop.execute("type", "hello EVA text", target="notepad")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertIn("read back", result["message"])

    def test_notepad_type_readback_mismatch_is_not_reported_as_success(self):
        with patch("eva.automation.desktop.platform.system", return_value="Windows"), \
             patch("eva.automation.desktop._focus_notepad_window", return_value={"ok": True, "verified": True}), \
             patch("eva.automation.desktop._windows", return_value={"ok": True, "verified": False}), \
             patch("eva.automation.desktop._verify_notepad_text", return_value={
                 "readable": True, "verified": False, "characters": 5,
                 "message": "Notepad content differs from the requested text."
             }):
            result = desktop.execute("type", "hello EVA text", target="notepad")
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["error_code"], "TEXT_ENTRY_READBACK_MISMATCH")

    def test_unreadable_notepad_controls_remain_unverified_not_false_verified(self):
        with patch("eva.automation.desktop.platform.system", return_value="Windows"), \
             patch("eva.automation.desktop._focus_notepad_window", return_value={"ok": True, "verified": True}), \
             patch("eva.automation.desktop._windows", return_value={"ok": True, "verified": False}), \
             patch("eva.automation.desktop._verify_notepad_text", return_value={
                 "readable": False, "verified": False,
                 "message": "Windows did not expose readable document text."
             }):
            result = desktop.execute("type", "hello EVA text", target="notepad")
        self.assertTrue(result["ok"])
        self.assertFalse(result["verified"])
        self.assertIn("readable document text", result["message"])

    def test_unrequested_office_extension_is_not_written_as_plain_text(self):
        # The format mapper must use a format-specific writer or fail closed.
        for suffix, expected_tool in ((".docx", "files.create_docx"), (".pptx", "files.create_pptx"),
                                      (".xlsx", "files.create_xlsx"), (".pdf", "files.create_pdf"),
                                      (".rtf", "files.create_rtf"), (".odt", "files.create_odt")):
            with self.subTest(suffix=suffix):
                plan = self._plan(f"create a deep{suffix} file on desktop")
                self.assertIsNotNone(plan)
                self.assertEqual(plan["steps"][0]["tool"], expected_tool)

    def test_generated_content_can_be_saved_to_requested_office_or_pdf_format(self):
        cases = ((".docx", "files.create_docx"), (".pdf", "files.create_pdf"))
        for suffix, writer in cases:
            with self.subTest(suffix=suffix):
                prompt = f"write a paragraph about cows and save it as cattle{suffix}"
                provider = FixedProvider("Cows are domesticated cattle. They can provide milk and are often raised on farms.")
                result = self.service.plan_and_execute(prompt, provider=provider)
                self.assertTrue(result.get("ok"), result)
                save_step = result["execution"]["steps"][-1]
                self.assertEqual(save_step["tool"], writer)
                path = Path(save_step["result"]["data"]["path"])
                self.assertTrue(path.is_file())
                readback = self.service.execute_tool("files.read_document", {"path": str(path), "max_bytes": 10000})
                self.assertTrue(readback.ok, readback.message)
                self.assertIn("Cows are domesticated cattle", readback.data["text"])

    def test_topic_missing_for_factual_paragraph_is_asked_before_actions(self):
        plan = self._plan("open notepad and write a paragraph")
        self.assertEqual(plan["steps"], [])
        self.assertTrue(plan["needs_input"])
        self.assertEqual(plan["needs_input"][0]["field"], "topic")


if __name__ == "__main__":
    unittest.main()
