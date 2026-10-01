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

required_packages=(ca-certificates curl gnupg git jq xfsprogs util-linux)
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
printf '%s' '{{DOCKERFILE_B64}}' | base64 -d >/opt/gha-runners/build/Dockerfile
printf '%s' '{{ENTRYPOINT_B64}}' | base64 -d >/opt/gha-runners/build/entrypoint.sh
printf '%s' '{{JOB_COMPLETED_B64}}' | base64 -d >/opt/gha-runners/build/job-completed.sh
printf '%s' '{{CLEAN_TARGET_B64}}' | base64 -d >/opt/gha-runners/build/clean_target.py
chmod 0755 /opt/gha-runners/build/entrypoint.sh
docker build --pull -t oxidex-self-hosted-runner:{{RUNNER_VERSION}} /opt/gha-runners/build

ORG_URL=https://github.com/{{ORG}}
RUNNER_LABELS='{{RUNNER_LABELS}}'
RUNNER_GROUP='{{RUNNER_GROUP}}'
RUNNER_PREFIX='{{RUNNER_PREFIX}}'
REGISTRATION_TOKEN={{REGISTRATION_TOKEN}}
RUNNER_COUNT={{RUNNER_COUNT}}
RUNNER_CPU={{RUNNER_CPU}}
RUNNER_MEMORY_GB={{RUNNER_MEMORY_GB}}
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
    -v "$state:/runner-state" -v "$work:/work" oxidex-self-hosted-runner:{{RUNNER_VERSION}}
  rm -f "$env_file"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker_args=(-d --restart unless-stopped --cpus="$RUNNER_CPU" --memory="${RUNNER_MEMORY_GB}g" \
    --name "$name" -e RUNNER_NAME="$name" -e RUNNER_LABELS="$runner_labels" \
    -e RUNNER_GROUP_NAME="$RUNNER_GROUP" -e GITHUB_RUNNER_URL="$ORG_URL" \
    -e RUNNER_STATE=/runner-state -v "$state:/runner-state" -v "$work:/work")
  docker_args+=(oxidex-self-hosted-runner:{{RUNNER_VERSION}})
  docker run "${docker_args[@]}"
done
unset REGISTRATION_TOKEN
printf '%s\n' '{{DEPLOYMENT_ID}}' >/var/lib/gha-runner-bootstrap.complete
