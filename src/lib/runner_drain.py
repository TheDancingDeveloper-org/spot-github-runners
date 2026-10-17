"""Quarantine a single host's runners and wait for CI completion before removal."""
from __future__ import annotations

from typing import Any
import json
import logging
import shlex
import subprocess
from .fleet_logging import vm_details
from .runner_labels import DEFAULT_RUNNER_LABELS
from .runner_fleet import GceVm

LOG = logging.getLogger('spot_runner_autoscaler')

# Host-side listener cordon for repository targets. GitHub keeps the read-only default labels
# (self-hosted, linux, X64) on a repository runner and personal repositories have no runner groups, so
# no GitHub-side change stops a job that selects only built-in labels. Instead the controller places a
# root-owned marker in this directory, which every runner container bind-mounts read-only, and the
# runner image's job-started hook (runner/job-started.sh) refuses every job that starts while it exists.
HOST_CORDON_DIR = '/run/gha-runner-cordon'
HOST_CORDON_MARKER = HOST_CORDON_DIR + '/draining'
HOST_CORDON_LOCK = '/run/gha-runner-cordon.lock'
# The job-started hook carries this exact text; hosts whose hook lacks it cannot be cordoned.
HOST_CORDON_CAPABILITY = 'GHA_RUNNER_CORDON_V1'
JOB_STARTED_HOOK = '/usr/local/bin/gha-runner-job-started.sh'
# Remote exit codes of the host cordon program (see _HOST_CORDON_PROGRAM).
CORDON_IDLE = 0          # marker was already in place and no Runner.Worker process runs on the host
CORDON_PLACED = 10       # marker placed now: a hook that read the old state may still be starting a job
CORDON_BUSY = 75         # marker in place but a job (or its hook) is still running; or the lock is held
CORDON_UNSUPPORTED = 76  # a runner container cannot refuse jobs at start; never retire under it
CORDON_IDENTITY = 77     # the SSH target is not the recorded provider instance
CORDON_STATES = {CORDON_IDLE: 'idle', CORDON_PLACED: 'placed', CORDON_BUSY: 'busy',
                 CORDON_UNSUPPORTED: 'unsupported', CORDON_IDENTITY: 'identity'}

