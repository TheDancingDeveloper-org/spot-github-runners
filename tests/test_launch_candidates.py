"""Expand ranked offers into zone candidates that preflight checks can launch."""
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from pricing_fixtures import storage_prices_for
from lib.compute_api import Offer
from lib.launch_candidates import LaunchInventory, candidate_key, launchable_candidates
from lib import spot_runner_autoscaler as controller

ALL_DISKS = {'pd-standard', 'pd-balanced', 'pd-ssd', 'hyperdisk-balanced'}
EMERALD = ['Intel Emerald Rapids', 'Intel Sapphire Rapids']


def inventory(**changes):
    base = dict(platforms_by_zone={'region-a': EMERALD, 'region-b': EMERALD},
                disk_types_by_zone={'region-a': ALL_DISKS, 'region-b': ALL_DISKS},
                region_quotas={'region': {'CPUS': (200.0, 0.0), 'C4_CPUS': (200.0, 0.0),
                                          'N2_CPUS': (200.0, 0.0), 'SSD_TOTAL_GB': (500.0, 0.0),
                                          'DISKS_TOTAL_GB': (4096.0, 0.0), 'INSTANCES': (24.0, 0.0),
                                          'PREEMPTIBLE_CPUS': (0.0, 0.0)}},
                global_quotas={'CPUS_ALL_REGIONS': (64.0, 0.0)})
    base.update(changes)
    return LaunchInventory(**base)


def ranked(*offers):
    return controller.eligible_offers(list(offers), Decimal('10'), storage_rates=storage_prices_for(offers))


class LaunchCandidateTests(unittest.TestCase):
    def setUp(self):
        blocker = patch('subprocess.run', side_effect=AssertionError('Unexpected external command'))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.c4 = Offer('region', 'c4-standard-32', 32, 120, .1, zones=('region-a', 'region-b'))

    def test_each_offer_expands_to_one_candidate_per_launchable_zone_in_rank_order(self):
        n2 = Offer('region', 'n2-standard-32', 32, 128, .05, zones=('region-b',))
        selected, skipped = launchable_candidates(
            ranked(self.c4, n2), inventory(platforms_by_zone={'region-a': EMERALD, 'region-b': EMERALD + ['Intel Ice Lake']}), 100)
        self.assertEqual([candidate_key(c) for c in selected], [
            'region-a/c4-standard-32/Intel Emerald Rapids', 'region-b/c4-standard-32/Intel Emerald Rapids',
            'region-b/n2-standard-32/Intel Ice Lake'])
        # Granite is never selectable outside its documented zones, and Cascade Lake is not advertised.
        self.assertTrue(any('Intel Granite Rapids' in reason for reason in skipped))
        self.assertTrue(any('Intel Cascade Lake' in reason for reason in skipped))

    def test_zone_without_the_machine_type_platform_or_disk_type_is_never_a_candidate(self):
        cases = {
            'machine type': dict(offer=replace(self.c4, zones=('region-b',))),
            'platform': dict(platforms_by_zone={'region-a': ['Intel Sapphire Rapids'], 'region-b': EMERALD}),
            'disk type': dict(disk_types_by_zone={'region-a': {'pd-standard'}, 'region-b': ALL_DISKS}),
            'zone status': dict(platforms_by_zone={'region-b': EMERALD}),
        }
        for name, change in cases.items():
            with self.subTest(name):
                offer = change.pop('offer', self.c4)
                selected, _ = launchable_candidates(ranked(offer), inventory(**change), 100)
                self.assertEqual([c.zone for c in selected], ['region-b'])

    def test_insufficient_quota_removes_every_zone_in_the_region(self):
        quotas = inventory().region_quotas['region']
        cases = {
            'family CPUs': {**quotas, 'C4_CPUS': (24.0, 0.0)},
            'regional CPUs': {**quotas, 'CPUS': (40.0, 10.0)},
            'instances': {**quotas, 'INSTANCES': (2.0, 2.0)},
            'hyperdisk has no quota metric but SSD is irrelevant': None,
        }
        for name, region_quota in cases.items():
            with self.subTest(name):
                if region_quota is None:
                    selected, _ = launchable_candidates(
                        ranked(self.c4), inventory(region_quotas={'region': {**quotas, 'SSD_TOTAL_GB': (0.0, 0.0)}}), 100)
                    self.assertEqual(len(selected), 2)
                    continue
                selected, skipped = launchable_candidates(
                    ranked(self.c4), inventory(region_quotas={'region': region_quota}), 100)
                self.assertEqual(selected, [])
                self.assertTrue(any('quota' in reason for reason in skipped))
        selected, _ = launchable_candidates(ranked(self.c4), inventory(global_quotas={'CPUS_ALL_REGIONS': (64.0, 40.0)}), 100)
        self.assertEqual(selected, [])

    def test_disk_quota_uses_the_selected_disk_type_metric(self):
        n2 = Offer('region', 'n2-standard-32', 32, 128, .05, zones=('region-a',))
        platforms = {'region-a': ['Intel Ice Lake']}
        quotas = inventory().region_quotas['region']
        selected, _ = launchable_candidates(ranked(n2), inventory(platforms_by_zone=platforms,
            region_quotas={'region': {**quotas, 'DISKS_TOTAL_GB': (4096.0, 4060.0)}}), 100)
        self.assertEqual(selected, [])  # The 40 GiB standard boot does not fit.
        selected, _ = launchable_candidates(ranked(n2), inventory(platforms_by_zone=platforms,
            region_quotas={'region': {**quotas, 'SSD_TOTAL_GB': (0.0, 0.0)}}), 100)
        self.assertEqual(selected, [])  # Balanced data requires 100 GiB SSD quota.

    def test_spot_vms_use_preemptible_cpu_quota_when_the_project_has_one(self):
        quotas = {**inventory().region_quotas['region'], 'CPUS': (0.0, 0.0), 'PREEMPTIBLE_CPUS': (64.0, 0.0)}
        selected, _ = launchable_candidates(ranked(self.c4), inventory(region_quotas={'region': quotas}), 100)
        self.assertEqual(len(selected), 2)

    def test_total_cap_includes_storage(self):
        # $9/day compute plus storage must not qualify under a $10/day total cap.
        offer = Offer('region', 'n2-standard-32', 32, 128, 9 / 24, zones=('region-a',))
        rates = {'region': {'pd-standard:capacity': Decimal('.33')}}  # About $1.52/day for 140 GiB.
        self.assertEqual(controller.eligible_offers([offer], Decimal('10'), storage_rates=rates), [])


