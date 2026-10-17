import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from lib import spot_runner_autoscaler as controller
from lib.runner_fleet import GceVm


class ResourceControllerTests(unittest.TestCase):
    def run_checks(self, observations, expected, pending=False, minimum='1', hosts=2):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            args=controller.args_parser().parse_args(['--once','--state-file',str(Path(directory)/'state.json'),
                                                      '--minimum-runners',minimum])
            vms=[GceVm(f'oxidex-runners-{name}','z','RUNNING','n4-standard-32',True,instance_id=str(index))
                 for index, name in enumerate('abcdef'[:hosts], 1)]
            for name, value in [('clear_catalog_cache',None),('load_cpu_platform_map',{}),
                                ('list_github_runners',[]),('list_gcloud_vms',vms),
                                ('reconcile_deletion_identities',None),('JobRecovery',None),
                                ('observe_vms',None),('log_hourly_spend',None),
                                ('reconcile_host_operation',pending),('reconcile_lost_vms',False),
                                ('reconcile_prices',False)]:
                if name=='JobRecovery':
                    stack.enter_context(patch.object(controller,name))
                else:stack.enter_context(patch.object(controller,name,return_value=value))
            metrics=stack.enter_context(patch.object(controller,'read_utilization',side_effect=observations))
            up=stack.enter_context(patch.object(controller,'scale_up',return_value=1))
            down=stack.enter_context(patch.object(controller,'scale_down',return_value=0))
            stack.enter_context(patch.object(controller,'offer_refresher',return_value=lambda: ['offer']))
            for index in range(len(observations)):
                with patch.object(controller.time,'time',return_value=1000+300*index):
                    controller._run_controller(args)
            self.assertEqual(up.call_count, expected.count('up'))
            self.assertEqual(down.call_count, expected.count('down'))
            if pending:metrics.assert_not_called()
            return controller.read_scale_state(Path(directory)/'state.json')

    def test_cpu_or_memory_requires_two_checks_before_one_launch(self):
        self.run_checks([[ (.8,.2),(.2,.2)],[(.2,.8),(.2,.2)]],['up'])

    def test_two_low_checks_drain_and_missing_check_breaks_streak(self):
        self.run_checks([[ (.7,.7),(.7,.7)]]*2,['down'])
        self.run_checks([[ (.7,.7),(.7,.7)],None,[(.7,.7),(.7,.7)]],[])

    def test_resource_mode_persists_the_minimum_runner_floor(self):
        # Two 8-listener hosts cannot drop to one while --minimum-runners asks for 16.
        state=self.run_checks([[(.1,.1),(.1,.1)]]*2,[],minimum='16')
        self.assertEqual(state['minimum_runners'],16)
        self.assertEqual(state['min_runner_floor'],2)

    def test_resource_mode_default_floor_spreads_across_hosts(self):
        # The default 25-runner floor needs four 8-listener hosts: five may shrink, four may not.
        state=self.run_checks([[(.1,.1)]*5]*2,['down'],minimum='25',hosts=5)
        self.assertEqual(state['min_runner_floor'],4)
        state=self.run_checks([[(.1,.1)]*4]*2,[],minimum='25',hosts=4)
        self.assertEqual(state['min_runner_floor'],4)

    def test_resource_mode_floor_of_one_behaves_as_before(self):
        state=self.run_checks([[(.1,.1),(.1,.1)]]*2,['down'],minimum='1')
        self.assertEqual(state['min_runner_floor'],1)

    def test_pending_operation_blocks_all_resource_decisions(self):
        self.run_checks([[ (.8,.8)]]*2,[],pending=True)

    def test_launch_agent_preserves_builder_opt_out(self):
        import plistlib
        with tempfile.TemporaryDirectory() as directory:
            args = controller.args_parser().parse_args(['--no-remote-builder'])
            with patch.object(Path, 'home', return_value=Path(directory)), patch.object(controller.subprocess, 'run'):
                path = controller.install_launch_agent(args)
            arguments = plistlib.loads(path.read_bytes())['ProgramArguments']
            self.assertIn('--no-remote-builder', arguments)
