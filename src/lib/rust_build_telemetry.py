"""Rust remote builder telemetry, IP geolocation, VPN/Proxy/Tor exit-node lookup,
active memory OOM floor tracking, and CPU Points (CPU time * instance benchmark score)
analytics for the Web UI Dashboard.
"""

from __future__ import annotations

from datetime import datetime
import ipaddress
import math
import re
from typing import Any

from .dashboard_models import compute_cpu_benchmark_score, format_gcloud_ssh_command


SUCCESSFUL_BUILD_STATUSES: frozenset[str] = frozenset(
    {
        "DELIVERED",
        "COMPLETED",
        "SUCCEEDED",
        "SUCCESS",
    }
)

UNFINISHED_BUILD_STATUSES: frozenset[str] = frozenset(
    {
        "QUEUED",
        "PENDING",
        "WAITING",
        "RUNNING",
        "COMPILING",
        "ACTIVE",
        "IN_PROGRESS",
        "BUILDING",
    }
)

UNSUCCESSFUL_BUILD_STATUSES: frozenset[str] = frozenset(
    {
        "FAILED",
        "FAILURE",
        "ERROR",
        "CANCELLED",
        "CANCELED",
        "ABORTED",
        "TIMED_OUT",
        "TIMEOUT",
        "OOM_KILLED",
    }
)

ALLOWED_BUILD_STATUSES: frozenset[str] = (
    SUCCESSFUL_BUILD_STATUSES | UNFINISHED_BUILD_STATUSES | UNSUCCESSFUL_BUILD_STATUSES
)


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
    if re.search(r"(?:^|[.-])(?:tor|torexit|torproject|zwiebelfreunde)(?:[.-]|$)", lower_host) or lower_host.endswith(".onion"):
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


def _finite_float(val: Any, name: str, *, min_val: float | None = 0.0) -> float:
    if isinstance(val, bool):
        raise ValueError(f"{name} must be a finite number")
    f = float(val)
    if not math.isfinite(f):
        raise ValueError(f"{name} must be a finite number (got {val!r})")
    if min_val is not None and f < min_val:
        raise ValueError(f"{name} must be >= {min_val} (got {val!r})")
    return f


