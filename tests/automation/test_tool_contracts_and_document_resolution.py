from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from eva.automation.engine import ExecutionContext
from eva.automation.service import AutomationService


class ToolContractsAndDocumentResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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

    def test_every_registered_tool_exposes_usage_contract(self):
        metadata = self.service._tool_metadata()
        self.assertTrue(metadata)
        for tool in metadata:
            self.assertTrue(tool.get("purpose"), tool["name"])
            self.assertTrue(tool.get("when_to_use"), tool["name"])
            self.assertTrue(tool.get("output_description"), tool["name"])
            self.assertTrue(tool.get("side_effects"), tool["name"])
            self.assertIn("input_schema", tool)
        by_name = {tool["name"]: tool for tool in metadata}
        self.assertIn("desktop.type", by_name)
        self.assertIn("content.generate_text", by_name)
        self.assertIn("files.resolve_by_name", by_name)
        self.assertIn("files.read_document", by_name)

    def test_resolver_prefers_exact_nonstandard_extension(self):
        exact = self.root / "main.docs"
        exact.write_text("exact spelling wins", encoding="utf-8")
        docx = self.root / "main.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>docx fallback</w:t></w:r></w:p></w:body></w:document>")
        result = self.service.execute_tool("files.resolve_by_name", {
            "path": str(self.root), "filename": "main.docs", "recursive": True,
        })
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(result.data["path"], str(exact))
        self.assertEqual(result.data["matched_by"], "exact_filename")

    def test_resolver_tries_docx_when_speech_transcribed_docs(self):
        docx = self.root / "main.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>Fallback text</w:t></w:r></w:p></w:body></w:document>")
        result = self.service.execute_tool("files.resolve_by_name", {
            "path": str(self.root), "filename": "main.docs", "recursive": True,
        })
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(result.data["path"], str(docx))
        read = self.service.execute_tool("files.read_document", {"path": result.data["path"]})
        self.assertTrue(read.ok, read.as_dict())
        self.assertEqual(read.data["text"], "Fallback text")

    def test_docx_reader_extracts_paragraphs_and_tabs(self):
        path = self.root / "sample.docx"
        xml = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>First paragraph</w:t></w:r></w:p><w:p><w:r><w:t>Second</w:t><w:tab/><w:t>part</w:t></w:r></w:p></w:body></w:document>"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)
        result = self.service.execute_tool("files.read_document", {"path": str(path)})
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(result.data["text"], "First paragraph\nSecond\tpart")
        self.assertEqual(result.data["format"], "docx")

    def test_planner_catalogue_exposes_actual_tools_and_usage_contracts(self):
        from eva.automation.task_planner import _tool_rows
        rows = _tool_rows(self.service, "Open Notepad and write an application to the principal")
        names = {row["name"] for row in rows}
        self.assertIn("content.generate_text", names)
        self.assertIn("desktop.open_target", names)
        self.assertIn("desktop.type", names)
        self.assertIn("system.local_time", names)
        self.assertTrue(all(row.get("when_to_use") and row.get("purpose") for row in rows))
        file_rows = _tool_rows(self.service, "Find main.docs, read it and show its text")
        file_names = {row["name"] for row in file_rows}
        self.assertIn("files.resolve_by_name", file_names)
        self.assertIn("files.read_document", file_names)

    def test_content_tool_does_not_inject_unrequested_date_or_force_a_reason(self):
        class Provider:
            def __init__(self):
                self.system_prompt = ""
                self.user_prompt = ""

            def generate_text(self, system_prompt, user_prompt, *, timeout=45, num_predict=1200):
                self.system_prompt = system_prompt
                self.user_prompt = user_prompt
                return "Draft text supplied by the model."

        provider = Provider()
        workflow = {"name": "write-test", "timeout_seconds": 20, "inputs": {}, "steps": [{
            "id": "draft", "tool": "content.generate_text", "args": {
                "prompt": "Write a short note with the user-provided details only.",
                "style": "clear and natural",
            }, "depends_on": [],
        }]}
        with patch("eva.tools.system_tools.get_local_datetime", return_value="2026-10-21 17:00:00"):
            result = self.service._execute_planned_workflow(workflow, provider)
        self.assertTrue(result.ok, result.as_dict())
        self.assertNotIn("2026-10-21 17:00:00", provider.user_prompt)
        self.assertNotIn("reasonable, conventional choice", provider.system_prompt)
        self.assertIn("do not invent the purpose or reason", provider.system_prompt.lower())
        self.assertNotIn("current local date/time:", provider.user_prompt.lower())


if __name__ == "__main__":
    unittest.main()
