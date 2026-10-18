"""Role uncertainty belongs to a physical disk and follows that disk's debt, not its VM's private storage."""

import copy
from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import DashboardStore, reconcile_instance_records
from lib.fleet_inventory import observe_fleet_items


class PhysicalDiskRoleTests(unittest.TestCase):
    now=1800000000.
    zone='us-south1-b'
    prefix='projects/p/zones/us-south1-b/'

    def setUp(self):
        price=patch('lib.dashboard_models.fetch_live_spot_pricing',return_value={})
        price.start();self.addCleanup(price.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.json'
        self.store=DashboardStore(state_path=self.path,fleet_source=lambda:[],clock=lambda:self.now)
        self.store.hydrate();self.addCleanup(self.store.close)

    def disk(self,name,identity,users,kind='pd-ssd'):
        return {'id':identity,'name':name,'type':kind,'sizeGb':'100','zone':'zones/'+self.zone,
                'creationTimestamp':datetime.fromtimestamp(self.now-36000,timezone.utc).isoformat(),
                'users':[self.prefix+'instances/'+user for user in users]}

    def items(self, *, shared=True,private=True,name='custom-cache',boot=False):
        result=[]
        for identity,vm in (('1','oxidex-runners-a'),('2','builder-b')):
            attached=[]
            if shared:
                attached.append({'source':self.prefix+'disks/'+name,'diskSizeGb':'100','type':'PERSISTENT',
                                 'boot':boot,'autoDelete':False})
            if private and identity=='1':
                attached.append({'source':self.prefix+'disks/runner-data-private','diskSizeGb':'100',
                                 'type':'PERSISTENT','boot':False,'autoDelete':False})
            at=datetime.fromtimestamp(self.now-36000,timezone.utc).isoformat()
            result.append({'id':identity,'name':vm,'zone':'zones/'+self.zone,'status':'RUNNING',
                'machineType':'machineTypes/n4-highcpu-16','lastStartTimestamp':at,'creationTimestamp':at,
                'scheduling':{'provisioningModel':'SPOT'},'disks':attached})
        return result

    def catalog(self, *, shared=True,private=True,attached=True,name='custom-cache',kind='pd-ssd'):
        values={}
        if shared:
            values[self.prefix+'disks/'+name]=self.disk(name,'shared',
                    ['oxidex-runners-a','builder-b'] if attached else [],kind)
        if private:
            values[self.prefix+'disks/runner-data-private']=self.disk('runner-data-private','private',
                    ['oxidex-runners-a'] if attached else [],'pd-balanced')
        return values

    def observe(self,items,catalog,known=True):
        return observe_fleet_items(items,disk_types=catalog,disk_types_known=known,
                shapes={(self.zone,'n4-highcpu-16'):{'vcpu':16,'ram_gib':32.}})

    def poll(self,rows,observed,hours,*,catalog=None,listed=True):
        return reconcile_instance_records(json.loads(json.dumps(rows)),copy.deepcopy(observed),project='p',
            now=self.now+hours*3600,disk_inventory=copy.deepcopy(catalog),disks_listed=listed,
            tax_overrides={'cicd_runner':0,'rust_builder':0})[0]

    def reload(self,rows,hours):
        self.store.state['instances']=rows
        self.store._persist();self.store.close()
        self.store=DashboardStore(state_path=self.path,fleet_source=lambda:[],clock=lambda:self.now+hours*3600)
        self.store.hydrate();self.addCleanup(self.store.close)
        return self.store.state['instances']

    def test_denied_removal_keeps_roles_on_unpaid_disk_and_recovers_detached(self):
        catalog=self.catalog()
        rows=self.poll([],self.observe(self.items(),catalog),0,catalog=catalog)
        rows=self.poll(rows,self.observe(self.items(shared=False),{},False),1,listed=False)
        rows=self.reload(rows,1)
        carrier=next(r for r in rows if r['instance_id']=='1')
        debt=next(d for d in carrier['unpaid_disk_details'] if d['id']=='shared')
        self.assertEqual(debt['storage_billing_ambiguous_roles'],['cicd_runner','rust_builder'])
        self.assertEqual(carrier['disks_detail'][0]['storage_billing_ambiguous_roles'],[])
        catalog=self.catalog(attached=False)
        observed=self.observe(self.items(shared=False),catalog)
        rows=self.poll(rows,observed,2,catalog=catalog)
        disk=next(r for r in rows if r['instance_id']=='disk-shared')
        self.assertTrue(disk['storage_billing_ambiguous'])
        self.assertEqual(disk['storage_billing_ambiguous_roles'],['cicd_runner','rust_builder'])
        self.reload(rows,2)
        self.store.state['scaling']['rust_builder']['include_disk_in_scale_budget']=True
        with patch('lib.dashboard_server.time.time',return_value=self.now+7200):
            self.assertIn('custom-cache',self.store._incomplete_cost_hosts('rust_builder'))

    def test_private_disk_never_inherits_shared_disk_roles_after_detachment(self):
        catalog=self.catalog()
        rows=self.poll([],self.observe(self.items(),catalog),0,catalog=catalog)
        private_only=self.catalog(shared=False,attached=False)
        rows=self.poll(rows,[],1,catalog=private_only)
        private=next(r for r in rows if r['instance_id']=='disk-private')
        self.assertFalse(private.get('storage_billing_ambiguous'))
        self.assertEqual(private['disks_detail'][0]['storage_billing_ambiguous_roles'],[])
        rows=self.poll(self.reload(rows,1),[],33*24,catalog=private_only)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['instance_id'],'disk-private')
        self.assertFalse(rows[0].get('storage_billing_ambiguous'))
        self.store.state['instances']=rows
        self.store.state['scaling']['rust_builder']['include_disk_in_scale_budget']=True
        with patch('lib.dashboard_server.time.time',return_value=self.now+33*86400):
            self.assertEqual(self.store._incomplete_cost_hosts('rust_builder'),[])

    def test_denied_remove_then_restore_keeps_physical_id_and_role_set(self):
        catalog=self.catalog()
        rows=self.poll([],self.observe(self.items(),catalog),0,catalog=catalog)
        rows=self.poll(rows,self.observe(self.items(shared=False),{},False),1,listed=False)
        rows=self.reload(rows,1)
        rows=self.poll(rows,self.observe(self.items(),{},False),2,listed=False)
        owner=next(r for r in rows if r['instance_id']=='1')
        shared=next(d for d in owner['disks_detail'] if d['source'].endswith('/custom-cache'))
        self.assertEqual(shared['id'],'shared')
        self.assertEqual(shared['storage_billing_ambiguous_roles'],['cicd_runner','rust_builder'])
        self.assertFalse(owner['unpaid_disk_details'])
        rows=self.poll(self.reload(rows,2),self.observe(self.items(),catalog),3,catalog=catalog)
        self.assertEqual(sum(d.get('billing_owner',True) for r in rows if r['status']=='RUNNING'
                             for d in r['disks_detail'] if d['id']=='shared'),1)

    def test_explicit_builder_performance_disk_holds_only_its_billing_role(self):
        for boot in (False,True):
            catalog=self.catalog(private=False,name='builder-cache',kind='pd-extreme')
            observed=self.observe(self.items(private=False,name='builder-cache',boot=boot),catalog)
            rows=self.poll([],observed,0,catalog=catalog)
            self.store.state['instances']=rows
            for role in ('cicd_runner','rust_builder'):
                self.store.state['scaling'][role]['include_disk_in_scale_budget']=True
                self.store.state['profiles'][role]['budget']['include_taxes']=False
                with patch('lib.dashboard_server.time.time',return_value=self.now):
                    affected=self.store._incomplete_cost_hosts(role)
                self.assertEqual(affected,['builder-b'] if role=='rust_builder' else [])
            runner=next(r for r in rows if r['role']=='cicd_runner')
            self.assertEqual(runner['pricing']['disk_hourly_usd'],0)
            self.assertTrue(runner['pricing_complete'])

    def test_old_nonowner_performance_flag_does_not_become_false_historical_debt(self):
        catalog=self.catalog(name='builder-cache',kind='pd-extreme')
        observed=self.observe(self.items(name='builder-cache'),catalog)
        rows=self.poll([],observed,0,catalog=catalog)
        runner=next(r for r in rows if r['role']=='cicd_runner')
        runner['unpriced_disk_performance']=True
        runner['pricing']['complete']=False
        runner['pricing_complete']=False
        for d in runner['disks_detail']:
            d.pop('unpriced_disk_performance',None)  # The old format still identified each attachment's type/owner.
        rows=self.poll(self.reload(rows,0),observed,1,catalog=catalog)
        self.store.state['instances']=rows
        self.store.state['scaling']['cicd_runner']['include_disk_in_scale_budget']=True
        with patch('lib.dashboard_server.time.time',return_value=self.now+3600):
            self.assertEqual(self.store._incomplete_cost_hosts('cicd_runner'),[])
        runner=next(r for r in rows if r['role']=='cicd_runner')
        self.assertTrue(runner['lifetime_cost_complete'])

    def test_retained_private_storage_does_not_keep_deleted_performance_disk_uncertainty(self):
        from lib.dashboard_server import _mark_instance_deleted
        catalog=self.catalog(name='runner-data-cache',kind='pd-extreme')
        items=self.items(name='runner-data-cache')
        for item in items:
            item['disks'][0]['autoDelete']=True
        rows=self.poll([],self.observe(items,catalog),0,catalog=catalog)
        owner=next(r for r in rows if r['role']=='cicd_runner')
        self.assertFalse(owner['pricing_complete'])
        retained=_mark_instance_deleted(owner,self.now+3600,disks_pending=True,project='p')
        self.assertTrue(retained['disk_liability_pending'])
        self.assertTrue(retained['pricing_complete'])
        self.assertGreater(retained['pricing']['disk_hourly_usd'],0)

    def test_retained_private_storage_does_not_keep_deleted_regional_disk_uncertainty(self):
        from lib.dashboard_server import _mark_instance_deleted
        catalog=self.catalog(name='runner-data-cache')
        catalog[self.prefix+'disks/runner-data-cache']['regional']=True
        items=self.items(name='runner-data-cache')
        for item in items:
            item['disks'][0]['autoDelete']=True
        rows=self.poll([],self.observe(items,catalog),0,catalog=catalog)
        owner=next(r for r in rows if r['role']=='cicd_runner')
        self.assertFalse(owner['pricing_complete'])
        retained=_mark_instance_deleted(owner,self.now+3600,disks_pending=True,project='p')
        self.assertTrue(retained['disk_liability_pending'])
        self.assertTrue(retained['pricing_complete'])
