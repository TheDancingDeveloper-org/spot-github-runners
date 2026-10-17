#!/usr/bin/env bash
set -Eeuo pipefail
# GHA_RUNNER_CORDON_V1: the controller retires a repository host by placing this root-owned marker in a
# directory every runner container bind-mounts read-only. GitHub cannot stop a job that selects only the
# built-in labels, so refuse it here, before admission and before any job step runs. The hook failure
# fails the job immediately; GitHub reports it and does not reassign it, so re-run it on another runner.
if [[ -e /run/gha-runner-cordon/draining ]]; then
  echo "::error title=Runner host is being retired::This self-hosted runner host is draining and refused the job before any step ran. Re-run the job; it will be scheduled on another runner." >&2
  exit 1
fi
if [[ "${GHA_ADMISSION_REQUIRED:-0}" == 1 ]]; then
  exec python3 /usr/local/bin/gha-host-admission.py acquire
fi
