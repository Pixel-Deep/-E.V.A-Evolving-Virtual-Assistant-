"""Regression tests for the automation prompts that previously fell through to chat/codegen."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from eva.automation.engine.models import ToolResult
from eva.automation.service import AutomationService
from eva.core.task_router import TaskRouter


CRITICAL_PROMPTS = [
    "Show all registered automation tools grouped by category, including required permissions and dependencies.",
    "Show me the usage instructions, parameters, limitations, and an example for every available automation tool.",
    "Open Notepad and create a new text document.",
    "Show me the running applications and processes, including their names and resource usage.",
    "Open my browser and Notepad, arrange them for multitasking side by side.",
    "Read the current clipboard text and save a copy to a new text file in my approved working folder.",
    "Find all PDF files in my Downloads folder and list their names, sizes, and locations.",
    "Organize my Downloads folder by file type. Preview all planned changes before moving anything.",
    "Find duplicate files in Downloads using file hashes. Do not delete anything; show me a report.",
    "Create a ZIP archive of my selected project folder, exclude caches and temporary files, and verify the archive.",
    "Open my browser and navigate to the website I specify.",
    "Open this URL, download the report to my chosen folder, and verify that the file exists and is not empty https://en.wikipedia.org/wiki/Internet_service_provider",
    "Visit these approved URLs, extract headings and facts, save results with source URLs, and create a summary report.",
    "Show Git status, branch, and diff summary for project path.",
    "Analyze errors in project, propose a minimal patch, run tests, and show changed files.",
    "Run registered build, lint, and test steps with timeouts, allowed commands, captured logs, and verification.",
    "Check URL reachability and HTTP status for https://example.com",
    "Create a reusable workflow to open apps, check project folder, and show pending tasks.",
    "Create a project health report.",
    "Break this task into deterministic steps, map each step to registered tools, identify missing capabilities, and create a validated workflow.",
]


class IntentRouterRegressionTests(unittest.TestCase):
    def setUp(self):
        self.router = TaskRouter()

    def test_critical_automation_prompts_do_not_route_to_chat_or_coding(self):
        for prompt in CRITICAL_PROMPTS:
            with self.subTest(prompt=prompt):
                decision = self.router.route(prompt)
                self.assertEqual(decision.handler, "desktop_automation", msg=decision)

    def test_real_code_generation_still_routes_to_coding(self):
        self.assertEqual(self.router.route("write a Python script to automate volume control").handler, "coding_engine")

    def test_navigation_verbs_multi_destination_routes_to_desktop_automation(self):
        decision = self.router.route("go to instagram and facebook")
        self.assertEqual(decision.handler, "desktop_automation")

    def test_run_tests_for_my_project_routes_to_code_test_not_chat(self):
        decision = self.router.route("run tests for my project")
        self.assertEqual(decision.handler, "unavailable")
        self.assertEqual(decision.category, "CODE_TEST")
        self.assertIn("code runner", decision.reason.lower())

    def test_ordered_apps_routing_and_multi_action_referential_query(self):
        decision = self.router.route("open apps in this order Spotify, GitHub, YouTube")
        self.assertEqual(decision.handler, "desktop_automation")
        from eva.automation.desktop import _multi_action
        with mock.patch("eva.automation.desktop.execute", return_value={"ok": True, "launched": True}) as mock_exec:
            res = _multi_action("play baby on youtube then play the same on spotify")
            self.assertIsNotNone(res)
            self.assertTrue(res["ok"])
            calls = [c[0] for c in mock_exec.call_args_list]
            self.assertEqual(calls, [("play_youtube", "baby"), ("play_spotify", "baby")])

    def test_orchestrator_dispatches_registry_help_without_calling_the_model(self):
        from eva.core.orchestrator import EVAOrchestrator
        fake_provider = mock.MagicMock()
        fake_provider.model = "test-provider-not-used"
        fake_provider.chat_stream.side_effect = AssertionError("Automation help must not fall through to the model")
        service = AutomationService(allowed_roots=[Path(tempfile.gettempdir())], audit_path=None, browser_headless=True)
        try:
            with mock.patch("eva.core.orchestrator.OllamaProvider", return_value=fake_provider), \
                 mock.patch("eva.core.orchestrator.run_desktop_automation", side_effect=service.handle_text):
                assistant = EVAOrchestrator()
                response = assistant.respond(CRITICAL_PROMPTS[0])
            self.assertIn("Registered deterministic automation tools:", response)
            self.assertIn("files.find_duplicates", response)
            fake_provider.chat_stream.assert_not_called()
        finally:
            service.close()

    def test_browser_followup_without_url_asks_for_input(self):
        prompt = "Open my browser and navigate to the website I specify."
        self.assertEqual(self.router.route(prompt).handler, "desktop_automation")

    def test_eva_workspace_preview_is_a_scoped_read_only_listing(self):
        temp = tempfile.TemporaryDirectory(prefix="eva-workspace-preview-")
        root = Path(temp.name).resolve()
        (root / "visible.txt").write_text("ok", encoding="utf-8")
        service = AutomationService(allowed_roots=[root], audit_path=None, browser_headless=True)
        try:
            with mock.patch.object(service, "plan_and_execute", side_effect=AssertionError("listing must not use Ollama")):
                result = service.handle_text("Show me a preview of files inside my EVA workspace.")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["action"], "workspace_preview")
            self.assertEqual(result["path"], str(root))
            self.assertIn("visible.txt", {item.get("name") for item in result["entries"]})
        finally:
            service.close()
            temp.cleanup()


class AutomationPromptExecutionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-automation-regression-")
        self.root = Path(self.temp.name).resolve()
        self.service = AutomationService(allowed_roots=[self.root], audit_path=None, browser_headless=True,
                                         capability_gaps_path=self.root / "capability_gaps.json")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_tool_catalog_and_help_come_from_live_registry(self):
        catalog = self.service.handle_text(CRITICAL_PROMPTS[0])
        self.assertTrue(catalog["ok"])
        self.assertEqual(catalog["action"], "tool_catalog")
        names = {tool["name"] for tool in catalog["tools"]}
        self.assertIn("files.find_duplicates", names)
        self.assertIn("desktop.processes.list", names)
        self.assertIn("developer.git_status", names)
        self.assertTrue(all(tool.get("dependencies") for tool in catalog["tools"]))

        help_result = self.service.handle_text(CRITICAL_PROMPTS[1])
        self.assertEqual(help_result["action"], "tool_help")
        self.assertEqual(help_result["count"], len(names))
        self.assertTrue(all("input_schema" in item and "example" in item and "dependencies" in item
                            for item in help_result["tools"]))

    def test_single_tool_help_filters_by_exact_registered_name(self):
        result = self.service.handle_text("/tool files.search")
        self.assertTrue(result["ok"])
        self.assertEqual([item["name"] for item in result["tools"]], ["files.search"])

    def test_pdf_search_returns_real_names_sizes_and_locations(self):
        downloads = self.root / "Downloads"
        downloads.mkdir()
        pdf = downloads / "notes.pdf"
        pdf.write_bytes(b"%PDF smoke test")
        (downloads / "readme.txt").write_text("not a PDF", encoding="utf-8")
        out = self.service.handle_text(f'Find all PDF files in "{downloads}" and list their names, sizes, and locations.')
        self.assertTrue(out["ok"])
        matches = out["data"]["matches"]
        self.assertEqual([Path(item["path"]).name for item in matches], ["notes.pdf"])
        self.assertEqual(matches[0]["size_bytes"], len(b"%PDF smoke test"))
        self.assertEqual(matches[0]["path"], str(pdf))

    def test_sha256_duplicate_scan_is_read_only(self):
        folder = self.root / "duplicate-scan"
        folder.mkdir()
        a = folder / "a.bin"
        b = folder / "b.bin"
        c = folder / "unique.bin"
        a.write_bytes(b"same content")
        b.write_bytes(b"same content")
        c.write_bytes(b"different")
        out = self.service.handle_text(f'Find duplicate files in "{folder}" using file hashes. Do not delete anything; show me a report.')
        self.assertTrue(out["ok"], out)
        data = out["data"]
        self.assertEqual(data["duplicate_group_count"], 1)
        self.assertEqual(set(data["duplicate_groups"][0]["files"]), {str(a), str(b)})
        self.assertEqual(data["duplicate_groups"][0]["sha256"], __import__("hashlib").sha256(b"same content").hexdigest())
        self.assertEqual(data["deletions_performed"], 0)
        self.assertTrue(a.exists() and b.exists() and c.exists())

    def test_duplicate_scan_for_downloads_uses_downloads_and_reports_actual_hash_bytes(self):
        downloads = self.root / "Downloads"
        downloads.mkdir()
        first = downloads / "one.dat"
        second = downloads / "two.dat"
        first.write_bytes(b"identical-bytes")
        second.write_bytes(b"identical-bytes")
        out = self.service.handle_text(CRITICAL_PROMPTS[8])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["action"], "files.find_duplicates")
        data = out["data"]
        self.assertEqual(data["path"], str(downloads))
        self.assertEqual(data["duplicate_group_count"], 1)
        self.assertEqual(data["files_hashed"], 2)
        self.assertEqual(data["bytes_hashed_read"], len(b"identical-bytes") * 2)
        self.assertEqual(data["total_file_bytes_considered"], len(b"identical-bytes") * 2)
        self.assertEqual(data["deletions_performed"], 0)
        self.assertTrue(first.exists() and second.exists())

    def test_zip_creation_excludes_cache_and_temp_dirs_and_verifies_integrity(self):
        source = self.root / "project"
        (source / "src").mkdir(parents=True)
        (source / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (source / ".pytest_cache").mkdir()
        (source / ".pytest_cache" / "state").write_text("skip", encoding="utf-8")
        (source / "tmp").mkdir()
        (source / "tmp" / "scratch.txt").write_text("skip", encoding="utf-8")
        destination = self.root / "project.zip"
        prompt = f'Create a ZIP archive from folder "{source}" to "{destination}", exclude caches and temporary files, and verify the archive.'
        out = self.service.handle_text(prompt)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["verified"])
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(), ["src/main.py"])

    def test_organization_preview_does_not_move_files_until_approval(self):
        downloads = self.root / "Downloads"
        downloads.mkdir()
        photo = downloads / "photo.png"
        report = downloads / "report.pdf"
        photo.write_bytes(b"image")
        report.write_bytes(b"pdf")
        out = self.service.handle_text(f'Organize folder "{downloads}" by file type. Preview all planned changes before moving anything.')
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["action"], "organization_preview")
        self.assertFalse(out["executed"])
        self.assertEqual(out["moved"], 0)
        self.assertTrue(photo.exists() and report.exists())
        self.assertFalse((downloads / "Images").exists())

        approved = self.service.handle_text("approve organize Downloads")
        self.assertEqual(approved["action"], "organization_result")
        self.assertEqual(approved["moved"], 2, approved)
        self.assertTrue((downloads / "Images" / "photo.png").exists())
        self.assertTrue((downloads / "Documents" / "report.pdf").exists())
        self.assertFalse(photo.exists() or report.exists())

    def test_organize_by_file_type_never_becomes_keyboard_typing(self):
        from eva.automation import desktop
        with mock.patch.object(desktop, "execute") as execute:
            result = desktop.route_and_execute(CRITICAL_PROMPTS[7])
        execute.assert_not_called()
        self.assertFalse(result.get("ok", False))
        self.assertNotEqual(result.get("action"), "type")

    def test_notepad_prompt_creates_a_unique_file_and_reports_editor_step(self):
        original_execute = self.service.execute_tool
        def execute(name, arguments=None, **kwargs):
            if name == "desktop.open_text_file":
                return ToolResult(True, name, {"path": arguments["path"], "launch_requested": True},
                                  "Mock editor launch requested.", verified=False)
            return original_execute(name, arguments, **kwargs)
        with mock.patch.object(self.service, "execute_tool", side_effect=execute):
            out = self.service.handle_text("Open Notepad and create a new text document.")
        self.assertTrue(out["ok"], out)
        created = Path(out["created_file"]["data"]["path"])
        self.assertTrue(created.is_file())
        self.assertEqual(created.read_text(encoding="utf-8"), "")
        self.assertEqual(out["open_editor"]["data"]["launch_requested"], True)

    def test_clipboard_is_not_saved_if_read_fails(self):
        failed_read = ToolResult(False, "desktop.clipboard.read", {}, "Clipboard backend unavailable.",
                                 "CLIPBOARD_UNAVAILABLE", verified=False)
        with mock.patch.object(self.service, "execute_tool", return_value=failed_read) as execute_tool:
            out = self.service.handle_text(CRITICAL_PROMPTS[5])
        self.assertFalse(out["ok"])
        self.assertEqual(out["action"], "desktop.clipboard.read")
        self.assertEqual(execute_tool.call_count, 1)
        self.assertEqual(execute_tool.call_args.args[0], "desktop.clipboard.read")
        self.assertFalse((self.root / "Documents" / "EVA-created-files" / "clipboard.txt").exists())

    def test_incomplete_browser_prompt_and_webpage_download_are_honest(self):
        missing = self.service.handle_text(CRITICAL_PROMPTS[10])
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["action"], "need_input")
        self.assertIn("full URL", missing["error"])

        download = self.service.handle_text(CRITICAL_PROMPTS[11])
        self.assertFalse(download["ok"])
        self.assertIn("direct downloadable file link", download["error"])
        self.assertIn("No download was started", download["error"])

    def test_missing_inputs_and_unimplemented_capabilities_are_not_reported_as_success(self):
        for prompt, expected in [
            (CRITICAL_PROMPTS[9], "source project-folder path"),
            (CRITICAL_PROMPTS[12], "complete approved URL list"),
            (CRITICAL_PROMPTS[14], "actual project directory path"),
        ]:
            with self.subTest(prompt=prompt):
                result = self.service.handle_text(prompt)
                self.assertFalse(result["ok"])
                self.assertIn(expected, result["error"])

        tests = self.service.handle_text(CRITICAL_PROMPTS[15])
        self.assertFalse(tests["ok"])
        self.assertIn("does not have a registered restricted test/build runner", tests["error"])
        workflow = self.service.handle_text(CRITICAL_PROMPTS[17])
        self.assertFalse(workflow["ok"])
        self.assertIn("no registered tool", workflow["error"])

    def test_multi_url_summary_report_uses_real_browser_tool_results_and_saves_sources(self):
        report = self.root / "summary-report.md"
        prompt = (
            'Visit these approved URLs "https://example.test/one" and "https://example.test/two", '
            f'extract headings and facts, save results with source URLs, and create a summary report to "{report}".'
        )
        original_execute = self.service.execute_tool
        calls = []

        def execute(name, arguments=None, **kwargs):
            arguments = arguments or {}
            calls.append((name, arguments))
            if name == "browser.navigate":
                url = arguments["url"]
                return ToolResult(True, name, {"url": url, "title": url.rsplit("/", 1)[-1].title(), "http_status": 200},
                                  "Navigation completed.", verified=False)
            if name == "browser.headings":
                return ToolResult(True, name, {"headings": ["Heading A", "Heading B"]}, "Headings extracted.", verified=True)
            if name == "browser.text":
                return ToolResult(True, name, {"text": "Fact excerpt one\nFact excerpt two"}, "Text extracted.", verified=True)
            return original_execute(name, arguments, **kwargs)

        with mock.patch.object(self.service, "execute_tool", side_effect=execute):
            out = self.service.handle_text(prompt)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["action"], "web_summary_report")
        self.assertEqual(out["source_count"], 2)
        self.assertTrue(report.is_file())
        content = report.read_text(encoding="utf-8")
        self.assertIn("https://example.test/one", content)
        self.assertIn("https://example.test/two", content)
        self.assertIn("Heading A", content)
        self.assertIn("Fact excerpt one", content)
        self.assertEqual(sum(1 for name, _ in calls if name == "browser.navigate"), 2)

    def test_direct_capability_gaps_are_persisted_and_visible_in_gaps_command(self):
        workflow = self.service.handle_text(
            "Create a reusable workflow to open apps, check project folder, and show pending tasks."
        )
        self.assertEqual(workflow["action"], "capability_gap")
        self.assertTrue(workflow["gap_recorded"], workflow)
        stored_names = {item["name"] for item in self.service.capability_gaps.list()}
        self.assertIn("tasks.pending_list", stored_names)
        self.assertIn("workflow.create_validate_save", stored_names)
        listed = self.service.handle_text("/gaps")
        self.assertEqual(listed["action"], "capability_gaps")
        self.assertEqual({item["name"] for item in listed["gaps"]}, stored_names)
        self.assertEqual(listed["path"], str(self.root / "capability_gaps.json"))

    def test_capability_map_is_not_preempted_by_missing_workflow_authoring(self):
        out = self.service.handle_text(CRITICAL_PROMPTS[19])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["action"], "capability_map")
        self.assertIn("workflow.create_validate_save (not currently registered; installed workflows can be previewed/run)",
                      out["missing_capabilities"])
        self.assertTrue(out["gap_recorded"])
        self.assertTrue(Path(out["capability_gap_file"]).is_file())
        self.assertTrue(any("No concrete task actions" in line for line in out["mapped_steps"]))
        self.assertTrue(out["tools"])

    def test_project_patch_and_test_request_is_reported_as_partial_not_complete(self):
        project = self.root / "partial-project"
        project.mkdir()
        (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
        out = self.service.handle_text(
            f'Analyze errors in project "{project}", propose a minimal patch, run tests, and show changed files.'
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["action"], "partial_capability_gap")
        self.assertTrue(out["inspection"]["ok"])
        self.assertIn("No patch was proposed or applied", out["message"])
        self.assertTrue(any(item.startswith("developer.run_checks") for item in out["missing_capabilities"]))

    def test_url_check_dispatches_to_real_tool_and_preserves_actual_result(self):
        response = ToolResult(True, "network.url_check", {
            "requested_url": "https://example.com", "http_status": 204, "reachable": True,
            "final_url": "https://example.com", "elapsed_ms": 11,
        }, "HTTP reachability check completed.", verified=True)
        with mock.patch.object(self.service, "execute_tool", return_value=response) as execute:
            out = self.service.handle_text(CRITICAL_PROMPTS[16])
        execute.assert_called_once_with("network.url_check", {"url": "https://example.com"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["http_status"], 204)

    def test_git_status_reads_real_repository_without_changing_it(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("Git CLI is not installed in this test environment")
        repo = self.root / "repo"
        repo.mkdir()
        init = subprocess.run([git, "init", "-b", "smoke" , str(repo)], capture_output=True, text=True)
        if init.returncode != 0:
            self.skipTest("Git init is not supported by installed Git")
        (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
        out = self.service.handle_text(f'Show Git status, branch, and diff summary for project path "{repo}".')
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["branch"], "smoke")
        self.assertIn("hello.txt", out["data"]["status"])
        self.assertTrue((repo / "hello.txt").exists())

    def test_readme_summary_fallback_uses_only_real_source_excerpts(self):
        readme = self.root / "README.md"
        readme.write_text(
            "# EVA\n\nEVA supports deterministic task routing for local automation requests. "
            "The tool registry validates arguments before execution.\n\n"
            "## Safety\n\nHigh-impact operations require explicit approval before execution.\n",
            encoding="utf-8",
        )
        with mock.patch("eva.automation.service.Path.cwd", return_value=self.root):
            out = self.service.handle_text("Summarize README in the EVA project", provider=None)
        self.assertEqual(out.get("action"), "project_readme_summary", out)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("method"), "extractive fallback from actual README text")
        summary = out.get("summary", "")
        self.assertIn("EVA supports deterministic task routing for local automation requests.", summary)
        self.assertIn("High-impact operations require explicit approval before execution.", summary)
        self.assertNotIn("The README lists build/test execution", summary)

    def test_project_health_is_a_read_only_python_syntax_check(self):
        project = self.root / "health"
        project.mkdir()
        source = project / "broken.py"
        source.write_text("def bad(:\n  pass\n", encoding="utf-8")
        out = self.service.handle_text(f'Create a project health report for "{project}".')
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["python_files_with_syntax_issues"], 1)
        self.assertFalse(out["data"]["source_executed"])
        self.assertFalse(out["data"]["tests_executed"])

    def test_windows_side_by_side_layout_uses_measured_two_pane_bounds(self):
        from eva.automation.tools import windows_ui
        from eva.automation.engine import ExecutionContext

        class FakeWindow:
            def __init__(self, title, handle, pid):
                self._title, self.handle, self._pid = title, handle, pid
            def window_text(self):
                return self._title
            def process_id(self):
                return self._pid
            def is_visible(self):
                return True

        class FakeDesktop:
            def windows(self, **kwargs):
                return [FakeWindow("Research - Google Chrome", 101, 1001),
                        FakeWindow("Untitled", 202, 1002)]

        expected_rects = {
            (101, 0, 0, 960, 1080): {"x": 0, "y": 0, "width": 960, "height": 1080},
            (202, 960, 0, 960, 1080): {"x": 960, "y": 0, "width": 960, "height": 1080},
        }
        with mock.patch.object(windows_ui.platform, "system", return_value="Windows"), \
             mock.patch.object(windows_ui, "_desktop", return_value=FakeDesktop()), \
             mock.patch.object(windows_ui, "_window_process_name", side_effect=lambda pid: "chrome.exe" if pid == 1001 else "notepad.exe"), \
             mock.patch.object(windows_ui, "_screen_size", return_value=(1920, 1080)), \
             mock.patch.object(windows_ui, "_move_window_handle", side_effect=lambda hwnd, x, y, w, h: expected_rects[(hwnd, x, y, w, h)]):
            result = windows_ui._arrange_windows_side_by_side(ExecutionContext(), {})
        self.assertTrue(result.ok, result)
        self.assertTrue(result.verified)
        self.assertEqual(result.data["browser_bounds"], {"x": 0, "y": 0, "width": 960, "height": 1080})
        self.assertEqual(result.data["notepad_bounds"], {"x": 960, "y": 0, "width": 960, "height": 1080})

    def test_process_metrics_tool_failure_is_clear_on_non_windows_host(self):
        from eva.automation import desktop
        # Force the non-Windows adapter path even when this suite runs on Windows.
        with mock.patch.object(desktop.platform, "system", return_value="Linux"):
            out = self.service.handle_text(CRITICAL_PROMPTS[3])
        self.assertFalse(out["ok"])
        self.assertIn("not implemented for Linux", out.get("message", ""))


if __name__ == "__main__":
    unittest.main()
