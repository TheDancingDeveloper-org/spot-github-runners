#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${GHA_ADMISSION_REQUIRED:-0}" == 1 ]]; then
  python3 - <<'RELEASE_CLIENT'
import runpy
import sys
import time

request = runpy.run_path('/usr/local/bin/gha-host-admission.py', run_name='gha_release_client')['request']
failures = 0
while True:
    try:
        if request('release') is True:
            break
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        # The daemon may restart or disconnect after removing the row but
        # before replying. Release is idempotent for this container identity.
        pass
    failures += 1
    if failures == 1 or failures % 12 == 0:
        print('Runner lease release unacknowledged; retrying in 5 seconds', file=sys.stderr)
    # Each request retains its 30-second socket timeout. Do not abandon a
    # persistent container's release obligation or reuse it before the ACK.
    time.sleep(5)
RELEASE_CLIENT
fi
exec python3 /usr/local/bin/gha-runner-clean-target.py