# Runs as root on the runner host under the cordon lock, after the instance-identity check.
# argv: cordon TOKEN NAME... | release TOKEN
_HOST_CORDON_TEMPLATE = r"""
import io, json, os, stat, subprocess, sys, tarfile
from pathlib import Path
DIR = Path(%(dir)r); MARKER = Path(%(marker)r); HOOK = %(hook)r; CAPABILITY = %(capability)r.encode()
DOCKER = %(docker)r; PROC = %(proc)r; OWNER_UID = %(owner_uid)r
HOOK_ENV = 'ACTIONS_RUNNER_HOOK_JOB_STARTED='
def done(code, **detail):
    print(json.dumps(detail, sort_keys=True)); raise SystemExit(code)
action, token, names = sys.argv[1], sys.argv[2], sys.argv[3:]
if action == 'release':
    # Remove only the marker this owner placed.
    if MARKER.is_file() and not MARKER.is_symlink() and MARKER.read_text() == token:
        MARKER.unlink()
    done(0, state='released')
if action != 'cordon' or not token or not names:
    done(76, reason='invalid cordon request')
def docker(*argv):
    return subprocess.run([DOCKER, *argv], check=True, capture_output=True, timeout=30).stdout
for name in names:
    try:
        row = json.loads(docker('inspect', '--type=container', name))[0]
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        done(76, reason='runner container %%s cannot be inspected' %% name)
    if row.get('Name', '').lstrip('/') != name:
        done(76, reason='runner container %%s has another identity' %% name)
    mounts = [m for m in row.get('Mounts') or [] if m.get('Destination') == str(DIR)]
    if (len(mounts) != 1 or mounts[0].get('Type') != 'bind' or mounts[0].get('Source') != str(DIR)
            or mounts[0].get('RW') is not False):
        done(76, reason='runner container %%s has no read-only host cordon mount' %% name)
    hooks = [e for e in (row.get('Config') or {}).get('Env') or [] if e.startswith(HOOK_ENV)]
    if hooks != [HOOK_ENV + HOOK]:
        done(76, reason='runner container %%s does not run the cordon-aware job-started hook' %% name)
    state = [m.get('Source') for m in row.get('Mounts') or [] if m.get('Destination') == '/runner-state']
    try:
        override = len(state) == 1 and HOOK_ENV.encode() in Path(state[0], '.env').read_bytes()
    except FileNotFoundError:
        override = False
    except OSError:
        override = True
    if len(state) != 1 or override:
        done(76, reason='runner container %%s may override its job-started hook' %% name)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(docker('cp', name + ':' + HOOK, '-')))
        member = archive.next()
        hook = archive.extractfile(member).read() if member is not None and member.isfile() else b''
    except (OSError, tarfile.TarError, subprocess.SubprocessError):
        hook = b''
    if CAPABILITY not in hook:
        done(76, reason='runner container %%s job-started hook has no cordon check' %% name)
info = os.lstat(DIR)
if not stat.S_ISDIR(info.st_mode) or info.st_uid != OWNER_UID or info.st_mode & 0o022:
    done(76, reason='host cordon directory is not a root-owned directory')
if not MARKER.is_file() or MARKER.is_symlink():
    temporary = DIR / ('.draining.%%d' %% os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'w') as target:
        target.write(token); target.flush(); os.fsync(target.fileno())
    os.replace(temporary, MARKER)
    done(10, state='placed')
# Every assigned job runs in a Runner.Worker, from before its job-started hook until it ends. After the
# marker exists, a new Worker's hook refuses its job, so no Worker here means no job can still run.
workers = 0
for entry in os.listdir(PROC):
    if entry.isdigit():
        try:
            workers += Path(PROC, entry, 'comm').read_text().strip() == 'Runner.Worker'
        except OSError:
            pass
if workers:
    done(75, state='busy', workers=workers)
done(0, state='idle')
"""


def host_cordon_program(*, directory: str = HOST_CORDON_DIR, hook: str = JOB_STARTED_HOOK,
                        docker: str = '/usr/bin/docker', proc: str = '/proc', owner_uid: int = 0) -> str:
    """Render the host cordon program; tests substitute a sandbox for the fixed host paths."""
    return _HOST_CORDON_TEMPLATE % {'dir': directory, 'marker': directory + '/draining', 'hook': hook,
                                    'capability': HOST_CORDON_CAPABILITY, 'docker': docker, 'proc': proc,
                                    'owner_uid': owner_uid}


_HOST_CORDON_PROGRAM = host_cordon_program()


def host_cordon_command(vm: GceVm, action: str, token: str, runner_names=()) -> str:
    """Return the identity-checked, lock-guarded root command for one host cordon action."""
    if action not in ('cordon', 'release') or not vm.instance_id or not token:
        raise ValueError('Host cordon needs an action, a provider instance identity, and an owner token')
    argv = ' '.join(shlex.quote(value) for value in (action, token, *runner_names))
    script = ('test "$(curl -fsS -H Metadata-Flavor:Google '
              'http://metadata.google.internal/computeMetadata/v1/instance/id)" = '
              + shlex.quote(vm.instance_id) + ' || exit ' + str(CORDON_IDENTITY) + '; '
              'exec /usr/bin/python3 -I -c ' + shlex.quote(_HOST_CORDON_PROGRAM) + ' ' + argv)
    return (f'sudo flock -w 30 -E {CORDON_BUSY} {HOST_CORDON_LOCK} sh -c ' + shlex.quote(script))


