import subprocess
import tempfile
import unittest
from lib.runner_manager import args_parser, select_sizing, render_startup


class TierBootstrapTests(unittest.TestCase):
    def test_shared_cpu_tiers_fit_host_memory_and_register_requested_slots(self):
        for tier,cpu,ram,slots in [('starting',8,16,8),('expanded',8,32,16),('large',16,64,20),('bulky',32,128,16)]:
            args = args_parser().parse_args(['--max-cost','.125','--tier',tier,'--tier-slots',str(slots),
                '--cpu',str(cpu),'--ram',str(ram),'--instance-type',f'n2-standard-{cpu}',
                '--remote-builder','--runner-prefix','gha','--tier-job-memory-gib','2'])
            select_sizing(args)
            self.assertEqual(args.runner_count,slots)
            self.assertLess(args.admission_work_gib,ram)
            self.assertLessEqual(args.builder_memory_gib,args.admission_work_gib)
            script = render_startup(args,'token','group','deployment')
            self.assertNotIn('{{',script)
            self.assertIn(f'RUNNER_COUNT={slots}',script)
            with tempfile.NamedTemporaryFile('w') as file:
                file.write(script);file.flush()
                subprocess.run(['bash','-n',file.name],check=True)

    def test_tier_rejects_mismatched_host_and_unsafe_memory(self):
        for flags in (['--cpu','32','--ram','128'], ['--cpu','8','--ram','16','--host-overhead-gib','15']):
            args = args_parser().parse_args(['--max-cost','.2','--tier','starting',*flags])
            with self.assertRaises((ValueError,RuntimeError)):select_sizing(args)
