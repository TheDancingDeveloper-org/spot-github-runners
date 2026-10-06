import unittest
from lib.fleet_evidence import record_resources, workload_signals
from lib.runner_fleet import GceVm


class EvidenceTests(unittest.TestCase):
    def test_absolute_demand_and_missing_identity_reset_window(self):
        vm = GceVm('oxidex-runners-a','z','RUNNING','n4-standard-32',True,32,128,instance_id='1')
        state = {}
        rows = record_resources(state,[vm],[(.25,.10)],1000)
        self.assertEqual(rows[0]['cpu_cores'],8)
        self.assertAlmostEqual(rows[0]['peak_ram_gib'],12.8)
        self.assertIsNone(record_resources(state,[vm],None,1300))
        self.assertEqual(state['resource_history'],{})
        self.assertIsNone(record_resources(state,[vm],[(float('nan'),.1)],1600))

    def test_early_poll_retains_real_peak_without_creating_an_extra_window(self):
        from lib.tier_policy import fits
        vm=GceVm('oxidex-runners-a','z','RUNNING','n4-standard-32',True,32,128,instance_id='1')
        state={}
        record_resources(state,[vm],[(.1,.1)],1000)
        spike=record_resources(state,[vm],[(.5,.5)],1100)
        self.assertEqual(spike[0]['windows'],1)
        self.assertEqual(spike[0]['peak_cpu_cores'],16)
        self.assertEqual(spike[0]['peak_ram_gib'],64)
        self.assertFalse(fits(('large',),spike),'an early CPU/RAM spike must veto consolidation')
        later=record_resources(state,[vm],[(.05,.05)],1300)
        self.assertEqual(later[0]['windows'],2)
        self.assertEqual(later[0]['peak_cpu_cores'],16)
        self.assertEqual(later[0]['peak_ram_gib'],64)
        self.assertFalse(fits(('large',),later),'a lower distinct poll must retain the observed spike')

    def test_changed_vm_id_cannot_reuse_memory_peak(self):
        old = GceVm('oxidex-runners-a','z','RUNNING','n2-standard-4',True,4,16,instance_id='1')
        new = GceVm('oxidex-runners-a','z','RUNNING','n2-standard-4',True,4,16,instance_id='2')
        state = {}
        record_resources(state,[old],[(.25,.9)],1000)
        rows = record_resources(state,[new],[(.25,.1)],1300)
        self.assertEqual(rows[0]['cpu_cores'],1)
        self.assertEqual(rows[0]['peak_ram_gib'],1.6)
        self.assertEqual(rows[0]['windows'],1)

    def test_only_compatible_queues_and_comparable_successes_drive_timing(self):
        jobs = [dict(id=1, status='queued', labels=['self-hosted','Linux','ARM64'], created_at='1970-01-01T00:00:00Z')]
        labels = [{'self-hosted','Linux','X64','spot'}]
        self.assertFalse(workload_signals(jobs,labels,1000)['pending'])
        jobs.append(dict(id=2,status='queued',labels=['self-hosted','Linux','X64','spot'],created_at='1970-01-01T00:00:00Z'))
        result = workload_signals(jobs,labels,1000)
        self.assertTrue(result['pending'])
        self.assertTrue(result['queue_delayed'])
        jobs[-1]['runner_group_id'] = 123
        self.assertFalse(workload_signals(jobs,labels,1000,group_ids={10})['pending'])

    def test_queue_labels_match_case_insensitively_without_bypassing_group_or_architecture(self):
        jobs=[dict(id=3,status='queued',labels=['SELF-HOSTED','linux','x64','SPOT'],
                   created_at='1970-01-01T00:00:00Z',runner_group_id=10)]
        available=[{'self-hosted','Linux','X64','spot'}]
        actual=workload_signals(jobs,available,1000,group_ids={10})
        self.assertTrue(actual['pending']);self.assertTrue(actual['queue_delayed'])
        self.assertFalse(workload_signals(jobs,available,1000,group_ids={11})['pending'])
        jobs[0]['labels']=['self-hosted','linux','arm64','spot']
        self.assertFalse(workload_signals(jobs,available,1000,group_ids={10})['pending'])

    def test_completion_label_case_does_not_split_comparable_duration_history(self):
        from datetime import datetime,timezone
        def iso(tick):return datetime.fromtimestamp(tick,timezone.utc).isoformat()
        jobs=[]
        for index in range(10):
            start=1000+index*1000;duration=100 if index<5 else 200
            jobs.append(dict(id=index,name='compile',repository='o/r',workflow_id=1,
                             labels=['self-hosted','linux','x64'] if index%2 else ['SELF-HOSTED','Linux','X64'],
                             conclusion='success',started_at=iso(start),completed_at=iso(start+duration)))
        self.assertTrue(workload_signals(jobs,[{'self-hosted','Linux','X64'}],12000)['duration_regression'])
