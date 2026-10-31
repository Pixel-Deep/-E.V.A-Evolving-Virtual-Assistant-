import json
import tempfile
import unittest
from pathlib import Path

from eva.coding.blueprint import build_blueprint, load_blueprint, validate_blueprint
from eva.coding.doctor import run_doctor


class BlueprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spec = {"schema_version": 1, "name": "Sample Project", "description": "test",
                     "files": [{"path": "src/main.py", "content": "def main():\n    return 7\n"},
                               {"path": "tests/test_main.py", "content": "assert True\n"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_validate_and_build_multi_file_project(self):
        checked = validate_blueprint(self.spec)
        self.assertEqual(len(checked["files"]), 2)
        target = self.root / "project"
        result = build_blueprint(self.spec, target)
        self.assertEqual(result["status"], "created")
        self.assertEqual((target / "src" / "main.py").read_text(), "def main():\n    return 7\n")
        manifest = json.loads((target / ".eva-blueprint-manifest.json").read_text())
        self.assertEqual(manifest["verification"], "not_run")
        self.assertFalse(manifest["executed_code"])

    def test_dry_run_does_not_create_target(self):
        target = self.root / "dry"
        result = build_blueprint(self.spec, target, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(target.exists())

    def test_refuses_existing_nonempty_target(self):
        target = self.root / "existing"
        target.mkdir()
        (target / "important.txt").write_text("keep")
        with self.assertRaises(FileExistsError):
            build_blueprint(self.spec, target)
        self.assertEqual((target / "important.txt").read_text(), "keep")

    def test_rejects_parent_traversal(self):
        bad = {**self.spec, "files": [{"path": "../escape.py", "content": "bad"}]}
        with self.assertRaises(ValueError):
            validate_blueprint(bad)

    def test_rejects_absolute_and_drive_paths(self):
        for path in ("/tmp/out.py", "C:/Windows/test.py", "\\\\server\\share\\file.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_blueprint({**self.spec, "files": [{"path": path, "content": "x"}]})

    def test_rejects_secret_file_names(self):
        for path in (".env", "config/.env.production", "credentials.json", ".git/config"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_blueprint({**self.spec, "files": [{"path": path, "content": "x"}]})

    def test_rejects_case_colliding_paths(self):
        bad = {**self.spec, "files": [{"path": "Readme.md", "content": "a"},
                                      {"path": "README.md", "content": "b"}]}
        with self.assertRaises(ValueError):
            validate_blueprint(bad)

    def test_rejects_unknown_schema_and_bad_file_shape(self):
        with self.assertRaises(ValueError):
            validate_blueprint({**self.spec, "schema_version": 2})
        with self.assertRaises(ValueError):
            validate_blueprint({**self.spec, "files": [{"path": "x.py", "content": "ok", "execute": True}]})

    def test_blueprint_load_roundtrip(self):
        path = self.root / "blueprint.json"
        path.write_text(json.dumps(self.spec), encoding="utf-8")
        self.assertEqual(load_blueprint(path)["name"], "Sample Project")

    def test_doctor_is_read_only_and_returns_summary(self):
        result = run_doctor()
        self.assertEqual(result["mode"], "read_only")
        self.assertIn("errors", result["summary"])
        self.assertTrue(any(x["name"] == "blueprint_engine" for x in result["checks"]))

    def test_refuses_symlink_target_pointing_inside(self):
        real_empty_dir = self.root / "real_inside"
        real_empty_dir.mkdir()
        symlink_target = self.root / "symlink_inside"
        try:
            symlink_target.symlink_to(real_empty_dir, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation not permitted in this environment")
        with self.assertRaises(FileExistsError):
            build_blueprint(self.spec, symlink_target)
        self.assertEqual(len(list(real_empty_dir.iterdir())), 0, "No files should be written into the symlink target")

    def test_refuses_symlink_target_pointing_outside(self):
        with tempfile.TemporaryDirectory() as outside_td:
            outside_dir = Path(outside_td) / "real_outside"
            outside_dir.mkdir()
            symlink_target = self.root / "symlink_outside"
            try:
                symlink_target.symlink_to(outside_dir, target_is_directory=True)
            except OSError:
                self.skipTest("Symlink creation not permitted in this environment")
            with self.assertRaises(FileExistsError):
                build_blueprint(self.spec, symlink_target)
            self.assertEqual(len(list(outside_dir.iterdir())), 0, "No files should be written into external symlink target")


if __name__ == "__main__":
    unittest.main()
