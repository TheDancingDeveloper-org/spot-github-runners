import unittest
from datetime import datetime, timezone
from lib import resource_metrics


def series(metric, instance, values):
    return {'resource': {'labels': {'instance_id': instance, 'project_id': 'p', 'zone': 'z'}},
            'metric': {'type': metric, 'labels': {'state': 'used'}},
            'points': [{'interval': {'endTime': datetime.fromtimestamp(t, timezone.utc).isoformat()},
                        'value': {'doubleValue': v}} for t, v in values]}


class MetricsTests(unittest.TestCase):
    def test_full_fresh_cpu_and_memory_window(self):
        rows=[series(resource_metrics.CPU, '1', [(800,.8),(860,.8),(920,.8),(980,.8)]),
              series(resource_metrics.MEMORY, '1', [(800,40),(860,40),(920,40),(980,40)])]
        self.assertEqual(resource_metrics.normalize(rows, [('1','p','z')], 1120), [(.8,.4)])

    def test_missing_stale_wrong_identity_duplicate_or_nonfinite_blocks(self):
        good = [(800, .8),(860,.8),(920,.8),(980,.8)]
        cpu = series(resource_metrics.CPU,'1',good)
        for vals, ident in (([(800,40)], '1'), ([(0,40),(60,40),(120,40),(180,40)],'1'),
                           ([(800,40),(860,40),(920,40),(980,40)],'2'),
                           ([(980,40)]*4,'1'), ([(800,float('nan')),(860,40),(920,40),(980,40)],'1')):
            self.assertIsNone(resource_metrics.normalize([cpu,series(resource_metrics.MEMORY,ident,vals)], [('1','p','z')],1120))
