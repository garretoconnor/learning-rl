"""Controller-only Responses gateway: expose only the sandbox's command tool."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import httpx

COMMAND = 'mcp__workspace__command'


def events(response):
    """Serialize a completed provider response as standard Responses SSE events."""
    sequence = 0
    def event(kind, **fields):
        nonlocal sequence
        result = {'type': kind, 'sequence_number': sequence, **fields}
        sequence += 1
        return ('event: '+kind+'\ndata: '+json.dumps(result)+'\n\n').encode()
    yield event('response.created', response={**response, 'status': 'in_progress', 'output': []})
    for index, item in enumerate(response.get('output', [])):
        initial = dict(item)
        if item['type'] == 'function_call':
            initial['arguments'] = ''
        elif item['type'] == 'message':
            initial['content'] = []
        yield event('response.output_item.added', output_index=index, item=initial)
        if item['type'] == 'function_call':
            yield event('response.function_call_arguments.delta', item_id=item['id'], output_index=index,
                        delta=item['arguments'])
            yield event('response.function_call_arguments.done', item_id=item['id'], output_index=index,
                        arguments=item['arguments'])
        elif item['type'] == 'message':
            for ci, content in enumerate(item.get('content', [])):
                if content['type'] != 'output_text':
                    continue
                fields = {'item_id': item['id'], 'output_index': index, 'content_index': ci}
                yield event('response.content_part.added', **fields, part={**content, 'text': ''})
                yield event('response.output_text.delta', **fields, delta=content['text'])
                yield event('response.output_text.done', **fields, text=content['text'])
                yield event('response.content_part.done', **fields, part=content)
        yield event('response.output_item.done', output_index=index, item=item)
    yield event('response.completed', response=response)


def restrict_tools(body, *, bare_command=False):
    tools = body.get('tools') or []
    # Never advertise host shell, file tools, hosted tools, tool discovery, or code execution.
    kept = []
    for tool in tools:
        if tool.get('type') == 'function' and tool.get('name') in ({COMMAND, 'command'} if bare_command else {COMMAND}):
            kept.append(tool)
        elif tool.get('type') == 'namespace' and tool.get('name') == 'mcp__workspace':
            nested = [t for t in tool.get('tools', []) if t.get('type') == 'function' and t.get('name') == 'command']
            if len(nested) != 1:
                raise ValueError('MCP namespace must contain exactly one command')
            kept.append({**tool, 'tools': nested})
    if len(kept) > 1:
        raise ValueError('Duplicate sandbox command tool')
    body = {**body, 'tools': kept, 'store': False, 'stream': False}
    choice = body.get('tool_choice')
    if isinstance(choice, dict) and choice.get('name') not in (COMMAND, 'command'):
        body['tool_choice'] = 'auto'
    return body, [tool.get('name', tool.get('type')) for tool in tools if tool not in kept]


class ResponseGate:
    def __init__(self, endpoint, secret, model, record, *, bare_command=False):
        self.endpoint, self.secret, self.model = endpoint.rstrip('/'), secret, model
        self.record = record
        self.client = httpx.Client(timeout=httpx.Timeout(None, connect=5), trust_env=False)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.rstrip('/') not in ('/models', '/v1/models'):
                    self.send_error(404)
                    return
                self.reply(200, {'object': 'list', 'data': [{'id': owner.model, 'object': 'model',
                                                           'created': 0, 'owned_by': 'deepseek'}]})

            def reply(self, status, value):
                payload = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer '+owner.secret:
                    self.reply(401, {'error': {'message': 'Unauthorized rollout'}})
                    return
                if self.path.rstrip('/') not in ('/responses', '/v1/responses'):
                    self.reply(403, {'error': {'message': 'Only Responses is enabled'}})
                    return
                try:
                    size = int(self.headers.get('Content-Length', 0))
                    if not 0 < size <= 16 * 1024 * 1024:
                        raise ValueError('Request size outside bound')
                    body = json.loads(self.rfile.read(size))
                    streamed = body.get('stream', False)
                    mediated, removed = restrict_tools(body, bare_command=bare_command)
                    owner.record('request', {'removed_tools': removed,
                                             'allowed_tools': [t['name'] for t in mediated['tools']],
                                             'stream_buffered': bool(streamed)})
                    response = owner.client.post(owner.endpoint+'/responses', json=mediated,
                        headers={'Authorization': 'Bearer '+owner.secret})
                    data = response.json()
                    if response.is_error:
                        self.reply(response.status_code, data)
                        return
                    for item in data.get('output', []):
                        if item['type'] in ('function_call', 'custom_tool_call'):
                            permitted = ((item.get('name') == COMMAND and not item.get('namespace')) or
                                (bare_command and item.get('name') == 'command' and not item.get('namespace')) or
                                (item.get('name') == 'command' and item.get('namespace') == 'mcp__workspace'))
                            if not permitted:
                                raise ValueError('Provider attempted an unapproved tool')
                        if item['type'] not in ('message', 'reasoning', 'function_call'):
                            raise ValueError('Provider attempted an unapproved output type')
                    if streamed:
                        payload = b''.join(events(data))
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.send_header('Content-Length', str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    else:
                        self.reply(200, data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as error:
                    owner.record('gateway_error', {'error_type': type(error).__name__})
                    self.reply(502, {'error': {'message': 'Controller Responses gateway failed'}})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f'http://127.0.0.1:{self.server.server_port}/v1'

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.client.close()
