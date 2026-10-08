"""Root-owned Task20 dispatch into authenticated sealed code, as UID1001."""
import os
from pathlib import Path
import re
import runpy
import sys


def main(args):
    if len(args)!=4:raise ValueError('Fixed Task20 runtime identity required')
    stage,head,project,attempt=args
    if not re.fullmatch('[0-9a-f]{40}',head) or not re.fullmatch('qualification-'+head[:12]+'-[0-9a-f]{32}',project) or not re.fullmatch('final-r[1-9][0-9]*',attempt):
        raise ValueError('Malformed Task20 runtime identity')
    names={'task20-bootstrap':'task20_bootstrap.py','task20-execute':'task20_worker.py',
           'task20-pack':'task20_results.py','task20-replay':'task20_results.py'}
    if stage not in names:raise ValueError('Unknown Task20 fixed runtime command')
    for key in list(os.environ):
        if key.startswith(('PYTHON','GIT_')):del os.environ[key]
    os.environ.update(PYTHONDONTWRITEBYTECODE='1',GIT_NO_REPLACE_OBJECTS='1',
                      GIT_CONFIG_GLOBAL='/dev/null',OXIDEX_OPS_DIR='/target/ops',
                      OXIDEX_TARGET_ROOT='/target',CARGO_TARGET_DIR='/target/'+attempt)
    os.environ['PATH']='/qualification-bin:'+os.environ.get('PATH','/usr/bin:/bin')
    code=Path('/target/checkout/tools/remote-build')
    sys.path[:]=[str(code),*[entry for entry in sys.path if entry.startswith(('/usr/','/opt/'))]]
    program=code/names[stage]
    arguments=[] if stage=='task20-bootstrap' else ['--round',attempt,'--expected-head',head,'--run-id',project]
    if stage in ('task20-pack','task20-replay'):arguments.insert(0,'--pack' if stage=='task20-pack' else '--replay')
    os.chdir('/src' if stage=='task20-bootstrap' else '/target/task20-source')
    sys.argv=[str(program),*arguments]
    runpy.run_path(str(program),run_name='__main__')


if __name__=='__main__':main(sys.argv[1:])
