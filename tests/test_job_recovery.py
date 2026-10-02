"""Exercise job-loss attribution and restart-safe reruns with an offline API."""

import copy
import json
import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from lib import job_recovery
from lib.runner_fleet import GceVm
from lib.spot_runner_autoscaler import args_parser, read_scale_state, write_scale_state, run_controller


VM = GceVm('oxidex-runners-test', 'asia-south2-a', 'RUNNING', 'n2-standard-32', True)
RUNNER = 'gha-oxidex-runners-test-3'
REPO = 'swack-tools/oxidex'
JOB = {'id': 11, 'run_id': 7, 'run_attempt': 1, 'name': 'Build & Test',
       'status': 'in_progress', 'conclusion': None, 'runner_id': 8, 'runner_name': RUNNER,
       'started_at': '2026-09-30T20:00:00Z', 'completed_at': None,
       'html_url': 'https://github.com/swack-tools/oxidex/actions/runs/7/job/11',
       'check_run_url': 'https://api.github.com/repos/swack-tools/oxidex/check-runs/11'}


class FakeGitHub:
    def __init__(self):
        self.job = copy.deepcopy(JOB)
        self.jobs = [self.job]
        self.run = {'id': 7, 'run_attempt': 1, 'status': 'in_progress', 'name': 'CI'}
        self.annotations = []
        self.posts = []
        self.fail_post = False
        self.fail_read = False

    def get(self, endpoint, *, paginate=False):
        if self.fail_read:
            raise RuntimeError('GitHub temporarily unavailable\ntry later')
        if '/annotations' in endpoint:
            return copy.deepcopy(self.annotations)
        if '/actions/jobs/' in endpoint:
            return copy.deepcopy(self.job)
        if '/jobs' in endpoint:
            return [{'jobs': copy.deepcopy(self.jobs)}] if paginate else {'jobs': copy.deepcopy(self.jobs)}
        if '/actions/runs/7' in endpoint:
            return copy.deepcopy(self.run)
        if '/actions/runs?' in endpoint:
            return [{'workflow_runs': [copy.deepcopy(self.run)]}]
        raise AssertionError(endpoint)

    def post(self, endpoint):
        self.posts.append(endpoint)
        if isinstance(self.fail_post, Exception):
            raise self.fail_post
        if self.fail_post:
            raise TimeoutError('response lost after request')

    def crash(self, message='The self-hosted runner: runner lost communication with the server.'):
        self.job.update(status='completed', conclusion='failure', completed_at='2026-09-30T20:05:00Z')
        self.run['status'] = 'completed'
        self.annotations = [{'annotation_level': 'failure', 'message': message}]


