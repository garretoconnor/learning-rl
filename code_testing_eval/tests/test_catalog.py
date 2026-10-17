import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("catalog_click", ROOT / "scripts/catalog_click.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
spec = importlib.util.spec_from_file_location('split_record', ROOT / 'scripts/split_record.py')
splitter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(splitter)
spec = importlib.util.spec_from_file_location('audit_catalog', ROOT / 'scripts/audit_catalog.py')
auditor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auditor)
spec = importlib.util.spec_from_file_location('verify_record', ROOT / 'scripts/verify_record.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class CatalogTests(unittest.TestCase):
    def test_import_failure_requires_reproducer_assertion(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            record_dir = Path(tmp) / 'record'
            repo.mkdir()
            record_dir.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args],
                                               stderr=subprocess.DEVNULL).decode().strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'Catalog Test')
            git('config', 'user.email', 'catalog-test@example.invalid')
            (repo / 'click').mkdir()
            (repo / 'click/__init__.py').write_text('from . import utils\n')
            (repo / 'click/utils.py').write_text('"""C:\\Users\\example"""\n')
            git('add', '.')
            git('commit', '-m', 'Invalid Unicode escape')
            before = git('rev-parse', 'HEAD')
            (repo / 'click/utils.py').write_text('r"""C:\\Users\\example"""\n')
            git('add', '.')
            git('commit', '-m', 'Use raw string')
            fixed = git('rev-parse', 'HEAD')
            reproducer = record_dir / 'reproducer.py'
            reproducer.write_text(
                'import pathlib, unittest\n'
                'class ImportTest(unittest.TestCase):\n'
                '    def test_import(self):\n'
                '        try:\n'
                '            import click\n'
                '        except SyntaxError as error:\n'
                '            if "unicodeescape" not in str(error):\n'
                '                raise\n'
                '            self.fail("CLICK_IMPORT_UNICODE_ESCAPE")\n'
                '        self.assertEqual(pathlib.Path(click.__file__).resolve(),\n'
                '                         pathlib.Path("click/__init__.py").resolve())\n'
                'unittest.main()\n')
            record = json.loads((ROOT / 'templates/record.example.json').read_text())
            record.update(bug_id='import-test', before_revision=before, fixed_revision=fixed,
                          reproducer={'path': 'reproducer.py', 'source_mode': 'reproducer_import',
                                      'failure_marker': 'CLICK_IMPORT_UNICODE_ESCAPE'})
            path = record_dir / 'record.json'
            # Missing declared dependencies must stop verification before bug execution.
            record['reproducer']['dependencies'] = ['catalog-test-package-that-does-not-exist']
            path.write_text(json.dumps(record))
            with self.assertRaises(subprocess.CalledProcessError):
                verifier.verify(record_dir, repo, Path(sys.executable), repetitions=1)
            self.assertFalse((record_dir / 'evidence/paired_runs.json').exists())
            self.assertEqual(json.loads(path.read_text())['status'], 'candidate')
            record['reproducer'].pop('dependencies')
            path.write_text(json.dumps(record))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(verifier.verify(record_dir, repo, Path(sys.executable), repetitions=1))
            evidence = json.loads((record_dir / 'evidence/paired_runs.json').read_text())
            self.assertIn('CLICK_SOURCE_LOCATION=', evidence['before'][0]['stdout'])
            self.assertNotIn('CLICK_SOURCE=', evidence['before'][0]['stdout'])
            # A raw import error, without the expected assertion, is not a bug proof.
            reproducer.write_text('import click\n')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(verifier.verify(record_dir, repo, Path(sys.executable), repetitions=1))
            self.assertEqual(json.loads(path.read_text())['status'], 'candidate')
            self.assertEqual(json.loads(path.read_text())['snapshot_results'], [])
            # The default mode still refuses to run a test on an unimportable snapshot.
            runs = verifier.run_snapshot(repo, before, reproducer, Path(sys.executable), 1)
            self.assertNotIn('CLICK_SOURCE=', runs[0]['stdout'])
            with self.assertRaises(ValueError):
                verifier.run_snapshot(repo, before, reproducer, Path(sys.executable), 1, 'invalid')

    def test_split_records_get_separate_evidence_and_are_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / 'private_grading'
            parent_dir = private / 'bugs/click-test'
            parent_dir.mkdir(parents=True)
            (private / 'reviews').mkdir()
            review = {'classification': 'bugfix', 'kind': 'runtime', 'reason': 'reviewed'}
            parent = json.loads((ROOT / 'templates/record.example.json').read_text())
            parent.update(bug_id='click-test', source_commit='https://example.invalid/fix',
                          before_revision='before', fixed_revision='after',
                          review=review, status='validated', snapshot_results=[{'result': 'bug_present'}],
                          runtime={'status': 'verified'}, verification={'status': 'validated'})
            (parent_dir / 'record.json').write_text(json.dumps(parent))
            for filename in ('fix.patch', 'regression.patch', 'full.patch'):
                (parent_dir / filename).write_text('reviewed patch')
            with contextlib.redirect_stdout(io.StringIO()):
                child_dir = splitter.split(parent_dir, 'other-behavior', 'Second claim', 'Second diff reviewed')
            child = json.loads((child_dir / 'record.json').read_text())
            self.assertEqual(child['status'], 'candidate')
            self.assertEqual(child['verification']['status'], 'not_run')
            self.assertEqual(child['runtime']['status'], 'not_verified')
            self.assertEqual(child['snapshot_results'], [])
            self.assertIsNone(child['reproducer']['path'])
            self.assertEqual(child['before_revision'], 'before')
            self.assertEqual(json.loads((parent_dir / 'record.json').read_text())['status'], 'validated')
            with self.assertRaises(ValueError):
                splitter.split(parent_dir, 'other-behavior', 'Second claim', 'reviewed')
            inventory = {'sha': 'test', 'review': review,
                         'record': str((parent_dir / 'record.json').relative_to(root))}
            (private / 'commit_inventory.jsonl').write_text(json.dumps(inventory) + '\n')
            (private / 'reviews/decisions.json').write_text('{}')
            (private / 'manifest.json').write_text('{}')
            with contextlib.redirect_stdout(io.StringIO()):
                auditor.audit(root)
            counts = json.loads((root / 'STATUS.json').read_text())['counts']
            self.assertEqual(counts['records'], 2)
            self.assertEqual(counts['separate_behavior_records'], 1)
            self.assertEqual(counts['record_validated'], 1)
            self.assertEqual(counts['record_candidate'], 1)
            # A validated behavior does not prove its whole commit was reviewed.
            (private / 'reviews/decisions.json').write_text(json.dumps({
                'test': {**review, 'complete': False}
            }))
            with contextlib.redirect_stdout(io.StringIO()):
                auditor.audit(root)
            status = json.loads((root / 'STATUS.json').read_text())
            manifest = json.loads((private / 'manifest.json').read_text())
            self.assertEqual(status['counts']['reviews_incomplete'], 1)
            self.assertFalse(manifest['complete_bugfix_audit'])
            self.assertFalse(manifest['completion_gates']['all_commits_reviewed'])
            (private / 'reviews/decisions.json').write_text(json.dumps({
                'test': {**review, 'complete': True}
            }))
            with contextlib.redirect_stdout(io.StringIO()):
                auditor.audit(root)
            manifest = json.loads((private / 'manifest.json').read_text())
            self.assertTrue(manifest['complete_bugfix_audit'])
            self.assertTrue(manifest['completion_gates']['all_commits_reviewed'])

    def test_historical_production_layouts(self):
        for path in ("click.py", "click/core.py", "src/click/core.py"):
            self.assertTrue(collector.production(path))
        for path in ("docs/click.py", "tests/test_click.py", "examples/click.py"):
            self.assertFalse(collector.production(path))

    def test_inventory_covers_unmatched_commits_and_preserves_curated_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            output = Path(tmp) / "catalog"
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
            git("init", "-b", "main")
            git("config", "user.name", "Catalog Test")
            git("config", "user.email", "catalog-test@example.invalid")
            (repo / "CHANGES.md").write_text("- Fix empty output. {pr}`1`\n")
            (repo / "src/click").mkdir(parents=True)
            (repo / "src/click/utils.py").write_text("x = 1\n")
            git("add", ".")
            git("commit", "-m", "Initial source")
            first = git("rev-parse", "HEAD")
            (repo / "src/click/utils.py").write_text("x = 2\n")
            git("add", ".")
            git("commit", "-m", "Fix empty output (#1)")
            fixed = git("rev-parse", "HEAD")
            (repo / "README.md").write_text("A new explanation\n")
            git("add", ".")
            git("commit", "-m", "Document usage")
            unmatched = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/main", unmatched)
            with contextlib.redirect_stdout(io.StringIO()):
                collector.collect(repo, output)
            inventory = [json.loads(l) for l in (output / "private_grading/commit_inventory.jsonl").read_text().splitlines()]
            self.assertEqual({r["sha"] for r in inventory}, {first, fixed, unmatched})
            entry = next(r for r in inventory if r["sha"] == unmatched)
            self.assertIsNone(entry["record"])
            self.assertEqual(entry["review"]["classification"], "unreviewed")
            record = output / "private_grading/bugs" / ("click-" + fixed) / "record.json"
            data = json.loads(record.read_text())
            self.assertEqual(data["before_revision"], first)
            self.assertEqual(data["fixed_revision"], fixed)
            self.assertEqual(data["verification"]["status"], "not_run")
            data["behavioral_claim"] = "Curated claim"
            data['related_records'] = ['click-' + fixed + '--other-behavior']
            record.write_text(json.dumps(data))
            with contextlib.redirect_stdout(io.StringIO()):
                collector.collect(repo, output)
            self.assertEqual(json.loads(record.read_text())["behavioral_claim"], "Curated claim")
            self.assertEqual(json.loads(record.read_text())['related_records'],
                             ['click-' + fixed + '--other-behavior'])
            manifest = json.loads((output / "private_grading/manifest.json").read_text())
            self.assertFalse(manifest["complete_bugfix_audit"])

    @unittest.skipUnless((ROOT / 'private_grading/bugs').is_dir(), 'Requires local grading data excluded from the code-only checkout')
    def test_validated_records_have_executed_evidence(self):
        for path in (ROOT / "private_grading/bugs").glob("*/record.json"):
            record = json.loads(path.read_text())
            if record["status"] != "validated":
                continue
            evidence = json.loads((path.parent / record["verification"]["evidence"]).read_text())
            self.assertTrue(evidence["verified_fail_to_pass"])
            self.assertEqual(evidence["repetitions"], 3)
            self.assertTrue(all(r["exit_code"] == 1 for r in evidence["before"]))
            self.assertTrue(all(r["exit_code"] == 0 for r in evidence["fixed"]))
            self.assertTrue((path.parent / "runtime.lock").exists())
            runtime = json.loads((path.parent / "runtime.lock").read_text())
            requested = record['reproducer'].get('dependencies', [])
            actual = runtime['third_party_dependencies']
            if requested:
                self.assertEqual([item['name'] for item in actual], requested)
                self.assertTrue(all(item['version'] for item in actual))
                self.assertEqual(evidence['runtime']['third_party_dependencies'], actual)



if __name__ == "__main__":
    unittest.main()
