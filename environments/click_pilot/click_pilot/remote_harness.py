"""All Prime harness loops use dedicated deny-egress VMs and one file relay."""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import time

import httpx

from .cloud.backend import PrimeBackend
from .cloud.host import save
from .response_gate import ResponseGate

ROOT = '/tmp/pilot-cli'
VARIANTS = {'minimal', 'codex', 'dsh'}
PYTHON_PINS = {'verifiers': '0.3.1', 'openai': '3.26.1', 'mcp': '1.30.0',
               'httpx': '0.28.1', 'tenacity': '9.2.1'}

BOUNDARY_PROBE = '''import json,pathlib,time
path=pathlib.Path('/tmp/pilot-cli/home/runtime-boundary.json')
for _ in range(200):
 if path.exists():break
 time.sleep(.05)
print(path.read_text())
'''



def vm_launch_payload(config):
    """Explicit allowlist: no host paths, run identity, credentials, or task bank."""
    if config['variant'] not in VARIANTS:
        raise ValueError('Unknown Prime harness variant')
    return {k: config[k] for k in ('variant', 'model', 'secret', 'prompt', 'trace_id')}


def checked_assets(config):
    variant = config['variant']
    if variant not in VARIANTS:
        raise ValueError('Unknown Prime harness variant')
    names = {'minimal': [], 'codex': ['codex_linux_archive'],
             'dsh': ['node_linux_archive', 'dsh_linux_archive']}[variant]
    paths = {name: Path(config[name]) for name in names}
    if variant == 'dsh':
        paths['node_libatomic'] = Path(config['node_linux_archive']).parent/'libatomic.so.1'
    assets = {}
    for name, path in paths.items():
        if not path.is_file():
            raise ValueError('Pinned Linux harness asset is missing: '+name)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if variant == 'dsh':
            manifest = json.loads((path.parent/('libatomic-manifest.json' if name == 'node_libatomic' else 'manifest.json')).read_text())
            if manifest['sha256'] != digest:
                raise ValueError('Linux harness asset hash differs: '+name)
        assets[name] = {'path': path, 'sha256': digest, 'bytes': path.stat().st_size}
    return assets


def relay_reply(request, relay, endpoint, mcp_url, record):
    destinations = {'/v1/responses': endpoint+'/responses', '/mcp': mcp_url}
    if request['path'] not in destinations:
        raise ValueError('Unexpected VM relay path')
    began = time.monotonic()
    try:
        result = relay.post(destinations[request['path']], content=base64.b64decode(request['body']),
                            headers=request['headers'])
        status, body = result.status_code, result.content
        headers = {k:v for k,v in result.headers.items() if k.lower() in
                   ('content-type', 'mcp-session-id', 'mcp-protocol-version')}
    except httpx.TransportError as error:
        # Give the native client a bounded, secret-free error it can retry.
        status, body = 502, b'{"error":{"message":"Harness relay transport failed"}}'
        headers = {'content-type': 'application/json'}
        record('relay_transport_error', {'error_type': type(error).__name__})
    record('vm_relay', {'path': request['path'], 'status': status,
                        'seconds': time.monotonic()-began})
    return {'id': request['id'], 'status': status, 'body': base64.b64encode(body).decode(), 'headers': headers}


