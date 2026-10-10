#!/usr/bin/env bash
# GCE startup script (runs on every boot). Idempotent: installs once, then
# just (re)starts the runner. Rendered by Terraform templatefile().
set -Eeuo pipefail
# google_metadata_script_runner copies stdout/stderr to the journal and the serial
# console ("startup-script:" lines), readable without SSH. Never echo secrets.
exec 2>&1
trap 'echo "gha-startup: failed at line $LINENO (exit $?)"' ERR

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

# --- self power-off when idle (scale_to_zero) ----------------------------------
# Installed first so the VM always stops itself, even if a later step fails.
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


# --- packages, Docker, runner user (first boot only) ---------------------------
if [[ ! -f /var/lib/gha-provisioned ]]; then
  export DEBIAN_FRONTEND=noninteractive
  # A previous boot may have been stopped mid-install, and unattended-upgrades
  # holds the dpkg lock on a fresh image: repair, then wait for locks.
  APT="apt-get -o DPkg::Lock::Timeout=900"
  dpkg --configure -a || true
  # An interrupted earlier "apt-get update" leaves InRelease files without their
  # package lists, so later updates "Hit" and never fetch them: start clean.
  rm -rf /var/lib/apt/lists/*
  # GCE Ubuntu images ship with only "main"; clang, protobuf-compiler and pip are in universe.
  add-apt-repository -y -n universe || sed -i 's/^Components: main restricted$/Components: main restricted universe/' /etc/apt/sources.list.d/ubuntu.sources
  $APT update
  $APT install -y --no-install-recommends ca-certificates curl git jq unzip zip xz-utils zstd \
    build-essential cmake pkg-config libssl-dev python3
  # Optional extras from universe; on the GCE image universe has not resolved (follow-up).
  $APT install -y --no-install-recommends clang protobuf-compiler python3-pip || echo "gha-startup: optional universe packages unavailable"
  # codex fork-ci (rust-heavy): linux-sandbox links libcap via pkg-config; its tests run bubblewrap.
  $APT install -y --no-install-recommends libcap-dev libasound2-dev bubblewrap || echo "gha-startup: codex CI packages unavailable"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
    >/etc/apt/sources.list.d/docker.list
  $APT update
  $APT install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
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

# --- catch-up: packages added after a disk was first provisioned -----------------
# The first-boot block above is skipped on an already-provisioned disk, so
# install any later additions here, only when missing.
missing=()
for pkg in libcap-dev libasound2-dev bubblewrap; do
  dpkg-query -W -f='$${db:Status-Status}' "$pkg" 2>/dev/null | grep -qx installed || missing+=("$pkg")
done
if (( $${#missing[@]} > 0 )); then
  apt-get -o DPkg::Lock::Timeout=900 update
  apt-get -o DPkg::Lock::Timeout=900 install -y --no-install-recommends "$${missing[@]}" \
    || echo "gha-startup: catch-up install failed: $${missing[*]}"
fi

# --- unprivileged user namespaces (every boot) --------------------------------
# bubblewrap sandbox tests need them; Ubuntu 24.04+ AppArmor restricts them by default.
cat >/etc/sysctl.d/60-gha-userns.conf <<'EOF'
kernel.unprivileged_userns_clone = 1
EOF
if sysctl -n kernel.apparmor_restrict_unprivileged_userns >/dev/null 2>&1; then
  echo 'kernel.apparmor_restrict_unprivileged_userns = 0' >>/etc/sysctl.d/60-gha-userns.conf
fi
sysctl -p /etc/sysctl.d/60-gha-userns.conf || echo "gha-startup: userns sysctl failed"

# --- Tailscale: join as tag:ci-gcp (ACL: Redis 6380 + Loki 3101 out, Netdata in)
# The auth key is single-use and pre-authorised; once joined, node state on the
# persistent disk keeps the identity and the key in metadata is spent.
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled
if ! tailscale status >/dev/null 2>&1; then
  TS_KEY=$(md tailscale-auth-key)
  if [[ -n "$TS_KEY" ]]; then
    tailscale up --authkey="$TS_KEY" --hostname="$NAME" --advertise-tags=tag:ci-gcp --ssh=false --accept-dns=false \
      || echo "gha-startup: tailscale up failed"
  fi
  unset TS_KEY
fi
TS_UP=0; tailscale status >/dev/null 2>&1 && TS_UP=1

# --- Netdata: scraped by Node B Prometheus over the tailnet --------------------
# Bound to all interfaces: the VPC has no inbound firewall rules, so only
# tailnet peers allowed by the ACL (Node B -> :19999) can reach it.
if [[ ! -x /usr/sbin/netdata ]]; then
  curl -fsSL https://get.netdata.cloud/kickstart.sh -o /tmp/netdata-kickstart.sh \
    && sh /tmp/netdata-kickstart.sh --stable-channel --disable-telemetry --dont-wait --non-interactive \
    || echo "gha-startup: netdata install failed"
fi

# --- Grafana Alloy: ship the journal to Loki on Node B ------------------------
if [[ ! -x /usr/bin/alloy ]]; then
  curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/grafana.gpg
  echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" >/etc/apt/sources.list.d/grafana.list
  apt-get -o DPkg::Lock::Timeout=900 update && apt-get -o DPkg::Lock::Timeout=900 install -y alloy \
    || echo "gha-startup: alloy install failed"
fi
if [[ -x /usr/bin/alloy ]]; then
  cat >/etc/alloy/config.alloy <<'ALLOY'
loki.relabel "journal" {
  forward_to = []
  rule {
    source_labels = ["__journal__systemd_unit"]
    target_label  = "unit"
  }
}
loki.source.journal "journal" {
  forward_to    = [loki.write.nodeb.receiver]
  relabel_rules = loki.relabel.journal.rules
  labels        = { job = "gha-spot", host = "gha-spot-1" }
}
loki.write "nodeb" {
  endpoint {
    url = "http://100.92.54.45:3101/loki/api/v1/push"
  }
}
ALLOY
  usermod -aG systemd-journal alloy 2>/dev/null || true
  systemctl enable alloy >/dev/null 2>&1 || true
  systemctl restart alloy || echo "gha-startup: alloy restart failed"
fi

# --- job environment + per-job cleanup hook ------------------------------------
{
  echo "CARGO_HOME=$CACHE/cargo"
  echo "RUSTC_WRAPPER=/usr/local/bin/sccache"
  echo "SCCACHE_DIR=$CACHE/sccache"
  echo "SCCACHE_CACHE_SIZE=40G"
  echo "CARGO_INCREMENTAL=0"
  echo "ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/bin/gha-job-completed"
  if [[ -n "$SCCACHE_REDIS" && "$TS_UP" == 1 ]]; then echo "SCCACHE_REDIS_ENDPOINT=$SCCACHE_REDIS"; fi
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
