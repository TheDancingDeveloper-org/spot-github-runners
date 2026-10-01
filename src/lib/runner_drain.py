"""Quarantine a single host's runners and wait for CI completion before removal."""
from __future__ import annotations

import json
import logging
import subprocess
from .fleet_logging import vm_details
from .runner_labels import DEFAULT_RUNNER_LABELS

LOG = logging.getLogger('spot_runner_autoscaler')


def api(org, method, path, body=None, paginate=False):
    command = ['gh', 'api', '-X', method, path]
    if paginate:
        command += ['--paginate', '--slurp']
    if body is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, check=True, timeout=60)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def pages(result, collection):
    if not isinstance(result, list):
        raise RuntimeError('Invalid paginated GitHub drain inventory')
    return [row for page in result for row in page[collection]]


def isolated_group(operation, org, save):
    name = operation['drain_group_name']
    if not operation.get('drain_group_id'):
        groups = pages(api(org, 'GET', f'orgs/{org}/actions/runner-groups', paginate=True), 'runner_groups')
        found = [group for group in groups if group['name'] == name]
        if len(found) > 1:
            raise RuntimeError('Ambiguous drain group identity')
        group = found[0] if found else api(org, 'POST', f'orgs/{org}/actions/runner-groups',
            {'name': name, 'visibility': 'selected', 'selected_repository_ids': [],
             'allows_public_repositories': False})
        operation['drain_group_id'] = int(group['id'])
        save()
    group_id = operation['drain_group_id']
    path = f'orgs/{org}/actions/runner-groups/{group_id}'
    group = api(org, 'GET', path)
    repositories = pages(api(org, 'GET', path + '/repositories', paginate=True), 'repositories')
    if (group.get('name') != name or group.get('visibility') != 'selected'
            or group.get('default') or group.get('inherited')
            or group.get('allows_public_repositories') is not False or repositories):
        raise RuntimeError('Drain group is not an isolated group with zero repository access')
    return path


def jobs_finished(vm, state, org):
    for record in state.get('job_tracking', {}).get('jobs', {}).values():
        if record.get('vm', {}).get('instance_id') != vm.instance_id:
            continue
        job = record['job']
        if job.get('status') == 'completed':
            continue
        current = api(org, 'GET', f"repos/{record['repository']}/actions/jobs/{job['id']}")
        if current.get('status') != 'completed':
            LOG.info('Drain waits for CI job %s on %s', job['id'], vm_details(vm))
            return False
    return True


def drain_runners(operation, vm, args, state, save, assigned):
    """Return true only after fresh observations prove all draining runners absent."""
    legacy_group = operation.get('drain_mode') != 'label_only'
    path = isolated_group(operation, args.org, save) if legacy_group else None
    recorded = operation.setdefault('runner_names', {})
    done = set(operation.get('deregistered_runner_ids', []))
    changed = not operation.get('runners_isolated')
    for runner in assigned:
        key = str(runner.runner_id)
        if key in recorded and recorded[key] != runner.name:
            raise RuntimeError('Drain runner identity changed')
        if key not in recorded:
            recorded[key] = runner.name
            operation['runner_ids'].append(runner.runner_id)
            changed = True
            save()
        if runner.runner_id in done:
            raise RuntimeError('A deregistered runner appeared again during drain')
    if legacy_group:
        members = {row['id'] for row in pages(api(args.org, 'GET', path + '/runners', paginate=True), 'runners')}
        if members - set(operation['runner_ids']):
            raise RuntimeError('Drain group contains unrelated runners')
    for runner in assigned:
        if set(runner.labels) - DEFAULT_RUNNER_LABELS:
            result = api(args.org, 'DELETE', f'orgs/{args.org}/actions/runners/{runner.runner_id}/labels')
            if any(label.get('type') != 'read-only' for label in result['labels']):
                raise RuntimeError('Custom scheduling labels remain after removal')
            changed = True
        if legacy_group and runner.runner_id not in members:
            api(args.org, 'PUT', path + f'/runners/{runner.runner_id}')
            changed = True
    if changed:
        operation['runners_isolated'] = True
        save()
        LOG.info('Removed scheduling labels and isolated runner access for %s; waiting for fresh CI completion observations', vm_details(vm))
        return False
    expected = set(operation['runner_ids'])
    present = {runner.runner_id for runner in assigned}
    pending = operation.get('deregistering_runner_id')
    if pending is not None and pending in expected and pending not in present:
        done.add(pending)
        operation['deregistered_runner_ids'] = sorted(done)
        operation.pop('deregistering_runner_id', None)
        save()
    if expected != present | done:
        LOG.warning('Drain waits for missing runner records on %s', vm_details(vm))
        return False
    if any(not runner.online or runner.busy for runner in assigned):
        LOG.info('Drain waits for every runner to be online and idle on %s', vm_details(vm))
        return False
    if not jobs_finished(vm, state, args.org):
        return False
    if assigned:
        for runner in assigned:
            fresh = api(args.org, 'GET', f'orgs/{args.org}/actions/runners/{runner.runner_id}')
            if fresh.get('name') != runner.name or fresh.get('busy') is not False or fresh.get('status') != 'online':
                LOG.info('Drain waits for fresh idle confirmation on runner %s; %s', runner.name, vm_details(vm))
                return False
            operation['deregistering_runner_id'] = runner.runner_id
            save()
            api(args.org, 'DELETE', f'orgs/{args.org}/actions/runners/{runner.runner_id}')
            done.add(runner.runner_id)
            operation['deregistered_runner_ids'] = sorted(done)
            operation.pop('deregistering_runner_id', None)
            save()
        return False  # Require a fresh inventory proving registration records absent.
    return True


def cleanup_group(operation, org):
    if not operation.get('drain_group_id'):
        return
    path = f"orgs/{org}/actions/runner-groups/{operation['drain_group_id']}"
    try:
        group = api(org, 'GET', path)
    except subprocess.CalledProcessError as exc:
        if 'HTTP 404' in (exc.stderr or ''):
            return
        raise
    if group.get('name') != operation['drain_group_name'] or group.get('default') or group.get('inherited'):
        raise RuntimeError('Refusing to remove a drain group with changed identity')
    if pages(api(org, 'GET', path + '/runners', paginate=True), 'runners'):
        raise RuntimeError('Drain group still contains runners')
    api(org, 'DELETE', path)
