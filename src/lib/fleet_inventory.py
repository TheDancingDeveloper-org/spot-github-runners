"""Read-only Compute Engine inventory of the runner hosts and dedicated builders the dashboard shows.

The dashboard's instance list comes only from this listing: :func:`gce_fleet_source` lists every instance in the
project once (one aggregated call, plus one aggregated disk listing and a cached machine-type lookup per shape),
keeps the ``oxidex-runners-*`` and ``builder-*`` VMs, and returns one normalized observation per VM. Nothing here
creates, changes, or deletes a cloud resource.
"""

from __future__ import annotations

from datetime import datetime
import os
import re
from typing import Any, Callable, Mapping

from .runner_fleet import (RUNNER_HOST_NAME_PREFIXES, gce_metadata_items, gce_timestamp,
                           patch_record_from_gce_item, runner_deployment_from_gce_item)

ENV_FLEET_INVENTORY = "DASHBOARD_FLEET_INVENTORY"
# Runner hosts (runner_manager) and dedicated Rust builders (render_builder_startup). Control VMs
# (``spot-control-*``) and anything else in the project are never part of the fleet view.
FLEET_NAME_PREFIXES = RUNNER_HOST_NAME_PREFIXES + ("builder-",)
# Status of an inventory record for a fleet disk that no VM uses (for example a dedicated builder's cache disk,
# created with auto-delete=no, after its VM was deleted): it bills storage until it is deleted.
DETACHED_DISK_STATUS = "DETACHED"
# Dedicated builders run one build container capped at 16 vCPU / 28 GiB (see AGENTS.md).
BUILDER_CONTAINER_VCPU = 16
BUILDER_CONTAINER_RAM_GIB = 28.0
DEFAULT_DISK_TYPE = "pd-balanced"
# Performance included in a Hyperdisk Balanced disk's capacity price; above it IOPS and throughput bill separately.
HYPERDISK_BALANCED_INCLUDED_IOPS = 3000
HYPERDISK_BALANCED_INCLUDED_THROUGHPUT = 140


def _disk_detail(disk_types: Mapping[str, Any], source: str) -> dict[str, Any]:
    detail = disk_types.get(source)
    if isinstance(detail, str):
        return {"type": detail}
    return dict(detail) if isinstance(detail, Mapping) else {}


def _unpriced_performance(detail: Mapping[str, Any]) -> bool:
    """True when a disk bills provisioned performance the dashboard does not price (or reports none to check)."""
    kind = str(detail.get("type", ""))
    if kind == "pd-extreme":
        return True  # Extreme PD bills its provisioned IOPS separately.
    if kind == "hyperdisk-throughput":
        return True  # Provisioned throughput is always billed separately.
    if kind.startswith("hyperdisk-"):
        try:
            return (int(detail["provisionedIops"]) > HYPERDISK_BALANCED_INCLUDED_IOPS
                    or int(detail["provisionedThroughput"]) > HYPERDISK_BALANCED_INCLUDED_THROUGHPUT)
        except (KeyError, TypeError, ValueError):
            return True  # Unknown provisioned performance: never assume it is free.
    return False


def _disk_billing_metadata(detail: Mapping[str, Any], source: str, *,
                           disk_types_known: bool, ambiguous_roles: list[str]) -> dict[str, Any]:
    """Physical disk evidence, independent of which attachment carries its charge."""
    return {
        "storage_billing_ambiguous_roles": list(ambiguous_roles),
        "unpriced_disk_performance": _unpriced_performance(detail),
        "regional": bool(detail.get("regional")) or "/regions/" in source,
        "disk_types_known": bool(disk_types_known and detail.get("type")),
        **{key: detail[key] for key in ("provisionedIops", "provisionedThroughput") if key in detail},
    }


def builder_shape_compatible(record: Mapping[str, Any]) -> bool:
    """Whether physical capacity matches a dedicated builder; container limits are checked separately.

    Templates can omit specs_known; an explicit False means the capacity is unverified.
    """
    return (record.get("specs_known") is not False and record.get("machine_type") == "n4-highcpu-16"
            and record.get("vcpu") == 16 and record.get("ram_gib") == 32.0)


_MACHINE_VCPU_RE = re.compile(r"-(\d+)$")


