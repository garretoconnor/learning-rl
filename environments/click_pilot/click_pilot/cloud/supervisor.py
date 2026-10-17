"""Trusted VM supervisor. Agent code runs only in restricted child containers."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import resource
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path('/var/lib/click-pilot/runs')
IMAGE = 'click-pilot-exec:pinned'


def checked_root(token):
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('Invalid workspace identity')
    return ROOT / token


def docker_args(workspace, command, *, editable=False, worker=False, name=None):
    args = ['docker', 'run', '--rm', '--name', name or ('pilot-' + uuid.uuid4().hex),
            '--pull', 'never', '--network', 'none', '--read-only', '--user', '65534:65534',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true',
            '--pids-limit', '64', '--memory', '512m', '--memory-swap', '512m', '--cpus', '1',
            '--ulimit', 'cpu=10:10', '--ulimit', 'fsize=2097152:2097152', '--ulimit', 'core=0:0',
            '--log-driver', 'none', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777',
            '--workdir', '/workspace', '--mount', f'type=bind,src={workspace},dst=/workspace,readonly',
            '--mount', f'type=bind,src={workspace}/scratch,dst=/workspace/scratch',
            '--env', 'PATH=/usr/local/bin:/usr/bin:/bin', '--env', 'HOME=/workspace/scratch',
            '--env', 'TMPDIR=/workspace/scratch', '--env', 'LANG=C.UTF-8', '--env', 'LC_ALL=C.UTF-8',
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1',
            '--env', 'PYTHONIOENCODING=utf-8']
    if editable:
        args += ['--mount', f'type=bind,src={workspace}/submissions,dst=/workspace/submissions']
    if worker:
        args += ['--mount', 'type=bind,src=/opt/click-pilot/worker.py,dst=/worker.py,readonly']
    return args + [IMAGE, *command]


def bootstrap():
    ROOT.mkdir(parents=True, exist_ok=True)
    if subprocess.run(['docker', 'info'], capture_output=True).returncode:
        log = open('/var/log/click-pilot-docker.log', 'ab')
        subprocess.Popen(['dockerd', '--iptables=false', '--bridge=none', '--storage-driver=vfs'],
                         stdout=log, stderr=log, start_new_session=True)
        for _ in range(60):
            if subprocess.run(['docker', 'info'], capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('Docker did not start; refusing unrestricted execution')
    if subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True).returncode:
        subprocess.run(['docker', 'import', '/opt/click-pilot/exec-rootfs.tar', IMAGE], check=True, capture_output=True)
    code = '''import hashlib,importlib.metadata,json,pathlib,platform,sys
p=pathlib.Path(sys.executable).resolve()
print(json.dumps({'python':platform.python_version(),'platform':'linux-'+platform.machine(),
'executable':str(p),'executable_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
'dependencies':{n:importlib.metadata.version(n) for n in ('pytest','pluggy','packaging','iniconfig','pygments')}}))'''
    result = subprocess.run(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
                             IMAGE, '/usr/local/bin/python3', '-I', '-c', code], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def install(token, archive_path):
    root = checked_root(token)
    root.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        if len(members) > 1000 or sum(m.size for m in members) > 16 * 1024 * 1024:
            raise ValueError('Workspace archive exceeds limits')
        for member in members:
            if not member.isfile() and not member.isdir():
                raise ValueError('Workspace links and special files are forbidden')
            if not (root / member.name).resolve().is_relative_to(root):
                raise ValueError('Workspace path traversal')
        archive.extractall(root, members=members, filter='data')
    for path in root.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)
    for name in ('scratch', 'submissions'):
        (root / name).mkdir(exist_ok=True)
        (root / name).chmod(0o777)
    Path(archive_path).unlink()
    return {'ready': True}


def collect(token):
    folder = checked_root(token) / 'submissions'
    files = {}
    total = 0
    for path in folder.iterdir():
        if path.name == '__pycache__' and path.is_dir() and not path.is_symlink():
            continue
        if not stat.S_ISREG(path.lstat().st_mode) or not re.fullmatch(r'test_[A-Za-z0-9_]+\.py', path.name):
            raise ValueError('Only regular flat test_*.py files may be collected')
        if path.stat().st_size > 32768 or len(files) >= 16:
            raise ValueError('Submission size/count limit exceeded')
        raw = path.read_bytes()
        total += len(raw)
        if total > 131072:
            raise ValueError('Submission total exceeds 128 KiB')
        files[path.name] = raw.decode('utf-8')
    return {'files': files}


def run(request):
    workspace = checked_root(request['token'])
    if any((workspace / p).is_symlink() for p in ('scratch', 'submissions')):
        raise ValueError('Writable directories must not be links')
    command = request['command']
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError('Invalid command')
    name = 'pilot-' + uuid.uuid4().hex
    args = docker_args(workspace, command, editable=request.get('editable', False),
                       worker=request.get('worker', False), name=name)
    # Create before attaching. The name must exist before model code starts,
    # so timeout cleanup cannot race a still-pending container creation.
    create = ['docker', 'create', *[arg for arg in args[2:] if arg != '--rm']]
    created = subprocess.run(create, capture_output=True, text=True, timeout=30)
    if created.returncode:
        raise RuntimeError('Container creation failed: ' + created.stderr[-1000:])
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (2097152, 2097152))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryDirectory(prefix='pilot-output-') as directory:
        directory = Path(directory)
        with (directory/'out').open('wb') as out, (directory/'err').open('wb') as err:
            process = subprocess.Popen(['docker', 'start', '--attach', name], stdout=out, stderr=err, preexec_fn=limits)
            timed_out = False
            exit_code = None
            try:
                process.wait(timeout=min(float(request.get('timeout', 15)), 15))
                inspected = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', name],
                                           capture_output=True, text=True, check=True, timeout=15)
                state = json.loads(inspected.stdout)
                if state.get('Error'):
                    raise RuntimeError('Container start failed: ' + state['Error'])
                exit_code = state['ExitCode']
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
                if process.poll() is None:
                    process.kill()
                process.wait()
        result = {'exit_code': process.returncode if timed_out else exit_code, 'timed_out': timed_out,
                  'stdout': (directory/'out').read_bytes()[:1048576].decode('utf-8', 'replace'),
                  'stderr': (directory/'err').read_bytes()[:1048576].decode('utf-8', 'replace')}
        return result


def main(request):
    action = request['action']
    if action == 'bootstrap':
        return bootstrap()
    if action == 'install':
        return install(request['token'], request['archive'])
    if action == 'collect':
        try:
            return collect(request['token'])
        except (ValueError, UnicodeError, OSError) as error:
            return {'invalid_submission': str(error)}
    if action == 'remove':
        shutil.rmtree(checked_root(request['token']), ignore_errors=True)
        return {'removed': True}
    if action == 'run':
        return run(request)
    raise ValueError('Unknown supervisor action')


if __name__ == '__main__':
    print(json.dumps(main(json.loads(sys.argv[1]))))
