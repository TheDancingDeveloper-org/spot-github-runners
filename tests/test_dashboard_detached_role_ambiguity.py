"""A disk keeps unresolved workload-role evidence after all attaching VMs disappear."""

import copy
from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import DashboardStore, instance_usage, reconcile_instance_records
from lib.fleet_inventory import observe_detached_disks


class DetachedRoleAmbiguityTests(unittest.TestCase):
    now = 1800000000.
    source = 'projects/p/zones/us-south1-b/disks/custom-cache'

    def setUp(self):
        price=patch('lib.dashboard_models.fetch_live_spot_pricing',return_value={})
        price.start();self.addCleanup(price.stop)
        self.catalog={self.source:{'id':'123','name':'custom-cache','type':'pd-ssd','sizeGb':'100',
            'zone':'zones/us-south1-b','creationTimestamp':datetime.fromtimestamp(self.now-36000,timezone.utc).isoformat(),
            'users':[]}}
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=DashboardStore(state_path=Path(self.temp.name)/'state.json')
        self.store.start();self.addCleanup(self.store.close)
        self.store.fleet_source=lambda:[]

    def observations(self):
        result=[]
        for identity,role in (('a','cicd_runner'),('b','rust_builder')):
            owner=identity=='a'
            detail={'id':'123','source':self.source,'type':'pd-ssd','gb':100,'auto_delete':False,'boot':False,
                    'created_epoch':self.now-36000,'billing_owner':owner}
            result.append({'instance_id':identity,'name':('oxidex-runners-' if owner else 'builder-')+identity,
                'role':role,'status':'RUNNING','spot':True,'kind':'vm','zone':'us-south1-b','region':'us-south1',
                'machine_type':'n4-highcpu-16','vcpu':0,'ram_gib':0.,'created_epoch':self.now-36000,
                'last_start_epoch':self.now-36000,'boot_disk_type':'pd-balanced','boot_disk_gb':0,
                'data_disk_type':'pd-ssd','data_disk_gb':100,'data_disks':{'pd-ssd':100} if owner else {},
                'data_disks_attached_gb':100,'disks_detail':[detail], 'disk_types_known':True,
                'storage_billing_ambiguous':True,'storage_billing_ambiguous_roles':['cicd_runner','rust_builder']})
        return result

    def poll(self, records, now, *, catalog=None, observations=None):
        return reconcile_instance_records(json.loads(json.dumps(records)),observations or [],project='p',now=now,
                disk_inventory=copy.deepcopy(self.catalog if catalog is None else catalog),
                tax_overrides={'cicd_runner':0,'rust_builder':0})[0]

    def assert_both_storage_budgets_held(self, rows, now):
        self.store.state['instances']=json.loads(json.dumps(rows))
        self.store._persist()
        self.store.close()
        self.store=DashboardStore(state_path=Path(self.temp.name)/'state.json', project='p',
                                  fleet_source=lambda:[], clock=lambda:now)
        self.store.hydrate()
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.state['instances'],json.loads(json.dumps(rows)))
        for role in ('cicd_runner','rust_builder'):
            self.store.state['profiles'][role]['budget']['include_taxes']=False
            self.store.state['scaling'][role]['shared_global_budget']=False
            for storage in (False,True):
                self.store.state['scaling'][role]['include_disk_in_scale_budget']=storage
                with patch('lib.dashboard_server.time.time',return_value=now):
                    affected=self.store._incomplete_cost_hosts(role)
                if storage:
                    self.assertIn('custom-cache',affected)
                else:
                    self.assertEqual(affected,[])

    def test_ambiguity_survives_detach_reload_and_history_retention(self):
        rows=self.poll([],self.now,observations=self.observations())
        rows=self.poll(rows,self.now+3600)
        detached=next(r for r in rows if r['kind']=='disk')
        self.assertTrue(detached.get('storage_billing_ambiguous'))
        self.assertEqual(detached.get('storage_billing_ambiguous_roles'),['cicd_runner','rust_builder'])
        self.assert_both_storage_budgets_held(rows,self.now+3600)
        later=self.now+33*86400
        rows=self.poll(rows,later)
        self.assertEqual(len(rows),1)  # The deleted VM history is no longer providing an accidental hold.
        self.assertEqual(rows[0]['kind'],'disk')
        self.assertTrue(rows[0]['storage_billing_ambiguous'])
        self.assertEqual(rows[0]['storage_billing_ambiguous_roles'],['cicd_runner','rust_builder'])
        self.assert_both_storage_budgets_held(rows,later)
        before=instance_usage(rows[0],later)['cost_all_in']
        self.assertGreater(instance_usage(rows[0],later+3600)['cost_all_in'],before)
        stable=self.poll(rows,later+3600)
        self.assertEqual(len(stable),1)
        self.assertAlmostEqual(instance_usage(stable[0],later+3600)['cost_all_in'],
                               instance_usage(rows[0],later+3600)['cost_all_in'],places=6)

    def test_new_user_role_evidence_resolves_current_ambiguity(self):
        rows=self.poll([],self.now,observations=self.observations())
        rows=self.poll(rows,self.now+3600)
        catalog=copy.deepcopy(self.catalog)
        catalog[self.source]['users']=['projects/p/zones/us-south1-b/instances/builder-new']
        resolved=self.poll(rows,self.now+7200,catalog=catalog)
        detached=next(r for r in resolved if r['kind']=='disk')
        self.assertEqual(detached['role'],'rust_builder')
        self.assertFalse(detached.get('storage_billing_ambiguous'))
        self.assertFalse(detached.get('storage_billing_ambiguous_roles'))

    def test_a_different_immutable_disk_id_never_inherits_the_old_ambiguity(self):
        rows=self.poll([],self.now,observations=self.observations())
        catalog=copy.deepcopy(self.catalog)
        catalog[self.source]['id']='replacement'
        catalog[self.source]['creationTimestamp']=datetime.fromtimestamp(self.now+60,timezone.utc).isoformat()
        detached=observe_detached_disks(catalog,tracked_roles={'id:123':'cicd_runner'})
        self.assertEqual(detached,[])
        gone=self.poll(rows,self.now+3600,catalog=catalog)
        self.assertFalse(any(r.get('kind')=='disk' for r in gone))

    def test_legacy_ambiguity_flag_without_role_list_stays_fail_closed(self):
        observations=self.observations()
        for obs in observations:
            obs.pop('storage_billing_ambiguous_roles')
        rows=self.poll([],self.now,observations=observations)
        rows=self.poll(rows,self.now+3600)
        detached=next(r for r in rows if r['kind']=='disk')
        self.assertTrue(detached['storage_billing_ambiguous'])
        self.assertEqual(detached['storage_billing_ambiguous_roles'],['cicd_runner','rust_builder'])
        self.assert_both_storage_budgets_held(rows,self.now+3600)

    def test_source_only_debt_carries_roles_when_the_disk_id_is_first_learned(self):
        observations=self.observations()
        for obs in observations:
            obs['disk_types_known']=False
            obs['data_disk_type']='pd-balanced'
            obs['data_disks']={'pd-balanced':100} if obs['instance_id']=='a' else {}
            obs['disks_detail'][0].update(id='',type='pd-balanced',created_epoch=None)
        rows=self.poll([],self.now,observations=observations,catalog={})
        rows=self.poll(rows,self.now+3600)
        detached=next(r for r in rows if r['kind']=='disk')
        self.assertTrue(detached.get('storage_billing_ambiguous'))
        self.assertEqual(detached.get('storage_billing_ambiguous_roles'),['cicd_runner','rust_builder'])
        self.assert_both_storage_budgets_held(rows,self.now+3600)
