"""Core data models, CPU benchmark catalog, region filtering, instance qualification,
VM templates, and Cloud-Init parser/visualizer for the Web UI Dashboard.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
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
      RUNNER_CPU_LIMIT="4"
      RUNNER_MEMORY_GB="16"
      HOST_RESERVE_GIB="0"
      DISK_DEVICE="/dev/disk/by-id/google-runner-data"
      DISK_MOUNT="/mnt/runner-data"

runcmd:
  # 1. Mount persistent runner data disk at /mnt/runner-data and relocate Docker/runner state
  - 'bash -lc ''set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && dev="${DISK_DEVICE:-/dev/disk/by-id/google-runner-data}" && mp="${DISK_MOUNT:-/mnt/runner-data}" && if [[ -b "$dev" ]]; then (blkid "$dev" >/dev/null 2>&1 || mkfs.ext4 -F "$dev") && mkdir -p "$mp" && uuid="$(blkid -s UUID -o value "$dev")" && (grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $mp ext4 defaults,nofail 0 2" >> /etc/fstab) && (mountpoint -q "$mp" || mount "$mp") && mountpoint -q "$mp"; else mkdir -p "$mp"; fi && mkdir -p "$mp/docker" "$mp/containerd" "$mp/instances" "$mp/work"'''
  # 2. Install Official Docker Engine & Buildx (select Debian vs Ubuntu repository from /etc/os-release)
  - install -m 0755 -d /etc/apt/keyrings
  - 'curl -fsSL "https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}")/gpg" -o /etc/apt/keyrings/docker.asc'
  - chmod a+r /etc/apt/keyrings/docker.asc
  - 'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}") $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list'
  - apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  - 'bash -lc ''set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && mp="${DISK_MOUNT:-/mnt/runner-data}" && systemctl stop docker containerd || true && cp -a /var/lib/containerd/. "$mp/containerd/" && install -d -m 0755 /etc/docker /etc/systemd/system/containerd.service.d && printf "{\\"data-root\\":\\"%s/docker\\",\\"log-driver\\":\\"local\\",\\"log-opts\\":{\\"max-size\\":\\"20m\\",\\"max-file\\":\\"3\\"}}\\n" "$mp" > /etc/docker/daemon.json && printf "[Service]\\nExecStart=\\nExecStart=/usr/bin/containerd --root %s/containerd --state /run/containerd\\n" "$mp" > /etc/systemd/system/containerd.service.d/runner-data.conf && systemctl daemon-reload && systemctl enable --now containerd docker'''
  # 3. Download, Configure & Start Persistent GitHub Actions Runner Slots with live GCE region & machine-family labels
  - curl -fsSL https://github.com/actions/runner/releases/download/v2.322.0/actions-runner-linux-x64-2.322.0.tar.gz -o /tmp/actions-runner.tar.gz
  - bash -lc 'set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && mp="${DISK_MOUNT:-/mnt/runner-data}" && host_cpus="$(nproc 2>/dev/null || echo 16)" && mem_kib="$(grep -m1 "^MemTotal:" /proc/meminfo 2>/dev/null | tr -dc "0-9" || true)" && mem_kib="${mem_kib:-67108864}" && host_mem_gib="$(((mem_kib+mem_kib/48+524288)/2097152*2))" && usable_mem_gib="$((host_mem_gib>${HOST_RESERVE_GIB:-0}?host_mem_gib-${HOST_RESERVE_GIB:-0}:host_mem_gib))" && max_by_cpu="$((host_cpus/${RUNNER_CPU_LIMIT:-4}))" && max_by_mem="$((usable_mem_gib/${RUNNER_MEMORY_GB:-16}))" && fit_slots="$((max_by_cpu<max_by_mem?max_by_cpu:max_by_mem))" && fit_slots="$((fit_slots<1?1:fit_slots))" && RUNNERS_COUNT="$((${RUNNERS_COUNT:-4}>fit_slots?fit_slots:${RUNNERS_COUNT:-4}))" && cpu_quota="$(( ${RUNNER_CPU_LIMIT:-4} * 100 ))%" && reserve_mib="$((${HOST_RESERVE_GIB:-0}>1?${HOST_RESERVE_GIB:-0}*1024:1024))" && usable_mem_mib="$((mem_kib/1024>reserve_mib?mem_kib/1024-reserve_mib:mem_kib/1024))" && slot_mem_mib="$((usable_mem_mib/${RUNNERS_COUNT:-4}))" && req_mem_mib="$((${RUNNER_MEMORY_GB:-16}*1024))" && mem_max="${RUNNER_MEMORY_GB:-16}G" && if (( slot_mem_mib > 0 && slot_mem_mib < req_mem_mib )); then mem_max="${slot_mem_mib}M"; fi && vm_zone="$(basename "$(curl -fsSL -H Metadata-Flavor:Google http://metadata.google.internal/computeMetadata/v1/instance/zone 2>/dev/null || true)")" && vm_region="${vm_zone%-*}" && vm_mtype="$(basename "$(curl -fsSL -H Metadata-Flavor:Google http://metadata.google.internal/computeMetadata/v1/instance/machine-type 2>/dev/null || true)")" && vm_family="${vm_mtype%%-*}" && eff_labels="${RUNNER_LABELS}" && [[ -z "${vm_region}" ]] || eff_labels="${eff_labels},gcp-${vm_region}" && [[ -z "${vm_family}" ]] || eff_labels="${eff_labels},${vm_family}" && for slot in $(seq 1 "${RUNNERS_COUNT:-4}"); do state_dir="${mp}/instances/actions-runner-${slot}"; work_dir="${mp}/work/runner-${slot}"; dir="/opt/actions-runner-${slot}"; install -d -m 0755 -o runner -g runner "${state_dir}" "${work_dir}"; ln -sfn "${state_dir}" "${dir}"; if [[ ! -x "${state_dir}/run.sh" ]]; then tar -xzf /tmp/actions-runner.tar.gz -C "${state_dir}"; fi; chown -R runner:runner "${state_dir}" "${work_dir}"; (cd "${state_dir}" && if [[ ! -f .runner ]]; then runuser -u runner -- ./config.sh --unattended --replace --name "$(hostname -s)-runner-${slot}" --url "${GITHUB_ORG_URL}" --token "${GITHUB_RUNNER_TOKEN}" --runnergroup "${RUNNER_GROUP}" --labels "${eff_labels}" --work "${work_dir}"; fi && rm -f .service && ./svc.sh install runner && ./svc.sh start && unit_name="$(tr -d "[:space:]" < .service)" && install -d -m 0755 "/etc/systemd/system/${unit_name}.d" && printf "[Service]\\nCPUAccounting=yes\\nMemoryAccounting=yes\\nCPUQuota=%s\\nMemoryMax=%s\\nMemorySwapMax=0\\n" "${cpu_quota}" "${mem_max}" > "/etc/systemd/system/${unit_name}.d/runner-limits.conf" && systemctl daemon-reload && systemctl restart "${unit_name}"); done && rm -f /tmp/actions-runner.tar.gz'
"""

