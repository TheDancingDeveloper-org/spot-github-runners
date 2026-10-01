"""Exercise billing-export totals and launch logging without cloud requests."""
from contextlib import ExitStack
import unittest
from decimal import Decimal
from unittest.mock import patch

from lib import project_costs


class ProjectCostTests(unittest.TestCase):
    def result(self):
        return {'jobComplete': True, 'rows': [{'f': [{'v': v} for v in
                ['USD', '1.12', '2.12', '7.89', '8.89', '30.45', '31.45', '2026-09-30T23:00:00Z', '2026-09-01T00:00:00Z']]}]}

    def test_query_uses_all_services_net_credits_and_rolling_windows(self):
        calls = []
        def request(method, path, token, body=None):
            calls.append((method, path, body))
            return {'location': 'US'} if method == 'GET' else self.result()
        with patch.object(project_costs, '_request', side_effect=request), \
             patch.object(project_costs, '_access_token', return_value='test-token'):
            totals = project_costs.project_cost_totals('homelab-424523', 'billing.costs.gcp_billing_export_v1_ABC')
        query = calls[1][2]
        self.assertEqual(query['queryParameters'][0]['parameterValue']['value'], 'homelab-424523')
        self.assertIn('UNNEST(credits)', query['query'])
        for interval in ['24 HOUR', '7 DAY', '30 DAY']:
            self.assertIn(interval, query['query'])
        self.assertNotIn('service.id', query['query'])
        self.assertFalse(query['useLegacySql'])
        self.assertEqual(query['maximumBytesBilled'], str(1024 ** 3))
        self.assertEqual(totals.amounts, (Decimal('1.12'), Decimal('7.89'), Decimal('30.45')))

    def test_missing_export_does_not_access_cloud_or_log_zero(self):
        with patch.object(project_costs, 'project_cost_totals') as query, \
             self.assertLogs('spot_runner_autoscaler', level='WARNING') as logs:
            project_costs.log_project_costs('homelab', None)
        query.assert_not_called()
        self.assertIn('unavailable', logs.output[0])
        self.assertNotIn('$0', logs.output[0])

    def test_query_errors_are_one_line_and_do_not_abort_launch(self):
        with patch.object(project_costs, 'project_cost_totals', side_effect=RuntimeError('denied\ntry later')), \
             self.assertLogs('spot_runner_autoscaler', level='WARNING') as logs:
            project_costs.log_project_costs('homelab', 'billing.costs.table')
        self.assertNotIn('\n', logs.output[0])

    def test_totals_log_all_windows_and_export_time(self):
        with patch.object(project_costs, '_request', side_effect=[{'location': 'US'}, self.result()]), \
             patch.object(project_costs, '_access_token', return_value='test-token'), \
             self.assertLogs('spot_runner_autoscaler', level='INFO') as logs:
            project_costs.log_project_costs('homelab', 'billing.costs.table')
        for value in ['last 24h $1.12', 'last 7d $7.89', 'last 30d $30.45', '2026-09-30T23:00:00Z', 'provisional']:
            self.assertIn(value, logs.output[0])

    def test_invalid_table_and_empty_or_mixed_currency_data_fail(self):
        with self.assertRaisesRegex(RuntimeError, 'project.dataset.table'):
            project_costs.project_cost_totals('homelab', 'table`; DROP TABLE x')
        empty = self.result()
        empty['rows'][0]['f'][7]['v'] = None
        mixed = self.result()
        mixed['rows'].append(mixed['rows'][0])
        for response in [empty, mixed, {'jobComplete': True, 'errors': [{'message': 'denied'}]}]:
            with patch.object(project_costs, '_request', side_effect=[{'location': 'US'}, response]), \
                 patch.object(project_costs, '_access_token', return_value='test-token'), \
                 self.assertRaises(RuntimeError):
                project_costs.project_cost_totals('homelab', 'billing.costs.table')

    def test_incomplete_query_is_polled_in_its_location(self):
        pending = {'jobComplete': False, 'jobReference': {'projectId': 'homelab', 'jobId': 'job', 'location': 'EU'}}
        with patch.object(project_costs, '_request', side_effect=[{'location': 'EU'}, pending, self.result()]) as request, \
             patch.object(project_costs, '_access_token', return_value='test-token'):
            project_costs.project_cost_totals('homelab', 'billing.costs.table')
        self.assertIn('location=EU', request.call_args.args[1])

    def test_export_partition_is_pruned_without_excluding_delayed_records(self):
        for partition, expected in [({'field': 'export_time', 'type': 'DAY'}, 'AND export_time >='),
                                    ({'type': 'DAY'}, 'AND (_PARTITIONTIME >='),
                                    ({'field': 'usage_start_time', 'type': 'DAY'}, 'AND usage_start_time >=')]:
            with patch.object(project_costs, '_request', side_effect=[{'location': 'US', 'timePartitioning': partition}, self.result()]) as request, \
                 patch.object(project_costs, '_access_token', return_value='test-token'):
                project_costs.project_cost_totals('homelab', 'billing.costs.table')
            query = request.call_args.args[3]['query']
            self.assertIn(expected, query)
            if 'export_time' in expected:
                self.assertIn('INTERVAL 31 DAY', query)
                self.assertNotIn('export_time <=', query)

    def test_manager_logs_history_before_provisioning(self):
        from lib import runner_manager as manager
        events = []
        def provision(*args):
            events.append('provision')
            raise RuntimeError('stop mock deployment')
        with ExitStack() as mocks:
            mocks.enter_context(patch('sys.argv', ['manager', '--max-cost', '.4', '--apply',
                               '--billing-export-table', 'billing.costs.table']))
            for name in ['configure_logging', 'load_env', 'remove_startup_metadata_if_vm_exists']:
                mocks.enter_context(patch.object(manager, name))
            mocks.enter_context(patch.object(manager, 'verify_price_and_capacity', return_value=Decimal('.2')))
            mocks.enter_context(patch.object(manager, 'verify_runner_group', return_value={'name': 'group'}))
            mocks.enter_context(patch.object(manager, 'run', return_value='token'))
            mocks.enter_context(patch.object(manager, 'render_startup', return_value='script'))
            costs = mocks.enter_context(patch.object(manager, 'log_project_costs', side_effect=lambda *args: events.append('costs')))
            mocks.enter_context(patch.object(manager, 'apply_startup', side_effect=provision))
            with self.assertRaisesRegex(RuntimeError, 'stop mock deployment'):
                manager.main()
        costs.assert_called_once_with('homelab-424523', 'billing.costs.table')
        self.assertEqual(events, ['costs', 'provision'])


if __name__ == '__main__':
    unittest.main()
