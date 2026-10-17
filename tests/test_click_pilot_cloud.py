"""Offline cloud transport, isolation, archive, and credential boundary tests.

These checks do not certify Docker, Linux, or Prime; the live audits do that.
"""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import httpx
from unittest.mock import MagicMock, patch

import verifiers.v1 as vf
from click_pilot.cloud.backend import PrimeBackend, workspace_archive
from click_pilot.cloud.host import modules
from click_pilot.cloud.images import context_tar
from click_pilot.cloud.images import build as build_image
from click_pilot.cloud.migrate import controller_payload
from click_pilot.cloud import supervisor
from click_pilot.config import HostConfig


def fake_client(*, vm=True, denied=True, python='3.12.3'):
    client = MagicMock()
    client.create.return_value = SimpleNamespace(id='test-vm', vm=vm)
    client.get_network.return_value = SimpleNamespace(applied=True, policy=SimpleNamespace(
        allowlist=[] if denied else ['*'], denylist=None))
    client.execute_command.return_value = SimpleNamespace(exit_code=0, stderr='',
        stdout=json.dumps({'python': python, 'platform': 'linux-x86_64', 'executable': '/usr/local/bin/python3.12',
                           'executable_sha256': 'test-hash', 'dependencies': {}}))
    return client


class CloudBoundaryTests(unittest.TestCase):
    def test_relative_bundle_survives_tool_subprocess_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = HostConfig(root=root, bundle=Path('private/bundle'))
            payload = config.model_dump_json()
            script = ('import sys; from click_pilot.config import HostConfig; '
                      'print(HostConfig.model_validate_json(sys.argv[1]).bundle)')
            result = subprocess.run([sys.executable, '-c', script, payload],
                                    cwd='/', capture_output=True, text=True, check=True)
            self.assertEqual(Path(result.stdout.strip()), root.resolve()/'private/bundle')

    def test_vm_request_has_no_secrets_and_blocks_egress_before_bootstrap(self):
        client = fake_client()
        with PrimeBackend('prime/test/runtime:hash', client=client):
            request = client.create.call_args.args[0]
            self.assertEqual(request.network_allowlist, [])
            self.assertIsNone(request.secrets)
            self.assertIsNone(request.environment_vars)
            self.assertEqual(request.gpu_count, 0)
            self.assertGreater(request.timeout_minutes, 0)
            calls = [call[0] for call in client.mock_calls]
            self.assertLess(calls.index('get_network'), calls.index('execute_command'))
        client.delete.assert_called_once_with('test-vm')

    def test_wrong_isolation_or_network_cannot_execute_and_is_deleted(self):
        for client in (fake_client(vm=False), fake_client(denied=False)):
            with self.assertRaises(RuntimeError):
                PrimeBackend('prime/test/runtime:hash', client=client)
            client.execute_command.assert_not_called()
            client.delete.assert_called_once_with('test-vm')

    def test_wrong_python_is_deleted_without_accepting_runtime(self):
        client = fake_client(python='3.13.0')
        with self.assertRaisesRegex(RuntimeError, 'Python'):
            PrimeBackend('prime/test/runtime:hash', client=client)
        client.delete.assert_called_once_with('test-vm')

    def test_container_policy_does_not_expose_the_vm_control_socket(self):
        args = supervisor.docker_args(Path('/trusted/workspace'), ['/bin/bash', '-c', 'ls'], editable=True)
        self.assertEqual(args[args.index('--network')+1], 'none')
        self.assertIn('--read-only', args)
        self.assertEqual(args[args.index('--user')+1], '65534:65534')
        self.assertEqual(args[args.index('--cap-drop')+1], 'ALL')
        self.assertNotIn('--privileged', args)
        self.assertNotIn('docker.sock', ' '.join(args))
        self.assertNotIn('PRIME_API_KEY', ' '.join(args))
        mounts = [args[i+1] for i, v in enumerate(args) if v == '--mount']
        self.assertTrue(any('dst=/workspace,readonly' in value for value in mounts))
        self.assertTrue(any('dst=/workspace/submissions' in value for value in mounts))
        self.assertFalse(any('submissions' in value for value in supervisor.docker_args(Path('/trusted/workspace'), ['true'])))

    def test_workspace_upload_rejects_links_and_drops_scratch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'scratch').mkdir()
            (root/'scratch/private.txt').write_text('do not upload')
            (root/'click.py').write_text('pass')
            with tarfile.open(fileobj=io.BytesIO(workspace_archive(root))) as archive:
                self.assertEqual(archive.getnames(), ['click.py'])
            (root/'link.py').symlink_to(root/'click.py')
            with self.assertRaisesRegex(ValueError, 'regular workspace'):
                workspace_archive(root)

    def test_supervisor_refuses_archive_traversal_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root/'bad.tar'
            with tarfile.open(archive_path, 'w') as archive:
                info = tarfile.TarInfo('../../outside')
                info.size = 4
                archive.addfile(info, io.BytesIO(b'evil'))
            with patch.object(supervisor, 'ROOT', root/'runs'):
                with self.assertRaisesRegex(ValueError, 'traversal'):
                    supervisor.install('a'*32, archive_path)
            self.assertFalse((root/'outside').exists())

    def test_supervisor_collects_only_bounded_regular_test_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root/('a'*32)/'submissions'
            folder.mkdir(parents=True)
            (folder/'test_ok.py').write_text('import unittest\n')
            with patch.object(supervisor, 'ROOT', root):
                self.assertEqual(supervisor.collect('a'*32)['files'], {'test_ok.py': 'import unittest\n'})
                (folder/'test_link.py').symlink_to(folder/'test_ok.py')
                with self.assertRaisesRegex(ValueError, 'regular flat'):
                    supervisor.collect('a'*32)
                (folder/'test_link.py').unlink()
                (folder/'test_big.py').write_bytes(b'x'*32769)
                with self.assertRaisesRegex(ValueError, 'size/count'):
                    supervisor.collect('a'*32)

    def test_image_context_rejects_unlisted_files_before_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'context.json').write_text(json.dumps({'files': {}}))
            (root/'private-fix.patch').write_text('should never upload')
            with self.assertRaisesRegex(ValueError, 'Unexpected file'):
                context_tar(root)

    def test_image_build_saves_recovery_id_without_persisting_upload_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root/'build.json'
            response = SimpleNamespace(build_id='build-1', full_image_path='prime/test/runtime:hash',
                                       upload_url='https://example.invalid/signed-private-upload')
            with (patch('click_pilot.cloud.images.context_tar', return_value=({'context_sha256': 'a'*64}, b'context')),
                patch('click_pilot.cloud.images.ImageClient') as client,
                patch('click_pilot.cloud.images.httpx.put') as upload):
                client.return_value.initiate_build.return_value = response
                upload.return_value.is_error = False
                build_image(root, record)
            saved = record.read_text()
            self.assertIn('build-1', saved)
            self.assertNotIn('signed-private-upload', saved)
            self.assertNotIn('upload_url', saved)
            client.return_value.start_build.assert_called_once_with('build-1')
            self.assertEqual(upload.call_args.kwargs['headers']['Content-Type'], 'application/octet-stream')

    def test_timeout_removes_the_container_and_reaps_the_attached_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root/('a'*32)
            (workspace/'scratch').mkdir(parents=True)
            (workspace/'submissions').mkdir()
            process = MagicMock(returncode=-9)
            process.wait.side_effect = [subprocess.TimeoutExpired('docker', 0.01), -9]
            process.poll.return_value = None
            completed = SimpleNamespace(returncode=0, stderr='')
            with patch.object(supervisor, 'ROOT', root), patch.object(supervisor.subprocess, 'run', return_value=completed) as control, patch.object(supervisor.subprocess, 'Popen', return_value=process):
                result = supervisor.run({'token': 'a'*32, 'command': ['sleep', '60'], 'timeout': 0.01})
            self.assertTrue(result['timed_out'])
            self.assertEqual(control.call_args_list[0].args[0][:2], ['docker', 'create'])
            self.assertEqual(control.call_args_list[-1].args[0][:3], ['docker', 'rm', '-f'])
            process.kill.assert_called_once()
            self.assertEqual(process.wait.call_count, 2)

    def test_upload_transport_failure_keeps_recovery_id_and_redacts_signed_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root/'build.json'
            response = SimpleNamespace(build_id='build-recover', full_image_path='prime/test/runtime:hash',
                                       upload_url='https://example.invalid/private-signed-upload')
            with (patch('click_pilot.cloud.images.context_tar', return_value=({'context_sha256': 'a'*64}, b'context')),
                  patch('click_pilot.cloud.images.ImageClient') as client,
                  patch('click_pilot.cloud.images.httpx.put', side_effect=httpx.ConnectError(response.upload_url))):
                client.return_value.initiate_build.return_value = response
                with self.assertRaises(RuntimeError) as caught:
                    build_image(root, record)
            self.assertNotIn('private-signed-upload', str(caught.exception))
            self.assertEqual(json.loads(record.read_text())['status'], 'upload_pending')
            client.return_value.start_build.assert_not_called()

    @unittest.skipUnless((HostConfig().bundle / 'bundle_manifest.json').is_file(), 'Requires local grading data excluded from the code-only checkout')
    def test_private_controller_payload_excludes_workspaces_catalog_and_credentials(self):
        config = HostConfig()
        with tarfile.open(fileobj=io.BytesIO(controller_payload(config))) as archive:
            names = archive.getnames()
            self.assertTrue(any(n.endswith('/plan.json') for n in names))
            self.assertTrue(any('/gold/' in n for n in names))
            for forbidden in ('verifiers_runs', 'private_grading/bugs', '.git/', '.prime/', '.venv/', 'controller-auth'):
                self.assertFalse(any(forbidden in n for n in names), forbidden)

    @unittest.skipUnless((HostConfig().bundle / 'bundle_manifest.json').is_file(), 'Requires local grading data excluded from the code-only checkout')
    def test_cloud_grading_never_falls_back_to_native_execution(self):
        config = HostConfig()
        environment = modules(config)
        source = config.bundle
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)/'bundle'
            bundle.mkdir()
            for name in ('plan.json', 'source.tar.gz'):
                shutil.copyfile(source/name, bundle/name)
            shutil.copytree(source/'gold', bundle/'gold')
            metadata = {'python': '3.12.3', 'platform': 'linux-x86_64', 'dependencies': {}}
            (bundle/'runtime.lock').write_text(json.dumps(metadata))
            from pilot.build import seal
            seal(bundle)
            folder = Path(temporary)/'submission'
            folder.mkdir()
            (folder/'test_pass.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_pass(self):self.assertTrue(True)\n')
            outcome = {'phase': 'tests', 'tests': [{'id': 'test_pass.T.test_pass', 'status': 'passed', 'click_called': False, 'details': []}]}
            backend = SimpleNamespace(runtime_info=metadata, run=MagicMock(return_value={
                'exit_code': 0, 'timed_out': False, 'stdout': 'PILOT_RESULT='+json.dumps(outcome), 'stderr': ''}))
            with patch.object(environment, 'run', side_effect=AssertionError('native fallback')), patch.object(environment, 'runtime', side_effect=AssertionError('native metadata')):
                result = environment.grade(bundle, folder, bundle/'grading.json', backend=backend)
            self.assertEqual(result['status'], 'graded')
            self.assertEqual(result['score'], 0)
            self.assertEqual(backend.run.call_count, 6)
            backend.runtime_info = {**metadata, 'python': '3.13.0'}
            with self.assertRaisesRegex(ValueError, 'Runtime differs'):
                environment.grade(bundle, folder, bundle/'wrong-runtime.json', backend=backend)


class CloudLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_credential_file_is_private_and_removed_on_stop(self):
        config = vf.resolve_env_config({'taskset': {'id': 'click-pilot', 'task': {'host': {'backend': 'prime'}}},
                                        'agent': {'runtime': {'type': 'subprocess'}}})
        env = vf.load_environment(config)
        with patch('prime_sandboxes.APIClient') as client:
            client.return_value.api_key = 'fake-test-credential'
            await env.start()
        path = env.taskset.config.task.host.auth_file
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.read_text(), 'fake-test-credential')
        self.assertNotIn('fake-test-credential', next(iter(env.taskset)).data.model_dump_json())
        await env.stop()
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
