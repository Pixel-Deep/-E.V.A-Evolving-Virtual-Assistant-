from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eva.automation.service import AutomationService


class CodingToolsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-coding-tools-")
        self.root = Path(self.temp.name).resolve()
        self.service = AutomationService(
            allowed_roots=[self.root], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "state" / "gaps.json",
            browser_headless=True,
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_static_validation_passes_without_executing_source(self):
        source = self.root / "sample.py"
        marker = self.root / "must-not-be-created.txt"
        source.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('source executed')\n"
            "def answer():\n    return 42\n",
            encoding="utf-8",
        )
        result = self.service.execute_tool("coding.validate_file", {"path": str(source)})
        self.assertTrue(result.ok, result.as_dict())
        self.assertTrue(result.verified)
        self.assertFalse(result.data["source_executed"])
        self.assertEqual(result.data["status"], "passed")
        self.assertFalse(marker.exists(), "Static validation must never execute source code")

    def test_static_validation_reports_syntax_error_without_running_source(self):
        source = self.root / "broken.py"
        source.write_text("def answer(:\n    return 42\n", encoding="utf-8")
        result = self.service.execute_tool("coding.validate_file", {"path": str(source)})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CODE_VALIDATION_FAILED")
        self.assertFalse(result.data["source_executed"])
        self.assertEqual(source.read_text(encoding="utf-8"), "def answer(:\n    return 42\n")

    def test_existing_source_validation_routes_to_static_tool_not_project_generation(self):
        from eva.core.task_router import TaskRouter
        from eva.automation.task_planner import TaskPlanner
        source = self.root / "sample.py"
        source.write_text("print('safe static check')\n", encoding="utf-8")
        request = "Validate the syntax of my Python script sample.py"
        decision = TaskRouter().route(request)
        self.assertEqual(decision.handler, "desktop_automation")
        plan = TaskPlanner(self.service, None, self.service.capability_gaps)._deterministic_plan(request)
        self.assertEqual([step["tool"] for step in plan["steps"]], ["files.resolve_by_name", "coding.validate_file"])
        result = TaskPlanner(self.service, None, self.service.capability_gaps).run(request)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["execution"]["steps"][-1]["tool"], "coding.validate_file")
        self.assertFalse(result["execution"]["steps"][-1]["result"]["data"]["source_executed"])

    def test_new_code_generation_still_routes_to_the_coding_engine(self):
        from eva.core.task_router import TaskRouter
        decision = TaskRouter().route("Create a Python script and validate it")
        self.assertEqual(decision.handler, "coding_engine")

    def test_coding_tools_refuse_paths_outside_approved_roots(self):
        outside = self.root.parent / (self.root.name + "-outside.py")
        outside.write_text("print('not run')\n", encoding="utf-8")
        try:
            result = self.service.execute_tool("coding.validate_file", {"path": str(outside)})
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "PATH_OUTSIDE_APPROVED_ROOT")
        finally:
            outside.unlink(missing_ok=True)

    def test_deterministic_scaffold_creates_and_verifies_files_without_overwrite(self):
        target = self.root / "starter"
        result = self.service.execute_tool("coding.scaffold_project", {
            "template": "python-cli", "target": str(target), "name": "Demo Project"
        })
        self.assertTrue(result.ok, result.as_dict())
        self.assertTrue(result.verified)
        self.assertTrue((target / "pyproject.toml").is_file())
        self.assertTrue((target / ".eva-project.json").is_file())
        again = self.service.execute_tool("coding.scaffold_project", {
            "template": "python-cli", "target": str(target), "name": "Demo Project"
        })
        self.assertFalse(again.ok)
        self.assertEqual(again.error_code, "ALREADY_EXISTS")


if __name__ == "__main__":
    unittest.main()
