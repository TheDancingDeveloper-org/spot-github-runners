#!/usr/bin/env bash
# Startup script for throwaway rust-heavy runner VMs (from the gha-rust-heavy
# image). Runs exactly one GitHub Actions job with a just-in-time registration,
# then powers off; the poller deletes the VM and its disk. Rendered by Terraform
# templatefile() into the instance template.
set -Eeuo pipefail
exec 2>&1
MD=http://metadata.google.internal/computeMetadata/v1/instance/attributes
md() { curl -fsS -H 'Metadata-Flavor: Google' "$MD/$1" 2>/dev/null || true; }
SCCACHE_BUCKET="${sccache_bucket}"
IDLE_LIMIT_MIN="${idle_limit_minutes}"
MAX_LIFETIME_MIN="${max_lifetime_minutes}"
NAME=$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/name)
echo "gha-vm: $NAME booting ($(tr '\n' ' ' </etc/gha-image 2>/dev/null))"

# --- watchdogs: never run (and bill) unattended --------------------------------
# Power off if no job has started within IDLE_LIMIT_MIN, and unconditionally
# after MAX_LIFETIME_MIN. Both run detached from this script.
systemd-run --unit=gha-idle-limit --on-active="$${IDLE_LIMIT_MIN}m" \
  /bin/sh -c 'pgrep -f Runner.Worker >/dev/null || { echo "gha-vm: no job within limit, powering off"; systemctl poweroff; }'
systemd-run --unit=gha-max-lifetime --on-active="$${MAX_LIFETIME_MIN}m" \
  /bin/sh -c 'echo "gha-vm: max lifetime reached, powering off"; systemctl poweroff'

# --- prerequisites the jobs rely on (baked; verified here) ----------------------
sysctl -q -p /etc/sysctl.d/60-gha-userns.conf || true
bwrap_path=$(command -v bwrap || true)
echo "gha-prereq: bwrap=$${bwrap_path:-MISSING} userns=$(sysctl -n kernel.unprivileged_userns_clone) apparmor_restrict=$(sysctl -n kernel.apparmor_restrict_unprivileged_userns)"
if [[ -z "$bwrap_path" ]]; then echo "gha-prereq: FAILED"; systemctl poweroff; exit 1; fi

# --- tailnet (ephemeral node via Tailscale federated identity) + Loki logs ------
# No stored key: the VM's service-account identity token (issuer accounts.google.com,
# subject = gha-runner-vm's unique ID) is exchanged for a one-time tag:ci-gcp login.
TS_CLIENT_ID="${ts_client_id}"
TS_AUDIENCE="${ts_audience}"
if [[ -n "$TS_CLIENT_ID" ]]; then
  systemctl enable --now tailscaled >/dev/null 2>&1 || true
  id_token=$(curl -fsS -H 'Metadata-Flavor: Google' \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=$TS_AUDIENCE&format=full" || true)
  if [[ -n "$id_token" ]] && tailscale up --client-id="$TS_CLIENT_ID?ephemeral=true&preauthorized=true" --id-token="$id_token" \
       --hostname="$NAME" --advertise-tags=tag:ci-gcp --ssh=false --accept-dns=false --timeout=60s; then
    echo "gha-vm: tailnet up as $(tailscale ip -4 2>/dev/null)"
  else
    echo "gha-vm: tailscale login failed; continuing without the tailnet"
  fi
  unset id_token
  cat >/etc/alloy/config.alloy <<ALLOY
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
  labels        = { job = "gha-spot", host = "$NAME" }
}
loki.write "nodeb" {
  endpoint {
    url = "http://100.92.54.45:3101/loki/api/v1/push"
  }
}
ALLOY
  usermod -aG systemd-journal alloy 2>/dev/null || true
  systemctl restart alloy || echo "gha-vm: alloy failed"
fi

# --- job environment ---------------------------------------------------------------
RUNNER_HOME=/opt/actions-runner
{
  echo "PATH=/home/runner/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  echo "RUSTC_WRAPPER=/usr/local/bin/sccache"
  echo "CARGO_INCREMENTAL=0"
  if [[ -n "$SCCACHE_BUCKET" ]]; then
    # Shared compile cache in GCS (same region: free reads). The VM's service
    # account has objectUser on this bucket only; sccache uses the metadata server.
    echo "SCCACHE_GCS_BUCKET=$SCCACHE_BUCKET"
    echo "SCCACHE_GCS_RW_MODE=READ_WRITE"
    echo "SCCACHE_GCS_KEY_PREFIX=rust-heavy"
  else
    echo "SCCACHE_DIR=/var/cache/sccache"
    echo "SCCACHE_CACHE_SIZE=20G"
  fi
} >"$RUNNER_HOME/.env"
install -d -o runner -g runner /var/cache/sccache
chown runner:runner "$RUNNER_HOME/.env"

# --- run exactly one job ------------------------------------------------------------
JIT=$(md gha-jitconfig)
if [[ -z "$JIT" ]]; then echo "gha-vm: no gha-jitconfig in metadata, powering off"; systemctl poweroff; exit 0; fi
echo "gha-vm: starting runner (single job)"
set +e
sudo -u runner -H "$RUNNER_HOME/run.sh" --jitconfig "$JIT"
rc=$?
set -e
unset JIT
echo "gha-vm: runner exited ($rc); powering off"
systemctl poweroff
