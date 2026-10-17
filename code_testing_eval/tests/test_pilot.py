import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pilot.build import apply, digest, extract
from pilot.environment import check_workspace, stable_rows
from pilot.submission import read_submission

BUNDLE = ROOT / 'private_grading/pilots/click-pilot-01'


class PilotTests(unittest.TestCase):
    def test_changed_test_outcomes_are_not_treated_as_stable_evidence(self):
        def run(status, kind):
            return {'outcome': {'phase': 'tests', 'tests': [{'id': 'test.case', 'status': status,
                    'details': [{'type': kind}]}]}}
        self.assertIsNone(stable_rows([run('failed', 'AssertionError'), run('passed', 'AssertionError')]))
        self.assertIsNone(stable_rows([run('error', 'TypeError'), run('error', 'NameError')]))
        self.assertIsNotNone(stable_rows([run('failed', 'AssertionError')] * 3))

    @unittest.skipUnless((BUNDLE / 'bundle_manifest.json').is_file(), 'Requires local grading data excluded from the code-only checkout')
    def test_private_bundle_inputs_are_sealed(self):
        manifest = json.loads((BUNDLE / 'bundle_manifest.json').read_text())
        self.assertIn('plan.json', manifest)
        self.assertIn('runtime.lock', manifest)
        self.assertIn('source.tar.gz', manifest)
        for relative, expected in manifest.items():
            self.assertEqual(digest((BUNDLE / relative).read_bytes()), expected, relative)
        plan = json.loads((BUNDLE / 'plan.json').read_text())
        self.assertEqual(len(plan['bugs']), 10)
        for bug in plan['bugs']:
            record = json.loads((ROOT / 'private_grading/bugs' / bug['catalog_record'] / 'record.json').read_text())
            self.assertEqual(record['status'], 'validated')
            self.assertEqual(record['before_revision'], plan['baseline_revision'])

    @unittest.skipUnless((BUNDLE / 'bundle_manifest.json').is_file(), 'Requires local grading data excluded from the code-only checkout')
    def test_each_restored_variant_equals_reversing_only_that_repair(self):
        plan = json.loads((BUNDLE / 'plan.json').read_text())
        ids = {b['id'] for b in plan['bugs']}
        touched = {e['path'] for b in plan['bugs'] for e in b['edits']}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixed = root / 'fixed'
            extract(BUNDLE, fixed)
            apply(fixed, plan, ids)
            for bug in plan['bugs']:
                variant = root / bug['id']
                extract(BUNDLE, variant)
                apply(variant, plan, ids - {bug['id']})
                expected = {file: (fixed / file).read_text() for file in touched}
                for edit in reversed(bug['edits']):
                    text = expected[edit['path']]
                    self.assertEqual(text.count(edit['after']), 1)
                    expected[edit['path']] = text.replace(edit['after'], edit['before'], 1)
                for file in touched:
                    self.assertEqual((variant / file).read_text(), expected[file], (bug['id'], file))

    @unittest.skipUnless((BUNDLE / 'bundle_manifest.json').is_file(), 'Requires local grading data excluded from the code-only checkout')
    def test_gold_uses_the_public_submission_contract(self):
        files = read_submission(BUNDLE / 'gold')
        self.assertEqual(len(files), 11)

    def test_contract_rejects_alias_introspection_and_non_test_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            file = folder / 'test_bad.py'
            for source in ['from sys import setprofile as change\n',
                           'from click import __file__ as source_location\n',
                           'import unittest\n@unittest.expectedFailure\nclass Bad(unittest.TestCase):pass\n',
                           'import socket\n', 'print("side effect")\n']:
                file.write_text(source)
                with self.assertRaises(ValueError):
                    read_submission(folder)
            file.unlink()
            (folder / 'click.py').write_text('changed library')
            with self.assertRaises(ValueError):
                read_submission(folder)

    def test_standard_unittest_footer_is_accepted_without_accepting_arbitrary_module_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            file = folder / 'test_example.py'
            file.write_text("import unittest\nclass Tests(unittest.TestCase):\n def test_ok(self):self.assertTrue(True)\nif __name__ == '__main__':\n unittest.main()\n")
            self.assertIn(file.name, read_submission(folder))
            file.write_text("if True:\n print('module side effect')\n")
            with self.assertRaises(ValueError):
                read_submission(folder)

    def test_writable_directory_symlinks_are_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'scratch').symlink_to(BUNDLE.resolve(), target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                check_workspace({}, workspace)


if __name__ == '__main__':
    unittest.main()