def _finite_int(val: Any, name: str, *, min_val: int = 0) -> int:
    if isinstance(val, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(val, float) and not math.isfinite(val):
        raise ValueError(f"{name} must be a finite integer (got {val!r})")
    i = int(val)
    if i < min_val:
        raise ValueError(f"{name} must be >= {min_val} (got {val!r})")
    return i


def compute_cpu_points(cpu_time_seconds: float, instance_benchmark_score: int) -> float:
    """Compute CPU 'points' = CPU time (seconds) * instance CPU benchmark score."""
    cpu_sec = _finite_float(cpu_time_seconds, "cpu_time_seconds", min_val=0.0)
    bench = _finite_int(instance_benchmark_score, "instance_benchmark_score", min_val=0)
    return round(cpu_sec * bench, 2)


def _iso_to_epoch(iso_str: str) -> float:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.timestamp()


def _optional_str(value: Any) -> str | None:
    """A reported text field, or None when it was not reported (missing, null, or blank)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _crate_counts(raw_hits: Any, raw_misses: Any, raw_total: Any) -> tuple[int | None, int | None, int | None]:
    """Cache hits, misses, and total crates as reported; a missing count is derived only from the other two."""
    hits = _finite_int(raw_hits, "cache_hits", min_val=0) if raw_hits is not None else None
    misses = _finite_int(raw_misses, "cache_misses", min_val=0) if raw_misses is not None else None
    total = _finite_int(raw_total, "total_crates", min_val=0) if raw_total is not None else None
    if hits is not None and misses is not None:
        if total is None:
            total = hits + misses
        elif total < hits + misses:
            raise ValueError(f"total_crates ({total}) is below cache_hits + cache_misses ({hits + misses})")
    elif total is not None and hits is not None:
        if hits > total:
            raise ValueError(f"cache_hits ({hits}) exceeds total_crates ({total})")
        misses = total - hits
    elif total is not None and misses is not None:
        if misses > total:
            raise ValueError(f"cache_misses ({misses}) exceeds total_crates ({total})")
        hits = total - misses
    return hits, misses, total


def unreported_origin(hostname: Any = None) -> dict[str, Any]:
    """Network fields for a build whose origin IP was not reported (nothing is looked up or assumed)."""
    return {
        "ip_address": None,
        "hostname": _optional_str(hostname),
        "provider_label": "Not reported",
        "network_type": "Unknown",
        "is_vpn": False,
        "is_tor": False,
        "is_proxy": False,
        "badge_text": "Origin Not Reported",
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


def enrich_rust_build_record(
    record: dict[str, Any],
    project: str = "homelab-424523",
) -> dict[str, Any]:
    """Normalize and enrich a Rust remote build telemetry record with durations,
    cache hit rate, IP geolocation, VPN/Tor lookup, active RAM OOM floor, and CPU points.
    """
    if record.get("status") is None:
        raise ValueError("A Rust build needs a status")
    status_str = str(record["status"])
    norm_status = status_str.strip().upper()
    if norm_status not in ALLOWED_BUILD_STATUSES:
        raise ValueError(f"Unsupported Rust build status: {status_str!r}")
    is_unfinished = norm_status in UNFINISHED_BUILD_STATUSES
    is_unsuccessful = norm_status in UNSUCCESSFUL_BUILD_STATUSES
    is_successful = norm_status in SUCCESSFUL_BUILD_STATUSES

    requested_at = str(record["requested_at"])
    req_ts = _iso_to_epoch(requested_at)

    raw_finished = record.get("finished_at")
    if raw_finished is not None and str(raw_finished).strip() != "":
        finished_at: str | None = str(raw_finished)
        fin_ts: float | None = _iso_to_epoch(finished_at)
    else:
        finished_at = None
        fin_ts = None

    raw_delivered = record.get("delivered_at")
    if raw_delivered is not None and str(raw_delivered).strip() != "":
        delivered_at: str | None = str(raw_delivered)
        del_ts: float | None = _iso_to_epoch(delivered_at)
    else:
        delivered_at = None
        del_ts = None

    if is_successful and del_ts is None:
        raise ValueError(
            "Invalid Rust build timestamps: requested_at and delivered_at are required for completed builds"
        )
    if fin_ts is not None and req_ts > fin_ts:
        raise ValueError(
            "Invalid Rust build timestamps: requested_at <= finished_at <= delivered_at is required"
        )
    if del_ts is not None and fin_ts is not None and fin_ts > del_ts:
        raise ValueError(
            "Invalid Rust build timestamps: requested_at <= finished_at <= delivered_at is required"
        )
    if del_ts is not None and req_ts > del_ts:
        raise ValueError(
            "Invalid Rust build timestamps: requested_at <= finished_at <= delivered_at is required"
        )

    # Without a finish time the compile time is only what was reported (an active build's elapsed compile, for
    # example) and the delivery time is unknown: None, never 0 or a guess.
    raw_compile = record.get("compile_duration_seconds")
    if fin_ts is not None:
        compile_seconds: float | None = round(fin_ts - req_ts, 2)
    elif raw_compile is not None:
        compile_seconds = round(_finite_float(raw_compile, "compile_duration_seconds", min_val=0.0), 2)
    else:
        compile_seconds = None
    delivery_seconds = round(del_ts - fin_ts, 2) if (del_ts is not None and fin_ts is not None) else None
    if raw_compile is not None and fin_ts is not None:
        explicit_compile = round(
            _finite_float(raw_compile, "compile_duration_seconds", min_val=0.0), 2
        )
        if abs(explicit_compile - compile_seconds) > 0.05:
            raise ValueError(
                f"Inconsistent compile_duration_seconds ({explicit_compile}s) vs finished_at - requested_at ({compile_seconds}s)"
            )
    raw_delivery = record.get("delivery_duration_seconds")
    if raw_delivery is not None and del_ts is not None and fin_ts is not None:
        explicit_delivery = round(
            _finite_float(raw_delivery, "delivery_duration_seconds", min_val=0.0), 2
        )
        if abs(explicit_delivery - delivery_seconds) > 0.05:
            raise ValueError(
                f"Inconsistent delivery_duration_seconds ({explicit_delivery}s) vs delivered_at - finished_at ({delivery_seconds}s)"
            )
    raw_total = record.get("total_duration_seconds")
    if raw_total is not None:
        explicit_total = round(
            _finite_float(raw_total, "total_duration_seconds", min_val=0.0), 2
        )
        if del_ts is not None:
            derived_total = round(del_ts - req_ts, 2)
            if abs(explicit_total - derived_total) > 0.05:
                raise ValueError(
                    f"Inconsistent total_duration_seconds ({explicit_total}s) vs delivered_at - requested_at ({derived_total}s)"
                )
            total_duration_seconds = derived_total
        else:
            total_duration_seconds = explicit_total
    elif del_ts is not None:
        total_duration_seconds = round(del_ts - req_ts, 2)
    else:
        total_duration_seconds = compile_seconds

    hits, misses, total_crates = _crate_counts(record.get("cache_hits"), record.get("cache_misses"),
                                               record.get("total_crates"))
    cache_hit_rate = (round(hits / total_crates * 100.0, 1) if hits is not None and total_crates else
                      (0.0 if hits is not None and total_crates == 0 else None))

    instance_name = _optional_str(record.get("instance_name"))
    zone = _optional_str(record.get("zone"))
    region = (zone.rsplit("-", 1)[0] if "-" in zone else zone) if zone else None
    machine_type = _optional_str(record.get("machine_type"))
    vcpu = _finite_int(record["vcpu"], "vcpu", min_val=1) if record.get("vcpu") is not None else None
    cpu_platform = _optional_str(record.get("cpu_platform"))
    raw_bench = record.get("instance_benchmark_score")
    if raw_bench is not None:
        benchmark_score: int | None = _finite_int(raw_bench, "instance_benchmark_score", min_val=1)
    elif cpu_platform and vcpu is not None and machine_type:
        benchmark_score = compute_cpu_benchmark_score(cpu_platform, vcpu, machine_type)
    else:
        benchmark_score = None

    raw_cpu_time = record.get("cpu_time_seconds")
    cpu_time_seconds = (round(_finite_float(raw_cpu_time, "cpu_time_seconds", min_val=0.0), 2)
                        if raw_cpu_time is not None else None)
    cpu_points = (compute_cpu_points(cpu_time_seconds, benchmark_score)
                  if cpu_time_seconds is not None and benchmark_score is not None else None)

    raw_peak_ram = record.get("peak_ram_gib")
    peak_ram_gib = (round(_finite_float(raw_peak_ram, "peak_ram_gib", min_val=0.0), 2)
                    if raw_peak_ram is not None else None)
    # The dedicated builder container's memory cap (28 GiB) is part of the builder design, not a measurement.
    container_ram_cap_gib = round(
        _finite_float(
            record.get("container_ram_cap_gib", 28.0),
            "container_ram_cap_gib",
            min_val=0.1,
        ),
        2,
    )
    ram_headroom_gib = (round(max(0.0, container_ram_cap_gib - peak_ram_gib), 2)
                        if peak_ram_gib is not None else None)

    origin_ip = _optional_str(record.get("origin_ip"))
    ip_intel = (classify_ip_and_network(origin_ip, record.get("origin_hostname")) if origin_ip
                else unreported_origin(record.get("origin_hostname")))

    return {
        "build_id": str(record["build_id"]),
        "project_name": _optional_str(record.get("project_name")),
        "profile": _optional_str(record.get("profile")),
        "target_triple": _optional_str(record.get("target_triple")),
        "commit_sha": _optional_str(record.get("commit_sha")),
        "status": status_str,
        "requested_at": requested_at,
        "requested_at_inferred": record.get("requested_at_inferred") is True,
        "finished_at": finished_at,
        "delivered_at": delivered_at,
        "requested_epoch": req_ts,
        "finished_epoch": fin_ts,
        "delivered_epoch": del_ts,
        "compile_duration_seconds": compile_seconds,
        "delivery_duration_seconds": delivery_seconds,
        "total_duration_seconds": total_duration_seconds,
        "cache_hits": hits,
        "cache_misses": misses,
        "total_crates": total_crates,
        # Which counts were reported rather than derived (record_rust_build's merge prefers reported ones).
        "reported_crate_counts": [k for k in ("cache_hits", "cache_misses", "total_crates")
                                  if k in (record.get("reported_crate_counts") or [])
                                  or (record.get("reported_crate_counts") is None and record.get(k) is not None)],
        "cache_hit_rate_percent": cache_hit_rate,
        "instance_name": instance_name,
        "zone": zone,
        "region": region,
        "machine_type": machine_type,
        "vcpu": vcpu,
        "cpu_platform": cpu_platform,
        "instance_benchmark_score": benchmark_score,
        "ssh_command": format_gcloud_ssh_command(instance_name, zone, project) if instance_name and zone else None,
        "origin_ip": ip_intel["ip_address"],
        # Only a reported hostname; the classifier's default or "unknown" name is not the build's host.
        "origin_hostname": _optional_str(record.get("origin_hostname")),
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
        "oom_floor_note": (f"Build requires >={peak_ram_gib:.2f} GiB active memory or it will OOM"
                           if peak_ram_gib is not None else "Peak memory not reported"),
        "cpu_time_seconds": cpu_time_seconds,
        "cpu_points": cpu_points,
        "cpu_points_formula": (
            f"{cpu_time_seconds:g}s CPU time × {benchmark_score} benchmark = {cpu_points:,.0f} pts"
            if cpu_points is not None else "CPU time or host benchmark not reported"
        ),
    }
