#!/usr/bin/env bash
set -Eeuo pipefail

state="${RUNNER_STATE:-/runner-state}"
config="$state/.runner"
mkdir -p "$state"
if [[ ! -x "$state/run.sh" ]]; then
  cp -a /home/runner/. "$state/"
fi
cd "$state"
if [[ "${RUNNER_CONFIGURE_ONLY:-0}" == 1 ]]; then
  if [[ -f "$config" ]]; then
    exit 0
  fi
  : "${GITHUB_RUNNER_URL:?GITHUB_RUNNER_URL is required}"
  : "${GITHUB_RUNNER_TOKEN:?GITHUB_RUNNER_TOKEN is required for initial registration}"
  ./config.sh --unattended --replace --url "$GITHUB_RUNNER_URL" \
    --token "$GITHUB_RUNNER_TOKEN" --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" --runnergroup "$RUNNER_GROUP_NAME" \
    --work /work
  exit 0
fi
if [[ ! -f "$config" ]]; then
  echo "Runner state is not registered at $config" >&2
  exit 1
fi
exec ./run.sh
