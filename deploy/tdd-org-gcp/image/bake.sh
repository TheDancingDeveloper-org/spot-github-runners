#!/usr/bin/env bash
# Provision a golden image for throwaway rust-heavy runner VMs.
# Runs once as root on a builder VM (startup-script), then powers off.
# tools/bake-image.sh turns the stopped builder's disk into an image.
set -Eeuo pipefail
trap 'echo "gha-bake: FAILED at line $LINENO (exit $?)"' ERR
exec 2>&1
export DEBIAN_FRONTEND=noninteractive
MD=http://metadata.google.internal/computeMetadata/v1/instance/attributes
md() { curl -fsS -H 'Metadata-Flavor: Google' "$MD/$1" 2>/dev/null || true; }

RUST_TOOLCHAIN=$(md rust-toolchain); RUST_TOOLCHAIN=${RUST_TOOLCHAIN:-1.95.0}
NEXTEST_VERSION=$(md nextest-version); NEXTEST_VERSION=${NEXTEST_VERSION:-0.9.103}
RUNNER_VERSION=$(md runner-version); RUNNER_VERSION=${RUNNER_VERSION:-2.337.0}
SCCACHE_VERSION=$(md sccache-version); SCCACHE_VERSION=${SCCACHE_VERSION:-0.10.0}
IMAGE_VERSION=$(md image-version); IMAGE_VERSION=${IMAGE_VERSION:-dev}
APT="apt-get -o DPkg::Lock::Timeout=900"

echo "gha-bake: start $IMAGE_VERSION (rust $RUST_TOOLCHAIN, nextest $NEXTEST_VERSION, runner $RUNNER_VERSION)"

# --- packages -------------------------------------------------------------------
rm -rf /var/lib/apt/lists/*
add-apt-repository -y -n universe
$APT update
$APT install -y --no-install-recommends ca-certificates curl git jq unzip zip xz-utils zstd gnupg \
  build-essential cmake pkg-config libssl-dev python3 clang protobuf-compiler python3-pip \
  libcap-dev libasound2-dev bubblewrap \
  musl-tools lld libc++-dev libc++abi-dev
# musl-tools .. libc++abi-dev: codex fork-release's static musl build
# (.github/scripts/install-musl-build-tools.sh), which jobs here cannot apt-install.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release; echo "$VERSION_CODENAME") stable" \
  >/etc/apt/sources.list.d/docker.list
curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/grafana.gpg
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" >/etc/apt/sources.list.d/grafana.list
$APT update
$APT install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin alloy
systemctl disable alloy >/dev/null 2>&1 || true   # configured and started per VM
curl -fsSL https://tailscale.com/install.sh | sh
systemctl disable tailscaled >/dev/null 2>&1 || true  # joined per VM, as an ephemeral node

# Unprivileged user namespaces for bubblewrap sandbox tests (AppArmor restricts them on 24.04).
cat >/etc/sysctl.d/60-gha-userns.conf <<'EOF'
kernel.unprivileged_userns_clone = 1
kernel.apparmor_restrict_unprivileged_userns = 0
EOF

# --- runner user, Rust, tools -----------------------------------------------------
id runner >/dev/null 2>&1 || useradd -m -s /bin/bash -u 1001 runner
usermod -aG docker runner
sudo -u runner -H bash -c "curl -fsS --proto =https --tlsv1.2 https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain $RUST_TOOLCHAIN -c clippy -c rustfmt -c rust-src"
sudo -u runner -H bash -c "curl -fsSL https://get.nexte.st/$NEXTEST_VERSION/linux | tar -xz -C ~/.cargo/bin"
curl -fsSL "https://github.com/mozilla/sccache/releases/download/v$SCCACHE_VERSION/sccache-v$SCCACHE_VERSION-x86_64-unknown-linux-musl.tar.gz" \
  | tar -xz -C /usr/local/bin --strip-components=1 --wildcards '*/sccache'
install -d -o runner -g runner /opt/actions-runner
curl -fsSL "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz" \
  | sudo -u runner tar -xz -C /opt/actions-runner
/opt/actions-runner/bin/installdependencies.sh

# --- verify -----------------------------------------------------------------------
sudo -u runner -H bash -lc 'rustc --version && cargo --version && cargo nextest --version | head -1'
/usr/local/bin/sccache --version
command -v bwrap && bwrap --version
docker --version
echo "gha-bake: tools ok"

# --- generalise: every VM from this image gets its own identity --------------------
cat >/etc/gha-image <<EOF
image_version=$IMAGE_VERSION
rust=$RUST_TOOLCHAIN
nextest=$NEXTEST_VERSION
runner=$RUNNER_VERSION
sccache=$SCCACHE_VERSION
baked=$(date -u +%FT%TZ)
EOF
$APT clean
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
rm -f /etc/ssh/ssh_host_*
cloud-init clean --logs --machine-id >/dev/null 2>&1 || truncate -s 0 /etc/machine-id
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
echo "gha-bake: done $IMAGE_VERSION"
systemctl poweroff
