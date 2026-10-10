#!/usr/bin/env bash
# GCE startup script (runs on every boot). Idempotent: installs once, then
# just (re)starts the runner. Rendered by Terraform templatefile().
set -Eeuo pipefail
exec > >(logger -t gha-startup) 2>&1

ORG="${github_org}"
GROUP="${runner_group}"
LABELS="${runner_labels}"
VERSION="${runner_version}"
NAME="${runner_name}"
IDLE_MINUTES="${idle_minutes}"
SCCACHE_REDIS="${sccache_redis_url}"
MD=http://metadata.google.internal/computeMetadata/v1/instance/attributes
md() { curl -fsS -H 'Metadata-Flavor: Google' "$MD/$1" 2>/dev/null || true; }
RUNNER_HOME=/opt/actions-runner
CACHE=/var/cache/ci

# --- packages, Docker, runner user (first boot only) ---------------------------
if [[ ! -f /var/lib/gha-provisioned ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl git jq unzip zip xz-utils zstd \
    build-essential clang cmake pkg-config libssl-dev protobuf-compiler python3 python3-pip
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner
  usermod -aG docker runner # Docker for jobs: runner runs on the host, not in a container
  install -d -o runner -g runner "$CACHE" "$CACHE/sccache" "$CACHE/cargo" "$RUNNER_HOME"
  # Rust toolchain + sccache for the runner user; workflows may still pin their own.
  sudo -u runner -H bash -c 'curl -fsS --proto =https --tlsv1.2 https://sh.rustup.rs | sh -s -- -y --profile minimal'
  curl -fsSL "https://github.com/mozilla/sccache/releases/download/v0.10.0/sccache-v0.10.0-x86_64-unknown-linux-musl.tar.gz" \
    | tar -xz -C /usr/local/bin --strip-components=1 --wildcards '*/sccache'
  cd "$RUNNER_HOME"
  curl -fsSL "https://github.com/actions/runner/releases/download/v$VERSION/actions-runner-linux-x64-$VERSION.tar.gz" \
    | sudo -u runner tar -xz
  ./bin/installdependencies.sh
  touch /var/lib/gha-provisioned
fi

# --- Tailscale (optional; needed only to reach the estate sccache Redis) -------
TS_KEY=$(md tailscale-auth-key)
if [[ -n "$TS_KEY" ]]; then
  command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
  tailscale status >/dev/null 2>&1 || tailscale up --authkey="$TS_KEY" --hostname="$NAME" --advertise-tags=tag:ci-gcp --ssh=false
fi

# --- job environment + per-job cleanup hook ------------------------------------
{
  echo "CARGO_HOME=$CACHE/cargo"
  echo "RUSTC_WRAPPER=/usr/local/bin/sccache"
  echo "SCCACHE_DIR=$CACHE/sccache"
  echo "SCCACHE_CACHE_SIZE=40G"
  echo "CARGO_INCREMENTAL=0"
  echo "ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/bin/gha-job-completed"
  if [[ -n "$SCCACHE_REDIS" && -n "$TS_KEY" ]]; then echo "SCCACHE_REDIS_ENDPOINT=$SCCACHE_REDIS"; fi
} >"$RUNNER_HOME/.env"
chown runner:runner "$RUNNER_HOME/.env"
# Cargo binaries: seed the shared CARGO_HOME from the rustup install once.
[[ -x "$CACHE/cargo/bin/cargo" ]] || sudo -u runner cp -a /home/runner/.cargo/. "$CACHE/cargo/"
echo "PATH=$CACHE/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" >>"$RUNNER_HOME/.env"

cat >/usr/local/bin/gha-job-completed <<'HOOK'
#!/usr/bin/env bash
# Remove job containers/networks/volumes; keep images and build cache warm.
docker ps -aq | xargs -r docker rm -f >/dev/null 2>&1 || true
docker network prune -f >/dev/null 2>&1 || true
docker volume prune -f >/dev/null 2>&1 || true
# Keep the disk from filling: trim images when free space < 20 GiB.
avail=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)
if (( avail < 20 )); then docker system prune -af --filter until=72h >/dev/null 2>&1 || true; fi
exit 0
HOOK
chmod 0755 /usr/local/bin/gha-job-completed

# --- register once (persistent runner; caches survive between jobs) -------------
if [[ ! -f "$RUNNER_HOME/.runner" ]]; then
  TOKEN=$(md gha-reg-token)
  if [[ -z "$TOKEN" ]]; then echo "no gha-reg-token in metadata; cannot register" >&2; exit 1; fi
  sudo -u runner -H "$RUNNER_HOME/config.sh" --unattended --replace \
    --url "https://github.com/$ORG" --token "$TOKEN" --name "$NAME" \
    --runnergroup "$GROUP" --labels "$LABELS" --work /opt/actions-runner/_work
  (cd "$RUNNER_HOME" && ./svc.sh install runner)
fi
(cd "$RUNNER_HOME" && ./svc.sh start) || true

# --- self power-off when idle (scale_to_zero) ----------------------------------
if (( IDLE_MINUTES > 0 )); then
  cat >/usr/local/bin/gha-idle-check <<IDLE
#!/usr/bin/env bash
# Counts consecutive idle minutes; a running Runner.Worker means a job is active.
f=/run/gha-idle-minutes
if pgrep -f Runner.Worker >/dev/null; then echo 0 >"\$f"; exit 0; fi
n=\$(( \$(cat "\$f" 2>/dev/null || echo 0) + 1 )); echo "\$n" >"\$f"
if (( n >= $IDLE_MINUTES )); then logger -t gha-idle "idle \$n min, powering off"; systemctl poweroff; fi
IDLE
  chmod 0755 /usr/local/bin/gha-idle-check
  cat >/etc/systemd/system/gha-idle.service <<'UNIT'
[Unit]
Description=Power off the runner VM when idle
[Service]
Type=oneshot
ExecStart=/usr/local/bin/gha-idle-check
UNIT
  cat >/etc/systemd/system/gha-idle.timer <<'UNIT'
[Unit]
Description=Check runner idleness every minute
[Timer]
OnBootSec=5min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now gha-idle.timer
fi
