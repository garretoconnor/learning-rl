"""Public workspace export and private differential grading."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

from .build import apply, digest, dump, extract
from .isolation import run, runtime
from .submission import read_submission

PROMPT = '''Find incorrect behavior in this Click checkout and write regression tests.

This is an unmodified historical snapshot of Click. No artificial bugs were
inserted. A real defect counts even if it was present in an official release.
Judge behavior against the public API and documentation. Familiar or historical
behavior can still be a bug; you do not need a different release to prove it.

Work offline. Inspect the source, documentation, and existing tests. Add new
unittest tests under submissions/test_*.py. Keep the library and existing tests
unchanged. Use ordinary behavioral assertions against Click. Do not inspect
source files, versions, grader state, or runtime internals from your tests.
No implementation changes, network access, skipped tests, or third-party imports.

As soon as you confirm a defect, save a small unittest under submissions/ and
run it before exploring another area. Keep each confirmed finding as a saved
test. Scratch probes and your final message are not submissions. You can add
more tests after saving the first one. A failing regression test is expected
on this checkout when it demonstrates incorrect library behavior.

Run your new tests with: python3 -I -X utf8 -B run_tests.py
Existing upstream tests are available for reference; they are not your submission.
Run them with:
python3 -X utf8 -B -m pytest tests -q -p no:warnings -p no:cacheprovider
This command disables pytest warning capture and cache writes. The historical
warning-output test otherwise fails under pytest's warning capture, and the
read-only checkout prevents cache writes. These are test-runner issues.

Tests should fail because of incorrect Click behavior and pass when that behavior
is corrected. More than one independent bug may earn credit. Repeated tests for
the same bug earn no additional credit. Findings outside the known bank are
queued for review rather than automatically declared incorrect.
'''

PUBLIC_RUNNER = '''import pathlib, sys, unittest
root=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(root))
suite=unittest.defaultTestLoader.discover(str(root/'submissions'),pattern='test_*.py')
if not suite.countTestCases():
    print('Add unittest cases under submissions/test_*.py',file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
'''


def load_bundle(bundle, *, runtime_info=None):
    manifest = json.loads((bundle / 'bundle_manifest.json').read_text())
    for relative, expected in manifest.items():
        path = bundle / relative
        if not path.resolve().is_relative_to(bundle.resolve()) or digest(path.read_bytes()) != expected:
            raise ValueError('Private bundle integrity check failed: ' + relative)
    plan = json.loads((bundle / 'plan.json').read_text())
    if digest((bundle / 'source.tar.gz').read_bytes()) != plan['source_archive_sha256']:
        raise ValueError('Source archive hash mismatch')
    expected = json.loads((bundle / 'runtime.lock').read_text())
    if (runtime_info if runtime_info is not None else runtime()) != expected:
        raise ValueError('Runtime differs from the pinned pilot runtime; rebuild/revalidate explicitly')
    return plan


def assets(root, *, runtime_info=None):
    (root / 'submissions').mkdir()
    (root / 'scratch').mkdir()
    (root / 'TASK.md').write_text(PROMPT)
    (root / 'run_tests.py').write_text(PUBLIC_RUNNER)
    info = runtime_info if runtime_info is not None else runtime()
    dump(root / 'RUNTIME.json', {'python': info['python'], 'dependencies': info['dependencies']})


def export(bundle, workspace, *, runtime_info=None):
    plan = load_bundle(bundle, runtime_info=runtime_info)
    extract(bundle, workspace)
    assets(workspace, runtime_info=runtime_info)
    return {'task_id': plan['task_id'], 'workspace': str(workspace), 'prompt': str(workspace / 'TASK.md')}


def check_workspace(plan, workspace, *, runtime_info=None):
    if any((workspace / name).is_symlink() for name in ('scratch', 'submissions')):
        raise ValueError('Writable workspace directories must not be symlinks')
    expected = dict(plan['source_files'])
    expected.update({'TASK.md': digest(PROMPT.encode()), 'run_tests.py': digest(PUBLIC_RUNNER.encode())})
    info = runtime_info if runtime_info is not None else runtime()
    expected['RUNTIME.json'] = digest((json.dumps({'python': info['python'], 'dependencies': info['dependencies']},
                                                 indent=2, sort_keys=True) + '\n').encode())
    actual = {}
    for path in workspace.rglob('*'):
        relative = path.relative_to(workspace)
        if relative.parts[0] in {'submissions', 'scratch'}:
            continue
        if '__pycache__' in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError('Workspace contains a symlink')
        if path.is_file():
            actual[str(relative)] = digest(path.read_bytes())
    if actual != expected:
        raise ValueError('Library/existing tests/public task files changed; test-only submissions required')


def execute(bundle, plan, files, enabled, repetitions=3, *, backend=None):
    results = []
    for _ in range(repetitions):
        with tempfile.TemporaryDirectory(prefix='click-task-') as directory:
            root = Path(directory) / 'workspace'
            extract(bundle, root)
            apply(root, plan, enabled)
            assets(root, runtime_info=backend.runtime_info if backend is not None else None)
            for name, text in files.items():
                (root / 'submissions' / name).write_text(text)
            worker = Path(__file__).with_name('worker.py').resolve()
            runner = backend.run if backend is not None else run
            result = runner(root, [sys.executable, '-I', '-X', 'utf8', '-S', str(worker), str(root)], worker=worker)
            lines = [line.removeprefix('PILOT_RESULT=') for line in result['stdout'].splitlines() if line.startswith('PILOT_RESULT=')]
            if result['exit_code'] != 0 or result['timed_out']:
                result['outcome'] = {'phase': 'execution_error'}
            elif len(lines) != 1:
                # Collection errors deliberately have no test result sentinel.
                result['outcome'] = {'phase': 'collection_error'}
            else:
                try:
                    result['outcome'] = json.loads(lines[0])
                except json.JSONDecodeError:
                    result['outcome'] = {'phase': 'execution_error'}
            result['stdout'] = result['stdout'].replace(str(root), '<WORKSPACE>')
            result['stderr'] = result['stderr'].replace(str(root), '<WORKSPACE>')
            results.append(result)
    return results


def behavior(row):
    return row['status'] in {'failed', 'error'} and row['click_called'] and not any(
        d.get('setup_error') or d['type'] in {'ImportError', 'ModuleNotFoundError', 'PermissionError'} for d in row['details'])


def stable_rows(runs):
    if any(run['outcome'].get('phase') != 'tests' for run in runs):
        return None
    rows = [{row['id']: row for row in run['outcome']['tests']} for run in runs]
    if not rows or not rows[0] or any(set(r) != set(rows[0]) for r in rows):
        return None
    for name in rows[0]:
        signature = lambda row: (row['status'], tuple(d['type'] for d in row.get('details', [])))
        if any(signature(trial[name]) != signature(rows[0][name]) for trial in rows[1:]):
            return None
    return rows


def grade(bundle, submissions, report, *, workspace=None, backend=None):
    if not report.resolve().is_relative_to(bundle.resolve()):
        raise ValueError('Write grading reports inside the private bundle')
    runtime_info = backend.runtime_info if backend is not None else None
    plan = load_bundle(bundle, runtime_info=runtime_info)
    try:
        if workspace is not None:
            check_workspace(plan, workspace, runtime_info=runtime_info)
        files = read_submission(submissions)
    except (ValueError, SyntaxError, UnicodeError, OSError) as error:
        result = {'status': 'invalid_submission', 'score': 0.0, 'error': str(error)}
        dump(report, result)
        return result
    identifiers = {bug['id'] for bug in plan['bugs']}
    evidence = {'baseline': execute(bundle, plan, files, set(), backend=backend),
                'all_repaired': execute(bundle, plan, files, identifiers, backend=backend)}
    base = stable_rows(evidence['baseline'])
    control = stable_rows(evidence['all_repaired'])
    source_hash = digest(json.dumps(files, sort_keys=True).encode())
    result = {'schema_version': 1, 'submission_sha256': source_hash, 'score': 0.0,
              'plan_sha256': digest((bundle / 'plan.json').read_bytes()),
              'grader_sha256': digest(b''.join(path.name.encode() + path.read_bytes() for path in sorted(Path(__file__).parent.glob('*.py')))),
              'known_bugs_total': len(identifiers), 'matched_bugs': [], 'needs_review': [],
              'evidence': evidence, 'repetitions': 3}
    if base is None or control is None or set(base[0]) != set(control[0]):
        result.update(status='invalid_execution', error='Collection/setup failure, timeout, or unstable collection')
    elif any(r['status'] == 'skipped' for runs in (base, control) for trial in runs for r in trial.values()):
        result.update(status='invalid_submission', error='Skipped tests are not accepted')
    elif any(row['status'] in {'failed', 'error'} and not behavior(row)
             for runs in (base, control) for trial in runs for row in trial.values()):
        result.update(status='invalid_submission', error='Failure is unrelated to executed Click behavior')
    else:
        eligible = {name for name in base[0] if all(behavior(trial[name]) for trial in base)
                    and all(trial[name]['status'] == 'passed' for trial in control)}
        for bug in plan['bugs']:
            if not eligible:
                break
            runs = execute(bundle, plan, files, identifiers - {bug['id']}, backend=backend)
            evidence['restored_' + bug['id']] = runs
            rows = stable_rows(runs)
            if rows is None or set(rows[0]) != set(base[0]):
                result['needs_review'].append({'reason': 'Unstable or invalid diagnostic variant', 'bug': bug['id']})
                continue
            killed = sorted(name for name in eligible if all(behavior(trial[name]) for trial in rows))
            if killed:
                result['matched_bugs'].append({'bug': bug['id'], 'tests': killed})
        matched_tests = {test for item in result['matched_bugs'] for test in item['tests']}
        for name in base[0]:
            if any(behavior(trial[name]) for trial in base) and name not in matched_tests:
                result['needs_review'].append({'test': name, 'reason': 'Behavioral failure has no confirmed bank attribution'})
        result['score'] = len(result['matched_bugs']) / len(identifiers)
        result['status'] = 'needs_review' if result['needs_review'] else 'graded'
        result['reward_basis'] = 'Distinct known bugs; baseline fails, repaired control passes, one-bug-restored variant fails, all three times'
    if result.get('needs_review'):
        dump(bundle / 'review_queue' / (source_hash + '.json'), {'submission': files, 'report': str(report.resolve()),
                                                               'findings': result['needs_review']})
    dump(report, result)
    return result
