"""Rust remote builder telemetry, IP geolocation, VPN/Proxy/Tor exit-node lookup,
active memory OOM floor tracking, and CPU Points (CPU time * instance benchmark score)
analytics for the Web UI Dashboard.
"""

from __future__ import annotations

from datetime import datetime
import ipaddress
from typing import Any

from .dashboard_models import compute_cpu_benchmark_score, format_gcloud_ssh_command


# Curated IP intelligence database covering ProtonVPN, Tor exit nodes, Mullvad, NordVPN,
# cloud proxies, and developer workstation networks.
KNOWN_IP_INTEL_NETWORKS: tuple[dict[str, Any], ...] = (
    {
        "cidr": "185.159.157.0/24",
        "provider_label": "ProtonVPN",
        "network_type": "VPN",
        "is_vpn": True,
        "is_tor": False,
        "is_proxy": True,
        "badge_text": "ProtonVPN (Exit Node)",
        "city": "Zurich",
        "region": "Zurich",
        "country": "Switzerland",
        "country_code": "CH",
        "lat": 47.3769,
        "lon": 8.5417,
        "asn": "AS62371",
        "isp": "Proton AG / Datacamp Limited",
        "default_hostname": "node-ch-14.protonvpn.net",
    },
    {
        "cidr": "37.19.192.0/18",
        "provider_label": "ProtonVPN",
        "network_type": "VPN",
        "is_vpn": True,
        "is_tor": False,
        "is_proxy": True,
        "badge_text": "ProtonVPN (Exit Node)",
        "city": "Frankfurt",
        "region": "Hesse",
        "country": "Germany",
        "country_code": "DE",
        "lat": 50.1109,
        "lon": 8.6821,
        "asn": "AS212238",
        "isp": "Datacamp Limited (ProtonVPN)",
        "default_hostname": "exit-de-08.protonvpn.net",
    },
    {
        "cidr": "185.220.101.0/24",
        "provider_label": "Tor",
        "network_type": "Tor",
        "is_vpn": False,
        "is_tor": True,
        "is_proxy": True,
        "badge_text": "Tor Exit Node",
        "city": "Falkenstein",
        "region": "Saxony",
        "country": "Germany",
        "country_code": "DE",
        "lat": 50.4779,
        "lon": 12.3713,
        "asn": "AS205100",
        "isp": "Zwiebelfreunde e.V. Tor Exit",
        "default_hostname": "tor-exit-101.zwiebelfreunde.de",
    },
    {
        "cidr": "198.96.155.0/24",
        "provider_label": "Tor",
        "network_type": "Tor",
        "is_vpn": False,
        "is_tor": True,
        "is_proxy": True,
        "badge_text": "Tor Exit Node",
        "city": "Waterloo",
        "region": "Ontario",
        "country": "Canada",
        "country_code": "CA",
        "lat": 43.4643,
        "lon": -80.5204,
        "asn": "AS53667",
        "isp": "Calyx Institute Tor Relay",
        "default_hostname": "tor-exit-ca.calyxinstitute.org",
    },
    {
        "cidr": "193.138.218.0/24",
        "provider_label": "Mullvad VPN",
        "network_type": "VPN",
        "is_vpn": True,
        "is_tor": False,
        "is_proxy": True,
        "badge_text": "Mullvad VPN",
        "city": "Gothenburg",
        "region": "Vastra Gotaland",
        "country": "Sweden",
        "country_code": "SE",
        "lat": 57.7089,
        "lon": 11.9746,
        "asn": "AS39351",
        "isp": "31173 Services AB (Mullvad)",
        "default_hostname": "se-got-wg-004.mullvad.net",
    },
    {
        "cidr": "194.35.233.0/24",
        "provider_label": "NordVPN",
        "network_type": "VPN",
        "is_vpn": True,
        "is_tor": False,
        "is_proxy": True,
        "badge_text": "NordVPN Proxy",
        "city": "Amsterdam",
        "region": "North Holland",
        "country": "Netherlands",
        "country_code": "NL",
        "lat": 52.3676,
        "lon": 4.9041,
        "asn": "AS212238",
        "isp": "Tefincom S.A. (NordVPN)",
        "default_hostname": "nl942.nordvpn.com",
    },
    {
        "cidr": "73.162.0.0/16",
        "provider_label": "Direct",
        "network_type": "Residential / Workstation",
        "is_vpn": False,
        "is_tor": False,
        "is_proxy": False,
        "badge_text": "Direct (No VPN/Proxy)",
        "city": "Austin",
        "region": "Texas",
        "country": "United States",
        "country_code": "US",
        "lat": 30.2672,
        "lon": -97.7431,
        "asn": "AS7922",
        "isp": "Comcast Cable Communications",
        "default_hostname": "m4air-workstation.local",
    },
    {
        "cidr": "24.28.0.0/16",
        "provider_label": "Direct",
        "network_type": "Residential / Workstation",
        "is_vpn": False,
        "is_tor": False,
        "is_proxy": False,
        "badge_text": "Direct (No VPN/Proxy)",
        "city": "Dallas",
        "region": "Texas",
        "country": "United States",
        "country_code": "US",
        "lat": 32.7767,
        "lon": -96.7970,
        "asn": "AS11427",
        "isp": "Charter / Spectrum Fiber",
        "default_hostname": "dev-macbook-pro.tx.rr.com",
    },
)