def gce_epoch(value: Any) -> float | None:
    """Epoch seconds of a GCE RFC 3339 timestamp (``2026-10-18T09:12:33.123-07:00``), or None."""
    text = str(value or "").strip()
    if not text or len(text) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def fleet_role(name: str) -> str | None:
    """``cicd_runner`` or ``rust_builder`` for a fleet VM name, None for anything else."""
    if name.startswith("builder-"):
        return "rust_builder"
    if name.startswith(RUNNER_HOST_NAME_PREFIXES):
        return "cicd_runner"
    return None


_CUSTOM_MACHINE_VCPU_RE = re.compile(r"custom-(\d+)-\d+(?:-ext)?$")


def _vcpu_from_machine_type(machine_type: str) -> int:
    """vCPUs encoded in a machine type name (0 when unknown): ``n4-standard-16`` -> 16, ``n2-custom-32-112640`` -> 32."""
    if "custom" in machine_type:
        custom = _CUSTOM_MACHINE_VCPU_RE.search(machine_type)
        return int(custom.group(1)) if custom else 0
    match = _MACHINE_VCPU_RE.search(machine_type)
    return int(match.group(1)) if match else 0


def observe_fleet_items(
    items: list[dict],
    *,
    disk_types: Mapping[str, Any] | None = None,
    shapes: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    disk_types_known: bool = True,
) -> list[dict[str, Any]]:
    """Normalize a Compute Engine aggregated instance listing into fleet observations.

    Each observation carries the VM's identity (``instance_id``, ``name``, ``zone``, ``region``), its GCE
    ``status`` verbatim, its shape and disks, and its lifetime timestamps as epoch seconds. Shapes come from
    ``shapes[(zone, machine_type)]`` (``{"vcpu", "ram_gib"}``); when unknown, vCPU is read from the machine type
    name and ``specs_known`` is False. Disk types come from ``disk_types[selfLink]``.
    """
    disk_types = disk_types or {}
    shapes = shapes or {}
    observed: list[dict[str, Any]] = []
    # A shared disk bills once. Select its role before choosing a stable owner within that role, so arbitrary
    # instance IDs cannot move explicitly attributed storage between the runner and builder budgets.
    billed_disks: set[str] = set()
    fleet_items = sorted(
        [vm for vm in items if isinstance(vm, dict) and isinstance(vm.get("name"), str)
         and fleet_role(vm["name"]) is not None and str(vm.get("id") or "").strip()],
        key=lambda vm: (str(vm["id"]), vm["name"]))
    attachment_counts: dict[str, int] = {}
    disk_participants: dict[str, list[tuple[tuple[str, str], str]]] = {}
    disk_roles: dict[str, set[str]] = {}
    disk_user_roles: dict[str, set[str]] = {}
    for vm in fleet_items:
        for disk in vm.get("disks") or []:
            if (isinstance(disk, dict) and disk.get("source")
                    and str(disk.get("type", "")).upper() != "SCRATCH"):
                source = str(disk["source"])
                detail = _disk_detail(disk_types, source)
                identity = str(detail.get("id") or source)
                attachment_counts[identity] = attachment_counts.get(identity, 0) + 1
                disk_participants.setdefault(identity, []).append(((str(vm["id"]), vm["name"]),
                                                                    fleet_role(vm["name"])))
                # The disk listing is later than the VM snapshot. Its users can establish cross-role sharing
                # even when the other VM or its attachment is absent from that snapshot.
                for user in detail.get("users") or []:
                    user_role = fleet_role(str(user).rstrip("/").rsplit("/", 1)[-1])
                    if user_role:
                        disk_user_roles.setdefault(identity, set()).add(user_role)
                # Catalog name and source identify the actual disk. deviceName is only an attachment alias;
                # disk labels are not in the API projection and cannot establish ownership here.
                disk_name = str(detail.get("name") or source.rstrip("/").rsplit("/", 1)[-1])
                named_role = ("cicd_runner" if disk_name == "runner-data" or disk_name.startswith("runner-data-")
                              else fleet_role(disk_name))
                if named_role:
                    disk_roles.setdefault(identity, set()).add(named_role)
    billing_owners: dict[str, tuple[str, str]] = {}
    ambiguous_disks: dict[str, set[str]] = {}
    for identity, participants in disk_participants.items():
        candidates = participants
        participating_roles = {role for _, role in participants} | disk_user_roles.get(identity, set())
        if len(participating_roles) > 1:
            named_roles = disk_roles.get(identity, set())
            candidates = [p for p in participants if p[1] in named_roles] if len(named_roles) == 1 else []
            if not candidates:
                # Keep fleet burn deduplicated. Persist all affected roles on the visible carrier(s), including
                # when the named role has no attachment yet and therefore cannot own the numerical charge.
                ambiguous_disks[identity] = participating_roles
                candidates = participants
        billing_owners[identity] = candidates[0][0]
    for item in fleet_items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = item["name"]
        role = fleet_role(name)
        if role is None:
            continue
        instance_id = str(item.get("id", "") or "").strip()
        if not instance_id:
            continue
        zone = str(item.get("zone", "")).rsplit("/", 1)[-1]
        machine_type = str(item.get("machineType", "")).rsplit("/", 1)[-1]
        shape = shapes.get((zone, machine_type)) or {}
        vcpu = shape.get("vcpu")
        ram_gib = shape.get("ram_gib")
        specs_known = isinstance(vcpu, int) and isinstance(ram_gib, (int, float))
        if not specs_known:
            vcpu, ram_gib = _vcpu_from_machine_type(machine_type), 0.0
        boot_disk_type, boot_disk_gb = DEFAULT_DISK_TYPE, 0
        boot_disk_billed_gb = 0
        data_disk_type, data_disk_gb = DEFAULT_DISK_TYPE, 0
        data_disks: dict[str, int] = {}
        local_ssd_gb = 0
        # Each persistent disk with its own creation time, so history before a later-attached disk is not billed
        # at its rate.
        disks_detail: list[dict[str, Any]] = []
        shared_attachments: list[dict[str, Any]] = []
        for disk in item.get("disks", []) if isinstance(item.get("disks"), list) else []:
            if not isinstance(disk, dict):
                continue
            size_raw = str(disk.get("diskSizeGb", "") or "")
            size = int(size_raw) if size_raw.isdigit() else 0
            if str(disk.get("type", "")).upper() == "SCRATCH":
                # Local SSD: no persistent disk behind it, billed (and lost on stop) differently.
                local_ssd_gb += size
                continue
            detail = _disk_detail(disk_types, str(disk.get("source", "")))
            identity = str(detail.get("id") or "") or str(disk.get("source", "") or "")
            shared_elsewhere = bool(identity) and (
                identity in billed_disks
                or billing_owners.get(identity, (str(item["id"]), name)) != (str(item["id"]), name))
            if identity and not shared_elsewhere:
                billed_disks.add(identity)
            dtype = str(detail.get("type") or "") or DEFAULT_DISK_TYPE
            disks_detail.append({"type": dtype, "gb": size, "created_epoch": gce_epoch(detail.get("creationTimestamp")),
                                 "source": str(disk.get("source", "") or ""), "id": str(detail.get("id", "") or ""),
                                 **_disk_billing_metadata(detail, str(disk.get("source", "") or ""),
                                     disk_types_known=disk_types_known,
                                     ambiguous_roles=sorted(ambiguous_disks.get(identity, ()))),
                                 # None when the listing did not say (older observations).
                                 "boot": bool(disk.get("boot")),
                                 "billing_owner": not shared_elsewhere,
                                 "auto_delete": disk.get("autoDelete") if isinstance(disk.get("autoDelete"), bool)
                                 else None})
            if attachment_counts.get(identity, 0) > 1:
                shared_attachments.append(dict(disks_detail[-1]))
            if disk.get("boot"):
                boot_disk_type, boot_disk_gb = dtype, size
                boot_disk_billed_gb = 0 if shared_elsewhere else size
            else:
                # Several data disks: report the first one's type and the total size; data_disks keeps the size of
                # each type so each is priced at its own rate.
                if data_disk_gb == 0:
                    data_disk_type = dtype
                data_disk_gb += size
                if not shared_elsewhere:
                    data_disks[dtype] = data_disks.get(dtype, 0) + size
        # Physical evidence remains on every attachment. Only owned or ambiguously attributed disks affect
        # this VM's billing trust; a private disk never inherits another disk's cross-role uncertainty.
        billing_details = [d for d in disks_detail if d["billing_owner"] or d["storage_billing_ambiguous_roles"]]
        storage_billing_ambiguous_roles = sorted({role for d in billing_details
                                                  for role in d["storage_billing_ambiguous_roles"]})
        scheduling = item.get("scheduling") if isinstance(item.get("scheduling"), dict) else {}
        spot = (str(scheduling.get("provisioningModel", "")).upper() == "SPOT"
                or bool(scheduling.get("preemptible", False)))
        deployment = runner_deployment_from_gce_item(item)
        metadata = gce_metadata_items(item)
        if role == "rust_builder":
            runner_limits_known = builder_shape_compatible({"specs_known": specs_known, "machine_type": machine_type,
                                                            "vcpu": vcpu, "ram_gib": ram_gib})
            runners_count = 1 if runner_limits_known else 0
            runner_vcpu = BUILDER_CONTAINER_VCPU if runner_limits_known else 0
            runner_ram_gib = BUILDER_CONTAINER_RAM_GIB if runner_limits_known else 0.0
        else:
            # Instance metadata records the listener count only; per-job limits depend on the tier and admission
            # (listeners are not memory capacity), so they are unknown here rather than guessed.
            runners_count = int(deployment.get("runners_count", 0) or 0)
            runner_vcpu, runner_ram_gib, runner_limits_known = 0, 0.0, False
        # The recorded count is what provisioning asked for; it counts as current runners only once the host
        # confirmed its bootstrap in this boot (applied patch record, nothing pending; see runner_fleet).
        patch = patch_record_from_gce_item(item)
        bootstrap_confirmed = bool(
            patch["patch_sha256"] and not patch["patch_pending_sha256"]
            and patch["patch_boot"] and patch["patch_boot"] == gce_timestamp(item.get("lastStartTimestamp"))
        )
        runners_count_known = (runner_limits_known if role == "rust_builder"
                               else "runners_count" in deployment and bootstrap_confirmed)
        observed.append({
            "instance_id": instance_id,
            "name": name,
            "role": role,
            "status": str(item.get("status", "UNKNOWN") or "UNKNOWN").upper(),
            "zone": zone,
            "region": zone.rsplit("-", 1)[0] if "-" in zone else zone,
            "machine_type": machine_type,
            "cpu_platform": str(item.get("cpuPlatform", "") or ""),
            "vcpu": int(vcpu or 0),
            "ram_gib": float(ram_gib or 0.0),
            "specs_known": specs_known,
            "spot": spot,
            "boot_disk_type": boot_disk_type,
            "boot_disk_gb": boot_disk_gb,
            "boot_disk_billed_gb": boot_disk_billed_gb,
            "data_disk_type": data_disk_type,
            "data_disk_gb": data_disk_gb,
            # data_disks is the billed breakdown; data_disk_gb is everything attached (the VM's shape).
            "data_disks": data_disks,
            "data_disks_attached_gb": data_disk_gb,
            "shared_attachments": shared_attachments,
            "storage_billing_ambiguous": bool(storage_billing_ambiguous_roles),
            "storage_billing_ambiguous_roles": storage_billing_ambiguous_roles,
            "local_ssd_gb": local_ssd_gb,
            "unpriced_disk_performance": any(d["unpriced_disk_performance"] for d in billing_details),
            "regional_disks": any(d["regional"] for d in billing_details),
            # Unknown physical types stay on each detail; only relevant disks can make billing incomplete.
            "disk_types_known": all(d["disk_types_known"] for d in billing_details),
            "disks_detail": disks_detail,
            "runners_count": runners_count,
            "runner_vcpu": runner_vcpu,
            "runner_ram_gib": runner_ram_gib,
            "runner_limits_known": runner_limits_known,
            "runners_count_known": runners_count_known,
            "runner_label": str(deployment.get("runner_label", "") or ""),
            "target_label": str((item.get("labels") or {}).get("spot-target", "")
                                if isinstance(item.get("labels"), dict) else ""),
            "created_epoch": gce_epoch(item.get("creationTimestamp")),
            "last_start_epoch": gce_epoch(item.get("lastStartTimestamp")),
            "last_stop_epoch": gce_epoch(item.get("lastStopTimestamp")),
            "last_suspended_epoch": gce_epoch(item.get("lastSuspendedTimestamp")),
            "patch_target": metadata.get("spot-runner-patch-target", ""),
        })
    return observed


