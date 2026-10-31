from __future__ import annotations

import tempfile
import re
import unittest
from pathlib import Path
from unittest import mock

from eva.automation.service import AutomationService
from eva.automation.engine import ToolResult
from eva.automation.task_planner import TaskPlanner
from eva.core.intent_understanding import analyze_intent
from eva.core.task_router import TaskRouter


class IntentOrchestrationStrengtheningTests(unittest.TestCase):
    def setUp(self):
        self.router = TaskRouter()

    def test_previously_misrouted_prompts_get_semantic_intents(self):
        cases = {
            "Create a folder called EVA Demo in my approved workspace.": "file_creation",
            "Create an Excel expense tracker with columns for date, category, description and amount.": "office_artifact_creation",
            "Convert my expenses CSV into JSON and save the result next to the original.": "data_conversion",
            "Compress the project folder into a ZIP archive.": "archive_operation",
            "Inspect this project and tell me its folder structure and main entry point.": "project_inspection",
            "Open Wikipedia, find the page about artificial intelligence, and tell me the main headings.": "web_browsing",
            "Open a webpage, inspect its buttons, and click the button named Search.": "browser_control",
            "Check the current page for browser errors and failed requests.": "browser_diagnostics",
            "Show me the available applications and current system status.": "system_status",
            "Copy this text to the clipboard: EVA is ready": "desktop_action",
            "Open amazon website and add white shirt in my cart.": "shopping_workflow",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(analyze_intent(prompt).intent, expected)
                self.assertEqual(self.router.route(prompt).handler, "desktop_automation")

    def test_natural_language_variants_route_by_goal_not_incidental_keywords(self):
        cases = {
            "Check whether my PC is running out of memory and show the top memory-consuming apps.": "system_status",
            "I want you to organize my downloads by file type.": "file_organization",
            "Search my computer for resume and open the best match.": "file_inspection",
            "Find information on the official Python docs, summarize the installation steps, and save them to a file.": "web_browsing",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                analysis = analyze_intent(prompt)
                self.assertEqual(analysis.intent, expected)
                self.assertEqual(self.router.route(prompt).handler, "desktop_automation")

        compound = "Could you open the browser and find the official page for Python, then explain the first section?"
        self.assertEqual(self.router.route(compound).handler, "desktop_automation")

    def test_literal_clipboard_write_has_a_deterministic_exact_payload_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AutomationService(allowed_roots=[root], audit_path=None,
                                        workflows_dir=root / "workflows",
                                        capability_gaps_path=root / "state" / "gaps.json",
                                        browser_headless=True)
            try:
                planner = TaskPlanner(service, provider=None, gap_store=service.capability_gaps)
                plan = planner._deterministic_plan("Copy this text to the clipboard: EVA is ready")
                self.assertIsNotNone(plan)
                self.assertEqual(len(plan["steps"]), 1)
                self.assertEqual(plan["steps"][0]["tool"], "desktop.clipboard.write")
                self.assertEqual(plan["steps"][0]["args"]["value"], "EVA is ready")
            finally:
                service.close()

    def test_compound_web_research_is_not_misclassified_as_archive_extraction(self):
        prompt = ("Search the web for an introduction to Python. Open a relevant result, "
                  "extract its main headings and useful information, then create a short report in a text file.")
        self.assertEqual(analyze_intent(prompt).intent, "web_research")
        self.assertEqual(self.router.route(prompt).handler, "desktop_automation")

    def test_date_inside_file_task_does_not_steal_route(self):
        prompt = "Create a folder named EVA_Demo and a report.txt containing today's date and three tasks."
        self.assertEqual(self.router.route(prompt).handler, "desktop_automation")

    def test_plain_date_question_still_uses_clock(self):
        self.assertEqual(self.router.route("What is today's date?").handler, "system_time")

    def test_office_artifact_plan_uses_real_xlsx_writer_without_fabricated_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AutomationService(allowed_roots=[root], audit_path=None,
                                        workflows_dir=root / "workflows",
                                        capability_gaps_path=root / "state" / "gaps.json",
                                        browser_headless=True)
            try:
                planner = TaskPlanner(service, provider=None, gap_store=service.capability_gaps)
                plan = planner._deterministic_plan(
                    "Create an Excel expense tracker with columns for date, category, description and amount."
                )
                self.assertIsNotNone(plan)
                self.assertEqual(plan["steps"][0]["tool"], "files.create_xlsx")
                self.assertEqual(plan["steps"][0]["args"]["content"], "date,category,description,amount")
                self.assertTrue(plan["steps"][0]["args"]["path"].endswith(".xlsx"))
                self.assertEqual(plan["needs_input"], [])
            finally:
                service.close()

    def test_named_python_file_is_read_and_explained_without_running_it_or_needing_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.py"
            original = (
                '"""Tiny arithmetic helpers."""\n\n'
                'def add(a, b):\n'
                '    """Return the sum of two values."""\n'
                '    return a + b\n\n'
                'def main():\n'
                '    print(add(2, 3))\n\n'
                'if __name__ == "__main__":\n'
                '    main()\n'
            )
            source.write_text(original, encoding="utf-8")
            service = AutomationService(allowed_roots=[root], audit_path=None,
                                        workflows_dir=root / "workflows",
                                        capability_gaps_path=root / "state" / "gaps.json",
                                        browser_headless=True)
            try:
                result = service.handle_text(
                    "Explain what sample.py does before changing anything.", provider=None
                )
                self.assertTrue(result.get("ok"), result)
                steps = result["execution"]["steps"]
                self.assertEqual([step["tool"] for step in steps], [
                    "files.resolve_by_name", "files.read_document", "content.generate_text"
                ])
                explanation = steps[-1]["result"]["data"]["text"]
                self.assertIn("Tiny arithmetic helpers", explanation)
                self.assertIn("add(a, b)", explanation)
                self.assertIn("not executed or modified", explanation)
                self.assertEqual(source.read_text(encoding="utf-8"), original)
            finally:
                service.close()

    def test_project_inspection_returns_bounded_tree_and_entry_point_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "helper.py").write_text("def helper():\n    return True\n", encoding="utf-8")
            service = AutomationService(allowed_roots=[root], audit_path=None,
                                        workflows_dir=root / "workflows",
                                        capability_gaps_path=root / "state" / "gaps.json",
                                        browser_headless=True)
            try:
                result = service.execute_tool("developer.inspect_project", {"path": str(root)})
                self.assertTrue(result.ok, result.as_dict())
                self.assertIn("main.py", result.data["tree"])
                self.assertIn("src/helper.py", result.data["tree"])
                self.assertEqual(result.data["entry_point_candidates"][0]["relative_path"], "main.py")
                self.assertFalse(result.data["source_executed"])
            finally:
                service.close()

    def test_folder_name_suffix_is_not_absorbed_as_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = AutomationService(allowed_roots=[root], audit_path=None,
                                        workflows_dir=root / "workflows",
                                        capability_gaps_path=root / "state" / "gaps.json",
                                        browser_headless=True)
            try:
                result = service.handle_text("Create a folder called EVA Demo in my approved workspace.")
                self.assertTrue(result.get("ok"), result)
                created_path = Path(result["data"]["path"])
                self.assertEqual(created_path.name, "EVA Demo")
            finally:
                service.close()

class DeterministicWorkflowExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-strong-routing-")
        self.root = Path(self.temp.name).resolve()
        self.service = AutomationService(allowed_roots=[self.root], audit_path=None,
                                         workflows_dir=self.root / "workflows",
                                         capability_gaps_path=self.root / "state" / "gaps.json",
                                         browser_headless=True)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_csv_to_json_uses_real_source_and_sibling_output(self):
        source = self.root / "expenses.csv"
        source.write_text("date,category,amount\n2026-01-01,food,100\n", encoding="utf-8")
        result = self.service.handle_text(
            "Convert my expenses CSV into JSON and save the result next to the original."
        )
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("verified"), result)
        destination = self.root / "expenses.json"
        self.assertTrue(destination.is_file())
        self.assertIn('"category": "food"', destination.read_text(encoding="utf-8"))

    def test_excel_expense_tracker_creates_real_workbook(self):
        result = self.service.handle_text(
            "Create an Excel expense tracker with columns for date, category, description and amount."
        )
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("verified"), result)
        step = result["execution"]["steps"][0]
        self.assertEqual(step["result"]["data"]["format"], "xlsx")
        self.assertEqual(step["result"]["data"]["rows"], 1)
        self.assertTrue(Path(step["result"]["data"]["path"]).is_file())

    def test_word_document_request_creates_real_docx_template(self):
        result = self.service.handle_text(
            "Create a Word document with a title, introduction, three key points and a conclusion."
        )
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("verified"), result)
        step = result["execution"]["steps"][0]
        self.assertEqual(step["result"]["data"]["format"], "docx")
        self.assertTrue(Path(step["result"]["data"]["path"]).is_file())

    def test_wikipedia_heading_request_becomes_api_free_browse_chain(self):
        planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)
        plan = planner._deterministic_plan(
            "Open Wikipedia, find the page about artificial intelligence, and tell me the main headings."
        )
        self.assertEqual([step["tool"] for step in plan["steps"]], ["web.browse", "content.generate_text"])
        self.assertEqual(plan["steps"][0]["args"]["query"], "artificial intelligence")
        self.assertIn("headings", plan["steps"][1]["args"]["prompt"].casefold())

    def test_browser_diagnostics_and_named_control_use_browser_tools_not_search(self):
        planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)
        diagnostics = planner._deterministic_plan("Check the current page for browser errors and failed requests.")
        self.assertEqual(diagnostics["steps"][0]["tool"], "browser.diagnostics")
        control = planner._deterministic_plan(
            "Open a webpage, inspect its buttons, and click the button named Search."
        )
        self.assertEqual([step["tool"] for step in control["steps"]], ["browser.snapshot", "browser.click_by_role"])
        self.assertEqual(control["steps"][1]["args"]["name"], "Search")

    def test_folder_and_dated_report_are_one_verified_workflow(self):
        prompt = "Create a folder named EVA_Demo and a report.txt containing today's date and three tasks."
        result = self.service.handle_text(prompt)
        self.assertTrue(result.get("ok"), result)
        report = self.root / "Documents" / "EVA-created-files" / "EVA_Demo" / "report.txt"
        self.assertTrue(report.is_file())
        content = report.read_text(encoding="utf-8")
        self.assertIn("Date:", content)
        self.assertEqual(len([line for line in content.splitlines() if re.match(r"^\d+\. ", line)]), 3)
        self.assertTrue(result.get("verified"), result)

    def test_compress_current_project_uses_real_zip_tool_and_verifies_archive(self):
        import os
        import zipfile
        project = self.root / "demo-project"
        project.mkdir()
        (project / "main.py").write_text("print('ready')\n", encoding="utf-8")
        previous = Path.cwd()
        try:
            os.chdir(project)
            result = self.service.handle_text("Compress the project folder into a ZIP archive.")
        finally:
            os.chdir(previous)
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("verified"), result)
        destination = Path(result["data"]["destination"])
        self.assertTrue(destination.is_file())
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("main.py", archive.namelist())

    def test_shopping_request_never_claims_cart_updated_without_evidence(self):
        def fake_execute(name, arguments=None, **kwargs):
            if name == "browser.tabs.open":
                return ToolResult(True, name, {"url": arguments["url"], "opened": True}, "Opened search page.", verified=True)
            if name == "browser.snapshot":
                return ToolResult(True, name, {"snapshot_id": 1, "controls": ["Search"]}, "Page inspected.", verified=True)
            raise AssertionError(f"Unexpected tool call: {name}")
        with mock.patch.object(self.service, "execute_tool", side_effect=fake_execute):
            result = self.service.handle_text("Open amazon website and add white shirt in my cart.")
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "shopping_selection_required")
        self.assertFalse(result["cart_updated"])
        self.assertIn("exact product", result["message"])

    def test_browse_report_saves_inside_named_folder(self):
        planner = TaskPlanner(self.service, provider=None, gap_store=self.service.capability_gaps)
        plan = planner._deterministic_plan(
            "Search the web for an introduction to Python. Open a relevant result, extract its main headings and useful information, then create a short report in a text file inside EVA_Demo."
        )
        self.assertEqual([step["tool"] for step in plan["steps"]], [
            "web.browse", "content.generate_text", "files.create_unique_text_file"
        ])
        self.assertEqual(Path(plan["steps"][2]["args"]["directory"]), self.root / "EVA_Demo")