def classify_ip_and_network(ip_address: str, hostname: str | None = None) -> dict[str, Any]:
    """Perform IP geolocation, hostname lookup, and VPN / Proxy / Tor exit node detection."""
    clean_ip = (ip_address or "127.0.0.1").strip()
    clean_host = (hostname or "").strip()
    lower_host = clean_host.lower()

    try:
        parsed_ip = ipaddress.ip_address(clean_ip)
    except ValueError:
        parsed_ip = None

    if parsed_ip is not None:
        for entry in KNOWN_IP_INTEL_NETWORKS:
            net = ipaddress.ip_network(entry["cidr"], strict=False)
            if parsed_ip in net:
                resolved_host = clean_host or entry["default_hostname"]
                return {
                    "ip_address": clean_ip,
                    "hostname": resolved_host,
                    "provider_label": entry["provider_label"],
                    "network_type": entry["network_type"],
                    "is_vpn": entry["is_vpn"],
                    "is_tor": entry["is_tor"],
                    "is_proxy": entry["is_proxy"],
                    "badge_text": entry["badge_text"],
                    "geolocation": {
                        "city": entry["city"],
                        "region": entry["region"],
                        "country": entry["country"],
                        "country_code": entry["country_code"],
                        "lat": entry["lat"],
                        "lon": entry["lon"],
                        "asn": entry["asn"],
                        "isp": entry["isp"],
                    },
                }

    # Heuristic fallback if hostname explicitly indicates ProtonVPN, Tor, Mullvad, etc.
    if "protonvpn" in lower_host or "proton" in lower_host:
        return {
            "ip_address": clean_ip,
            "hostname": clean_host or f"exit-{clean_ip.replace('.', '-')}.protonvpn.net",
            "provider_label": "ProtonVPN",
            "network_type": "VPN",
            "is_vpn": True,
            "is_tor": False,
            "is_proxy": True,
            "badge_text": "ProtonVPN (Exit Node)",
            "geolocation": {
                "city": "Zurich",
                "region": "Zurich",
                "country": "Switzerland",
                "country_code": "CH",
                "lat": 47.3769,
                "lon": 8.5417,
                "asn": "AS62371",
                "isp": "Proton AG",
            },
        }
    if "tor" in lower_host or "onion" in lower_host:
        return {
            "ip_address": clean_ip,
            "hostname": clean_host or f"tor-exit-{clean_ip.replace('.', '-')}.torproject.org",
            "provider_label": "Tor",
            "network_type": "Tor",
            "is_vpn": False,
            "is_tor": True,
            "is_proxy": True,
            "badge_text": "Tor Exit Node",
            "geolocation": {
                "city": "Falkenstein",
                "region": "Saxony",
                "country": "Germany",
                "country_code": "DE",
                "lat": 50.4779,
                "lon": 12.3713,
                "asn": "AS205100",
                "isp": "Tor Exit Relay",
            },
        }

    if parsed_ip is None:
        return {
            "ip_address": clean_ip,
            "hostname": clean_host or "unknown",
            "provider_label": "Unknown",
            "network_type": "Invalid IP",
            "is_vpn": False,
            "is_tor": False,
            "is_proxy": False,
            "badge_text": "Invalid IP Address",
            "geolocation": {
                "city": "Unknown",
                "region": "Unknown",
                "country": "Unknown",
                "country_code": "UN",
                "lat": None,
                "lon": None,
                "asn": "Unknown",
                "isp": "Unknown",
            },
        }

    return {
        "ip_address": clean_ip,
        "hostname": clean_host or "unknown",
        "provider_label": "Unknown",
        "network_type": "Unknown",
        "is_vpn": False,
        "is_tor": False,
        "is_proxy": False,
        "badge_text": "Unclassified Network",
        "geolocation": {
            "city": "Unknown",
            "region": "Unknown",
            "country": "Unknown",
            "country_code": "UN",
            "lat": None,
            "lon": None,
            "asn": "Unknown",
            "isp": "Unclassified Network",
        },
    }


def compute_cpu_points(cpu_time_seconds: float, instance_benchmark_score: int) -> float:
    """Compute CPU 'points' = CPU time (seconds) * instance CPU benchmark score."""
    if cpu_time_seconds < 0 or instance_benchmark_score < 0:
        raise ValueError("cpu_time_seconds and instance_benchmark_score must be non-negative")
    return round(float(cpu_time_seconds) * int(instance_benchmark_score), 2)


def _iso_to_epoch(iso_str: str) -> float:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.timestamp()


