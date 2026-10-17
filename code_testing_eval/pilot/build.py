"""Build auditable, scoped repairs; never export repair data to the agent workspace."""
import ast
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

BASE = 'dcc07195f9d3c120d419cb8d2d1d732e61f6f289'
MERGE = '4fd8955adb373e56c25dec6a95dffc991afd247d'
KEYS = ['', '--python3-length-hint', '--attribute-error-length-fallback',
        '--runner-environment-restore', '--runner-exit-none', '--runner-exception-exit-status',
        '--file-only-path-label', '--secho-none', '--standalone-broken-pipe', '--usage-help-hint']
TOPS = {'click', 'docs', 'tests', 'examples', 'LICENSE', 'README', 'README.rst', 'README.md',
        'setup.py', 'setup.cfg', 'tox.ini', 'Makefile'}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def seal(bundle):
    files = [bundle / name for name in ('plan.json', 'runtime.lock', 'source.tar.gz')]
    files += sorted((bundle / 'gold').glob('*.py'))
    manifest = {str(path.relative_to(bundle)): digest(path.read_bytes()) for path in files}
    dump(bundle / 'bundle_manifest.json', manifest)


def source(repo, rev, path):
    return subprocess.check_output(['git', '-C', str(repo), 'show', rev + ':' + path]).decode()


def edits(repo):
    def edit(path, before, after):
        assert before != after
        assert source(repo, BASE, path).count(before) == 1, (path, before)
        return {'path': path, 'before': before, 'after': after}
    exceptions = source(repo, BASE, 'click/exceptions.py')
    node = next(n for n in ast.parse(exceptions).body if isinstance(n, ast.ClassDef) and n.name == 'BadOptionUsage')
    old_class = ast.get_source_segment(exceptions, node)
    new_class = old_class.replace('def __init__(self, message, ctx=None):',
                                 'def __init__(self, option_name, message, ctx=None):') + '\n        self.option_name = option_name'
    bad = [edit('click/exceptions.py', old_class, new_class)]
    parser = source(repo, BASE, 'click/parser.py')
    for line in parser.splitlines(keepends=True):
        if 'raise BadOptionUsage(' in line:
            bad.append(edit('click/parser.py', line, line.replace('BadOptionUsage(', 'BadOptionUsage(opt, ')))
    length = [edit('click/_termui_impl.py', 'not isinstance(hint, (int, long))', 'not isinstance(hint, int_types)'),
              edit('click/_termui_impl.py', 'get_best_encoding, WIN\n', 'get_best_encoding, WIN, int_types\n'),
              edit('click/_compat.py', '    string_types = (str, unicode)\n', '    string_types = (str, unicode)\n    int_types = (int, long)\n'),
              edit('click/_compat.py', '    string_types = (str,)\n', '    string_types = (str,)\n    int_types = (int,)\n')]
    fallback = [edit('click/_termui_impl.py', '        return len(obj)\n    except TypeError:',
                     '        return len(obj)\n    except (AttributeError, TypeError):')]
    env = [edit('click/testing.py', 'old_env[key] = os.environ.get(value)', 'old_env[key] = os.environ.get(key)')]
    none = [edit('click/testing.py',
                '                if e.code != 0:\n                    exception = e\n\n                exc_info = sys.exc_info()\n\n                exit_code = e.code\n',
                '                exc_info = sys.exc_info()\n                exit_code = 0 if e.code is None else e.code\n                if exit_code != 0:\n                    exception = e\n')]
    exception = [edit('click/testing.py', '                exception = e\n                exit_code = -1\n                exc_info = sys.exc_info()',
                      '                exception = e\n                exit_code = 1\n                exc_info = sys.exc_info()')]
    path = [edit('click/types.py', '        if self.dir_okay and not self.file_okay:\n',
                 '        elif self.dir_okay and not self.file_okay:\n')]
    # Keep the existing keyword name. The historical rename was unrelated to
    # None handling and introduced a separate keyword regression.
    secho = [edit('click/termui.py',
                  '    return echo(style(text, **styles), file=file, nl=nl, err=err, color=color)\n',
                  '    if text is not None:\n        text = style(text, **styles)\n    return echo(text, file=file, nl=nl, err=err, color=color)\n')]
    pipe = [edit('click/core.py', 'import os\n', 'import errno\nimport os\n'),
            edit('click/core.py', '                sys.exit(e.exit_code)\n        except Abort:',
                 '                sys.exit(e.exit_code)\n            except IOError as e:\n                if standalone_mode and e.errno == errno.EPIPE:\n                    sys.exit(1)\n                raise\n        except Abort:')]
    def show_method(rev):
        text = source(repo, rev, 'click/exceptions.py')
        node = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == 'UsageError')
        method = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == 'show')
        return ''.join(text.splitlines(keepends=True)[method.lineno - 1:method.end_lineno])
    repaired_show = show_method(MERGE).replace('self.cmd is not None', 'self.ctx is not None').replace('self.cmd.get_help_option', 'self.ctx.command.get_help_option')
    help_hint = [edit('click/exceptions.py', show_method(BASE), repaired_show)]
    return [bad, length, fallback, env, none, exception, path, secho, pipe, help_hint]


