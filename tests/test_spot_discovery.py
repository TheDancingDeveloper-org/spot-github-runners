"""Price Spot shapes from one authenticated catalog snapshot, offline."""
import asyncio
import contextlib
import io
import unittest
from unittest.mock import AsyncMock, patch

from lib import spot_finder
from lib import spot_pricing
from lib.compute_api import Auth, Offer


def sku(description, group, usage, regions, dollars):
    units, nanos = divmod(round(dollars * 1_000_000_000), 1_000_000_000)
    return {'description': description, 'serviceRegions': regions,
            'category': {'resourceGroup': group, 'usageType': usage},
            'pricingInfo': [{'pricingExpression': {'usageUnit': 'h', 'tieredRates': [
                {'unitPrice': {'currencyCode': 'USD', 'units': str(units), 'nanos': nanos}}]}}]}


class SpotDiscoveryTests(unittest.TestCase):
    def setUp(self):
        blocker = patch('subprocess.run', side_effect=AssertionError('Unexpected external command'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_component_rates_parse_spot_and_on_demand_core_and_ram(self):
        rates = spot_pricing.parse_component_rates([
            sku('Spot Preemptible C4D Instance Core running in Las Vegas', 'CPU', 'Preemptible', ['us-west4'], .01),
            sku('Spot Preemptible C4D Instance Ram running in Las Vegas', 'RAM', 'Preemptible', ['us-west4'], .001),
            sku('C4D Instance Core running in Las Vegas', 'CPU', 'OnDemand', ['us-west4'], .04),
            sku('Spot Preemptible C4D Instance Local SSD running in Las Vegas', 'LocalSSD', 'Preemptible', ['us-west4'], 9),
        ])
        self.assertEqual(rates, {('spot', 'us-west4', 'c4d', 'CPU'): .01,
                                 ('spot', 'us-west4', 'c4d', 'RAM'): .001,
                                 ('ondemand', 'us-west4', 'c4d', 'CPU'): .04})

    def test_vendor_word_before_instance_does_not_hide_the_series(self):
        rates = spot_pricing.parse_component_rates([
            sku('Spot Preemptible N2D AMD Instance Core running in Salt Lake City', 'CPU', 'Preemptible', ['us-west3'], .002),
            sku('Spot Preemptible C2D AMD Instance Ram running in Salt Lake City', 'RAM', 'Preemptible', ['us-west3'], .0004),
            sku('Spot Preemptible N2D AMD Custom Instance Core running in Salt Lake City', 'CPU', 'Preemptible', ['us-west3'], .001),
            sku('Spot Preemptible N2D AMD Custom Extended Instance Ram running in Salt Lake City', 'RAM', 'Preemptible', ['us-west3'], .0001),
        ])
        self.assertEqual(rates, {('spot', 'us-west3', 'n2d', 'CPU'): .002, ('spot', 'us-west3', 'c2d', 'RAM'): .0004})

    def test_supplied_catalog_rates_price_shapes_without_capacity_history(self):
        machines = [{'name': 'c4d-standard-32', 'zone': 'zones/us-west4-a', 'guestCpus': 32, 'memoryMb': 126976},
                    {'name': 'c4d-standard-32', 'zone': 'zones/us-west4-c', 'guestCpus': 32, 'memoryMb': 126976},
                    # Without a catalog price the manager cannot deploy it, so it is not looked up elsewhere.
                    {'name': 'm3-megamem-64', 'zone': 'zones/us-west4-a', 'guestCpus': 64, 'memoryMb': 999424}]
        rates = {('spot', 'us-west4', 'c4d', 'CPU'): .01, ('spot', 'us-west4', 'c4d', 'RAM'): .001}
        history = AsyncMock(side_effect=AssertionError('capacityHistory must not be queried'))
        with patch.object(spot_finder, 'wait_for_quota_cooldown', AsyncMock()), \
             patch.object(spot_finder, 'list_regions', AsyncMock(return_value=[{'name': 'us-west4'}])), \
             patch.object(spot_finder, 'list_machines', AsyncMock(return_value=(machines, []))), \
             patch.object(spot_finder, 'spot_price', history):
            offers, _, _, errors = asyncio.run(spot_finder.find(
                Auth('token', 'project'), 32, 110, None, 1, catalog_rates=rates))
        self.assertEqual(errors, [])
        self.assertEqual(len(offers), 1)
        self.assertAlmostEqual(offers[0].hourly_usd, .01 * 32 + .001 * 124)
        self.assertEqual(offers[0].zones, ('us-west4-a', 'us-west4-c'))

    def test_cost_per_runner_divides_by_runner_count_not_vcpu_per_runner(self):
        offer = Offer('us-west4', 'c4d-standard-32', 32, 124.0, 1.0)
        # Default: vCPU / 4 = 8 runners, so $24/day and $730/month split eight ways.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            spot_finder.print_offer(offer)
            spot_finder.render_table([offer], None)
        text = out.getvalue()
        self.assertIn('8 runners, 4 vCPU/runner', text)
        self.assertIn('$3.0000/runner/day', text)
        self.assertIn('$91.2500/runner/month', text)
        row = [line for line in text.splitlines() if line.startswith('us-west4 |')][0]
        cells = [cell.strip() for cell in row.split('|')]
        self.assertEqual(cells[-4:], ['8', '4', '$3.0000', '$91.2500'])
        self.assertNotIn('$6.0000', text)

    def test_cost_per_runner_honors_explicit_runner_count(self):
        offer = Offer('us-west4', 'c4d-standard-32', 32, 124.0, 1.0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            spot_finder.print_offer(offer, runners=2)
            spot_finder.render_table([offer], 2)
        text = out.getvalue()
        self.assertIn('2 runners, 16 vCPU/runner', text)
        self.assertIn('$12.0000/runner/day', text)
        self.assertIn('$365.0000/runner/month', text)
        row = [line for line in text.splitlines() if line.startswith('us-west4 |')][0]
        cells = [cell.strip() for cell in row.split('|')]
        self.assertEqual(cells[-4:], ['2', '16', '$12.0000', '$365.0000'])

    def test_cost_per_runner_guards_zero_runners(self):
        offer = Offer('us-west4', 'c4d-standard-32', 32, 124.0, 1.0)
        self.assertEqual(spot_finder.per_runner_usd(24.0, offer, 0), 0.0)
        self.assertEqual(spot_finder.vcpu_per_runner(offer, 0), 0.0)

    def test_small_hosts_default_to_one_whole_runner(self):
        for vcpu, ram in ((2, 8.0), (2, 16.0), (8, 8.0), (6, 24.0)):
            offer = Offer('us-west4', f'shape-{vcpu}', vcpu, ram, 1.0)
            expected = 1.0 if vcpu < 4 or ram <= 8 else float(vcpu // 4)
            self.assertEqual(spot_finder.runner_count(offer, None), expected)
            self.assertLessEqual(spot_finder.per_runner_usd(24.0, offer, None), 24.0)
        offer = Offer('us-west4', 'e2-standard-2', 2, 8.0, 1.0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            spot_finder.print_offer(offer)
        self.assertIn('1 runners, 2 vCPU/runner', out.getvalue())
        self.assertIn('$24.0000/runner/day', out.getvalue())

    def test_capacity_history_waits_out_repeated_resource_not_ready(self):
        api = AsyncMock()
        api.post_json.side_effect = [RuntimeError('HTTP 400 resourceNotReady')] * 4 + [
            {'priceHistory': [{'interval': {'endTime': '1'}, 'listPrice': {'units': '0', 'nanos': 500000000}}]}]
        with patch.object(spot_pricing.asyncio, 'sleep', AsyncMock()) as sleep:
            _, _, hourly = asyncio.run(spot_pricing.spot_price(api, 'project', 'us-west4', 'c4d-standard-32'))
        self.assertEqual(hourly, .5)
        self.assertGreaterEqual(sum(call.args[0] for call in sleep.await_args_list), 10)


if __name__ == '__main__':
    unittest.main()
