"""Responses protocol and CLI execution-boundary regression checks."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import OpenAI
from pydantic import ValidationError

from click_pilot.harness import ClickPilotHarness, ClickPilotHarnessConfig
from click_pilot.responses_program import main as run_responses
from click_pilot.validation import scripted_model

from click_pilot.response_gate import COMMAND, ResponseGate, events, restrict_tools
from click_pilot.cli_harness import codex_config, dsh_patch


def response(output):
    return {'id': 'resp_test', 'object': 'response', 'created_at': 0,
            'model': 'test-model', 'status': 'completed', 'output': output,
            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}


class ResponsesBoundaryTests(unittest.TestCase):
    def test_chat_protocol_is_rejected_for_every_variant(self):
        for variant in ('minimal', 'codex', 'dsh'):
            with self.subTest(variant=variant), self.assertRaises(ValidationError):
                ClickPilotHarnessConfig(id='click-pilot', variant=variant, protocol='chat')

    def test_codex_approves_only_the_shared_mcp_command(self):
        import tomllib
        with tempfile.TemporaryDirectory() as temporary:
            home = codex_config({'model': 'deepseek/test', 'compact_tokens': 32000,
                'mcp_urls': {'workspace': 'http://127.0.0.1:1234/mcp'}},
                Path(temporary), 'http://127.0.0.1:5678/v1')
            config = tomllib.loads((home/'config.toml').read_text())
        self.assertEqual(config['model_providers']['pilot']['wire_api'], 'responses')
        self.assertEqual(config['mcp_servers']['workspace']['enabled_tools'], ['command'])
        self.assertEqual(config['mcp_servers']['workspace']['tools']['command']['approval_mode'], 'approve')
        self.assertFalse(config['features']['shell_tool'])
        self.assertEqual(config['model_auto_compact_token_limit'], 32000)

    def test_only_the_prime_command_is_advertised(self):
        safe = {'type': 'function', 'name': COMMAND, 'parameters': {'type': 'object'}}
        body, removed = restrict_tools({'tools': [safe, {'type': 'function', 'name': 'exec_command'},
                                                {'type': 'web_search'}, {'type': 'custom', 'name': 'apply_patch'}],
                                       'store': True, 'stream': True})
        self.assertEqual(body['tools'], [safe])
        self.assertEqual(removed, ['exec_command', 'web_search', 'apply_patch'])
        self.assertFalse(body['store'])
        self.assertFalse(body['stream'])

    def test_provider_cannot_reintroduce_a_native_execution_tool(self):
        records = []
        gate = ResponseGate('http://127.0.0.1:1/v1', 'rollout-secret', 'test-model',
                            lambda *value: records.append(value))
        gate.client.close()
        gate.client = Mock()
        gate.client.post.return_value = SimpleNamespace(is_error=False,
            json=lambda: response([{'type': 'function_call', 'name': 'exec_command',
                                    'arguments': '{}', 'id': 'fc_test', 'call_id': 'call_test'}]))
        with gate as endpoint:
            with httpx.Client(trust_env=False) as client:
                result = client.post(endpoint+'/responses', headers={'Authorization': 'Bearer rollout-secret'},
                                     json={'input': 'test', 'tools': []})
        self.assertEqual(result.status_code, 502)
        self.assertEqual(records[-1][0], 'gateway_error')

    def test_codex_namespace_keeps_only_the_sandbox_command(self):
        body, removed = restrict_tools({'tools': [{'type': 'namespace', 'name': 'mcp__workspace',
            'tools': [{'type': 'function', 'name': 'command'}, {'type': 'function', 'name': 'read_host_file'}]}]})
        self.assertEqual(body['tools'][0]['tools'], [{'type': 'function', 'name': 'command'}])

    def test_gateway_rejects_other_endpoints_and_wrong_rollout_secret(self):
        gate = ResponseGate('http://127.0.0.1:1/v1', 'secret', 'model', lambda *value: None)
        with gate as endpoint:
            with httpx.Client(trust_env=False) as client:
                self.assertEqual(client.post(endpoint+'/responses', json={}).status_code, 401)
                self.assertEqual(client.post(endpoint+'/chat/completions', json={},
                    headers={'Authorization': 'Bearer secret'}).status_code, 403)

    def test_buffered_sse_preserves_function_arguments_for_the_sdk(self):
        native = response([{'id': 'fc_test', 'type': 'function_call', 'call_id': 'call_test',
                            'name': COMMAND, 'arguments': '{"command":"pwd"}', 'status': 'completed'}])
        payload = b''.join(events(native))
        transport = httpx.MockTransport(lambda request: httpx.Response(200,
            headers={'Content-Type': 'text/event-stream'}, content=payload))
        with OpenAI(api_key='test', base_url='http://localhost/v1',
                    http_client=httpx.Client(transport=transport)) as client:
            stream = client.responses.create(model='test-model', input='test', stream=True)
            received = list(stream)
        self.assertEqual(received[-1].response.output[0].arguments, '{"command":"pwd"}')
        self.assertEqual(received[-1].type, 'response.completed')

    def test_dsh_composition_disables_host_tools_and_uses_responses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # The path shape matches the installed, pinned CLI layout.
            binary = root/'node_modules/@deepseek-ai/dsh/lib/bin.js'
            binary.parent.mkdir(parents=True)
            binary.touch()
            patch = json.loads(dsh_patch({'dsh_bin': str(binary), 'compact_tokens': 32000,
                'model': 'deepseek/test', 'mcp_urls': {'': 'http://127.0.0.1:1234/mcp'}},
                root, 'http://127.0.0.1:5678/v1').read_text())
            disabled = {row['id'] for row in patch if row.get('disabled')}
            self.assertTrue({'tool-fs', 'persistent-bash', 'persistent-pwsh', 'tool-working-directory'} <= disabled)
            provider = patch[-1]['insert'][0]['config']['providers']['pilot']
            self.assertEqual(provider['api'], 'openai-responses')
            self.assertEqual(provider['apiKeyEnv'], 'HARNESS_GATEWAY_KEY')


class ResponsesCutoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_scripted_server_rejects_chat_without_recording_a_request(self):
        async with scripted_model([]) as (endpoint, requests):
            async with httpx.AsyncClient(trust_env=False) as client:
                result = await client.post(endpoint+'/chat/completions', json={'messages': []})
                self.assertEqual(result.status_code, 404)
                self.assertEqual(requests, [])
                result = await client.post(endpoint+'/responses', json={'input': []})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()['object'], 'response')
                self.assertEqual(len(requests), 1)

    async def test_minimal_sdk_loop_replays_responses_tool_calls_and_outputs(self):
        async with scripted_model(['pwd']) as (endpoint, requests):
            with tempfile.TemporaryDirectory() as temporary:
                payload = Path(temporary)/'launch.json'
                payload.write_text(json.dumps({'endpoint': endpoint, 'secret': 'test-only',
                    'model': 'scripted-pilot-check', 'system': 'Use command.', 'prompt': 'Inspect the task.',
                    'mcp_urls': {'workspace': 'http://unused'}, 'tool_timeout': 10}))
                with patch('click_pilot.responses_program.connect_mcp', new_callable=AsyncMock,
                           return_value=([{'type': 'function', 'function': {'name': 'command',
                               'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}}}}],
                               {'command': 'workspace'}, {})), \
                     patch('click_pilot.responses_program.call_mcp', new_callable=AsyncMock,
                           return_value='tool result') as command:
                    await run_responses(payload)
                    command.assert_awaited_once_with({}, {'command': 'workspace'}, 'command', {'command': 'pwd'})
                self.assertFalse(payload.exists())
            self.assertEqual(len(requests), 2)
            self.assertTrue(all('messages' not in request for request in requests))
            self.assertEqual(requests[0]['instructions'], 'Use command.')
            self.assertFalse(requests[0]['store'])
            self.assertEqual(requests[1]['input'][-1],
                {'type': 'function_call_output', 'call_id': 'call_0', 'output': 'tool result'})
            self.assertEqual(requests[1]['input'][-2]['type'], 'function_call')

    async def test_local_minimal_launcher_only_uses_responses(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = ClickPilotHarness(ClickPilotHarnessConfig(id='click-pilot', record_root=Path(temporary)))
            runtime = SimpleNamespace(write=AsyncMock(), run_program=AsyncMock(return_value='ran'))
            trace = SimpleNamespace(id='trace', state=SimpleNamespace(run_id='a'*32))
            await harness.launch(SimpleNamespace(model='model'), trace, runtime, 'endpoint', 'secret', {},
                                 SimpleNamespace(prompt='task', system_prompt='system'))
            self.assertEqual(runtime.run_program.call_args.args[0][2], 'click_pilot.responses_program')
            payload = json.loads(runtime.write.call_args.args[1])
            self.assertEqual(payload['prompt'], 'task')
            self.assertEqual(payload['system'], 'system')


if __name__ == '__main__':
    unittest.main()
