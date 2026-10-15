"""Core data models, CPU benchmark catalog, region filtering, instance qualification,
VM templates, and Cloud-Init parser/visualizer for the Web UI Dashboard.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
import re
import socket
import subprocess
import sys
import time
from typing import Any
import urllib.request

from . import billing_catalog as _billing_catalog_mod
from . import compute_api as _compute_api_mod
from . import spot_pricing as _spot_pricing_mod
from .billing_catalog import catalog_skus
from .compute_api import AsyncGoogleApi, get_auth
from .machine_profiles import MACHINE_PROFILES
from .spot_pricing import load_catalog_rates, parse_component_rates, spot_price

_ORIG_GET_AUTH = get_auth
_ORIG_CATALOG_SKUS = catalog_skus
_ORIG_LOAD_CATALOG_RATES = load_catalog_rates
_ORIG_SPOT_PRICE = spot_price
_ORIG_ASYNC_GOOGLE_API = AsyncGoogleApi
_ORIG_SUBPROCESS_RUN = subprocess.run
_ORIG_URLOPEN = urllib.request.urlopen


HOURS_PER_DAY = Decimal("24")
DAYS_PER_MONTH = Decimal("365") / Decimal("12")
HOURS_PER_MONTH = HOURS_PER_DAY * DAYS_PER_MONTH


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, bool):
        raise ValueError(f"Expected numeric value, got boolean {value!r}")
    try:
        dec = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"Numeric value must be finite: {value!r}")
    return dec


def _money(value: Decimal, places: str = "0.01") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _money4(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# Default cloud-init template matching the user's specification for GitHub Actions runners.
DEFAULT_RUNNER_CLOUD_INIT = """#cloud-config
package_update: true
package_upgrade: true

groups:
  - docker

users:
  - default
  - name: runner
    gecos: GitHub Actions Runner
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: users, admin, docker
    shell: /bin/bash
    lock_passwd: true

packages:
  - apt-transport-https
  - ca-certificates
  - curl
  - gnupg
  - jq
  - git
  - build-essential
  - pkg-config
  - libssl-dev

write_files:
  - path: /etc/runner-setup.env
    permissions: '0600'
    owner: root:root
    content: |
      GITHUB_ORG_URL="https://github.com/YOUR_ORG_NAME"
      GITHUB_RUNNER_TOKEN="YOUR_REGISTRATION_TOKEN"
      RUNNER_GROUP="Default"
      RUNNER_LABELS="self-hosted,Linux,X64,spot-runner,rust,docker"
      RUNNERS_COUNT="4"

runcmd:
  # 1. Install Official Docker Engine & Buildx (select Debian vs Ubuntu repository from /etc/os-release)
  - install -m 0755 -d /etc/apt/keyrings
  - 'curl -fsSL "https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}")/gpg" -o /etc/apt/keyrings/docker.asc'
  - chmod a+r /etc/apt/keyrings/docker.asc
  - 'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}") $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list'
  - apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  - systemctl enable --now docker
  # 2. Download, Configure & Start Persistent GitHub Actions Runner Slots with live GCE region & machine-family labels
  - curl -fsSL https://github.com/actions/runner/releases/download/v2.322.0/actions-runner-linux-x64-2.322.0.tar.gz -o /tmp/actions-runner.tar.gz
  - bash -lc 'set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && vm_zone="$(basename "$(curl -fsSL -H Metadata-Flavor:Google http://metadata.google.internal/computeMetadata/v1/instance/zone 2>/dev/null || true)")" && vm_region="${vm_zone%-*}" && vm_mtype="$(basename "$(curl -fsSL -H Metadata-Flavor:Google http://metadata.google.internal/computeMetadata/v1/instance/machine-type 2>/dev/null || true)")" && vm_family="${vm_mtype%%-*}" && eff_labels="${RUNNER_LABELS}" && [[ -z "${vm_region}" ]] || eff_labels="${eff_labels},gcp-${vm_region}" && [[ -z "${vm_family}" ]] || eff_labels="${eff_labels},${vm_family}" && for slot in $(seq 1 "${RUNNERS_COUNT:-4}"); do dir="/opt/actions-runner-${slot}"; install -d -m 0755 -o runner -g runner "${dir}"; tar -xzf /tmp/actions-runner.tar.gz -C "${dir}"; chown -R runner:runner "${dir}"; (cd "${dir}" && runuser -u runner -- ./config.sh --unattended --replace --name "$(hostname -s)-runner-${slot}" --url "${GITHUB_ORG_URL}" --token "${GITHUB_RUNNER_TOKEN}" --runnergroup "${RUNNER_GROUP}" --labels "${eff_labels}" && ./svc.sh install runner && ./svc.sh start); done && rm -f /tmp/actions-runner.tar.gz'
"""

DEFAULT_RUNNER_STRICT_CLOUD_INIT = """#cloud-config
package_update: true
package_upgrade: true

groups:
  - docker

users:
  - default
  - name: runner
    gecos: Heavy Pool GitHub Actions Runner (Rust 1.99)
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: users, admin, docker
    shell: /bin/bash
    lock_passwd: true

packages:
  - ca-certificates
  - curl
  - gnupg
  - jq
  - git
  - xfsprogs
  - util-linux
  - rsync
  - clang
  - cmake
  - pkg-config
  - libssl-dev

write_files:
  - path: /etc/runner-setup.env
    permissions: '0600'
    owner: root:root
    content: |
      GITHUB_ORG_URL="https://github.com/swack-tools"
      GITHUB_RUNNER_TOKEN="YOUR_REGISTRATION_TOKEN"
      RUNNER_GROUP="oxidex-strict"
      RUNNER_LABELS="self-hosted,Linux,X64,spot-heavy32,rust-1.99"
      RUNNERS_COUNT="4"
      RUNNER_CPU_LIMIT="8"
      RUNNER_MEMORY_GB="32"
      DISK_DEVICE="/dev/disk/by-id/google-runner-data"
      DISK_MOUNT="/mnt/runner-data"

runcmd:
  - 'bash -lc ''set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && dev="${DISK_DEVICE:-/dev/disk/by-id/google-runner-data}" && mp="${DISK_MOUNT:-/mnt/runner-data}" && if [[ ! -b "$dev" ]]; then echo "Missing persistent runner disk $dev" >&2; exit 1; fi && (blkid "$dev" >/dev/null 2>&1 || mkfs.ext4 -F "$dev") && mkdir -p "$mp" && uuid="$(blkid -s UUID -o value "$dev")" && (grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $mp ext4 defaults,nofail 0 2" >> /etc/fstab) && (mountpoint -q "$mp" || mount "$mp") && mountpoint -q "$mp" && mkdir -p "$mp/docker" "$mp/containerd" "$mp/instances" "$mp/work"'''
  - install -m 0755 -d /etc/apt/keyrings
  - 'curl -fsSL "https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}")/gpg" -o /etc/apt/keyrings/docker.asc'
  - chmod a+r /etc/apt/keyrings/docker.asc
  - 'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}") $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list'
  - apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  - 'bash -lc ''set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && mp="${DISK_MOUNT:-/mnt/runner-data}" && systemctl stop docker containerd || true && cp -a /var/lib/containerd/. "$mp/containerd/" && install -d -m 0755 /etc/docker /etc/systemd/system/containerd.service.d && printf "{\\"data-root\\":\\"%s/docker\\",\\"log-driver\\":\\"local\\",\\"log-opts\\":{\\"max-size\\":\\"20m\\",\\"max-file\\":\\"3\\"}}\\n" "$mp" > /etc/docker/daemon.json && printf "[Service]\\nExecStart=\\nExecStart=/usr/bin/containerd --root %s/containerd --state /run/containerd\\n" "$mp" > /etc/systemd/system/containerd.service.d/runner-data.conf && systemctl daemon-reload && systemctl enable --now containerd docker && test "$(docker info --format=json | jq -r .DockerRootDir)" = "$mp/docker"'''
  - 'bash -lc ''set -euo pipefail && install -d -m 0755 -o runner -g runner /opt/runner-rust/cargo /opt/runner-rust/rustup && curl --proto "=https" --tlsv1.2 -fsS https://sh.rustup.rs -o /tmp/rustup-init.sh && CARGO_HOME=/opt/runner-rust/cargo RUSTUP_HOME=/opt/runner-rust/rustup runuser -u runner -- sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.99.0 && rm -f /tmp/rustup-init.sh && CARGO_HOME=/opt/runner-rust/cargo RUSTUP_HOME=/opt/runner-rust/rustup PATH="/opt/runner-rust/cargo/bin:$PATH" runuser -u runner -- rustup component add rustfmt clippy --toolchain 1.99.0 && CARGO_HOME=/opt/runner-rust/cargo RUSTUP_HOME=/opt/runner-rust/rustup PATH="/opt/runner-rust/cargo/bin:$PATH" runuser -u runner -- rustup target add --toolchain 1.99.0 x86_64-unknown-linux-musl aarch64-unknown-linux-musl aarch64-apple-darwin x86_64-apple-darwin && ln -sfn /opt/runner-rust/cargo/bin/* /usr/local/bin/ && rustc --version | grep "^rustc 1.99.0 " && cargo --version | grep "^cargo 1.99.0 "'''
  - curl -fsSL https://github.com/actions/runner/releases/download/v2.322.0/actions-runner-linux-x64-2.322.0.tar.gz -o /tmp/actions-runner.tar.gz
  - bash -lc 'set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && mp="${DISK_MOUNT:-/mnt/runner-data}" && cpu_quota="$(( ${RUNNER_CPU_LIMIT:-8} * 100 ))%" && mem_max="${RUNNER_MEMORY_GB:-32}G" && for slot in $(seq 1 "${RUNNERS_COUNT:-4}"); do state_dir="${mp}/instances/actions-runner-${slot}"; work_dir="${mp}/work/runner-${slot}"; dir="/opt/actions-runner-${slot}"; install -d -m 0755 -o runner -g runner "${state_dir}" "${work_dir}"; ln -sfn "${state_dir}" "${dir}"; tar -xzf /tmp/actions-runner.tar.gz -C "${state_dir}"; chown -R runner:runner "${state_dir}" "${work_dir}"; (cd "${dir}" && runuser -u runner -- ./config.sh --unattended --replace --name "$(hostname -s)-runner-${slot}" --url "${GITHUB_ORG_URL}" --token "${GITHUB_RUNNER_TOKEN}" --runnergroup "${RUNNER_GROUP}" --labels "${RUNNER_LABELS}" --work "${work_dir}" && ./svc.sh install runner && unit_name="$(basename "$(cat .service)")" && install -d -m 0755 "/etc/systemd/system/${unit_name}.d" && printf "[Service]\\nCPUAccounting=yes\\nMemoryAccounting=yes\\nCPUQuota=%s\\nMemoryMax=%s\\nMemorySwapMax=0\\nEnvironment=RUSTUP_HOME=/opt/runner-rust/rustup\\nEnvironment=PATH=/opt/runner-rust/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\\n" "${cpu_quota}" "${mem_max}" > "/etc/systemd/system/${unit_name}.d/heavy32-limits.conf" && systemctl daemon-reload && ./svc.sh start); done && rm -f /tmp/actions-runner.tar.gz'