class FilteredGitHub(FakeGitHub):
    """Honor provider filters so discovery tests cannot pass on ignored queries."""
    def __init__(self):
        super().__init__()
        self.created = '2026-09-30T19:00:00Z'
        self.queries = []
        self.deleted_runs = set()

    def get(self, endpoint, *, paginate=False):
        if any(endpoint.endswith(f'/actions/runs/{run_id}') for run_id in self.deleted_runs):
            raise subprocess.CalledProcessError(1, ['gh'], stderr='gh: Not Found (HTTP 404)')
        if '/actions/runs?' in endpoint:
            query = parse_qs(urlparse(endpoint).query)
            self.queries.append((endpoint, query))
            matches = query['status'][0] == self.run['status']
            if 'created' in query:
                matches = matches and job_recovery.instant(self.created) >= job_recovery.instant(query['created'][0][2:])
            return [{'workflow_runs': [copy.deepcopy(self.run)] if matches else []}]
        if '/actions/runs/' in endpoint and '/jobs' not in endpoint:
            return copy.deepcopy(self.run)
        return super().get(endpoint, paginate=paginate)


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger('spot_runner_autoscaler')
        handler = logging.NullHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.json'
        self.state = {'idle_loops': 0, 'scale_up_active': False}
        self.api = FakeGitHub()
        self.tracker = self.make_tracker()
        self.poll_count = 0

    def make_tracker(self):
        return job_recovery.JobRecovery(self.state, lambda: write_scale_state(self.path, self.state),
                                        [REPO], api=self.api)

    def poll(self, vms):
        self.poll_count += 1
        now = '2026-09-30T20:01:00Z' if self.poll_count == 1 else '2026-09-30T20:06:00Z'
        with patch('lib.job_recovery.utc_now', return_value=now):
            self.tracker.poll(vms)

    def test_saved_running_job_is_linked_to_missing_vm_and_rerun_once_after_restart(self):
        self.poll([VM])
        self.state = read_scale_state(self.path)
        self.tracker = self.make_tracker()
        self.api.crash()
        with self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
            self.poll([])
        text = ' '.join(logs.output)
        self.assertIn('causing these CI jobs to crash', text)
        self.assertIn('Build & Test', text)
        self.assertIn(JOB['html_url'], text)
        self.assertIn('Rerunning the crashed jobs', text)
        self.assertIn('terminated unexpectedly', text)
        self.assertEqual(self.api.posts, ['repos/swack-tools/oxidex/actions/jobs/11/rerun'])
        self.state = read_scale_state(self.path)
        self.tracker = self.make_tracker()
        self.poll([])
        self.assertEqual(len(self.api.posts), 1)

    def test_missing_vm_waits_for_terminal_job_and_runner_loss_evidence(self):
        self.poll([VM])
        with self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
            self.poll([])
        self.assertIn('running when', ' '.join(logs.output))
        self.assertNotIn('causing these CI jobs to crash', ' '.join(logs.output))
        self.assertEqual(self.api.posts, [])
        self.api.crash('Process completed with exit code 1.')
        self.poll([])
        self.assertEqual(self.api.posts, [])

    def test_success_cancelled_and_unrelated_runner_failures_are_not_retried(self):
        for status in ('success', 'cancelled'):
            with self.subTest(conclusion=status):
                self.setUp()
                self.poll([VM])
                self.api.crash()
                self.api.job['conclusion'] = status
                self.poll([])
                self.assertEqual(self.api.posts, [])
        self.setUp()
        self.api.job['runner_name'] = 'gha-unrelated-host-3'
        self.poll([VM])
        self.api.crash()
        self.poll([])
        self.assertEqual(self.api.posts, [])

    def test_intentional_controller_deletion_does_not_trigger_unexpected_loss_retry(self):
        self.poll([VM])
        self.state['intentional_deletions'] = [f'{VM.zone}/{VM.name}']
        self.api.crash()
        self.poll([])
        self.assertEqual(self.api.posts, [])

    def test_post_receipt_is_saved_before_request_and_uncertain_result_is_not_repeated(self):
        self.poll([VM])
        self.api.crash()
        self.api.fail_post = True
        self.poll([])
        self.state = read_scale_state(self.path)
        self.tracker = self.make_tracker()
        self.poll([])
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(next(iter(self.state['job_tracking']['jobs'].values()))['rerun'], 'uncertain')

    def test_read_failure_retains_jobs_and_inventory_for_next_poll(self):
        self.poll([VM])
        self.api.fail_read = True
        self.poll([])
        self.assertTrue(self.state['job_tracking']['jobs'])
        self.api.fail_read = False
        self.api.crash()
        self.poll([])
        self.assertEqual(len(self.api.posts), 1)

    def test_job_that_failed_before_vm_was_last_seen_is_not_attributed(self):
        self.poll([VM])
        self.api.crash()
        self.api.job['completed_at'] = '2026-09-30T19:00:00Z'
        self.poll([])
        self.assertEqual(self.api.posts, [])

    def test_manual_rerun_is_recognized_without_posting_again(self):
        self.poll([VM])
        self.api.crash()
        self.api.run['run_attempt'] = 2
        self.api.jobs = [{**self.api.job, 'id': 12, 'run_attempt': 2, 'status': 'queued'}]
        self.poll([])
        self.assertEqual(self.api.posts, [])

    def test_parallel_failed_jobs_rerun_serially_without_repeating_first_request(self):
        second = {**self.api.job, 'id': 21, 'name': 'Source tables', 'check_run_url': 'https://api.github.com/repos/swack-tools/oxidex/check-runs/21'}
        self.api.jobs.append(second)
        self.poll([VM])
        self.api.crash()
        second.update(status='completed', conclusion='failure', completed_at='2026-09-30T20:06:00Z')
        self.poll([])
        self.assertEqual(len(self.api.posts), 1)
        self.api.run.update(status='completed', run_attempt=2)
        self.api.jobs = [{**self.api.job, 'id': 12, 'run_attempt': 2, 'conclusion': 'success'}, second]
        self.poll([])
        self.assertEqual(self.api.posts, ['repos/swack-tools/oxidex/actions/jobs/11/rerun',
                                          'repos/swack-tools/oxidex/actions/jobs/21/rerun'])

    def test_definite_http_rejection_can_retry_after_permissions_recover(self):
        self.poll([VM])
        self.api.crash()
        self.api.fail_post = subprocess.CalledProcessError(1, ['gh'], stderr='gh: Forbidden (HTTP 403)')
        self.poll([])
        self.assertEqual(next(iter(self.state['job_tracking']['jobs'].values()))['rerun'], 'retry')
        self.api.fail_post = False
        self.poll([])
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(next(iter(self.state['job_tracking']['jobs'].values()))['rerun'], 'accepted')

    def test_successful_jobs_are_removed_from_polling_state(self):
        self.poll([VM])
        self.api.crash()
        self.api.job['conclusion'] = 'success'
        self.poll([VM])
        self.assertEqual(self.state['job_tracking']['jobs'], {})

    def test_job_started_after_loss_is_not_attributed_to_an_old_loss(self):
        self.poll([VM])
        self.poll([])
        self.state['job_tracking']['jobs'] = {}
        self.api.crash()
        self.api.job['started_at'] = '2026-09-30T20:07:00Z'
        self.poll([])
        self.assertEqual(self.api.posts, [])

    def test_long_running_workflow_is_retained_while_spot_job_is_still_queued(self):
        self.api = FilteredGitHub()
        self.tracker = self.make_tracker()
        self.api.job.update(status='queued', runner_name='', started_at=None)
        self.poll([VM])
        self.api.job.update(runner_name=RUNNER, started_at='2026-09-30T20:02:00Z')
        self.api.crash()
        self.poll([])
        self.assertEqual(self.api.posts, ['repos/swack-tools/oxidex/actions/jobs/11/rerun'])

    def test_each_repository_keeps_its_discovery_watermark_during_downtime(self):
        self.api = FilteredGitHub()
        self.state['job_tracking'] = {'last_poll_at': '2026-09-30T19:00:00Z'}
        self.tracker = job_recovery.JobRecovery(self.state, lambda: write_scale_state(self.path, self.state),
                                               [REPO, 'swack-tools/second'], api=self.api)
        self.poll([])
        bounds = [query['created'][0] for endpoint, query in self.api.queries if 'created' in query]
        self.assertEqual(bounds, ['>=2026-09-30T18:50:00+00:00', '>=2026-09-30T18:50:00+00:00'])

    def test_deleted_saved_run_does_not_block_other_crash_jobs_or_record_expiration(self):
        self.api = FilteredGitHub()
        self.tracker = self.make_tracker()
        self.poll([VM])
        self.api.deleted_runs.add(7)
        self.api.run['id'] = 8
        self.api.created = '2026-09-30T20:02:00Z'
        self.api.job.update(id=12, run_id=8, check_run_url='https://api.github.com/repos/swack-tools/oxidex/check-runs/12')
        self.api.crash()
        self.poll([])
        self.assertEqual(self.api.posts, ['repos/swack-tools/oxidex/actions/jobs/12/rerun'])
        with patch('lib.job_recovery.utc_now', return_value='2026-11-01T20:06:00Z'):
            self.tracker.poll([])
        self.assertNotIn('swack-tools/oxidex/11', self.state['job_tracking']['jobs'])

    def test_controller_saves_job_tracking_and_recovers_it_on_next_invocation(self):
        args = args_parser().parse_args(['--once', '--state-file', str(self.path)])
        with patch('lib.job_recovery.GitHubJobsApi', return_value=self.api), \
                patch('lib.spot_runner_autoscaler.list_github_runners', return_value=[]), \
                patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[VM]), \
                patch('lib.spot_runner_autoscaler.reconcile_prices', return_value=True), \
                patch('lib.spot_runner_autoscaler.log_hourly_spend'), \
                patch('lib.job_recovery.utc_now', return_value='2026-09-30T20:01:00Z'):
            self.assertEqual(run_controller(args), 0)
        self.assertIn('swack-tools/oxidex/11', read_scale_state(self.path)['job_tracking']['jobs'])
        self.api.crash()
        with patch('lib.job_recovery.GitHubJobsApi', return_value=self.api), \
                patch('lib.spot_runner_autoscaler.list_github_runners', return_value=[]), \
                patch('lib.spot_runner_autoscaler.list_gcloud_vms', return_value=[]), \
                patch('lib.spot_runner_autoscaler.reconcile_prices', return_value=True), \
                patch('lib.spot_runner_autoscaler.log_hourly_spend'), \
                patch('lib.job_recovery.utc_now', return_value='2026-09-30T20:06:00Z'):
            self.assertEqual(run_controller(args), 0)
        self.assertEqual(self.api.posts, ['repos/swack-tools/oxidex/actions/jobs/11/rerun'])

    def test_second_controller_cannot_acquire_active_state_lock(self):
        import fcntl
        args = args_parser().parse_args(['--once', '--state-file', str(self.path)])
        with self.path.with_suffix('.json.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'Another controller owns state'):
                run_controller(args)

    def test_api_adapter_paginates_reads_and_targets_one_job_for_rerun(self):
        responses = [SimpleNamespace(stdout='[{"jobs": []}]'), SimpleNamespace(stdout='')]
        with patch('lib.job_recovery.subprocess.run', side_effect=responses) as command:
            api = job_recovery.GitHubJobsApi()
            self.assertEqual(api.get('repos/swack-tools/oxidex/actions/runs/7/jobs', paginate=True), [{'jobs': []}])
            api.post('repos/swack-tools/oxidex/actions/jobs/11/rerun')
        self.assertEqual(command.call_args_list[0].args[0],
                         ['gh', 'api', 'repos/swack-tools/oxidex/actions/runs/7/jobs', '--paginate', '--slurp'])
        self.assertEqual(command.call_args_list[1].args[0],
                         ['gh', 'api', '--method', 'POST', 'repos/swack-tools/oxidex/actions/jobs/11/rerun'])

    def test_terminal_run_is_retained_until_jobs_can_be_read_successfully(self):
        self.api = FilteredGitHub()
        self.tracker = self.make_tracker()
        self.api.job.update(status='queued', runner_name='', started_at=None)
        self.poll([VM])
        self.api.job.update(runner_name=RUNNER, started_at='2026-09-30T20:02:00Z')
        self.api.crash()
        get = self.api.get
        def unavailable_jobs(endpoint, **kwargs):
            if '/jobs' in endpoint:
                raise RuntimeError('temporary jobs API failure')
            return get(endpoint, **kwargs)
        with patch.object(self.api, 'get', side_effect=unavailable_jobs):
            self.poll([])
        self.poll([])
        self.assertEqual(self.api.posts, ['repos/swack-tools/oxidex/actions/jobs/11/rerun'])

    def test_uncertain_receipt_is_confirmed_and_rerun_outcome_logged_once(self):
        self.poll([VM])
        self.api.crash()
        self.api.fail_post = True
        self.poll([])
        self.api.run['run_attempt'] = 2
        self.api.jobs = [{**self.api.job, 'id': 12, 'run_attempt': 2, 'conclusion': 'success'}]
        with self.assertLogs('spot_runner_autoscaler', 'INFO') as logs:
            self.poll([])
        self.assertIn('rerun completed with success', ' '.join(logs.output))
        self.assertEqual(next(iter(self.state['job_tracking']['jobs'].values()))['rerun'], 'accepted')
        self.poll([])
        self.assertEqual(len(self.api.posts), 1)

    def test_new_loss_with_runner_error_is_retried_even_without_a_prior_running_job_poll(self):
        self.poll([VM])
        self.state['job_tracking']['jobs'] = {}
        self.api.crash()
        self.poll([])
        self.assertEqual(len(self.api.posts), 1)


if __name__ == '__main__':
    unittest.main()