def _run_host_cordon(vm: GceVm, args: Any, action: str, token: str, runner_names=()) -> tuple[str, dict]:
    try:
        command = host_cordon_command(vm, action, token, runner_names)
    except ValueError as exc:
        return 'identity', {'reason': str(exc)}
    try:
        result = subprocess.run(['gcloud', 'compute', 'ssh', vm.name, f'--zone={vm.zone}',
                                 f'--project={args.project}', '--quiet', '--command=' + command],
                                text=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return 'unreachable', {'reason': type(exc).__name__}
    detail: dict = {}
    for line in reversed((result.stdout or '').splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            detail = parsed
            break
    return CORDON_STATES.get(result.returncode, 'unreachable'), detail


def cordon_runner_host(vm: GceVm, args: Any, token: str, runner_names, mutation_guard=None) -> tuple[str, dict]:
    """Ensure the host refuses new jobs at start, then report whether any job still runs there.

    Returns ``(state, detail)``: ``placed`` (cordon placed by this call; observe again before trusting
    idleness), ``idle`` (cordon was already in place and no job runs), ``busy``, ``unsupported`` (the host
    cannot refuse new jobs: hold retirement), ``identity`` or ``unreachable``. Only ``idle`` permits
    deregistration.
    """
    names = sorted(set(runner_names))
    if not names:
        return 'unsupported', {'reason': 'no runner containers recorded for this host'}
    if mutation_guard is not None:
        mutation_guard()
    return _run_host_cordon(vm, args, 'cordon', token, names)


def release_runner_host_cordon(vm: GceVm, args: Any, token: str, mutation_guard=None) -> bool:
    """Remove a host cordon marker this owner placed; leave anyone else's marker. True when done."""
    if mutation_guard is not None:
        mutation_guard()
    status, detail = _run_host_cordon(vm, args, 'release', token)
    return status == 'idle' and detail.get('state') == 'released'


def host_cordon_token(kind: str, vm: Any) -> str:
    instance_id = vm.get('instance_id') if isinstance(vm, dict) else getattr(vm, 'instance_id', '')
    return f'{kind}-{instance_id or "unknown"}'


def log_cordon_hold(vm: GceVm, state: str, detail: dict) -> None:
    reason = detail.get('reason') or state
    if state == 'unsupported':
        LOG.error('Retirement held for %s: the host cannot refuse new jobs at job start (%s). Repository '
                  'runners keep their built-in labels, so it stays in service, undrained and registered, '
                  'until it is replaced by a host whose runners run the cordon-aware job-started hook',
                  vm_details(vm), reason)
    elif state == 'busy':
        LOG.info('Drain waits for running jobs on cordoned host %s (%s Runner.Worker process(es))',
                 vm_details(vm), detail.get('workers', 'unknown'))
    elif state == 'placed':
        LOG.info('Placed the host job-start cordon on %s; waiting for a fresh idle observation', vm_details(vm))
    else:
        LOG.warning('Drain waits: host cordon on %s is unverified (%s)', vm_details(vm), reason)


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
        operation['cordon_mode'] = 'host'
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
    cordon = None
    if repo_target and assigned:
        # Cordon on the host BEFORE any GitHub change, and re-verify it on every observation: only a
        # cordon already in place with no Runner.Worker on the host proves that no job can still run.
        # An unsupported host keeps its labels and registrations: retirement is held, not forced.
        if not operation.get('host_cordon_token'):
            operation['host_cordon_token'] = host_cordon_token('drain', vm)
            save()
        token = operation['host_cordon_token']
        cordon, detail = cordon_runner_host(vm, args, token, [runner.name for runner in assigned],
                                            mutation_guard)
        if cordon not in ('placed', 'busy', 'idle'):
            log_cordon_hold(vm, cordon, detail)
            return False
        if cordon == 'placed':
            log_cordon_hold(vm, cordon, detail)
            changed = True
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
    if assigned and repo_target and cordon != 'idle':
        log_cordon_hold(vm, cordon or 'unverified', {})
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