def apply(root, plan, enabled):
    for bug in plan['bugs']:
        if bug['id'] not in enabled:
            continue
        for change in bug['edits']:
            path = root / change['path']
            text = path.read_text()
            if text.count(change['before']) != 1:
                raise ValueError('Repair precondition failed: ' + bug['id'] + ' ' + change['path'])
            path.write_text(text.replace(change['before'], change['after'], 1))


def extract(bundle, target):
    target.mkdir(parents=True, exist_ok=False)
    with tarfile.open(bundle / 'source.tar.gz') as archive:
        archive.extractall(target, filter='data')


def build(repo, catalog, bundle):
    if bundle.exists():
        raise ValueError('Use a new private bundle directory')
    repairs = edits(repo)
    bugs = []
    for i, (key, changes) in enumerate(zip(KEYS, repairs), 1):
        record_dir = catalog / 'private_grading/bugs' / ('click-' + MERGE + key)
        record = json.loads((record_dir / 'record.json').read_text())
        assert record['status'] == 'validated' and record['before_revision'] == BASE
        bugs.append({'id': f'B{i:02}', 'catalog_record': record['bug_id'],
                     'canonical_record': record['duplicate_of'], 'claim': record['behavioral_claim'],
                     'failure_marker': record['reproducer']['failure_marker'], 'edits': changes,
                     'scope_note': 'Scoped repair derived from reviewed upstream fix; preserve unrelated baseline behavior.'})
    bundle.mkdir(parents=True)
    archive = subprocess.check_output(['git', '-C', str(repo), 'archive', BASE])
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as upstream, tarfile.open(bundle / 'source.tar.gz', 'w:gz') as output:
        for member in upstream:
            path = Path(member.name)
            if path.parts[0] not in TOPS or not member.isfile():
                continue
            if any(part.startswith('.git') or part == '__pycache__' for part in path.parts):
                continue
            data = upstream.extractfile(member).read()
            member.mtime = 0
            member.uid = member.gid = 0
            member.uname = member.gname = ''
            member.pax_headers = {}
            output.addfile(member, io.BytesIO(data))
            files[member.name] = digest(data)
    plan = {'schema_version': 1, 'task_id': 'click-discovery-pilot', 'baseline_revision': BASE,
            'bugs': bugs, 'source_files': files, 'source_archive_sha256': digest((bundle / 'source.tar.gz').read_bytes()),
            'selection': 'Ten distinct runtime behaviors validated on one actual pre-fix snapshot; no transplanted bugs.',
            'repair_policy': 'Keep unrelated keyword and non-standalone pipe behavior; omit unrelated historical merge changes.'}
    dump(bundle / 'plan.json', plan)
    gold = bundle / 'gold'
    gold.mkdir()
    for bug in bugs:
        folder = catalog / 'private_grading/bugs' / bug['catalog_record']
        tree = ast.parse((folder / 'reproducer.py').read_text())
        tree.body = [node for node in tree.body if not (isinstance(node, ast.If) and
                     isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__')]
        (gold / ('test_' + bug['id'].lower() + '.py')).write_text(ast.unparse(tree) + '\n')
    from .controls import SOURCE
    (gold / 'test_compatibility_controls.py').write_text(SOURCE)
    (bundle / 'canary.txt').write_text('PRIVATE GRADING CANARY: must not be readable inside the agent sandbox.\n')
    return plan
