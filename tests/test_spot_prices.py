"""Catalog-label, shape-selection, and pricing regressions with no network."""
import contextlib
import csv
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest.mock import patch


class SpotPriceTests(unittest.TestCase):
    def query(self, labels, shapes):
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tools/spot_prices.py'), run_name='price_control')
        skus = []
        for label, units in labels:
            for kind in ('Core', 'Ram'):
                skus.append({'category': {'usageType': 'Preemptible'},
                    'description': 'Spot Preemptible ' + label + ' Instance ' + kind + ' running in test',
                    'serviceRegions': ['us-test1'], 'pricingInfo': [{'effectiveTime': '2020-01-01T00:00:00Z',
                    'pricingExpression': {'usageUnit': 'h', 'tieredRates': [{'startUsageAmount': 0,
                    'unitPrice': {'currencyCode': 'USD', 'units': str(units), 'nanos': 0}}]}}]})
        inventory = {'items': {'zones/us-test1-a': {'machineTypes': [
            dict(shape, zone='us-test1-a') for shape in shapes]}}}
        responses = [io.BytesIO(json.dumps(value).encode()) for value in ({'skus': skus}, inventory)]
        def gcloud(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout='synthetic-token' if 'print-access-token' in argv else 'synthetic-project', stderr='')
        output, diagnostic = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['spot_prices.py', '--min-cpu', '1', '--min-ram', '1', '--price-per-day', '1000000']), patch('subprocess.run', side_effect=gcloud), patch('urllib.request.urlopen', side_effect=responses), contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
            self.assertEqual(module['main'](), 0)
        return list(csv.DictReader(io.StringIO(output.getvalue()))), diagnostic.getvalue()

    def test_shared_core_shapes_are_reported_unsupported(self):
        rows, diagnostic = self.query([('E2', 1)], [
            {'name': 'e2-micro', 'guestCpus': 2, 'memoryMb': 1024},
            {'name': 'g1-small', 'guestCpus': 1, 'memoryMb': 1740},
            {'name': 'e2-standard-4', 'guestCpus': 4, 'memoryMb': 16384}])
        self.assertEqual([r['machine_type'] for r in rows], ['e2-standard-4'])
        self.assertIn('2 additional shapes lack supported', diagnostic)
        self.assertEqual(rows[0]['USD_day'], '480')

    def test_n1_predefined_component_skus_are_priced(self):
        rows, _ = self.query([('N1 Predefined', 1)], [
            {'name': 'n1-standard-4', 'guestCpus': 4, 'memoryMb': 15360}])
        self.assertEqual([r['machine_type'] for r in rows], ['n1-standard-4'])
        self.assertEqual(rows[0]['USD_day'], '456')

    def test_descriptive_c2_and_m3_catalog_labels_are_priced(self):
        rows, _ = self.query([('Compute optimized', 1), ('M3 Memory-optimized', 1)], [
            {'name': 'c2-standard-4', 'guestCpus': 4, 'memoryMb': 16384},
            {'name': 'm3-ultramem-32', 'guestCpus': 32, 'memoryMb': 976 * 1024}])
        self.assertEqual([r['machine_type'] for r in rows], ['c2-standard-4', 'm3-ultramem-32'])
        self.assertEqual([r['USD_day'] for r in rows], ['480', '24192'])

    def test_obsolete_and_deleted_are_excluded_but_deprecated_is_retained(self):
        rows, _ = self.query([('E2', 1)], [
            {'name': 'e2-standard-4', 'guestCpus': 4, 'memoryMb': 16384, 'deprecated': {'state': 'OBSOLETE'}},
            {'name': 'e2-highmem-2', 'guestCpus': 2, 'memoryMb': 16384, 'deprecated': {'state': 'DELETED'}},
            {'name': 'e2-highcpu-4', 'guestCpus': 4, 'memoryMb': 4096, 'deprecated': {'state': 'DEPRECATED'}}])
        self.assertEqual([r['machine_type'] for r in rows], ['e2-highcpu-4'])

    def test_specific_machine_sku_wins_over_generic_family(self):
        rows, _ = self.query([('M4', 1), ('M4Ultramem224', 2)], [
            {'name': 'm4-ultramem-224', 'guestCpus': 224, 'memoryMb': 6144 * 1024}])
        self.assertEqual(rows[0]['USD_day'], '305664')

    def test_custom_shape_does_not_use_predefined_rates(self):
        rows, diagnostic = self.query([('N2', 1), ('N2 Custom', 2)], [
            {'name': 'n2-custom-4-16384', 'guestCpus': 4, 'memoryMb': 16384}])
        self.assertEqual(rows, [])
        self.assertIn('1 additional shapes lack supported', diagnostic)

    def test_memory_optimized_and_legacy_aliases_use_memory_rates(self):
        rows, _ = self.query([('Memory-optimized', 1), ('N1 Predefined', 9)], [
            {'name': 'm1-ultramem-40', 'guestCpus': 40, 'memoryMb': 961 * 1024},
            {'name': 'n1-ultramem-40', 'guestCpus': 40, 'memoryMb': 961 * 1024},
            {'name': 'm2-ultramem-208', 'guestCpus': 208, 'memoryMb': 5888 * 1024}])
        self.assertEqual({r['machine_type']: r['USD_day'] for r in rows},
                         {'m1-ultramem-40': '24024', 'n1-ultramem-40': '24024',
                          'm2-ultramem-208': '146304'})

    def test_partial_specific_rate_does_not_fall_back_to_family(self):
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tools/spot_prices.py'), run_name='price_control')
        from decimal import Decimal
        rates = {('r', 'm4', 'core'): Decimal(1), ('r', 'm4', 'ram'): Decimal(1),
                 ('r', 'm4ultramem224', 'core'): Decimal(2)}
        self.assertIsNone(module['component_rates']('r', 'm4-ultramem-224', rates))

    def test_ambiguous_components_exclude_only_affected_offers(self):
        rows, diagnostic = self.query([('Memory-optimized', 1), ('Memory-optimized', 2), ('E2', 1)], [
            {'name': 'm1-ultramem-40', 'guestCpus': 40, 'memoryMb': 961 * 1024},
            {'name': 'e2-standard-4', 'guestCpus': 4, 'memoryMb': 16384}])
        self.assertEqual([r['machine_type'] for r in rows], ['e2-standard-4'])
        self.assertIn('2 catalog components have conflicting prices', diagnostic)
        self.assertIn('1 additional shapes lack supported', diagnostic)


if __name__ == '__main__':
    unittest.main()
