import unittest
from lib.tier_policy import plan_capacity


def sample(cpu=.1, memory=.1, cores=.8, ram=1.6, windows=6):
    return dict(instance_id='1',zone='z',cpu_fraction=cpu,memory_fraction=memory,
                cpu_cores=cores,peak_cpu_cores=cores,used_ram_gib=ram,peak_ram_gib=ram,windows=windows,
                io_complete=True,io_pressure_peaks=dict(full_avg10=0,full_avg60=0,some_avg10=0,some_avg60=0))


class TierPolicyTests(unittest.TestCase):
    def test_queued_low_checks_do_not_accumulate_a_shrink_streak(self):
        state={}
        for t in range(1000,2800,300):
            plan_capacity(('starting',),[sample()],{'pending':True},state,t)
        self.assertEqual(plan_capacity(('starting',),[sample()],{},state,2800)['target'],('starting',))

    def test_queue_with_headroom_increases_density_before_topology(self):
        state={}
        workload={'pending':True,'queue_delayed':True,'slots':8}
        plan_capacity(('expanded',),[sample()],workload,state,1000)
        result=plan_capacity(('expanded',),[sample()],workload,state,1300)
        self.assertEqual(result['target'],('expanded',))
        self.assertEqual(result['action'],'density')
        self.assertEqual(result['slots'],12)

    def test_two_hot_checks_and_memory_reason(self):
        state = {}
        evidence = [sample(memory=.8,ram=12.8)]
        self.assertEqual(plan_capacity(('starting',),evidence,{},state,1000)['target'],('starting',))
        self.assertEqual(plan_capacity(('starting',),evidence,{},state,1300)['target'],('expanded',))

    def test_shrink_requires_six_fresh_checks_and_safe_projection(self):
        state = {}
        for t in range(1000,2500,300):
            self.assertEqual(plan_capacity(('starting',),[sample()],{},state,t)['target'],('starting',))
        self.assertEqual(plan_capacity(('starting',),[sample()],{},state,2500)['target'],('small',))
        for ram in (14, float('nan')):
            state = {}
            for t in range(1000,2800,300):
                result = plan_capacity(('starting',),[sample(ram=ram)],{},state,t)
            self.assertEqual(result['target'],('starting',))

    def test_pending_unknown_and_cooldown_block_consolidation(self):
        for signals in ({'pending':True},{'complete':False}):
            state = {}
            for t in range(1000,2800,300):
                result = plan_capacity(('expanded',),[sample()],signals,state,t)
            self.assertEqual(result['target'],('expanded',))
        state = {'changed_at':1000}
        for t in (1000,1300):
            self.assertEqual(plan_capacity(('starting',),[sample(cpu=.9,cores=7.2)],{},state,t)['target'],('starting',))

    def test_new_host_oom_bypasses_streak_but_container_oom_does_not_add_hosts(self):
        result = plan_capacity(('starting',),[sample()],{'host_oom':True}, {},1000)
        self.assertEqual(result['target'],('expanded',))
        self.assertEqual(result['slots'],8)
        result = plan_capacity(('starting',),[sample()],{'container_oom':True}, {},1000)
        self.assertEqual(result['target'],('starting',))
        self.assertEqual(result['action'],'budget')

    def test_legacy_migration_uses_absolute_demand_and_empty_bootstraps(self):
        result = plan_capacity(('legacy',),[sample(cores=1,ram=3)],{}, {},1000)
        self.assertEqual(result['target'],('starting',))
        result = plan_capacity(('legacy',),[sample(cores=11,ram=50)],{}, {},1000)
        self.assertEqual(result['target'],('bulky',))
        self.assertEqual(plan_capacity((),None,{}, {},1000)['target'],('starting',))

    def test_compatible_duration_regression_blocks_shrink_and_grows_capacity(self):
        state={}
        workload={'duration_regression':True,'slots':8}
        plan_capacity(('starting',),[sample()],workload,state,1000)
        result=plan_capacity(('starting',),[sample()],workload,state,1300)
        self.assertEqual(result['target'],('expanded',))
        self.assertEqual(result['action'],'topology')
