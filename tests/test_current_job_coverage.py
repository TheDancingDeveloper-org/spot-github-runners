"""Bounded current-job coverage controls using only a synthetic GitHub API."""
import copy
import io
import json
import subprocess
import threading
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from urllib.parse import parse_qs,urlparse
from lib import fleet_evidence
from lib.job_recovery import JobRecovery

REPO='org/oxidex';NOW='2026-10-04T17:00:00Z'
STATUSES=('in_progress','queued','waiting','pending','requested')


class FakeApi:
    def __init__(self,status='waiting',gone=False,defect=None):
        self.status=status;self.gone=gone;self.defect=defect;self.calls=[];self.coverage_on_read=[];self.current_coverage=lambda:None
    def get(self,endpoint,*,paginate=False):
        self.calls.append(endpoint)
        self.coverage_on_read.append(self.current_coverage())
        if '/actions/runs?' in endpoint:
            status=parse_qs(urlparse(endpoint).query)['status'][0]
            runs=[] if self.gone or status!=self.status else [{'id':7,'status':self.status,'run_attempt':1}]
            pages=[dict(total_count=len(runs),workflow_runs=runs)]
            if status==self.status:
                if self.defect=='count':pages[0]['total_count']=2
                if self.defect=='cap':pages[0]['total_count']=1000
                if self.defect=='malformed':pages[0].pop('total_count')
                if self.defect=='unknown':pages[0]['workflow_runs'][0]['status']='unrecognized'
                if self.defect=='api':raise RuntimeError('partial current scan')
            return pages
        if endpoint.endswith('/actions/runs/7'):
            raise subprocess.CalledProcessError(1,['gh'],stderr='HTTP 404')
        if '/jobs?' in endpoint:
            pages=[{'total_count':1,'jobs':[{'id':11,'name':'build','status':self.status,
                'labels':['X64','n2-standard-16'],'runner_name':'','runner_group_id':10}]}]
            if self.defect=='jobs_count':pages[0]['total_count']=2
            return pages
        raise AssertionError(endpoint)
    def post(self,*_):raise AssertionError('POST forbidden')


