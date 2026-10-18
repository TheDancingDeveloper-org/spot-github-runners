"""Known physical disk metadata survives owner changes while its current listing is unavailable."""

import copy
from datetime import datetime, timezone
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.fleet_inventory import observe_detached_disks
from lib.dashboard_server import (DashboardStore, _live_instance_record, _single_disk_all_in,
                                  instance_usage, reconcile_instance_records)


class DeniedDiskHandoffTests(unittest.TestCase):
    now = 1800000000.

    def setUp(self):
        pricing = patch('lib.dashboard_models.fetch_live_spot_pricing', return_value={})
        pricing.start()
        self.addCleanup(pricing.stop)
        self.disk = {'id': '123', 'source': 'z/builder-cache', 'type': 'pd-ssd', 'gb': 100,
                     'created_epoch': self.now-10*3600, 'boot': False, 'auto_delete': False, 'billing_owner': True}
        self.unknown = dict(self.disk, id='', type='pd-balanced', created_epoch=None)

    def observation(self, disk, *, detached=False, known=True, identity='vm'):
        return {'instance_id': 'disk-123' if detached else identity, 'name': 'builder-cache' if detached else 'builder-'+identity,
                'kind': 'disk' if detached else 'vm', 'role': 'rust_builder', 'status': 'DETACHED' if detached else 'RUNNING',
                'zone': 'us-south1-b', 'region': 'us-south1', 'machine_type': '' if detached else 'n4-highcpu-16',
                'vcpu': 0, 'ram_gib': 0., 'spot': True, 'disk_types_known': known,
                'created_epoch': self.now-12*3600, 'last_start_epoch': self.now-12*3600,
                'boot_disk_type': 'pd-balanced', 'boot_disk_gb': 0, 'data_disk_type': disk['type'],
                'data_disk_gb': disk['gb'], 'data_disks': {disk['type']: disk['gb']},
                'data_disks_attached_gb': disk['gb'], 'disks_detail': [copy.deepcopy(disk)]}

    def poll(self, records, observations, hours, listed):
        return reconcile_instance_records(json.loads(json.dumps(records)), observations, project='p',
             now=self.now-hours*3600, disks_listed=listed, tax_overrides={'rust_builder': 0})[0]

    def assert_known(self, record):
        detail=record['disks_detail'][0]
        for key in ('id', 'type', 'created_epoch'):
            self.assertEqual(detail[key], self.disk[key])
        self.assertFalse(record['disk_types_known'])  # Last-known evidence never claims a current verification.
        expected=_single_disk_all_in(self.disk, family='n4', region='us-south1', project='p', tax_rate_percent=0)
        self.assertAlmostEqual(record['pricing']['disk_hourly_usd'], expected, delta=.0001)

    def test_detached_metadata_reused_for_denied_attachment_and_repeated_polls(self):
        rows=self.poll([], [self.observation(self.disk, detached=True)], 8, True)
        for hours in (4, 2):
            rows=self.poll(rows, [self.observation(self.unknown, known=False)], hours, False)
            self.assert_known(next(r for r in rows if r['instance_id']=='vm'))
        rows=self.poll(rows, [self.observation(self.disk)], 0, True)
        rate=_single_disk_all_in(self.disk, family='n4', region='us-south1', project='p', tax_rate_percent=0)
        self.assertAlmostEqual(sum(instance_usage(r,self.now)['cost_all_in'] for r in rows),10*rate,delta=.0001)

    def test_pending_retained_carrier_supplies_metadata_to_new_owner(self):
        rows=self.poll([], [self.observation(self.disk,identity='old')],8,True)
        rows=self.poll(rows,[],6,False)
        self.assertTrue(rows[0]['disk_liability_pending'])
        rows=self.poll(rows,[self.observation(self.unknown,known=False,identity='new')],4,False)
        self.assert_known(next(r for r in rows if r['instance_id']=='new'))
        rows=self.poll(rows,[self.observation(self.disk,identity='new')],0,True)
        rate=_single_disk_all_in(self.disk,family='n4',region='us-south1',project='p',tax_rate_percent=0)
        self.assertAlmostEqual(sum(instance_usage(r,self.now)['cost_all_in'] for r in rows),10*rate,delta=.0001)

    def test_final_deleted_disk_is_not_a_trusted_source_for_name_reuse(self):
        rows=self.poll([], [self.observation(self.disk,detached=True)],8,True)
        rows=self.poll(rows,[],6,True)
        self.assertEqual(rows[0]['status'],'DELETED')
        rows=self.poll(rows,[self.observation(self.unknown,known=False)],4,False)
        current=next(r for r in rows if r['instance_id']=='vm')
        self.assertEqual(current['disks_detail'][0]['type'],'pd-balanced')
        self.assertFalse(current['disks_detail'][0].get('id'))

    def test_changed_identity_or_size_never_borrows_known_metadata(self):
        for disk in (dict(self.unknown,id='replacement'), dict(self.unknown,gb=200),
                     dict(self.unknown,created_epoch=self.now-5*3600)):
            with self.subTest(disk=disk):
                rows=self.poll([], [self.observation(self.disk,detached=True)],8,True)
                rows=self.poll(rows,[self.observation(disk,known=False)],4,False)
                current=next(r for r in rows if r['instance_id']=='vm')
                self.assertEqual(current['disks_detail'][0]['type'],'pd-balanced')
                self.assertNotEqual(current['disks_detail'][0].get('id'),self.disk['id'])


class SplitSnapshotAmbiguityConsumers(unittest.TestCase):
    def test_single_ambiguous_disk_holds_both_separate_storage_budgets_only(self):
        with tempfile.TemporaryDirectory() as temp, patch('lib.dashboard_models.fetch_live_spot_pricing',return_value={}):
            store=DashboardStore(state_path=Path(temp)/'state.json')
            store.start()
            self.addCleanup(store.close)
            now=time.time()
            obs = observe_detached_disks({'projects/p/zones/us-south1-b/disks/custom-cache': {
                'id': '123', 'name': 'custom-cache', 'type': 'pd-ssd', 'sizeGb': '100',
                'zone': 'zones/us-south1-b', 'creationTimestamp': datetime.fromtimestamp(now-3600, timezone.utc).isoformat(),
                'users': ['projects/p/zones/us-south1-b/instances/oxidex-runners-a',
                          'projects/p/zones/us-south1-b/instances/builder-b']}})[0]
            self.assertTrue(obs['storage_billing_ambiguous'])
            store.state['instances']=[_live_instance_record(obs,project='p',now=now)]
            for role in ('cicd_runner','rust_builder'):
                store.state['scaling'][role]['shared_global_budget']=False
                for storage in (False,True):
                    store.state['profiles'][role]['budget']['include_disk_costs']=storage
                    store.state['scaling'][role]['include_disk_in_scale_budget']=storage
                    self.assertEqual(store._incomplete_cost_hosts(role),['custom-cache'] if storage else [])
            self.assertEqual(len(store.state['instances']),1)
            self.assertGreater(store.state['instances'][0]['pricing']['disk_hourly_usd'],0)
