import tempfile
import unittest
from pathlib import Path

from eva.coding.diagnostics import classify_diagnostic
from eva.coding.engine import CodingEngine
from eva.coding.store import CodingStore
from eva.coding.validators import validate_file
from eva.coding.workspace import inspect_workspace


class CodingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CodingStore(Path(self.tmp.name) / "db.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_knowledge_is_unverified_by_default_and_searchable(self):
        item_id = self.store.add_knowledge("Python AST parsing", "Use ast.parse to parse Python source.", language="python")
        self.assertGreater(item_id, 0)
        found = self.store.search_knowledge("parse Python source", language="python")
        self.assertEqual(found[0]["id"], item_id)
        self.assertEqual(found[0]["status"], "unverified")

    def test_duplicate_source_and_content_updates_existing_record(self):
        first = self.store.add_knowledge("Title", "Same body", source="src")
        second = self.store.add_knowledge("Renamed", "Same body", source="src")
        self.assertEqual(first, second)
        self.assertEqual(self.store.stats()["knowledge_items"], 1)

    def test_rejected_and_outdated_knowledge_are_not_returned(self):
        self.store.add_knowledge("Old technique", "legacy old method", status="outdated")
        self.store.add_knowledge("Bad technique", "unsafe old method", status="rejected")
        self.assertEqual(self.store.search_knowledge("legacy old method"), [])

    def test_promotion_requires_all_validation_gates(self):
        with self.assertRaises(ValueError):
            self.store.promote_rule("rule", "pattern", "fix", language="python",
                                    original_test_passed=True, regression_tests_passed=False,
                                    reviewed=True, evidence={})
        rule_id = self.store.promote_rule("rule", "pattern", "fix", language="python",
                                          original_test_passed=True, regression_tests_passed=True,
                                          reviewed=True, evidence={"test": "test_case_1"})
        self.assertGreater(rule_id, 0)
        self.assertEqual(self.store.stats()["verified_rules"], 1)

    def test_prior_experience_is_retrievable_for_future_tasks(self):
        self.store.record_experience("Python NameError undefined variable total", language="python",
                                      outcome="failure", category="undefined_name", evidence="NameError total")
        hits = self.store.search_experiences("Python undefined variable NameError", language="python")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["outcome"], "failure")

    def test_experience_and_repair_attempt_are_linked(self):
        exp_id = self.store.record_experience("bad function", language="python", outcome="failure",
                                              category="syntax", evidence="SyntaxError")
        attempt_id = self.store.record_repair_attempt(exp_id, "fix missing colon", result="failed")
        self.assertGreater(attempt_id, 0)
        with self.assertRaises(ValueError):
            self.store.record_repair_attempt(9999, "fix", result="passed")
        self.assertEqual(self.store.stats()["failures"], 1)


