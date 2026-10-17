"""Quarantine a single host's runners and wait for CI completion before removal."""
from __future__ import annotations

from typing import Any
import json
import logging
import subprocess
from .fleet_logging import vm_details
from .runner_labels import DEFAULT_RUNNER_LABELS
from .runner_fleet import GceVm

LOG = logging.getLogger('spot_runner_autoscaler')


def is_repo_target(target: Any) -> bool:
    """Return True if target represents a repository ('owner/repo')."""
    if hasattr(target, "is_repo"):
        return bool(target.is_repo)
    if hasattr(target, "name"):
        return "/" in str(target.name)
    return "/" in str(target)


def target_api_prefix(target: Any) -> str:
    """Return 'repos/{owner}/{repo}' or 'orgs/{org}'."""
    if hasattr(target, "api_path"):
        return target.api_path
    name = getattr(target, "name", str(target)).strip()
    return f"repos/{name}" if is_repo_target(target) else f"orgs/{name}"


def resolve_target(operation: dict[str, Any], args: Any, default: str | None = None) -> str:
    """Resolve the target from the operation, then args, then the caller's default.

    'swack-tools' is used only when no operation target, args target/org, or default exists.
    """
    return (
        operation.get("target")
        or getattr(args, "target", None)
        or getattr(args, "org", None)
        or default
        or "swack-tools"
    )


def api(org, method, path, body=None, paginate=False):
    command = ['gh', 'api', '-X', method, path]
    if paginate:
        command += ['--paginate', '--slurp']
    if body is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, check=True, timeout=60)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def guarded_api(guard,*args,**kwargs):
    method=args[1] if len(args)>1 else kwargs['method']
    if method!='GET' and guard is not None:guard()
    return api(*args,**kwargs)


def pages(result, collection):
    if not isinstance(result, list):
        raise RuntimeError('Invalid paginated GitHub drain inventory')
    return [row for page in result for row in page[collection]]


def isolated_group(operation, org, save, mutation_guard=None):
    if is_repo_target(org):
        raise RuntimeError("Runner groups are not supported for personal repository targets")
    request=lambda *a,**kw:guarded_api(mutation_guard,*a,**kw)
    name = operation['drain_group_name']
    if not operation.get('drain_group_id'):
        groups = pages(request(org, 'GET', f'orgs/{org}/actions/runner-groups', paginate=True), 'runner_groups')
        found = [group for group in groups if group['name'] == name]
        if len(found) > 1:
            raise RuntimeError('Ambiguous drain group identity')
        group = found[0] if found else request(org, 'POST', f'orgs/{org}/actions/runner-groups',
            {'name': name, 'visibility': 'selected', 'selected_repository_ids': [],
             'allows_public_repositories': False})
        operation['drain_group_id'] = int(group['id'])
        save()
    group_id = operation['drain_group_id']
    path = f'orgs/{org}/actions/runner-groups/{group_id}'
    group = request(org, 'GET', path)
    repositories = pages(request(org, 'GET', path + '/repositories', paginate=True), 'repositories')
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


def drain_runners(operation, vm, args, state, save, assigned, mutation_guard=None):
    request = lambda *a, **kw: guarded_api(mutation_guard, *a, **kw)
    """Return true only after fresh observations prove all draining runners absent."""
    target = resolve_target(operation, args)
    prefix = target_api_prefix(target)
    repo_target = is_repo_target(target)

    if repo_target:
        legacy_group = False
        operation['drain_mode'] = 'label_only'
        operation['cordon_mode'] = 'labels'
    else:
        legacy_group = operation.get('cordon_mode') == 'isolated_group' or operation.get('drain_mode') != 'label_only'
        if legacy_group:
            operation.setdefault('drain_group_name', f'spot-drain-{vm.name}-{vm.instance_id}')
            operation['cordon_mode'] = 'isolated_group'
        else:
            operation['cordon_mode'] = 'labels'
    path = isolated_group(operation, target, save, mutation_guard) if legacy_group else None
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
        members = {row['id'] for row in pages(request(target, 'GET', path + '/runners', paginate=True), 'runners')}
        if members - set(operation['runner_ids']):
            raise RuntimeError('Drain group contains unrelated runners')
    for runner in assigned:
        if set(runner.labels) - DEFAULT_RUNNER_LABELS:
            result = request(target, 'DELETE', f'{prefix}/actions/runners/{runner.runner_id}/labels')
            if any(label.get('type') != 'read-only' for label in result.get('labels', [])):
                raise RuntimeError('Custom scheduling labels remain after removal')
            changed = True
        if legacy_group and runner.runner_id not in members:
            request(target, 'PUT', path + f'/runners/{runner.runner_id}')
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
    if not jobs_finished(vm, state, target):
        return False
    if assigned:
        for runner in assigned:
            fresh = request(target, 'GET', f'{prefix}/actions/runners/{runner.runner_id}')
            if fresh.get('name') != runner.name or fresh.get('busy') is not False or fresh.get('status') != 'online':
                LOG.info('Drain waits for fresh idle confirmation on runner %s; %s', runner.name, vm_details(vm))
                return False
            operation['deregistering_runner_id'] = runner.runner_id
            save()
            request(target, 'DELETE', f'{prefix}/actions/runners/{runner.runner_id}')
            done.add(runner.runner_id)
            operation['deregistered_runner_ids'] = sorted(done)
            operation.pop('deregistering_runner_id', None)
            save()
        return False  # Require a fresh inventory proving registration records absent.
    return True


def cleanup_group(operation, org, mutation_guard=None):
    target = resolve_target(operation, None, org)
    if is_repo_target(target):
        return
    request=lambda *a,**kw:guarded_api(mutation_guard,*a,**kw)
    if not operation.get('drain_group_id'):
        return
    path = f"orgs/{target}/actions/runner-groups/{operation['drain_group_id']}"
    try:
        group = request(target, 'GET', path)
    except subprocess.CalledProcessError as exc:
        if 'HTTP 404' in (exc.stderr or ''):
            return
        raise
    if group.get('name') != operation['drain_group_name'] or group.get('default') or group.get('inherited'):
        raise RuntimeError('Refusing to remove a drain group with changed identity')
    if pages(request(target, 'GET', path + '/runners', paginate=True), 'runners'):
        raise RuntimeError('Drain group still contains runners')
    request(target, 'DELETE', path)


def delete_vm_and_disks(vm: GceVm, project: str, *, timeout: int = 180) -> None:
    """Cleanly delete a GCP compute instance and all attached auto-deleting disks."""
    subprocess.run([
        "gcloud", "compute", "instances", "delete", vm.name,
        f"--zone={vm.zone}", f"--project={project}", "--delete-disks=all", "--quiet",
    ], check=True, text=True, capture_output=True, timeout=timeout)
    LOG.info("Deleted drained Spot VM %s and its attached disks", vm_details(vm))
