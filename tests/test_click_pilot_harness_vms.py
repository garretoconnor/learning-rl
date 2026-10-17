"""Shared Linux harness launcher, credential boundary, and failure cleanup."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx

from click_pilot.cli_relay_worker import launch_spec
from click_pilot.cli_harness import dsh_patch
from click_pilot import cli_relay_worker
from click_pilot.cloud.backend import PrimeBackend
from click_pilot.harness import ClickPilotHarness, ClickPilotHarnessConfig
from click_pilot.remote_harness import checked_assets, relay_reply, run, vm_launch_payload
from click_pilot.response_gate import restrict_tools


class HarnessVMTests(unittest.TestCase):
    def test_vm_module_paths_are_not_resolved_on_the_controller(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(Path,'resolve',side_effect=AssertionError('Do not resolve VM paths on the host')):
            path=dsh_patch({'dsh_bin':'/tmp/pilot-cli/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js',
                'compact_tokens':32000,'model':'model','mcp_urls':{'workspace':'http://127.0.0.1:8765/mcp'}},
                Path(temporary),'http://127.0.0.1:8765/v1')
            patch_data=json.loads(path.read_text())
            for plugin in patch_data[-1]['insert']:
                self.assertTrue(plugin['name'].startswith('/tmp/pilot-cli/dsh/node_modules/'))

    def test_child_attests_identity_before_exec(self):
        for uid in (65534, 0):
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);(root/'home').mkdir();(root/'work').mkdir()
                (root/'launch.json').write_text(json.dumps({'variant':'minimal','secret':'rollout-only'}))
                with patch.object(cli_relay_worker,'ROOT',root), patch('sys.platform','linux'), \
                     patch('os.getuid',return_value=uid), patch.dict('os.environ',{},clear=True), \
                     patch('os.execvpe') as execute:
                    if uid == 65534:cli_relay_worker.launch()
                    else:
                        with self.assertRaisesRegex(RuntimeError,'boundary check failed'):cli_relay_worker.launch()
                    self.assertEqual(execute.called,uid == 65534)
                attestation=json.loads((root/'home/runtime-boundary.json').read_text())
                self.assertEqual(attestation['unprivileged'],uid == 65534)
                self.assertTrue(attestation['provider_key_absent'])
                self.assertTrue(attestation['cloud_key_absent'])

    def test_all_variants_have_fixed_linux_entrypoints_and_clean_environments(self):
        with patch.dict('os.environ', {'OPENROUTER_API_KEY':'private', 'PRIME_API_KEY':'private'}):
            for variant in ('minimal','codex','dsh'):
                argv, env = launch_spec({'variant':variant,'secret':'rollout-only','prompt':'task'})
                self.assertTrue(argv[0].startswith(('/usr/local/bin/', '/tmp/pilot-cli/')))
                self.assertNotIn('OPENROUTER_API_KEY',env)
                self.assertNotIn('PRIME_API_KEY',env)
                self.assertEqual(env['HARNESS_GATEWAY_KEY'],'rollout-only')
                self.assertEqual(env['HOME'],'/tmp/pilot-cli/home')
                self.assertNotIn('sandbox-exec',' '.join(argv))
        with self.assertRaises(ValueError):launch_spec({'variant':'shell','secret':'x'})

    def test_vm_payload_excludes_controller_paths_keys_and_bank(self):
        config={'variant':'dsh','model':'model','prompt':'task','secret':'rollout-only','trace_id':'a'*32,
                'run_dir':'/private/bank/run','endpoint':'http://controller','api_key':'private',
                'gold':'reference tests','image':'private image','record_dir':'/private/events'}
        value=vm_launch_payload(config)
        self.assertEqual(set(value),{'variant','model','prompt','secret','trace_id'})
        self.assertNotIn('/private',json.dumps(value))
        self.assertNotIn('reference',json.dumps(value))

    def test_minimal_uses_only_bare_sandbox_command_when_explicitly_enabled(self):
        tools=[{'type':'function','name':'command'},{'type':'function','name':'exec_command'}]
        body,_=restrict_tools({'tools':tools},bare_command=True)
        self.assertEqual(body['tools'],[tools[0]])
        self.assertEqual(restrict_tools({'tools':tools})[0]['tools'],[])
        with self.assertRaises(ValueError):
            restrict_tools({'tools':[tools[0],tools[0]]},bare_command=True)

    def test_relay_refuses_unexpected_paths_and_redacts_transport_failure(self):
        client=Mock(); record=Mock()
        with self.assertRaises(ValueError):
            relay_reply({'path':'/private'},client,'http://gate','http://mcp',record)
        client.post.assert_not_called()
        client.post.side_effect=httpx.ConnectError('sensitive transport details')
        value=relay_reply({'id':'a','path':'/mcp','body':'e30=','headers':{}},client,'http://gate','http://mcp',record)
        self.assertEqual(value['status'],502)
        self.assertNotIn('sensitive',json.dumps(value))
        self.assertEqual(client.post.call_args.args[0],'http://mcp')

    def test_changed_linux_asset_is_rejected_before_vm_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);asset=root/'node.tar.xz';asset.write_bytes(b'changed')
            (root/'manifest.json').write_text(json.dumps({'sha256':'bad'}))
            with self.assertRaisesRegex(ValueError,'hash differs'):
                checked_assets({'variant':'dsh','node_linux_archive':asset,'dsh_linux_archive':asset})

    def test_failed_provision_is_deleted_and_both_journals_are_updated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);client=MagicMock()
            client.create.return_value=SimpleNamespace(id='owned-harness-vm',vm=True)
            client.get_network.return_value=SimpleNamespace(applied=True,policy=SimpleNamespace(allowlist=[],denylist=None))
            client.execute_command.return_value=SimpleNamespace(exit_code=0,stderr='',stdout=json.dumps({'python':'3.12.3'}))
            config={'variant':'minimal','record_dir':str(root/'events'),'run_dir':str(root/'run'),
                    'secret':'rollout-only','mcp_urls':{'':'http://127.0.0.1:1/mcp'},
                    'endpoint':'http://127.0.0.1:2/v1','model':'model','image':'prime/test/runtime:hash'}
            def backend(image,**kwargs):return PrimeBackend(image,client=client,**kwargs)
            with patch('click_pilot.remote_harness.PrimeBackend',side_effect=backend), \
                 patch('click_pilot.remote_harness.provision',side_effect=RuntimeError('fixture failure')):
                with self.assertRaisesRegex(RuntimeError,'fixture failure'):run(config)
            client.delete.assert_called_once_with('owned-harness-vm')
            for folder in ('run','events'):
                value=json.loads((root/folder/'harness-vm.json').read_text())
                self.assertTrue(value['deleted'])
                self.assertEqual(value['variant'],'minimal')


class HarnessRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_prime_tasks_route_every_variant_to_shared_vm_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for variant in ('minimal','codex','dsh'):
                harness=ClickPilotHarness(ClickPilotHarnessConfig(id='click-pilot',variant=variant,
                    prime_required=True,prime_image='prime/test/runtime:hash',execution_bundle=root,record_root=root/'events'))
                runtime=SimpleNamespace(write=AsyncMock(),run_program=AsyncMock(return_value='ran'))
                trace=SimpleNamespace(id=variant,state=SimpleNamespace(run_id='a'*32))
                result=await harness.launch(SimpleNamespace(model='model'),trace,runtime,
                    'http://127.0.0.1:1/v1','rollout-only',{'':'http://127.0.0.1:2/mcp'},
                    SimpleNamespace(prompt='task',system_prompt=None))
                self.assertEqual(result,'ran')
                payload=json.loads(runtime.write.call_args.args[1]);self.assertEqual(payload['variant'],variant)
                self.assertIn('click_pilot.cli_harness',runtime.run_program.call_args.args[0])
                self.assertEqual(payload['run_dir'],str(root/'verifiers_runs'/('a'*32)))

    async def test_missing_prime_image_cannot_fall_back_to_local_minimal(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness=ClickPilotHarness(ClickPilotHarnessConfig(id='click-pilot',prime_required=True,record_root=Path(temporary)))
            runtime=SimpleNamespace(write=AsyncMock(),run_program=AsyncMock())
            with self.assertRaisesRegex(ValueError,'no local fallback'):
                await harness.launch(SimpleNamespace(model='model'),SimpleNamespace(id='trace',state=SimpleNamespace(run_id='a'*32)),
                    runtime,'endpoint','secret',{'':'mcp'},SimpleNamespace(prompt='task',system_prompt=None))
            runtime.run_program.assert_not_called()
