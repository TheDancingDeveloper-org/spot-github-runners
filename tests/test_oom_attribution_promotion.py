import copy
import unittest
from unittest.mock import patch

from lib.oom_obligations import (clear_oom, obligations_for, pending_kinds,
                                 promote_attributed_ooms, remember_oom)


class AttributionPromotionTests(unittest.TestCase):
    key='asia-northeast2-a/2161856278046720537'
    boot='6c5d3d5e-6694-43ec-b222-9dbd441c3df3'
    decision='8dc324ea7d8a9b9fa8604979c74639216515cc28842ebd7a17a9c07907340dad'
    kill='fa0180a9652e997572c0698fde54692b6785b0f22517ff7356d1eeed55ba5449'
    unrelated='f'*64

    def state(self):
        state={'seen_oom_ids':[self.decision,self.kill],
               'host_health':{self.key:{'boot_id':self.boot}}}
        remember_oom(state,self.key,'unknown',self.boot,[self.decision,self.kill,self.unrelated])
        remember_oom(state,self.key,'host',self.boot,['e'*64])
        remember_oom(state,'other/1','unknown',self.boot,['d'*64])
        return state

    def observation(self,role='runner',historical=False):
        events=[dict(id=identity,kind='container',workload=role,at=100)
                for identity in (self.decision,self.kill)]
        return dict(complete=True,observed_at=10000,boot_id=self.boot,
                    instance_id=self.key.rsplit('/',1)[-1],
                    events=[] if historical else events,
                    reclassified_events=events if historical else [],
                    reclassification_complete=historical)

    def promote(self,state,observed):
        with patch('lib.oom_obligations.time.time',return_value=10000):
            return promote_attributed_ooms(state,self.key,observed)

    def test_already_seen_exact_ids_transfer_without_clearing_any_repair(self):
        state=self.state();other=copy.deepcopy(obligations_for(state,'other/1'))
        self.assertEqual(self.promote(state,self.observation()),sorted([self.decision,self.kill]))
        rows=obligations_for(state,self.key)
        self.assertEqual(rows['runner']['event_ids'],sorted([self.decision,self.kill]))
        self.assertEqual(rows['unknown']['event_ids'],[self.unrelated])
        self.assertEqual(rows['host']['event_ids'],['e'*64])
        self.assertEqual(obligations_for(state,'other/1'),other)
        self.assertEqual(state['seen_oom_ids'],[self.decision,self.kill])
        self.assertEqual(pending_kinds(state,self.key),{'runner','unknown','host'})
        self.assertEqual(self.promote(state,self.observation()),[],'a second sample does not duplicate IDs')
        # Only an explicit completed runner repair can discharge its obligation.
        clear_oom(state,self.key,'runner')
        self.assertEqual(pending_kinds(state,self.key),{'unknown','host'})

    def test_complete_historical_projection_is_required_for_expired_event_ids(self):
        for complete in (False,True):
            state=self.state();observed=self.observation('builder',historical=True)
            observed['reclassification_complete']=complete
            before=copy.deepcopy(state)
            promoted=self.promote(state,observed)
            if complete:
                self.assertEqual(promoted,sorted([self.decision,self.kill]))
                self.assertIn('builder',pending_kinds(state,self.key))
            else:
                self.assertEqual(promoted,[]);self.assertEqual(state,before)

    def test_whole_unknown_origin_transfer_cannot_resurrect_legacy_projection(self):
        state={};remember_oom(state,self.key,'unknown',self.boot,[self.decision,self.kill])
        self.promote(state,self.observation())
        self.assertEqual(pending_kinds(state,self.key),{'runner'})
        self.assertEqual(state['pending_oom'][self.key],'runner')
        clear_oom(state,self.key,'runner')
        self.assertEqual(pending_kinds(state,self.key),set())

    def test_wrong_identity_boot_stale_incomplete_and_quarantine_cannot_promote(self):
        variants=[dict(instance_id='different'),dict(boot_id='different'),
                  dict(observed_at=9549),dict(observed_at=10001),dict(observed_at=10451),dict(observed_at=float('nan')),
                  dict(observed_at=True),dict(complete=False)]
        for changes in variants:
            with self.subTest(changes=changes):
                state=self.state();before=copy.deepcopy(state)
                observed=self.observation();observed.update(changes)
                self.assertEqual(self.promote(state,observed),[]);self.assertEqual(state,before)
        state=self.state();state['pending_oom_obligations'][self.key]['unknown']['quarantined']=True
        before=copy.deepcopy(state)
        self.assertEqual(self.promote(state,self.observation()),[]);self.assertEqual(state,before)
        state=self.state();state['pending_oom_obligations'][self.key]['unknown']['kind']='host'
        before=copy.deepcopy(state)
        self.assertEqual(self.promote(state,self.observation()),[]);self.assertEqual(state,before)

    def test_contradictory_claims_and_malformed_fields_preserve_unknown(self):
        for claim in (dict(id=self.kill,kind='host',workload='runner'),
                      dict(id=self.kill,kind='container',workload='builder'),
                      dict(id=self.kill,kind=[],workload='runner'),
                      dict(id='short',kind='container',workload='runner')):
            state=self.state();observed=self.observation()
            observed['events']=[observed['events'][1],claim]
            promoted=self.promote(state,observed)
            if claim['id']==self.kill:
                self.assertEqual(promoted,[])
                self.assertIn(self.kill,obligations_for(state,self.key)['unknown']['event_ids'])
            else:
                self.assertEqual(promoted,[self.kill])

    def test_wrong_boot_quarantined_or_malformed_destination_stays_held(self):
        for changes in (dict(boot_id='different'),dict(quarantined=True),dict(kind='host'),dict(event_ids=[{}])):
            state=self.state();remember_oom(state,self.key,'runner',self.boot,['a'*64])
            state['pending_oom_obligations'][self.key]['runner'].update(changes)
            before=copy.deepcopy(state)
            self.assertEqual(self.promote(state,self.observation()),[]);self.assertEqual(state,before)

    def test_legacy_container_origin_maps_but_wrong_boot_legacy_quarantine_never_does(self):
        state={};remember_oom(state,self.key,'container',self.boot,[self.decision])
        self.assertEqual(self.promote(state,self.observation()),[self.decision])
        self.assertEqual(pending_kinds(state,self.key),{'runner'})
        state={'pending_oom':{self.key:'unknown'}};before=copy.deepcopy(state)
        self.assertEqual(self.promote(state,self.observation()),[]);self.assertEqual(state,before)
