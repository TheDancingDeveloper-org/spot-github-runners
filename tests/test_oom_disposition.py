import copy
import hashlib
import unittest
from lib.oom_disposition import dispose_false_context


class OomDispositionTests(unittest.TestCase):
    def fixture(self):
        message='python3 invoked oom-killer: gfp_mask=0x140cca'
        boot='initial-boot';at=50.0
        event=hashlib.sha256(f'{boot}:{at}:{message}'.encode()).hexdigest()
        state={'pending_oom':{'z/123':'host','other/456':'host'},
               'host_health':{'z/123':dict(instance_id='123',boot_id=boot,
                                          events=[dict(id=event,kind='host')])}}
        provider=dict(id='123',zone='zones/z',status='RUNNING',
                      creationTimestamp='1970-01-01T00:00:00Z',
                      lastStartTimestamp='1970-01-01T00:00:05Z')
        guest=dict(instance_id='123',boot_id=boot,kernel_boot_timestamp=10,
                   complete=True,boot_oom_complete=True,boot_host_oom_ids=[],
                   observed_at=100,boot_query_complete=True)
        journal=[dict(MESSAGE=message,__REALTIME_TIMESTAMP='50000000'),
                 dict(MESSAGE='oom-kill:constraint=CONSTRAINT_MEMCG,task=rustc',
                      __REALTIME_TIMESTAMP='51000000')]
        return state,provider,guest,journal

    def test_only_proven_legacy_flag_is_cleared_and_audited(self):
        state,provider,guest,journal=self.fixture()
        audit=dispose_false_context(state,provider,guest,journal,110)
        self.assertEqual(state['pending_oom'],{'other/456':'host'})
        self.assertEqual(state['oom_dispositions']['z/123'],audit)
        self.assertEqual(audit['instance_id'],'123')

    def test_false_host_disposition_preserves_independent_runner_and_builder_obligations(self):
        from lib.oom_obligations import remember_oom
        state,provider,guest,journal=self.fixture()
        remember_oom(state,'z/123','runner','initial-boot',['runner-oom'])
        remember_oom(state,'z/123','builder','initial-boot',['builder-oom'])
        audit=dispose_false_context(state,provider,guest,journal,110)
        self.assertEqual(state['pending_oom'],{'z/123':'builder','other/456':'host'})
        self.assertEqual(set(state['pending_oom_obligations']['z/123']),{'runner','builder'})
        self.assertEqual(state['pending_oom_origins']['z/123']['event_ids'],['builder-oom'])
        self.assertEqual(state['oom_dispositions']['z/123'],audit)

    def test_uncertain_or_real_evidence_cannot_mutate_state(self):
        variants=('global','unknown','earlier_boot','identity','stale','incomplete',
                  'unmatched','genuine_origin','origin_boot','truncated','future')
        for variant in variants:
            with self.subTest(variant=variant):
                state,provider,guest,journal=self.fixture()
                if variant=='global':guest['boot_host_oom_ids']=['real']
                elif variant=='unknown':journal.append(dict(MESSAGE='Out of memory: Killed process 42',__REALTIME_TIMESTAMP='60000000'))
                elif variant=='earlier_boot':guest['kernel_boot_timestamp']=130
                elif variant=='identity':guest['instance_id']='456'
                elif variant=='stale':guest['observed_at']=-20
                elif variant=='incomplete':guest['boot_query_complete']=False
                elif variant=='unmatched':state['host_health']['z/123']['events'][0]['id']='different'
                elif variant=='genuine_origin':state['pending_oom_origins']={'z/123':dict(kind='host',boot_id='initial-boot',quarantined=False)}
                elif variant=='origin_boot':state['pending_oom_origins']={'z/123':dict(kind='host',boot_id='other-boot',quarantined=True)}
                elif variant=='truncated':journal*=1000
                else:journal[-1]['__REALTIME_TIMESTAMP']='101000000'
                original=copy.deepcopy(state)
                with self.assertRaises(ValueError):dispose_false_context(state,provider,guest,journal,110)
                self.assertEqual(state,original)


if __name__=='__main__':unittest.main()
