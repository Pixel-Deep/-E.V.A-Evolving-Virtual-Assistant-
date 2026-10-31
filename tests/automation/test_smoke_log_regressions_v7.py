"""Regression coverage for failures found in the 30-prompt EVA smoke run."""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from eva.automation.engine.models import ToolResult
from eva.automation.service import AutomationService
from eva.core.intent_understanding import analyze_intent
from eva.core.task_router import TaskRouter


class SmokeLogRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-smoke-log-v7-")
        self.root = Path(self.temp.name).resolve()
        self.desktop = self.root / "Desktop"
        self.downloads = self.root / "Downloads"
        self.documents = self.root / "Documents"
        self.desktop.mkdir()
        self.downloads.mkdir()
        self.documents.mkdir()
        self.service = AutomationService(
            allowed_roots=[self.root], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "state" / "capability_gaps.json",
            browser_headless=True,
        )
        self.router = TaskRouter()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_logged_prompts_route_to_the_intended_specialist(self):
        cases = [
            ("Convert my expenses CSV into JSON and save the result next to the original.", "desktop_automation", "data_conversion"),
            ("Create an Excel expense tracker with columns for date, category, description and amount.", "desktop_automation", "office_artifact_creation"),
            ("Create a Word document with a title, introduction, three key points and a conclusion.", "desktop_automation", "office_artifact_creation"),
            ("Compress the project folder into a ZIP archive.", "desktop_automation", "archive_operation"),
            ("Inspect this project and tell me its folder structure and main entry point", "desktop_automation", "project_inspection"),
            ("Check the Git status and tell me which files have changed", "desktop_automation", "git_status"),
            ("Explain this task in simple words: organize all my Downloads by file type.", "ollama", "explanation"),
            ("Open three apps in this order: Notepad, Calculator, then Paint.", "desktop_automation", "multi_app_open"),
        ]
        for prompt, handler, intent in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.router.route(prompt).handler, handler)
                self.assertEqual(analyze_intent(prompt).intent, intent)

    def test_folder_named_in_approved_workspace_uses_only_the_folder_name(self):
        out = self.service.handle_text("Create a folder called EVA Demo in my approved workspace.", provider=None)
        expected = self.root / "Documents" / "EVA-created-files" / "EVA Demo"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(expected.is_dir())
        self.assertEqual(out.get("folder_name"), "EVA Demo")
        self.assertFalse((expected.parent / "EVA Demo in my approved workspace").exists())

    def test_folder_for_purpose_uses_a_clean_semantic_name(self):
        out = self.service.handle_text("Make a folder for my college documents.", provider=None)
        expected = self.root / "Documents" / "EVA-created-files" / "College Documents"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(expected.is_dir())
        self.assertEqual(out.get("folder_name"), "College Documents")

    def test_folder_then_nested_file_creates_both_objects_in_order(self):
        out = self.service.handle_text(
            "Create a folder named StudyPlan on my Desktop, then create a file inside it named timetable.txt.",
            provider=None,
        )
        target_dir = self.desktop / "StudyPlan"
        target_file = target_dir / "timetable.txt"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(target_dir.is_dir())
        self.assertTrue(target_file.is_file())
        self.assertEqual(target_file.read_bytes(), b"")
        self.assertEqual([step["tool"] for step in out["steps"]], ["files.create_directory", "files.create_text_file"])

    def test_word_document_request_creates_a_real_docx(self):
        out = self.service.handle_text(
            "Create a Word document with a title, introduction, three key points and a conclusion.",
            provider=None,
        )
        target = self.root / "Documents" / "EVA-created-files" / "structured-document.docx"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(target.is_file())
        self.assertTrue(zipfile.is_zipfile(target))
        with zipfile.ZipFile(target) as archive:
            content = archive.read("word/document.xml").decode("utf-8")
        for expected in ("Structured Document", "Introduction", "Key Point 1", "Key Point 2", "Key Point 3", "Conclusion"):
            self.assertIn(expected, content)
        self.assertEqual(out["plan"]["steps"][0]["tool"], "files.create_docx")

    def test_titled_desktop_report_creates_named_docx_with_expected_sections(self):
        out = self.service.handle_text(
            "Create a report titled 'Computer Project' with three headings and save it on my Desktop.",
            provider=None,
        )
        target = self.desktop / "Computer Project.docx"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(target.is_file())
        self.assertTrue(zipfile.is_zipfile(target))
        with zipfile.ZipFile(target) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        for expected in ("Computer Project", "Introduction", "Key Points", "Conclusion"):
            self.assertIn(expected, document)
        self.assertEqual(out["plan"]["steps"][0]["tool"], "files.create_docx")

    def test_csv_to_json_converts_the_actual_file_to_its_sibling(self):
        source = self.downloads / "expenses.csv"
        source.write_text("Date,Category,Amount\n2026-10-23,Food,120\n", encoding="utf-8")
        out = self.service.handle_text(
            "Convert my expenses CSV into JSON and save the result next to the original.", provider=None
        )
        target = source.with_suffix(".json")
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(target.is_file())
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data, [{"Date": "2026-10-23", "Category": "Food", "Amount": "120"}])
        self.assertEqual(out["plan"]["steps"][-1]["tool"], "data.csv_to_json")

    def test_excel_expense_tracker_creates_a_real_workbook_with_requested_columns(self):
        out = self.service.handle_text(
            "Create an Excel expense tracker with columns for date, category, description and amount.",
            provider=None,
        )
        target = self.root / "Documents" / "EVA-created-files" / "expense-tracker.xlsx"
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(zipfile.is_zipfile(target))
        with zipfile.ZipFile(target) as archive:
            xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
            shared = archive.read("xl/sharedStrings.xml").decode("utf-8") if "xl/sharedStrings.xml" in archive.namelist() else ""
        self.assertTrue(all(column in (xml + shared).casefold() for column in ("date", "category", "description", "amount")))
        self.assertEqual(out["plan"]["steps"][0]["tool"], "files.create_xlsx")

    def test_largest_files_search_includes_subfolders_and_sorts_by_actual_size(self):
        (self.downloads / "small.txt").write_bytes(b"abc")
        (self.downloads / "medium.pdf").write_bytes(b"x" * 9)
        nested = self.downloads / "Telegram Desktop"
        nested.mkdir()
        biggest = nested / "largest.bin"
        biggest.write_bytes(b"x" * 40)
        with patch("eva.automation.service.Path.home", return_value=self.root):
            out = self.service.handle_text("List the five largest files in my Downloads folder.", provider=None)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out["action"], "largest_download_files")
        self.assertEqual(Path(out["files"][0]["path"]), biggest)
        self.assertEqual([item["size_bytes"] for item in out["files"]], [40, 9, 3])
        self.assertFalse(out["truncated"])

    def test_explanation_of_download_organization_never_starts_organization(self):
        with patch.object(self.service, "_plan_download_organization", side_effect=AssertionError("must not organize")) as organize:
            out = self.service.handle_text(
                "Explain this task in simple words: organize all my Downloads by file type.", provider=None
            )
        organize.assert_not_called()
        self.assertEqual(out["action"], "explanation")
        self.assertFalse(out["executed"])
        self.assertIn("No files are moved", out["message"])

    def test_ordered_app_prompt_uses_named_targets_in_requested_order(self):
        calls = []
        def fake_execute(tool, args, **kwargs):
            calls.append((tool, dict(args)))
            return ToolResult(True, tool, {"target": args.get("target")}, "Mock launch succeeded", verified=True)
        with patch.object(self.service, "execute_tool", side_effect=fake_execute):
            out = self.service.handle_text("Open three apps in this order: Notepad, Calculator, then Paint.", provider=None)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out["ordered_targets"], ["notepad", "calculator", "paint"])
        self.assertEqual([args["target"] for tool, args in calls], ["notepad", "calculator", "paint"])
        self.assertEqual(len(out["steps"]), 3)

    def test_calculator_followup_is_removed_before_expression_is_sent_to_ui(self):
        calls = []
        def fake_execute(tool, args, **kwargs):
            calls.append((tool, dict(args)))
            if tool == "desktop.type_notepad":
                return ToolResult(True, tool, {"value": args["value"]}, "Mocked Notepad typing", verified=True)
            return ToolResult(True, tool, {"target": args.get("target")}, "Mocked app launch", verified=True)
        ui_result = {"ok": True, "verified": True, "result": "12", "display": "Display is 12"}
        with patch.object(self.service, "execute_tool", side_effect=fake_execute), \
             patch("eva.automation.desktop.calculate_in_windows_calculator", return_value=ui_result) as calc_ui:
            out = self.service.handle_text(
                "Open Calculator, calculate 144 divided by 12, then open Notepad and write the answer.",
                provider=None,
            )
        self.assertTrue(out.get("ok"), out)
        calc_ui.assert_called_once_with("144 divided by 12")
        self.assertEqual([item[1].get("target") for item in calls if item[0] == "desktop.open_target"], ["calculator", "notepad"])
        self.assertIn(("desktop.type_notepad", {"value": "12", "target": "notepad"}), calls)

    def test_project_inspection_uses_active_approved_project_and_reports_entry_point(self):
        project = self.root / "EVA"
        project.mkdir()
        (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (project / "README.md").write_text("# Project\n", encoding="utf-8")
        with patch("eva.automation.service.Path.cwd", return_value=project):
            out = self.service.handle_text(
                "Inspect this project and tell me its folder structure and main entry point", provider=None
            )
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out["action"], "project_inspection")
        self.assertTrue(any(Path(item).name == "main.py" for item in out["data"]["tree"]))
        self.assertTrue(any(Path(item["path"]).name == "main.py" for item in out["data"]["entry_point_candidates"]))
        self.assertNotIn("${user_goal.path}", json.dumps(out))

    def test_git_status_defaults_to_the_active_approved_project_without_requesting_path(self):
        project = self.root / "EVA"
        project.mkdir()
        calls = []
        def fake_execute(tool, args, **kwargs):
            calls.append((tool, dict(args)))
            return ToolResult(True, tool, {"branch": "main", "changed_files": ["main.py"]}, "Git status read", verified=True)
        with patch("eva.automation.service.Path.cwd", return_value=project), \
             patch.object(self.service, "execute_tool", side_effect=fake_execute):
            out = self.service.handle_text("Check the Git status and tell me which files have changed", provider=None)
        self.assertEqual(out["action"], "git_status")
        self.assertEqual(calls, [("developer.git_status", {"path": str(project)})])

    def test_git_non_repository_error_is_explained_without_claiming_changed_files(self):
        project = self.root / "EVA"
        project.mkdir()
        failure = ToolResult(False, "developer.git_status", {}, "fatal: not a git repository",
                             "NOT_A_GIT_REPOSITORY", verified=False)
        with patch("eva.automation.service.Path.cwd", return_value=project), \
             patch.object(self.service, "execute_tool", return_value=failure):
            out = self.service.handle_text("Check the Git status and tell me which files have changed", provider=None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["action"], "git_status")
        self.assertIn("not inside a repository", out["message"])
        self.assertIn("No files were modified", out["message"])

    def test_archive_project_folder_uses_active_project_and_is_integrity_checked(self):
        project = self.root / "EVA"
        project.mkdir()
        (project / "main.py").write_text("print('archive test')\n", encoding="utf-8")
        with patch("eva.automation.service.Path.cwd", return_value=project):
            out = self.service.handle_text("Compress the project folder into a ZIP archive.", provider=None)
        self.assertTrue(out.get("ok"), out)
        target = Path(out["data"]["destination"])
        self.assertTrue(target.is_file())
        with zipfile.ZipFile(target) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("main.py", archive.namelist())

    def test_explain_python_file_finds_workspace_candidates_instead_of_requesting_paste(self):
        project = self.root / "EVA"
        project.mkdir()
        (project / "main.py").write_text("print('main')\n", encoding="utf-8")
        (project / "helper.py").write_text("def helper():\n    return True\n", encoding="utf-8")
        with patch("eva.automation.service.Path.cwd", return_value=project):
            out = self.service.handle_text("Explain what this Python file does before changing anything.", provider=None)
        self.assertEqual(out["action"], "python_file_candidates")
        self.assertEqual(out["count"], 2)
        self.assertTrue(all(item.get("path", "").endswith(".py") for item in out["candidates"]))
        self.assertIn("filename/path", out["message"])

    def test_terminal_period_is_ignored_for_registered_notepad_target(self):
        with patch("eva.automation.desktop.execute", return_value={"ok": True, "verified": True, "message": "Notepad launched"}) as native:
            out = self.service.execute_tool("desktop.open_target", {"target": "notepad."})
        self.assertTrue(out.ok, out)
        native.assert_called_once_with("app_open", "notepad")

    def test_plain_natural_language_open_notepad_period_reaches_clean_alias(self):
        with patch("eva.automation.desktop.execute", return_value={"ok": True, "verified": True, "opened": "Notepad"}) as native:
            out = self.service.handle_text("Open Notepad.", provider=None)
        self.assertTrue(out.get("ok"), out)
        native.assert_called_once_with("app_open", "notepad")

    def test_paint_uses_native_windows_process_launch_not_powershell(self):
        from eva.automation import desktop
        with patch("eva.automation.desktop.platform.system", return_value="Windows"), \
             patch.dict("eva.automation.desktop.os.environ", {"WINDIR": r"C:\Windows"}), \
             patch("eva.automation.desktop.os.path.isfile", return_value=True), \
             patch("eva.automation.desktop.subprocess.Popen") as process:
            out = desktop._try_open_native_app("paint")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["opened"], "Paint")
        self.assertEqual(out["method"], "native_process_launch")
        command = process.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertTrue(command[0].lower().endswith("mspaint.exe"), command)
        self.assertNotIn("powershell", " ".join(command).casefold())


if __name__ == "__main__":
    unittest.main()
