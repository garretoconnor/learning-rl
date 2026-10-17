"""Build a minimal explicit image context; never upload the repository wholesale."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

import httpx
from prime_sandboxes import BuildImageRequest, ImageClient, ImageVisibility

BASE = '3.12.3-slim-bookworm'
RUNTIME_PINS = {'pytest': '8.4.1', 'pluggy': '1.6.0', 'packaging': '25.0',
                'iniconfig': '2.1.0', 'pygments': '2.19.2'}


def base_digest():
    with httpx.Client(timeout=30) as client:
        response = client.get('https://auth.docker.io/token', params={
            'service': 'registry.docker.io', 'scope': 'repository:library/python:pull'})
        response.raise_for_status()
        token = response.json()['token']
        response = client.get('https://registry-1.docker.io/v2/library/python/manifests/' + BASE,
            headers={'Authorization': 'Bearer ' + token,
                     'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'})
        response.raise_for_status()
        manifest = next(m for m in response.json()['manifests']
                        if m.get('platform', {}).get('os') == 'linux' and m['platform'].get('architecture') == 'amd64')
        return 'python@' + manifest['digest']


def runtime_requirements():
    lines = []
    with httpx.Client(timeout=30) as client:
        for name, version in RUNTIME_PINS.items():
            response = client.get(f'https://pypi.org/pypi/{name}/{version}/json')
            response.raise_for_status()
            hashes = sorted({file['digests']['sha256'] for file in response.json()['urls']})
            lines.append(f'{name}=={version} ' + ' '.join('--hash=sha256:' + h for h in hashes))
    return '\n'.join(lines) + '\n'


def prepare(root, output):
    if output.exists():
        raise ValueError('Use a new image context directory')
    output.mkdir(parents=True)
    base = base_digest()
    framework = subprocess.check_output(['uv', 'export', '--frozen', '--no-dev', '--no-emit-workspace',
                                          '--format', 'requirements-txt'], cwd=root, text=True)
    # A local path must never enter the cloud dependency build.
    if any(line.startswith(('-e ', './', 'file:')) for line in framework.splitlines()):
        raise ValueError('Controller dependency export contains local packages')
    (output/'requirements-controller.txt').write_text(framework)
    (output/'requirements-runtime.txt').write_text(runtime_requirements())
    here = Path(__file__).parent
    (output/'supervisor.py').write_bytes((here/'supervisor.py').read_bytes())
    (output/'worker.py').write_bytes((root/'code_testing_eval/pilot/worker.py').read_bytes())
    dockerfile = (here/'Dockerfile').read_text().replace('__PYTHON_BASE__', base)
    (output/'Dockerfile').write_text(dockerfile)
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir())}
    context_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    metadata = {'python_base': base, 'files': manifest, 'context_sha256': context_hash,
                'visibility': 'private', 'platform': 'linux/amd64', 'status': 'prepared_not_built'}
    (output/'context.json').write_text(json.dumps(metadata, indent=2) + '\n')
    return metadata


def context_tar(context):
    manifest = json.loads((context/'context.json').read_text())
    expected = set(manifest['files']) | {'context.json'}
    if {p.name for p in context.iterdir()} != expected:
        raise ValueError('Unexpected file in public runtime build context')
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        for name, digest in sorted(manifest['files'].items()):
            path = context/name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Image context integrity mismatch')
            archive.add(path, arcname=name, recursive=False)
    return manifest, data.getvalue()


def build(context, record):
    if record.exists():
        raise ValueError('Build record already exists; inspect it instead of creating a duplicate')
    manifest, archive = context_tar(context)
    client = ImageClient()
    response = client.initiate_build(BuildImageRequest(image_name='click-pilot-runtime',
        image_tag=manifest['context_sha256'][:16], visibility=ImageVisibility.PRIVATE))
    # Record identity before upload/start, so a lost connection is recoverable.
    state = {'build_id': response.build_id, 'image': response.full_image_path,
             'context_sha256': manifest['context_sha256'], 'status': 'upload_pending'}
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(state, indent=2) + '\n')
    try:
        upload = httpx.put(response.upload_url, content=archive, headers={'Content-Type': 'application/octet-stream'}, timeout=120)
    except httpx.HTTPError as error:
        raise RuntimeError(f'Image context upload transport failed ({type(error).__name__}); build identity is recorded') from None
    if upload.is_error:
        # Signed upload URLs are temporary credentials; omit them from errors.
        raise RuntimeError(f'Image context upload failed with HTTP {upload.status_code}; build identity is recorded')
    client.start_build(response.build_id)
    state['status'] = 'building'
    record.write_text(json.dumps(state, indent=2) + '\n')
    return state