DEFAULT_RUNNER_STRICT_CLOUD_INIT = """#cloud-config
package_update: true
package_upgrade: true

users:
  - default
  - name: runner
    gecos: Heavy Pool GitHub Actions Runner (Rust 1.99)
    sudo: false
    groups: users
    shell: /bin/bash
    lock_passwd: true

packages:
  - ca-certificates
  - curl
  - gnupg
  - jq
  - git
  - python3
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
      HOST_RESERVE_GIB="8"
      ADMISSION_WORK_GIB="56"
      STRICT_HEAVY_POOL="1"
      DISK_DEVICE="/dev/disk/by-id/google-runner-data"
      DISK_MOUNT="/mnt/runner-data"
  - path: /opt/gha-runners/host_admission.py
    permissions: '0755'
    owner: root:root
    content: |
      #!/usr/bin/env python3
      import concurrent.futures, fcntl, json, os, re, socket, struct, subprocess, sys, threading, time
      from pathlib import Path

      CONFIG_PATH = Path('/etc/gha-admission.json')
      LEASES_PATH = Path('/var/lib/gha-admission/leases.json')
      SOCKET_PATH = Path('/run/gha-admission/socket')
      INFLIGHT_LOCK = threading.Lock()
      INFLIGHT_BY_CID = {}
      RESERVED_PEERS = {}
      MAX_INFLIGHT_PER_CONTAINER = 2
      MAX_TOTAL_INFLIGHT = 32

      class OrphanRunnerHook(ValueError):
          # The requesting hook has outlived its GitHub-assigned Worker.
          pass

      def process_record(pid):
          root = Path(f'/proc/{pid}')
          stat = (root / 'stat').read_text().rsplit(')', 1)[1].split()
          if stat[0] == 'Z':
              raise FileNotFoundError('Zombie process')
          return int(stat[1]), stat[19], (root / 'comm').read_text().strip(), (root / 'cgroup').read_text()

      def runner_cgroup(cgroup):
          ids = re.findall(r'(?:docker[-/])([a-f0-9]{64})(?:\\.scope)?', cgroup)
          return next(iter(set(ids))) if len(set(ids)) == 1 else None

      def docker_identity(container):
          row = json.loads(
              subprocess.run(
                  ['/usr/bin/docker', 'inspect', container],
                  check=True,
                  capture_output=True,
                  text=True,
                  timeout=10,
              ).stdout
          )[0]
          if not row['State']['Running']:
              raise ValueError('Container is not running')
          return f"container:{row['Id']}:{row['State']['StartedAt']}", row

      def worker_owner(pid, container_id, container_started):
          seen = set()
          for _ in range(32):
              if pid <= 1 or pid in seen:
                  break
              seen.add(pid)
              try:
                  parent, start, comm, cgroup = process_record(pid)
              except FileNotFoundError as exc:
                  raise OrphanRunnerHook('Runner hook lost its Worker ancestry') from exc
              if runner_cgroup(cgroup) != container_id:
                  raise OrphanRunnerHook('Runner hook left its container')
              if comm == 'Runner.Worker':
                  return f'runner:{container_id}:{container_started}:{pid}:{start}'
              pid = parent
          raise OrphanRunnerHook('Runner hook has no live Worker ancestor')

      def peer_owner(pid, uid, cfg):
          if uid != 1001:
              raise ValueError('Unauthorized admission peer')
          cgroup = Path(f'/proc/{pid}/cgroup').read_text()
          cid = runner_cgroup(cgroup)
          if cid is None:
              raise ValueError('Runner must belong to a Docker cgroup')
          _, row = docker_identity(cid)
          if row['Name'].lstrip('/') not in cfg.get('runner_names', []):
              raise ValueError('Container is outside this runner deployment')
          return worker_owner(pid, cid, row['State']['StartedAt'])

      def container_job_active(cid, started, worker_start=None):
          procs = {}
          for entry in Path('/proc').iterdir():
              if not entry.name.isdigit():
                  continue
              p = int(entry.name)
              try:
                  ppid, pstart, comm, cgrp = process_record(p)
              except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
                  continue
              if runner_cgroup(cgrp) == cid:
                  procs[p] = (ppid, int(pstart), comm)
          if not procs:
              return False
          if any(comm == 'Runner.Worker' for _, (_, _, comm) in procs.items()):
              return False
          max_idle_start = int(worker_start) if worker_start is not None else None
          listeners = [
              (pstart, p)
              for p, (_, pstart, comm) in procs.items()
              if comm == 'Runner.Listener' and (max_idle_start is None or pstart <= max_idle_start)
          ]
          idle_pids = set()
          if listeners:
              _, lp = min(listeners)
              cur = lp
              while cur in procs and cur not in idle_pids:
                  ppid, pstart, _ = procs[cur]
                  if max_idle_start is not None and pstart > max_idle_start:
                      break
                  idle_pids.add(cur)
                  cur = ppid
          stray = [p for p in procs if p not in idle_pids]
          if not stray:
              return False
          for p in stray:
              try:
                  os.kill(p, 9)
              except OSError:
                  pass
          return True

      def worker_alive(owner):
          try:
              match = re.fullmatch(r'runner:([a-f0-9]{64}):(.+):([0-9]+):([0-9]+)', owner)
              if match is None:
                  return False
              cid, started, pid, start = match.groups()
              try:
                  _, actual_start, comm, cgroup = process_record(int(pid))
              except FileNotFoundError:
                  return False
              if comm != 'Runner.Worker' or actual_start != start or runner_cgroup(cgroup) != cid:
                  return False
              actual, _ = docker_identity(cid)
              return actual == f'container:{cid}:{started}'
          except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
              return True

      def owner_alive(owner):
          if worker_alive(owner):
              return True
          try:
              match = re.fullmatch(r'runner:([a-f0-9]{64}):(.+):([0-9]+):([0-9]+)', owner)
              if match is None:
                  return False
              cid, started, _, start = match.groups()
              return container_job_active(cid, started, start)
          except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
              return True

      def runner_reservation(owner, cfg):
          match = re.fullmatch(r'runner:([a-f0-9]{64}):(.+):([0-9]+):([0-9]+)', owner)
          if match is None:
              raise ValueError('Invalid Worker lease identity')
          cid, started, _, _ = match.groups()
          actual, row = docker_identity(cid)
          if actual != f'container:{cid}:{started}' or row['Id'] != cid:
              raise ValueError('Runner identity changed before admission')
          if row['Name'].lstrip('/') not in cfg.get('runner_names', []):
              raise ValueError('Runner left this deployment')
          if not worker_alive(owner):
              raise OrphanRunnerHook('Runner Worker exited before grant')
          runner_gib = int(cfg.get('runner_gib', 32))
          caps = row.get('HostConfig') or {}
          expected = runner_gib * (1024 ** 3)
          if caps.get('Memory') != expected or caps.get('MemorySwap') != expected:
              raise ValueError('Runner memory cap differs from reservation')
          return runner_gib

      def host_work_budget_gib(cfg):
          configured_work = int(cfg.get('work_gib', 56))
          reserve_gib = max(4, int(cfg.get('host_reserve_gib', 8)))
          try:
              for line in Path('/proc/meminfo').read_text().splitlines():
                  if line.startswith('MemTotal:'):
                      mem_kib = int(line.split()[1])
                      phys_gib = mem_kib // (1024 * 1024)
                      return max(0, min(configured_work, phys_gib - reserve_gib))
          except (OSError, ValueError, IndexError):
              pass
          return configured_work

      def reap_dead_leases():
          lock_fd = os.open(str(LEASES_PATH) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
          with os.fdopen(lock_fd, 'r+') as lock_f:
              fcntl.flock(lock_f, fcntl.LOCK_EX)
              rows = json.loads(LEASES_PATH.read_text()) if LEASES_PATH.exists() else {}
              leases = {k: v for k, v in rows.items() if owner_alive(k)}
              if leases != rows:
                  tmp_path = LEASES_PATH.with_suffix('.tmp')
                  tmp_fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                  with os.fdopen(tmp_fd, 'w') as out_f:
                      json.dump(leases, out_f, sort_keys=True)
                      out_f.flush()
                      os.fsync(out_f.fileno())
                  tmp_path.replace(LEASES_PATH)

      def reserve_inflight(conn):
          conn.settimeout(5.0)
          pid, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
          if uid != 1001:
              raise ValueError('Unauthorized admission peer')
          cid_slot = runner_cgroup(Path(f'/proc/{pid}/cgroup').read_text())
          if cid_slot is None:
              raise ValueError('Runner must belong to a Docker cgroup')
          with INFLIGHT_LOCK:
              if sum(INFLIGHT_BY_CID.values()) >= MAX_TOTAL_INFLIGHT:
                  raise RuntimeError('Too many concurrent admission requests')
              cur_inflight = INFLIGHT_BY_CID.get(cid_slot, 0)
              if cur_inflight >= MAX_INFLIGHT_PER_CONTAINER:
                  raise RuntimeError('Too many concurrent admission requests from container')
              INFLIGHT_BY_CID[cid_slot] = cur_inflight + 1
              RESERVED_PEERS[id(conn)] = (pid, uid, cid_slot)
          return pid, uid, cid_slot

      def release_inflight(cid_slot):
          if cid_slot is not None:
              with INFLIGHT_LOCK:
                  rem = INFLIGHT_BY_CID.get(cid_slot, 1) - 1
                  if rem <= 0:
                      INFLIGHT_BY_CID.pop(cid_slot, None)
                  else:
                      INFLIGHT_BY_CID[cid_slot] = rem

      def handle_client(conn):
          cid_slot = None
          with conn:
              try:
                  with INFLIGHT_LOCK:
                      reserved = RESERVED_PEERS.pop(id(conn), None)
                  if reserved is None:
                      pid, uid, cid_slot = reserve_inflight(conn)
                      with INFLIGHT_LOCK:
                          RESERVED_PEERS.pop(id(conn), None)
                  else:
                      pid, uid, cid_slot = reserved
                      conn.settimeout(5.0)
                  cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
                  owner = peer_owner(pid, uid, cfg)
                  raw = conn.recv(4096).decode('utf-8', errors='replace').strip()
                  req = json.loads(raw)
                  if req.get('kind', 'runner') != 'runner':
                      raise ValueError('Admission kind does not match peer identity')
                  action = req.get('action') or req.get('op')
                  work_gib = host_work_budget_gib(cfg)
                  lock_fd = os.open(str(LEASES_PATH) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                  with os.fdopen(lock_fd, 'r+') as lock_f:
                      fcntl.flock(lock_f, fcntl.LOCK_EX)
                      rows = json.loads(LEASES_PATH.read_text()) if LEASES_PATH.exists() else {}
                      leases = {k: v for k, v in rows.items() if owner_alive(k)}
                      if action == 'release':
                          if owner_alive(owner):
                              raise ValueError('Active Worker lease cannot be released while Runner.Worker is alive')
                          leases.pop(owner, None)
                          accepted = True
                      elif action == 'acquire':
                          runner_gib = runner_reservation(owner, cfg)
                          if owner in leases or sum(leases.values()) + runner_gib <= work_gib:
                              leases[owner] = runner_gib
                              accepted = True
                              if not worker_alive(owner):
                                  leases.pop(owner, None)
                                  accepted = False
                          else:
                              accepted = False
                      else:
                          raise ValueError('Unknown admission action')
                      tmp_path = LEASES_PATH.with_suffix('.tmp')
                      tmp_fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                      with os.fdopen(tmp_fd, 'w') as out_f:
                          json.dump(leases, out_f, sort_keys=True)
                          out_f.flush()
                          os.fsync(out_f.fileno())
                      tmp_path.replace(LEASES_PATH)
                  answer = {'ok': accepted, 'accepted': accepted}
              except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
                  answer = {'ok': False, 'accepted': False, 'error': str(exc)[:200]}
                  if isinstance(exc, OrphanRunnerHook):
                      answer['code'] = 'orphaned-runner-hook'
                      answer['error'] = 'ORPHAN_RUNNER_HOOK: ' + str(exc)[:180]
              finally:
                  release_inflight(cid_slot)
              try:
                  conn.sendall((json.dumps(answer) + '\\n').encode('utf-8'))
              except OSError:
                  pass

      def serve():
          if os.geteuid() != 0:
              raise RuntimeError('Admission service must run as root')
          SOCKET_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
          LEASES_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
          if not LEASES_PATH.exists():
              LEASES_PATH.write_text('{}\\n')
          SOCKET_PATH.unlink(missing_ok=True)
          with (
              socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv,
              concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool,
          ):
              srv.bind(str(SOCKET_PATH))
              os.chown(SOCKET_PATH, 0, 1001)
              os.chmod(SOCKET_PATH, 0o660)
              srv.listen(128)
              srv.settimeout(1.0)
              while True:
                  try:
                      conn, _ = srv.accept()
                  except socket.timeout:
                      try:
                          reap_dead_leases()
                      except Exception:
                          pass
                      continue
                  try:
                      _, _, cid_slot = reserve_inflight(conn)
                  except (OSError, ValueError, RuntimeError) as exc:
                      with conn:
                          try:
                              conn.sendall(
                                  (json.dumps({'ok': False, 'accepted': False, 'error': str(exc)[:200]}) + '\\n').encode('utf-8')
                              )
                          except OSError:
                              pass
                      continue
                  try:
                      pool.submit(handle_client, conn)
                  except Exception:
                      with INFLIGHT_LOCK:
                          RESERVED_PEERS.pop(id(conn), None)
                      release_inflight(cid_slot)
                      conn.close()

      def request(action='acquire'):
          with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
              sock.settimeout(30.0)
              sock.connect(str(SOCKET_PATH))
              sock.sendall((json.dumps({'action': action, 'kind': 'runner'}) + '\\n').encode('utf-8'))
              sock.shutdown(socket.SHUT_WR)
              raw = sock.recv(4096).decode('utf-8', errors='replace').strip()
          resp = json.loads(raw)
          if isinstance(resp, dict) and resp.get('code') == 'orphaned-runner-hook':
              raise OrphanRunnerHook(resp.get('error', 'Runner hook lost its Worker ancestry'))
          if 'error' in resp:
              raise RuntimeError(resp['error'])
          return bool(resp.get('accepted'))

      def write_config(runners_count, work_gib, runner_gib, host_reserve_gib=8):
          host = socket.gethostname().split('.')[0]
          names = [f'{host}-runner-{i}' for i in range(1, int(runners_count) + 1)]
          CONFIG_PATH.write_text(
              json.dumps(
                  {
                      'role': 'runner',
                      'work_gib': int(work_gib),
                      'runner_gib': int(runner_gib),
                      'host_reserve_gib': int(host_reserve_gib),
                      'runner_names': names,
                      'heavy_runners': names,
                  },
                  indent=2,
                  sort_keys=True,
              )
              + '\\n'
          )
          os.chmod(CONFIG_PATH, 0o644)

      if __name__ == '__main__':
          if sys.argv[1:] == ['serve']:
              serve()
          elif sys.argv[1:2] == ['write-config']:
              reserve_arg = sys.argv[5] if len(sys.argv) > 5 else 8
              write_config(sys.argv[2], sys.argv[3], sys.argv[4], reserve_arg)
          elif sys.argv[1:] == ['acquire']:
              while True:
                  try:
                      if request('acquire'):
                          break
                  except OrphanRunnerHook:
                      raise
                  except (OSError, RuntimeError):
                      # Retry when the root admission service restarts or rejects concurrent requests during an assigned job.
                      time.sleep(5)
                  else:
                      time.sleep(5)
          elif sys.argv[1:] == ['release']:
              while True:
                  try:
                      if request('release'):
                          break
                  except OrphanRunnerHook:
                      raise SystemExit(75)
                  except (OSError, RuntimeError, ValueError):
                      # Retry until the root daemon acknowledges release or reports an orphaned hook.
                      time.sleep(5)
  - path: /opt/gha-runners/build/entrypoint.sh
    permissions: '0755'
    owner: root:root
    content: |
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
  - path: /opt/gha-runners/build/job-started.sh
    permissions: '0755'
    owner: root:root
    content: |
      #!/usr/bin/env bash
      set -Eeuo pipefail
      if [[ "${GHA_ADMISSION_REQUIRED:-0}" == 1 ]]; then
        exec python3 /usr/local/bin/gha-host-admission.py acquire
      fi
  - path: /opt/gha-runners/build/job-completed.sh
    permissions: '0755'
    owner: root:root
    content: |
      #!/usr/bin/env bash
      set -Eeuo pipefail
      # Terminate any background processes spawned by the job outside the live
      # Runner.Listener / Runner.Worker / hook lineage before Runner.Worker exits.
      python3 - <<'PY' || true
      import os
      from pathlib import Path
      keep = set()
      cur = os.getpid()
      while cur > 1 and cur not in keep:
          keep.add(cur)
          try:
              stat = Path(f'/proc/{cur}/stat').read_text().rsplit(')', 1)[1].split()
              cur = int(stat[1])
          except Exception:
              break
      for entry in Path('/proc').iterdir():
          if entry.name.isdigit() and int(entry.name) not in keep:
              try:
                  os.kill(int(entry.name), 9)
              except OSError:
                  pass
      PY
      exit 0
  - path: /opt/gha-runners/build/Dockerfile
    permissions: '0644'
    owner: root:root
    content: |
      FROM ghcr.io/actions/actions-runner:2.337.0
      ARG DEBIAN_FRONTEND=noninteractive
      USER root
      RUN apt-get update && apt-get install -y --no-install-recommends build-essential clang cmake curl git jq libssl-dev make pkg-config python3 ca-certificates unzip zip xz-utils zstd && rm -rf /var/lib/apt/lists/* && rm -f /etc/sudoers /etc/sudoers.d/* && install -d -o runner -g runner /runner-state /work /opt/runner-rust/cargo /opt/runner-rust/rustup
      COPY entrypoint.sh /usr/local/bin/gha-runner-entrypoint
      COPY job-started.sh /usr/local/bin/gha-runner-job-started.sh
      COPY job-completed.sh /usr/local/bin/gha-runner-job-completed.sh
      COPY host_admission.py /usr/local/bin/gha-host-admission.py
      RUN chmod 0755 /usr/local/bin/gha-runner-entrypoint /usr/local/bin/gha-runner-job-started.sh /usr/local/bin/gha-runner-job-completed.sh /usr/local/bin/gha-host-admission.py
      USER runner
      ENV CARGO_HOME=/opt/runner-rust/cargo
      ENV RUSTUP_HOME=/opt/runner-rust/rustup
      ENV PATH=/opt/runner-rust/cargo/bin:${PATH}
      RUN curl --proto '=https' --tlsv1.2 -fsS https://sh.rustup.rs -o /tmp/rustup-init.sh && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.99.0 && rm -f /tmp/rustup-init.sh && rustup component add rustfmt clippy --toolchain 1.99.0 && rustup target add --toolchain 1.99.0 x86_64-unknown-linux-musl aarch64-unknown-linux-musl aarch64-apple-darwin x86_64-apple-darwin && rustc --version | grep "^rustc 1.99.0 " && cargo --version | grep "^cargo 1.99.0 "
      USER root
      RUN chown -R root:root /opt/runner-rust && chmod -R a-w /opt/runner-rust
      USER 1001:1001
      ENV CARGO_HOME=/work/.cargo
      ENV PATH=/opt/runner-rust/cargo/bin:/work/.cargo/bin:${PATH}
      ENV RUNNER_STATE=/runner-state
      ENV ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/gha-runner-job-started.sh
      ENV ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/bin/gha-runner-job-completed.sh
      WORKDIR /work
      ENTRYPOINT ["/usr/local/bin/gha-runner-entrypoint"]

runcmd:
  - 'bash -lc ''set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && dev="${DISK_DEVICE:-/dev/disk/by-id/google-runner-data}" && mp="${DISK_MOUNT:-/mnt/runner-data}" && if [[ ! -b "$dev" ]]; then echo "Missing persistent runner disk $dev" >&2; exit 1; fi && (blkid "$dev" >/dev/null 2>&1 || mkfs.ext4 -F "$dev") && mkdir -p "$mp" && uuid="$(blkid -s UUID -o value "$dev")" && (grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $mp ext4 defaults,nofail 0 2" >> /etc/fstab) && (mountpoint -q "$mp" || mount "$mp") && mountpoint -q "$mp" && mkdir -p "$mp/docker" "$mp/containerd" "$mp/instances" "$mp/work"'''
  - install -m 0755 -d /etc/apt/keyrings
  - 'curl -fsSL "https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}")/gpg" -o /etc/apt/keyrings/docker.asc'
  - chmod a+r /etc/apt/keyrings/docker.asc
  - 'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$(. /etc/os-release && echo "${ID:-debian}") $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list'
  - apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  - 'bash -lc ''set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && mp="${DISK_MOUNT:-/mnt/runner-data}" && systemctl stop docker containerd || true && rm -rf "$mp/docker/containers"/* && cp -a /var/lib/containerd/. "$mp/containerd/" && install -d -m 0755 /etc/docker /etc/systemd/system/containerd.service.d && printf "{\\"data-root\\":\\"%s/docker\\",\\"log-driver\\":\\"local\\",\\"log-opts\\":{\\"max-size\\":\\"20m\\",\\"max-file\\":\\"3\\"}}\\n" "$mp" > /etc/docker/daemon.json && printf "[Service]\\nExecStart=\\nExecStart=/usr/bin/containerd --root %s/containerd --state /run/containerd\\n" "$mp" > /etc/systemd/system/containerd.service.d/runner-data.conf && systemctl daemon-reload && systemctl enable --now containerd docker && test "$(docker info --format=json | jq -r .DockerRootDir)" = "$mp/docker" && old_cids="$(docker ps -aq)" && if [[ -n "$old_cids" ]]; then docker rm -f $old_cids >/dev/null 2>&1 || true; fi'''
  - 'bash -lc ''set -euo pipefail && install -d -m 0755 -o runner -g runner /opt/runner-rust/cargo /opt/runner-rust/rustup && curl --proto "=https" --tlsv1.2 -fsS https://sh.rustup.rs -o /tmp/rustup-init.sh && CARGO_HOME=/opt/runner-rust/cargo RUSTUP_HOME=/opt/runner-rust/rustup runuser -u runner -- sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.99.0 && rm -f /tmp/rustup-init.sh && CARGO_HOME=/opt/runner-rust/cargo RUSTUP_HOME=/opt/runner-rust/rustup PATH="/opt/runner-rust/cargo/bin:$PATH" runuser -u runner -- rustup component add rustfmt clippy --toolchain 1.99.0 && CARGO_HOME=/opt/runner-rust/cargo RUSTUP_HOME=/opt/runner-rust/rustup PATH="/opt/runner-rust/cargo/bin:$PATH" runuser -u runner -- rustup target add --toolchain 1.99.0 x86_64-unknown-linux-musl aarch64-unknown-linux-musl aarch64-apple-darwin x86_64-apple-darwin && chown -R root:root /opt/runner-rust && chmod -R a-w /opt/runner-rust && ln -sfn /opt/runner-rust/cargo/bin/* /usr/local/bin/ && rustc --version | grep "^rustc 1.99.0 " && cargo --version | grep "^cargo 1.99.0 "'''
  - 'bash -lc ''set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && host_cpus="$(nproc 2>/dev/null || echo 16)" && mem_kib="$(grep -m1 "^MemTotal:" /proc/meminfo 2>/dev/null | tr -dc "0-9" || true)" && mem_kib="${mem_kib:-67108864}" && host_mem_gib="$(((mem_kib + mem_kib/48 + 524288) / 2097152 * 2))" && usable_mem_gib="$((host_mem_gib>${HOST_RESERVE_GIB:-8}?host_mem_gib-${HOST_RESERVE_GIB:-8}:host_mem_gib))" && max_by_cpu="$((host_cpus/${RUNNER_CPU_LIMIT:-8}))" && max_by_mem="$((usable_mem_gib/${RUNNER_MEMORY_GB:-32}))" && fit_slots="$((max_by_cpu<max_by_mem?max_by_cpu:max_by_mem))" && fit_slots="$((fit_slots<1?1:fit_slots))" && RUNNERS_COUNT="$((${RUNNERS_COUNT:-4}>fit_slots?fit_slots:${RUNNERS_COUNT:-4}))" && install -d -m 0755 /run/gha-admission /var/lib/gha-admission && cp /opt/gha-runners/host_admission.py /opt/gha-runners/build/host_admission.py && chmod 0755 /opt/gha-runners/build/entrypoint.sh /opt/gha-runners/build/job-started.sh /opt/gha-runners/build/job-completed.sh /opt/gha-runners/build/host_admission.py && /usr/bin/python3 /opt/gha-runners/host_admission.py write-config "${RUNNERS_COUNT:-4}" "${ADMISSION_WORK_GIB:-56}" "${RUNNER_MEMORY_GB:-32}" "${HOST_RESERVE_GIB:-8}" && printf "[Unit]\\nDescription=Strict runner memory admission\\nAfter=docker.service\\nRequires=docker.service\\n[Service]\\nExecStart=/usr/bin/python3 /opt/gha-runners/host_admission.py serve\\nRestart=always\\nRestartSec=5\\n[Install]\\nWantedBy=multi-user.target\\n" > /etc/systemd/system/gha-admission.service && systemctl daemon-reload && systemctl enable --now gha-admission && docker build -t oxidex-self-hosted-runner:2.337.0 /opt/gha-runners/build'''
  - bash -lc 'set -euo pipefail && set -a && . /etc/runner-setup.env && set +a && host_cpus="$(nproc 2>/dev/null || echo 16)" && mem_kib="$(grep -m1 "^MemTotal:" /proc/meminfo 2>/dev/null | tr -dc "0-9" || true)" && mem_kib="${mem_kib:-67108864}" && host_mem_gib="$(((mem_kib + mem_kib/48 + 524288) / 2097152 * 2))" && usable_mem_gib="$((host_mem_gib>${HOST_RESERVE_GIB:-8}?host_mem_gib-${HOST_RESERVE_GIB:-8}:host_mem_gib))" && max_by_cpu="$((host_cpus/${RUNNER_CPU_LIMIT:-8}))" && max_by_mem="$((usable_mem_gib/${RUNNER_MEMORY_GB:-32}))" && fit_slots="$((max_by_cpu<max_by_mem?max_by_cpu:max_by_mem))" && fit_slots="$((fit_slots<1?1:fit_slots))" && RUNNERS_COUNT="$((${RUNNERS_COUNT:-4}>fit_slots?fit_slots:${RUNNERS_COUNT:-4}))" && gpasswd -d runner docker >/dev/null 2>&1 || true && gpasswd -d runner sudo >/dev/null 2>&1 || true && gpasswd -d runner admin >/dev/null 2>&1 || true && rm -f /etc/sudoers.d/runner /etc/sudoers.d/90-cloud-init-users && old_cids="$(docker ps -aq)" && if [[ -n "$old_cids" ]]; then docker rm -f $old_cids >/dev/null 2>&1 || true; fi && mp="${DISK_MOUNT:-/mnt/runner-data}" && cpu_quota="$(( ${RUNNER_CPU_LIMIT:-8} * 100 ))%" && mem_max="${RUNNER_MEMORY_GB:-32}G" && for slot in $(seq 1 "${RUNNERS_COUNT:-4}"); do name="$(hostname -s)-runner-${slot}"; state_dir="${mp}/instances/actions-runner-${slot}"; work_dir="${mp}/work/runner-${slot}"; dir="/opt/actions-runner-${slot}"; install -d -m 0755 -o 1001 -g 1001 "${state_dir}" "${work_dir}"; ln -sfn "${state_dir}" "${dir}"; install -d -m 0755 "/etc/systemd/system/gha-runner-${slot}.service.d"; printf "[Service]\\nCPUAccounting=yes\\nMemoryAccounting=yes\\nCPUQuota=%s\\nMemoryMax=%s\\nMemorySwapMax=0\\nNoNewPrivileges=yes\\n" "${cpu_quota}" "${mem_max}" > "/etc/systemd/system/gha-runner-${slot}.service.d/heavy32-limits.conf"; env_file="/run/${name}.env"; umask 077; printf "GITHUB_RUNNER_TOKEN=%s\\n" "${GITHUB_RUNNER_TOKEN}" > "${env_file}"; docker run --rm --user 1001:1001 --cap-drop=ALL --security-opt=no-new-privileges:true --env-file "${env_file}" -e RUNNER_CONFIGURE_ONLY=1 -e RUNNER_NAME="${name}" -e RUNNER_LABELS="${RUNNER_LABELS}" -e RUNNER_GROUP_NAME="${RUNNER_GROUP}" -e GITHUB_RUNNER_URL="${GITHUB_ORG_URL}" -e RUNNER_STATE=/runner-state -v "${state_dir}:/runner-state" -v "${work_dir}:/work" oxidex-self-hosted-runner:2.337.0; rm -f "${env_file}"; docker rm -f "${name}" >/dev/null 2>&1 || true; docker run -d --restart unless-stopped --label oxidex.strict-runner=true --name "${name}" --user 1001:1001 --cap-drop=ALL --security-opt=no-new-privileges:true --cpu-shares=1024 --cpus="${RUNNER_CPU_LIMIT:-8}" --memory="${RUNNER_MEMORY_GB:-32}g" --memory-swap="${RUNNER_MEMORY_GB:-32}g" -e RUNNER_NAME="${name}" -e RUNNER_STATE=/runner-state -e GHA_ADMISSION_REQUIRED=1 -e ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/gha-runner-job-started.sh -e ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/bin/gha-runner-job-completed.sh -v /run/gha-admission:/run/gha-admission:ro -v "${state_dir}:/runner-state" -v "${work_dir}:/work" oxidex-self-hosted-runner:2.337.0; done'
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
    sudo: ["ALL=(root) NOPASSWD: /usr/local/bin/oxidex-remote-build, /usr/local/bin/oxidex-remote-transaction"]
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
  - iptables
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
      %oxidex-build ALL=(root) NOPASSWD: /usr/local/bin/oxidex-remote-build, /usr/local/bin/oxidex-remote-transaction
  - path: /etc/oxidex-remote-build.json
    permissions: '0644'
    owner: root:root
    content: |
      {
        "role": "builder",
        "cpus": 16,
        "memory_gib": 28,
        "image": "oxidex-remote-builder:1.99.0",
        "cache_mount": "/mnt/runner-data/remote-build",
        "service_account": "none",
        "scopes": []
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
      RUN groupadd -g 1002 runner && useradd -m -u 1002 -g 1002 -s /bin/bash runner && install -d -o 1002 -g 1002 /src /target /cargo
      COPY entrypoint.py /usr/local/bin/oxidex-build-entrypoint
      RUN chmod 0755 /usr/local/bin/oxidex-build-entrypoint
      USER 1002:1002
      ENV PATH=/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
      ENV CARGO_HOME=/cargo
      ENV CARGO_TARGET_DIR=/target
      WORKDIR /src
      ENTRYPOINT ["/usr/bin/python3", "/usr/local/bin/oxidex-build-entrypoint"]
  - path: /usr/local/bin/oxidex-remote-transaction
    permissions: '0755'
    owner: root:root
    content: |
      #!/usr/bin/env python3
      # Root-owned remote builder transaction wrapper; validates operations without allowing shell commands.
      import argparse, fcntl, gzip, hashlib, json, os, pwd, re, shutil, stat, subprocess, sys, tarfile
      from pathlib import Path

      MAX_UPLOAD_ARCHIVE_BYTES = 256 * 1024 * 1024
      MAX_SOURCE_ARCHIVE_MEMBERS = 50000
      MAX_SOURCE_MEMBER_BYTES = 128 * 1024 * 1024
      MAX_SOURCE_EXTRACTED_BYTES = 1024 * 1024 * 1024
      MAX_SOURCE_DECOMPRESSED_BYTES = MAX_SOURCE_EXTRACTED_BYTES + 16 * 1024 * 1024
      MAX_TAR_EXTENDED_HEADER_BYTES = 65536

      class BoundedGzipStream:
          def __init__(self, gz_stream, max_bytes):
              self._gz = gz_stream
              self._max_bytes = max_bytes

          def _check_pos(self, pos):
              if pos > self._max_bytes:
                  raise RuntimeError('Source archive uncompressed size exceeds limit')

          def read(self, size=-1):
              if size is None or size < 0 or size > MAX_SOURCE_MEMBER_BYTES:
                  raise RuntimeError('Source archive decompressed read exceeds limit')
              self._check_pos(self._gz.tell() + size)
              data = self._gz.read(size)
              self._check_pos(self._gz.tell())
              return data

          def tell(self):
              pos = self._gz.tell()
              self._check_pos(pos)
              return pos

          def seek(self, offset, whence=os.SEEK_SET):
              if whence == os.SEEK_SET and offset > self._max_bytes:
                  raise RuntimeError('Source archive uncompressed size exceeds limit')
              pos = self._gz.seek(offset, whence)
              self._check_pos(pos)
              return pos

      class SafeTarInfo(tarfile.TarInfo):
          def _proc_member(self, tarfile_obj):
              if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
                  if self.size < 0 or self.size > MAX_TAR_EXTENDED_HEADER_BYTES:
                      raise RuntimeError('Source archive extended header exceeds size limit')
              elif self.size < 0 or self.size > MAX_SOURCE_MEMBER_BYTES:
                  raise RuntimeError('Source archive member size exceeds limit')
              return super()._proc_member(tarfile_obj)

      def validate_identity(project, run, upload):
          if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', project):
              raise ValueError('Invalid project identifier')
          if not re.fullmatch(r'run-[0-9a-f]{32}', run):
              raise ValueError('Invalid run identifier')
          expected_name = f'oxidex-remote-source-{run}.tar.gz'
          if Path(upload).name != expected_name:
              raise ValueError('Invalid upload archive name')
          caller_uid = int(os.environ.get('SUDO_UID', '0'))
          caller_home = Path(pwd.getpwuid(caller_uid).pw_dir).resolve()
          upload_path = (caller_home / expected_name) if not Path(upload).is_absolute() else Path(upload)
          if upload_path.is_symlink() or upload_path.parent.resolve() != caller_home:
              raise ValueError('Upload archive must reside directly in caller home directory')
          return upload_path

      def verify_instance_id(instance_id):
          if not instance_id:
              return
          if not re.fullmatch(r'[0-9]+', str(instance_id)):
              raise ValueError('Invalid VM instance_id')
          actual = subprocess.check_output(
              ['curl', '--fail', '--silent', '--show-error', '--max-time', '10',
               '-H', 'Metadata-Flavor:Google', 'http://metadata.google.internal/computeMetadata/v1/instance/id'],
              text=True,
          ).strip()
          if actual != str(instance_id):
              raise SystemExit(75)

      def cleanup_source_fd(source_fd):
          device = os.fstat(source_fd).st_dev
          count = [0]

          def _empty(dir_fd, depth):
              if depth > 64:
                  raise RuntimeError('Source cleanup depth bound exceeded')
              for name in os.listdir(dir_fd):
                  count[0] += 1
                  if count[0] > 100000:
                      raise RuntimeError('Source cleanup entry bound exceeded')
                  info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                  if info.st_dev != device:
                      raise RuntimeError('Foreign filesystem inside source')
                  if stat.S_ISDIR(info.st_mode):
                      child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
                      try:
                          _empty(child_fd, depth + 1)
                      finally:
                          os.close(child_fd)
                      os.rmdir(name, dir_fd=dir_fd)
                  else:
                      os.unlink(name, dir_fd=dir_fd)

          _empty(source_fd, 0)
          os.fsync(source_fd)

      def open_nofollow_subdir(parent_fd, name, device):
          if name not in os.listdir(parent_fd):
              os.mkdir(name, 0o700, dir_fd=parent_fd)
          child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
          info = os.fstat(child_fd)
          if (
              not stat.S_ISDIR(info.st_mode)
              or info.st_dev != device
              or info.st_uid != 0
              or (info.st_mode & 0o077) != 0
          ):
              os.close(child_fd)
              raise RuntimeError('Untrusted intermediate directory in source tree')
          return child_fd

      def finalize_source_tree(dir_fd, device, depth=0):
          if depth > 64:
              raise RuntimeError('Source tree depth bound exceeded')
          for name in os.listdir(dir_fd):
              info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
              if info.st_dev != device:
                  raise RuntimeError('Foreign filesystem inside source')
              if stat.S_ISDIR(info.st_mode):
                  child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
                  try:
                      finalize_source_tree(child_fd, device, depth + 1)
                      os.fchown(child_fd, 1002, 1002)
                      os.fchmod(child_fd, 0o2750)
                  finally:
                      os.close(child_fd)

      def extract_safe_source_archive(upload_path, source, expected_digest=None):
          source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
          try:
              root_info = os.fstat(source_fd)
              if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != 0:
                  raise RuntimeError('Source root must be a root-owned directory')
              os.fchown(source_fd, 0, 0)
              os.fchmod(source_fd, 0o700)
              cleanup_source_fd(source_fd)
              upload_fd = os.open(upload_path, os.O_RDONLY | os.O_NOFOLLOW)
              upload_info = os.fstat(upload_fd)
              if not stat.S_ISREG(upload_info.st_mode) or upload_info.st_size > MAX_UPLOAD_ARCHIVE_BYTES:
                  os.close(upload_fd)
                  raise RuntimeError('Upload archive exceeds maximum size bound or is not a regular file')
              with os.fdopen(upload_fd, 'rb') as upload_f:
                  if expected_digest is not None:
                      hasher = hashlib.sha256()
                      hashed_bytes = 0
                      while True:
                          chunk = upload_f.read(1024 * 1024)
                          if not chunk:
                              break
                          hashed_bytes += len(chunk)
                          if hashed_bytes > MAX_UPLOAD_ARCHIVE_BYTES:
                              raise RuntimeError('Upload archive exceeds maximum size bound')
                          hasher.update(chunk)
                      if hasher.hexdigest() != expected_digest:
                          raise RuntimeError('Upload archive sha256 mismatch')
                      upload_f.seek(0)
                  with gzip.GzipFile(fileobj=upload_f, mode='rb') as gz_f:
                      bounded_gz = BoundedGzipStream(gz_f, MAX_SOURCE_DECOMPRESSED_BYTES)
                      with tarfile.open(fileobj=bounded_gz, mode='r:', tarinfo=SafeTarInfo) as tar:
                          member_count = 0
                          extracted_bytes = 0
                          for member in tar:
                              member_count += 1
                              if member_count > MAX_SOURCE_ARCHIVE_MEMBERS:
                                  raise RuntimeError('Source archive member count exceeds limit')
                              rel_parts = Path(member.name).parts
                              if (
                                  not rel_parts
                                  or Path(member.name).is_absolute()
                                  or any(p in ('', '.', '..') for p in rel_parts)
                              ):
                                  raise RuntimeError('Unsafe archive member path in source snapshot')
                              dir_depth = len(rel_parts) if member.isdir() else (len(rel_parts) - 1)
                              if dir_depth > 64 or len(rel_parts) > 64:
                                  raise RuntimeError('Source archive member path depth exceeds limit')
                              if member.isdir():
                                  cur_fd = source_fd
                                  opened_fds = []
                                  try:
                                      for part in rel_parts:
                                          cur_fd = open_nofollow_subdir(cur_fd, part, root_info.st_dev)
                                          opened_fds.append(cur_fd)
                                  finally:
                                      for fd_item in reversed(opened_fds):
                                          os.close(fd_item)
                              elif member.isfile():
                                  if member.size < 0 or member.size > MAX_SOURCE_MEMBER_BYTES:
                                      raise RuntimeError('Source archive member size exceeds limit')
                                  if extracted_bytes + member.size > MAX_SOURCE_EXTRACTED_BYTES:
                                      raise RuntimeError('Source archive uncompressed size exceeds limit')
                                  cur_fd = source_fd
                                  opened_fds = []
                                  try:
                                      for part in rel_parts[:-1]:
                                          cur_fd = open_nofollow_subdir(cur_fd, part, root_info.st_dev)
                                          opened_fds.append(cur_fd)
                                      stream = tar.extractfile(member)
                                      if stream is None:
                                          raise RuntimeError('Unable to read archive file member')
                                      file_mode = 0o750 if (member.mode & 0o100) else 0o640
                                      fd = os.open(
                                          rel_parts[-1],
                                          os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                          0o600,
                                          dir_fd=cur_fd,
                                      )
                                      with os.fdopen(fd, 'wb') as out_f:
                                          member_written = 0
                                          while True:
                                              buf = stream.read(65536)
                                              if not buf:
                                                  break
                                              member_written += len(buf)
                                              extracted_bytes += len(buf)
                                              if (
                                                  member_written > member.size
                                                  or member_written > MAX_SOURCE_MEMBER_BYTES
                                                  or extracted_bytes > MAX_SOURCE_EXTRACTED_BYTES
                                              ):
                                                  raise RuntimeError('Source archive uncompressed size exceeds limit')
                                              out_f.write(buf)
                                          os.fchown(out_f.fileno(), 1002, 1002)
                                          os.fchmod(out_f.fileno(), file_mode)
                                  finally:
                                      for fd_item in reversed(opened_fds):
                                          os.close(fd_item)
                              else:
                                  raise RuntimeError('Unsupported non-regular archive member in source snapshot')
              finalize_source_tree(source_fd, root_info.st_dev)
              os.fchown(source_fd, 0, 1002)
              os.fchmod(source_fd, 0o750)
              os.fsync(source_fd)
          finally:
              os.close(source_fd)

      def run_cleanup(root, project, run, upload_path, instance_id):
          verify_instance_id(instance_id)
          upload_path.unlink(missing_ok=True)
          export = root / 'exports' / run
          for path in (export, *export.parents):
              if path.is_symlink():
                  raise RuntimeError('Symlinked export path')
              if not path.exists():
                  continue
              info = path.lstat()
              if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                  raise RuntimeError('Export cleanup requires root-owned private directories')
          if export.exists():
              if export.resolve() != export:
                  raise RuntimeError('Noncanonical export path')
              shutil.rmtree(export)

      def reconcile_project_containers(source, target):
          ids = subprocess.check_output(
              ['/usr/bin/docker', 'ps', '-aq', '--no-trunc', '--filter', 'label=oxidex.remote-build=true'],
              text=True,
          ).split()
          for cid in ids:
              try:
                  rows = json.loads(
                      subprocess.check_output(
                          ['/usr/bin/docker', 'inspect', cid],
                          text=True,
                          stderr=subprocess.DEVNULL,
                      )
                  )
              except (subprocess.CalledProcessError, json.JSONDecodeError):
                  continue
              if not rows or not isinstance(rows[0], dict):
                  raise SystemExit(75)
              record = rows[0]
              mounts = record.get('Mounts') or []
              matches_project = any(
                  isinstance(m, dict) and m.get('Source') in (str(source), str(target))
                  for m in mounts
              )
              if not matches_project:
                  continue
              state = record.get('State') or {}
              is_stopped = (
                  isinstance(state, dict)
                  and state.get('Running') is False
                  and state.get('Paused') is False
                  and state.get('Restarting') is False
                  and state.get('Pid') == 0
                  and state.get('Status') in ('created', 'exited', 'dead')
              )
              if not is_stopped:
                  raise SystemExit(75)
              subprocess.run(
                  ['/usr/bin/docker', 'rm', cid],
                  check=False,
                  stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL,
              )

      def run_build(root, project, run, upload_path, digest, profile, instance_id):
          if not re.fullmatch(r'[0-9a-f]{64}', digest):
              raise ValueError('Invalid archive sha256 digest')
          if profile not in ('debug', 'release'):
              raise ValueError('Invalid build profile')
          try:
              verify_instance_id(instance_id)
              if Path('/run/oxidex-build-draining').exists():
                  raise SystemExit(75)
              source = root / 'sources' / project
              target = root / 'targets' / project
              lock_fd = os.open(f'/run/oxidex-cache-active-{project}.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
              os.fchown(lock_fd, 0, 0)
              os.fchmod(lock_fd, 0o600)
              with os.fdopen(lock_fd, 'a') as cache_lock:
                  try:
                      fcntl.flock(cache_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                  except BlockingIOError:
                      raise SystemExit(75)
                  tx_env = {**os.environ, 'OXIDEX_ACTIVE_LOCK_FD': str(cache_lock.fileno())}
                  subprocess.run(
                      ['/usr/local/bin/oxidex-remote-build', project, 'prepare'],
                      check=True,
                      env=tx_env,
                      pass_fds=(cache_lock.fileno(),),
                  )
                  reconcile_project_containers(source, target)
                  extract_safe_source_archive(upload_path, source, digest)
                  stages = [['cargo', 'fetch', '--locked', '--target', 'x86_64-unknown-linux-gnu']]
                  if profile == 'release':
                      stages.append(['just', 'cbindgen-check'])
                  build_cmd = ['cargo', 'build'] + (['--release'] if profile == 'release' else []) + ['--locked', '--bin', 'oxidex']
                  run_cmd = ['cargo', 'run'] + (['--release'] if profile == 'release' else []) + ['--locked', '--bin', 'oxidex', '--', '--readonly', '--version']
                  stages.extend([build_cmd, run_cmd])
                  for stage in stages:
                      rc = subprocess.run(
                          ['/usr/local/bin/oxidex-remote-build', project, *stage],
                          env=tx_env,
                          pass_fds=(cache_lock.fileno(),),
                      ).returncode
                      if rc != 0:
                          raise SystemExit(rc)
                  binary = target / profile / 'oxidex'
                  if not binary.is_file() or binary.is_symlink() or binary.resolve() != binary:
                      raise RuntimeError('Verified build binary is missing or a symlink')
                  export = root / 'exports' / run / 'oxidex'
                  export.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                  os.chown(root / 'exports', 0, 0)
                  os.chmod(root / 'exports', 0o755)
                  os.chown(export.parent, 0, 0)
                  os.chmod(export.parent, 0o755)
                  shutil.copy2(binary, export)
                  os.chown(export, 0, 0)
                  os.chmod(export, 0o755)
                  out_digest = hashlib.sha256(export.read_bytes()).hexdigest()
                  print(f'{out_digest}  {export}')
          finally:
              upload_path.unlink(missing_ok=True)

      def main():
          if os.geteuid() != 0:
              raise RuntimeError('Invoke this root-owned transaction wrapper with sudo')
          parser = argparse.ArgumentParser()
          parser.add_argument('operation', choices=['build', 'cleanup'])
          parser.add_argument('--project', required=True)
          parser.add_argument('--run', required=True)
          parser.add_argument('--upload', required=True)
          parser.add_argument('--digest', default='')
          parser.add_argument('--profile', default='release')
          parser.add_argument('--instance-id', default='')
          args = parser.parse_args()
          config = json.loads(Path('/etc/oxidex-remote-build.json').read_text())
          root = Path(config.get('cache_mount', '/mnt/runner-data/remote-build'))
          mount_root = root.parent if root.name == 'remote-build' else root
          if not os.path.ismount(mount_root):
              raise RuntimeError(f'Builder cache mount point is not mounted: {mount_root}')
          upload_path = validate_identity(args.project, args.run, args.upload)
          with open('/run/oxidex-build.lock', 'a') as build_lock, open(f'/run/oxidex-project-{args.project}.lock', 'a') as proj_lock:
              fcntl.flock(build_lock, fcntl.LOCK_SH)
              fcntl.flock(proj_lock, fcntl.LOCK_EX)
              if args.operation == 'cleanup':
                  run_cleanup(root, args.project, args.run, upload_path, args.instance_id)
              else:
                  run_build(root, args.project, args.run, upload_path, args.digest, args.profile, args.instance_id)

      if __name__ == '__main__':
          main()
  - path: /usr/local/bin/oxidex-remote-build
    permissions: '0755'
    owner: root:root
    content: |
      #!/usr/bin/env python3
      import fcntl, json, os, re, subprocess, sys
      from pathlib import Path

      allowed_commands = {'cargo', 'rustc', 'rustfmt', 'clippy-driver', 'just', 'cbindgen'}

      def validate_project(project):
          if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', project):
              raise ValueError('Project must be a simple identifier, never a path')
          return project

      def checked_path(path):
          if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
              raise ValueError('Symlinked builder paths are forbidden')

      def ensure_metadata_isolation():
          for chain, rule in (
              ('DOCKER-USER', ['-d', '169.254.169.254/32', '-j', 'REJECT']),
              ('OUTPUT', ['-d', '169.254.169.254/32', '-m', 'owner', '!', '--uid-owner', '0', '-j', 'REJECT']),
          ):
              if subprocess.run(
                  ['iptables', '-C', chain, *rule],
                  stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL,
              ).returncode != 0:
                  subprocess.run(['iptables', '-I', chain, '1', *rule], check=True)
          sa_probe = subprocess.run(
              [
                  'curl', '--fail', '--silent', '--max-time', '2',
                  '-H', 'Metadata-Flavor:Google',
                  'http://169.254.169.254/computeMetadata/v1/instance/service-accounts/',
              ],
              capture_output=True,
              text=True,
          )
          if sa_probe.returncode == 0 and sa_probe.stdout.strip():
              raise RuntimeError('Builder VM must be provisioned with --no-service-account --no-scopes')

      def main():
          if os.geteuid() != 0:
              raise RuntimeError('Invoke this root-owned launcher with sudo')
          if sys.argv[1:2] in (['transaction'], ['cleanup-export']):
              op = 'cleanup' if sys.argv[1] == 'cleanup-export' else 'build'
              os.execv('/usr/local/bin/oxidex-remote-transaction', ['/usr/local/bin/oxidex-remote-transaction', op, *sys.argv[2:]])
          config = json.loads(Path('/etc/oxidex-remote-build.json').read_text())
          if config.get('service_account', 'none') != 'none' or config.get('scopes', []):
              raise RuntimeError('Builder config forbids attached service accounts or scopes')
          root = Path(config.get('cache_mount', '/mnt/runner-data/remote-build'))
          mount_root = root.parent if root.name == 'remote-build' else root
          if not os.path.ismount(mount_root):
              raise RuntimeError(f'Builder cache mount point is not mounted: {mount_root}')
          image = config.get('image', 'oxidex-remote-builder:1.99.0')
          cpus = int(config.get('cpus', 16))
          memory_gib = int(config.get('memory_gib', 28))
          if sys.argv[1:] == ['--verify-readiness']:
              ensure_metadata_isolation()
              subprocess.run(['docker', 'image', 'inspect', image], check=True, stdout=subprocess.DEVNULL)
              for sub in ('cargo', 'sources', 'targets'):
                  checked_path(root / sub)
                  if not (root / sub).is_dir():
                      raise RuntimeError(f'Missing builder cache directory: {root / sub}')
              return
          if len(sys.argv) < 2:
              raise ValueError('Project identifier is required')
          project = validate_project(sys.argv[1])
          active_lock_path = Path(f'/run/oxidex-cache-active-{project}.lock')
          active_lock_fd = os.open(active_lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
          os.fchown(active_lock_fd, 0, 0)
          os.fchmod(active_lock_fd, 0o600)
          with open('/run/oxidex-build.lock', 'a') as lock, os.fdopen(active_lock_fd, 'a') as active_lock:
              fcntl.flock(lock, fcntl.LOCK_SH)
              inherited_fd = os.environ.get('OXIDEX_ACTIVE_LOCK_FD')
              if inherited_fd is not None:
                  try:
                      fd_stat = os.fstat(int(inherited_fd))
                      path_stat = active_lock_path.lstat()
                  except (OSError, ValueError):
                      raise SystemExit(75)
                  if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                      raise SystemExit(75)
              else:
                  if int(os.environ.get('SUDO_UID') or '0') != 0:
                      raise PermissionError('Direct container stage invocation requires root transaction wrapper')
                  try:
                      fcntl.flock(active_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                  except BlockingIOError:
                      raise SystemExit(75)
              if Path('/run/oxidex-build-draining').exists():
                  raise SystemExit(75)
              for path in (root, root / 'sources', root / 'targets', root / 'cargo', root / 'sources' / project, root / 'targets' / project):
                  checked_path(path)
                  path.mkdir(parents=True, exist_ok=True)
              if sys.argv[2:] == ['prepare']:
                  source = root / 'sources' / project
                  os.chown(root, 0, 0)
                  root.chmod(0o755)
                  for anc in (root / 'sources', source, root / 'targets'):
                      os.chown(anc, 0, 1002)
                      anc.chmod(0o750)
                  for path in (root / 'targets' / project, root / 'cargo'):
                      os.chown(path, 1002, 1002)
                      path.chmod(0o750)
                  print(source)
                  return
              command = sys.argv[2:] or ['cargo', 'build', '--release', '--locked', '--bin', 'oxidex']
              if command[0] not in allowed_commands:
                  raise ValueError(f'Unsupported build command: {command[0]}')
              if cpus < 1 or memory_gib < 1:
                  raise ValueError('Resource limits must be positive')
              ensure_metadata_isolation()
              net_args = (
                  ['--add-host=metadata.google.internal:127.0.0.1', '--add-host=metadata:127.0.0.1']
                  if command[:2] == ['cargo', 'fetch']
                  else ['--network=none']
              )
              cmd = [
                  'docker', 'run', '--rm', '--user=1002:1002', f'--cpus={cpus}',
                  '--label=oxidex.remote-build=true',
                  f'--memory={memory_gib}g', f'--memory-swap={memory_gib}g', '--pids-limit=4096',
                  '--cap-drop=ALL', '--security-opt=no-new-privileges',
                  *net_args,
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
  - iptables -C DOCKER-USER -d 169.254.169.254/32 -j REJECT 2>/dev/null || iptables -I DOCKER-USER 1 -d 169.254.169.254/32 -j REJECT
  - iptables -C OUTPUT -d 169.254.169.254/32 -m owner ! --uid-owner 0 -j REJECT 2>/dev/null || iptables -I OUTPUT 1 -d 169.254.169.254/32 -m owner ! --uid-owner 0 -j REJECT
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
    provisioning: str = "spot",
) -> dict[str, Any]:
    """Calculate compute-only and all-in (with disk & taxes) hourly, daily, and monthly prices.

    ``provisioning="ondemand"`` prices a standard (non-Spot) VM from the Billing Catalog's on-demand component
    rates. Without them the Spot baseline would understate it, so the result is marked ``"complete": False``.
    """
    on_demand = provisioning == "ondemand"
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
    if machine_type and not on_demand:  # capacityHistory prices are Spot prices
        live_machine_rate = machine_hourly_map.get((region, machine_type))
        if live_machine_rate is None:
            live_machine_rate = machine_hourly_map.get(f"{region}/{machine_type}")

    series_key = family.lower()
    price_kind = "ondemand" if on_demand else "spot"
    live_cpu_rate = comp_rates_map.get((price_kind, region, series_key, "CPU"))
    live_ram_rate = comp_rates_map.get((price_kind, region, series_key, "RAM"))
    applicable_components_known = (
        (vcpu > 0 or ram_gib > 0)
        and (vcpu <= 0 or live_cpu_rate is not None)
        and (ram_gib <= 0 or live_ram_rate is not None)
    )

    if live_machine_rate is not None:
        compute_hourly = _dec(live_machine_rate)
        pricing_source = "gcp_capacity_history"
    elif (live_cpu_rate is not None and live_ram_rate is not None) or applicable_components_known:
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

    live_boot_disk = next(
        (disk_rates_map[key] for key in ((region, boot_disk_type), f"{region}/{boot_disk_type}", boot_disk_type)
         if disk_rates_map.get(key) is not None),
        None,
    )
    live_data_disk = next(
        (disk_rates_map[key] for key in ((region, data_disk_type), f"{region}/{data_disk_type}", data_disk_type)
         if disk_rates_map.get(key) is not None),
        None,
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

    result = {
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
    custom_shape = bool(machine_type) and ("custom" in machine_type or machine_type.endswith("-ext"))
    # These types bill provisioned throughput or IOPS separately, which this capacity price leaves out.
    performance_billed = {"hyperdisk-throughput", "pd-extreme"} & {
        kind for kind, gb in ((boot_disk_type, boot_disk_gb), (data_disk_type, data_disk_gb)) if gb > 0
    }
    if on_demand:
        result["provisioning"] = "ondemand"
        result["complete"] = pricing_source == "gcp_billing_catalog" and not custom_shape
        if not result["complete"]:
            result["pricing_source"] = "spot_baseline_for_on_demand_vm"
    if custom_shape and pricing_source != "gcp_capacity_history":
        # Custom and extended shapes bill their own SKUs; predefined series rates do not price them.
        result["complete"] = False
    if performance_billed:
        result["complete"] = False
    # A disk type with neither a live rate nor a static one is priced at a generic fallback, which is not a price.
    unknown_disk = [kind for kind, gb, live in ((boot_disk_type, boot_disk_gb, live_boot_disk),
                                               (data_disk_type, data_disk_gb, live_data_disk))
                    if gb and live is None and kind not in DISK_MONTHLY_RATES_USD_PER_GIB]
    if unknown_disk:
        result["complete"] = False
        result["disk_rate_unknown"] = sorted(set(unknown_disk))
    if pricing_source == "catalog_baseline" and vcpu and family not in SERIES_SPOT_BASE_RATES:
        # No machine, component, or family rate: the generic per-vCPU/GiB fallback is not a trusted price.
        result["complete"] = False
        result["family_rate_unknown"] = True
    # Static baselines have no regional multiplier outside the catalog. Tax trust is separate.
    if reg_info is None:
        if (vcpu > 0 or ram_gib > 0) and pricing_source == "catalog_baseline":
            result["regional_compute_rate_unknown"] = True
            result["complete"] = False
        if any(gb > 0 and live is None for gb, live in (
            (boot_disk_gb, live_boot_disk), (data_disk_gb, live_data_disk),
        )):
            result["regional_disk_rate_unknown"] = True
            result["complete"] = False
    return result


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


def validate_builder_container_limits(values: dict[str, Any]) -> None:
    """Validate supplied builder limit fields before coercion; unrelated patches are allowed.

    Call with mutation fields to preserve unrelated edits to legacy profiles, and with
    the full profile/policy before rendering or planning new builder capacity.
    """
    for keys, cap, integer in (
        (("runner_vcpu", "container_vcpu", "builder_vcpu"), Decimal("16"), True),
        (("runner_ram_gib", "container_ram_gib", "builder_ram_gib"), Decimal("28"), False),
    ):
        supplied = []
        for key in keys:
            if key not in values:
                continue
            value = _dec(values[key])
            if value <= 0 or value > cap or (integer and value != value.to_integral_value()):
                raise ValueError(f"{key} must be {'an integer ' if integer else ''}> 0 and <= {cap} for a dedicated builder")
            supplied.append(value)
        if len(set(supplied)) > 1:
            raise ValueError(f"Builder limit aliases must agree: {', '.join(keys)}")


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
    if role == "rust_builder":
        try:
            validate_builder_container_limits(profile_config)
        except ValueError as exc:
            # Keep old profiles readable and budget-editable, but never recommend impossible capacity.
            return {
                "role": role, "pricing_source": "unavailable", "requirements": dict(profile_config),
                "hardware_qualifying_count": 0, "spot_price_qualifying_count": 0,
                "hardware_qualifying": [], "spot_price_qualifying": [],
                "disqualified_counts": {}, "validation_error": str(exc),
            }
    min_runners = max(1, int(profile_config.get("min_runners", 4 if role == "cicd_runner" else 1)))
    min_instances = max(1, int(profile_config.get("min_instances", 1)))
    max_instances = max(min_instances, int(profile_config.get("max_instances", 10)))

    # Per-runner / per-builder unit requirements
    unit_vcpu = max(1, int(profile_config.get("runner_vcpu", 4 if role == "cicd_runner" else 16)))
    unit_ram_gib = float(max(1, int(math.ceil(float(profile_config.get("runner_ram_gib", 16 if role == "cicd_runner" else 28))))))
    host_overhead_gib = float(max(0, int(math.ceil(float(profile_config.get("host_overhead_gib", 0.0))))))
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
            if role == "rust_builder" and (
                shape["machine_type"] != "n4-highcpu-16" or shape["vcpu"] != 16 or shape["ram_gib"] != 32
            ):
                continue
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
        pending_flow_lines: list[str] = []
        pending_flow_start_idx: int = 0
        pending_flow_indent: int = 0
        in_single = False
        in_double = False
        escape = False
        bracket_depth = 0
        brace_depth = 0
        for idx, raw in enumerate(lines[1:], start=2):
            if not pending_flow_lines:
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
                pending_flow_lines = [stripped]
                pending_flow_start_idx = idx
                pending_flow_indent = indent
                in_single = False
                in_double = False
                escape = False
                bracket_depth = 0
                brace_depth = 0
                scan_segment = stripped
            else:
                if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                    errors.append(f"YAML syntax error on line {idx}: tab characters are not allowed for indentation.")
                    pending_flow_lines = []
                    in_single = in_double = escape = False
                    bracket_depth = brace_depth = 0
                    continue
                if not in_single and not in_double:
                    if not raw.strip() or raw.lstrip().startswith("#"):
                        continue
                    scan_segment = _strip_yaml_inline_comment(raw.strip())
                    if not scan_segment:
                        continue
                else:
                    scan_segment = raw.strip()
                pending_flow_lines.append(scan_segment)

            # Check balanced brackets, braces, and quotes across single-line or multiline flow constructs
            escape = False
            for ch in scan_segment:
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
            if bracket_depth < 0 or brace_depth < 0:
                errors.append(
                    f"YAML syntax error on line {idx}: unclosed bracket, brace, or quote in '{scan_segment}'."
                )
                pending_flow_lines = []
                in_single = in_double = escape = False
                bracket_depth = brace_depth = 0
                continue
            if in_single or in_double or bracket_depth > 0 or brace_depth > 0:
                continue

            if len(pending_flow_lines) > 1:
                stripped = re.sub(r",\s*([\]}]+)$", r"\1", " ".join(pending_flow_lines))
            else:
                stripped = pending_flow_lines[0]
            idx = pending_flow_start_idx
            indent = pending_flow_indent
            pending_flow_lines = []

            if stripped == "---" and indent == 0 and not scopes:
                continue
            if stripped == "..." and indent == 0:
                scopes.clear()
                continue

            mapping_key_re = r'^(?P<key>[A-Za-z0-9_.-]+|"(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\')\s*:(?P<val>\s+.*|\s*)$'
            m_map_line = re.match(mapping_key_re, stripped)
            is_mapping = bool(m_map_line)
            is_list_item = bool(re.match(r"^-(?:\s+.*|\s*)$", stripped))
            is_flow_collection_node = (
                not is_mapping
                and not is_list_item
                and stripped.startswith(("[", "{"))
                and indent > 0
                and bool(scopes)
                and scopes[-1]["allow_children"]
                and scopes[-1]["child_indent"] is None
            )
            if not (is_mapping or is_list_item or is_flow_collection_node):
                errors.append(
                    f"YAML syntax error on line {idx}: expected mapping 'key: value' or list item '- value'."
                )
                continue

            while scopes and scopes[-1]["indent"] > indent:
                scopes.pop()

            if is_flow_collection_node:
                errors.extend(
                    _validate_yaml_flow_value(stripped, idx, defined_anchors=defined_anchors)
                )
                scopes[-1]["allow_children"] = False
                continue

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
                m_list_map = re.match(mapping_key_re, after_dash) if after_dash else None
                if not after_dash:
                    scopes.append(
                        {"indent": indent, "kind": "list", "child_indent": None, "allow_children": True}
                    )
                elif m_list_map:
                    list_key_str = m_list_map.group("key")
                    if list_key_str.startswith(("'", '"')):
                        errors.extend(
                            _validate_yaml_flow_value(list_key_str, idx, defined_anchors=defined_anchors)
                        )
                    key_indent = indent + (len(stripped) - len(after_dash))
                    scopes.append(
                        {"indent": indent, "kind": "list", "child_indent": key_indent, "allow_children": True}
                    )
                    v_part = m_list_map.group("val")
                    v_str = v_part.strip()
                    if re.search(r"^[|>](?:[1-9][-+]?|[-+]?[1-9]?)\s*$", v_str):
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
                    m_list_anchor = re.match(r"^&([A-Za-z0-9_-]+)$", after_dash)
                    if m_list_anchor:
                        defined_anchors.add(m_list_anchor.group(1))
                        scopes.append(
                            {"indent": indent, "kind": "list", "child_indent": None, "allow_children": True}
                        )
                        continue
                    if re.search(r"^[|>](?:[1-9][-+]?|[-+]?[1-9]?)\s*$", after_dash):
                        block_scalar_indent = indent
                    else:
                        errors.extend(
                            _validate_yaml_flow_value(after_dash, idx, defined_anchors=defined_anchors)
                        )
                    scopes.append(
                        {"indent": indent, "kind": "list", "child_indent": None, "allow_children": False}
                    )
            else:
                assert m_map_line is not None
                map_key_str = m_map_line.group("key")
                if map_key_str.startswith(("'", '"')):
                    errors.extend(
                        _validate_yaml_flow_value(map_key_str, idx, defined_anchors=defined_anchors)
                    )
                v_part = m_map_line.group("val")
                v_str = v_part.strip()
                if re.search(r"^[|>](?:[1-9][-+]?|[-+]?[1-9]?)\s*$", v_str):
                    block_scalar_indent = indent
                    scopes.append(
                        {"indent": indent, "kind": "map", "child_indent": None, "allow_children": False}
                    )
                elif not v_str:
                    scopes.append(
                        {"indent": indent, "kind": "map", "child_indent": None, "allow_children": True}
                    )
                else:
                    m_block_anchor = re.match(
                        r"^&([A-Za-z0-9_-]+)\s+[|>](?:[1-9][-+]?|[-+]?[1-9]?)\s*$",
                        v_str,
                    )
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

        if pending_flow_lines:
            errors.append(
                f"YAML syntax error on line {pending_flow_start_idx}: unclosed bracket, brace, or quote in '{pending_flow_lines[0]}'."
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
                            'raw = STDIN.read; doc = begin; YAML.safe_load(raw, permitted_classes: [], permitted_symbols: [], aliases: true); rescue ArgumentError; YAML.safe_load(raw, [], [], true); end; abort("YAML syntax error: top-level cloud-config document must be a mapping") unless doc.is_a?(Hash)',
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

    package_update = bool(re.search(r'^\s*["\']?package_update["\']?\s*:\s*true\b', text, re.M | re.I))
    package_upgrade = bool(re.search(r'^\s*["\']?package_upgrade["\']?\s*:\s*true\b', text, re.M | re.I))

    def _extract_top_list(section_name: str) -> list[str]:
        items: list[str] = []
        in_section = False
        flow_buf: list[str] = []
        flow_depth = 0
        for raw in lines:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if flow_buf:
                seg = _strip_yaml_inline_comment(stripped)
                if seg:
                    flow_buf.append(seg)
                    flow_depth += seg.count("[") - seg.count("]")
                if flow_depth <= 0:
                    joined = " ".join(flow_buf).strip()
                    if joined.startswith("[") and joined.endswith("]"):
                        for part in _split_yaml_flow_top_level(joined[1:-1], ","):
                            cleaned = part.strip()
                            if cleaned:
                                items.append(cleaned)
                    break
                continue
            m_sec = re.match(rf'^["\']?{re.escape(section_name)}["\']?\s*:\s*(.*)$', raw)
            if m_sec:
                rest = _strip_yaml_inline_comment(m_sec.group(1).strip())
                if rest.startswith("["):
                    flow_buf = [rest]
                    flow_depth = rest.count("[") - rest.count("]")
                    if flow_depth <= 0 and rest.endswith("]"):
                        for part in _split_yaml_flow_top_level(rest[1:-1], ","):
                            cleaned = part.strip()
                            if cleaned:
                                items.append(cleaned)
                        break
                    continue
                if not rest:
                    in_section = True
                    continue
            if in_section:
                if re.match(r'^["\']?[A-Za-z0-9_.-]+["\']?\s*:\s*', raw):
                    break
                seg = _strip_yaml_inline_comment(stripped)
                if seg.startswith("[") and not items:
                    flow_buf = [seg]
                    flow_depth = seg.count("[") - seg.count("]")
                    if flow_depth <= 0 and seg.endswith("]"):
                        for part in _split_yaml_flow_top_level(seg[1:-1], ","):
                            cleaned = part.strip()
                            if cleaned:
                                items.append(cleaned)
                        break
                    continue
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
        if re.match(r'^["\']?users["\']?\s*:\s*$', raw):
            in_users = True
            continue
        if in_users:
            if re.match(r'^["\']?[A-Za-z0-9_.-]+["\']?\s*:\s*', raw):
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
                    current_user = {k.strip().strip("'\""): v.strip()}
                else:
                    users.append({"name": val, "role": "system default"})
                    current_user = None
            elif current_user is not None:
                kv = re.match(r'^\s+(["\']?[A-Za-z0-9_.-]+["\']?)\s*:\s*(.+)$', raw)
                if kv:
                    current_user[kv.group(1).strip().strip("'\"")] = kv.group(2).strip()
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


def render_cloud_init_for_profile(
    content: str,
    *,
    role: str = "cicd_runner",
    runners_count: int = 4,
    runner_vcpu: int = 4,
    runner_ram_gib: float = 16.0,
    instance_ram_gib: float = 64.0,
    host_overhead_gib: float = 0.0,
) -> str:
    """Synchronize runner density and per-slot resource limits in a cloud-init template."""
    if role == "rust_builder":
        validate_builder_container_limits({"runner_vcpu": runner_vcpu, "runner_ram_gib": runner_ram_gib})
    text = str(content or "")
    slots = max(1, int(runners_count))
    vcpu = max(1, int(runner_vcpu))
    ram_gib = max(1, int(math.ceil(float(runner_ram_gib))))
    reserve_gib = max(0, int(math.ceil(float(host_overhead_gib))))
    usable_work_gib = max(
        ram_gib,
        int(math.floor(float(instance_ram_gib)) - reserve_gib),
    )
    if role == "rust_builder":
        text = re.sub(
            r'("cpus"\s*:\s*)\d+',
            rf"\g<1>{vcpu}",
            text,
        )
        text = re.sub(
            r'("memory_gib"\s*:\s*)\d+',
            rf"\g<1>{ram_gib}",
            text,
        )
        return text

    replacements = {
        "RUNNERS_COUNT": str(slots),
        "RUNNER_CPU_LIMIT": str(vcpu),
        "RUNNER_MEMORY_GB": str(ram_gib),
        "HOST_RESERVE_GIB": str(reserve_gib),
        "ADMISSION_WORK_GIB": str(usable_work_gib),
    }
    for env_key, env_val in replacements.items():
        text = re.sub(
            rf'^(\s*{re.escape(env_key)}=)"?[^"\n]*"?\s*$',
            rf'\g<1>"{env_val}"',
            text,
            flags=re.M,
        )
    return text

