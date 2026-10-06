#!/usr/bin/env python3
"""Install small Cargo shims that execute scripts from this Git checkout."""
import argparse
import os
from pathlib import Path
import shlex

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--bin-dir',type=Path,default=Path(os.environ.get('CARGO_HOME',str(Path.home()/'.cargo')))/'bin')
args=parser.parse_args()
repo=Path(__file__).resolve().parents[1]
args.bin_dir.mkdir(parents=True,exist_ok=True)
for name,script in [('cargo-oxidex-auto','auto_remote_build.py'),('cargo-oxidex-remote','cargo_remote.py')]:
    path=args.bin_dir/name
    # Cargo sends its subcommand name as argv[1]; remove only that argument.
    content='#!/bin/sh\nshift\nexec python3 '+shlex.quote(str(repo/'src'/script))+' "$@"\n'
    path.write_text(content);path.chmod(0o755)
    print(f'{path} -> {repo / "src" / script}')
