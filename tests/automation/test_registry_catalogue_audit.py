from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eva.automation.service import AutomationService
from eva.automation.task_planner import _tool_rows


class RegistryCatalogueAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-registry-audit-")
        self.root = Path(self.temp.name)
        self.service = AutomationService(
            allowed_roots=[self.root], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "state" / "gaps.json",
            browser_headless=True,
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_every_registered_tool_has_a_valid_discoverable_contract(self):
        specs = self.service.registry.list()
        catalog = {item["name"]: item for item in self.service._tool_metadata()}
        self.assertEqual(set(catalog), set(self.service.registry.names()))
        self.assertGreaterEqual(len(specs), 80)
        for spec in specs:
            with self.subTest(tool=spec.name):
                binding = self.service.registry.get(spec.name)
                self.assertIsNotNone(binding)
                self.assertTrue(callable(binding.handler))
                self.assertTrue(spec.description.strip())
                self.assertEqual(spec.input_schema.get("type"), "object")
                self.assertIs(spec.input_schema.get("additionalProperties"), False)
                self.assertTrue(set(spec.input_schema.get("required", [])) <= set(spec.input_schema.get("properties", {})))
                for key, rule in spec.input_schema.get("properties", {}).items():
                    if rule.get("type") == "array":
                        self.assertIsInstance(rule.get("items"), dict, f"{spec.name}.{key} must declare array item schema")
                meta = catalog[spec.name]
                self.assertTrue(meta.get("purpose"), f"{spec.name} purpose")
                self.assertTrue(meta.get("when_to_use"), f"{spec.name} when_to_use")
                self.assertTrue(meta.get("output_description"), f"{spec.name} output contract")
                self.assertTrue(meta.get("side_effects"), f"{spec.name} side-effect contract")
                if spec.risk.value in self.service.policy.approval_required_risks:
                    self.assertTrue(spec.requires_approval or spec.risk.value in self.service.policy.approval_required_risks)

    def test_multi_domain_request_exposes_the_first_steps_for_each_requested_deliverable(self):
        prompt = "Create an Excel expense tracker and a Python script that reads it"
        rows = _tool_rows(self.service, prompt)
        names = {row["name"] for row in rows}
        self.assertTrue({"files.create_xlsx", "content.generate_text", "files.create_unique_text_file"} <= names)
        self.assertLessEqual(len(rows), 16)
        self.assertTrue(all(row.get("params") is not None for row in rows))

    def test_live_desktop_typing_is_approval_gated_at_the_executor_boundary(self):
        spec = self.service.registry.require("desktop.type").spec
        self.assertTrue(spec.requires_approval)
        result = self.service.execute_tool("desktop.type", {"value": "must not be typed", "target": "notepad"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "APPROVAL_REQUIRED")

    def test_notepad_scoped_typing_does_not_require_generic_focus_approval(self):
        from unittest.mock import patch
        with patch("eva.automation.desktop.execute", return_value={"ok": True, "verified": True, "message": "Mock Notepad typing"}) as typed:
            result = self.service.execute_tool("desktop.type_notepad", {"value": "safe test", "target": "notepad"})
        self.assertTrue(result.ok, result.as_dict())
        typed.assert_called_once_with("type", "safe test", target="notepad")

    def test_array_item_constraints_survive_compact_schema_generation(self):
        rows = _tool_rows(self.service, "Find the newest PDF and exclude the EVA output directories")
        latest = next(row for row in rows if row["name"] == "files.find_latest")
        self.assertEqual(latest["params"]["exclude_directories"]["type"], "array")
        self.assertEqual(latest["params"]["exclude_directories"]["items"]["type"], "string")
        self.assertEqual(latest["params"]["exclude_directories"]["items"]["minLength"], 1)


if __name__ == "__main__":
    unittest.main()
