from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eva.automation.service import AutomationService
from eva.automation.task_planner import TaskPlanner
from eva.core.intent_understanding import analyze_intent
from eva.core.task_router import TaskRouter


class SmokeProvider:
    """A bounded fake provider for deterministic orchestration smoke tests."""

    model = "smoke-provider"

    def __init__(self, summary: str = "- Alpha\n- Beta\n- Gamma"):
        self.summary = summary
        self.plan_calls = 0
        self.text_calls = 0
        self.last_text_prompt = ""

    def generate_project_json(self, *args, **kwargs):
        self.plan_calls += 1
        return '{"goal":"empty","steps":[],"needs_input":[],"missing_capabilities":[]}'

    def generate_text(self, *args, **kwargs):
        self.text_calls += 1
        self.last_text_prompt = str(args[1] if len(args) > 1 else kwargs.get("user_prompt", ""))
        return self.summary


class HardPromptRoutingTests(unittest.TestCase):
    def setUp(self):
        self.router = TaskRouter()

    def assert_handler(self, prompts: list[str], expected_handler: str) -> None:
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                decision = self.router.route(prompt)
                self.assertEqual(decision.handler, expected_handler, msg=f"{decision!r} for {prompt!r}")

    def test_local_file_tasks_win_over_python_latest_and_report_keywords(self):
        self.assert_handler([
            "Find my latest assignment, correct its spelling only, save it as Assignment_Final.docx, then open it.",
            "Find my report, summarize it, and save both the summary and a copy of the original.",
            "List Python files inside EVA; do not generate anything.",
            "Search the project for task_router.py and show its path.",
            "Find README and summarize its first section.",
            "Find all PDFs in Downloads and list their names.",
            "In my Downloads, locate every .png file and list only the full paths.",
            "Can you list files with .py extension in the EVA repository but please do not edit them?",
            "Open my latest project README and tell me what it says.",
            "Find my newest report, fix typos only, save it as final.docx, then open the saved file.",
            "Find the file called Python_Notes.txt and show the text, not code.",
            "Locate the newest Java assignment in Documents and tell me its real filename.",
            "Search this folder for .py files. Do not edit, execute, or generate any code.",
            "Show me the folders inside Downloads, not instructions for how to do it.",
            "Read my notes and create a separate five-point summary file.",
        ], "desktop_automation")

    def test_explicit_desktop_application_intent_beats_math_or_chat(self):
        self.assert_handler([
            "Open the Documents folder in File Explorer.",
            "Use Calculator to evaluate 987 plus 654 and confirm the display.",
            "Open Notepad and write a water reminder.",
            "Open Calculator and calculate 123 * 456.",
            "Create a file on Desktop named note.txt with exact text \"Smoke test passed\".",
            "Create a folder named EVA_Smoke on my Desktop, NOT Documents.",
            "Please create a text file called packing.txt on Desktop with the three items socks, charger, and ID.",
            "Launch Notepad, type the supplied sentence exactly, but do not save a file.",
        ], "desktop_automation")

    def test_real_code_creation_and_modification_still_route_to_coding(self):
        self.assert_handler([
            "Write a Python script to rename files by date.",
            "Refactor task_router.py so project inspections never route to code generation.",
            "Build a Flask web app for tracking study time.",
            "Fix the syntax errors in my C factorial program.",
            "Create a Java program to print factorial of a number.",
            "Write a Python file named report.py to search files in a folder.",
            "Implement a function in JavaScript that sorts records by date.",
            "Generate a C program that calculates the HCF and LCM.",
        ], "coding_engine")

    def test_plain_math_routes_to_math_engine_even_when_wording_is_natural(self):
        self.assert_handler([
            "987 + 654",
            "What is 144 divided by 12?",
            "Solve x^2 - 5*x + 6 = 0",
            "What is 18 percent of 450?",
            "Find the derivative of x^3 + 2x",
            "7 to the power of 8",
            "Calculate the determinant of this 2 by 2 matrix: [[1,2],[3,4]].",
        ], "math_engine")

    def test_explicit_online_research_precedes_local_desktop_heuristics(self):
        self.assert_handler([
            "Look up free typing practice sites on the web.",
            "Search online for the Kolkata weather forecast for tomorrow.",
            "Open search results for beginner Java tutorials on YouTube.",
        ], "web_search")
        self.assertEqual(self.router.route("Please research online the latest Python stable release.").handler,
                         "desktop_automation")

    def test_existing_code_execution_is_not_misrouted_as_code_generation_or_math(self):
        for prompt in (
            "Run my Python script and tell me if it works",
            "Test this Java file with inputs 0, 1, 5, 20, and -3",
        ):
            with self.subTest(prompt=prompt):
                decision = self.router.route(prompt)
                self.assertEqual(decision.category, "CODE_TEST")
                self.assertEqual(decision.handler, "unavailable")
                self.assertFalse(decision.implemented)

    def test_explanatory_how_to_questions_are_not_code_generation(self):
        prompt = "Explain how to create a file search tool in Python"
        self.assertEqual(analyze_intent(prompt).intent, "explanation")
        self.assertEqual(self.router.route(prompt).handler, "ollama")

    def test_general_type_question_is_not_a_keyboard_typing_action(self):
        decision = self.router.route("What type of automation can you do?")
        self.assertEqual(decision.handler, "desktop_automation")
        self.assertNotEqual(decision.category, "DESKTOP_AUTOMATION")
        self.assertEqual(self.router.route("type Hello into Notepad").handler, "desktop_automation")

    def test_research_followed_by_download_is_orchestrated_not_search_only(self):
        prompt = "Search the web for the latest Nvidia driver and download it"
        decision = self.router.route(prompt)
        self.assertEqual(decision.handler, "desktop_automation")
        self.assertEqual(decision.category, "DESKTOP_AUTOMATION")

    def test_project_routing_audit_language_routes_to_project_inspection(self):
        prompt = "Can you look through my project and tell me where routing breaks?"
        self.assertEqual(analyze_intent(prompt).intent, "project_inspection")
        self.assertEqual(self.router.route(prompt).handler, "desktop_automation")

    def test_definition_and_routing_questions_are_not_executed_as_actions(self):
        self.assert_handler([
            "What does the word latest mean in a file search?",
            "Explain why the word Python in a filename should not route to the coding engine.",
            "Define task orchestration in simple words.",
        ], "ollama")
        self.assertEqual(analyze_intent("Explain why the word Python in a filename should not route to the coding engine.").intent,
                         "explanation")

    def test_conceptual_and_verb_queries_do_not_falsely_route_to_code_or_automation(self):
        prompts = [
            "What is a unit test?",
            "What is a python script?",
            "How does a compiler work?",
            "What is a directory in Linux?",
            "Can I open a file in binary mode in Python?",
            "What happens when you delete a file?",
            "Is it possible to edit a read-only file?",
            "What is the difference between test cases and unit tests?",
            "Why does open file return a descriptor?",
            "How does git commit work?",
            "What is a function in javascript?",
            "Can you explain unit tests?",
            "Is it safe to run code from the internet?",
            "Why should we write unit tests?",
        ]
        self.assert_handler(prompts, "ollama")

    def test_misleading_keywords_do_not_change_intent(self):
        cases = {
            "List Python files inside EVA; do not generate anything.": "file_inspection",
            "Write a Python file named report.py to search files in a folder.": "code_generation",
            "Please research online the latest Python stable release.": "web_browsing",
            "What does latest mean when searching a report?": "explanation",
            "Use Calculator to evaluate 987 plus 654.": "calculator_ui",
            "Find the latest report and save the summary as final.txt.": "file_inspection",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(analyze_intent(prompt).intent, expected)


class HardPromptPlanningAndExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-hard-routing-")
        self.root = Path(self.temp.name).resolve()
        self.service = AutomationService(
            allowed_roots=[self.root], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "state" / "capability_gaps.json",
            browser_headless=True,
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_latest_assignment_plan_preserves_output_filename_and_dependencies(self):
        provider = SmokeProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        plan = planner._deterministic_plan(
            "Find my latest assignment, correct its spelling only, save it as Assignment_Final.docx, then open it."
        )
        self.assertIsNotNone(plan)
        steps = {item["id"]: item for item in plan["steps"]}
        self.assertEqual(list(steps), ["find_latest", "read_source", "transform", "save_output", "open_output"])
        self.assertEqual(steps["find_latest"]["tool"], "files.find_latest")
        self.assertEqual(steps["find_latest"]["args"]["exclude_name"], "Assignment_Final.docx")
        self.assertEqual(steps["read_source"]["depends_on"], ["find_latest"])
        self.assertEqual(steps["transform"]["depends_on"], ["read_source"])
        self.assertEqual(steps["save_output"]["depends_on"], ["transform"])
        self.assertEqual(steps["open_output"]["depends_on"], ["save_output"])
        self.assertTrue(steps["save_output"]["args"]["path"].endswith("Assignment_Final.docx"))
        self.assertEqual(provider.plan_calls, 0, "Known workflow plan should be deterministic.")

    def test_report_summary_and_original_copy_are_both_created_from_real_source(self):
        source_dir = self.root / "source"
        source_dir.mkdir()
        source = source_dir / "quarterly-report.txt"
        source_content = "Revenue increased. Costs declined. Customer retention improved."
        source.write_text(source_content, encoding="utf-8")
        provider = SmokeProvider("Summary based only on source: revenue increased; costs declined.")

        result = self.service.handle_text(
            "Find my report, summarize it, and save both the summary and a copy of the original.",
            provider=provider,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("action"), "task_plan")
        steps = {step["id"]: step for step in result["execution"]["steps"]}
        self.assertEqual({key for key, step in steps.items() if step["status"] == "succeeded"},
                         {"find_source", "read_source", "summarize", "save_summary", "copy_original"})
        output_dir = self.root / "Documents" / "EVA-created-files"
        summary_files = list(output_dir.glob("report-summary*.txt"))
        original_copies = list(output_dir.glob("quarterly-report*.txt"))
        self.assertEqual(len(summary_files), 1)
        self.assertEqual(summary_files[0].read_text(encoding="utf-8"), provider.summary)
        self.assertEqual(len(original_copies), 1)
        self.assertEqual(original_copies[0].read_bytes(), source.read_bytes())
        self.assertEqual(provider.plan_calls, 0)
        self.assertEqual(provider.text_calls, 1)

    def test_exact_file_and_folder_creation_respect_desktop_destination_not_documents(self):
        # In this isolated test workspace, model the real Windows Desktop folder
        # as an existing location beneath the service's approved root.
        (self.root / "Desktop").mkdir(exist_ok=True)
        file_result = self.service.handle_text(
            'Create a file on Desktop named note.txt with exact text "Smoke test passed".'
        )
        folder_result = self.service.handle_text(
            "Create a folder named EVA_Smoke on my Desktop, NOT Documents."
        )
        expected_file = self.root / "Desktop" / "note.txt"
        expected_folder = self.root / "Desktop" / "EVA_Smoke"
        self.assertTrue(file_result.get("ok"), file_result)
        self.assertTrue(folder_result.get("ok"), folder_result)
        self.assertEqual(expected_file.read_text(encoding="utf-8"), "Smoke test passed")
        self.assertTrue(expected_folder.is_dir())
        self.assertFalse((self.root / "Documents" / "EVA_Smoke").exists())

    def test_generated_outputs_are_not_reselected_as_source_on_repeat_runs(self):
        source_dir = self.root / "project"
        source_dir.mkdir()
        source = source_dir / "report.txt"
        source.write_text("Original report facts only.", encoding="utf-8")
        provider = SmokeProvider("Summary: original report facts.")
        prompt = "Find my report, summarize it, and save both the summary and a copy of the original."
        first = self.service.handle_text(prompt, provider=provider)
        second = self.service.handle_text(prompt, provider=provider)
        self.assertTrue(first.get("ok"), first)
        self.assertTrue(second.get("ok"), second)
        out = self.root / "Documents" / "EVA-created-files"
        copies = [path for path in out.glob("report*.txt") if "summary" not in path.name]
        summaries = list(out.glob("report-summary*.txt"))
        self.assertEqual(len(copies), 2)
        self.assertEqual(len(summaries), 2)
        self.assertTrue(all(path.read_text(encoding="utf-8") == "Original report facts only." for path in copies))

    def test_ambiguous_report_source_asks_then_resumes_after_user_selects_a_candidate(self):
        source_dir = self.root / "source"
        source_dir.mkdir()
        report_a = source_dir / "report-a.txt"
        report_b = source_dir / "report-b.txt"
        report_a.write_text("Report A: unique source content.", encoding="utf-8")
        report_b.write_text("Report B: different source content.", encoding="utf-8")
        provider = SmokeProvider("Summary based on the selected report.")

        first = self.service.handle_text(
            "Find my report, summarize it, and save both the summary and a copy of the original.",
            provider=provider,
        )
        self.assertFalse(first.get("ok"), first)
        self.assertEqual(first.get("action"), "need_input")
        self.assertEqual(first.get("clarification_type"), "file_choice")
        self.assertIn("1.", first["questions"][0])
        self.assertTrue(self.service.has_pending_task_clarification())
        self.assertFalse((self.root / "Documents" / "EVA-created-files").exists())

        candidates = self.service._pending_task_clarification["clarification_candidates"]
        selected_path = Path(candidates[1]["path"])
        second = self.service.handle_text("2", provider=provider)
        self.assertTrue(second.get("ok"), second)
        self.assertEqual(second.get("action"), "task_plan")
        self.assertFalse(self.service.has_pending_task_clarification())
        output_dir = self.root / "Documents" / "EVA-created-files"
        summary_files = list(output_dir.glob("report-summary*.txt"))
        copied_files = [item for item in output_dir.glob("report-*.txt") if "summary" not in item.name]
        self.assertEqual(len(summary_files), 1)
        self.assertEqual(summary_files[0].read_text(encoding="utf-8"), provider.summary)
        self.assertTrue(any(item.read_bytes() == selected_path.read_bytes() for item in copied_files))
        self.assertIn(selected_path.read_text(encoding="utf-8"), provider.last_text_prompt)
        self.assertEqual(provider.plan_calls, 0)
        self.assertEqual(provider.text_calls, 1)


if __name__ == "__main__":
    unittest.main()
