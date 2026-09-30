"""Exercise actual emitted log records with mocked provider responses."""
import asyncio
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib.compute_api import AsyncGoogleApi, Offer, is_quota_error
from lib.fleet_logging import lifecycle_log, observe_vms
from lib.runner_fleet import GceVm
from lib.spot_runner_autoscaler import args_parser, launch_offer, read_scale_state, write_scale_state


class LogLineTests(unittest.TestCase):
    def setUp(self):
        external = patch('subprocess.run', side_effect=AssertionError('Unexpected external command in offline test'))
        external.start()
        self.addCleanup(external.stop)

    def test_multiline_messages_and_tracebacks_are_one_physical_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.log'
            with lifecycle_log(path):
                logger = logging.getLogger('spot_runner_autoscaler')
                logger.warning('provider failure: %s', 'line one\nline two\rline three')
                try:
                    raise RuntimeError('broken\nrequest')
                except RuntimeError:
                    logger.exception('failed')
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 4)
            self.assertIn('line one line two line three', lines[1])
            self.assertIn('RuntimeError', lines[2])

    def test_google_error_response_has_message_and_reason_without_json(self):
        async def request():
            body = json.dumps({'error': {'code': 400, 'message': "The resource 'homelab-424523' is not ready", 'errors': [{'reason': 'resourceNotReady'}]}}, indent=2).encode()
            reader = asyncio.StreamReader()
            reader.feed_data(b'HTTP/1.1 400 Bad Request\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            reader.feed_eof()
            writer = SimpleNamespace(write=lambda data: None, drain=asyncio.sleep, close=lambda: None, is_closing=lambda: True)
            async def drain():
                pass
            writer.drain = drain
            api = AsyncGoogleApi('not-a-real-token', 1)
            api.connection_count = 1
            async def connection():
                return reader, writer
            api._connection = connection
            with self.assertRaises(RuntimeError) as error:
                await api.get_json('/test')
            message = str(error.exception)
            self.assertIn('HTTP 400', message)
            self.assertIn("The resource 'homelab-424523' is not ready", message)
            self.assertIn('resourceNotReady', message)
            self.assertNotIn('{', message)
            self.assertEqual(len(message.splitlines()), 1)
        asyncio.run(request())

    def test_missing_vm_retains_actual_specs_after_state_restart(self):
        vm = GceVm('oxidex-runners-test', 'asia-south2-a', 'RUNNING', 'custom-8-16384', True,
                   vcpu=8, ram_gib=16, disks=({'source': 'https://www.googleapis.com/compute/v1/projects/project/zones/asia-south2-a/disks/root', 'boot': True, 'diskSizeGb': '20', 'diskType': 'pd-ssd', 'autoDelete': True},))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            state = {'idle_loops': 0, 'scale_up_active': False}
            observe_vms([vm], state)
            write_scale_state(path, state)
            state = read_scale_state(path)
            with self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
                observe_vms([], state)
            message = ' '.join(logs.output)
            for detail in ('custom-8-16384', '8 vCPU', '16 GiB RAM', '20 GiB', 'pd-ssd', 'asia-south2-a', 'oxidex-runners-test'):
                self.assertIn(detail, message)
            self.assertIn('pending_vm_recovery', state)
            self.assertEqual(len(state['pending_vm_recovery']), 1)
            self.assertIn('ensuring', message)

    def test_error_summaries_handle_malformed_envelopes_and_keep_quota_detection(self):
        from lib.log_format import error_detail
        for body in ('{bad\njson', '{"error": {"message": "failed", "errors": null}}',
                     '{"error": "failed"}', '{"error": {"message": "failed", "errors": 2}}'):
            self.assertEqual(len(error_detail(body).splitlines()), 1)
        message = error_detail('{"error": {"message": "slow down", "errors": [{"reason": "rateLimitExceeded"}]}}')
        self.assertTrue(is_quota_error(message))
        self.assertNotIn('{', message)

    def test_plain_google_envelope_preserves_http_code_once(self):
        from lib.log_format import error_detail
        payload = '{"error":{"code":400,"message":"bad request","errors":[{"reason":"invalid"}]}}'
        self.assertIn('HTTP 400', error_detail(payload))
        self.assertEqual(error_detail('Google API HTTP 400: ' + payload).count('HTTP 400'), 1)

    def test_cli_exception_boundaries_emit_one_physical_line(self):
        import contextlib
        import runpy
        import subprocess
        from unittest.mock import patch
        for wrapper, module in (('src/spot_runner_autoscaler.py', 'lib.spot_runner_autoscaler.main'),
                                ('src/spot_runner_manager.py', 'lib.runner_manager.main')):
            stderr = io.StringIO()
            root = logging.getLogger()
            previous_handlers = root.handlers[:]
            root.handlers.clear()
            with patch(module, side_effect=subprocess.CalledProcessError(1, ['gcloud'], stderr='denied\nretry later')), \
                    contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as result:
                    runpy.run_path(wrapper, run_name='__main__')
            root.handlers.clear()
            root.handlers.extend(previous_handlers)
            self.assertEqual(result.exception.code, 2)
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)
            self.assertIn('denied retry later', stderr.getvalue())

    def test_adoption_logging_uses_observed_disk_sizes(self):
        from lib.runner_manager import args_parser as manager_args, deployment_details
        args = manager_args().parse_args(['--max-cost', '0.2', '--existing-instance'])
        args.deployment_vm = GceVm('oxidex-runners-adopted', 'us-central1-a', 'RUNNING', 'n2-standard-32', True,
                                   vcpu=32, ram_gib=128, disks=(
                                   {'boot': True, 'diskSizeGb': '80', 'diskType': 'pd-ssd', 'interface': 'NVME'},
                                   {'boot': False, 'diskSizeGb': '200', 'diskType': 'pd-standard', 'interface': 'SCSI'}))
        description = deployment_details(args)
        self.assertIn('80 GiB root disk pd-ssd NVME', description)
        self.assertIn('200 GiB data disk pd-standard SCSI', description)
        self.assertNotIn('40 GiB', description)

    def test_special_hardware_reports_gpu_nvme_and_actual_disk_sizes(self):
        from lib.fleet_logging import vm_details
        vm = GceVm('oxidex-runners-gpu', 'us-central1-a', 'RUNNING', 'a2-highgpu-1g', True,
                   vcpu=12, ram_gib=85,
                   disks=({'type': 'SCRATCH', 'interface': 'NVME', 'diskSizeGb': '375'},),
                   accelerators=({'acceleratorType': 'zones/us-central1-a/acceleratorTypes/nvidia-tesla-a100', 'acceleratorCount': 1},))
        text = vm_details(vm)
        for value in ('12 vCPU', '85 GiB RAM', '375 GiB', 'SCRATCH', 'NVME', '1 GPU', 'nvidia-tesla-a100'):
            self.assertIn(value, text)

    def test_manager_reports_first_online_observation_once(self):
        from lib.runner_manager import args_parser as manager_args, online_runners
        args = manager_args().parse_args(['--max-cost', '0.2', '--instance-name', 'oxidex-runners-test'])
        args.runner_count = 2
        args.deployment_started_at = 10
        payload = json.dumps({'name': f'{args.runner_prefix}-{args.instance_name}-1', 'status': 'online'})
        complete = payload + '\n' + json.dumps({'name': f'{args.runner_prefix}-{args.instance_name}-2', 'status': 'online'})
        with patch('lib.runner_manager.run', side_effect=[payload, complete]), \
                patch('lib.runner_manager.time.monotonic', return_value=35), \
                patch('lib.runner_manager.time.sleep'), \
                self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
            self.assertEqual(len(online_runners(args)), 2)
        registered = [line for line in logs.output if 'first seen online' in line]
        self.assertEqual(len(registered), 1)
        self.assertIn('25.0 seconds', registered[0])
        self.assertIn('swack-tools', registered[0])
        self.assertIn('n2-standard-32', registered[0])
        self.assertIn('40 GiB', registered[0])

    def test_intentional_deletion_survives_restart_without_replacement(self):
        from dataclasses import asdict
        vm = GceVm('oxidex-runners-test', 'asia-south2-a', 'RUNNING', 'n2-standard-32', True)
        key = f'{vm.zone}/{vm.name}'
        state = {'observed_vms': {key: asdict(vm)}, 'intentional_deletions': [key],
                 'job_tracking': {'unrelated': 'preserved'}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            write_scale_state(path, state)
            state = read_scale_state(path)
            observe_vms([], state)
        self.assertEqual(state['pending_vm_recovery'], {})
        self.assertEqual(state['job_tracking'], {'unrelated': 'preserved'})

    def test_partial_inventory_preserves_last_known_specs_for_same_identity(self):
        from dataclasses import asdict, replace
        vm = GceVm('oxidex-runners-test', 'asia-south2-a', 'RUNNING', 'n2-standard-32', True,
                   vcpu=32, ram_gib=128, instance_id='123',
                   disks=({'source': 'disk-source', 'diskId': '456', 'autoDelete': True},))
        key = f'{vm.zone}/{vm.name}'
        state = {'observed_vms': {key: asdict(vm)}}
        partial = replace(vm, vcpu=None, ram_gib=None, disks=({'source': 'disk-source', 'autoDelete': True},))
        observe_vms([partial], state)
        self.assertEqual(state['observed_vms'][key]['vcpu'], 32)
        self.assertEqual(state['observed_vms'][key]['disks'][0]['diskId'], '456')
        observe_vms([replace(partial, instance_id='999')], state)
        self.assertIsNone(state['observed_vms'][key]['vcpu'])
        self.assertNotIn('diskId', state['observed_vms'][key]['disks'][0])

    def test_launch_failover_logs_specs_and_registration_seconds(self):
        args = args_parser().parse_args([])
        offer = Offer('us-west3', 'c2d-standard-32', 32, 128, 0.157184)
        results = [SimpleNamespace(returncode=0, stdout='us-west3-a\nus-west3-b\n'),
                   SimpleNamespace(returncode=2, stdout='', stderr='capacity unavailable\ntry later'),
                   SimpleNamespace(returncode=0, stdout='[]', stderr=''),
                   SimpleNamespace(returncode=0, stdout='manager done\n', stderr='')]
        with tempfile.TemporaryDirectory() as directory, \
                patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[]), \
                patch('lib.spot_runner_autoscaler.list_github_runners', return_value=[]), \
                patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=results), \
                patch('lib.spot_runner_autoscaler.time.monotonic', side_effect=[10, 20, 45]), \
                self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
            args.state_file = Path(directory) / "state.json"
            launch_offer(offer, args)
        failed = next(line for line in logs.output if 'failed; trying another zone' in line)
        for detail in ('c2d-standard-32', '32 vCPU', '128 GiB RAM', '40 GiB', '100 GiB', 'pd-balanced', 'capacity unavailable'):
            self.assertIn(detail, failed)
        registered = next(line for line in logs.output if 'registered' in line)
        self.assertIn('25.0 seconds', registered)
        self.assertIn('swack-tools', registered)


if __name__ == '__main__':
    unittest.main()
