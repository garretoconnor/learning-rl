"""Explicit migration operations. No model inference is launched by this command."""
import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import shlex
import shutil
import sys
import tarfile
import tempfile
import uuid

from prime_sandboxes import APIClient, ImageClient, SandboxClient, UnauthorizedError
from ..config import HostConfig, PilotConfig, ROOT
from .backend import PrimeBackend
from .host import modules, save, cleanup_run
from .images import prepare, build


def initialize(config, source):
    environment = modules(config)
    # Check the existing sealed source as historical evidence. Its macOS
    # runtime lock stays intact; no native verification is claimed here.
    environment.load_bundle(source, runtime_info=json.loads((source/'runtime.lock').read_text()))
    if config.bundle.exists():
        raise ValueError('Use a new Linux bundle directory')
    operation = config.bundle.parent/(config.bundle.name+'-initialize')
    operation.mkdir(parents=True, exist_ok=True)
    cleanup_run(config, operation)
    from .host import new_backend
    try:
        with new_backend(config, operation, 'runtime-probe') as backend:
            config.bundle.mkdir(parents=True)
            for name in ('plan.json', 'source.tar.gz'):
                shutil.copyfile(source/name, config.bundle/name)
            shutil.copytree(source/'gold', config.bundle/'gold')
            from pilot.build import dump, seal
            dump(config.bundle/'runtime.lock', backend.runtime_info)
            seal(config.bundle)
            provenance = {'source_manifest_sha256': hashlib.sha256((source/'bundle_manifest.json').read_bytes()).hexdigest(),
                          'image': config.image, 'status': 'runtime_pinned_not_yet_validated'}
            dump(config.bundle/'migration.json', provenance)
    finally:
        cleanup_run(config, operation)
    return provenance


def controller_payload(config):
    """Explicit private deployment payload; excludes catalog, history, credentials."""
    environment = modules(config)
    environment.load_bundle(config.bundle, runtime_info=json.loads((config.bundle/'runtime.lock').read_text()))
    data = io.BytesIO()
    entries = []
    for relative in ('environments/click_pilot/click_pilot', 'code_testing_eval/pilot'):
        for path in (config.root/relative).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                entries.append((path, str(path.relative_to(config.root))))
    remote_bundle = 'code_testing_eval/private_grading/pilots/click-pilot-linux-01'
    for name in ('plan.json', 'runtime.lock', 'source.tar.gz', 'bundle_manifest.json'):
        entries.append((config.bundle/name, remote_bundle+'/'+name))
    for path in (config.bundle/'gold').glob('*.py'):
        entries.append((path, remote_bundle+'/gold/'+path.name))
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        for path, name in entries:
            if path.is_symlink():
                raise ValueError('Controller payload contains a symlink')
            archive.add(path, arcname=name, recursive=False)
    return data.getvalue()