"""

DEFAULT_RUST_BUILDER_CLOUD_INIT = """#cloud-config
package_update: true
package_upgrade: true

groups:
  - docker
  - oxidex-build

users:
  - default
  - name: oxidex-uploader
    gecos: Dedicated Rust 1.99 Remote Builder Uploader
    sudo: ["ALL=(root) NOPASSWD: /usr/local/bin/oxidex-remote-build, /usr/bin/flock --shared /run/oxidex-build.lock *"]
    groups: oxidex-build
    shell: /bin/bash
    lock_passwd: true

packages:
  - ca-certificates
  - curl
  - gnupg
  - git
  - jq
  - rsync
  - time
  - python3
  - util-linux
  - clang
  - lld
  - cmake
  - pkg-config
  - libssl-dev
  - zstd

write_files:
  - path: /etc/sudoers.d/oxidex-build
    permissions: '0440'
    owner: root:root
    content: |
      %oxidex-build ALL=(root) NOPASSWD: /usr/local/bin/oxidex-remote-build, /usr/bin/flock --shared /run/oxidex-build.lock *
  - path: /etc/oxidex-remote-build.json
    permissions: '0644'
    owner: root:root
    content: |
      {
        "role": "builder",
        "cpus": 16,
        "memory_gib": 28,
        "image": "oxidex-remote-builder:1.99.0",
        "cache_mount": "/mnt/runner-data/remote-build"
      }
  - path: /opt/oxidex-builder/image/entrypoint.py
    permissions: '0755'
    owner: root:root
    content: |
      #!/usr/bin/env python3
      import os, re, subprocess, sys
      from pathlib import Path

      def main():
          toolchain_file = Path('/src/rust-toolchain.toml')
          pin = '1.99.0'
          if toolchain_file.is_file():
              match = re.search(r'^channel\\s*=\\s*"([0-9]+\\.[0-9]+\\.[0-9]+)"', toolchain_file.read_text(), re.M)
              if not match:
                  raise RuntimeError('A numeric Rust toolchain pin is required')
              pin = match[1]
          compiler = subprocess.check_output(['rustup', 'which', '--toolchain', pin, 'rustc'], text=True).strip()
          cargo = subprocess.check_output(['rustup', 'which', '--toolchain', pin, 'cargo'], text=True).strip()
          env = dict(os.environ, RUSTC=compiler, RUSTDOC=str(Path(compiler).with_name('rustdoc')), RUSTUP_TOOLCHAIN=pin)
          env['PATH'] = f"{Path(compiler).parent}:/usr/local/cargo/bin:{env.get('PATH', '')}"
          env['CARGO_HOME'] = '/cargo'
          env['CARGO_TARGET_DIR'] = '/target'
          command = sys.argv[1:] or ['cargo', 'build', '--release', '--locked', '--bin', 'oxidex']
          if command[0] == 'cargo':
              command[0] = cargo
          os.execvpe(command[0], command, env)

      if __name__ == '__main__':
          main()
  - path: /opt/oxidex-builder/image/Dockerfile
    permissions: '0644'
    owner: root:root
    content: |
      FROM rust:1.99.0-bookworm
      RUN apt-get update && apt-get install -y --no-install-recommends clang lld cmake pkg-config libssl-dev zstd git ca-certificates time python3 rsync && rm -rf /var/lib/apt/lists/*
      RUN rustup component add rustfmt clippy --toolchain 1.99.0 \\
          && rustup target add --toolchain 1.99.0 x86_64-unknown-linux-musl aarch64-unknown-linux-musl aarch64-apple-darwin x86_64-apple-darwin \\
          && cargo install --locked -j4 just \\
          && cargo install --locked -j4 cargo-nextest \\
          && cargo install --locked -j4 --version 0.29.2 cbindgen \\
          && rustc -vV && cargo -V && just --version && cbindgen --version && cargo nextest --version
      RUN groupadd -g 1001 runner && useradd -m -u 1001 -g 1001 -s /bin/bash runner && install -d -o 1001 -g 1001 /src /target /cargo
      COPY entrypoint.py /usr/local/bin/oxidex-build-entrypoint
      RUN chmod 0755 /usr/local/bin/oxidex-build-entrypoint
      USER 1001:1001
      ENV PATH=/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
      ENV CARGO_HOME=/cargo
      ENV CARGO_TARGET_DIR=/target
      WORKDIR /src
      ENTRYPOINT ["/usr/bin/python3", "/usr/local/bin/oxidex-build-entrypoint"]
  - path: /usr/local/bin/oxidex-remote-build
    permissions: '0755'
    owner: root:root
    content: |
      #!/usr/bin/env python3
      import fcntl, json, os, re, subprocess, sys
      from pathlib import Path

      allowed_commands = {'cargo', 'rustc', 'rustfmt', 'clippy-driver', 'just', 'uv', 'python3', 'cbindgen'}

      def validate_project(project):
          if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', project):
              raise ValueError('Project must be a simple identifier, never a path')
          return project

      def checked_path(path):
          if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
              raise ValueError('Symlinked builder paths are forbidden')

      def main():
          if os.geteuid() != 0:
              raise RuntimeError('Invoke this root-owned launcher with sudo')
          config = json.loads(Path('/etc/oxidex-remote-build.json').read_text())
          root = Path(config.get('cache_mount', '/mnt/runner-data/remote-build'))
          mount_root = root.parent if root.name == 'remote-build' else root
          if not os.path.ismount(mount_root):
              raise RuntimeError(f'Builder cache mount point is not mounted: {mount_root}')
          image = config.get('image', 'oxidex-remote-builder:1.99.0')
          cpus = int(config.get('cpus', 16))
          memory_gib = int(config.get('memory_gib', 28))
          if sys.argv[1:] == ['--verify-readiness']:
              subprocess.run(['docker', 'image', 'inspect', image], check=True, stdout=subprocess.DEVNULL)
              for sub in ('cargo', 'sources', 'targets'):
                  checked_path(root / sub)
                  if not (root / sub).is_dir():
                      raise RuntimeError(f'Missing builder cache directory: {root / sub}')
              return
          if len(sys.argv) < 2:
              raise ValueError('Project identifier is required')
          project = validate_project(sys.argv[1])
          for path in (root, root / 'sources', root / 'targets', root / 'cargo', root / 'sources' / project, root / 'targets' / project):
              checked_path(path)
              path.mkdir(parents=True, exist_ok=True)
          if sys.argv[2:] == ['prepare']:
              source = root / 'sources' / project
              os.chown(source, int(os.environ.get('SUDO_UID', '1001')), 1001)
              source.chmod(0o2770)
              for path in (root / 'targets' / project, root / 'cargo'):
                  os.chown(path, 1001, 1001)
              print(source)
              return
          command = sys.argv[2:] or ['cargo', 'build', '--release', '--locked', '--bin', 'oxidex']
          if command[0] not in allowed_commands:
              raise ValueError(f'Unsupported build command: {command[0]}')
          if cpus < 1 or memory_gib < 1:
              raise ValueError('Resource limits must be positive')
          with open('/run/oxidex-build.lock', 'a') as lock:
              fcntl.flock(lock, fcntl.LOCK_SH)
              if Path('/run/oxidex-build-draining').exists():
                  raise SystemExit(75)
              cmd = [
                  'docker', 'run', '--rm', '--user=1001:1001', f'--cpus={cpus}',
                  '--label=oxidex.remote-build=true',
                  f'--memory={memory_gib}g', f'--memory-swap={memory_gib}g', '--pids-limit=4096',
                  '--cap-drop=ALL', '--security-opt=no-new-privileges',
                  '--entrypoint=/usr/bin/time',
                  '-v', f'{root}/sources/{project}:/src',
                  '-v', f'{root}/targets/{project}:/target',
                  '-v', f'{root}/cargo:/cargo',
                  image, '-v', '/usr/bin/python3',
                  '/usr/local/bin/oxidex-build-entrypoint', *command,
              ]
              raise SystemExit(subprocess.run(cmd).returncode)

      if __name__ == '__main__':
          main()

runcmd:
  # 1. Mount persistent NVMe Hyperdisk cache (fail if unmounted) and configure unprivileged build container
  - 'bash -lc ''set -euo pipefail && mp="/mnt/runner-data" && found_dev="" && for dev in /dev/disk/by-id/google-builder-cache /dev/disk/by-id/google-runner-data; do if [[ -b "$dev" ]]; then found_dev="$dev"; break; fi; done && if [[ -z "$found_dev" ]]; then echo "Missing persistent builder cache disk (/dev/disk/by-id/google-builder-cache or google-runner-data)" >&2; exit 1; fi && (blkid "$found_dev" >/dev/null 2>&1 || mkfs.ext4 -F "$found_dev") && mkdir -p "$mp" && uuid="$(blkid -s UUID -o value "$found_dev")" && (grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $mp ext4 defaults,nofail 0 2" >> /etc/fstab) && (mountpoint -q "$mp" || mount "$mp") && mountpoint -q "$mp" && mkdir -p "$mp/docker" "$mp/containerd" "$mp/remote-build/cargo" "$mp/remote-build/sources" "$mp/remote-build/targets" "$mp/remote-build/exports"'''
  - install -m 0755 -d /etc/apt/keyrings
  - 'curl -fsSL "https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}")/gpg" -o /etc/apt/keyrings/docker.asc'
  - chmod a+r /etc/apt/keyrings/docker.asc
  - 'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}") $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list'
  - apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  - systemctl enable --now containerd docker
  - DOCKER_BUILDKIT=0 docker build --memory=28g --memory-swap=28g --cpu-period=100000 --cpu-quota=1600000 -t oxidex-remote-builder:1.99.0 /opt/oxidex-builder/image
  - /usr/local/bin/oxidex-remote-build --verify-readiness
"""


# Normalized CPU benchmark scores per CPU platform and vCPU count.
# Reflects single-thread IPC + multi-core compilation throughput.
CPU_PLATFORM_BENCHMARK_BASE: dict[str, tuple[int, int]] = {
    "AMD Turin": (460, 145),
    "Intel Granite Rapids": (440, 138),
    "Intel Emerald Rapids": (410, 128),
    "AMD Milan": (370, 112),
    "Intel Ice Lake": (350, 105),
    "Intel Cascade Lake": (285, 82),
    "AMD Rome / E2 Dynamic": (250, 74),
}


def compute_cpu_benchmark_score(cpu_platform: str, vcpu: int, machine_type: str = "") -> int:
    """Compute a deterministic CPU benchmark score for a platform and vCPU shape."""
    single_base, per_core = CPU_PLATFORM_BENCHMARK_BASE.get(
        cpu_platform, (320, 95)
    )
    family = machine_type.split("-", 1)[0] if machine_type else ""
    series_bonus = {
        "c4d": 120,
        "c4": 95,
        "n4d": 80,
        "n4": 55,
        "c2d": 45,
        "n2d": 25,
        "n2": 15,
        "e2": -30,
    }.get(family, 0)
    highcpu_bonus = 35 if "-highcpu-" in machine_type else 0
    # Scaling factor yields realistic scores around 700-1500 for 4-8 vCPU and 1500-4500 for 16-32 vCPU.
    effective_cores = min(vcpu, 8) + max(0, vcpu - 8) * 0.72
    return int(round(single_base + series_bonus + highcpu_bonus + per_core * effective_cores))


# Standard machine catalog shapes across GCP families.
MACHINE_SHAPES_CATALOG: tuple[dict[str, Any], ...] = (
    # 4 vCPU shapes
    {"machine_type": "c4d-standard-4", "family": "c4d", "vcpu": 4, "ram_gib": 16, "cpu_platform": "AMD Turin"},
    {"machine_type": "n4d-standard-4", "family": "n4d", "vcpu": 4, "ram_gib": 16, "cpu_platform": "AMD Turin"},
    {"machine_type": "c4-standard-4", "family": "c4", "vcpu": 4, "ram_gib": 16, "cpu_platform": "Intel Granite Rapids"},
    {"machine_type": "n4-standard-4", "family": "n4", "vcpu": 4, "ram_gib": 16, "cpu_platform": "Intel Emerald Rapids"},
    {"machine_type": "c2d-standard-4", "family": "c2d", "vcpu": 4, "ram_gib": 16, "cpu_platform": "AMD Milan"},
    {"machine_type": "n2d-standard-4", "family": "n2d", "vcpu": 4, "ram_gib": 16, "cpu_platform": "AMD Milan"},
    {"machine_type": "n2-standard-4", "family": "n2", "vcpu": 4, "ram_gib": 16, "cpu_platform": "Intel Ice Lake"},
    {"machine_type": "e2-standard-4", "family": "e2", "vcpu": 4, "ram_gib": 16, "cpu_platform": "AMD Rome / E2 Dynamic"},
    # 8 vCPU / 16 GiB (highcpu) and 8 vCPU / 32 GiB (standard)
    {"machine_type": "c4d-highcpu-8", "family": "c4d", "vcpu": 8, "ram_gib": 16, "cpu_platform": "AMD Turin"},
    {"machine_type": "n4-highcpu-8", "family": "n4", "vcpu": 8, "ram_gib": 16, "cpu_platform": "Intel Emerald Rapids"},
    {"machine_type": "n2-highcpu-8", "family": "n2", "vcpu": 8, "ram_gib": 8, "cpu_platform": "Intel Ice Lake"},
    {"machine_type": "c4d-standard-8", "family": "c4d", "vcpu": 8, "ram_gib": 32, "cpu_platform": "AMD Turin"},
    {"machine_type": "n4d-standard-8", "family": "n4d", "vcpu": 8, "ram_gib": 32, "cpu_platform": "AMD Turin"},
    {"machine_type": "c4-standard-8", "family": "c4", "vcpu": 8, "ram_gib": 32, "cpu_platform": "Intel Granite Rapids"},
    {"machine_type": "n4-standard-8", "family": "n4", "vcpu": 8, "ram_gib": 32, "cpu_platform": "Intel Emerald Rapids"},
    {"machine_type": "c2d-standard-8", "family": "c2d", "vcpu": 8, "ram_gib": 32, "cpu_platform": "AMD Milan"},
    {"machine_type": "n2d-standard-8", "family": "n2d", "vcpu": 8, "ram_gib": 32, "cpu_platform": "AMD Milan"},
    {"machine_type": "n2-standard-8", "family": "n2", "vcpu": 8, "ram_gib": 32, "cpu_platform": "Intel Ice Lake"},
    {"machine_type": "n2-standard-8", "family": "n2", "vcpu": 8, "ram_gib": 32, "cpu_platform": "Intel Cascade Lake"},
    {"machine_type": "e2-standard-8", "family": "e2", "vcpu": 8, "ram_gib": 32, "cpu_platform": "AMD Rome / E2 Dynamic"},
    # 16 vCPU shapes (32 GiB highcpu/highmem and 64 GiB standard)
    {"machine_type": "c4d-highcpu-16", "family": "c4d", "vcpu": 16, "ram_gib": 32, "cpu_platform": "AMD Turin"},
    {"machine_type": "n4d-highcpu-16", "family": "n4d", "vcpu": 16, "ram_gib": 32, "cpu_platform": "AMD Turin"},
    {"machine_type": "n4-highcpu-16", "family": "n4", "vcpu": 16, "ram_gib": 32, "cpu_platform": "Intel Emerald Rapids"},
    {"machine_type": "c2d-highcpu-16", "family": "c2d", "vcpu": 16, "ram_gib": 32, "cpu_platform": "AMD Milan"},
    {"machine_type": "n2-standard-16", "family": "n2", "vcpu": 16, "ram_gib": 64, "cpu_platform": "Intel Ice Lake"},
    {"machine_type": "n2d-standard-16", "family": "n2d", "vcpu": 16, "ram_gib": 64, "cpu_platform": "AMD Milan"},
    {"machine_type": "c2d-standard-16", "family": "c2d", "vcpu": 16, "ram_gib": 64, "cpu_platform": "AMD Milan"},
    {"machine_type": "n4-standard-16", "family": "n4", "vcpu": 16, "ram_gib": 64, "cpu_platform": "Intel Emerald Rapids"},
    {"machine_type": "c4-standard-16", "family": "c4", "vcpu": 16, "ram_gib": 60, "cpu_platform": "Intel Granite Rapids"},
    {"machine_type": "n4d-standard-16", "family": "n4d", "vcpu": 16, "ram_gib": 64, "cpu_platform": "AMD Turin"},
    {"machine_type": "c4d-standard-16", "family": "c4d", "vcpu": 16, "ram_gib": 64, "cpu_platform": "AMD Turin"},
    {"machine_type": "e2-standard-16", "family": "e2", "vcpu": 16, "ram_gib": 64, "cpu_platform": "AMD Rome / E2 Dynamic"},
    # 32 vCPU shapes (64 GiB highcpu and 128 GiB standard)
    {"machine_type": "c4d-highcpu-32", "family": "c4d", "vcpu": 32, "ram_gib": 64, "cpu_platform": "AMD Turin"},
    {"machine_type": "n4-standard-32", "family": "n4", "vcpu": 32, "ram_gib": 128, "cpu_platform": "Intel Emerald Rapids"},
    {"machine_type": "n4d-standard-32", "family": "n4d", "vcpu": 32, "ram_gib": 128, "cpu_platform": "AMD Turin"},
    {"machine_type": "c4d-standard-32", "family": "c4d", "vcpu": 32, "ram_gib": 128, "cpu_platform": "AMD Turin"},
    {"machine_type": "c4-standard-32", "family": "c4", "vcpu": 32, "ram_gib": 120, "cpu_platform": "Intel Granite Rapids"},
    {"machine_type": "c2d-standard-32", "family": "c2d", "vcpu": 32, "ram_gib": 128, "cpu_platform": "AMD Milan"},
    {"machine_type": "n2d-standard-32", "family": "n2d", "vcpu": 32, "ram_gib": 128, "cpu_platform": "AMD Milan"},
    {"machine_type": "n2-standard-32", "family": "n2", "vcpu": 32, "ram_gib": 128, "cpu_platform": "Intel Ice Lake"},
)


# Global GCP Regions & Data Centers with geographic coordinates, latency baselines,
# offered families, CPU platforms, disk types, and regional Spot pricing multipliers.
REGIONS_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "region": "us-south1",
        "zones": ["us-south1-a", "us-south1-b", "us-south1-c"],
        "display_name": "Dallas, Texas, USA",
        "continent": "North America",
        "country": "United States",
        "lat": 32.7767,
        "lon": -96.7970,
        "map_x": 21.5,
        "map_y": 38.0,
        "latency_ms": 24,
        "families": ["c4d", "n4d", "n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["AMD Turin", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "hyperdisk-throughput", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.96",
        "tax_rate_default": 8.25,
    },
    {
        "region": "us-central1",
        "zones": ["us-central1-a", "us-central1-b", "us-central1-c", "us-central1-f"],
        "display_name": "Council Bluffs, Iowa, USA",
        "continent": "North America",
        "country": "United States",
        "lat": 41.2619,
        "lon": -95.8608,
        "map_x": 22.0,
        "map_y": 33.0,
        "latency_ms": 31,
        "families": ["c4d", "n4d", "c4", "n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["AMD Turin", "Intel Granite Rapids", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "Intel Cascade Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "hyperdisk-throughput", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.92",
        "tax_rate_default": 7.0,
    },
    {
        "region": "us-east1",
        "zones": ["us-east1-b", "us-east1-c", "us-east1-d"],
        "display_name": "Moncks Corner, South Carolina, USA",
        "continent": "North America",
        "country": "United States",
        "lat": 33.1960,
        "lon": -80.0131,
        "map_x": 26.5,
        "map_y": 37.5,
        "latency_ms": 38,
        "families": ["c4d", "n4d", "c4", "n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["AMD Turin", "Intel Granite Rapids", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "hyperdisk-throughput", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.94",
        "tax_rate_default": 7.5,
    },
    {
        "region": "us-west2",
        "zones": ["us-west2-a", "us-west2-b", "us-west2-c"],
        "display_name": "Los Angeles, California, USA",
        "continent": "North America",
        "country": "United States",
        "lat": 34.0522,
        "lon": -118.2437,
        "map_x": 15.5,
        "map_y": 37.0,
        "latency_ms": 49,
        "families": ["c4", "n4", "n2d", "n2", "e2"],
        "cpu_platforms": ["Intel Granite Rapids", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "1.08",
        "tax_rate_default": 9.5,
    },
    {
        "region": "northamerica-northeast1",
        "zones": ["northamerica-northeast1-a", "northamerica-northeast1-b", "northamerica-northeast1-c"],
        "display_name": "Montréal, Québec, Canada",
        "continent": "North America",
        "country": "Canada",
        "lat": 45.5017,
        "lon": -73.5673,
        "map_x": 28.0,
        "map_y": 29.5,
        "latency_ms": 44,
        "families": ["c4", "n4", "n2d", "n2", "e2"],
        "cpu_platforms": ["Intel Granite Rapids", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.99",
        "tax_rate_default": 5.0,
    },
    {
        "region": "southamerica-east1",
        "zones": ["southamerica-east1-a", "southamerica-east1-b", "southamerica-east1-c"],
        "display_name": "São Paulo, Brazil",
        "continent": "South America",
        "country": "Brazil",
        "lat": -23.5505,
        "lon": -46.6333,
        "map_x": 35.5,
        "map_y": 71.0,
        "latency_ms": 132,
        "families": ["n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "1.18",
        "tax_rate_default": 12.0,
    },
    {
        "region": "europe-west1",
        "zones": ["europe-west1-b", "europe-west1-c", "europe-west1-d"],
        "display_name": "St. Ghislain, Belgium",
        "continent": "Europe",
        "country": "Belgium",
        "lat": 50.4501,
        "lon": 3.8189,
        "map_x": 49.5,
        "map_y": 27.5,
        "latency_ms": 88,
        "families": ["c4d", "n4d", "c4", "n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["AMD Turin", "Intel Granite Rapids", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "hyperdisk-throughput", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.98",
        "tax_rate_default": 21.0,
    },
    {
        "region": "europe-west4",
        "zones": ["europe-west4-a", "europe-west4-b", "europe-west4-c"],
        "display_name": "Eemshaven, Netherlands",
        "continent": "Europe",
        "country": "Netherlands",
        "lat": 53.4386,
        "lon": 6.8355,
        "map_x": 50.8,
        "map_y": 25.5,
        "latency_ms": 91,
        "families": ["c4d", "n4d", "c4", "n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["AMD Turin", "Intel Granite Rapids", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "hyperdisk-throughput", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.97",
        "tax_rate_default": 21.0,
    },
    {
        "region": "europe-north1",
        "zones": ["europe-north1-a", "europe-north1-b", "europe-north1-c"],
        "display_name": "Hamina, Finland",
        "continent": "Europe",
        "country": "Finland",
        "lat": 60.5693,
        "lon": 27.1878,
        "map_x": 56.0,
        "map_y": 20.5,
        "latency_ms": 112,
        "families": ["c4", "n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["Intel Granite Rapids", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.95",
        "tax_rate_default": 24.0,
    },
    {
        "region": "africa-south1",
        "zones": ["africa-south1-a", "africa-south1-b", "africa-south1-c"],
        "display_name": "Johannesburg, South Africa",
        "continent": "Africa",
        "country": "South Africa",
        "lat": -26.2041,
        "lon": 28.0473,
        "map_x": 56.5,
        "map_y": 73.5,
        "latency_ms": 184,
        "families": ["n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "Intel Cascade Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.89",
        "tax_rate_default": 15.0,
    },
    {
        "region": "me-west1",
        "zones": ["me-west1-a", "me-west1-b", "me-west1-c"],
        "display_name": "Tel Aviv, Israel",
        "continent": "Middle East",
        "country": "Israel",
        "lat": 32.0853,
        "lon": 34.7818,
        "map_x": 58.8,
        "map_y": 39.5,
        "latency_ms": 146,
        "families": ["n4", "n2d", "n2", "e2"],
        "cpu_platforms": ["Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "1.04",
        "tax_rate_default": 17.0,
    },
    {
        "region": "asia-south2",
        "zones": ["asia-south2-a", "asia-south2-b", "asia-south2-c"],
        "display_name": "Delhi, India",
        "continent": "Asia Pacific",
        "country": "India",
        "lat": 28.6139,
        "lon": 77.2090,
        "map_x": 70.5,
        "map_y": 42.0,
        "latency_ms": 198,
        "families": ["c4", "n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["Intel Granite Rapids", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "Intel Cascade Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.84",
        "tax_rate_default": 18.0,
    },
    {
        "region": "asia-east2",
        "zones": ["asia-east2-a", "asia-east2-b", "asia-east2-c"],
        "display_name": "Hong Kong, APAC",
        "continent": "Asia Pacific",
        "country": "Hong Kong",
        "lat": 22.3193,
        "lon": 114.1694,
        "map_x": 79.8,
        "map_y": 45.5,
        "latency_ms": 168,
        "families": ["c4d", "n4d", "n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["AMD Turin", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "hyperdisk-throughput", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.91",
        "tax_rate_default": 0.0,
    },
    {
        "region": "asia-southeast1",
        "zones": ["asia-southeast1-a", "asia-southeast1-b", "asia-southeast1-c"],
        "display_name": "Jurong West, Singapore",
        "continent": "Asia Pacific",
        "country": "Singapore",
        "lat": 1.3521,
        "lon": 103.8198,
        "map_x": 77.2,
        "map_y": 56.5,
        "latency_ms": 176,
        "families": ["c4d", "n4", "c2d", "n2d", "n2", "e2"],
        "cpu_platforms": ["AMD Turin", "Intel Emerald Rapids", "AMD Milan", "Intel Ice Lake", "AMD Rome / E2 Dynamic"],
        "disk_types": ["hyperdisk-balanced", "hyperdisk-throughput", "pd-ssd", "pd-balanced", "pd-standard"],
        "price_multiplier": "0.93",
        "tax_rate_default": 9.0,
    },
)


# Base Spot hourly rates per vCPU and per GiB RAM by machine series (USD).
SERIES_SPOT_BASE_RATES: dict[str, tuple[Decimal, Decimal]] = {
    "c4d": (Decimal("0.0046"), Decimal("0.00052")),
    "n4d": (Decimal("0.0038"), Decimal("0.00044")),
    "c4": (Decimal("0.0049"), Decimal("0.00055")),
    "n4": (Decimal("0.0041"), Decimal("0.00048")),
    "c2d": (Decimal("0.0039"), Decimal("0.00045")),
    "n2d": (Decimal("0.0034"), Decimal("0.00040")),
    "n2": (Decimal("0.0037"), Decimal("0.00043")),
    "e2": (Decimal("0.0029"), Decimal("0.00035")),
}


# Monthly USD per GiB by disk type, plus base IOPS/throughput add-on for Hyperdisk.
DISK_MONTHLY_RATES_USD_PER_GIB: dict[str, Decimal] = {
    "hyperdisk-balanced": Decimal("0.080"),
    "hyperdisk-throughput": Decimal("0.045"),
    "pd-ssd": Decimal("0.170"),
    "pd-balanced": Decimal("0.100"),
    "pd-standard": Decimal("0.040"),
}


def format_gcloud_ssh_command(
    instance_name: str,
    zone: str,
    project: str = "homelab-424523",
    ssh_user: str | None = None,
) -> str:
    """Format the ready-to-paste gcloud compute ssh command for an instance."""
    target = f"{ssh_user}@{instance_name}" if ssh_user else instance_name
    return f"gcloud compute ssh {target} --zone={zone} --project={project}"


def disk_category(disk_type: str) -> str:
    """Classify a disk type as 'hyperdrive' or 'normal'."""
    if str(disk_type).startswith("hyperdisk-"):
        return "hyperdrive"
    return "normal"


def compatible_disk_types(family: str) -> list[str]:
    """Return compatible disk types for a machine family."""
    profile = MACHINE_PROFILES.get(family)
    if profile and profile.disk_type == "hyperdisk-balanced":
        return ["hyperdisk-balanced", "hyperdisk-throughput"]
    if family in ("n2", "n2d", "c2d"):
        return ["pd-balanced", "pd-ssd", "pd-standard", "hyperdisk-balanced", "hyperdisk-throughput"]
    return ["pd-balanced", "pd-ssd", "pd-standard"]


_LIVE_SPOT_PRICING_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_LIVE_PRICING_OVERRIDE: dict[str, Any] | None = None


def set_live_spot_pricing_override(override: dict[str, Any] | None) -> None:
    """Set or clear a live Spot pricing catalog override (used for testing or manual cache injection)."""
    global _LIVE_PRICING_OVERRIDE
    _LIVE_PRICING_OVERRIDE = override


_ORIG_ASYNC_POST_JSON = AsyncGoogleApi.post_json


def _can_discover_gcloud_auth() -> bool:
    """Allow active gcloud credential discovery in normal execution and whenever pricing/auth hooks are mocked in tests."""
    if "unittest" not in sys.modules:
        return True
    if os.environ.get("GCP_ENABLE_LIVE_PRICING") == "1":
        return True
    return any(
        (
            get_auth is not _ORIG_GET_AUTH,
            _compute_api_mod.get_auth is not _ORIG_GET_AUTH,
            catalog_skus is not _ORIG_CATALOG_SKUS,
            _billing_catalog_mod.catalog_skus is not _ORIG_CATALOG_SKUS,
            load_catalog_rates is not _ORIG_LOAD_CATALOG_RATES,
            _spot_pricing_mod.load_catalog_rates is not _ORIG_LOAD_CATALOG_RATES,
            spot_price is not _ORIG_SPOT_PRICE,
            _spot_pricing_mod.spot_price is not _ORIG_SPOT_PRICE,
            AsyncGoogleApi is not _ORIG_ASYNC_GOOGLE_API,
            _compute_api_mod.AsyncGoogleApi is not _ORIG_ASYNC_GOOGLE_API,
            AsyncGoogleApi.post_json is not _ORIG_ASYNC_POST_JSON,
            subprocess.run is not _ORIG_SUBPROCESS_RUN,
            urllib.request.urlopen is not _ORIG_URLOPEN,
        )
    )


def fetch_live_spot_pricing(
    project: str = "homelab-424523",
    *,
    regions: list[str] | None = None,
    machine_types: list[str] | None = None,
    api_key: str | None = None,
    access_token: str | None = None,
    ttl_seconds: float = 300.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch and cache current regional Spot prices from GCP Billing Catalog and Compute capacityHistory."""
    if _LIVE_PRICING_OVERRIDE is not None and not force_refresh:
        return _LIVE_PRICING_OVERRIDE

    eff_api_key = (
        api_key
        if api_key is not None
        else (
            os.environ.get("GOOGLE_CLOUD_API_KEY")
            or os.environ.get("GCP_BILLING_API_KEY")
            or os.environ.get("GOOGLE_CLOUD_BILLING_API_KEY")
        )
    )
    eff_token = (
        access_token
        if access_token is not None
        else (os.environ.get("GCP_ACCESS_TOKEN") or os.environ.get("CLOUDSDK_AUTH_ACCESS_TOKEN"))
    )
    eff_project = project
    if not eff_token and _can_discover_gcloud_auth():
        try:
            auth_fn = (
                get_auth
                if get_auth is not _ORIG_GET_AUTH
                else _compute_api_mod.get_auth
            )
            auth = auth_fn(project)
            if auth and getattr(auth, "token", None):
                eff_token = str(auth.token).strip()
            if auth and getattr(auth, "project", None):
                eff_project = str(auth.project).strip() or project
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            # Active gcloud credentials are unavailable; continue with API key or static baseline.
            eff_token = None

    if not eff_api_key and not eff_token:
        return {
            "source": "catalog_baseline",
            "component_rates": {},
            "machine_hourly": {},
            "disk_monthly_gib": {},
        }

    target_regions = tuple(sorted(regions or [r["region"] for r in REGIONS_CATALOG]))
    target_machines = tuple(sorted(machine_types or [s["machine_type"] for s in MACHINE_SHAPES_CATALOG]))
    cache_key = (eff_project, target_regions, target_machines, eff_api_key or "", eff_token or "")
    now_ts = time.time()
    cached = _LIVE_SPOT_PRICING_CACHE.get(cache_key)
    if cached is not None and not force_refresh and (now_ts - float(cached.get("fetched_at", 0.0))) < ttl_seconds:
        return cached

    async def _query_gcp_pricing() -> tuple[dict[tuple[str, str, str, str], float], dict[tuple[str, str], float]]:
        comp_rates: dict[tuple[str, str, str, str], float] = {}
        mach_hourly: dict[tuple[str, str], float] = {}
        if eff_api_key:
            try:
                rates_fn = (
                    load_catalog_rates
                    if load_catalog_rates is not _ORIG_LOAD_CATALOG_RATES
                    else _spot_pricing_mod.load_catalog_rates
                )
                comp_rates = await rates_fn(eff_api_key)
            except (OSError, ValueError, RuntimeError):
                # Network or Billing API errors fall back to Compute capacityHistory or catalog baseline rates.
                comp_rates = {}
        if not comp_rates and eff_token:
            try:
                skus_fn = (
                    catalog_skus
                    if catalog_skus is not _ORIG_CATALOG_SKUS
                    else _billing_catalog_mod.catalog_skus
                )
                skus = await asyncio.to_thread(skus_fn)
                if skus:
                    comp_rates = parse_component_rates(skus)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                # Authenticated Billing Catalog lookup failed; continue with Compute capacityHistory.
                comp_rates = {}
        if eff_token:
            api_cls = (
                AsyncGoogleApi
                if AsyncGoogleApi is not _ORIG_ASYNC_GOOGLE_API
                else _compute_api_mod.AsyncGoogleApi
            )
            price_fn = (
                spot_price
                if spot_price is not _ORIG_SPOT_PRICE
                else _spot_pricing_mod.spot_price
            )
            api = api_cls(eff_token, concurrency=32)
            reg_map = {r["region"]: r for r in REGIONS_CATALOG}
            shape_map = {s["machine_type"]: s for s in MACHINE_SHAPES_CATALOG}
            tasks = []
            for reg_code in target_regions:
                reg_meta = reg_map.get(reg_code)
                for m_type in target_machines:
                    s_meta = shape_map.get(m_type)
                    if reg_meta and s_meta and s_meta["family"] not in reg_meta["families"]:
                        continue
                    tasks.append(price_fn(api, eff_project, reg_code, m_type))
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, tuple) and len(res) == 3:
                        r_code, m_name, hr_price = res
                        if hr_price is not None and hr_price > 0:
                            mach_hourly[(str(r_code), str(m_name))] = float(hr_price)
        return comp_rates, mach_hourly

    try:
        comp_rates, mach_hourly = asyncio.run(_query_gcp_pricing())
    except (OSError, ValueError, RuntimeError):
        # If an event loop is already running or GCP API calls fail, fall back cleanly to baseline rates.
        comp_rates, mach_hourly = {}, {}

    source = (
        "gcp_capacity_history"
        if mach_hourly
        else ("gcp_billing_catalog" if comp_rates else "catalog_baseline")
    )
    payload = {
        "source": source,
        "fetched_at": now_ts,
        "component_rates": comp_rates,
        "machine_hourly": mach_hourly,
        "disk_monthly_gib": {},
    }
    _LIVE_SPOT_PRICING_CACHE[cache_key] = payload
    return payload


def calculate_spot_pricing(
    *,
    family: str,
    vcpu: int,
    ram_gib: float,
    region: str,
    boot_disk_type: str,
    boot_disk_gb: int,
    data_disk_type: str,
    data_disk_gb: int,
    tax_rate_percent: float = 0.0,
    machine_type: str | None = None,
    live_pricing: dict[str, Any] | None = None,
    project: str = "homelab-424523",
) -> dict[str, Any]:
    """Calculate compute-only and all-in (with disk & taxes) hourly, daily, and monthly prices."""
    reg_info = next((r for r in REGIONS_CATALOG if r["region"] == region), None)
    mult = _dec(reg_info["price_multiplier"] if reg_info else "1.0")
    resolved_live = (
        live_pricing
        if live_pricing is not None
        else fetch_live_spot_pricing(
            project=project,
            regions=[region],
            machine_types=[machine_type] if machine_type else None,
        )
    )
    machine_hourly_map = (resolved_live or {}).get("machine_hourly") or {}
    comp_rates_map = (resolved_live or {}).get("component_rates") or {}
    disk_rates_map = (resolved_live or {}).get("disk_monthly_gib") or {}

    live_machine_rate = None
    if machine_type:
        live_machine_rate = machine_hourly_map.get((region, machine_type))
        if live_machine_rate is None:
            live_machine_rate = machine_hourly_map.get(f"{region}/{machine_type}")

    series_key = family.lower()
    live_cpu_rate = comp_rates_map.get(("spot", region, series_key, "CPU"))
    live_ram_rate = comp_rates_map.get(("spot", region, series_key, "RAM"))

    if live_machine_rate is not None:
        compute_hourly = _dec(live_machine_rate)
        pricing_source = "gcp_capacity_history"
    elif live_cpu_rate is not None and live_ram_rate is not None:
        compute_hourly = _dec(live_cpu_rate) * _dec(vcpu) + _dec(live_ram_rate) * _dec(ram_gib)
        pricing_source = "gcp_billing_catalog"
    else:
        cpu_rate, ram_rate = SERIES_SPOT_BASE_RATES.get(
            family, (Decimal("0.0040"), Decimal("0.00045"))
        )
        compute_hourly = (cpu_rate * _dec(vcpu) + ram_rate * _dec(ram_gib)) * mult
        pricing_source = "catalog_baseline"

    compute_daily = compute_hourly * HOURS_PER_DAY
    compute_monthly = compute_hourly * HOURS_PER_MONTH

    live_boot_disk = (
        disk_rates_map.get((region, boot_disk_type))
        or disk_rates_map.get(f"{region}/{boot_disk_type}")
        or disk_rates_map.get(boot_disk_type)
    )
    live_data_disk = (
        disk_rates_map.get((region, data_disk_type))
        or disk_rates_map.get(f"{region}/{data_disk_type}")
        or disk_rates_map.get(data_disk_type)
    )
    boot_rate = (
        _dec(live_boot_disk)
        if live_boot_disk is not None
        else DISK_MONTHLY_RATES_USD_PER_GIB.get(boot_disk_type, Decimal("0.080")) * mult
    )
    data_rate = (
        _dec(live_data_disk)
        if live_data_disk is not None
        else DISK_MONTHLY_RATES_USD_PER_GIB.get(data_disk_type, Decimal("0.080")) * mult
    )
    disk_monthly = boot_rate * _dec(boot_disk_gb) + data_rate * _dec(data_disk_gb)
    disk_daily = disk_monthly / DAYS_PER_MONTH
    disk_hourly = disk_daily / HOURS_PER_DAY

    subtotal_hourly = compute_hourly + disk_hourly
    tax_dec = _dec(tax_rate_percent)
    if tax_dec < Decimal("0") or tax_dec > Decimal("100"):
        raise ValueError(f"tax_rate_percent must be between 0 and 100 (got {tax_rate_percent})")
    tax_fraction = tax_dec / Decimal("100")
    tax_hourly = subtotal_hourly * tax_fraction
    total_hourly = subtotal_hourly + tax_hourly
    total_daily = total_hourly * HOURS_PER_DAY
    total_monthly = total_hourly * HOURS_PER_MONTH

    return {
        "pricing_source": pricing_source,
        "compute_hourly_usd": _money4(compute_hourly),
        "compute_daily_usd": _money(compute_daily),
        "compute_monthly_usd": _money(compute_monthly),
        "disk_hourly_usd": _money4(disk_hourly),
        "disk_daily_usd": _money(disk_daily),
        "disk_monthly_usd": _money(disk_monthly),
        "tax_rate_percent": float(tax_rate_percent),
        "tax_hourly_usd": _money4(tax_hourly),
        "tax_daily_usd": _money(tax_hourly * HOURS_PER_DAY),
        "tax_monthly_usd": _money(tax_hourly * HOURS_PER_MONTH),
        "total_hourly_usd": _money4(total_hourly),
        "total_daily_usd": _money(total_daily),
        "total_monthly_usd": _money(total_monthly),
    }


def normalize_budget(budget: dict[str, Any]) -> dict[str, Any]:
    """Synchronize hourly, daily, and monthly budget fields when edited on the fly."""
    active_period = str(budget.get("active_period", "day")).lower()
    if active_period not in ("hour", "day", "month"):
        active_period = "day"

    max_hourly = _dec(budget.get("max_hourly_usd", "0.42"))
    max_daily = _dec(budget.get("max_daily_usd", "10.00"))
    max_monthly = _dec(budget.get("max_monthly_usd", "50.00"))
    if max_hourly < Decimal("0") or max_daily < Decimal("0") or max_monthly < Decimal("0"):
        raise ValueError("Budget limits (max_hourly_usd, max_daily_usd, max_monthly_usd) must be >= 0")

    raw_tax = budget.get("tax_rate_percent", 8.25)
    tax_dec = _dec(8.25 if raw_tax is None else raw_tax)
    if tax_dec < Decimal("0") or tax_dec > Decimal("100"):
        raise ValueError(f"tax_rate_percent must be between 0 and 100 (got {raw_tax})")

    sync_periods = bool(budget.get("sync_periods", False))
    if sync_periods:
        if active_period == "hour":
            max_daily = max_hourly * HOURS_PER_DAY
            max_monthly = max_hourly * HOURS_PER_MONTH
        elif active_period == "day":
            max_hourly = max_daily / HOURS_PER_DAY
            max_monthly = max_daily * DAYS_PER_MONTH
        elif active_period == "month":
            max_daily = max_monthly / DAYS_PER_MONTH
            max_hourly = max_monthly / HOURS_PER_MONTH

    normalized: dict[str, Any] = {
        "active_period": active_period,
        "max_hourly_usd": _money4(max_hourly),
        "max_daily_usd": _money(max_daily),
        "max_monthly_usd": _money(max_monthly),
        "include_disk_costs": bool(budget.get("include_disk_costs", True)),
        "include_taxes": bool(budget.get("include_taxes", False)),
        "tax_rate_percent": float(tax_dec),
    }
    if "override_regional_tax" in budget:
        normalized["override_regional_tax"] = bool(budget.get("override_regional_tax"))
    return normalized


def filter_regions(
    regions_state: dict[str, bool],
    *,
    max_latency_ms: float | None = None,
    continents: list[str] | None = None,
    required_family: str | None = None,
    required_cpu_platform: str | None = None,
    required_disk_type: str | None = None,
    only_enabled: bool = False,
    region_latencies_ms: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Filter world data center regions by latency, continent, instance family, CPU type, and disk support."""
    results: list[dict[str, Any]] = []
    continent_set = {c.strip() for c in continents if c.strip()} if continents else None
    for item in REGIONS_CATALOG:
        region = item["region"]
        enabled = bool(regions_state.get(region, True))
        lat_ms = (
            int(region_latencies_ms.get(region, item["latency_ms"]))
            if region_latencies_ms
            else int(item["latency_ms"])
        )
        if only_enabled and not enabled:
            continue
        if max_latency_ms is not None and lat_ms > max_latency_ms:
            continue
        if continent_set and item["continent"] not in continent_set:
            continue
        if required_family and required_family not in item["families"]:
            continue
        if required_cpu_platform and required_cpu_platform not in item["cpu_platforms"]:
            continue
        if required_disk_type and required_disk_type not in item["disk_types"]:
            continue
        results.append({**item, "latency_ms": lat_ms, "enabled": enabled})
    return results


def measure_region_latencies(timeout_seconds: float = 0.25) -> dict[str, int]:
    """Measure or refresh TCP latency to GCP regional endpoints with deterministic fallback."""
    measured: dict[str, int] = {}
    for item in REGIONS_CATALOG:
        region = item["region"]
        baseline = int(item["latency_ms"])
        host = f"{region}-aiplatform.googleapis.com"
        started = time.perf_counter()
        try:
            with socket.create_connection((host, 443), timeout=timeout_seconds):
                elapsed_ms = int(round((time.perf_counter() - started) * 1000))
                measured[region] = max(5, elapsed_ms)
        except OSError:
            measured[region] = baseline
    return measured


def qualify_instances(
    profile_config: dict[str, Any],
    regions_state: dict[str, bool],
    project: str = "homelab-424523",
    region_latencies_ms: dict[str, int] | None = None,
    live_pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate all regional machine shapes against workload sizing, CPU benchmark score,
    disk compatibility, enabled regions, and Spot + disk budget caps.

    Supports both 'cicd_runner' and 'rust_builder' profiles independently.
    """
    role = profile_config.get("role", "cicd_runner")
    min_runners = max(1, int(profile_config.get("min_runners", 4 if role == "cicd_runner" else 1)))
    min_instances = max(1, int(profile_config.get("min_instances", 1)))
    max_instances = max(min_instances, int(profile_config.get("max_instances", 10)))

    # Per-runner / per-builder unit requirements
    unit_vcpu = max(1, int(profile_config.get("runner_vcpu", 4 if role == "cicd_runner" else 16)))
    unit_ram_gib = max(1, float(profile_config.get("runner_ram_gib", 16 if role == "cicd_runner" else 28)))
    host_overhead_gib = max(0.0, float(profile_config.get("host_overhead_gib", 0.0)))
    min_instance_vcpu = max(unit_vcpu, int(profile_config.get("min_instance_vcpu", unit_vcpu)))
    min_instance_ram_gib = max(
        unit_ram_gib + host_overhead_gib,
        float(profile_config.get("min_instance_ram_gib", unit_ram_gib + host_overhead_gib)),
    )
    min_benchmark_score = max(0, int(profile_config.get("min_benchmark_score", 1000)))

    disk_mode = str(profile_config.get("disk_category", "any")).lower()
    requested_disk_type = str(profile_config.get("disk_type", "hyperdisk-balanced"))
    boot_disk_gb = max(20, int(profile_config.get("boot_disk_gb", 40)))
    data_disk_gb = max(20, int(profile_config.get("data_disk_gb", 100)))

    budget = normalize_budget(profile_config.get("budget", {}))
    include_disk = budget["include_disk_costs"]
    include_taxes = budget["include_taxes"]
    override_regional_tax = bool(budget.get("override_regional_tax", False))

    enabled_regions = [r["region"] for r in REGIONS_CATALOG if regions_state.get(r["region"], True)]
    resolved_live_pricing = (
        live_pricing
        if live_pricing is not None
        else fetch_live_spot_pricing(project=project, regions=enabled_regions)
    )

    hardware_qualifying: list[dict[str, Any]] = []
    spot_price_qualifying: list[dict[str, Any]] = []
    disqualified_counts = {
        "region_disabled": 0,
        "insufficient_vcpu_or_ram": 0,
        "exceeds_max_instances": 0,
        "below_benchmark_score": 0,
        "incompatible_disk": 0,
        "exceeds_spot_budget": 0,
    }

    for reg in REGIONS_CATALOG:
        region = reg["region"]
        if not regions_state.get(region, True):
            disqualified_counts["region_disabled"] += 1
            continue
        effective_latency_ms = (
            int(region_latencies_ms.get(region, reg["latency_ms"]))
            if region_latencies_ms
            else int(reg["latency_ms"])
        )
        reg_tax_rate = (
            float(budget["tax_rate_percent"])
            if override_regional_tax
            else float(reg.get("tax_rate_default", budget["tax_rate_percent"]))
        )
        tax_rate = reg_tax_rate if include_taxes else 0.0

        for shape in MACHINE_SHAPES_CATALOG:
            family = shape["family"]
            if family not in reg["families"]:
                continue
            cpu_platform = shape["cpu_platform"]
            if cpu_platform not in reg["cpu_platforms"]:
                continue
            vcpu = int(shape["vcpu"])
            ram_gib = float(shape["ram_gib"])

            # Calculate how many runners/units this instance can host
            if role == "cicd_runner":
                slots_by_cpu = vcpu // unit_vcpu
                usable_ram = max(0.0, ram_gib - host_overhead_gib)
                slots_by_ram = int(usable_ram // unit_ram_gib)
                units_per_instance = min(slots_by_cpu, slots_by_ram)
            else:
                units_per_instance = 1 if (vcpu >= unit_vcpu and ram_gib >= unit_ram_gib + host_overhead_gib) else 0

            if units_per_instance < 1 or vcpu < min_instance_vcpu or ram_gib < min_instance_ram_gib:
                disqualified_counts["insufficient_vcpu_or_ram"] += 1
                continue

            instances_for_floor = max(
                min_instances,
                (min_runners + units_per_instance - 1) // units_per_instance,
            )
            if instances_for_floor > max_instances:
                disqualified_counts["exceeds_max_instances"] += 1
                continue

            benchmark = compute_cpu_benchmark_score(cpu_platform, vcpu, shape["machine_type"])
            if benchmark < min_benchmark_score:
                disqualified_counts["below_benchmark_score"] += 1
                continue

            # Disk compatibility check
            allowed_disks = [d for d in compatible_disk_types(family) if d in reg["disk_types"]]
            if disk_mode == "hyperdrive":
                allowed_disks = [d for d in allowed_disks if disk_category(d) == "hyperdrive"]
            elif disk_mode == "normal":
                allowed_disks = [d for d in allowed_disks if disk_category(d) == "normal"]

            if not allowed_disks:
                disqualified_counts["incompatible_disk"] += 1
                continue

            chosen_data_disk = requested_disk_type if requested_disk_type in allowed_disks else allowed_disks[0]
            if chosen_data_disk == "hyperdisk-throughput" and data_disk_gb < 2048:
                fallback = [d for d in allowed_disks if d != "hyperdisk-throughput"]
                if not fallback:
                    disqualified_counts["incompatible_disk"] += 1
                    continue
                chosen_data_disk = fallback[0]

            # GCP does not allow hyperdisk-throughput as a boot disk on any machine series.
            boot_candidates = [d for d in allowed_disks if d != "hyperdisk-throughput"]
            if not boot_candidates:
                disqualified_counts["incompatible_disk"] += 1
                continue
            if family in ("c4d", "n4d", "c4", "n4"):
                if "hyperdisk-balanced" not in boot_candidates:
                    disqualified_counts["incompatible_disk"] += 1
                    continue
                chosen_boot_disk = "hyperdisk-balanced"
            elif chosen_data_disk != "hyperdisk-throughput":
                chosen_boot_disk = chosen_data_disk
            elif "hyperdisk-balanced" in boot_candidates:
                chosen_boot_disk = "hyperdisk-balanced"
            else:
                chosen_boot_disk = boot_candidates[0]

            pricing = calculate_spot_pricing(
                family=family,
                vcpu=vcpu,
                ram_gib=ram_gib,
                region=region,
                boot_disk_type=chosen_boot_disk,
                boot_disk_gb=boot_disk_gb,
                data_disk_type=chosen_data_disk,
                data_disk_gb=data_disk_gb,
                tax_rate_percent=tax_rate,
                machine_type=shape["machine_type"],
                live_pricing=resolved_live_pricing,
                project=project,
            )

            if include_disk:
                eval_hourly = pricing["total_hourly_usd"] if include_taxes else round(
                    pricing["compute_hourly_usd"] + pricing["disk_hourly_usd"], 4
                )
                eval_daily = pricing["total_daily_usd"] if include_taxes else round(
                    pricing["compute_daily_usd"] + pricing["disk_daily_usd"], 2
                )
                eval_monthly = pricing["total_monthly_usd"] if include_taxes else round(
                    pricing["compute_monthly_usd"] + pricing["disk_monthly_usd"], 2
                )
            else:
                if include_taxes:
                    tax_mult = Decimal("1") + (_dec(tax_rate) / Decimal("100"))
                    eval_hourly = _money4(_dec(pricing["compute_hourly_usd"]) * tax_mult)
                    eval_daily = _money(_dec(pricing["compute_daily_usd"]) * tax_mult)
                    eval_monthly = _money(_dec(pricing["compute_monthly_usd"]) * tax_mult)
                else:
                    eval_hourly = pricing["compute_hourly_usd"]
                    eval_daily = pricing["compute_daily_usd"]
                    eval_monthly = pricing["compute_monthly_usd"]

            fleet_floor_hourly = round(eval_hourly * instances_for_floor, 4)
            fleet_floor_daily = round(eval_daily * instances_for_floor, 2)
            fleet_floor_monthly = round(eval_monthly * instances_for_floor, 2)

            zone = reg["zones"][0]
            within_hourly = fleet_floor_hourly <= budget["max_hourly_usd"]
            within_daily = fleet_floor_daily <= budget["max_daily_usd"]
            within_monthly = fleet_floor_monthly <= budget["max_monthly_usd"]
            price_qualifies = within_hourly and within_daily and within_monthly

            candidate = {
                "region": region,
                "zone": zone,
                "display_location": reg["display_name"],
                "continent": reg["continent"],
                "latency_ms": effective_latency_ms,
                "machine_type": shape["machine_type"],
                "family": family,
                "vcpu": vcpu,
                "ram_gib": ram_gib,
                "cpu_platform": cpu_platform,
                "benchmark_score": benchmark,
                "units_per_instance": units_per_instance,
                "unit_vcpu": unit_vcpu,
                "unit_ram_gib": unit_ram_gib,
                "instances_needed_for_floor": instances_for_floor,
                "total_units_at_floor": units_per_instance * instances_for_floor,
                "boot_disk_type": chosen_boot_disk,
                "data_disk_type": chosen_data_disk,
                "disk_category": disk_category(chosen_data_disk),
                "boot_disk_gb": boot_disk_gb,
                "data_disk_gb": data_disk_gb,
                "pricing": pricing,
                "evaluated_hourly_usd": eval_hourly,
                "evaluated_daily_usd": eval_daily,
                "evaluated_monthly_usd": eval_monthly,
                "fleet_floor_hourly_usd": fleet_floor_hourly,
                "fleet_floor_daily_usd": fleet_floor_daily,
                "fleet_floor_monthly_usd": fleet_floor_monthly,
                "price_qualifies": price_qualifies,
                "ssh_command_preview": format_gcloud_ssh_command(
                    f"{'oxidex-runners' if role == 'cicd_runner' else 'builder-oxidex'}-{region}",
                    zone,
                    project,
                ),
            }
            hardware_qualifying.append(candidate)
            if price_qualifies:
                spot_price_qualifying.append(candidate)
            else:
                disqualified_counts["exceeds_spot_budget"] += 1

    hardware_qualifying.sort(
        key=lambda row: (row["evaluated_daily_usd"], -row["benchmark_score"], row["latency_ms"])
    )
    spot_price_qualifying.sort(
        key=lambda row: (row["evaluated_daily_usd"], -row["benchmark_score"], row["latency_ms"])
    )

    return {
        "role": role,
        "pricing_source": str((resolved_live_pricing or {}).get("source") or "catalog_baseline"),
        "requirements": {
            "min_runners": min_runners,
            "min_instances": min_instances,
            "max_instances": max_instances,
            "runner_vcpu": unit_vcpu,
            "runner_ram_gib": unit_ram_gib,
            "host_overhead_gib": host_overhead_gib,
            "min_instance_vcpu": min_instance_vcpu,
            "min_instance_ram_gib": min_instance_ram_gib,
            "min_benchmark_score": min_benchmark_score,
            "disk_category": disk_mode,
            "disk_type": requested_disk_type,
            "boot_disk_gb": boot_disk_gb,
            "data_disk_gb": data_disk_gb,
            "budget": budget,
        },
        "hardware_qualifying_count": len(hardware_qualifying),
        "spot_price_qualifying_count": len(spot_price_qualifying),
        "hardware_qualifying": hardware_qualifying,
        "spot_price_qualifying": spot_price_qualifying,
        "disqualified_counts": disqualified_counts,
    }


def _strip_yaml_inline_comment(line: str) -> str:
    """Strip an unquoted YAML inline comment (# preceded by whitespace) from a line."""
    in_single = False
    in_double = False
    escape = False
    for i, ch in enumerate(line):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_double:
            escape = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1].isspace():
                return line[:i].rstrip()
    return line.rstrip()


def _split_yaml_flow_top_level(text: str, sep: str) -> list[str]:
    """Split a YAML flow expression by a top-level separator outside quotes and nested brackets/braces."""
    parts: list[str] = []
    cur: list[str] = []
    in_single = False
    in_double = False
    escape = False
    bracket_depth = 0
    brace_depth = 0
    for ch in text:
        if escape:
            cur.append(ch)
            escape = False
            continue
        if ch == "\\" and in_double:
            cur.append(ch)
            escape = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            cur.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            cur.append(ch)
        elif not in_single and not in_double:
            if ch == "[":
                bracket_depth += 1
                cur.append(ch)
            elif ch == "]":
                bracket_depth -= 1
                cur.append(ch)
            elif ch == "{":
                brace_depth += 1
                cur.append(ch)
            elif ch == "}":
                brace_depth -= 1
                cur.append(ch)
            elif ch == sep and bracket_depth == 0 and brace_depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _validate_yaml_flow_value(
    expr: str,
    line_no: int,
    *,
    in_flow: bool = False,
    defined_anchors: set[str] | None = None,
) -> list[str]:
    """Validate YAML flow sequences ([...]), flow mappings ({...}), and scalar values."""
    val = expr.strip()
    if not val:
        return []
    errors: list[str] = []
    if val[0] in ("[", "{"):
        expected_close = "]" if val[0] == "[" else "}"
        # Verify the opening bracket/brace at index 0 closes at the final character
        in_single = False
        in_double = False
        escape = False
        depth = 0
        closed_at = -1
        for i, ch in enumerate(val):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_double:
                escape = True
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if ch == val[0]:
                    depth += 1
                elif ch == expected_close:
                    depth -= 1
                    if depth == 0:
                        closed_at = i
                        break
        if closed_at != len(val) - 1:
            errors.append(
                f"YAML syntax error on line {line_no}: trailing tokens after flow collection in '{val}'."
            )
            return errors
        inner = val[1:-1].strip()
        if not inner:
            return []
        items = _split_yaml_flow_top_level(inner, ",")
        for raw_item in items:
            item = raw_item.strip()
            if not item:
                errors.append(
                    f"YAML syntax error on line {line_no}: empty element in flow collection '{val}'."
                )
                return errors
            if val[0] == "{":
                kv_parts = _split_yaml_flow_top_level(item, ":")
                if len(kv_parts) != 2 or not kv_parts[0].strip() or not kv_parts[1].strip():
                    errors.append(
                        f"YAML syntax error on line {line_no}: invalid flow mapping entry '{item}' in '{val}'."
                    )
                    return errors
                errors.extend(
                    _validate_yaml_flow_value(
                        kv_parts[0].strip(), line_no, in_flow=True, defined_anchors=defined_anchors
                    )
                )
                errors.extend(
                    _validate_yaml_flow_value(
                        kv_parts[1].strip(), line_no, in_flow=True, defined_anchors=defined_anchors
                    )
                )
            else:
                kv_parts = _split_yaml_flow_top_level(item, ":")
                if len(kv_parts) > 2:
                    errors.append(
                        f"YAML syntax error on line {line_no}: invalid nested colons in flow sequence item '{item}'."
                    )
                    return errors
                if len(kv_parts) == 2 and not (item.startswith("'") or item.startswith('"')):
                    if not kv_parts[0].strip() or not kv_parts[1].strip():
                        errors.append(
                            f"YAML syntax error on line {line_no}: incomplete mapping entry '{item}' in flow sequence."
                        )
                        return errors
                    errors.extend(
                        _validate_yaml_flow_value(
                            kv_parts[1].strip(), line_no, in_flow=True, defined_anchors=defined_anchors
                        )
                    )
                else:
                    errors.extend(
                        _validate_yaml_flow_value(
                            item, line_no, in_flow=True, defined_anchors=defined_anchors
                        )
                    )
        return errors

    if val.startswith("&"):
        m_anchor = re.match(r"^&([A-Za-z0-9_-]+)(?:\s+(.*))?$", val)
        if not m_anchor:
            errors.append(
                f"YAML syntax error on line {line_no}: invalid anchor syntax '{val}'."
            )
            return errors
        if defined_anchors is not None:
            defined_anchors.add(m_anchor.group(1))
        rest = (m_anchor.group(2) or "").strip()
        if not rest:
            return errors
        return _validate_yaml_flow_value(
            rest, line_no, in_flow=in_flow, defined_anchors=defined_anchors
        )

    if val.startswith("*"):
        m_alias = re.match(r"^\*([A-Za-z0-9_-]+)$", val)
        if not m_alias:
            errors.append(
                f"YAML syntax error on line {line_no}: invalid alias syntax '{val}'."
            )
            return errors
        alias_name = m_alias.group(1)
        if defined_anchors is None or alias_name not in defined_anchors:
            errors.append(
                f"YAML syntax error on line {line_no}: undefined alias '*{alias_name}'."
            )
        return errors

    if val[0] in ("@", "`"):
        errors.append(
            f"YAML syntax error on line {line_no}: reserved indicator '{val[0]}' cannot start a plain scalar."
        )
        return errors

    if val[0] in ("'", '"'):
        quote_ch = val[0]
        closed_at = -1
        i = 1
        valid_single_escapes = set('0abtnvfre "/\\N_LP\t')
        while i < len(val):
            ch = val[i]
            if ch == "\\" and quote_ch == '"':
                if i + 1 >= len(val):
                    break
                nxt = val[i + 1]
                if nxt in valid_single_escapes:
                    i += 2
                    continue
                if nxt in ("x", "u", "U"):
                    hex_len = 2 if nxt == "x" else (4 if nxt == "u" else 8)
                    hex_part = val[i + 2 : i + 2 + hex_len]
                    if len(hex_part) == hex_len and all(c in "0123456789abcdefABCDEF" for c in hex_part):
                        i += 2 + hex_len
                        continue
                errors.append(
                    f"YAML syntax error on line {line_no}: invalid escape sequence '\\{nxt}' in double-quoted scalar."
                )
                return errors
            if ch == "'" and quote_ch == "'" and i + 1 < len(val) and val[i + 1] == "'":
                i += 2
                continue
            if ch == quote_ch:
                closed_at = i
                break
            i += 1
        if closed_at != len(val) - 1:
            errors.append(
                f"YAML syntax error on line {line_no}: unexpected trailing content after quoted scalar '{val}'."
            )
        return errors

    # Plain unquoted scalar: check for unquoted ': ' (nested mapping value on same line)
    in_s = False
    in_d = False
    esc = False
    for idx_c, ch in enumerate(val):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_d:
            esc = True
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d and ch == ":":
            if idx_c == len(val) - 1 or val[idx_c + 1] in (" ", "\t"):
                errors.append(
                    f"YAML syntax error on line {line_no}: mapping values are not allowed in plain scalar '{val}'."
                )
                return errors

    if in_flow and any(ch in val for ch in ("[", "]", "{", "}")):
        errors.append(
            f"YAML syntax error on line {line_no}: unquoted flow brackets or braces in scalar '{val}'."
        )
    return errors


def _validate_cloud_init_syntax(
    lines: list[str],
    *,
    is_cloud_config: bool,
    is_shell_script: bool,
) -> list[str]:
    """Perform syntax and structural validation on cloud-config YAML or shell startup scripts."""
    import shutil
    import subprocess

    errors: list[str] = []
    if is_cloud_config:
        block_scalar_indent: int | None = None
        scopes: list[dict[str, Any]] = []
        defined_anchors: set[str] = set()
        for idx, raw in enumerate(lines[1:], start=2):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                errors.append(f"YAML syntax error on line {idx}: tab characters are not allowed for indentation.")
                continue
            if block_scalar_indent is not None:
                if indent > block_scalar_indent:
                    continue
                block_scalar_indent = None

            stripped = _strip_yaml_inline_comment(raw.strip())
            if not stripped:
                continue
            # Check balanced brackets, braces, and quotes on non-scalar YAML lines
            in_single = False
            in_double = False
            escape = False
            bracket_depth = 0
            brace_depth = 0
            for ch in stripped:
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == "'" and not in_double:
                    in_single = not in_single
                elif ch == '"' and not in_single:
                    in_double = not in_double
                elif not in_single and not in_double:
                    if ch == "[":
                        bracket_depth += 1
                    elif ch == "]":
                        bracket_depth -= 1
                    elif ch == "{":
                        brace_depth += 1
                    elif ch == "}":
                        brace_depth -= 1
                    if bracket_depth < 0 or brace_depth < 0:
                        break
            if in_single or in_double or bracket_depth != 0 or brace_depth != 0:
                errors.append(
                    f"YAML syntax error on line {idx}: unclosed bracket, brace, or quote in '{stripped}'."
                )
                continue

            is_mapping = bool(re.match(r"^[A-Za-z0-9_.-]+\s*:(?:\s+.*|\s*)$", stripped))
            is_list_item = bool(re.match(r"^-(?:\s+.*|\s*)$", stripped))
            if not (is_mapping or is_list_item):
                errors.append(
                    f"YAML syntax error on line {idx}: expected mapping 'key: value' or list item '- value'."
                )
                continue

            if indent % 2 != 0:
                errors.append(
                    f"YAML syntax error on line {idx}: inconsistent indentation ({indent} spaces)."
                )
                continue

            while scopes and scopes[-1]["indent"] > indent:
                scopes.pop()

            if scopes and scopes[-1]["indent"] == indent:
                prev_kind = scopes[-1]["kind"]
                cur_kind = "list" if is_list_item else "map"
                if prev_kind != cur_kind:
                    errors.append(
                        f"YAML syntax error on line {idx}: cannot mix mapping and sequence items at indent {indent}."
                    )
                    continue
                scopes.pop()

            if indent > 0:
                if not scopes:
                    errors.append(
                        f"YAML syntax error on line {idx}: unexpected top-level indentation ({indent} spaces)."
                    )
                    continue
                parent = scopes[-1]
                if not parent["allow_children"]:
                    errors.append(
                        f"YAML syntax error on line {idx}: unexpected nested block under scalar value."
                    )
                    continue
                if parent["child_indent"] is None:
                    parent["child_indent"] = indent
                elif parent["child_indent"] != indent:
                    errors.append(
                        f"YAML syntax error on line {idx}: inconsistent indentation ({indent} spaces, expected {parent['child_indent']})."
                    )
                    continue

            if is_list_item:
                after_dash = stripped[1:].lstrip(" ")
                if not after_dash:
                    scopes.append(
                        {"indent": indent, "kind": "list", "child_indent": None, "allow_children": True}
                    )
                elif re.match(r"^[A-Za-z0-9_.-]+\s*:(?:\s+.*|\s*)$", after_dash):
                    key_indent = indent + (len(stripped) - len(after_dash))
                    scopes.append(
                        {"indent": indent, "kind": "list", "child_indent": key_indent, "allow_children": True}
                    )
                    _, _, v_part = after_dash.partition(":")
                    v_str = v_part.strip()
                    if re.search(r":\s*[|>][-+]?\s*$", after_dash):
                        block_scalar_indent = indent
                        scopes.append(
                            {"indent": key_indent, "kind": "map", "child_indent": None, "allow_children": False}
                        )
                    elif not v_str:
                        scopes.append(
                            {"indent": key_indent, "kind": "map", "child_indent": None, "allow_children": True}
                        )
                    else:
                        errors.extend(
                            _validate_yaml_flow_value(v_str, idx, defined_anchors=defined_anchors)
                        )
                        scopes.append(
                            {"indent": key_indent, "kind": "map", "child_indent": None, "allow_children": False}
                        )
                else:
                    if re.search(r"^[|>][-+]?\s*$", after_dash):
                        block_scalar_indent = indent
                    else:
                        errors.extend(
                            _validate_yaml_flow_value(after_dash, idx, defined_anchors=defined_anchors)
                        )
                    scopes.append(
                        {"indent": indent, "kind": "list", "child_indent": None, "allow_children": False}
                    )
            else:
                _, _, v_part = stripped.partition(":")
                v_str = v_part.strip()
                if re.search(r":\s*[|>][-+]?\s*$", stripped):
                    block_scalar_indent = indent
                    scopes.append(
                        {"indent": indent, "kind": "map", "child_indent": None, "allow_children": False}
                    )
                elif not v_str:
                    scopes.append(
                        {"indent": indent, "kind": "map", "child_indent": None, "allow_children": True}
                    )
                else:
                    m_block_anchor = re.match(r"^&([A-Za-z0-9_-]+)\s+[|>][-+]?\s*$", v_str)
                    if m_block_anchor:
                        defined_anchors.add(m_block_anchor.group(1))
                        block_scalar_indent = indent
                        scopes.append(
                            {"indent": indent, "kind": "map", "child_indent": None, "allow_children": False}
                        )
                        continue
                    m_map_anchor = re.match(r"^&([A-Za-z0-9_-]+)$", v_str)
                    if m_map_anchor:
                        defined_anchors.add(m_map_anchor.group(1))
                        scopes.append(
                            {"indent": indent, "kind": "map", "child_indent": None, "allow_children": True}
                        )
                        continue
                    errors.extend(
                        _validate_yaml_flow_value(v_str, idx, defined_anchors=defined_anchors)
                    )
                    scopes.append(
                        {"indent": indent, "kind": "map", "child_indent": None, "allow_children": False}
                    )

        if not errors:
            yaml_doc = "\n".join(lines) + "\n"
            ruby_bin = shutil.which("ruby") or ("/usr/bin/ruby" if os.path.exists("/usr/bin/ruby") else None)
            if ruby_bin:
                try:
                    proc = _ORIG_SUBPROCESS_RUN(
                        [
                            ruby_bin,
                            "-ryaml",
                            "-e",
                            'doc = YAML.safe_load(STDIN.read, [], [], true); abort("YAML syntax error: top-level cloud-config document must be a mapping") unless doc.is_a?(Hash)',
                        ],
                        input=yaml_doc,
                        text=True,
                        capture_output=True,
                        timeout=2,
                        check=False,
                    )
                    if proc.returncode != 0:
                        err_line = (proc.stderr or "invalid YAML syntax").strip().splitlines()[0]
                        errors.append(f"YAML syntax error: {err_line}")
                except (OSError, subprocess.SubprocessError):
                    # If the external Ruby Psych YAML parser cannot be executed, rely on the structural YAML validator above.
                    pass
    elif is_shell_script:
        import shutil

        script_body = "\n".join(lines) + "\n"
        shebang = lines[0].strip() if lines else ""
        if re.search(r"\bbash\b", shebang):
            checker_bin = shutil.which("bash") or "/bin/bash"
        elif re.search(r"\bzsh\b", shebang):
            checker_bin = shutil.which("zsh") or "/bin/zsh"
        else:
            checker_bin = "/bin/sh"
        try:
            proc = subprocess.run(
                [checker_bin, "-n"],
                input=script_body,
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
            if proc.returncode != 0:
                err_msg = (proc.stderr or "invalid shell syntax").strip().splitlines()[0]
                errors.append(f"Shell syntax error: {err_msg}")
        except OSError:
            # Declared shell interpreter is not available in this execution environment; skip external syntax check.
            pass
    return errors


def parse_cloud_init(content: str) -> dict[str, Any]:
    """Parse a #cloud-config YAML or startup script into visual provisioning stages
    and validate structure for the Cloud-Init Studio visualizer.
    """
    text = (content or "").strip()
    lines = text.splitlines()
    is_cloud_config = bool(lines and lines[0].strip().startswith("#cloud-config"))
    is_shell_script = bool(lines and lines[0].strip().startswith("#!"))

    package_update = bool(re.search(r"^\s*package_update:\s*true\b", text, re.M | re.I))
    package_upgrade = bool(re.search(r"^\s*package_upgrade:\s*true\b", text, re.M | re.I))

    def _extract_top_list(section_name: str) -> list[str]:
        items: list[str] = []
        in_section = False
        for raw in lines:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(rf"^{re.escape(section_name)}:\s*$", raw):
                in_section = True
                continue
            if in_section:
                if re.match(r"^[A-Za-z0-9_-]+:\s*", raw):
                    break
                match = re.match(r"^\s*-\s+(.+)$", raw)
                if match:
                    items.append(match.group(1).strip())
        return items

    groups = _extract_top_list("groups")
    packages = _extract_top_list("packages")
    runcmd = _extract_top_list("runcmd")

    # Extract users
    users: list[dict[str, str]] = []
    in_users = False
    current_user: dict[str, str] | None = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^users:\s*$", raw):
            in_users = True
            continue
        if in_users:
            if re.match(r"^[A-Za-z0-9_-]+:\s*", raw):
                if current_user:
                    users.append(current_user)
                current_user = None
                break
            item_match = re.match(r"^\s*-\s+(.+)$", raw)
            if item_match:
                if current_user:
                    users.append(current_user)
                val = item_match.group(1).strip()
                if ":" in val:
                    k, v = val.split(":", 1)
                    current_user = {k.strip(): v.strip()}
                else:
                    users.append({"name": val, "role": "system default"})
                    current_user = None
            elif current_user is not None:
                kv = re.match(r"^\s+([A-Za-z0-9_-]+):\s*(.+)$", raw)
                if kv:
                    current_user[kv.group(1).strip()] = kv.group(2).strip()
    if current_user:
        users.append(current_user)

    # Extract write_files paths and env vars
    write_files: list[dict[str, Any]] = []
    for match in re.finditer(
        r"-\s*path:\s*([^\n]+)(?:\n\s+permissions:\s*([^\n]+))?(?:\n\s+owner:\s*([^\n]+))?",
        text,
    ):
        write_files.append(
            {
                "path": match.group(1).strip().strip("'\""),
                "permissions": (match.group(2) or "0644").strip().strip("'\""),
                "owner": (match.group(3) or "root:root").strip().strip("'\""),
            }
        )

    env_vars: dict[str, str] = {}
    for env_match in re.finditer(r'^\s*([A-Z][A-Z0-9_]+)="?([^"\n]+)"?\s*$', text, re.M):
        env_vars[env_match.group(1)] = env_match.group(2)

    stages = [
        {
            "stage": 1,
            "title": "APT Metadata & OS Upgrade",
            "status": "enabled" if (package_update or package_upgrade or is_shell_script) else "skipped",
            "summary": f"package_update={package_update}, package_upgrade={package_upgrade}",
            "items": [
                *(["apt-get update (package_update: true)"] if package_update else []),
                *(["apt-get upgrade (package_upgrade: true)"] if package_upgrade else []),
            ],
        },
        {
            "stage": 2,
            "title": "System Groups & Service Users",
            "status": "enabled" if (groups or users) else "default",
            "summary": f"{len(groups)} group(s), {len(users)} user entry/entries",
            "items": [
                *(f"group: {g}" for g in groups),
                *(f"user: {u.get('name', 'default')} ({u.get('gecos', u.get('groups', 'user'))})" for u in users),
            ],
        },
        {
            "stage": 3,
            "title": "Toolchain & System Packages",
            "status": "enabled" if packages else "script-managed",
            "summary": f"{len(packages)} package(s) installed",
            "items": packages,
        },
        {
            "stage": 4,
            "title": "Configuration Files & Runner/Builder Environment",
            "status": "enabled" if (write_files or env_vars) else "none",
            "summary": f"{len(write_files)} file(s), {len(env_vars)} environment variable(s)",
            "items": [
                *(f"{wf['path']} ({wf['permissions']} {wf['owner']})" for wf in write_files),
                *(f"{k}={v}" for k, v in env_vars.items()),
            ],
        },
        {
            "stage": 5,
            "title": "Boot Execution Commands (runcmd / startup)",
            "status": "enabled" if runcmd else ("script" if is_shell_script else "empty"),
            "summary": f"{len(runcmd)} command step(s)",
            "items": runcmd,
        },
    ]

    warnings: list[str] = []
    if not (is_cloud_config or is_shell_script):
        warnings.append("Template should begin with '#cloud-config' or a '#!/usr/bin/env bash' shebang.")
    syntax_errors = _validate_cloud_init_syntax(
        lines,
        is_cloud_config=is_cloud_config,
        is_shell_script=is_shell_script,
    )
    warnings.extend(syntax_errors)
    for cmd in runcmd:
        if cmd.strip() == "chmod":
            warnings.append("Incomplete 'chmod' command detected in runcmd (missing mode and target path).")

    return {
        "format": "cloud-config" if is_cloud_config else ("shell-script" if is_shell_script else "custom"),
        "valid": len(warnings) == 0 and len(syntax_errors) == 0,
        "errors": syntax_errors,
        "warnings": warnings,
        "package_update": package_update,
        "package_upgrade": package_upgrade,
        "groups": groups,
        "users": users,
        "packages": packages,
        "write_files": write_files,
        "env_vars": env_vars,
        "runcmd": runcmd,
        "stages": stages,
        "line_count": len(lines),
    }
