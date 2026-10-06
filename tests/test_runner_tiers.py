import unittest
from lib.runner_tiers import TIERS, next_topology, tier_for_shape, validate_topology


class TierTopologyTests(unittest.TestCase):
    def test_growth_and_reverse_stop_at_five_large_hosts(self):
        stages = [('large',), ('large', 'small'), ('large', 'expanded'),
                  ('large', 'large'), ('large', 'large', 'small')]
        for before, after in zip(stages, stages[1:]):
            self.assertEqual(next_topology(before, 'up', 'queue'), after)
            self.assertEqual(next_topology(after, 'down', 'low'), before)
        self.assertEqual(next_topology(('large',)*5, 'up', 'queue'), ('large',)*5)
        self.assertEqual(next_topology(('large',)*5, 'down', 'low'), ('large',)*4+('expanded',))
        with self.assertRaises(ValueError):
            validate_topology(('large',)*6)

    def test_first_host_and_memory_upgrade_preserve_cpu_shape(self):
        self.assertEqual(next_topology((), 'up', 'empty'), ('starting',))
        self.assertEqual(next_topology(('starting',), 'up', 'memory'), ('expanded',))
        self.assertEqual(next_topology(('starting',), 'up', 'cpu'), ('large',))
        self.assertEqual(next_topology(('expanded',), 'down', 'low'), ('starting',))
        self.assertEqual(next_topology(('starting',), 'down', 'low'), ('small',))
        self.assertEqual(next_topology(('small',), 'down', 'low'), ('small',))

    def test_forbidden_shape_cannot_receive_tier_or_price_authorization(self):
        self.assertIsNone(tier_for_shape(64,256))
        self.assertEqual(tier_for_shape(32,128).name,'bulky')
        for name, limit in [('small','.60'), ('starting','1.25'), ('expanded','1.25'), ('large','3'), ('bulky','4.50')]:
            self.assertFalse(TIERS[name].affordable(limit))
            self.assertFalse(TIERS[name].affordable('NaN'))
        self.assertTrue(TIERS['expanded'].affordable('1.24'))

    def test_measured_pressure_can_expand_single_host_then_reverse(self):
        self.assertEqual(next_topology(('large',),'up','cpu'),('bulky',))
        self.assertEqual(next_topology(('large',),'up','memory'),('bulky',))
        self.assertEqual(next_topology(('bulky',),'down','low'),('large',))
        self.assertEqual(next_topology(('bulky',),'up','queue'),('bulky','small'))
        self.assertEqual(next_topology(('bulky','bulky','large'),'up','cpu'),('bulky','bulky','large'))

    def test_small_host_memory_growth_increases_ram(self):
        for reason in ('memory','oom'):
            self.assertEqual(next_topology(('small',),'up',reason),('expanded',))
        self.assertEqual(next_topology(('small',),'up','cpu'),('starting',))
