import unittest
from lib import autoscaling_policy as policy


class ResourceScalingTests(unittest.TestCase):
    def test_either_resource_high_for_two_five_minute_checks_scales_up(self):
        state = {}
        self.assertEqual(policy.resource_decision([(0.76, .4)], state, 1000), 'wait')
        self.assertEqual(policy.resource_decision([(.4, .76)], state, 1300), 'up')

    def test_both_low_for_two_checks_scales_down_and_equality_holds(self):
        state = {}
        self.assertEqual(policy.resource_decision([(.74, .74)], state, 1000), 'wait')
        self.assertEqual(policy.resource_decision([(.74, .74)], state, 1300), 'down')
        self.assertEqual(policy.resource_decision([(.75, .2)], {}, 1000), 'wait')

    def test_repeated_or_missing_or_mixed_window_does_not_trigger(self):
        state = {}
        policy.resource_decision([(.8, .3)], state, 1000)
        self.assertEqual(policy.resource_decision([(.8, .3)], state, 1000), 'wait')
        self.assertEqual(policy.resource_decision(None, state, 1300), 'wait')
        self.assertEqual(policy.resource_decision([(.8, .3)], state, 1600), 'wait')
        self.assertEqual(policy.resource_decision([(.2, .3)], state, 1900), 'wait')

    def test_one_hot_node_prevents_scale_down_invalid_values_hold(self):
        for samples in ([(.1,.1),(.9,.1)], [(float('nan'),.1)], [(-.1,.1)], [(1.1,.1)]):
            state = {}
            self.assertNotEqual(policy.resource_decision(samples, state, 1000), 'down')
            self.assertNotEqual(policy.resource_decision(samples, state, 1300), 'down')

    def test_short_interval_and_long_gap_do_not_count_as_two_checks(self):
        state = {}
        policy.resource_decision([(.8,.1)], state, 1000)
        self.assertEqual(policy.resource_decision([(.8,.1)], state, 1050), 'wait')
        self.assertEqual(policy.resource_decision([(.8,.1)], state, 2000), 'wait')
