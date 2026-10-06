import unittest
from unittest.mock import patch

from lib import spot_runner_autoscaler as controller
from lib.runner_fleet import GceVm
from lib.tier_controller import evidence_for


class PendingOomReclassificationTests(unittest.TestCase):
    def fixture(self):
        vm=GceVm('oxidex-runners-new','z','RUNNING','n2-standard-32',True,32,128,instance_id='2')
        state={'pending_oom':{'z/2':'host'},'seen_oom_ids':['runner-oom'],
               'host_health':{'z/2':{'boot_id':'same-boot'}}}
        observed={'instance_id':'2','boot_id':'same-boot','observed_at':1000,'complete':True,
                  'boot_oom_complete':True,'boot_host_oom_ids':[],
                  'events':[{'id':'runner-oom','kind':'container','workload':'runner','at':999}]}
        return vm,state,observed,controller.args_parser().parse_args(['--scaling-mode','tiered'])

    def test_ambiguous_legacy_host_origin_is_quarantined_even_after_same_boot_refresh(self):
        vm,state,observed,args=self.fixture()
        with patch('lib.tier_controller.time.time',return_value=1000), \
             patch.object(controller,'read_utilization',return_value=[(.1,.06)]), \
             patch('lib.tier_controller.read_host_events',return_value=observed):
            evidence_for([vm],[],args,state)
        self.assertEqual(state['pending_oom']['z/2'],'host')
        self.assertTrue(state['pending_oom_origins']['z/2']['quarantined'])

    def test_actual_historical_global_or_unproven_history_retains_host_classification(self):
        for variant in ('historical_global','changed_boot','incomplete_boot','wrong_identity','fresh_global'):
            with self.subTest(variant=variant):
                vm,state,observed,args=self.fixture()
                if variant=='historical_global':observed['boot_host_oom_ids']=['real-old-global']
                if variant=='changed_boot':observed['boot_id']='new-boot'
                if variant=='incomplete_boot':observed['boot_oom_complete']=False
                if variant=='wrong_identity':observed['instance_id']='different'
                if variant=='fresh_global':observed['events'].append({'id':'new-global','kind':'host','workload':'unknown','at':999})
                with patch('lib.tier_controller.time.time',return_value=1000), \
                     patch.object(controller,'read_utilization',return_value=[(.1,.06)]), \
                     patch('lib.tier_controller.read_host_events',return_value=observed):
                    evidence_for([vm],[],args,state)
                self.assertEqual(state['pending_oom']['z/2'],'host')

    def test_two_successive_new_boot_samples_do_not_clear_older_global_oom(self):
        vm,state,observed,args=self.fixture()
        observed['boot_id']='new-boot'
        for tick in (1000,1300):
            observed['observed_at']=tick
            with patch('lib.tier_controller.time.time',return_value=tick), \
                 patch.object(controller,'read_utilization',return_value=[(.1,.06)]), \
                 patch('lib.tier_controller.read_host_events',return_value=observed):
                evidence_for([vm],[],args,state)
            self.assertEqual(state['pending_oom'].get('z/2'),'host')
        self.assertEqual(state['pending_oom_origins']['z/2']['boot_id'],'same-boot')

    def test_new_global_oom_records_its_origin_before_subsequent_reboots(self):
        vm,state,observed,args=self.fixture()
        state['pending_oom']={}
        observed['events']=[dict(id='real-global',kind='host',workload='unknown',at=999)]
        observed['boot_host_oom_ids']=['real-global']
        with patch('lib.tier_controller.time.time',return_value=1000), \
             patch.object(controller,'read_utilization',return_value=[(.1,.06)]), \
             patch('lib.tier_controller.read_host_events',return_value=observed):
            evidence_for([vm],[],args,state)
        observed['boot_id']='new-boot';observed['events']=[];observed['boot_host_oom_ids']=[]
        for tick in (1300,1600):
            observed['observed_at']=tick
            with patch('lib.tier_controller.time.time',return_value=tick), \
                 patch.object(controller,'read_utilization',return_value=[(.1,.06)]), \
                 patch('lib.tier_controller.read_host_events',return_value=observed):
                evidence_for([vm],[],args,state)
            self.assertEqual(state['pending_oom'].get('z/2'),'host')
        origin=state['pending_oom_origins']['z/2']
        self.assertEqual(origin['boot_id'],'same-boot')
        self.assertEqual(origin['event_ids'],['real-global'])
