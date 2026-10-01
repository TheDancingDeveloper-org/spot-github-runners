"""Read reported project spend through the BigQuery billing export API."""
import json
import logging
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote, urlencode

from .log_format import error_detail

LOG = logging.getLogger('spot_runner_autoscaler')


@dataclass(frozen=True)
class ProjectCosts:
    currency: str
    amounts: tuple[Decimal, ...]
    before_credits: tuple[Decimal, ...]
    exported_at: str
    earliest_usage: str


def _access_token():
    token = subprocess.run(['gcloud', 'auth', 'print-access-token'], check=True,
                           text=True, capture_output=True, timeout=30).stdout.strip()
    if not token:
        raise RuntimeError('No credentials for billing export lookup')
    return token


def _request(method, path, token, body=None):
    request = urllib.request.Request('https://bigquery.googleapis.com/bigquery/v2/' + path,
        method=method, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def project_cost_totals(project: str, table: str) -> ProjectCosts:
    if not re.fullmatch(r'[a-z][a-z0-9-]*\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*', table):
        raise RuntimeError('Billing export table must be project.dataset.table')
    source_project, dataset, name = table.split('.')
    token = _access_token()
    metadata = _request('GET', f'projects/{source_project}/datasets/{dataset}/tables/{name}', token)
    location = metadata['location']
    partition_filter = ''
    partitioning = metadata.get('timePartitioning')
    if partitioning:
        # Usage is exported after it occurs. Include an extra partition day
        # and no upper bound so delayed exports still contribute to totals.
        field = partitioning.get('field')
        if field == 'export_time':
            partition_filter = 'AND export_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 31 DAY)'
        elif not field:
            partition_filter = 'AND (_PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 31 DAY) OR _PARTITIONTIME IS NULL)'
    # Project-specific usage totals across every service. Cast before summing
    # to avoid accumulating FLOAT64 monetary rounding errors.
    sums = []
    for interval in ['24 HOUR', '7 DAY', '30 DAY']:
        for amount in ['net', 'gross']:
            sums.append(f'SUM(IF(usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {interval}), {amount}, 0))')
    query = f"""WITH usage AS (
      SELECT currency, usage_start_time, export_time,
        CAST(cost AS NUMERIC) AS gross,
        CAST(cost AS NUMERIC) + IFNULL((SELECT SUM(CAST(c.amount AS NUMERIC)) FROM UNNEST(credits) c), 0) AS net
      FROM `{table}`
      WHERE project.id = @project_id
        AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
        AND usage_start_time <= CURRENT_TIMESTAMP()
        {partition_filter}
    ) SELECT currency, {', '.join(sums)},
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', MAX(export_time)),
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', MIN(usage_start_time))
      FROM usage GROUP BY currency"""
    response = _request('POST', f'projects/{quote(project, safe="")}/queries', token,
        {'query': query, 'useLegacySql': False, 'location': location,
         'timeoutMs': 10000, 'maximumBytesBilled': str(1024 ** 3),
         'parameterMode': 'NAMED', 'queryParameters': [
             {'name': 'project_id', 'parameterType': {'type': 'STRING'},
              'parameterValue': {'value': project}}]})
    deadline = time.monotonic() + 30
    while not response.get('jobComplete') and not response.get('errors'):
        if time.monotonic() >= deadline:
            raise RuntimeError('Billing export query is still running; totals unavailable for this launch')
        reference = response['jobReference']
        path = f'projects/{quote(reference["projectId"], safe="")}/queries/{quote(reference["jobId"], safe="")}?'
        response = _request('GET', path + urlencode({'location': reference.get('location', location), 'timeoutMs': 10000}), token)
    if response.get('errors'):
        raise RuntimeError(error_detail(response['errors']))
    rows = response.get('rows', [])
    if len(rows) != 1:
        raise RuntimeError('Billing export has no recent project usage or contains multiple currencies')
    values = [field.get('v') for field in rows[0]['f']]
    if len(values) != 9 or not values[0] or not values[7] or not values[8]:
        raise RuntimeError('Billing export has no usable project cost history')
    amounts = tuple(Decimal(str(value)) for value in values[1:7])
    if not all(amount.is_finite() for amount in amounts):
        raise RuntimeError('Billing export returned invalid monetary values')
    return ProjectCosts(values[0], amounts[::2], amounts[1::2], values[7], values[8])


def log_project_costs(project: str, table: str | None) -> None:
    if not table:
        LOG.warning('Project costs for %s: last 24h/7d/30d unavailable; configure GCP_BILLING_EXPORT_TABLE (project.dataset.table)', project)
        return
    try:
        costs = project_cost_totals(project, table)
        symbol = '$' if costs.currency == 'USD' else ''
        windows = '; '.join(f'last {label} {symbol}{net:.2f} after credits ({symbol}{gross:.2f} before credits)'
                            for label, net, gross in zip(['24h', '7d', '30d'], costs.amounts, costs.before_credits))
        LOG.info('Project costs for %s: %s; %s; latest export %s; earliest usage in window %s; provisional, export may lag or contain incomplete history',
                 project, windows, costs.currency, costs.exported_at, costs.earliest_usage)
    except Exception as exc:
        LOG.warning('Project costs for %s: last 24h/7d/30d unavailable; %s', project, error_detail(exc))