def deploy_controller(config, output):
    """Run the scripted framework audit on a separate Prime controller VM."""
    from prime_sandboxes import CreateSandboxRequest
    migration = json.loads((config.bundle/'migration.json').read_text())
    if not all(migration.get(name, {}).get('passed') for name in ('linux_audit', 'framework_audit')):
        raise ValueError('Complete the Linux and cloud framework audits before controller deployment')
    if output.exists():
        raise ValueError('Use a new controller evidence directory')
    output.mkdir(parents=True)
    client_api = APIClient()
    client = SandboxClient(client_api)
    # The API key is supplied only to the trusted controller VM. Agent and
    # grading VMs receive no secrets and run submitted code in child containers.
    sandbox = client.create(CreateSandboxRequest(name='click-pilot-controller-'+uuid.uuid4().hex[:8],
        docker_image=config.image, cpu_cores=1, memory_gb=2, disk_size_gb=10,
        timeout_minutes=60, idle_timeout_minutes=15,
        labels=['click-pilot', 'migration', 'controller'],
        secrets={'PRIME_API_KEY': client_api.api_key},
        environment_vars={'PRIME_API_BASE_URL': client_api.base_url,
                          'PRIME_TEAM_ID': client_api.config.team_id or ''}))
    save(output/'controller.json', {'sandbox_id': sandbox.id, 'deleted': False, 'status': 'starting'})
    remote_bundle = '/app/code_testing_eval/private_grading/pilots/click-pilot-linux-01'
    try:
        if not sandbox.vm:
            raise RuntimeError('Controller must run in a VM')
        client.wait_for_creation(sandbox.id)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'controller.tar.gz'
            path.write_bytes(controller_payload(config))
            client.upload_file(sandbox.id, '/tmp/controller.tar.gz', str(path))
        setup = client.execute_command(sandbox.id,
            "mkdir -p /app && tar -xzf /tmp/controller.tar.gz -C /app && rm /tmp/controller.tar.gz", timeout=60)
        if setup.exit_code:
            raise RuntimeError('Controller package extraction failed')
        command = shlex.join(['/usr/local/bin/python3', '-m', 'click_pilot.validation',
            '--backend', 'prime', '--root', '/app', '--bundle', remote_bundle,
            '--image', config.image, '--output', remote_bundle+'/evidence/hosted-framework-audit'])
        command = 'PYTHONPATH=/app/environments/click_pilot ' + command
        result = client.execute_command(sandbox.id, command, working_dir='/app', timeout=2700)
        (output/'stdout.txt').write_text(result.stdout)
        (output/'stderr.txt').write_text(result.stderr)
        # Return evidence even for a failed audit. Credentials are not included.
        archive_code = """from pathlib import Path
import tarfile
root=Path(%r)
with tarfile.open('/tmp/controller-evidence.tar.gz','w:gz') as archive:
 for directory in ('evidence','verifiers_runs','review_queue'):
  for path in (root/directory).rglob('*.json'):
   if 'workspace' not in path.parts:archive.add(path,arcname=str(path.relative_to(root)),recursive=False)
""" % remote_bundle
        pack = client.execute_command(sandbox.id, shlex.join(['/usr/local/bin/python3', '-c', archive_code]), timeout=120)
        if pack.exit_code == 0:
            client.download_file(sandbox.id, '/tmp/controller-evidence.tar.gz', str(output/'evidence.tar.gz'))
        state = {'sandbox_id': sandbox.id, 'deleted': False, 'status': 'passed' if result.exit_code == 0 else 'failed',
                 'scope': 'Controller and execution run on Prime; scripted model, no external inference.'}
        save(output/'controller.json', state)
        if result.exit_code:
            raise RuntimeError('Hosted controller audit failed; inspect its private logs')
        return state
    finally:
        # Recover nested VMs even when the remote controller fails before scoring.
        cleanup_code = """from pathlib import Path
from click_pilot.cloud.host import cleanup_run
from click_pilot.config import HostConfig
c=HostConfig(root=Path('/app'),bundle=Path(%r),backend='prime',image=%r)
for p in (c.bundle/'verifiers_runs').glob('*'):cleanup_run(c,p)
""" % (remote_bundle, config.image)
        try:
            result = client.execute_command(sandbox.id,
                'PYTHONPATH=/app/environments/click_pilot ' + shlex.join(['/usr/local/bin/python3', '-c', cleanup_code]), timeout=120)
            if result.exit_code:
                (output/'cleanup-error.txt').write_text(result.stderr)
        finally:
            client.delete(sandbox.id)
            state = json.loads((output/'controller.json').read_text())
            state['deleted'] = True
            save(output/'controller.json', state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'prepare', 'build', 'status', 'initialize', 'audit', 'framework-audit', 'controller', 'cleanup'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--bundle', type=Path, default=ROOT/'code_testing_eval/private_grading/pilots/click-pilot-linux-01')
    parser.add_argument('--source', type=Path, default=ROOT/'code_testing_eval/private_grading/pilots/click-pilot-01')
    parser.add_argument('--context', type=Path, default=ROOT/'code_testing_eval/.cache/prime-migration/runtime-current')
    parser.add_argument('--record', type=Path, default=ROOT/'code_testing_eval/private_grading/pilots/prime-migration-01/image-build.json')
    parser.add_argument('--image')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    image = args.image
    if not image and args.record.exists():
        image = json.loads(args.record.read_text())['image']
    config = HostConfig(root=args.root.resolve(), bundle=args.bundle.resolve(), backend='prime', image=image)
    try:
        if args.action == 'preflight':
            SandboxClient(APIClient()).list(per_page=1)
            ImageClient().list(limit=1)
            result = {'authorized': True}
        elif args.action == 'prepare':
            result = prepare(config.root, args.context)
        elif args.action == 'build':
            result = build(args.context, args.record)
        elif args.action == 'status':
            state = json.loads(args.record.read_text())
            status = ImageClient().get_build_status(state['build_id'])
            # Retain the API response privately; never print signed upload URLs.
            save(args.record.with_name('build-status.json'), status)
            result = {'build_id': state['build_id'], 'image': state['image'],
                      'status': status.get('status', status.get('build', {}).get('status', 'inspect build-status.json'))}
        elif args.action == 'initialize':
            result = initialize(config, args.source.resolve())
        elif args.action == 'cleanup':
            for run_dir in (config.bundle/'verifiers_runs').glob('*'):
                cleanup_run(config, run_dir)
            result = {'cleanup': 'complete'}
        else:
            if args.output is None:
                parser.error('--output is required for audit, framework-audit, and controller')
            if not image:
                parser.error('A successfully built --image is required')
            if args.action == 'audit':
                modules(config)
                from pilot.verify import verify_pilot
                journal = args.output.with_suffix('.vm.json')
                with PrimeBackend(image, role='audit', on_create=lambda identity: save(journal,
                        {'sandbox_id': identity, 'deleted': False})) as backend:
                    try:
                        result = verify_pilot(config.bundle, args.output, backend=backend)
                    finally:
                        backend.close()
                        save(journal, {'sandbox_id': backend.sandbox_id, 'deleted': True})
            elif args.action == 'framework-audit':
                from ..validation import audit
                result = asyncio.run(audit(PilotConfig(host=config), args.output))
            else:
                result = deploy_controller(config, args.output)
            if args.action in ('audit', 'framework-audit'):
                path = config.bundle/'migration.json'
                migration = json.loads(path.read_text())
                migration['linux_audit' if args.action == 'audit' else 'framework_audit'] = {
                    'passed': bool(result['passed']), 'summary': str((args.output/'summary.json').resolve()),
                    'summary_sha256': hashlib.sha256((args.output/'summary.json').read_bytes()).hexdigest()}
                save(path, migration)
        print(json.dumps(result, indent=2))
    except UnauthorizedError:
        print(json.dumps({'status': 'blocked', 'reason': 'Prime returned 401 Unauthorized; refresh Prime authentication. No fallback was used.'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
