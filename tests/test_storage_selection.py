"""Choose storage by compatible total cost rather than capacity rate alone."""
import unittest
from decimal import Decimal
from unittest.mock import patch

from lib import runner_manager as manager
from lib import spot_runner_autoscaler as controller


class StorageSelectionTests(unittest.TestCase):
    def test_old_family_defaults_to_standard_pd(self):
        args = manager.args_parser().parse_args(['--max-cost', '.2'])
        manager.select_sizing(args)
        self.assertEqual((args.boot_disk_type, args.disk_type), ('pd-standard', 'pd-standard'))

    def test_cheapest_selection_uses_total_cost_and_size_constraints(self):
        from lib.storage_selection import cheapest_storage
        rates = {'hyperdisk-balanced:capacity': Decimal('.112'),
                 'hyperdisk-throughput:capacity': Decimal('.007'),
                 'hyperdisk-throughput:throughput': Decimal('.35')}
        boot, data, monthly = cheapest_storage('n4-standard-32', 100, rates)
        self.assertEqual((boot, data, monthly), ('hyperdisk-balanced', 'hyperdisk-balanced', Decimal('15.680')))
        boot, data, monthly = cheapest_storage('n4-standard-32', 2048, rates)
        self.assertEqual((boot, data), ('hyperdisk-balanced', 'hyperdisk-throughput'))
        self.assertEqual(monthly, Decimal('25.816'))
        rates['hyperdisk-throughput:throughput'] = Decimal('20')
        self.assertEqual(cheapest_storage('n4-standard-32', 2048, rates)[1], 'hyperdisk-balanced')

    def test_cheapest_selection_fails_when_no_compatible_price_exists(self):
        from lib.storage_selection import cheapest_storage
        with self.assertRaisesRegex(RuntimeError, 'price'):
            cheapest_storage('n4-standard-32', 100, {'pd-standard:capacity': Decimal('.04')})
        with self.assertRaisesRegex(RuntimeError, 'price'):
            cheapest_storage('n2-standard-32', 100, {'hyperdisk-balanced:capacity': Decimal('.08')})

    def test_multi_region_pd_skus_without_a_location_suffix_are_priced(self):
        from lib import storage_selection
        def sku(description, regions, dollars):
            return {'description': description, 'serviceRegions': regions, 'pricingInfo': [{'pricingExpression': {
                'usageUnit': 'GiBy.mo', 'tieredRates': [{'unitPrice': {'currencyCode': 'USD', 'units': '0',
                                                                       'nanos': int(dollars * 1e9)}}]}}]}
        skus = (sku('Storage PD Capacity', ['us-central1', 'europe-west1'], .04),
                sku('Balanced PD Capacity', ['europe-west1'], .1),
                sku('Regional Storage PD Capacity', ['europe-west1'], .08),
                sku('Storage PD Capacity in Belgium', ['europe-west2'], .05))
        with patch.object(storage_selection, 'catalog_skus', return_value=skus):
            rates = storage_selection.storage_rates('europe-west1')
        self.assertEqual(rates, {'pd-standard:capacity': Decimal('0.04'), 'pd-balanced:capacity': Decimal('0.1')})

    def test_missing_disk_price_skips_only_that_disk_type(self):
        from lib.storage_selection import cheapest_storage
        rates = {'pd-balanced:capacity': Decimal('.10'), 'pd-ssd:capacity': Decimal('.17')}
        boot, data, monthly = cheapest_storage('n2-standard-32', 100, rates)
        self.assertEqual((boot, data, monthly), ('pd-balanced', 'pd-balanced', Decimal('14.00')))

    def test_missing_optional_throughput_sku_keeps_priced_baseline_choice(self):
        from lib.storage_selection import cheapest_storage
        optional_rates = [{}, {'hyperdisk-throughput:capacity': Decimal('.007')},
                          {'hyperdisk-throughput:throughput': Decimal('.35')}]
        for optional in optional_rates:
            rates = {'hyperdisk-balanced:capacity': Decimal('.112'), **optional}
            boot, data, monthly = cheapest_storage('n4-standard-32', 2048, rates)
            self.assertEqual((boot, data), ('hyperdisk-balanced', 'hyperdisk-balanced'))
            self.assertEqual(monthly, Decimal('233.856'))

    def test_existing_host_total_cap_uses_retained_boot_disk_cost(self):
        from lib.runner_fleet import GceVm
        import json
        args = manager.args_parser().parse_args(['--max-cost', '.4', '--existing-instance',
                                                '--cheapest-storage', '--max-total-daily-usd', '5'])
        manager.select_sizing(args)
        args.deployment_vm = GceVm(args.instance_name, 'asia-south2-a', 'RUNNING', args.instance_type, True,
            disks=({'boot': True, 'diskType': 'pd-ssd', 'diskSizeGb': 40},
                   {'boot': False, 'deviceName': args.disk_device_name, 'diskType': 'pd-standard', 'diskSizeGb': 100}))
        rates = {'pd-standard:capacity': Decimal('.04'), 'pd-balanced:capacity': Decimal('.1'), 'pd-ssd:capacity': Decimal('1')}
        with patch.object(manager, 'gcloud', return_value=json.dumps({'guestCpus': 32, 'memoryMb': 131072})), \
             patch.object(manager, 'spot_rates', return_value=(Decimal('.005'), Decimal(0))), \
             patch.object(manager, 'storage_rates', return_value=rates):
            with self.assertRaisesRegex(RuntimeError, 'Total compute/storage price'):
                manager.verify_price_and_capacity(args, 'asia-south2-a')

    def test_zone_map_is_rebuilt_from_fresh_provider_inventory(self):
        responses = [{'name': 'zone-a', 'status': 'UP', 'availableCpuPlatforms': ['Intel Ice Lake']},
                     {'name': 'zone-b', 'status': 'DOWN', 'availableCpuPlatforms': ['AMD Turin']}]
        with patch('lib.spot_runner_autoscaler.subprocess.run') as external:
            import json
            external.return_value.stdout = json.dumps(responses)
            first = controller.load_cpu_platform_map('project')
            responses[0]['availableCpuPlatforms'] = ['Intel Ice Lake', 'AMD Turin']
            external.return_value.stdout = json.dumps(responses)
            second = controller.load_cpu_platform_map('project')
        self.assertEqual(first, {'zone-a': ['Intel Ice Lake']})
        self.assertEqual(second, {'zone-a': ['Intel Ice Lake', 'AMD Turin']})

    def test_zone_map_rejects_incomplete_platform_information(self):
        from types import SimpleNamespace
        with patch('lib.spot_runner_autoscaler.subprocess.run', return_value=SimpleNamespace(stdout='[{"name":"zone-a","status":"UP"}]')):
            with self.assertRaisesRegex(RuntimeError, 'platform'):
                controller.load_cpu_platform_map('project')

    def test_selection_compares_total_cost_and_applies_total_cap(self):
        from lib.compute_api import Offer
        offers = [Offer('cheap-compute', 'n4-standard-32', 32, 128, .15),
                  Offer('cheap-total', 'n4-standard-32', 32, 128, .16)]
        rates = {'cheap-compute': {'hyperdisk-balanced:capacity': Decimal('.24')},
                 'cheap-total': {'hyperdisk-balanced:capacity': Decimal('.08')}}
        selected = controller.eligible_offers(offers, Decimal('5'), storage_rates=rates)
        self.assertEqual([o.region for o in selected], ['cheap-total', 'cheap-compute'])
        selected = controller.eligible_offers(offers, Decimal('4.5'), storage_rates=rates)
        self.assertEqual([o.region for o in selected], ['cheap-total'])
        self.assertEqual(selected[0].total_daily_usd, Decimal('3.84') + Decimal('11.2') * 24 / 730)

    def test_running_vm_price_uses_actual_disk_performance(self):
        from lib.runner_fleet import GceVm
        from lib.price_guard import check_running_prices
        from types import SimpleNamespace
        vm = GceVm('oxidex-runners-test', 'region-a', 'RUNNING', 'n4-standard-32', True,
                   disks=({'diskType': 'hyperdisk-balanced', 'diskSizeGb': '100',
                           'provisionedIops': '4000', 'provisionedThroughput': '150'},))
        rates = {'hyperdisk-balanced:capacity': Decimal('.1'),
                 'hyperdisk-balanced:iops': Decimal('.005'),
                 'hyperdisk-balanced:throughput': Decimal('.04')}
        with patch('lib.price_guard.spot_rates', return_value=(Decimal('.006'), Decimal(0))), \
             patch('lib.price_guard.storage_rates', return_value=rates), \
             patch('lib.price_guard.subprocess.run', return_value=SimpleNamespace(stdout='{"guestCpus":32,"memoryMb":131072}')):
            self.assertEqual(check_running_prices([vm], 'project', Decimal('5')), [vm])

    def test_regional_storage_is_not_mispriced_as_zonal_storage(self):
        from lib.storage_selection import observed_storage_monthly
        with self.assertRaisesRegex(RuntimeError, 'Regional'):
            observed_storage_monthly([{'source': 'projects/project/regions/region/disks/data',
                                       'diskType': 'pd-balanced', 'diskSizeGb': 100}],
                                      {'pd-balanced:capacity': Decimal('.1')})

    def test_total_cap_preserves_explicit_storage_without_optimization(self):
        import json
        args = manager.args_parser().parse_args(['--max-cost', '.4', '--disk-type', 'pd-ssd',
                                                '--max-total-daily-usd', '10'])
        manager.select_sizing(args)
        with patch.object(manager, 'gcloud', return_value=json.dumps({'guestCpus': 32, 'memoryMb': 131072})), \
             patch.object(manager, 'spot_rates', return_value=(Decimal('.005'), Decimal(0))), \
             patch.object(manager, 'storage_rates', return_value={'pd-standard:capacity': Decimal('.04'),
                                                               'pd-ssd:capacity': Decimal('.17')}):
            manager.verify_price_and_capacity(args, 'asia-south2-a')
        self.assertEqual(args.disk_type, 'pd-ssd')
        self.assertEqual(args.storage_monthly_usd, Decimal('18.6'))

    def test_reused_unattached_disk_must_match_priced_size_and_performance(self):
        from types import SimpleNamespace
        import json
        cases = [('n2-standard-32', {'type': 'pd-standard', 'sizeGb': '5000'}, 'size'),
                 ('n4-standard-32', {'type': 'hyperdisk-balanced', 'sizeGb': '100',
                                     'provisionedIops': '4000', 'provisionedThroughput': '140'}, 'performance')]
        for shape, disk, reason in cases:
            args = manager.args_parser().parse_args(['--max-cost', '.4', '--instance-type', shape])
            manager.select_sizing(args)
            responses = [SimpleNamespace(returncode=1, stdout=''),
                         SimpleNamespace(returncode=0, stdout=json.dumps(disk))]
            with patch.object(manager, 'cleanup_orphan_runner_disks'), \
                 patch.object(manager.subprocess, 'run', side_effect=responses), \
                 patch.object(manager, 'gcloud') as cloud, \
                 self.assertRaisesRegex(RuntimeError, reason):
                manager.apply_startup(args, 'asia-south2-a', 'mock script')
            cloud.assert_not_called()

    def test_missing_storage_prices_exclude_candidate_instead_of_counting_as_zero(self):
        from lib.compute_api import Offer
        self.assertEqual(controller.eligible_offers([Offer('region', 'n4-standard-32', 32, 128, .1)],
                                                    Decimal('5'), storage_rates={}), [])

    def test_catalog_parses_component_hour_units_and_standard_pd_skus(self):
        from lib.billing_catalog import unit_price
        from lib.storage_selection import storage_rates
        def sku(description, unit, nanos):
            return {'description': description, 'serviceRegions': ['region'],
                    'pricingInfo': [{'pricingExpression': {'usageUnit': unit,
                     'tieredRates': [{'startUsageAmount': 0, 'unitPrice': {'currencyCode': 'USD', 'units': '0', 'nanos': nanos}}]}}]}
        self.assertEqual(unit_price(sku('cpu', 'vCPU.h', 5000000), 'h'), Decimal('.005'))
        rows = [sku('Storage PD Capacity in Region', 'GiBy.mo', 40000000)]
        with patch('lib.storage_selection.catalog_skus', return_value=rows):
            self.assertEqual(storage_rates('region'), {'pd-standard:capacity': Decimal('.04')})

    def test_throughput_skus_accept_plain_and_compound_monthly_units(self):
        from lib.storage_selection import storage_rates
        for unit in ['mo', 'MiBy/s.mo']:
            rows = [{'description': description, 'serviceRegions': ['region'],
                     'pricingInfo': [{'pricingExpression': {'usageUnit': unit,
                      'tieredRates': [{'startUsageAmount': 0,
                        'unitPrice': {'currencyCode': 'USD', 'units': '0', 'nanos': nanos}}]}}]}
                    for description, nanos in [('Hyperdisk Balanced Throughput in Region', 56000000),
                                                ('Hyperdisk Throughput Throughput in Region', 350000000)]]
            with patch('lib.storage_selection.catalog_skus', return_value=rows):
                rates = storage_rates('region')
            self.assertEqual(rates, {'hyperdisk-balanced:throughput': Decimal('.056'),
                                     'hyperdisk-throughput:throughput': Decimal('.35')})


if __name__ == '__main__':
    unittest.main()
