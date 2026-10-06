"""Exercise persisted migration completion through real identity-scoped disk cleanup."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from lib import spot_runner_autoscaler as controller
from lib.fleet_migration import begin_migration,source_absence_proven,record_missing_sources,record_retirement_completion
from lib.runner_fleet import GceVm


class MigrationSourceDiskCompletionTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'state.json'
        self.args=controller.args_parser().parse_args(['--project','project','--scaling-mode','tiered'])
        self.disks=[dict(source=f'https://www.googleapis.com/compute/v1/projects/project/zones/z/disks/{name}',
                         diskId=str(index),boot=index==1,autoDelete=False) for index,name in ((1,'boot'),(2,'data'))]
        self.source=GceVm('oxidex-runners-old','z','RUNNING','n2-standard-4',True,4,16,
                          instance_id='123',disks=tuple(self.disks))
        self.state={};begin_migration([self.source],('expanded',),self.state,self.path,'oom')
        controller.begin_host_operation('delete',self.source,[],[self.source],self.state,self.path,
            drained=True,migration_retirement=dict(started_at=self.state['migration']['started_at'],survivor_ids=[]))
        self.state['intentional_deletions']=['z/'+self.source.name]
        self.state['intentional_deletion_ids']={'z/'+self.source.name:'123'}
        controller.write_scale_state(self.path,self.state);self.state=json.loads(self.path.read_text())
        self.inventory={row['source']:dict(selfLink=row['source'],id=row['diskId'],status='READY',users=[]) for row in self.disks}
        self.calls=[];self.keep_disk=False

    def provider(self,args,**kwargs):
        self.calls.append(args)
        if args[:3]==['gcloud','compute','instances'] and 'list' in args:
            return SimpleNamespace(stdout='[]',returncode=0)
        if args[:3]==['gcloud','compute','disks'] and 'list' in args:
            return SimpleNamespace(stdout=json.dumps(list(self.inventory.values())),returncode=0)
        if args[:4]==['gcloud','compute','disks','delete']:
            if not self.keep_disk:
                self.inventory={key:value for key,value in self.inventory.items() if key.rsplit('/',1)[-1]!=args[4]}
            return SimpleNamespace(stdout='',returncode=0)
        raise AssertionError('Unexpected external command: '+str(args))

    def reconcile(self):
        controller.write_scale_state(self.path,self.state)
        with patch('lib.vm_recovery.subprocess.run',side_effect=self.provider):
            controller.reconcile_host_operation([],[],self.args,self.state,self.path)
        self.state=json.loads(self.path.read_text())

    def test_persisted_delete_vm_absent_cleans_non_auto_delete_drives_before_receipt(self):
        self.reconcile()
        self.assertFalse(self.inventory,'all recorded drives must actually be absent')
        self.assertEqual([row[4] for row in self.calls if row[:4]==['gcloud','compute','disks','delete']],['boot','data'])
        self.assertNotIn('host_operation',self.state)
        receipt=self.state['migration']['completed_retirements']['123']
        self.assertEqual(receipt['vm']['disks'],self.state['migration']['sources'][0]['disks'])
        self.assertTrue(source_absence_proven(self.state['migration']['sources'][0],self.state))

    def test_partial_disk_cleanup_after_restart_holds_then_confirms_all_original_drives(self):
        self.keep_disk=True;self.reconcile()
        self.assertIn('host_operation',self.state)
        self.assertNotIn('completed_retirements',self.state['migration'])
        self.keep_disk=False;self.calls=[];self.reconcile()
        self.assertFalse(self.inventory);self.assertNotIn('host_operation',self.state)

    def test_changed_attached_foreign_or_unknown_drives_do_not_certify_or_delete(self):
        for defect in ('changed','attached','foreign','unknown','alias','abbreviated'):
            with self.subTest(defect=defect):
                self.setUp()
                if defect=='changed':self.inventory[self.disks[1]['source']]['id']='changed'
                if defect=='attached':self.inventory[self.disks[1]['source']]['users']=['another-vm']
                if defect=='foreign':
                    for record in (self.state['host_operation']['vm'],self.state['migration']['sources'][0]):
                        record['disks'][1]['source']=record['disks'][1]['source'].replace('/project/','/foreign/')
                if defect in ('alias','abbreviated'):
                    for record in (self.state['host_operation']['vm'],self.state['migration']['sources'][0]):
                        record['disks'][1]['source']=record['disks'][1]['source'].replace('https://www.googleapis.com/compute/v1/','' if defect=='abbreviated' else 'https://compute.googleapis.com/compute/v1/')
                if defect=='unknown':
                    for record in (self.state['host_operation']['vm'],self.state['migration']['sources'][0]):record['disks'][1].pop('diskId')
                self.reconcile()
                self.assertIn('host_operation',self.state);self.assertNotIn('completed_retirements',self.state['migration'])
                self.assertFalse(any(row[:5]==['gcloud','compute','disks','delete','data'] for row in self.calls))

    def test_delete_cleanup_cached_success_requires_fresh_disk_absence_before_receipt(self):
        self.state['host_operation'].update(kind='delete_cleanup',drives_deleted=True)
        self.inventory[self.disks[1]['source']]['users']=['another-vm'];self.reconcile()
        self.assertIn('host_operation',self.state);self.assertNotIn('completed_retirements',self.state['migration'])

    def test_recovery_completion_does_not_use_cached_cleanup_when_recorded_disk_remains(self):
        record=copy.deepcopy(self.state['migration']['sources'][0])
        self.state['pending_vm_recovery']={'old':record}
        self.state['host_operation'].update(kind='recovery',recovery_key='old',drives_deleted=True)
        self.inventory[self.disks[1]['source']]['users']=['another-vm'];self.reconcile()
        self.assertIn('host_operation',self.state)
        self.assertIsNot(self.state['pending_vm_recovery']['old'].get('_cleanup_complete'),True)
        self.inventory[self.disks[1]['source']]['users']=[];self.reconcile()
        self.assertFalse(self.inventory);self.assertNotIn('host_operation',self.state)
        self.assertTrue(self.state['pending_vm_recovery']['old']['_cleanup_complete'])
        self.assertNotIn('completed_retirements',self.state['migration'],'preemption is recovery, not intentional retirement')

    def test_receipt_helper_refuses_unverified_persisted_deletion_intent(self):
        record_retirement_completion(self.state['host_operation'],self.state)
        self.assertNotIn('completed_retirements',self.state['migration'])
        self.assertFalse(source_absence_proven(self.state['migration']['sources'][0],self.state))

    def test_legacy_completed_recovery_marker_gets_reverified_without_inventing_disk_proof(self):
        self.state.pop('host_operation')
        source=copy.deepcopy(self.state['migration']['sources'][0])
        self.state['pending_vm_recovery']={'old':dict(source,_cleanup_complete=True)}
        record_missing_sources([],self.state)
        self.assertFalse(self.state['pending_vm_recovery']['old']['_cleanup_complete'])
        self.assertNotIn('_disks_absent',self.state['pending_vm_recovery']['old'])
        self.inventory[self.disks[1]['source']]['users']=['another-vm']
        with patch('lib.vm_recovery.subprocess.run',side_effect=self.provider):
            controller.reconcile_lost_vms([],[],self.args,self.state,self.path)
        self.assertEqual(self.state['host_operation']['kind'],'recovery')
        self.reconcile()
        self.assertIn('host_operation',self.state)
        self.assertFalse(source_absence_proven(source,self.state))
        self.inventory[self.disks[1]['source']]['users']=[]
        self.reconcile();self.assertIn('host_operation',self.state)
        self.reconcile();self.assertNotIn('host_operation',self.state)
        self.assertFalse(self.inventory);self.assertTrue(source_absence_proven(source,self.state))
        self.assertNotIn('completed_retirements',self.state['migration'])

    def test_group_cleanup_failure_does_not_mark_recovery_or_retirement_complete(self):
        for kind in ('delete','recovery'):
            with self.subTest(kind=kind):
                self.setUp();self.state['host_operation'].update(kind=kind,drained=True,drives_deleted=True,drain_group_id=77)
                if kind=='recovery':
                    self.state['pending_vm_recovery']={'old':copy.deepcopy(self.state['migration']['sources'][0])}
                    self.state['host_operation']['recovery_key']='old'
                with patch('lib.runner_drain.cleanup_group',side_effect=RuntimeError('group inventory incomplete')):
                    self.reconcile()
                self.assertFalse(self.inventory)
                self.assertIn('host_operation',self.state)
                self.assertNotIn('completed_retirements',self.state['migration'])
                if kind=='recovery':self.assertIsNot(self.state['pending_vm_recovery']['old'].get('_cleanup_complete'),True)
                # The retry still calls real disk inventory; group success is the final barrier.
                self.calls=[]
                with patch('lib.runner_drain.cleanup_group',return_value=None):self.reconcile()
                self.assertNotIn('host_operation',self.state)
                self.assertTrue(any(row[:4]==['gcloud','compute','disks','list'] for row in self.calls))
                self.assertTrue(source_absence_proven(self.state['migration']['sources'][0],self.state))

if __name__=='__main__':unittest.main()
