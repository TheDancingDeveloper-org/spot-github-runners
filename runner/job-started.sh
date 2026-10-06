#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${GHA_ADMISSION_REQUIRED:-0}" == 1 ]]; then
  exec python3 /usr/local/bin/gha-host-admission.py acquire
fi
