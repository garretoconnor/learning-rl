"""Controller-side Prime VM transport; no submitted code executes here."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import shlex
import sys
import tarfile
import tempfile
import time
import uuid

from prime_sandboxes import APIClient, CreateSandboxRequest, SandboxClient


def api_client(auth_file=None):
    # Verifiers strips API_KEY variables from child tool processes. A private
    # controller-only file passes the credential to trusted tools, never agents.
    key = Path(auth_file).read_text() if auth_file is not None else None
    return APIClient(api_key=key)


def workspace_archive(workspace):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        for path in sorted(workspace.rglob('*')):
            relative = path.relative_to(workspace)
            if '__pycache__' in relative.parts or relative.parts[0] == 'scratch':
                continue
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError('Only regular workspace files and directories can be uploaded')
            archive.add(path, arcname=str(relative), recursive=False)
    if len(data.getvalue()) > 16 * 1024 * 1024:
        raise ValueError('Workspace upload exceeds 16 MiB')
    return data.getvalue()


class PrimeBackend:
    def __init__(self, image, *, sandbox_id=None, client=None, region=None, role='grading', on_create=None, auth_file=None):
        if not image.startswith('prime/'):
            raise ValueError('Use the checked private Prime runtime image')
        self.image = image
        self.client = client or SandboxClient(api_client(auth_file))
        self.owned = sandbox_id is None
        self.sandbox_id = sandbox_id
        self.closed = False
        try:
            if self.owned:
                request = CreateSandboxRequest(name='click-pilot-' + role + '-' + uuid.uuid4().hex[:8],
                    docker_image=image, cpu_cores=1, memory_gb=2, disk_size_gb=10,
                    timeout_minutes=45, idle_timeout_minutes=10, network_allowlist=[],
                    labels=['click-pilot', 'migration', role], region=region)
                sandbox = self.client.create(request)
                self.sandbox_id = sandbox.id
                if on_create:
                    on_create(self.sandbox_id)
                if not sandbox.vm:
                    raise RuntimeError('Prime did not create a VM; refusing container-only isolation')
                self.client.wait_for_creation(self.sandbox_id)
            deadline = time.monotonic() + 60
            while True:
                policy = self.client.get_network(self.sandbox_id)
                denied = ((policy.policy.allowlist == [] and not policy.policy.denylist)
                          or (policy.policy.denylist == ['*'] and policy.policy.allowlist is None))
                if policy.applied and denied:
                    break
                if policy.applied or time.monotonic() >= deadline:
                    raise RuntimeError('Prime deny-all egress policy is not applied; refusing execution')
                time.sleep(0.2)
            self.runtime_info = self.call('bootstrap', api_timeout=180)
            self.runtime_info['prime_image'] = image
            if self.runtime_info['python'] != '3.12.3':
                raise RuntimeError('Cloud Python differs from the required 3.12.3 runtime')
        except BaseException:
            if self.owned and self.sandbox_id:
                self.close()
            raise

    def call(self, action, *, api_timeout=60, **fields):
        command = shlex.join(['/usr/local/bin/python3', '-I', '/opt/click-pilot/supervisor.py',
                              json.dumps({'action': action, **fields})])
        result = self.client.execute_command(self.sandbox_id, command, timeout=api_timeout)
        if result.exit_code:
            raise RuntimeError(f'Prime supervisor {action} failed: {result.stderr[-1500:]}')
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise RuntimeError('Invalid Prime supervisor response') from error

    def install(self, workspace):
        token = uuid.uuid4().hex
        remote = '/tmp/pilot-upload-' + token + '.tar.gz'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workspace.tar.gz'
            path.write_bytes(workspace_archive(workspace))
            self.client.upload_file(self.sandbox_id, remote, str(path))
        self.call('install', token=token, archive=remote)
        return token

    def command(self, token, command, *, editable=False, worker=False, timeout=15):
        return self.call('run', token=token, command=command, editable=editable, worker=worker, timeout=timeout)

    def run(self, workspace, command, *, editable=False, worker=None, timeout=15):
        token = self.install(workspace)
        translated = []
        for i, part in enumerate(map(str, command)):
            if i == 0 and part == sys.executable:
                part = '/usr/local/bin/python3'
            if worker is not None and part == str(worker):
                part = '/worker.py'
            elif part == str(workspace):
                part = '/workspace'
            translated.append(part)
        try:
            result = self.command(token, translated, editable=editable, worker=worker is not None, timeout=timeout)
            if editable:
                collected = self.call('collect', token=token)
                if 'invalid_submission' in collected:
                    raise ValueError(collected['invalid_submission'])
                for name, text in collected['files'].items():
                    if Path(name).name != name or not name.startswith('test_') or not name.endswith('.py'):
                        raise ValueError('Invalid collected filename')
                    (workspace/'submissions'/name).write_text(text)
            # Keep tracebacks independent of execution backend.
            for key in ('stdout', 'stderr'):
                result[key] = result[key].replace('/workspace', str(workspace))
            return result
        finally:
            self.call('remove', token=token)

    def close(self):
        if not self.closed and self.sandbox_id:
            self.client.delete(self.sandbox_id)
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.owned:
            self.close()
