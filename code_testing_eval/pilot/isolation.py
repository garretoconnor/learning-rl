"""Native macOS filesystem/network boundary. Refuse to run without it."""
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import signal
import subprocess
import sys
import tempfile


def runtime():
    import importlib.metadata
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'executable': str(Path(sys.executable).resolve()),
            'executable_sha256': hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
            'dependencies': {name: importlib.metadata.version(name)
                             for name in ('pytest', 'pluggy', 'packaging', 'iniconfig', 'pygments')}}


def profile(workspace, scratch, python, *, editable=False, worker=None):
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise RuntimeError('This pilot requires the native macOS sandbox; no unsandboxed fallback')
    q = lambda path: json.dumps(str(Path(path).resolve()))
    framework = Path(python).resolve().parents[1]
    read_roots = [workspace, framework, '/System/Library', '/System/Cryptexes', '/usr/lib', '/usr/share', '/bin', '/sbin', '/usr/bin', '/usr/sbin',
                  '/Library/Apple/System', '/private/etc']
    rules = ['(version 1)', '(deny default)', '(allow process*)', '(allow sysctl-read)',
             '(allow file-read-metadata)', '(allow file-read* (literal "/"))',
             '(allow mach-lookup (global-name "com.apple.system.logger"))']
    for path in read_roots:
        rules.append('(allow file-read* (subpath ' + q(path) + '))')
    if worker:
        rules.append('(allow file-read* (literal ' + q(worker) + '))')
    for path in ['/dev/null', '/dev/urandom', '/dev/random', '/dev/fd']:
        rules.append('(allow file-read* file-write* (subpath ' + q(path) + '))')
    rules.append('(allow file-write* (subpath ' + q(scratch) + '))')
    if editable:
        rules.append('(allow file-write* (subpath ' + q(Path(workspace) / 'submissions') + '))')
    return '\n'.join(rules)


def run(workspace, command, *, editable=False, worker=None, timeout=15):
    workspace = workspace.resolve()
    if any((workspace / name).is_symlink() for name in ('scratch', 'submissions')):
        raise ValueError('Writable workspace directories must not be symlinks')
    scratch = workspace / 'scratch'
    scratch.mkdir(exist_ok=True)
    env = {'PATH': str(Path(sys.executable).resolve().parent) + ':/usr/bin:/bin',
           'HOME': str(scratch), 'TMPDIR': str(scratch), 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8',
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
           'PYTHONIOENCODING': 'utf-8'}
    def limits():
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Output files stay outside the sandbox's readable/writable paths. Child
    # stdout/stderr descriptors are bounded by RLIMIT_FSIZE.
    with tempfile.TemporaryDirectory(prefix='pilot-control-') as control:
        control = Path(control)
        policy = control / 'policy.sb'
        policy.write_text(profile(workspace, scratch, sys.executable, editable=editable, worker=worker))
        with (control / 'stdout').open('wb') as out, (control / 'stderr').open('wb') as err:
            process = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(policy), *map(str, command)],
                                       cwd=workspace, env=env, stdout=out, stderr=err,
                                       start_new_session=True, preexec_fn=limits)
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        return {'exit_code': process.returncode, 'timed_out': timed_out,
                'stdout': (control / 'stdout').read_bytes()[:1024 * 1024].decode('utf-8', 'replace'),
                'stderr': (control / 'stderr').read_bytes()[:1024 * 1024].decode('utf-8', 'replace')}