def provision(client, id, command, config, assets, folder):
    """Upload only generic runtimes and this rollout's public input."""
    from .cli_harness import CODEX_VERSION, DSH_VERSION, codex_config, dsh_patch
    here = Path(__file__).parent
    def upload(local, name):
        try:
            client.upload_file(id, ROOT+'/'+name, str(local))
        except Exception as error:
            # SDK exceptions may carry signed upload URLs. Keep them out of traces.
            raise RuntimeError('Harness asset upload failed: '+type(error).__name__) from None
    command('mkdir -p '+ROOT+'/home/codex '+ROOT+'/work')
    variant = config['variant']
    endpoint = 'http://127.0.0.1:8765/v1'
    mcp = 'http://127.0.0.1:8765/mcp'
    remote = {**config, 'mcp_urls': {'workspace': mcp}}
    if variant == 'codex':
        upload(assets['codex_linux_archive']['path'], 'codex.gz')
        command('gzip -d '+ROOT+'/codex.gz && chmod 755 '+ROOT+'/codex', timeout=180)
        if command(ROOT+'/codex --version').strip() != 'codex-cli '+CODEX_VERSION:
            raise ValueError('Linux Codex version mismatch')
        home = codex_config(remote, folder, endpoint)
        upload(home/'config.toml', 'home/codex/config.toml')
    elif variant == 'dsh':
        upload(assets['node_linux_archive']['path'], 'node.tar.xz')
        upload(assets['dsh_linux_archive']['path'], 'dsh.tar.gz')
        # The pinned image has Python's lzma module but no external xz executable.
        extract = "import tarfile,pathlib\nwith tarfile.open('/tmp/pilot-cli/node.tar.xz') as archive: archive.extractall('/tmp/pilot-cli',filter='data')\npathlib.Path('/tmp/pilot-cli/node-v26.0.0-linux-x64').rename('/tmp/pilot-cli/node')"
        command(shlex.join(['/usr/local/bin/python3', '-I', '-c', extract]), timeout=180)
        command('mkdir -p '+ROOT+'/dsh && tar -xzf '+ROOT+'/dsh.tar.gz -C '+ROOT+'/dsh', timeout=180)
        command('mkdir -p '+ROOT+'/node/lib')
        upload(assets['node_libatomic']['path'], 'node/lib/libatomic.so.1')
        if command('LD_LIBRARY_PATH='+ROOT+'/node/lib '+ROOT+'/node/bin/node --version').strip() != 'v26.0.0':
            raise ValueError('Linux Node version mismatch')
        binding = "require('/tmp/pilot-cli/dsh/node_modules/node-addon-require-builtin'); console.log('NATIVE_BINDING_OK')"
        if command('LD_LIBRARY_PATH='+ROOT+'/node/lib '+shlex.join([ROOT+'/node/bin/node', '-e', binding])).strip() != 'NATIVE_BINDING_OK':
            raise ValueError('Linux dsh native binding check failed')
        code = "import json;print(json.load(open('/tmp/pilot-cli/dsh/node_modules/@deepseek-ai/dsh/package.json'))['version'])"
        if command(shlex.join(['/usr/local/bin/python3', '-I', '-c', code])).strip() != DSH_VERSION:
            raise ValueError('Linux dsh version mismatch')
        patch = dsh_patch({**remote, 'dsh_bin': ROOT+'/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js'}, folder, endpoint)
        upload(patch, 'dsh-patch.json')
        upload(here/'dsh_program.py', 'dsh_program.py')
    else:
        code = 'import importlib.metadata,json;print(json.dumps({k:importlib.metadata.version(k) for k in '+repr(list(PYTHON_PINS))+'}))'
        actual = json.loads(command(shlex.join(['/usr/local/bin/python3', '-I', '-c', code])))
        if actual != PYTHON_PINS:
            raise ValueError('Minimal harness dependencies differ from pinned versions')
        minimal = folder/'minimal.json'
        minimal.write_text(json.dumps({'endpoint': endpoint, 'secret': config['secret'], 'model': config['model'],
            'system': config['system'], 'prompt': config['prompt'], 'mcp_urls': {'': mcp},
            'tool_timeout': config['tool_timeout']}))
        upload(minimal, 'home/minimal.json')
        upload(here/'responses_program.py', 'responses_program.py')
    launch = folder/'launch.json'
    launch.write_text(json.dumps(vm_launch_payload(config)))
    upload(launch, 'launch.json')
    upload(here/'cli_relay_worker.py', 'relay.py')
    command('nohup /usr/local/bin/python3 -I '+ROOT+'/relay.py serve >'+ROOT+'/relay.log 2>&1 </dev/null &')