class ValidatorTests(unittest.TestCase):
    def test_python_valid_syntax_passes_without_execution(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "safe.py"
            p.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
            result = validate_file(p)
            self.assertEqual(result["status"], "passed")
            self.assertFalse(result["executed_source"])

    def test_empty_java_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "Main.java"
            p.write_text("\n  \t\n", encoding="utf-8")
            result = validate_file(p)
            self.assertEqual(result["status"], "failed")
            self.assertIn("empty", result["message"])

    def test_java_invalid_formatter_conversion_is_detected(self):
        from eva.coding.validators import _java_formatter_error
        source = 'System.out.printf("factorial is %ld%n", result);'
        error = _java_formatter_error(source)
        self.assertIsNotNone(error)
        self.assertIn("%l", error)

    def test_java_valid_integer_formatter_is_accepted(self):
        from eva.coding.validators import _java_formatter_error
        self.assertIsNone(_java_formatter_error('System.out.printf("factorial is %d%n", result);'))

    def test_java_non_format_string_percent_ld_is_not_a_formatter_error(self):
        from eva.coding.validators import _java_formatter_error
        source = 'String documentation = "The invalid C-style token is %ld";'
        self.assertIsNone(_java_formatter_error(source))
        self.assertIsNone(_java_formatter_error('System.out.printf("literal %%ld%n");'))

    def test_java_interactive_request_rejects_hardcoded_number(self):
        from eva.coding.validators import _java_input_error
        source = 'public class Main { public static void main(String[] args) { int n = 12; } }'
        error = _java_input_error(source, "Write a Java program to calculate factorial of a number entered by the user")
        self.assertIsNotNone(error)
        self.assertIn("hardcode", error)

    def test_java_interactive_request_accepts_scanner_read(self):
        from eva.coding.validators import _java_input_error
        source = 'Scanner sc = new Scanner(System.in); int n = sc.nextInt();'
        self.assertIsNone(_java_input_error(source, "Write a Java program to calculate factorial of a number entered by the user"))

    def test_python_syntax_error_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.py"
            p.write_text("def broken(:\n    pass\n", encoding="utf-8")
            result = validate_file(p)
            self.assertEqual(result["status"], "failed")
            self.assertIn("SyntaxError", result["message"])

    def test_css_unclosed_block_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.css"
            p.write_text(".card { color: red;", encoding="utf-8")
            result = validate_file(p)
            self.assertEqual(result["status"], "failed")
            self.assertIn("unclosed", result["message"])

    def test_css_missing_declaration_colon_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.css"
            p.write_text(".card { color red; }", encoding="utf-8")
            result = validate_file(p)
            self.assertEqual(result["status"], "failed")
            self.assertIn("missing ':'", result["message"])

    def test_json_syntax_error_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text('{"name": }', encoding="utf-8")
            result = validate_file(p)
            self.assertEqual(result["status"], "failed")
            self.assertIn("JSON syntax error", result["message"])

    def test_html_and_css_are_included_in_project_validation(self):
        from eva.coding.project_agent import _validate_project
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            html = "<!doctype html><html><body><h1>Hi</h1></body></html>"
            css = ".card { color red; }"
            (root / "index.html").write_text(html, encoding="utf-8")
            (root / "styles.css").write_text(css, encoding="utf-8")
            checks = _validate_project(root, {"index.html": html, "styles.css": css})
            self.assertTrue(any(c["file"] == "styles.css" and c["status"] == "failed" for c in checks))

    def test_large_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "large.py"
            p.write_text("#" + ("x" * 2_000_001), encoding="utf-8")
            self.assertEqual(validate_file(p)["status"], "skipped")


class WorkspaceTests(unittest.TestCase):
    def test_inspection_ignores_secrets_and_generated_folders(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "main.py").write_text("print('hi')", encoding="utf-8")
            (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dep.js").write_text("bad", encoding="utf-8")
            (root / "test_main.py").write_text("def test_ok(): pass", encoding="utf-8")
            report = inspect_workspace(root)
            paths = {f["path"] for f in report["files"]}
            self.assertIn("main.py", paths)
            self.assertIn("test_main.py", paths)
            self.assertNotIn(".env", paths)
            self.assertFalse(any("node_modules" in x for x in paths))
            self.assertEqual(report["languages"]["Python"], 2)

    def test_rejects_file_as_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            p.write_text("x")
            with self.assertRaises(ValueError):
                inspect_workspace(p)


class DiagnosticsAndEngineTests(unittest.TestCase):
    def test_diagnostic_classification_never_claims_auto_fix(self):
        result = classify_diagnostic("NameError: name 'x' is not defined")
        self.assertEqual(result["category"], "undefined_name")
        self.assertFalse(result["automatically_fixable"])

    def test_unknown_diagnostic_requires_review(self):
        result = classify_diagnostic("some entirely new failure")
        self.assertEqual(result["category"], "unknown")
        self.assertTrue(result["needs_review"])

    def test_plan_is_deterministic_and_does_not_edit_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "hello.py"
            src.write_text("print('hello')", encoding="utf-8")
            engine = CodingEngine(root / "data")
            plan = engine.plan("debug the Python script", root)
            self.assertEqual(plan["action"], "diagnose_then_validate")
            self.assertEqual(src.read_text(encoding="utf-8"), "print('hello')")
            self.assertEqual(plan["mode"], "deterministic_plan")

    def test_empty_plan_request_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            engine = CodingEngine(Path(td) / "data")
            with self.assertRaises(ValueError):
                engine.plan("  ", td)

    def test_ingestion_indexes_source_as_unverified_without_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            root.mkdir()
            (root / "example.py").write_text("raise RuntimeError('never run')\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=do-not-index", encoding="utf-8")
            engine = CodingEngine(Path(td) / "data")
            result = engine.ingest_workspace(root)
            self.assertEqual(result["indexed"], 1)
            self.assertFalse(result["executed_files"])
            hits = engine.store.search_knowledge("RuntimeError", language="python")
            self.assertEqual(hits[0]["status"], "unverified")
            self.assertNotIn("do-not-index", hits[0]["body"])


if __name__ == "__main__":
    unittest.main()
