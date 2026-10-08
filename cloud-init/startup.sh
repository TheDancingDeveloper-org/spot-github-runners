#!/usr/bin/env bash
# Rendered by spot_runner_manager.py and delivered as GCE startup-script metadata.
set -Eeuo pipefail
set +x
exec > >(tee -a /var/log/gha-runner-bootstrap.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
DEVICE="{{DISK_DEVICE}}"
MOUNT_POINT=/mnt/runner-data
if [[ ! -b "$DEVICE" ]]; then echo "Missing persistent runner disk $DEVICE" >&2; exit 1; fi
if ! blkid "$DEVICE" >/dev/null 2>&1; then mkfs.ext4 -F "$DEVICE"; fi
mkdir -p "$MOUNT_POINT"
UUID=$(blkid -s UUID -o value "$DEVICE")
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID $MOUNT_POINT ext4 defaults,nofail 0 2" >>/etc/fstab
mountpoint -q "$MOUNT_POINT" || mount "$MOUNT_POINT"
mkdir -p "$MOUNT_POINT/apt/lists/partial" "$MOUNT_POINT/apt/cache/archives/partial"
cat >/etc/apt/apt.conf.d/99runner-data <<EOF
Dir::State::lists "$MOUNT_POINT/apt/lists";
Dir::Cache "$MOUNT_POINT/apt/cache";
EOF

required_packages=(ca-certificates curl gnupg git jq xfsprogs util-linux rsync)
missing_packages=()
for package in "${required_packages[@]}"; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed' || missing_packages+=("$package")
done
if ((${#missing_packages[@]})); then
  apt-get clean
  rm -rf /var/lib/apt/lists/*
  apt-get update
  apt-get install -y "${missing_packages[@]}"
fi
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" >/etc/apt/sources.list.d/docker.list
apt-get clean
rm -rf /var/lib/apt/lists/*
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
mkdir -p "$MOUNT_POINT/docker" "$MOUNT_POINT/instances" "$MOUNT_POINT/work"
mkdir -p "$MOUNT_POINT/containerd"
cat >/etc/docker/daemon.json <<EOF
{"data-root":"$MOUNT_POINT/docker","log-driver":"local","log-opts":{"max-size":"20m","max-file":"3"}}
EOF
# Docker's data-root alone does not move containerd snapshots. Copy its current
# content once, then point containerd at the persistent data disk before Docker
# starts. This puts image overlays (what `df /` inside a job sees) on the 100 GB
# disk too, rather than filling the VM's small boot disk.
systemctl stop docker || true
systemctl stop containerd || true
containerd_exec=$(systemctl show -p ExecStart --value containerd)
if [[ "$containerd_exec" != *"--root $MOUNT_POINT/containerd"* ]]; then
  # Repeat an interrupted copy safely from the untouched boot-disk source.
  cp -a /var/lib/containerd/. "$MOUNT_POINT/containerd/"
fi
install -d -m 0755 /etc/systemd/system/containerd.service.d
cat >/etc/systemd/system/containerd.service.d/runner-data.conf <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/containerd --root $MOUNT_POINT/containerd --state /run/containerd
EOF
systemctl daemon-reload
systemctl enable --now containerd
systemctl enable docker
systemctl restart docker
containerd_exec=$(systemctl show -p ExecStart --value containerd)
[[ "$containerd_exec" == *"--root $MOUNT_POINT/containerd"* ]] || { echo "containerd service root is unexpected: $containerd_exec" >&2; exit 1; }
docker_root=$(docker info --format=json | jq -r .DockerRootDir)
[[ "$docker_root" == "$MOUNT_POINT/docker" ]] || { echo "Docker root is $docker_root, expected $MOUNT_POINT/docker" >&2; exit 1; }
# Remove the previous default roots after both services use the persistent disk.
rm -rf /var/lib/containerd/* /var/lib/docker/* /var/lib/apt/lists/* /var/cache/apt/archives/*
apt-get clean

install -d -m 0755 /opt/gha-runners/build
RESOURCE_MONITORING={{RESOURCE_MONITORING}}
if [[ "$RESOURCE_MONITORING" == 1 ]]; then
  curl --proto '=https' --tlsv1.2 -fsS https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh -o /run/install-ops-agent.sh
  bash /run/install-ops-agent.sh --also-install
  rm /run/install-ops-agent.sh
  cat >/etc/google-cloud-ops-agent/config.yaml <<'METRICS'
logging:
  service:
    pipelines:
      default_pipeline:
        receivers: []
metrics:
  receivers:
    hostmetrics:
      type: hostmetrics
      collection_interval: 60s
  processors:
    metrics_filter:
      type: exclude_metrics
      metrics_pattern:
        - agent.googleapis.com/cpu/*
        - agent.googleapis.com/disk/*
        - agent.googleapis.com/interface/*
        - agent.googleapis.com/network/*
        - agent.googleapis.com/swap/*
        - agent.googleapis.com/processes/*
  service:
    pipelines:
      default_pipeline:
        receivers: [hostmetrics]
        processors: [metrics_filter]
METRICS
  systemctl restart google-cloud-ops-agent
fi
printf '%s' '{{DOCKERFILE_B64}}' | base64 -d >/opt/gha-runners/build/Dockerfile
printf '%s' '{{ENTRYPOINT_B64}}' | base64 -d >/opt/gha-runners/build/entrypoint.sh
printf '%s' '{{JOB_COMPLETED_B64}}' | base64 -d >/opt/gha-runners/build/job-completed.sh
printf '%s' '{{CLEAN_TARGET_B64}}' | base64 -d >/opt/gha-runners/build/clean_target.py
printf '%s' '{{ADMISSION_B64}}' | base64 -d >/opt/gha-runners/build/host_admission.py
printf '%s' '{{JOB_STARTED_B64}}' | base64 -d >/opt/gha-runners/build/job-started.sh
printf '%s' '{{EVIDENCE_B64}}' | base64 -d >/opt/gha-runners/host_evidence.py
TIERED={{TIERED}}
STRICT_HEAVY_POOL={{STRICT_HEAVY_POOL}}
if [[ "$TIERED" == 1 ]]; then
  install -d -m 0755 /run/gha-admission
  cp /opt/gha-runners/build/host_admission.py /opt/gha-runners/host_admission.py
  printf '%s' '{{DOCKER_ADMISSION_B64}}' | base64 -d >/usr/local/bin/docker
  chmod 0755 /usr/local/bin/docker
  python3 - <<'ADMISSION_CONFIG'
import json, socket
names = ['{{RUNNER_PREFIX}}-'+socket.gethostname().split('.')[0]+'-'+str(i) for i in range(1,{{RUNNER_COUNT}}+1)]
config=dict(work_gib={{ADMISSION_WORK_GIB}},runner_gib={{RUNNER_MEMORY_GB}},builder_gib={{BUILDER_MEMORY_GIB}},runner_names=names)
if {{STRICT_HEAVY_POOL}}:
    config.update(role='runner',heavy_runners=names)
with open('/etc/gha-admission.json','w') as target:
    json.dump(config,target)
ADMISSION_CONFIG
  chmod 0644 /etc/gha-admission.json
  cat >/etc/systemd/system/gha-admission.service <<'ADMISSION_SERVICE'
[Unit]
Description=Shared runner and builder memory admission
After=docker.service
Requires=docker.service
[Service]
ExecStart=/usr/bin/python3 /opt/gha-runners/host_admission.py serve
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
ADMISSION_SERVICE
  systemctl daemon-reload
  systemctl enable --now gha-admission
  for attempt in $(seq 1 30); do [[ ! -S /run/gha-admission/socket ]] || break; sleep 1; done
  [[ -S /run/gha-admission/socket ]] || exit 1
fi
chmod 0755 /opt/gha-runners/build/entrypoint.sh
docker build --pull -t oxidex-self-hosted-runner:{{RUNNER_VERSION}} /opt/gha-runners/build

REMOTE_BUILDER={{REMOTE_BUILDER}}
if [[ "$REMOTE_BUILDER" == 1 ]]; then
  install -d -m 0755 /opt/gha-runners/builder
  printf '%s' '{{BUILDER_DOCKERFILE_B64}}' | base64 -d >/opt/gha-runners/builder/Dockerfile
  printf '%s' '{{BUILDER_ENTRYPOINT_B64}}' | base64 -d >/opt/gha-runners/builder/entrypoint.py
  printf '%s' '{{HOST_LAUNCHER_B64}}' | base64 -d >/usr/local/bin/oxidex-remote-build
  chmod 0755 /usr/local/bin/oxidex-remote-build
  printf '%s\n' '{"cpus":{{BUILDER_CPUS}},"memory_gib":{{BUILDER_MEMORY_GIB}}}' >/etc/oxidex-remote-build.json
  chmod 0644 /etc/oxidex-remote-build.json
  docker build -t oxidex-remote-builder:1.97.1 /opt/gha-runners/builder
fi

ORG_URL=https://github.com/{{ORG}}
RUNNER_LABELS='{{RUNNER_LABELS}}'
RUNNER_GROUP='{{RUNNER_GROUP}}'
RUNNER_PREFIX='{{RUNNER_PREFIX}}'
RUNNER_IMAGE=$(docker image inspect oxidex-self-hosted-runner:{{RUNNER_VERSION}} | jq -er '.[0].Id')
[[ "$RUNNER_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 1
RUST_VOLUME="gha-rust-${RUNNER_IMAGE#sha256:}"
REGISTRATION_TOKEN={{REGISTRATION_TOKEN}}
RUNNER_COUNT={{RUNNER_COUNT}}
RUNNER_MEMORY_GB={{RUNNER_MEMORY_GB}}
# Empty unless fixed sizing requested an explicit per-runner vCPU cap.
RUNNER_CPU_LIMIT='{{RUNNER_CPU_LIMIT}}'
# Docker Engine storage (images, layers, build cache and containers) must stay
# on the mounted persistent data disk. Stop if it did not mount as expected.
docker_root=$(docker info --format=json | jq -r .DockerRootDir)
[[ "$docker_root" == "$MOUNT_POINT/docker" ]] || { echo "Docker root is $docker_root, expected $MOUNT_POINT/docker" >&2; exit 1; }
for i in $(seq 1 "$RUNNER_COUNT"); do
  name="${RUNNER_PREFIX}-$(hostname -s)-${i}"
  state="$MOUNT_POINT/instances/$name"
  work="$MOUNT_POINT/work/$name"
  mkdir -p "$state" "$work"
  chown -R 1001:1001 "$state" "$work"
  runner_labels="$RUNNER_LABELS"
  env_file="/run/${name}.env"
  umask 077
  printf 'GITHUB_RUNNER_TOKEN=%s\n' "$REGISTRATION_TOKEN" >"$env_file"
  docker run --rm --env-file "$env_file" -e RUNNER_CONFIGURE_ONLY=1 \
    -e RUNNER_NAME="$name" -e RUNNER_LABELS="$runner_labels" \
    -e RUNNER_GROUP_NAME="$RUNNER_GROUP" -e GITHUB_RUNNER_URL="$ORG_URL" \
    -e RUNNER_STATE=/runner-state \
    -v "$state:/runner-state" -v "$work:/work" "$RUNNER_IMAGE"
  rm -f "$env_file"
  docker rm -f "$name" >/dev/null 2>&1 || true
  # Equal CPU weights split cores only under contention; an idle host lets one
  # job use every vCPU. Memory stays capped so one job cannot starve the others.
  docker_args=(-d --restart unless-stopped --cpu-shares=1024 --memory="${RUNNER_MEMORY_GB}g" \
    --name "$name" -e RUNNER_NAME="$name" -e RUNNER_LABELS="$runner_labels" \
    -e RUNNER_GROUP_NAME="$RUNNER_GROUP" -e GITHUB_RUNNER_URL="$ORG_URL" \
    -e RUNNER_STATE=/runner-state -v "$state:/runner-state" -v "$work:/work")
  if [[ "$TIERED" == 1 ]]; then
    docker_args+=(-e GHA_ADMISSION_REQUIRED=1 -v /run/gha-admission:/run/gha-admission:ro)
  fi
  [[ -z "$RUNNER_CPU_LIMIT" ]] || docker_args+=(--cpus="$RUNNER_CPU_LIMIT")
  docker_args+=(--mount "type=volume,src=$RUST_VOLUME,dst=/opt/runner-rust,readonly")
  [[ "$STRICT_HEAVY_POOL" != 1 ]] || docker_args+=(--memory-swap="32g")
  docker_args+=("$RUNNER_IMAGE")
  docker run "${docker_args[@]}"
done
unset REGISTRATION_TOKEN
printf '%s\n' '{{DEPLOYMENT_ID}}' >/var/lib/gha-runner-bootstrap.complete