def run(config):
    assets = checked_assets(config)
    variant = config['variant']
    record_dir = Path(config['record_dir'])
    record_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(config['run_dir'])
    secret = config['secret']
    def record(kind, value):
        with (record_dir/'events.jsonl').open('a') as stream:
            if stream.tell() < 32*1024*1024:
                stream.write(json.dumps({'kind': kind, 'value': value}).replace(secret, '<ROLLOUT_SECRET>')+'\n')
    journals = [run_dir/'harness-vm.json', record_dir/'harness-vm.json']
    def created(id):
        for path in journals: save(path, {'sandbox_id': id, 'deleted': False, 'variant': variant})
    record('runtime', {'variant': variant, 'controller': 'dedicated Prime Linux VM; deny-all egress; file-mediated relay',
        'assets': {name:{k:v for k,v in item.items() if k != 'path'} for name,item in assets.items()},
        'python_pins': PYTHON_PINS if variant == 'minimal' else None})
    backend = None
    if len(config['mcp_urls']) != 1:
        raise ValueError('Harness VM requires exactly one isolated workspace MCP endpoint')
    with ResponseGate(config['endpoint'], secret, config['model'], record, bare_command=variant=='minimal') as endpoint:
        setup_began = time.monotonic()
        try:
            with PrimeBackend(config['image'], role='harness', on_create=created) as backend:
                client, id = backend.client, backend.sandbox_id
                def command(text, timeout=60):
                    result = client.execute_command(id, text, timeout=timeout)
                    if result.exit_code:
                        raise RuntimeError('Harness VM command failed: '+result.stderr[-1000:])
                    return result.stdout
                with tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary)
                    provision(client, id, command, config, assets, folder)
                    record('provisioned', {'seconds': time.monotonic()-setup_began})
                    try:
                        boundary = json.loads(command(shlex.join(['/usr/local/bin/python3', '-I', '-c', BOUNDARY_PROBE])))
                    except Exception:
                        record('startup_error', command('tail -c 8000 '+ROOT+'/relay.log'))
                        raise RuntimeError('Harness startup failed; inspect private startup log') from None
                    record('runtime_boundary', boundary)
                    if not all(boundary.values()):
                        raise RuntimeError('Harness VM boundary check failed')
                    with httpx.Client(timeout=httpx.Timeout(None, connect=5), trust_env=False) as relay:
                        while True:
                            response = json.loads(command('/usr/local/bin/python3 -I '+ROOT+'/relay.py poll'))
                            for row in response.get('logs', []):
                                try: value = json.loads(row['line'])
                                except ValueError: value = row['line'].rstrip()
                                record('codex_session' if row['file'].startswith('home/') else
                                       (variant if row['file'] == 'stdout.jsonl' else 'stderr'), value)
                            replies = [relay_reply(request, relay, endpoint, next(iter(config['mcp_urls'].values())), record)
                                       for request in response['requests']]
                            if replies:
                                path = folder/'replies.json'
                                path.write_text(json.dumps(replies))
                                try:
                                    client.upload_file(id, ROOT+'/replies.json', str(path))
                                except Exception as error:
                                    raise RuntimeError('Harness relay upload failed: '+type(error).__name__) from None
                                command('/usr/local/bin/python3 -I '+ROOT+'/relay.py reply '+ROOT+'/replies.json')
                            if not response['status']['running']:
                                break
                            time.sleep(.15)
                    for name in ['stdout.jsonl', 'stderr.txt']:
                        text = command('tail -c 16777216 '+ROOT+'/'+name)
                        (record_dir/name).write_text(text.replace(secret, '<ROLLOUT_SECRET>'))
                    record('completed', response['status'])
                    if response['status']['exit_code']:
                        raise RuntimeError('Linux harness failed; inspect private harness logs')
        finally:
            if backend is not None and backend.closed:
                for path in journals:
                    if path.exists():
                        state = json.loads(path.read_text());state['deleted'] = True;save(path, state)
