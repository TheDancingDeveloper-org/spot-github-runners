#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${GHA_ADMISSION_REQUIRED:-0}" == 1 ]]; then
  python3 - <<'RELEASE_CLIENT'
import runpy
import sys
import time

api = runpy.run_path('/usr/local/bin/gha-host-admission.py', run_name='gha_release_client')
request = api['request']
OrphanRunnerHook = api.get('OrphanRunnerHook', ())
failures = 0
while True:
    try:
        if request('release') is True:
            break
    except OrphanRunnerHook:
        # The Worker has ended. Its worker-bound row is reclaimed by the
        # daemon; a stale completion hook must not clean a subsequent job.
        print('Runner Worker ended; skipping stale completion cleanup', file=sys.stderr)
        raise SystemExit(75)
    except RuntimeError as error:
        # Existing containers still have the old request decoder. The root
        # daemon also marks its terminal error in their error string.
        if str(error).startswith('ORPHAN_RUNNER_HOOK: '):
            print('Runner Worker ended; skipping stale completion cleanup', file=sys.stderr)
            raise SystemExit(75)
    except (OSError, ValueError, KeyError, TypeError):
        # The daemon may restart or disconnect after removing the row but
        # before replying. Release is idempotent for this Worker identity.
        pass
    failures += 1
    if failures == 1 or failures % 12 == 0:
        print('Runner lease release unacknowledged; retrying in 5 seconds', file=sys.stderr)
    # Each request retains its 30-second socket timeout. Do not abandon a
    # Worker's release obligation or reuse it before the ACK.
    time.sleep(5)
RELEASE_CLIENT
fi
exec python3 /usr/local/bin/gha-runner-clean-target.py