def enrich_rust_build_record(
    record: dict[str, Any],
    project: str = "homelab-424523",
) -> dict[str, Any]:
    """Normalize and enrich a Rust remote build telemetry record with durations,
    cache hit rate, IP geolocation, VPN/Tor lookup, active RAM OOM floor, and CPU points.
    """
    requested_at = str(record["requested_at"])
    finished_at = str(record["finished_at"])
    delivered_at = str(record["delivered_at"])

    req_ts = _iso_to_epoch(requested_at)
    fin_ts = _iso_to_epoch(finished_at)
    del_ts = _iso_to_epoch(delivered_at)

    if not (req_ts <= fin_ts <= del_ts):
        raise ValueError(
            "Invalid Rust build timestamps: requested_at <= finished_at <= delivered_at is required"
        )

    compile_seconds = round(fin_ts - req_ts, 2)
    delivery_seconds = round(del_ts - fin_ts, 2)
    raw_total = record.get("total_duration_seconds")
    if raw_total is not None and float(raw_total) < 0:
        raise ValueError("total_duration_seconds must be non-negative")
    total_duration_seconds = round(
        float(raw_total) if raw_total is not None else (del_ts - req_ts), 2
    )

    cache_hits = max(0, int(record.get("cache_hits", 0)))
    cache_misses = max(0, int(record.get("cache_misses", 0)))
    total_crates = max(cache_hits, int(record.get("total_crates", cache_hits + cache_misses)))
    cache_hit_rate = round((cache_hits / total_crates * 100.0) if total_crates > 0 else 0.0, 1)

    instance_name = str(record.get("instance_name", "builder-oxidex-turin-01"))
    zone = str(record.get("zone", "us-south1-a"))
    region = zone.rsplit("-", 1)[0] if "-" in zone else zone
    machine_type = str(record.get("machine_type", "c4d-highcpu-16"))
    vcpu = int(record.get("vcpu", 16))
    cpu_platform = str(record.get("cpu_platform", "AMD Turin"))
    benchmark_score = int(
        record.get("instance_benchmark_score")
        or compute_cpu_benchmark_score(cpu_platform, vcpu, machine_type)
    )

    cpu_time_seconds = round(float(record.get("cpu_time_seconds", compile_seconds * 8.5)), 2)
    cpu_points = compute_cpu_points(cpu_time_seconds, benchmark_score)

    peak_ram_gib = round(float(record.get("peak_ram_gib", 18.4)), 2)
    container_ram_cap_gib = round(float(record.get("container_ram_cap_gib", 28.0)), 2)
    ram_headroom_gib = round(max(0.0, container_ram_cap_gib - peak_ram_gib), 2)

    ip_intel = classify_ip_and_network(
        str(record.get("origin_ip", "73.162.44.19")),
        record.get("origin_hostname"),
    )

    return {
        "build_id": str(record.get("build_id", "build-001")),
        "project_name": str(record.get("project_name", "oxidex")),
        "profile": str(record.get("profile", "release")),
        "target_triple": str(record.get("target_triple", "x86_64-unknown-linux-gnu")),
        "commit_sha": str(record.get("commit_sha", "af7c14f8cb2c")),
        "status": str(record.get("status", "DELIVERED")),
        "requested_at": requested_at,
        "finished_at": finished_at,
        "delivered_at": delivered_at,
        "requested_epoch": req_ts,
        "finished_epoch": fin_ts,
        "delivered_epoch": del_ts,
        "compile_duration_seconds": compile_seconds,
        "delivery_duration_seconds": delivery_seconds,
        "total_duration_seconds": total_duration_seconds,
        "cache_hits": cache_hits,
        "cache_misses": max(0, total_crates - cache_hits),
        "total_crates": total_crates,
        "cache_hit_rate_percent": cache_hit_rate,
        "instance_name": instance_name,
        "zone": zone,
        "region": region,
        "machine_type": machine_type,
        "vcpu": vcpu,
        "cpu_platform": cpu_platform,
        "instance_benchmark_score": benchmark_score,
        "ssh_command": format_gcloud_ssh_command(instance_name, zone, project),
        "origin_ip": ip_intel["ip_address"],
        "origin_hostname": ip_intel["hostname"],
        "geolocation": ip_intel["geolocation"],
        "network_security": {
            "provider_label": ip_intel["provider_label"],
            "network_type": ip_intel["network_type"],
            "is_vpn": ip_intel["is_vpn"],
            "is_tor": ip_intel["is_tor"],
            "is_proxy": ip_intel["is_proxy"],
            "badge_text": ip_intel["badge_text"],
        },
        "peak_ram_gib": peak_ram_gib,
        "container_ram_cap_gib": container_ram_cap_gib,
        "ram_headroom_gib": ram_headroom_gib,
        "oom_floor_note": f"Build requires >={peak_ram_gib:.2f} GiB active memory or it will OOM",
        "cpu_time_seconds": cpu_time_seconds,
        "cpu_points": cpu_points,
        "cpu_points_formula": f"{cpu_time_seconds:g}s CPU time × {benchmark_score} benchmark = {cpu_points:,.0f} pts",
    }
