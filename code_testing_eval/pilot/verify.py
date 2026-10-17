"""End-to-end checks for the complete task, using actual sandboxed execution."""
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile

from .build import apply, digest, dump, extract
from .environment import check_workspace, export, grade as grade_tests, load_bundle
from .isolation import run


def verify_pilot(bundle, output, *, backend=None):
    if output.exists():
        raise ValueError('Use a new verification output directory')
    output.mkdir(parents=True)
    runtime_info = backend.runtime_info if backend is not None else None
    plan = load_bundle(bundle, runtime_info=runtime_info)
    runner = backend.run if backend is not None else run
    def grade(*args, **kwargs):
        return grade_tests(*args, **kwargs, backend=backend)
    ids = {bug['id'] for bug in plan['bugs']}
    assert 8 <= len(ids) <= 16
    checks = {}
    with tempfile.TemporaryDirectory(prefix='pilot-audit-') as temp:
        temp = Path(temp)
        workspace = temp / 'workspace'
        export(bundle, workspace, runtime_info=runtime_info)
        check_workspace(plan, workspace, runtime_info=runtime_info)
        forbidden = {plan['baseline_revision'], *(b['canonical_record'] for b in plan['bugs']),
                     *(b['failure_marker'] for b in plan['bugs'])}
        for path in workspace.rglob('*'):
            assert path.name != '.git' and not path.is_symlink()
            if path.is_file():
                text = path.read_text(errors='replace')
                assert not any(secret in text for secret in forbidden), path
        checks['workspace_has_no_history_private_ids_or_markers'] = True

        # Real listener distinguishes blocked networking from a closed port.
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            target = ('1.1.1.1', 443) if backend is not None else ('127.0.0.1', port)
            code = f'''import errno,json,pathlib,socket
checks={{}}
for key,path in [('private_read',{str((bundle/'canary.txt').resolve())!r}),('private_listing',{str(bundle.resolve())!r}),('source_write','click/__init__.py')]:
 try:
  if key=='private_read':pathlib.Path(path).read_text()
  elif key=='private_listing':list(pathlib.Path(path).iterdir())
  else:pathlib.Path(path).write_text('tampered')
 except OSError as error:checks[key]=error.errno in (errno.EACCES,errno.EPERM,errno.ENOENT,errno.EROFS)
 else:checks[key]=False
try:
 s=socket.socket();s.settimeout(1);s.connect({target!r})
except OSError as error:checks['network']=error.errno in (errno.EACCES,errno.EPERM,errno.ENETUNREACH)
else:checks['network']=False
pathlib.Path('submissions/test_created.py').write_text('created')
checks['submission_write']=True
print(json.dumps(checks))
'''
            probe = runner(workspace, [sys.executable, '-I', '-X', 'utf8', '-S', '-c', code], editable=True)
        dump(output / 'isolation.json', probe)
        assert probe['exit_code'] == 0, probe
        isolation = json.loads(probe['stdout'])
        assert isolation and all(isolation.values()), isolation
        (workspace / 'submissions/test_created.py').unlink()
        check_workspace(plan, workspace, runtime_info=runtime_info)
        checks['native_isolation'] = isolation

        # Verify repair composition and each original source restoration exactly.
        all_fixed = temp / 'all-fixed'
        extract(bundle, all_fixed)
        apply(all_fixed, plan, ids)
        for bug in plan['bugs']:
            single = temp / ('without-' + bug['id'])
            extract(bundle, single)
            apply(single, plan, ids - {bug['id']})
            for file in plan['source_files']:
                original = (workspace / file).read_bytes()
                actual = (single / file).read_bytes()
                if not any(edit['path'] == file for b in plan['bugs'] if b['id'] != bug['id'] for edit in b['edits']):
                    assert digest(actual) == digest(original)
        checks['repair_composition'] = True

        cases = {}
        def case(name, files, expected_score, expected_status='graded'):
            folder = temp / name
            folder.mkdir()
            for file, text in files.items():
                (folder / file).write_text(text)
            result = grade(bundle, folder, output / (name + '.json'))
            assert result['status'] == expected_status, (name, result['status'], result.get('error'))
            assert result['score'] == expected_score, (name, result['score'])
            cases[name] = {'score': result['score'], 'status': result['status'],
                           'matched_bugs': result.get('matched_bugs', [])}
            return result

        gold = {path.name: path.read_text() for path in (bundle / 'gold').glob('test_*.py')}
        reference = case('reference_all', gold, 1.0)
        assert {match['bug'] for match in reference['matched_bugs']} == ids
        for bug in plan['bugs']:
            own_module = 'test_' + bug['id'].lower() + '.'
            assert any(test.startswith(own_module) for item in reference['matched_bugs']
                       if item['bug'] == bug['id'] for test in item['tests']), bug['id']
        checks['all_ten_bugs_independently_attributed'] = True
        passing = '''import io,unittest,click
class Tests(unittest.TestCase):
 def test_echo(self):
  out=io.StringIO();click.echo('hello',file=out);self.assertEqual(out.getvalue(),'hello\\n')
'''
        case('passing_only', {'test_passing.py': passing + "\nif __name__ == '__main__':\n    unittest.main()\n"}, 0.0)
        case('unrelated_failure', {'test_wrong.py': 'import unittest\nclass Tests(unittest.TestCase):\n def test_wrong(self):self.assertEqual(1,2)\n'}, 0.0, 'invalid_submission')
        case('collection_failure', {'test_missing.py': 'import click.this_module_does_not_exist\n'}, 0.0, 'invalid_execution')
        case('setup_failure', {'test_setup.py': 'import unittest\nclass Tests(unittest.TestCase):\n def setUp(self):raise RuntimeError("setup failure")\n def test_body(self):pass\n'}, 0.0, 'invalid_submission')
        case('duplicate_detection', {'test_first.py': gold['test_b07.py'], 'test_repeat.py': gold['test_b07.py']}, 0.1)
        case('different_known_bug', {'test_alternative.py': gold['test_b08.py']}, 0.1)
        case('multiple_known_bugs', {'test_first.py': gold['test_b07.py'], 'test_second.py': gold['test_b08.py']}, 0.2)
        # This is deliberately unproven: demonstrate queueing, not acceptance
        # of the claim as legitimate. Real Click runs, but the assertion is wrong.
        unknown = passing.replace("'hello\\n')", "'other\\n')")
        result = case('unmatched_claim', {'test_unmatched.py': unknown}, 0.0, 'needs_review')
        assert (bundle / 'review_queue' / (result['submission_sha256'] + '.json')).is_file()
        case('introspection_rejected', {'test_inspect.py': 'import click\ndef test_x():return click.__file__\n'}, 0.0, 'invalid_submission')
        (workspace / 'click/__init__.py').write_text('tampered')
        tamper = grade(bundle, workspace / 'submissions', output / 'source_tampering.json', workspace=workspace)
        assert tamper['status'] == 'invalid_submission' and tamper['score'] == 0
        checks['source_changes_rejected'] = True

        # A bounded direct sandbox check proves timeout cleanup without a slow
        # three-repeat submitted infinite loop.
        timed = runner(workspace, [sys.executable, '-I', '-S', '-c', 'while True: pass'], timeout=0.2)
        dump(output / 'timeout.json', timed)
        assert timed['timed_out']
        checks['timeout_cleanup'] = True
        checks['controlled_submissions'] = cases
    result = {'task_id': plan['task_id'], 'bugs': len(ids), 'checks': checks, 'passed': True,
              'scope': ('Prime VM and Linux container audit' if backend is not None else 'Native macOS audit') + '; no unbiased model capability trial.'}
    dump(output / 'summary.json', result)
    return result