def _permission_denied(exc: BaseException) -> bool:
    """True for a Compute Engine HTTP 403 (the service account lacks the permission)."""
    return "HTTP 403" in str(exc)


class FleetListing(list):
    """Fleet observations plus whether the project's disks could be listed (detached disks are counted only then)."""

    disks_listed: bool = True
    disk_inventory: dict[str, Any] | None = None


def observe_detached_disks(disk_details: Mapping[str, Any],
                           attached_sources: frozenset[str] = frozenset(),
                           snapshot_names: frozenset[str] | None = None, *,
                           tracked_roles: Mapping[str, str] | None = None,
                           tracked_ambiguities: Mapping[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """Observations for fleet-named persistent disks that no VM uses (status ``DETACHED``, storage only).

    An attached disk is billed through its VM's record; once no VM uses it (a builder cache disk outlives its
    VM), it gets its own record, which bills until the disk is gone from the listing.
    """
    observed = []
    for link, raw in disk_details.items():
        detail = raw if isinstance(raw, Mapping) else {}
        name = str(detail.get("name", "") or "")
        users = [str(u).rstrip("/").rsplit("/", 1)[-1] for u in (detail.get("users") or [])]
        user_roles = sorted({fleet_role(user) for user in users if fleet_role(user) is not None})
        disk_name = name or str(link).rstrip("/").rsplit("/", 1)[-1]
        named_role = ("cicd_runner" if disk_name == "runner-data" or disk_name.startswith("runner-data-")
                      else fleet_role(disk_name))
        # A later disk listing can show users in both roles before either attachment reaches the VM snapshot.
        # Keep one numerical carrier, but flag both role budgets via this disk record when attribution is unknown.
        ambiguous_roles = user_roles if len(user_roles) > 1 and named_role not in user_roles else []
        if not user_roles and named_role is None:
            # Detaching is not evidence of a workload role. Keep the previous physical disk's unresolved roles
            # until its current users or an explicit disk name resolve them; never match a reused name by URL.
            ambiguous_roles = sorted(set((tracked_ambiguities or {}).get("id:" + str(detail.get("id", "")), [])))
        storage_billing_ambiguous = len(ambiguous_roles) > 1
        role = (named_role if named_role in user_roles else user_roles[0]) if user_roles else (
            named_role or (tracked_roles or {}).get("id:" + str(detail.get("id", ""))))
        if role is None or not detail.get("id"):
            continue
        # Users from the later disk listing cannot prove the earlier instance snapshot contained this
        # attachment, even when the user VM was already present. Only attached_sources proves billing ownership.
        if users and not user_roles:
            # A disk with only external users is outside fleet billing. Mixed users still include fleet storage.
            continue
        # Keep fleet-used disks here until an instance snapshot actually includes their attachment.
        if str(link) in attached_sources:
            # The instance listing (taken just before) still shows it attached: its VM's record bills it.
            continue
        zone = str(detail.get("zone", "") or "").rsplit("/", 1)[-1]
        region = zone.rsplit("-", 1)[0] if "-" in zone else zone
        if not zone:
            # A regional disk has a region, not a zone (also in its self-link: .../regions/<region>/disks/...).
            region = str(detail.get("region", "") or "").rsplit("/", 1)[-1]
            if not region and "/regions/" in str(link):
                region = str(link).split("/regions/", 1)[1].split("/", 1)[0]
        size_raw = str(detail.get("sizeGb", "") or "")
        size = int(size_raw) if size_raw.isdigit() else 0
        kind = str(detail.get("type") or "") or DEFAULT_DISK_TYPE
        created = gce_epoch(detail.get("creationTimestamp"))
        disk_metadata = _disk_billing_metadata(detail, str(link), disk_types_known=True,
                                              ambiguous_roles=ambiguous_roles if storage_billing_ambiguous else [])
        observed.append({
            "instance_id": f"disk-{detail['id']}",
            "name": name,
            "role": role,
            "status": DETACHED_DISK_STATUS,
            "kind": "disk",
            "zone": zone,
            "region": region,
            "machine_type": "",
            "cpu_platform": "",
            "vcpu": 0,
            "ram_gib": 0.0,
            "specs_known": True,
            "spot": True,
            "boot_disk_type": kind,
            "boot_disk_gb": 0,
            "data_disk_type": kind,
            "data_disk_gb": size,
            "data_disks": {kind: size},
            "storage_billing_ambiguous": bool(disk_metadata["storage_billing_ambiguous_roles"]),
            "storage_billing_ambiguous_roles": list(disk_metadata["storage_billing_ambiguous_roles"]),
            "local_ssd_gb": 0,
            "unpriced_disk_performance": disk_metadata["unpriced_disk_performance"],
            "regional_disks": disk_metadata["regional"],
            "disk_types_known": disk_metadata["disk_types_known"],
            "disks_detail": [{"type": kind, "gb": size, "created_epoch": created, "source": str(link),
                              "id": str(detail["id"]), **disk_metadata}],
            "runners_count": 0,
            "runners_count_known": True,
            "runner_vcpu": 0,
            "runner_ram_gib": 0.0,
            "runner_limits_known": True,
            "created_epoch": created,
            "last_start_epoch": created,
            "last_stop_epoch": created,
            "last_suspended_epoch": None,
            "source": str(link),
        })
    return observed


def gce_fleet_source(project: str) -> Callable[[], list[dict[str, Any]]]:
    """A callable that lists the live fleet from Compute Engine (gcloud credentials or the metadata server).

    It raises when any zone is unreachable: a partial listing must never make a VM look deleted.
    """
    shape_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def source() -> list[dict[str, Any]]:
        import asyncio

        from . import compute_api

        async def listing() -> list[dict[str, Any]]:
            auth = compute_api.get_auth(project)
            api = compute_api.AsyncGoogleApi(auth.token, 4)
            try:
                items, unreachables = await compute_api.list_instances(
                    api, auth.project, instance_fields=compute_api.FLEET_INSTANCE_LIST_FIELDS)
                if unreachables:
                    raise RuntimeError(f"zones unreachable: {', '.join(unreachables[:5])}")
                fleet_items = [i for i in items if isinstance(i, dict)
                               and str(i.get("name", "")).startswith(FLEET_NAME_PREFIXES)]
                disk_types: dict[str, Any] = {}
                types_known = True
                try:
                    # Always: a fleet disk can outlive the last fleet VM (a builder cache disk) and still bill.
                    disk_types = await compute_api.list_disk_types(api, auth.project)
                except Exception as exc:
                    if not _permission_denied(exc):
                        # Transient failures still reject the whole refresh rather than publishing a partial view.
                        raise RuntimeError(f"disk listing failed: {type(exc).__name__}: {exc}") from exc
                    # Permission can be revoked after a successful listing. Keep the current VM snapshot;
                    # unknown disk types and the absent catalog let reconciliation preserve earlier evidence.
                    types_known = False
                wanted = {(str(i.get("zone", "")).rsplit("/", 1)[-1], str(i.get("machineType", "")).rsplit("/", 1)[-1])
                          for i in fleet_items}
                for zone, machine_type in wanted - shape_cache.keys():
                    if not zone or not machine_type:
                        continue
                    try:
                        shape_cache[(zone, machine_type)] = await compute_api.machine_type_capacity(
                            api, auth.project, zone, machine_type)
                    except Exception:  # noqa: BLE001 - reported below, retried next time
                        pass
                missing = sorted(f"{mt} in {zone}" for zone, mt in wanted - shape_cache.keys() if zone and mt)
                if missing:
                    # Unknown RAM would price as free: fail the refresh (records stay as they were) instead.
                    raise RuntimeError(f"machine type capacity unavailable: {', '.join(missing[:5])}")
                result = FleetListing(observe_fleet_items(fleet_items, disk_types=disk_types, shapes=shape_cache,
                                                          disk_types_known=types_known)
                                      + (observe_detached_disks(disk_types, frozenset(
                                          str(d.get("source", "")) for i in fleet_items
                                          for d in (i.get("disks") or []) if isinstance(d, dict)),
                                          frozenset(f"{str(i.get('zone', '')).rsplit('/', 1)[-1]}/{i.get('name', '')}"
                                                    for i in items if isinstance(i, dict)))
                                         if types_known else []))
                result.disks_listed = types_known
                result.disk_inventory = disk_types if types_known else None
                return result
            finally:
                while not api.connections.empty():
                    _reader, writer = api.connections.get_nowait()
                    writer.close()

        return asyncio.run(listing())

    return source


def fleet_source_from_env(project: str, env: Mapping[str, str] | None = None) -> Callable[[], list[dict[str, Any]]] | None:
    """The live fleet source when ``DASHBOARD_FLEET_INVENTORY=gce`` (set on the control VM), else None."""
    env = os.environ if env is None else env
    return gce_fleet_source(project) if str(env.get(ENV_FLEET_INVENTORY, "") or "").strip().lower() == "gce" else None
