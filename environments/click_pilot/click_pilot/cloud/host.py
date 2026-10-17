"""Private rollout controller for Prime execution. No model code runs here."""
import json
import hashlib
from pathlib import Path
import re
import shutil
import sys

from .backend import PrimeBackend, api_client


def modules(config):
    root = config.root.resolve()
    path = str(root/'code_testing_eval')
    if path not in sys.path:
        sys.path.insert(0, path)
    from pilot import environment
    return environment


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def cleanup_run(config, run_dir):
    from prime_sandboxes import APIClient, APIError, SandboxClient
    client = SandboxClient(api_client(config.auth_file))
    for path in run_dir.glob('*-vm.json'):
        state = json.loads(path.read_text())
        if not state.get('deleted'):
            try:
                client.delete(state['sandbox_id'])
            except APIError as error:
                response = getattr(error.__cause__, 'response', None)
                if getattr(response, 'status_code', None) != 404:
                    raise
            state['deleted'] = True
            save(path, state)


def new_backend(config, run_dir, role):
    if not config.image:
        raise ValueError('A built and verified Prime image is required')
    path = run_dir/(role+'-vm.json')
    backend = PrimeBackend(config.image, region=config.region, role=role, auth_file=config.auth_file,
        on_create=lambda identity: save(path, {'sandbox_id': identity, 'deleted': False}))
    # The rollout journal owns cleanup, including recovery after a process exit.
    backend.owned = False
    return backend


def main(config, action, run_id, **kwargs):
    if not re.fullmatch('[0-9a-f]{32}', run_id):
        raise ValueError('Invalid rollout identity')
    environment = modules(config)
    bundle = config.bundle.resolve()
    run_dir = bundle/'verifiers_runs'/run_id
    workspace = run_dir/'workspace'
    state_path = run_dir/'agent.json'
    if action == 'new':
        run_dir.mkdir(parents=True, exist_ok=False)
        try:
            backend = new_backend(config, run_dir, 'agent')
            environment.export(bundle, workspace, runtime_info=backend.runtime_info)
            token = backend.install(workspace)
            save(state_path, {'sandbox_id': backend.sandbox_id, 'token': token,
                              'runtime_info': backend.runtime_info, 'image': config.image})
            return {'ready': True}
        except BaseException:
            cleanup_run(config, run_dir)
            raise
    if action == 'check':
        return {'exists': state_path.is_file()}
    if action == 'cleanup':
        cleanup_run(config, run_dir)
        return {'cleaned': True}
    state = json.loads(state_path.read_text())
    if state['image'] != config.image:
        raise ValueError('Image changed during rollout')
    if action == 'exec':
        command = kwargs['command']
        if not isinstance(command, str) or len(command.encode()) > 32768:
            raise ValueError('Command exceeds 32 KiB')
        backend = PrimeBackend(config.image, sandbox_id=state['sandbox_id'], auth_file=config.auth_file)
        if backend.runtime_info != state['runtime_info']:
            raise ValueError('Agent runtime changed during rollout')
        return backend.command(state['token'], ['/bin/bash', '--noprofile', '--norc', '-c', command], editable=True)
    if action != 'grade':
        raise ValueError('Unknown cloud host action')
    try:
        agent = PrimeBackend(config.image, sandbox_id=state['sandbox_id'], auth_file=config.auth_file)
        collection = agent.call('collect', token=state['token'])
        # Stop the agent before any grading variant is provisioned.
        cleanup_run(config, run_dir)
        if 'invalid_submission' in collection:
            result = {'status': 'invalid_submission', 'score': 0.0, 'error': collection['invalid_submission']}
            save(run_dir/'grading.json', result)
        else:
            folder = workspace/'submissions'
            shutil.rmtree(folder)
            folder.mkdir()
            for name, text in collection['files'].items():
                # Defense in depth before writing any downloaded filename.
                if not re.fullmatch(r'test_[A-Za-z0-9_]+\.py', name):
                    raise ValueError('Invalid collected filename')
                (folder/name).write_text(text)
            with new_backend(config, run_dir, 'grading') as grading:
                result = environment.grade(bundle, folder, run_dir/'grading.json', workspace=workspace, backend=grading)
        result['execution_backend'] = 'prime'
        result['runtime_lock_sha256'] = hashlib.sha256((bundle/'runtime.lock').read_bytes()).hexdigest()
        here = Path(__file__).parents[1]
        result['adapter_sha256'] = hashlib.sha256(b''.join(str(p.relative_to(here)).encode()+p.read_bytes()
            for p in sorted(here.rglob('*')) if p.is_file()
            and (p.suffix in {'.py', '.md'} or p.name == 'Dockerfile'))).hexdigest()
        save(run_dir/'grading.json', result)
        return {'status': result['status'], 'score': result['score'],
                'matched': len(result.get('matched_bugs', [])), 'needs_review': len(result.get('needs_review', []))}
    finally:
        cleanup_run(config, run_dir)
