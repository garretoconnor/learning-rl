"""Generic isolated-VM HTTP/file relay. No provider or cloud credential needed."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path('/tmp/pilot-cli')


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def poll():
    values = []
    for path in (ROOT/'requests').glob('*.json'):
        values.append(json.loads(path.read_text()))
        path.unlink()
    status = json.loads((ROOT/'status.json').read_text()) if (ROOT/'status.json').exists() else {'running': True}
    positions_path = ROOT/'positions.json'
    positions = json.loads(positions_path.read_text()) if positions_path.exists() else {}
    logs = []
    paths = [ROOT/'stdout.jsonl', ROOT/'stderr.txt', *(ROOT/'home/codex/sessions').rglob('*.jsonl')]
    for path in paths:
        if not path.is_file():
            continue
        key = str(path.relative_to(ROOT))
        with path.open() as stream:
            stream.seek(positions.get(key, 0))
            for _ in range(100):
                before = stream.tell()
                line = stream.readline(2*1024*1024)
                if not line or not line.endswith('\n'):
                    stream.seek(before)
                    break
                if key in ('stdout.jsonl', 'stderr.txt') or 'compact' in line.lower():
                    logs.append({'file': key, 'line': line})
            positions[key] = stream.tell()
    save(positions_path, positions)
    print(json.dumps({'requests': values, 'status': status, 'logs': logs}))


def reply(path):
    for item in json.loads(Path(path).read_text()):
        if not item['id'].isalnum():
            raise ValueError('Invalid relay identity')
        save(ROOT/'responses'/(item['id']+'.json'), item)
    Path(path).unlink()


def launch_spec(config):
    """Fixed entry points and an empty credential environment for each variant."""
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(ROOT/'home'),
                   'HARNESS_GATEWAY_KEY': config['secret'], 'LANG': 'C.UTF-8', 'NO_COLOR': '1'}
    variant = config['variant']
    if variant == 'codex':
        environment['CODEX_HOME'] = str(ROOT/'home/codex')
        argv = [str(ROOT/'codex'), 'exec', '--skip-git-repo-check', '--ignore-rules',
                '--json', '--color', 'never', config['prompt']]
    elif variant == 'dsh':
        environment['PATH'] = str(ROOT/'node/bin')+':'+environment['PATH']
        environment['LD_LIBRARY_PATH'] = str(ROOT/'node/lib')
        environment['DSH_HOME'] = str(ROOT/'home/dsh')
        environment['DSH_SYSTEM_PROMPT'] = 'You are a software engineering agent. Use the configured MCP command tool to work in the task workspace.'
        argv = ['/usr/local/bin/python3', '-I', str(ROOT/'dsh_program.py'), str(ROOT/'launch.json')]
    elif variant == 'minimal':
        argv = ['/usr/local/bin/python3', '-I', str(ROOT/'responses_program.py'), str(ROOT/'home/minimal.json')]
    else:
        raise ValueError('Unknown harness variant')
    return argv, environment


def launch():
    """Attest the actual unprivileged child, then replace it with its harness."""
    config = json.loads((ROOT/'launch.json').read_text())
    checks = {'linux': sys.platform == 'linux', 'unprivileged': os.getuid() == 65534,
        'provider_key_absent': 'OPENROUTER_API_KEY' not in os.environ,
        'cloud_key_absent': 'PRIME_API_KEY' not in os.environ,
        'public_workspace_absent': not any((ROOT/'work').iterdir()),
        'host_accounts_absent': not (ROOT/'home/.prime').exists() and not (ROOT/'home/.env').exists()}
    save(ROOT/'home/runtime-boundary.json', checks)
    if not all(checks.values()):
        raise RuntimeError('Harness child boundary check failed')
    argv, environment = launch_spec(config)
    os.execvpe(argv[0], argv, environment)


def serve():
    config = json.loads((ROOT/'launch.json').read_text())
    # PrimeBackend verifies applied deny-all egress before provisioning this VM.
    for name in ['requests', 'responses', 'work', 'home']:
        (ROOT/name).mkdir(exist_ok=True)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_DELETE(self):
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_GET(self):
            if self.path.rstrip('/') == '/v1/models':
                body = json.dumps({'object': 'list', 'data': [{'id': config['model'], 'object': 'model',
                                                              'created': 0, 'owned_by': 'deepseek'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(405)

        def do_POST(self):
            if self.path not in ('/mcp', '/v1/responses'):
                self.send_error(403)
                return
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 16*1024*1024:
                self.send_error(413)
                return
            id = uuid.uuid4().hex
            request = {'id': id, 'path': self.path, 'body': base64.b64encode(self.rfile.read(size)).decode(),
                       'headers': {k: v for k, v in self.headers.items()
                                   if k.lower() in ('accept', 'authorization', 'content-type',
                                                    'mcp-session-id', 'mcp-protocol-version')}}
            save(ROOT/'requests'/(id+'.json'), request)
            path = ROOT/'responses'/(id+'.json')
            deadline = time.monotonic()+2100
            while not path.exists():
                if time.monotonic() > deadline:
                    self.send_error(504)
                    return
                time.sleep(.05)
            response = json.loads(path.read_text())
            path.unlink()
            payload = base64.b64decode(response['body'])
            self.send_response(response['status'])
            for key, value in response['headers'].items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    argv, environment = launch_spec(config)
    for root in [ROOT/'home', ROOT/'work']:
        for path in [root, *root.rglob('*')]:
            os.chown(path, 65534, 65534)
        root.chmod(0o700)
    (ROOT/'launch.json').chmod(0o600)
    os.chown(ROOT/'launch.json', 65534, 65534)
    began = time.monotonic()
    with (ROOT/'stdout.jsonl').open('wb') as out, (ROOT/'stderr.txt').open('wb') as err:
        process = subprocess.Popen(['/usr/local/bin/python3', '-I', str(ROOT/'relay.py'), 'launch'],
            cwd=ROOT/'work', env=environment, user=65534, group=65534, extra_groups=[],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        save(ROOT/'ready.json', {'ready': True, 'pid': process.pid, 'variant': config['variant']})
        code = process.wait()
    save(ROOT/'status.json', {'running': False, 'exit_code': code, 'variant': config['variant'],
                             'harness_seconds': time.monotonic()-began})
    # Let the controller retrieve final status and logs before deleting this VM.
    while True:
        time.sleep(30)


if __name__ == '__main__':
    if sys.argv[1] == 'poll': poll()
    elif sys.argv[1] == 'reply': reply(sys.argv[2])
    elif sys.argv[1] == 'launch': launch()
    else: serve()
