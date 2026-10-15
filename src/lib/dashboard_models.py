"""Core data models, CPU benchmark catalog, region filtering, instance qualification,
VM templates, and Cloud-Init parser/visualizer for the Web UI Dashboard.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import re
import socket
import time
from typing import Any

from .machine_profiles import MACHINE_PROFILES


HOURS_PER_DAY = Decimal("24")
DAYS_PER_MONTH = Decimal("365") / Decimal("12")
HOURS_PER_MONTH = HOURS_PER_DAY * DAYS_PER_MONTH


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


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
      RUNNER_LABELS="self-hosted,Linux,X64,gcp-dallas,n2,rust,docker"

runcmd:
  # 1. Install Official Docker Engine & Buildx
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  - chmod a+r /etc/apt/keyrings/docker.asc
  - apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  - systemctl enable --now docker
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
      DISK_MOUNT="/mnt/runner-data"

runcmd:
  - mkdir -p /mnt/runner-data/docker /mnt/runner-data/containerd /mnt/runner-data/work
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  - chmod a+r /etc/apt/keyrings/docker.asc
  - systemctl enable --now containerd docker
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
    sudo: ["ALL=(root) NOPASSWD: /usr/local/bin/oxidex-remote-build"]
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
  - clang
  - lld
  - cmake
  - pkg-config
  - libssl-dev
  - zstd

write_files:
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

runcmd:
  # 1. Mount persistent NVMe Hyperdisk cache and configure unprivileged build container
  - mkdir -p /mnt/runner-data/remote-build/cargo /mnt/runner-data/remote-build/targets
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  - chmod a+r /etc/apt/keyrings/docker.asc
  - systemctl enable --now containerd docker
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
) -> dict[str, float]:
    """Calculate compute-only and all-in (with disk & taxes) hourly, daily, and monthly prices."""
    reg_info = next((r for r in REGIONS_CATALOG if r["region"] == region), None)
    mult = _dec(reg_info["price_multiplier"] if reg_info else "1.0")
    cpu_rate, ram_rate = SERIES_SPOT_BASE_RATES.get(
        family, (Decimal("0.0040"), Decimal("0.00045"))
    )
    compute_hourly = (cpu_rate * _dec(vcpu) + ram_rate * _dec(ram_gib)) * mult
    compute_daily = compute_hourly * HOURS_PER_DAY
    compute_monthly = compute_hourly * HOURS_PER_MONTH

    boot_rate = DISK_MONTHLY_RATES_USD_PER_GIB.get(boot_disk_type, Decimal("0.080"))
    data_rate = DISK_MONTHLY_RATES_USD_PER_GIB.get(data_disk_type, Decimal("0.080"))
    disk_monthly = (boot_rate * _dec(boot_disk_gb) + data_rate * _dec(data_disk_gb)) * mult
    disk_daily = disk_monthly / DAYS_PER_MONTH
    disk_hourly = disk_daily / HOURS_PER_DAY

    subtotal_hourly = compute_hourly + disk_hourly
    tax_fraction = _dec(tax_rate_percent) / Decimal("100")
    tax_hourly = subtotal_hourly * tax_fraction
    total_hourly = subtotal_hourly + tax_hourly
    total_daily = total_hourly * HOURS_PER_DAY
    total_monthly = total_hourly * HOURS_PER_MONTH

    return {
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
    max_monthly = _dec(budget.get("max_monthly_usd", "300.00"))

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

    return {
        "active_period": active_period,
        "max_hourly_usd": _money4(max_hourly),
        "max_daily_usd": _money(max_daily),
        "max_monthly_usd": _money(max_monthly),
        "include_disk_costs": bool(budget.get("include_disk_costs", True)),
        "include_taxes": bool(budget.get("include_taxes", False)),
        "tax_rate_percent": float(budget.get("tax_rate_percent", 8.25)),
    }


def filter_regions(
    regions_state: dict[str, bool],
    *,
    max_latency_ms: float | None = None,
    continents: list[str] | None = None,
    required_family: str | None = None,
    required_cpu_platform: str | None = None,
    required_disk_type: str | None = None,
    only_enabled: bool = False,
) -> list[dict[str, Any]]:
    """Filter world data center regions by latency, continent, instance family, CPU type, and disk support."""
    results: list[dict[str, Any]] = []
    continent_set = {c.strip() for c in continents if c.strip()} if continents else None
    for item in REGIONS_CATALOG:
        region = item["region"]
        enabled = bool(regions_state.get(region, True))
        if only_enabled and not enabled:
            continue
        if max_latency_ms is not None and item["latency_ms"] > max_latency_ms:
            continue
        if continent_set and item["continent"] not in continent_set:
            continue
        if required_family and required_family not in item["families"]:
            continue
        if required_cpu_platform and required_cpu_platform not in item["cpu_platforms"]:
            continue
        if required_disk_type and required_disk_type not in item["disk_types"]:
            continue
        results.append({**item, "enabled": enabled})
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
    tax_rate = budget["tax_rate_percent"] if include_taxes else 0.0

    hardware_qualifying: list[dict[str, Any]] = []
    spot_price_qualifying: list[dict[str, Any]] = []
    disqualified_counts = {
        "region_disabled": 0,
        "insufficient_vcpu_or_ram": 0,
        "below_benchmark_score": 0,
        "incompatible_disk": 0,
        "exceeds_spot_budget": 0,
    }

    for reg in REGIONS_CATALOG:
        region = reg["region"]
        if not regions_state.get(region, True):
            disqualified_counts["region_disabled"] += 1
            continue

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

            chosen_boot_disk = (
                "hyperdisk-balanced"
                if "hyperdisk-balanced" in allowed_disks and family in ("c4d", "n4d", "c4", "n4")
                else chosen_data_disk
            )

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
                eval_hourly = pricing["compute_hourly_usd"]
                eval_daily = pricing["compute_daily_usd"]
                eval_monthly = pricing["compute_monthly_usd"]

            instances_for_floor = max(
                min_instances,
                (min_runners + units_per_instance - 1) // units_per_instance,
            )
            zone = reg["zones"][0]
            within_hourly = eval_hourly <= budget["max_hourly_usd"]
            within_daily = eval_daily <= budget["max_daily_usd"]
            within_monthly = eval_monthly <= budget["max_monthly_usd"]
            price_qualifies = within_hourly and within_daily and within_monthly

            candidate = {
                "region": region,
                "zone": zone,
                "display_location": reg["display_name"],
                "continent": reg["continent"],
                "latency_ms": reg["latency_ms"],
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
                "fleet_floor_daily_usd": round(eval_daily * instances_for_floor, 2),
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
    for cmd in runcmd:
        if cmd.strip() == "chmod":
            warnings.append("Incomplete 'chmod' command detected in runcmd (missing mode and target path).")

    return {
        "format": "cloud-config" if is_cloud_config else ("shell-script" if is_shell_script else "custom"),
        "valid": len(warnings) == 0,
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
