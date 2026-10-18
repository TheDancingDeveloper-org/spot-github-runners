"""Trusted regional components and unknown tax are selected independently."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.dashboard_server import DashboardStore, _live_instance_record, _pricing_component_reasons


class RegionalTaxModes(unittest.TestCase):
    def setUp(self):
        self.now=time.time()
        self.region='unlisted-region1'
        self.live={'machine_hourly':{(self.region,'n4-highcpu-16'):.2},
                   'disk_monthly_gib':{(self.region,'pd-ssd'):.17}}
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=DashboardStore(state_path=Path(self.temp.name)/'state.json')
        self.store.start();self.addCleanup(self.store.close)
        self.store.fleet_source=lambda:[]
        self.disk={'source':'z/cache','id':'123','type':'pd-ssd','gb':100,'created_epoch':self.now-3600,
                   'boot':False,'auto_delete':False,'billing_owner':True}
        self.obs={'instance_id':'a','name':'builder-a','role':'rust_builder','status':'RUNNING','spot':True,
            'zone':self.region+'-a','region':self.region,'machine_type':'n4-highcpu-16','vcpu':16,'ram_gib':32.,
            'boot_disk_type':'pd-balanced','boot_disk_gb':0,'data_disk_type':'pd-ssd','data_disk_gb':100,
            'data_disks':{'pd-ssd':100},'disks_detail':[self.disk],'created_epoch':self.now-3600,
            'last_start_epoch':self.now-3600,'disk_types_known':True}

    def install(self, *, live=None, override=False):
        with patch('lib.dashboard_models.fetch_live_spot_pricing',return_value=self.live if live is None else live):
            record=_live_instance_record(self.obs,project='p',now=self.now,
                    tax_overrides={'rust_builder':20} if override else {})
        self.store.state['instances']=[record]
        self.store.state['profiles']['rust_builder']['budget'].update(override_regional_tax=override,tax_rate_percent=20)
        return record

    def mode(self, *, disks, taxes):
        self.store.state['scaling']['rust_builder']['include_disk_in_scale_budget']=disks
        self.store.state['profiles']['rust_builder']['budget'].update(include_disk_costs=disks,include_taxes=taxes)
        return self.store._incomplete_cost_hosts('rust_builder')

    def test_trusted_rates_with_unknown_tax_remain_usable_for_pretax_budget(self):
        record=self.install()
        self.assertEqual(_pricing_component_reasons(record),([],[]))
        self.assertEqual(self.mode(disks=True,taxes=False),[])
        self.assertEqual(self.mode(disks=False,taxes=False),[])
        self.assertEqual(self.mode(disks=True,taxes=True),['builder-a'])
        self.assertEqual(self.mode(disks=False,taxes=True),['builder-a'])
        self.assertTrue(self.store.query_instances(include_disk_and_tax=False)['window_cost_complete'])
        self.assertFalse(self.store.query_instances(include_disk_and_tax=True)['window_cost_complete'])

    def test_explicit_override_makes_tax_known_with_trusted_components(self):
        record=self.install(override=True)
        self.assertTrue(record['tax_rate_overridden'])
        self.assertEqual(record['pricing']['tax_rate_percent'],20)
        self.assertEqual(self.mode(disks=True,taxes=True),[])
        self.assertTrue(self.store.query_instances(include_disk_and_tax=True)['window_cost_complete'])

    def test_unknown_component_rates_still_hold_when_tax_is_overridden_or_excluded(self):
        for override in (False,True):
            self.install(live={},override=override)
            self.assertTrue(self.mode(disks=True,taxes=False))
            self.assertTrue(self.mode(disks=False,taxes=False))
            self.assertTrue(self.mode(disks=True,taxes=True))
        partial={'machine_hourly':self.live['machine_hourly']}
        self.install(live=partial,override=True)
        self.assertEqual(self.mode(disks=False,taxes=True),[])
        self.assertEqual(self.mode(disks=True,taxes=True),['builder-a'])

    def test_tax_history_affects_tax_inclusive_budget_not_compute_or_storage_pretax(self):
        self.install()
        self.store.state['profiles']['rust_builder']['budget'].update(override_regional_tax=True,tax_rate_percent=20)
        self.store._clock=lambda:self.now+3600
        with patch('lib.dashboard_server.fetch_live_spot_pricing',return_value=self.live):
            self.store._refresh_instances_pricing(live=True)
        record=self.store.state['instances'][0]
        self.assertIn('fallback_compute_tax',record['unpriced_history_intervals'])
        self.assertIn('fallback_storage_tax',record['unpriced_history_intervals'])
        self.assertNotIn('fallback_compute',record['unpriced_history_intervals'])
        self.assertNotIn('fallback_storage',record['unpriced_history_intervals'])
        self.assertEqual(self.mode(disks=True,taxes=False),[])
        self.assertEqual(self.mode(disks=False,taxes=False),[])
        self.assertEqual(self.mode(disks=False,taxes=True),['builder-a'])
