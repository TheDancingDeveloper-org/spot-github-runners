import unittest
from unittest.mock import patch
from lib import runner_manager as manager
from lib.runner_assets import STARTUP_SCRIPT
from pathlib import Path

class StrictPoolStartup(unittest.TestCase):
    def args(self,count=2):
        return manager.args_parser().parse_args(['--max-cost','.1','--heavy-runner-pool',str(count),'--cpu','32','--ram','128'])
    def test_render_fixed_members_and_caps(self):
        args=self.args()
        with patch.object(manager,'configure_storage'),patch.object(manager,'configure_cpu_platform'):manager.select_sizing(args)
        script=manager.render_startup(args,'synthetic-token','restricted-oxidex','synthetic-deployment')
        self.assertEqual((args.runner_count,args.runner_cpu,args.runner_memory_gb,args.admission_work_gib),(2,8,32,64))
        self.assertIn("config.update(role='runner',heavy_runners=names)",script)
        self.assertIn('STRICT_HEAVY_POOL=1',script)
        self.assertIn('RUNNER_CPU_LIMIT=\'8\'',script)
        self.assertIn('--memory-swap="32g"',script)
        self.assertIn('spot-heavy32',script)
        self.assertNotIn('{{',script)
    def test_insufficient_host_refused(self):
        args=self.args();args.cpu=16
        with self.assertRaises(RuntimeError):manager.strict_pool_settings(args)
        args.cpu=32;args.ram=64
        with self.assertRaises(RuntimeError):manager.strict_pool_settings(args)
    def test_no_builder_or_tier_override(self):
        args=self.args();args.remote_builder=True
        with self.assertRaises(RuntimeError):manager.strict_pool_settings(args)
        args.remote_builder=False;args.tier='bulky'
        with self.assertRaises(RuntimeError):manager.strict_pool_settings(args)
    def test_actual_group_membership_required(self):
        import json
        args=self.args();args.runner_count=2
        rows=[{'name':f'{args.runner_prefix}-{args.instance_name}-{n}','id':n}for n in (1,2)]
        with patch.object(manager,'run',return_value='\n'.join(json.dumps(r)for r in rows)):manager.verify_strict_runner_membership(args)
        for invalid in ([],rows[:1],[rows[0],dict(rows[0])]):
            with patch.object(manager,'run',return_value='\n'.join(json.dumps(r)for r in invalid)),self.assertRaises(RuntimeError):manager.verify_strict_runner_membership(args)

    def test_cached_group_and_label_ids_must_match(self):
        ids={'runner1':10,'runner2':20};rows=[{'name':name,'id':value}for name,value in ids.items()]
        manager.validate_strict_identity_join(ids,dict(ids),rows)
        for cached,targets in ((dict(ids,runner1=99),rows),(ids,[dict(rows[0],id=99),rows[1]]),(ids,[rows[0],rows[0]])):
            with self.assertRaises(RuntimeError):manager.validate_strict_identity_join(ids,cached,targets)

    def test_public_repository_group_permission_required(self):
        import json
        args=self.args()
        for allowed in (True,False,None):
            group={'visibility':'selected','name':'OxiDex'}
            if allowed is not None:group['allows_public_repositories']=allowed
            replies=[json.dumps(group),json.dumps({'repositories':[{'full_name':'swack-tools/oxidex'}]})]
            with patch.object(manager,'run',side_effect=replies):
                if allowed is True:self.assertEqual(manager.verify_runner_group(args),group)
                else:
                    with self.assertRaises(RuntimeError):manager.verify_runner_group(args)

    def test_retained_stopped_host_starts_once_before_identity_read(self):
        from dataclasses import replace
        args=self.args();args.existing_instance=True
        for status in ('STOPPED','TERMINATED'):
            vm=manager.GceVm(args.instance_name,'test-a',status,args.instance_type,True,instance_id='123')
            args.deployment_vm=vm
            with patch.object(manager,'gcloud')as cloud,patch.object(manager,'observed_deployment_vm',side_effect=[replace(vm,status='STAGING'),replace(vm,status='RUNNING')]),patch.object(manager.time,'sleep'),patch.object(manager,'wait_for_retained_ssh'):
                manager.start_retained_strict_host(args,'test-a')
            self.assertEqual(cloud.call_count,1)
            self.assertEqual(cloud.call_args.args[0][:3],['compute','instances','start'])
            self.assertTrue(args.deployment_vm.running)
        with patch.object(manager,'gcloud')as cloud:
            manager.start_retained_strict_host(args,'test-a')
            cloud.assert_not_called()
    def test_ssh_readiness_retries_connection_refusal_then_checks_id(self):
        import subprocess
        args=self.args();args.deployment_vm=manager.GceVm(args.instance_name,'test-a','RUNNING',args.instance_type,True,instance_id='123')
        replies=[subprocess.CompletedProcess([],255,'','ssh: connect to host 192.0.2.1 port 22: Connection refused\n'),
                 subprocess.CompletedProcess([],0,'123\n','')]
        with patch.object(manager.subprocess,'run',side_effect=replies) as call,patch.object(manager.time,'monotonic',return_value=0),patch.object(manager.time,'sleep'):
            manager.wait_for_retained_ssh(args,'test-a',30)
        self.assertEqual(call.call_count,2)
        self.assertTrue(all(c.kwargs['timeout']<=20 for c in call.call_args_list))

    def test_ssh_readiness_rejects_identity_and_auth_failures_without_retry(self):
        import subprocess
        args=self.args();args.deployment_vm=manager.GceVm(args.instance_name,'test-a','RUNNING',args.instance_type,True,instance_id='123')
        for code,out,err in [(0,'999\n',''),(255,'','Permission denied (publickey).'),(255,'','Host key verification failed.')]:
            with self.subTest(err=err),patch.object(manager.subprocess,'run',return_value=subprocess.CompletedProcess([],code,out,err)) as call,patch.object(manager.time,'monotonic',return_value=0):
                with self.assertRaises(RuntimeError):manager.wait_for_retained_ssh(args,'test-a',30)
                call.assert_called_once()

    def test_ssh_readiness_deadline_clips_timeout_and_never_extends(self):
        import subprocess
        args=self.args();args.deployment_vm=manager.GceVm(args.instance_name,'test-a','RUNNING',args.instance_type,True,instance_id='123')
        with patch.object(manager.subprocess,'run',side_effect=subprocess.TimeoutExpired('gcloud',2)) as call,patch.object(manager.time,'monotonic',side_effect=[28,30,30]),patch.object(manager.time,'sleep'):
            with self.assertRaises(TimeoutError):manager.wait_for_retained_ssh(args,'test-a',30)
        self.assertEqual(call.call_args.kwargs['timeout'],2)
        call.assert_called_once()

    def test_retained_start_waits_for_ssh_not_just_running(self):
        from dataclasses import replace
        args=self.args();args.existing_instance=True
        vm=manager.GceVm(args.instance_name,'test-a','STOPPED',args.instance_type,True,instance_id='123')
        args.deployment_vm=vm
        with patch.object(manager,'gcloud'),patch.object(manager,'observed_deployment_vm',return_value=replace(vm,status='RUNNING')),patch.object(manager,'wait_for_retained_ssh',create=True) as ready:
            manager.start_retained_strict_host(args,'test-a')
        ready.assert_called_once()

    def test_retained_start_rejects_changed_identity(self):
        from dataclasses import replace
        args=self.args();args.existing_instance=True
        vm=manager.GceVm(args.instance_name,'test-a','STOPPED',args.instance_type,True,instance_id='123');args.deployment_vm=vm
        with patch.object(manager,'gcloud'),patch.object(manager,'observed_deployment_vm',return_value=replace(vm,status='RUNNING',instance_id='999')),self.assertRaises(RuntimeError):
            manager.start_retained_strict_host(args,'test-a')

    def test_strict_tool_mount_is_readonly_without_blocking_sudo(self):
        import subprocess
        line=next(line for line in STARTUP_SCRIPT.splitlines()if '--mount "type=volume,src=$RUST_VOLUME' in line)
        for enabled in ('1','0'):
            script='set -euo pipefail\nSTRICT_HEAVY_POOL='+enabled+'\nRUST_VOLUME=gha-rust-synthetic\ndocker_args=()\n'+line+'\nprintf "%s\\n" "${docker_args[@]}"'
            result=subprocess.run(['bash','-c',script],capture_output=True,text=True,check=True)
            values=[value for value in result.stdout.splitlines()if value]
            if enabled=='1':
                self.assertIn('type=volume,src=gha-rust-synthetic,dst=/opt/runner-rust,readonly',values)
                self.assertNotIn('--security-opt=no-new-privileges:true',values)
            else:self.assertIn('type=volume,src=gha-rust-synthetic,dst=/opt/runner-rust,readonly',values)

    def test_canonical_startup_parity(self):
        self.assertEqual(STARTUP_SCRIPT,(Path(manager.ROOT)/'cloud-init/startup.sh').read_text())

if __name__=='__main__':unittest.main()
