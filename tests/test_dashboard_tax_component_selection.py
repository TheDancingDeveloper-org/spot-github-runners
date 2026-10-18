"""Tax-only uncertainty follows the charged component across writes, reloads, and legacy history."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import (DashboardStore, _live_instance_record, _remember_deferred_trust,
                                  _unpriced_history_in_window)
from lib.fleet_inventory import FleetListing


class TaxComponentSelectionTests(unittest.TestCase):
    epoch=1800000000.
    region='unlisted-region1'

    def setUp(self):
        self.now=self.epoch
        self.live={'machine_hourly':{(self.region,'n4-highcpu-16'):.2},
                   'disk_monthly_gib':{(self.region,'pd-ssd'):.17}}
        for target in ('lib.dashboard_models.fetch_live_spot_pricing','lib.dashboard_server.fetch_live_spot_pricing'):
            price=patch(target,side_effect=lambda **kwargs:copy.deepcopy(self.live))
            price.start();self.addCleanup(price.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.json'
        self.store=self.new_store()

    def new_store(self):
        store=DashboardStore(state_path=self.path,project='p',clock=lambda:self.now,fleet_source=lambda:FleetListing([]))
        store.hydrate();self.addCleanup(store.close)
        return store

    def observation(self,*,kind='vm',status='RUNNING'):
        disk={'id':'123','source':'z/cache','type':'pd-ssd','gb':100,'created_epoch':self.epoch-10*3600,
              'billing_owner':True,'boot':False,'auto_delete':False}
        return {'instance_id':'a','name':'builder-a','role':'rust_builder','kind':kind,'status':status,'spot':True,
              'region':self.region,'zone':self.region+'-a','machine_type':'' if kind=='disk' else 'n4-highcpu-16',
              'vcpu':0 if kind=='disk' else 16,'ram_gib':0. if kind=='disk' else 32.,
              'boot_disk_type':'pd-balanced','boot_disk_gb':0,'data_disk_type':'pd-ssd','data_disk_gb':100,
              'data_disks':{'pd-ssd':100},'disks_detail':[disk],'disk_types_known':True,
              'created_epoch':self.epoch-10*3600,'last_start_epoch':self.epoch-10*3600,
              'last_stop_epoch':self.epoch-8*3600 if status=='TERMINATED' else None}

    def poll(self,hour,obs):
        self.now=self.epoch+hour*3600
        self.store.fleet_source=lambda:FleetListing([copy.deepcopy(obs)])
        self.store.refresh_fleet_inventory()
        self.assertFalse(self.store.fleet_inventory_status()['last_error'])

    def tax(self,hour,override):
        self.now=self.epoch+hour*3600
        self.store.update_budget({'target':'rust_builder','override_regional_tax':override,'tax_rate_percent':0})

    def incomplete(self,storage,tax):
        self.store.state['scaling']['rust_builder']['include_disk_in_scale_budget']=storage
        self.store.state['profiles']['rust_builder']['budget'].update(include_disk_costs=storage,include_taxes=tax)
        with patch('lib.dashboard_server.time.time',return_value=self.now):
            return self.store._incomplete_cost_hosts('rust_builder')

    def reload(self):
        self.store._persist();before=json.loads(self.path.read_text())['instances']
        self.store.close();self.store=self.new_store()
        self.assertEqual(self.store.state['instances'],before)

    def test_stopped_unknown_storage_tax_does_not_hold_compute_tax_budget(self):
        self.tax(-8,True)
        stopped=self.observation(status='TERMINATED')
        self.poll(-8,stopped)
        self.tax(-6,False)  # Numeric tax stays zero, but knowledge changes after compute has already stopped.
        self.poll(0,stopped)
        self.reload()
        intervals=self.store.state['instances'][0]['unpriced_history_intervals']
        self.assertNotIn('fallback_compute_tax',intervals)
        self.assertEqual(intervals['fallback_storage_tax'],[[self.epoch-6*3600,self.epoch]])
        self.assertEqual(self.incomplete(False,True),[])
        self.assertEqual(self.incomplete(True,False),[])
        self.assertEqual(self.incomplete(True,True),['builder-a'])
        self.tax(1,True)
        self.reload()
        self.assertEqual(self.incomplete(False,True),[])
        self.assertEqual(self.incomplete(True,True),['builder-a'])

    def test_running_mixed_tax_uncertainty_holds_only_tax_selected_modes(self):
        self.poll(0,self.observation())
        self.tax(1,True)
        self.reload()
        intervals=self.store.state['instances'][0]['unpriced_history_intervals']
        self.assertIn('fallback_compute_tax',intervals)
        self.assertIn('fallback_storage_tax',intervals)
        self.assertEqual(self.incomplete(False,False),[])
        self.assertEqual(self.incomplete(True,False),[])
        self.assertEqual(self.incomplete(False,True),['builder-a'])
        self.assertEqual(self.incomplete(True,True),['builder-a'])
        view=self.store.query_instances(include_disk_and_tax=False)
        self.assertTrue(view['window_cost_complete'])
        self.assertTrue(view['instances'][0]['lifetime_selected_cost_complete'])
        self.poll(33*24,self.observation())
        self.reload()
        view=self.store.query_instances(include_disk_and_tax=False)
        self.assertTrue(view['instances'][0]['lifetime_selected_cost_complete'])
        self.assertTrue(self.store.state['instances'][0]['unpriced_compute_tax_lifetime_history'])
        self.assertFalse(self.store.query_instances(include_disk_and_tax=True)['instances'][0]['lifetime_selected_cost_complete'])

    def test_disk_debt_emitter_is_storage_tax_only(self):
        obs=self.observation(kind='disk',status='DETACHED')
        record=_live_instance_record(obs,project='p',now=self.now,tax_overrides={'rust_builder':0})
        disk=dict(obs['disks_detail'][0],billing_deferred_pricing=[[self.epoch-4*3600,
            {'tax_rate_known':False,'tax_rate_percent':0,'known_disk_types':['pd-ssd'],'disk_monthly_gib':{'pd-ssd':.17}}]])
        _remember_deferred_trust(record,disk,self.epoch-4*3600,self.epoch-2*3600,self.now)
        intervals=record['unpriced_history_intervals']
        self.assertEqual(set(intervals),{'fallback_storage_tax'})
        self.store.state['instances']=[record]
        self.reload()
        self.assertEqual(self.incomplete(False,True),[])
        self.assertEqual(self.incomplete(True,False),[])
        self.assertEqual(self.incomplete(True,True),['builder-a'])
        self.assertTrue(self.store.query_instances(include_disk_and_tax=False)['window_cost_complete'])

    def test_legacy_disk_only_pair_tax_decodes_without_compute_pollution(self):
        obs=self.observation(kind='disk',status='DETACHED')
        record=_live_instance_record(obs,project='p',now=self.now,tax_overrides={'rust_builder':0})
        record['unpriced_history_intervals']={'fallback_tax':[self.epoch-4*3600,self.epoch-2*3600]}
        record['unpriced_lifetime_history']=True
        self.store.state['instances']=[record]
        self.reload()
        before=copy.deepcopy(self.store.state)
        self.assertEqual(self.incomplete(False,True),[])
        self.assertEqual(self.incomplete(True,True),['builder-a'])
        self.assertEqual(self.store.state['instances'],before['instances'])  # Reads do not rewrite saved history.
        self.poll(0,obs)
        intervals=self.store.state['instances'][0]['unpriced_history_intervals']
        self.assertNotIn('fallback_tax',intervals)
        self.assertNotIn('fallback_compute_tax',intervals)
        self.assertIn('fallback_storage_tax',intervals)

    def test_legacy_vm_envelope_is_split_at_recorded_compute_stop(self):
        self.tax(-8,True)
        obs=self.observation(status='TERMINATED')
        self.poll(-8,obs)
        record=self.store.state['instances'][0]
        record['unpriced_history_intervals']={'fallback_tax':[[self.epoch-9*3600,self.epoch-4*3600]]}
        self.now=self.epoch
        self.assertTrue(_unpriced_history_in_window(record,self.epoch-9*3600,self.epoch-8*3600,
                       include_storage=False,include_tax=True))
        self.assertFalse(_unpriced_history_in_window(record,self.epoch-7*3600,self.epoch-4*3600,
                        include_storage=False,include_tax=True))
        self.assertTrue(_unpriced_history_in_window(record,self.epoch-7*3600,self.epoch-4*3600,
                       include_storage=True,include_tax=True))
        self.assertFalse(_unpriced_history_in_window(record,self.epoch-9*3600,self.epoch-4*3600,
                        include_storage=True,include_tax=False))

    def test_legacy_tax_decode_keeps_folded_compute_but_not_storage_only_buckets(self):
        self.tax(-8,True)
        self.poll(-8,self.observation(status='TERMINATED'))
        self.now=self.epoch
        for compute_cost, expected in ((.2,True),(0.,False)):
            record=copy.deepcopy(self.store.state['instances'][0])
            record['prior_runs']=[]
            record['compute_segment_start']=self.epoch-8*3600
            record['folded_cost_buckets']={str(int(self.epoch-9*3600)):[1. if compute_cost else 0.,.2,compute_cost]}
            record['unpriced_history_intervals']={'fallback_tax':[self.epoch-9*3600,self.epoch-8*3600]}
            self.assertEqual(_unpriced_history_in_window(record,self.epoch-9*3600,self.epoch-8*3600,
                             include_storage=False,include_tax=True),expected)
