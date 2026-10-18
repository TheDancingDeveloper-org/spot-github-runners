"""Live inventory owns capacity and charges retained resources independently of usable slots."""

import copy
from datetime import datetime, timezone
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import DashboardStore, instance_usage, reconcile_instance_records, _single_disk_all_in
from lib.fleet_inventory import observe_fleet_items
from lib.dashboard_scaling import evaluate_builder_scaling_decision


class Round43Contracts(unittest.TestCase):
    def setUp(self):
        self.now=time.time()
        pricing=patch('lib.dashboard_models.fetch_live_spot_pricing',return_value={})
        pricing.start(); self.addCleanup(pricing.stop)
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=DashboardStore(state_path=Path(self.temp.name)/'state.json')
        self.store.start(); self.addCleanup(self.store.close)

    def obs(self, identity='a', role='cicd_runner', **changes):
        return dict({'instance_id':identity,'name':('builder-' if role=='rust_builder' else 'oxidex-runners-')+identity,
             'role':role,'status':'RUNNING','zone':'us-south1-b','region':'us-south1','machine_type':'n4-highcpu-16',
             'vcpu':16,'ram_gib':32.,'spot':True,'runners_count':4 if role=='cicd_runner' else 1,
             'runners_count_known':True,'runner_limits_known':True,'runner_vcpu':4,'runner_ram_gib':8.,
             'boot_disk_type':'pd-balanced','boot_disk_gb':0,'data_disk_type':'pd-balanced','data_disk_gb':0,
             'data_disks':{},'disks_detail':[],'created_epoch':self.now-36000,'last_start_epoch':self.now-36000},**changes)

    def install(self, observations):
        self.store.fleet_source=lambda: copy.deepcopy(observations)
        self.store.refresh_fleet_inventory()
        return self.store.state['instances']

    def test_unknown_listeners_hold_retire_noop_and_growth_targets(self):
        self.install([self.obs('ready'),self.obs('unknown',runners_count_known=False)])
        before=copy.deepcopy(self.store.state)
        for target in (0,1,2,3):
            with self.subTest(target=target),self.assertRaisesRegex(ValueError,'runner total is unknown'):
                self.store.scale_region_instances({'role':'cicd_runner','region':'us-south1','target_instances':target})
        self.assertEqual(before,self.store.state)

    def test_live_counts_and_rate_lists_cannot_be_replaced_by_api_telemetry(self):
        self.install([self.obs('a'),self.obs('b'),self.obs('builder',role='rust_builder')])
        result=self.store.update_scaling({'role':'cicd_runner','telemetry':{'current_runners':999,'current_instances':999,
             'current_builder_instances':999,'running_instance_hourly_rates':[0.], 'builder_instance_hourly_rates':[0.],
             'unknown_runner_count_hosts':[], 'queued_jobs':3}})
        telem=result['scaling_telemetry']
        self.assertEqual((telem['current_runners'],telem['current_instances'],telem['current_builder_instances']),(8,2,1))
        self.assertEqual(len(telem['running_instance_hourly_rates']),2)
        self.assertTrue(all(rate>0 for rate in telem['running_instance_hourly_rates']))
        self.assertEqual(telem['queued_jobs'],3)
        self.assertEqual(result['evaluations']['cicd_runner']['current_runners'],8)
        self.assertEqual(json.loads((Path(self.temp.name)/'state.json').read_text())['scaling_telemetry']['current_runners'],8)

    def test_legacy_count_telemetry_remains_supported_without_inventory(self):
        result=self.store.update_scaling({'telemetry':{'current_runners':8,'current_instances':2}})
        self.assertEqual(result['scaling_telemetry']['current_runners'],8)
        self.assertEqual(result['scaling_telemetry']['current_instances'],2)

    def test_incompatible_builder_burn_reserved_outside_usable_slots(self):
        self.install([self.obs('good',role='rust_builder'),self.obs('bad',role='rust_builder',runner_limits_known=False)])
        total=sum(self.store._running_role_hourly_rates('rust_builder'))
        usable=self.store._running_role_hourly_rates('rust_builder',usable_only=True)
        self.assertEqual(len(usable),1)
        self.assertEqual(self.store.state['scaling_telemetry']['current_builder_instances'],1)
        self.assertEqual(self.store.state['scaling_telemetry']['builder_instance_hourly_rates'],usable)
        outside=self.store._outside_burn('rust_builder',shared=False,other_running_burn=0)
        self.assertAlmostEqual(outside,total-sum(usable),places=4)
        policy=dict(self.store.state['scaling']['rust_builder'], min_instances=2,max_instances=4,
             max_scale_up_hourly_usd=total+.05,max_scale_up_daily_usd=(total+.05)*24,
             max_scale_up_monthly_usd=(total+.05)*730)
        result=evaluate_builder_scaling_decision(policy,self.store._builder_evaluation_telemetry(),
                   per_instance_hourly_usd=.1,other_role_hourly_usd=outside)
        self.assertEqual(result['current_instances'],1)
        self.assertNotEqual(result['direction'],'scale_up')
        self.assertTrue(result['budget_capped'])

    def test_overcap_reduction_is_already_allowed_by_growth_only_cap_checks(self):
        self.install([self.obs('a'),self.obs('b')])
        unit=sum(self.store._running_role_hourly_rates('cicd_runner'))/2
        self.store.state['scaling']['cicd_runner'].update(max_runners=4,max_scale_up_hourly_usd=1.5*unit,
                max_scale_up_daily_usd=1.5*unit*24,max_scale_up_monthly_usd=1.5*unit*730)
        plan=self.store.scale_region_instances({'role':'cicd_runner','region':'us-south1','target_instances':1})
        self.assertEqual(plan['planned_count'],1)
        self.assertEqual(len(plan['would_retire']),1)
        self.assertEqual(plan['would_launch'],[])

    def test_custom_disk_is_tracked_when_owning_vm_disappears(self):
        disk={'id':'123','source':'z/custom-cache','type':'pd-ssd','gb':100,'auto_delete':False,'boot':False,
              'created_epoch':self.now-36000,'billing_owner':True}
        obs=self.obs('owner',role='rust_builder',vcpu=0,ram_gib=0,data_disk_type='pd-ssd',data_disk_gb=100,
                     data_disks={'pd-ssd':100},disks_detail=[disk])
        records,_=reconcile_instance_records([], [obs],project='p',now=self.now-7200,tax_overrides={'rust_builder':0})
        catalog={disk['source']:{'id':'123','name':'custom-cache','type':'pd-ssd','sizeGb':'100','zone':'zones/us-south1-b',
                'creationTimestamp':datetime.fromtimestamp(disk['created_epoch'],timezone.utc).isoformat(),'users':[]}}
        records,_=reconcile_instance_records(json.loads(json.dumps(records)),[],project='p',now=self.now-3600,
                                             disk_inventory=catalog,tax_overrides={'rust_builder':0})
        detached=next(r for r in records if r['instance_id']=='disk-123')
        self.assertEqual(detached['status'],'DETACHED')
        self.assertEqual(detached['role'],'rust_builder')
        rate=_single_disk_all_in(disk,family='n4',region='us-south1',project='p',tax_rate_percent=0)
        self.assertAlmostEqual(sum(instance_usage(r,self.now)['cost_all_in'] for r in records),10*rate,delta=.0001)

    def test_final_deleted_source_does_not_suppress_replacement_initial_debt(self):
        old={'id':'old','source':'z/custom-cache','type':'pd-ssd','gb':100,'auto_delete':False,'boot':False,
             'created_epoch':self.now-36000,'billing_owner':True}
        def observation(disk,identity):
            return self.obs(identity,role='rust_builder',vcpu=0,ram_gib=0,data_disk_type=disk['type'],data_disk_gb=100,
                     data_disks={disk['type']:100},disks_detail=[disk],disk_types_known=bool(disk.get('id')))
        rows,_=reconcile_instance_records([], [observation(old,'old-vm')],project='p',now=self.now-8*3600,
                                           tax_overrides={'rust_builder':0})
        rows,_=reconcile_instance_records(rows,[],project='p',now=self.now-6*3600,tax_overrides={'rust_builder':0})
        unknown=dict(old,id='',type='pd-balanced',created_epoch=None)
        rows,_=reconcile_instance_records(rows,[observation(unknown,'new-vm')],project='p',now=self.now-4*3600,
                                           disks_listed=False,tax_overrides={'rust_builder':0})
        owner=next(r for r in rows if r['instance_id']=='new-vm')
        self.assertTrue(owner['deferred_disk_sources'] or owner.get('disk_history_deferred_at'))
        new=dict(old,id='new',created_epoch=self.now-5*3600)
        rows,_=reconcile_instance_records(json.loads(json.dumps(rows)),[observation(new,'new-vm')],project='p',now=self.now,
                                           tax_overrides={'rust_builder':0})
        rate=_single_disk_all_in(new,family='n4',region='us-south1',project='p',tax_rate_percent=0)
        self.assertAlmostEqual(sum(instance_usage(r,self.now)['cost_all_in'] for r in rows),9*rate,delta=.0001)

    def test_later_shared_users_hold_both_budgets_on_a_single_vm_carrier(self):
        source='projects/p/zones/us-south1-b/disks/custom-cache'
        at=datetime.fromtimestamp(self.now-3600,timezone.utc).isoformat()
        items=[{'id':'a','name':'oxidex-runners-a','status':'RUNNING','zone':'zones/us-south1-b',
                'machineType':'machineTypes/n4-highcpu-16','lastStartTimestamp':at,'creationTimestamp':at,
                'scheduling':{'provisioningModel':'SPOT'},
                'disks':[{'type':'PERSISTENT','source':source,'diskSizeGb':'100','boot':False,'autoDelete':False}]}]
        disks={source:{'id':'123','name':'custom-cache','type':'pd-ssd','sizeGb':'100','creationTimestamp':at,
                       'users':['projects/p/zones/us-south1-b/instances/oxidex-runners-a',
                                'projects/p/zones/us-south1-b/instances/builder-b']}}
        observations=observe_fleet_items(items,disk_types=disks,
                     shapes={('us-south1-b','n4-highcpu-16'):{'vcpu':16,'ram_gib':32.}})
        self.install(observations)
        self.assertEqual(len(self.store.state['instances']),1)
        record=self.store.state['instances'][0]
        self.assertEqual(record['storage_billing_ambiguous_roles'],['cicd_runner','rust_builder'])
        for role in ('cicd_runner','rust_builder'):
            for disks_in_budget in (False,True):
                self.store.state['scaling'][role]['include_disk_in_scale_budget']=disks_in_budget
                self.store.state['profiles'][role]['budget']['include_taxes']=False
                self.assertEqual(self.store._incomplete_cost_hosts(role),[record['name']] if disks_in_budget else [])

    def test_recovery_cannot_claim_an_old_price_for_a_newly_catalogued_disk_type(self):
        unknown={'id':'','source':'z/new-type','type':'pd-balanced','gb':100,'auto_delete':False,'boot':False,
                 'created_epoch':None,'billing_owner':True}
        base=self.obs('future',role='rust_builder',vcpu=0,ram_gib=0,data_disk_type='pd-balanced',data_disk_gb=100,
                      data_disks={'pd-balanced':100},disks_detail=[unknown],disk_types_known=False)
        rows,_=reconcile_instance_records([], [base],project='p',now=self.now-3600,disks_listed=False,
                                          tax_overrides={'rust_builder':0})
        known=dict(unknown,id='future-id',type='future-storage',created_epoch=self.now-7200)
        observed=dict(base,data_disk_type='future-storage',data_disks={'future-storage':100},
                      disks_detail=[known],disk_types_known=True)
        with patch('lib.dashboard_models.fetch_live_spot_pricing',return_value={'disk_monthly_gib':{'future-storage':.2}}):
            rows,_=reconcile_instance_records(json.loads(json.dumps(rows)),[observed],project='p',now=self.now,
                                              tax_overrides={'rust_builder':0})
        self.assertTrue(rows[0]['pricing_complete'])
        self.assertFalse(rows[0]['lifetime_cost_complete'])
        self.assertIn('fallback_storage',rows[0]['unpriced_history_intervals'])
