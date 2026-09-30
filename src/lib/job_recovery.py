"""Track jobs on managed Spot VMs and retry confirmed runner-loss failures."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode

from .runner_fleet import GceVm

LOG = logging.getLogger('spot_runner_autoscaler')


def one_line(value: object) -> str:
    return ' '.join(str(value).split())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def vm_description(vm: dict) -> str:
    specs = [vm.get('machine_type') or 'machine type unknown',
             f"{vm['vcpu']} vCPU" if vm.get('vcpu') is not None else 'vCPU unknown',
             f"{vm['ram_gib']:g} GiB RAM" if vm.get('ram_gib') is not None else 'RAM unknown']
    for disk in vm.get('disks', []):
        role = 'root disk' if disk.get('boot') else 'data disk'
        specs.append(f"{disk.get('diskSizeGb', 'unknown')} GiB {disk.get('diskType') or disk.get('type', 'type unknown')} {disk.get('interface', '')} {role}")
    for gpu in vm.get('accelerators', []):
        specs.append(f"GPU {gpu.get('acceleratorCount', '?')} x {gpu.get('acceleratorType', 'unknown').rsplit('/', 1)[-1]}")
    return one_line(f"instance type: {', '.join(specs)}; zone: {vm['zone']}; instance name: {vm['name']}")


class GitHubJobsApi:
    """Use existing gh authentication; never read or persist its credential."""

    def get(self, endpoint: str, *, paginate: bool = False):
        command = ['gh', 'api', endpoint]
        if paginate:
            command += ['--paginate', '--slurp']
        result = subprocess.run(command, check=True, text=True, capture_output=True, timeout=90)
        return json.loads(result.stdout)

    def post(self, endpoint: str) -> None:
        subprocess.run(['gh', 'api', '--method', 'POST', endpoint],
                       check=True, text=True, capture_output=True, timeout=90)


def runner_lost(annotations: list[dict]) -> bool:
    """Code failures and user cancellations are not runner-loss evidence."""
    for annotation in annotations:
        if annotation.get('annotation_level') != 'failure':
            continue
        message = str(annotation.get('message', '')).lower()
        if 'runner' in message and any(phrase in message for phrase in (
            'lost communication with the server', 'lost connection to the server',
            'runner has stopped responding', 'runner stopped responding',
        )):
            return True
    return False


def error_text(exc: Exception) -> str:
    return one_line(getattr(exc, 'stderr', None) or str(exc))[:1500]


class JobRecovery:
    def __init__(self, state: dict, save: Callable[[], None], repositories: list[str], *, api=None):
        if any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*', repo)
               for repo in repositories):
            raise ValueError('job repositories must use owner/repository names')
        self.state = state
        self.save = save
        self.repositories = list(dict.fromkeys(repositories))
        self.api = api or GitHubJobsApi()
        self.data = state.setdefault('job_tracking', {})
        self.data.setdefault('vms', {})
        self.data.setdefault('losses', {})
        self.data.setdefault('jobs', {})
        self.data.setdefault('runs', {})
        self.data.setdefault('last_poll_by_repository', {})

    def poll(self, vms: list[GceVm]) -> None:
        """Keep attribution even if GitHub is unavailable during a VM loss."""
        now = utc_now()
        # Expiry must precede network reads: an unavailable old run must not
        # prevent pruning or poison the next repository-wide scan.
        for collection, timestamp in (('jobs', 'observed_at'), ('runs', 'observed_at')):
            for key, record in list(self.data[collection].items()):
                if instant(now) - instant(record[timestamp]) > timedelta(days=30):
                    del self.data[collection][key]
        active_losses = {record['vm_key'] for record in self.data['jobs'].values()}
        for key, loss in list(self.data['losses'].items()):
            if key not in active_losses and instant(now) - instant(loss['lost_at']) > timedelta(days=1):
                del self.data['losses'][key]
        current = {f'{vm.zone}/{vm.name}': asdict(vm) for vm in vms
                   if vm.spot and vm.name.startswith('oxidex-runners-')}
        previous = self.data['vms']
        intentional = set(self.state.get('intentional_deletions', []))
        for key, vm in {**previous, **current}.items():
            lost = key not in current or current[key]['status'].upper() == 'TERMINATED'
            old_loss = self.data['losses'].get(key)
            new_episode = old_loss is None or (previous.get(key, {}).get('status', '').upper() == 'RUNNING'
                                               and instant(previous[key]['last_seen_at']) > instant(old_loss['lost_at']))
            if lost and key not in intentional and new_episode:
                self.data['losses'][key] = {'vm': vm, 'lost_at': now,
                                           'last_seen_at': previous.get(key, {}).get('last_seen_at', now)}
        self.state['intentional_deletions'] = [key for key in intentional if key not in current]
        self.data['vms'] = {key: {**vm, 'last_seen_at': now} for key, vm in current.items()}
        self.save()
        posted_runs = set()
        for repository in self.repositories:
            try:
                runs, jobs = self._scan(repository, now)
                self._remember(repository, jobs, current, previous, now)
                self.save()
                for key, record in list(self.data['jobs'].items()):
                    if record['repository'] != repository:
                        continue
                    if record.get('rerun') in ('ignored', 'external'):
                        continue
                    loss = self.data['losses'].get(record['vm_key'])
                    if not loss:
                        if record['job']['status'] == 'completed':
                            del self.data['jobs'][key]
                        continue
                    if instant(now) - instant(loss['lost_at']) > timedelta(days=1) and not record.get('rerun'):
                        LOG.warning('Job-loss investigation expired without a confirmed crash result: %s; %s', one_line(record['job'].get('html_url', '')), vm_description(record['vm']))
                        record['rerun'] = 'ignored'
                        continue
                    if record['job']['run_id'] not in runs:
                        continue
                    try:
                        self._recover(record, loss, runs, jobs, posted_runs, now)
                    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
                        LOG.warning('Crash-job investigation is pending for %s: %s', one_line(record['job'].get('html_url', '')), error_text(exc))
                self.data['last_poll_by_repository'][repository] = now
                self.save()
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
                LOG.warning('Job tracking for %s is pending; retrying next loop: %s', repository, error_text(exc))

    def _scan(self, repo: str, now: str) -> tuple[dict, dict]:
        lookback = self.data['last_poll_by_repository'].get(repo) or self.data.get('last_poll_at') or (instant(now) - timedelta(hours=1)).isoformat()
        # In-progress enumeration includes older runs; tracked jobs keep their
        # run IDs even when the run completes outside the discovery window.
        queries = [{'status': 'in_progress', 'per_page': 100},
                   {'status': 'completed', 'per_page': 100,
                    'created': '>=' + (instant(lookback) - timedelta(minutes=10)).isoformat()}]
        runs = {}
        for query in queries:
            pages = self.api.get(f'repos/{repo}/actions/runs?{urlencode(query)}', paginate=True)
            for page in pages:
                for run in page['workflow_runs']:
                    runs[run['id']] = run
        tracked_ids = {record['run_id'] for record in self.data['runs'].values()
                       if record['repository'] == repo}
        tracked_ids.update(record['job']['run_id'] for record in self.data['jobs'].values()
                           if record['repository'] == repo
                           and record.get('rerun') not in ('ignored', 'external')
                           and not record.get('outcome_logged'))
        for run_id in tracked_ids - runs.keys():
            try:
                runs[run_id] = self.api.get(f'repos/{repo}/actions/runs/{run_id}')
            except subprocess.CalledProcessError as exc:
                if not re.search(r'HTTP 404', error_text(exc)):
                    raise
                LOG.warning('Saved workflow run %s/%s is unavailable; skipping this run and retaining its records until expiry: %s', repo, run_id, error_text(exc))
        for run_id, run in runs.items():
            key = f'{repo}/{run_id}'
            if run['status'] != 'completed':
                self.data['runs'][key] = {'repository': repo, 'run_id': run_id, 'observed_at': now}
        jobs = {}
        for run_id, run in runs.items():
            pages = self.api.get(f'repos/{repo}/actions/runs/{run_id}/jobs?per_page=100', paginate=True)
            for page in pages:
                for job in page['jobs']:
                    jobs[job['id']] = {**job, 'run_id': run_id,
                                       'run_attempt': job.get('run_attempt', run.get('run_attempt', 1))}
            if run['status'] == 'completed':
                # Drop the active run only after its terminal jobs were read.
                self.data['runs'].pop(f'{repo}/{run_id}', None)
        return runs, jobs

    def _remember(self, repo: str, jobs: dict, current: dict, previous: dict, now: str) -> None:
        inventory = {key: loss['vm'] for key, loss in self.data['losses'].items()}
        inventory.update(previous)
        inventory.update(current)
        for job in jobs.values():
            runner = job.get('runner_name') or ''
            matches = [key for key, vm in inventory.items()
                       if re.search(r'(?:^|-)' + re.escape(vm['name']) + r'-[0-9]+$', runner)]
            if not matches:
                continue
            vm_key = max(matches, key=lambda key: len(inventory[key]['name']))
            loss = self.data['losses'].get(vm_key)
            if loss and current.get(vm_key, {}).get('status', '').upper() != 'RUNNING' and (
                    not job.get('started_at') or instant(job['started_at']) > instant(loss['lost_at'])):
                continue
            key = f"{repo}/{job['id']}"
            if key not in self.data['jobs']:
                if job['status'] != 'in_progress' and vm_key not in self.data['losses']:
                    continue
                self.data['jobs'][key] = {'repository': repo, 'vm_key': vm_key,
                                         'vm': inventory[vm_key], 'job': job, 'observed_at': now}
            else:
                self.data['jobs'][key]['job'] = job

    def _recover(self, record: dict, loss: dict, runs: dict, jobs: dict,
                 posted_runs: set, now: str) -> None:
        repo = record['repository']
        original = record['job']
        if not original.get('started_at') or instant(original['started_at']) > instant(loss['lost_at']):
            return
        label = one_line(f"{original['name']} ({repo}, run {original['run_id']}, job {original['id']}) {original.get('html_url', '')}")
        description = vm_description(record['vm'])
        if record.get('rerun') in ('accepted', 'uncertain', 'submitting'):
            if record['rerun'] == 'submitting':
                record['rerun'] = 'uncertain'
                self.save()
            self._observe_rerun(record, jobs, label, description)
            return
        job = jobs.get(original['id']) or self.api.get(f"repos/{repo}/actions/jobs/{original['id']}")
        record['job'] = job
        if job['status'] != 'completed':
            if not record.get('loss_logged'):
                LOG.warning('CI jobs were running when this VM instance disappeared or terminated: %s; affected job: %s; awaiting GitHub result', description, label)
                record['loss_logged'] = True
                self.save()
            return
        if job.get('conclusion') != 'failure':
            LOG.info('Job on lost VM completed with %s; no crash rerun: %s; %s', job.get('conclusion'), label, description)
            record['rerun'] = 'ignored'
            self.save()
            return
        completed = job.get('completed_at')
        if not completed or instant(completed) < instant(loss['last_seen_at']):
            record['rerun'] = 'ignored'
            self.save()
            return
        match = re.fullmatch(r'https://api\.github\.com/repos/' + re.escape(repo) + r'/check-runs/([0-9]+)', job.get('check_run_url', ''))
        if not match:
            LOG.warning('Cannot verify runner-loss evidence for job %s; %s', label, description)
            return
        pages = self.api.get(f'repos/{repo}/check-runs/{match[1]}/annotations?per_page=100', paginate=True)
        annotations = [item for page in pages for item in page] if pages and isinstance(pages[0], list) else pages
        if not runner_lost(annotations):
            LOG.info('Job failed on lost VM, but GitHub has no runner-loss error; no automatic rerun: %s; %s', label, description)
            # Annotations can arrive after the terminal result; recheck later.
            return
        if not record.get('crash_logged'):
            LOG.error('This VM instance terminated or disappeared, causing these CI jobs to crash: %s; %s; evidence: GitHub reports runner lost communication', description, label)
            record['crash_logged'] = True
            self.save()
        run = runs[job['run_id']]
        newer = [candidate for candidate in jobs.values()
                 if candidate['run_id'] == job['run_id'] and candidate['name'] == job['name']
                 and candidate['id'] != job['id'] and candidate.get('run_attempt', 1) > job.get('run_attempt', 1)]
        if newer:
            LOG.info('Crash job already rerun outside this request; not submitting again: %s; %s', label, description)
            record['rerun'] = 'external'
            self.save()
            return
        run_key = (repo, job['run_id'])
        if run['status'] != 'completed' or run_key in posted_runs:
            return
        record['rerun'] = 'submitting'
        record['rerun_requested_at'] = now
        self.save()  # Persist intent before sending a request that has no idempotency key.
        LOG.warning('Rerunning the crashed jobs %s because the last Spot instance terminated unexpectedly; %s; dependent jobs may also rerun', label, description)
        try:
            self.api.post(f"repos/{repo}/actions/jobs/{job['id']}/rerun")
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            if re.search(r'HTTP 4[0-9]{2}', error_text(exc)):
                record['rerun'] = 'retry'
                LOG.error('GitHub rejected crash-job rerun; retrying next loop: %s; %s', label, error_text(exc))
            else:
                record['rerun'] = 'uncertain'
                LOG.error('Rerun receipt is uncertain; checking GitHub before any further action: %s; %s', label, error_text(exc))
        else:
            record['rerun'] = 'accepted'
            LOG.info('GitHub accepted crash-job rerun: %s; %s', label, description)
        posted_runs.add(run_key)
        self.save()

    def _observe_rerun(self, record: dict, jobs: dict, label: str, description: str) -> None:
        original = record['job']
        newer = [job for job in jobs.values() if job['run_id'] == original['run_id']
                 and job['name'] == original['name'] and job['id'] != original['id']
                 and job.get('run_attempt', 1) > original.get('run_attempt', 1)]
        if not newer:
            if record['rerun'] == 'uncertain' and not record.get('attention_logged'):
                LOG.error('Crash-job rerun needs a manual GitHub check: no receipt or newer attempt is visible; automatic resubmission is held to avoid duplicate jobs: %s; %s', label, description)
                record['attention_logged'] = True
                self.save()
            return
        newest = max(newer, key=lambda job: (job.get('run_attempt', 1), job['id']))
        if record['rerun'] == 'uncertain':
            record['rerun'] = 'accepted'
            LOG.info('GitHub confirms the crash-job rerun after an uncertain receipt: %s; %s', label, description)
            self.save()
        if newest['status'] == 'completed' and not record.get('outcome_logged'):
            LOG.info('Crash-job rerun completed with %s: %s; new job: %s; %s', newest.get('conclusion'), label, newest.get('html_url', ''), description)
            record['outcome_logged'] = True
            self.save()
