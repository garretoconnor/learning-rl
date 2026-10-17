import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from batch_runner import prepare, run_batch
from benchmark_batch import comparable


class BatchTests(unittest.TestCase):
    def test_log_comparison_preserves_failure_items(self):
        def evidence(text):
            return {'before': [{'stderr': text}], 'fixed': [], 'verified_fail_to_pass': True,
                    'repetitions': 3, 'runtime': {}}
        first = evidence("AssertionError: Items in the first set but not the second:\n'b'\n'a'\n")
        reordered = evidence("AssertionError: Items in the first set but not the second:\n'a'\n'b'\n")
        changed = evidence("AssertionError: Items in the first set but not the second:\n'a'\n'c'\n")
        self.assertEqual(comparable(first, self.root), comparable(reordered, self.root))
        self.assertNotEqual(comparable(first, self.root), comparable(changed, self.root))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Catalog Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'click').mkdir()
        (self.repo / 'click/__init__.py').write_text('value = 0\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Before')
        self.before = self.git('rev-parse', 'HEAD')
        (self.repo / 'click/__init__.py').write_text('value = 1\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Fix value')
        self.fixed = self.git('rev-parse', 'HEAD')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def record(self, name):
        folder = self.root / name
        folder.mkdir()
        (folder / 'reproducer.py').write_text('''import pathlib, unittest
import click
class TestValue(unittest.TestCase):
    def test_value(self):
        # Detect shared mutable working directories across records.
        path = pathlib.Path('test-was-here')
        if path.exists():
            self.assertEqual(path.read_text(), str(pathlib.Path(__file__).parent))
        path.write_text(str(pathlib.Path(__file__).parent))
        self.assertEqual(click.value, 1, 'EXPECTED_VALUE_REPAIR')
unittest.main()
''')
        (folder / 'record.json').write_text(json.dumps({
            'bug_id': name, 'status': 'candidate', 'behavioral_claim': 'Value should be one',
            'before_revision': self.before, 'fixed_revision': self.fixed,
            'review': {'classification': 'bugfix'},
            'reproducer': {'path': 'reproducer.py', 'failure_marker': 'EXPECTED_VALUE_REPAIR', 'parallel_safe': True}}))
        return folder

    def test_shared_inputs_keep_fresh_runs_and_isolated_working_directories(self):
        folders = [self.record('one'), self.record('two')]
        report = run_batch(folders, self.repo, Path(sys.executable), jobs=2)
        self.assertEqual([r['status'] for r in report['records']], ['validated', 'validated'])
        self.assertEqual(report['test_processes'], 12)
        self.assertEqual(report['parallel_safe_records'], 2)
        self.assertEqual(report['setup_counts'], {'archive_exports': 2, 'archive_reuses': 2,
                                                  'runtime_probes': 1, 'runtime_reuses': 1})
        for folder in folders:
            evidence = json.loads((folder / 'evidence/paired_runs.json').read_text())
            self.assertEqual(len(evidence['before']), 3)
            self.assertEqual(len(evidence['fixed']), 3)
            self.assertTrue(evidence['verified_fail_to_pass'])
        # Editing a test must trigger fresh execution, never cached success.
        (folders[0] / 'reproducer.py').write_text('raise RuntimeError("different fault")\n')
        report = run_batch([folders[0]], self.repo, Path(sys.executable), jobs=1)
        self.assertEqual(report['records'][0]['status'], 'inspect_evidence')
        self.assertEqual(json.loads((folders[0] / 'record.json').read_text())['snapshot_results'], [])

    def test_setup_failure_clears_stale_success_without_stopping_other_records(self):
        bad, good = self.record('bad'), self.record('good')
        p = bad / 'record.json'
        r = json.loads(p.read_text())
        r.update(status='validated', snapshot_results=[{'result': 'bug_present'}])
        r['reproducer']['dependencies'] = ['catalog-nonexistent-dependency-84632']
        p.write_text(json.dumps(r))
        report = run_batch([bad, good], self.repo, Path(sys.executable), jobs=2)
        self.assertEqual([r['status'] for r in report['records']], ['blocked_setup', 'validated'])
        record = json.loads(p.read_text())
        self.assertEqual(record['status'], 'candidate')
        self.assertEqual(record['snapshot_results'], [])
        self.assertEqual(record['verification']['status'], 'blocked_setup')
        with self.assertRaises(ValueError):
            run_batch([good, good], self.repo, Path(sys.executable))

    def test_unreviewed_parallel_safety_runs_serially(self):
        folder = self.record('unmarked')
        p = folder / 'record.json'
        record = json.loads(p.read_text())
        record['reproducer'].pop('parallel_safe')
        p.write_text(json.dumps(record))
        report = run_batch([folder], self.repo, Path(sys.executable), jobs=4)
        self.assertEqual(report['parallel_safe_records'], 0)
        self.assertEqual(report['serial_records'], 1)
        self.assertEqual(report['records'][0]['status'], 'validated')

    def test_prepare_keeps_every_commit_and_does_not_classify(self):
        private = self.root / 'catalog/private_grading'
        (private / 'reviews').mkdir(parents=True)
        (private / 'reviews/decisions.json').write_text('{}')
        # Same change on a separate commit with the same parent.
        tree = self.git('rev-parse', self.fixed + '^{tree}')
        other = subprocess.check_output(['git', '-C', str(self.repo), 'commit-tree', tree,
                                         '-p', self.before], input=b'Another fix provenance\n').decode().strip()
        items = [{'sha': sha, 'parents': [self.before], 'title': sha, 'body': '',
                  'changed_files': ['click/__init__.py'], 'record': None,
                  'review': {'classification': 'unreviewed'}} for sha in [self.fixed, other]]
        inventory = private / 'commit_inventory.jsonl'
        original = ''.join(json.dumps(i) + '\n' for i in items)
        inventory.write_text(original)
        output = self.root / 'packet'
        report = prepare(self.repo, private.parent, output, 2)
        self.assertEqual(len(report['commits']), 2)
        self.assertEqual(len(report['patches']), 1)
        self.assertEqual(report['metrics']['ungrouped_patch_bytes'],
                         2 * report['metrics']['grouped_patch_bytes'])
        self.assertEqual(inventory.read_text(), original)
        self.assertEqual((private / 'reviews/decisions.json').read_text(), '{}')
        with self.assertRaises(ValueError):
            prepare(self.repo, private.parent, output, 2)


if __name__ == '__main__':
    unittest.main()