class CurrentJobCoverageTests(unittest.TestCase):
    def poll(self,status='waiting',gone=False,defect=None,initial=None):
        state=copy.deepcopy(initial or {});snapshots=[];api=FakeApi(status,gone,defect)
        tracker=JobRecovery(state,lambda:snapshots.append(copy.deepcopy(state)),[REPO],api=api)
        api.current_coverage=lambda:tracker.data.get('active_coverage_by_repository',{}).get(REPO,{}).get('complete')
        if gone:tracker.data['runs'][REPO+'/7']={'repository':REPO,'run_id':7,'observed_at':NOW}
        with patch('lib.job_recovery.utc_now',return_value=NOW),patch('subprocess.run',side_effect=AssertionError('provider command forbidden')):
            tracker.poll([])
        return state,api,snapshots

    def test_all_five_nonterminal_scopes_are_current_complete_and_preserve_pending_jobs(self):
        for status in STATUSES:
            with self.subTest(status=status):
                state,api,snapshots=self.poll(status)
                self.assertTrue(state['job_tracking'].get('capacity_jobs'))
                receipt=state['job_tracking'].get('active_coverage_by_repository',{}).get(REPO,{})
                self.assertIs(receipt.get('complete'),True)
                self.assertEqual(receipt.get('schema'),1)
                self.assertEqual(receipt.get('enumerated_statuses'),list(STATUSES))
                self.assertTrue(api.coverage_on_read);self.assertTrue(all(value is False for value in api.coverage_on_read))
                queries=[parse_qs(urlparse(endpoint).query) for endpoint in api.calls if '/actions/runs?' in endpoint]
                for query in queries:
                    if query['status'][0] in STATUSES:self.assertNotIn('created',query)

    def test_tracked404_preserves_exact_obligation_and_reports_incomplete_coverage(self):
        state,_,_=self.poll(gone=True);data=state['job_tracking']
        self.assertIn(REPO+'/7',data['runs'])
        receipt=data.get('active_coverage_by_repository',{}).get(REPO,{})
        self.assertIs(receipt.get('complete'),False)
        self.assertEqual(receipt.get('unavailable_run_ids'),[7])

    def test_failed_truncated_capped_or_malformed_scan_invalidates_previous_green(self):
        initial={'job_tracking':{'active_coverage_by_repository':{REPO:dict(schema=1,observed_at=NOW,
            complete=True,enumerated_statuses=list(STATUSES),unavailable_run_ids=[])}}}
        for defect in ('count','cap','malformed','unknown','api','jobs_count'):
            with self.subTest(defect=defect):
                state,api,snapshots=self.poll(defect=defect,initial=initial)
                receipt=state['job_tracking']['active_coverage_by_repository'][REPO]
                self.assertIs(receipt['complete'],False)
                self.assertTrue(api.coverage_on_read);self.assertTrue(all(value is False for value in api.coverage_on_read))

    def test_requested_compatible_job_is_pending_and_incompatible_job_stays_filtered(self):
        job=dict(status='requested',labels=['X64','n2-standard-16'],runner_group_id=10)
        signals=fleet_evidence.workload_signals([job],[{'X64','n2-standard-16'}],1000,group_ids={10})
        self.assertTrue(signals['pending'])
        self.assertFalse(fleet_evidence.workload_signals([job],[{'ARM64'}],1000,group_ids={10})['pending'])

    def test_coverage_consumer_requires_every_repository_fresh_explicit_scope(self):
        check=getattr(fleet_evidence,'current_job_coverage',None)
        self.assertIsNotNone(check,'production completeness consumer is required')
        state,_,_=self.poll();data=state['job_tracking'];timestamp=fleet_evidence.epoch(NOW)
        self.assertTrue(check(data,[REPO],timestamp))
        self.assertFalse(check(data,[REPO,'org/second'],timestamp))
        self.assertFalse(check(data,[REPO],timestamp+451))
        self.assertFalse(check(data,[REPO],timestamp-1))
        missing=copy.deepcopy(data);missing.pop('active_coverage_by_repository')
        self.assertFalse(check(missing,[REPO],timestamp))

    def test_readonly_dry_run_uses_real_scan_coverage_without_save_or_post(self):
        from lib import tier_controller
        from lib import spot_runner_autoscaler as controller
        args=controller.args_parser().parse_args(['--scaling-mode','tiered','--dry-run','--job-repository',REPO])
        saved={};stdout=io.StringIO()
        with patch('lib.job_recovery.GitHubJobsApi',return_value=FakeApi()),\
             patch('lib.job_recovery.utc_now',return_value=NOW),\
             patch('time.time',return_value=fleet_evidence.epoch(NOW)),\
             patch.object(controller,'list_gcloud_vms',return_value=[]),\
             patch.object(controller,'list_github_runners',return_value=[]),\
             patch.object(controller,'read_utilization',return_value=[]),\
             patch.object(controller,'discover_tier_offers',return_value=[]),\
             patch.object(controller,'write_scale_state',side_effect=AssertionError('dry-run save forbidden')),\
             patch('subprocess.run',side_effect=AssertionError('provider call forbidden')),redirect_stdout(stdout):
            self.assertEqual(tier_controller.dry_run(args,saved),0)
        self.assertEqual(saved,{})
        self.assertTrue(json.loads(stdout.getvalue())['workload']['complete'])

    def test_bounded_workload_cache_cannot_certify_coverage_after_dropping_active_record(self):
        state,_,_=self.poll();data=state['job_tracking']
        data['capacity_jobs']={str(index):dict(repository=REPO,observed_at=NOW,status='completed') for index in range(5000)}
        data['capacity_jobs']['last-active']=dict(repository=REPO,observed_at=NOW,status='waiting')
        fleet_evidence.remember_jobs(data,REPO,{}, {},NOW)
        self.assertFalse(fleet_evidence.current_job_coverage(data,[REPO],fleet_evidence.epoch(NOW)))

    def test_twenty_four_current_repositories_require_every_successful_scope_after_restart(self):
        repositories=[f'org/repo{index}' for index in range(24)];state={}
        with patch('lib.job_recovery.utc_now',return_value=NOW),patch('subprocess.run',side_effect=AssertionError('provider forbidden')):
            JobRecovery(state,lambda:None,repositories,api=FakeApi()).poll([])
            self.assertTrue(fleet_evidence.current_job_coverage(state['job_tracking'],repositories,fleet_evidence.epoch(NOW)))
            state=json.loads(json.dumps(state));api=FakeApi();read=api.get
            def partial(endpoint,**kwargs):
                if '/repo23/' in endpoint:raise RuntimeError('one repository incomplete')
                return read(endpoint,**kwargs)
            with patch.object(api,'get',side_effect=partial):JobRecovery(state,lambda:None,repositories,api=api).poll([])
        self.assertFalse(fleet_evidence.current_job_coverage(state['job_tracking'],repositories,fleet_evidence.epoch(NOW)))
        self.assertIs(state['job_tracking']['active_coverage_by_repository']['org/repo23']['complete'],False)

    def test_production_scan_bounds_three_readonly_queries_before_main_thread_state_changes(self):
        api=FakeApi();read=api.get;state={};main=threading.get_ident();lock=threading.Lock()
        barrier=threading.Barrier(3);active=0;peak=0;query_finished=[]
        def save():self.assertEqual(threading.get_ident(),main)
        tracker=JobRecovery(state,save,[REPO],api=api)
        def observed(endpoint,**kwargs):
            nonlocal active,peak
            if '/actions/runs?' in endpoint:
                self.assertIs(tracker.data['active_coverage_by_repository'][REPO]['complete'],False)
                with lock:active+=1;peak=max(peak,active)
                barrier.wait(timeout=1)
                response=read(endpoint,**kwargs)
                with lock:active-=1;query_finished.append(endpoint)
                return response
            self.assertEqual(threading.get_ident(),main)
            self.assertEqual(len(query_finished),6)
            return read(endpoint,**kwargs)
        with patch('lib.job_recovery.utc_now',return_value=NOW),patch.object(api,'get',side_effect=observed),\
             patch('subprocess.run',side_effect=AssertionError('provider forbidden')):
            tracker.poll([])
        self.assertEqual(peak,3)
        self.assertIs(state['job_tracking']['active_coverage_by_repository'][REPO]['complete'],True)
        self.assertEqual(state['job_tracking']['active_coverage_by_repository'][REPO]['enumerated_statuses'],list(STATUSES))

    def test_production_failed_batch_waits_every_submitted_read_without_partial_publication(self):
        api=FakeApi();read=api.get;finished=[];lock=threading.Lock();state={}
        def observed(endpoint,**kwargs):
            with lock:finished.append(endpoint)
            if '/actions/runs?' in endpoint and parse_qs(urlparse(endpoint).query)['status'][0]=='in_progress':
                raise RuntimeError('one failed scope')
            return read(endpoint,**kwargs)
        with patch('lib.job_recovery.utc_now',return_value=NOW),patch.object(api,'get',side_effect=observed),\
             patch('subprocess.run',side_effect=AssertionError('provider forbidden')):
            JobRecovery(state,lambda:None,[REPO],api=api).poll([])
        self.assertEqual(len(finished),6)
        self.assertEqual(state['job_tracking']['runs'],{})
        self.assertEqual(state['job_tracking']['capacity_jobs'] if 'capacity_jobs' in state['job_tracking'] else {},{})
        self.assertIs(state['job_tracking']['active_coverage_by_repository'][REPO]['complete'],False)


if __name__=='__main__':unittest.main()
