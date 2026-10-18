"""Suspension storage and running compute end at distinct reported boundaries."""
import json
import unittest
from unittest.mock import patch

from lib.dashboard_server import _live_instance_record, _retain_unpriced_history, _unpriced_history_in_window, instance_usage


class SuspensionTerminationTests(unittest.TestCase):
    def observation(self, **changes):
        return dict({'instance_id':'1', 'name':'builder-suspended', 'role':'rust_builder', 'kind':'vm',
            'status':'TERMINATED', 'region':'us-south1', 'zone':'us-south1-a',
            'machine_type':'n4-highcpu-16', 'vcpu':16, 'ram_gib':32, 'spot':True,
            'created_epoch':100, 'last_start_epoch':100, 'last_suspended_epoch':400,
            'last_stop_epoch':900, 'boot_disk_gb':0, 'data_disk_gb':0, 'data_disks':{},
            'disk_types_known':True}, **changes)

    def live(self, obs, now, previous=None):
        with patch('lib.dashboard_models.fetch_live_spot_pricing', return_value={}):
            return _live_instance_record(obs, project='p', now=now, previous=previous)

    def test_terminal_storage_interval_closes_at_reported_termination(self):
        previous={'status':'SUSPENDED','started_epoch':100,'stopped_epoch':400,
                  'unpriced_history_intervals':{'suspended_memory':[[400,None]]}}
        for status in ('TERMINATED','STOPPED'):
            with self.subTest(status=status):
                record={'status':status,'started_epoch':100,'stopped_epoch':400}
                _retain_unpriced_history(record, self.observation(status=status), previous, 1000)
                self.assertEqual(record['unpriced_history_intervals']['suspended_memory'], [[400.,900.]])
                self.assertFalse(_unpriced_history_in_window(record,900,1000,include_storage=True))
                self.assertNotIn('pending_suspension_epoch',record)

    def test_missing_termination_time_is_bounded_once_and_survives_reload_and_resume(self):
        obs=self.observation(last_stop_epoch=None)
        record=self.live(obs,1000)
        for now,status,start in ((1100,'TERMINATED',100),(1200,'STAGING',100),(1400,'RUNNING',1300)):
            record=json.loads(json.dumps(record))
            record=self.live(dict(obs,status=status,last_start_epoch=start),now,record)
            self.assertFalse(_unpriced_history_in_window(record,1000,now,include_storage=True),status)
        self.assertEqual(record['unpriced_history_intervals']['suspended_memory'], [[400.,1000.]])

    def test_first_observed_terminated_compute_ends_at_suspension_not_termination(self):
        for stop in (None,50,900):
            with self.subTest(stop=stop):
                record=self.live(self.observation(last_stop_epoch=stop),1000)
                self.assertEqual(record['stopped_epoch'],400)
                self.assertAlmostEqual(instance_usage(record,1000)['compute_hours'],300/3600)
                self.assertEqual(instance_usage(record,1000,500)['compute_hours_in_window'],0)

    def test_delayed_terminal_observation_trims_repriced_compute_at_suspension(self):
        record=self.live(self.observation(status='RUNNING',last_stop_epoch=None,last_suspended_epoch=None),200)
        record['prior_runs']=[[100,700,.2,.2]]
        record['compute_segment_start']=700
        record=self.live(self.observation(),1000,record)
        self.assertAlmostEqual(instance_usage(record,1000)['compute_hours'],300/3600)
        self.assertEqual(record['prior_runs'],[[100.,400.,.2,.2]])

    def test_old_suspension_does_not_shorten_a_new_run(self):
        record=self.live(self.observation(last_start_epoch=700,last_stop_epoch=900),1000)
        self.assertEqual(record['stopped_epoch'],900)
        self.assertAlmostEqual(instance_usage(record,1000)['compute_hours'],200/3600)

    def test_terminal_boundary_stays_closed_after_history_retention(self):
        day=86400.
        obs=self.observation(last_start_epoch=day,last_suspended_epoch=2*day,last_stop_epoch=3*day)
        record=self.live(obs,4*day)
        for now in (40*day,41*day):
            record=self.live(obs,now,json.loads(json.dumps(record)))
            self.assertFalse(_unpriced_history_in_window(record,now-31*day,now,include_storage=True))
            self.assertTrue(record['unpriced_storage_lifetime_history'])

    def test_missing_suspension_timestamp_preserves_confirmed_same_run_compute_end(self):
        obs=self.observation(status='SUSPENDED',last_stop_epoch=None)
        record=self.live(obs,500)
        for now in (1000,1100):
            record=self.live(self.observation(last_suspended_epoch=None),now,json.loads(json.dumps(record)))
            self.assertEqual(record['stopped_epoch'],400)
            self.assertAlmostEqual(instance_usage(record,now)['compute_hours'],300/3600)

    def test_restart_does_not_invent_compute_during_known_saved_memory_interval(self):
        for timestamp in (400,None):
            with self.subTest(suspension_timestamp=timestamp):
                record=self.live(self.observation(status='SUSPENDED',last_stop_epoch=None),500)
                record=self.live(self.observation(last_suspended_epoch=timestamp),1000,record)
                record=self.live(self.observation(status='RUNNING',last_start_epoch=1300,
                    last_suspended_epoch=timestamp),1400,json.loads(json.dumps(record)))
                self.assertNotIn('prior_compute',record['unpriced_history_intervals'])
                self.assertFalse(record.get('unpriced_compute_lifetime_history',False))
                self.assertAlmostEqual(instance_usage(record,1400)['compute_hours'],400/3600)

    def test_unknown_intervening_run_starts_after_confirmed_memory_termination(self):
        record=self.live(self.observation(status='SUSPENDED',last_stop_epoch=None),500)
        record=self.live(self.observation(),1000,record)
        record=self.live(self.observation(status='RUNNING',last_start_epoch=1300,last_stop_epoch=1100),1400,record)
        self.assertEqual(record['unpriced_history_intervals']['prior_compute'],[[900.,1100.]])