if __name__ == '__main__':
    unittest.main()


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        blocker = patch('subprocess.run', side_effect=AssertionError('Unexpected external command'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_inventory_reads_zones_disk_types_and_quotas_from_gcloud(self):
        import json
        from types import SimpleNamespace
        outputs = {
            'zones': [{'name': 'region-a', 'status': 'UP', 'availableCpuPlatforms': EMERALD},
                      {'name': 'region-c', 'status': 'DOWN', 'availableCpuPlatforms': EMERALD}],
            'disk-types': [{'name': 'pd-ssd', 'zone': 'https://x/zones/region-a'},
                           {'name': 'pd-ssd', 'region': 'https://x/regions/region'},
                           {'name': 'hyperdisk-balanced', 'zone': 'https://x/zones/region-a'}],
            'regions': [{'name': 'region', 'quotas': [{'metric': 'CPUS', 'limit': 200.0, 'usage': 32.0}]}],
            'project-info': {'quotas': [{'metric': 'CPUS_ALL_REGIONS', 'limit': 64.0, 'usage': 32.0}]},
        }
        def gcloud(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout=json.dumps(outputs[command[2]]), stderr='')
        with patch('lib.spot_runner_autoscaler.subprocess.run', side_effect=gcloud):
            found = controller.load_launch_inventory('project')
        self.assertEqual(found, LaunchInventory(
            platforms_by_zone={'region-a': EMERALD},
            disk_types_by_zone={'region-a': {'pd-ssd', 'hyperdisk-balanced'}},
            region_quotas={'region': {'CPUS': (200.0, 32.0)}},
            global_quotas={'CPUS_ALL_REGIONS': (64.0, 32.0)}))

    def test_discovery_logs_ranked_candidates_and_skip_reasons(self):
        c4 = Offer('region', 'c4-standard-32', 32, 120, .1, zones=('region-a', 'region-b'))
        with patch('lib.spot_runner_autoscaler.get_auth'), \
             patch('lib.spot_runner_autoscaler.catalog_skus', return_value=()), \
             patch('lib.spot_runner_autoscaler.find', return_value=([c4], 1, 1, ['HTTP 400 resourceNotReady'])), \
             patch('lib.spot_runner_autoscaler.storage_rates', return_value=storage_prices_for([c4])['region']), \
             patch('lib.spot_runner_autoscaler.load_launch_inventory', return_value=inventory()), \
             self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
            candidates = controller.discover_offers('project', Decimal('10'), disk_size_gb=100)
        self.assertEqual([candidate_key(c) for c in candidates],
                         ['region-a/c4-standard-32/Intel Emerald Rapids', 'region-b/c4-standard-32/Intel Emerald Rapids'])
        text = '\n'.join(logs.output)
        self.assertIn('1 Spot price lookup(s) failed', text)
        self.assertIn('2 launchable zone candidates', text)
        self.assertIn('#1 region-a c4-standard-32 Intel Emerald Rapids', text)
        self.assertIn('Intel Granite Rapids in region', text)
