import json
import tempfile
import unittest
from pathlib import Path

from eva.coding.construction import scaffold, available_templates
from eva.coding.patches import propose_text_patch, save_patch_proposal, apply_patch_proposal
from eva.coding.repair import suggest_repairs
from eva.coding.validators import validate_file

class ConstructionTests(unittest.TestCase):
    def test_templates_list(self):
        self.assertEqual({x['id'] for x in available_templates()}, {'python-cli', 'java-console', 'web-static'})

    def test_python_scaffold_compiles_and_has_test(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / 'newapp'
            result = scaffold('python-cli', target, 'New App')
            self.assertEqual(result['status'], 'created')
            self.assertTrue((target / 'new_app' / '__main__.py').exists())
            self.assertTrue((target / 'tests' / 'test_smoke.py').exists())
            for p in target.rglob('*.py'):
                self.assertEqual(validate_file(p)['status'], 'passed', p)

    def test_scaffold_refuses_nonempty_target(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / 'target'; target.mkdir(); (target / 'keep.txt').write_text('keep')
            with self.assertRaises(FileExistsError): scaffold('web-static', target, 'App')
            self.assertEqual((target / 'keep.txt').read_text(), 'keep')

    def test_scaffold_web_braces_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / 'site'
            scaffold('web-static', target, 'Site')
            self.assertIn('box-sizing', (target / 'styles.css').read_text())
            self.assertIn('addEventListener', (target / 'app.js').read_text())

class RepairTests(unittest.TestCase):
    def test_known_diagnostic_gives_steps_but_no_auto_edit(self):
        result = suggest_repairs("NameError: name 'total' is not defined")
        self.assertEqual(result['category'], 'undefined_name')
        self.assertFalse(result['automatic_edit'])
        self.assertTrue(result['needs_review'])

    def test_unknown_diagnostic_does_not_guess(self):
        result = suggest_repairs('something weird happened')
        self.assertEqual(result['confidence'], 'unknown')
        self.assertFalse(result['automatic_edit'])

class PatchTests(unittest.TestCase):
    def test_patch_requires_approval_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); target = root / 'file.py'; target.write_text('x = 1\n')
            proposal = propose_text_patch(target, 'x = 2\n')
            proposal_path = save_patch_proposal(proposal, root / 'patch.json')
            with self.assertRaises(PermissionError): apply_patch_proposal(proposal_path)
            result = apply_patch_proposal(proposal_path, approved=True)
            self.assertEqual(result['status'], 'applied')
            self.assertEqual(target.read_text(), 'x = 2\n')
            self.assertEqual(Path(result['backup']).read_text(), 'x = 1\n')

    def test_patch_rejects_stale_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); target = root / 'file.py'; target.write_text('x = 1\n')
            proposal_path = save_patch_proposal(propose_text_patch(target, 'x = 2\n'), root / 'patch.json')
            target.write_text('x = 3\n')
            with self.assertRaises(RuntimeError): apply_patch_proposal(proposal_path, approved=True)

if __name__ == '__main__': unittest.main()
