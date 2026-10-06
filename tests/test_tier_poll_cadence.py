import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from lib import spot_runner_autoscaler as controller
from lib.fleet_evidence import record_resources
from lib.runner_fleet import GceVm
from lib.tier_policy import fits


class PollFinished(Exception):
    pass


class TierPollCadenceTests(unittest.TestCase):
    def test_missing_stale_or_conflicting_samples_keep_full_wait(self):
        from lib.poll_cadence import tier_poll_delay
        for rows in (None, [], [{'observed_at': 999}], [{'observed_at': 1101}],
                     [{'observed_at': True}], [{'observed_at': float('nan')}],
                     [{'observed_at': 1000}, {'observed_at': 1001}]):
            with self.subTest(rows=rows):
                self.assertEqual(tier_poll_delay({'latest_resources': rows}, 300, 1000, 1100), 300)
        self.assertEqual(tier_poll_delay({'latest_resources': [{'observed_at': 1000}]},
                                        300, 1000, 999), 300)

    def test_long_health_check_never_produces_negative_wait(self):
        from lib.poll_cadence import tier_poll_delay
        self.assertEqual(tier_poll_delay({'latest_resources': [{'observed_at': 1000}]},
                                        300, 1000, 1400), 0)

    def test_real_controller_collects_six_distinct_windows_despite_poll_cost(self):
        clock = [1000.0]
        samples, delays = [], []
        host = GceVm('oxidex-runners-a', 'z', 'RUNNING', 'n4-standard-32',
                     True, 32, 128, instance_id='1')

        def inventory(*_):
            clock[0] += 40
            return []

        def check(vms, runners, args, state, path):
            samples.append(clock[0])
            # Preserve a real demand spike while collecting the fit history.
            fraction = .4 if len(samples) == 1 else .1
            state['latest_resources'] = record_resources(
                state, vms, [(fraction, .1)], clock[0])
            clock[0] += 25
            controller.write_scale_state(path, state)

        def sleep(delay):
            delays.append(delay)
            clock[0] += delay
            if len(samples) == 6:
                raise PollFinished

        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder) / 'state.json'
            args = controller.args_parser().parse_args([
                '--scaling-mode', 'tiered', '--state-file', str(path)])
            stack.enter_context(patch.object(controller.time, 'time', side_effect=lambda: clock[0]))
            stack.enter_context(patch.object(controller.time, 'sleep', side_effect=sleep))
            stack.enter_context(patch.object(controller, 'list_github_runners', side_effect=inventory))
            stack.enter_context(patch.object(controller, 'list_gcloud_vms', return_value=[host]))
            stack.enter_context(patch.object(controller, 'JobRecovery'))
            for name, value in [('clear_catalog_cache', None), ('load_cpu_platform_map', {}),
                                ('reconcile_deletion_identities', None), ('observe_vms', None),
                                ('log_hourly_spend', None), ('reconcile_host_operation', False),
                                ('reconcile_lost_vms', False), ('reconcile_prices', False)]:
                stack.enter_context(patch.object(controller, name, return_value=value))
            stack.enter_context(patch('lib.tier_controller.check_tiers', side_effect=check))
            with self.assertRaises(PollFinished):
                controller._run_controller(args)
            state = controller.read_scale_state(path)

        self.assertTrue(all(b-a >= 300 for a, b in zip(samples, samples[1:])))
        self.assertEqual(state['latest_resources'][0]['windows'], 6)
        self.assertEqual(state['latest_resources'][0]['peak_cpu_cores'], 12.8)
        self.assertFalse(fits(('large',), state['latest_resources']))
        self.assertTrue(all(0 < delay <= 300 for delay in delays))


if __name__ == '__main__':
    unittest.main()
