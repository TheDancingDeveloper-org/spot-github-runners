#!/usr/bin/env bash
set -euo pipefail

: "${RUNNER_URL:=https://github.com/swack-tools}"
: "${RUNNER_NAME:?RUNNER_NAME must be set by docker-compose.yml}"
: "${RUNNER_GROUP:=oxidex-m4air}"
: "${RUNNER_LABELS:=oxidex-m4air}"
: "${RUNNER_WORKDIR:=/runner-state/_work}"

mkdir -p "$RUNNER_HOME" "$RUNNER_WORKDIR"
cd "$RUNNER_HOME"
if [[ ! -f .runner ]]; then
  if [[ ! -r /run/secrets/runner.env ]]; then
    echo "Runner is not configured. Register it with a temporary read-only .env mount first." >&2
    exit 1
  fi
  set -a
  source /run/secrets/runner.env
  set +a
  : "${RUNNER_TOKEN:?RUNNER_TOKEN must be set in the private .env file}"
  cp -a /opt/actions-runner/. "$RUNNER_HOME/"
  ./config.sh \
    --unattended \
    --url "$RUNNER_URL" \
    --token "$RUNNER_TOKEN" \
    --name "$RUNNER_NAME" \
    --runnergroup "$RUNNER_GROUP" \
    --labels "$RUNNER_LABELS" \
    --work "$RUNNER_WORKDIR" \
    --disableupdate
fi

unset RUNNER_TOKEN
if [[ "${1:-}" == "--register-only" ]]; then
  exit 0
fi
if [[ "$#" -ne 0 ]]; then
  echo "Unexpected runner-entrypoint arguments: $*" >&2
  exit 2
fi
exec ./run.sh
