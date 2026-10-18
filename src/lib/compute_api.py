"""Google Compute Engine API authentication, quota handling, and inventory."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from .log_format import error_detail

API_HOST = "compute.googleapis.com"
API_ROOT = "/compute/v1"
CAPACITY_ROOT = "/compute/beta"
DEFAULT_CONCURRENCY = 600
QUOTA_RETRY_DELAY = 61.0
QUOTA_COOLDOWN = 70.0
LOG = logging.getLogger("spot_finder")


@dataclass(frozen=True)
class Offer:
    region: str
    machine_type: str
    vcpu: int
    ram_gib: float
    hourly_usd: float
    zones: tuple[str, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class Auth:
    token: str
    project: str


def log(level: int, message: str, *args: object) -> None:
    if getattr(LOG, "verbosity", 0) >= level:
        LOG.info(message, *args)


def debug(message: str, *args: object) -> None:
    if getattr(LOG, "verbosity", 0) >= 3:
        LOG.debug(message, *args)


def is_quota_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in (
        "ratelimitexceeded", "rate_limit_exceeded", "quota exceeded",
        "resource_exhausted", "http 429",
    ))


def quota_marker_path(project: str) -> str:
    safe_project = "".join(char if char.isalnum() or char in "._-" else "_" for char in project)
    return os.path.join(tempfile.gettempdir(), f"gcp-spot-finder-quota-{safe_project}.timestamp")


def record_quota(project: str) -> None:
    """Persist a shared Python/Rust cooldown timestamp for this project."""
    marker = quota_marker_path(project)
    timestamp = time.time()
    try:
        with open(marker, encoding="ascii") as current:
            timestamp = max(timestamp, float(current.read().strip()))
    except (OSError, ValueError):
        pass
    fd, temporary = tempfile.mkstemp(prefix="gcp-spot-finder-quota-", dir=tempfile.gettempdir())
    try:
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(f"{timestamp:.6f}\n")
        os.replace(temporary, marker)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


async def wait_for_quota_cooldown(project: str) -> None:
    try:
        with open(quota_marker_path(project), encoding="ascii") as marker:
            last_quota = float(marker.read().strip())
    except (OSError, ValueError):
        return
    remaining = QUOTA_COOLDOWN - (time.time() - last_quota)
    if remaining > 0:
        log(1, "GCP Compute quota cooldown: waiting %.1fs before starting requests", remaining)
        await asyncio.sleep(remaining)


METADATA_HOST = "metadata.google.internal"
METADATA_TOKEN_URL = (
    f"http://{METADATA_HOST}/computeMetadata/v1/instance/service-accounts/default/token"
)
METADATA_PROJECT_URL = f"http://{METADATA_HOST}/computeMetadata/v1/project/project-id"
ENV_TOKEN_SOURCE = "SPOT_GCP_TOKEN_SOURCE"
METADATA_TIMEOUT_SECONDS = 5.0
MAX_METADATA_RESPONSE_BYTES = 65_536


def _metadata_get(url: str, timeout: float = METADATA_TIMEOUT_SECONDS) -> str:
    """GET a GCE metadata server URL (never through a proxy) and return the decoded body."""
    request = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - fixed metadata URL
            if int(getattr(response, "status", 200)) != 200:
                raise RuntimeError(f"GCE metadata server returned HTTP {response.status}")
            if response.headers.get("Metadata-Flavor", "Google") != "Google":
                raise RuntimeError("GCE metadata response is missing Metadata-Flavor: Google")
            return response.read(MAX_METADATA_RESPONSE_BYTES).decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GCE metadata server returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"GCE metadata server is unreachable: {type(exc).__name__}") from exc


def metadata_access_token(timeout: float = METADATA_TIMEOUT_SECONDS) -> str:
    """Return the default service account access token from the GCE metadata server."""
    raw = _metadata_get(METADATA_TOKEN_URL, timeout)
    try:
        token = json.loads(raw).get("access_token")
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("GCE metadata server returned an invalid token response") from exc
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("GCE metadata server returned an empty access token")
    return token.strip()


def metadata_project_id(timeout: float = METADATA_TIMEOUT_SECONDS) -> str:
    return _metadata_get(METADATA_PROJECT_URL, timeout).strip()


def token_source_is_metadata(env: dict[str, str] | None = None) -> bool:
    source = (os.environ if env is None else env).get(ENV_TOKEN_SOURCE, "")
    return source.strip().lower() == "metadata"


def get_auth(project_arg: str | None) -> Auth:
    """Get the active project and an access token.

    Uses the logged-in gcloud command-line tool. When gcloud is not on PATH, or
    ``SPOT_GCP_TOKEN_SOURCE=metadata`` is set, uses the GCE metadata server's default service account.
    """
    if token_source_is_metadata():
        return _metadata_auth(project_arg)
    try:
        project = project_arg or subprocess.run(
            ["gcloud", "config", "get", "project"], check=True,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        token = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except FileNotFoundError as exc:
        try:
            return _metadata_auth(project_arg)
        except RuntimeError as meta_exc:
            raise RuntimeError(
                f"gcloud is not installed and the GCE metadata server fallback failed: {meta_exc}"
            ) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "No Application Default Credentials found"
        raise RuntimeError(f"Unable to read project or access token from gcloud: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("gcloud timed out while reading project credentials") from exc
    if not project:
        raise RuntimeError("No active project; use --project PROJECT_ID or `gcloud config set project PROJECT_ID`")
    if not token:
        raise RuntimeError("gcloud returned an empty ADC access token")
    return Auth(token, project)


def _metadata_auth(project_arg: str | None) -> Auth:
    token = metadata_access_token()
    project = project_arg or metadata_project_id()
    if not project:
        raise RuntimeError("No active project; use --project PROJECT_ID (metadata server returned none)")
    return Auth(token, project)


class AsyncGoogleApi:
    """Small stdlib async HTTP/1.1 client; connections are pooled by host."""

    def __init__(self, token: str, concurrency: int):
        self.token = token
        self.semaphore = asyncio.Semaphore(concurrency)
        self.ssl_context = ssl.create_default_context()
        self.connections: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = asyncio.Queue()
        self.connection_count = 0
        self.max_connections = concurrency
        self.lock = asyncio.Lock()

    async def _connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            reader, writer = self.connections.get_nowait()
            if not writer.is_closing():
                return reader, writer
        except asyncio.QueueEmpty:
            pass
        async with self.lock:
            if self.connection_count >= self.max_connections:
                # All sockets are checked out. Queue get waits until a request
                # returns one, bounded by the request semaphore.
                return await self.connections.get()
            self.connection_count += 1
        try:
            return await asyncio.open_connection(API_HOST, 443, ssl=self.ssl_context,
                                                 server_hostname=API_HOST)
        except Exception:
            async with self.lock:
                self.connection_count -= 1
            raise

    async def get_json(self, path: str, query: dict[str, str] | None = None) -> dict:
        if query:
            path += "?" + urlencode(query)
        return await self.request_json("GET", path)

    async def post_json(self, path: str, body: dict) -> dict:
        return await self.request_json("POST", path, json.dumps(body, separators=(",", ":")).encode())

    async def request_json(self, method: str, path: str, body: bytes | None = None) -> dict:
        async with self.semaphore:
            reader, writer = await self._connection()
            payload = body or b""
            headers = [
                f"{method} {path} HTTP/1.1",
                f"Host: {API_HOST}",
                f"Authorization: Bearer {self.token}",
                "Accept: application/json",
                "Accept-Encoding: gzip",
                "Connection: keep-alive",
                "User-Agent: gcp-spot-finder/0.2 (gzip)",
            ]
            if body is not None:
                headers.extend(["Content-Type: application/json", f"Content-Length: {len(payload)}"])
            headers.extend(["", ""])
            writer.write("\r\n".join(headers).encode("ascii") + payload)
            try:
                await writer.drain()
                status_line = await asyncio.wait_for(reader.readline(), timeout=40)
                if not status_line:
                    raise RuntimeError("Google API closed connection without a response")
                parts = status_line.decode("latin1").rstrip().split(" ", 2)
                status = int(parts[1])
                response_headers: dict[str, str] = {}
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    key, _, value = line.decode("latin1").partition(":")
                    response_headers[key.lower()] = value.strip()
                if "content-length" in response_headers:
                    response_body = await asyncio.wait_for(
                        reader.readexactly(int(response_headers["content-length"])), timeout=40)
                elif response_headers.get("transfer-encoding", "").lower() == "chunked":
                    response_body = await self._read_chunked(reader)
                else:
                    raise RuntimeError("Google API response did not include Content-Length or chunked encoding")
                if response_headers.get("content-encoding", "").lower() == "gzip":
                    response_body = gzip.decompress(response_body)
                if status >= 400:
                    detail = error_detail(f"Google API HTTP {status}: " + response_body.decode('utf-8', 'replace'))
                    raise RuntimeError(detail)
                return json.loads(response_body)
            except Exception:
                writer.close()
                async with self.lock:
                    self.connection_count -= 1
                raise
            else:
                pass
            finally:
                # successful response sockets are reusable; failed sockets have
                # already been closed and are not put back into the pool.
                if not writer.is_closing():
                    await self.connections.put((reader, writer))

    @staticmethod
    async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
        chunks: list[bytes] = []
        while True:
            line = await reader.readline()
            size = int(line.split(b";", 1)[0].strip(), 16)
            if size == 0:
                while await reader.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(chunks)
            chunks.append(await reader.readexactly(size))
            await reader.readexactly(2)


async def list_regions(api: AsyncGoogleApi, project: str) -> list[dict]:
    path = f"{API_ROOT}/projects/{quote(project, safe='')}/regions"
    rows: list[dict] = []
    token: str | None = None
    while True:
        query = {
            "maxResults": "500",
            "fields": "nextPageToken,items(name,status)",
        }
        if token:
            query["pageToken"] = token
        data = await api.get_json(path, query)
        rows.extend(r for r in data.get("items", []) if r.get("status") == "UP")
        token = data.get("nextPageToken")
        if not token:
            return rows


INSTANCE_LIST_FIELDS = "name,zone,status,machineType,cpuPlatform,lastStartTimestamp,labels,metadata/items"
# The dashboard's fleet inventory also needs identity, lifetime, Spot, and attached-disk fields.
FLEET_INSTANCE_LIST_FIELDS = (INSTANCE_LIST_FIELDS + ",id,creationTimestamp,lastStopTimestamp,lastSuspendedTimestamp,"
                              "scheduling(provisioningModel,preemptible),disks(boot,diskSizeGb,source,type,autoDelete)")


async def list_instances(api: AsyncGoogleApi, project: str, *,
                         instance_fields: str = INSTANCE_LIST_FIELDS) -> tuple[list[dict], list[str]]:
    """List every VM instance (name, zone, status, machine type, labels, metadata items) with one aggregated
    stream. Callers keep only the metadata keys they need; startup scripts are never stored."""
    path = f"{API_ROOT}/projects/{quote(project, safe='')}/aggregated/instances"
    query = {
        "maxResults": "500",
        "returnPartialSuccess": "true",
        "fields": f"nextPageToken,unreachables,items/*/instances({instance_fields})",
    }
    rows: list[dict] = []
    unreachables: list[str] = []
    while True:
        data = await api.get_json(path, query)
        for scoped in data.get("items", {}).values():
            rows.extend(scoped.get("instances", []))
        unreachables.extend(data.get("unreachables", []))
        token = data.get("nextPageToken")
        if not token:
            return rows, unreachables
        query["pageToken"] = token


async def list_disk_types(api: AsyncGoogleApi, project: str) -> dict[str, dict]:
    """``{disk selfLink: {"type", "provisionedIops", "provisionedThroughput"}}`` for every persistent disk, from one
    aggregated listing (the performance keys only where Compute Engine reports them)."""
    path = f"{API_ROOT}/projects/{quote(project, safe='')}/aggregated/disks"
    query = {
        "maxResults": "500",
        "returnPartialSuccess": "true",
        "fields": ("nextPageToken,unreachables,"
                   "items/*/disks(id,name,zone,region,sizeGb,users,selfLink,type,provisionedIops,provisionedThroughput,"
                   "creationTimestamp)"),
    }
    types: dict[str, dict] = {}
    while True:
        data = await api.get_json(path, query)
        if data.get("unreachables"):
            # A partial listing would silently drop those zones' disks (and their known types).
            raise RuntimeError(f"disk listing zones unreachable: {', '.join(data['unreachables'][:5])}")
        for scoped in data.get("items", {}).values():
            for disk in scoped.get("disks", []):
                if isinstance(disk, dict) and disk.get("selfLink"):
                    types[str(disk["selfLink"])] = {
                        "type": str(disk.get("type", "")).rsplit("/", 1)[-1],
                        # Regional (replicated) disks bill differently from zonal ones of the same type.
                        "regional": "/regions/" in str(disk.get("selfLink", "")) or "/regions/" in str(disk.get("type", "")),
                        **{k: disk[k] for k in ("provisionedIops", "provisionedThroughput", "creationTimestamp",
                                                "id", "name", "zone", "region", "sizeGb", "users") if k in disk},
                    }
        token = data.get("nextPageToken")
        if not token:
            return types
        query["pageToken"] = token


def runner_host_records(items: list[dict]) -> list[dict]:
    """Dashboard host records for runner hosts in a GCE instance listing.

    A runner host carries the ``spot-target`` label or recorded patch metadata. A host provisioned before either
    existed is recognized by its runner-host name prefix (``RUNNER_HOST_NAME_PREFIXES``) and marked
    ``unrecorded_host``: its target and patch are unknown, so the patch console lists it for every repository as
    ``unknown`` and refuses to hot-patch until it is reprovisioned or patched with ``--patch-live``. VMs with
    another ``spot-role`` (control VMs) and VMs outside those prefixes are never runner hosts. Only the recorded
    patch and deployment keys are kept (see runner_fleet); every other metadata value, including startup
    scripts, is dropped.
    """
    from .runner_fleet import (RUNNER_HOST_NAME_PREFIXES, gce_timestamp, patch_record_from_gce_item,
                               runner_deployment_from_gce_item)
    from .target_config import TARGET_LABEL_KEY

    hosts = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
        record = patch_record_from_gce_item(item)
        target_label = str(labels.get(TARGET_LABEL_KEY, "") or "")
        unrecorded = not target_label and not record["patch_sha256"] and not record["patch_pending_sha256"]
        if unrecorded and (not item["name"].startswith(RUNNER_HOST_NAME_PREFIXES) or labels.get("spot-role")):
            continue
        host = {
            "name": item["name"],
            "zone": str(item.get("zone", "")).rsplit("/", 1)[-1],
            "status": str(item.get("status", "UNKNOWN")),
            "machine_type": str(item.get("machineType", "")).rsplit("/", 1)[-1],
            "cpu_platform": str(item.get("cpuPlatform", "") or ""),
            "last_start_timestamp": gce_timestamp(item.get("lastStartTimestamp")),
            "role": "cicd_runner",
            "target_label": target_label,
            **runner_deployment_from_gce_item(item),
            **record,
        }
        if unrecorded:
            host["unrecorded_host"] = True
        hosts.append(host)
    return hosts


async def machine_type_capacity(api: AsyncGoogleApi, project: str, zone: str, machine_type: str) -> dict:
    """``{"vcpu": int, "ram_gib": float}`` for one machine type in one zone."""
    path = (f"{API_ROOT}/projects/{quote(project, safe='')}/zones/{quote(zone, safe='')}/machineTypes/"
            f"{quote(machine_type, safe='')}")
    data = await api.get_json(path, {"fields": "guestCpus,memoryMb"})
    vcpu, memory_mb = data.get("guestCpus"), data.get("memoryMb")
    if not isinstance(vcpu, int) or not isinstance(memory_mb, int) or vcpu <= 0 or memory_mb <= 0:
        raise RuntimeError(f"machine type {machine_type} in {zone} returned no capacity")
    return {"vcpu": vcpu, "ram_gib": memory_mb / 1024}


async def list_machines(api: AsyncGoogleApi, project: str,
                        min_vcpu: int, min_ram_mb: int) -> tuple[list[dict], list[str]]:
    """Fetch qualifying machine types for all zones with one aggregated-list stream."""
    path = f"{API_ROOT}/projects/{quote(project, safe='')}/aggregated/machineTypes"
    query = {
        "maxResults": "500",
        "filter": f"guestCpus >= {min_vcpu} AND memoryMb >= {min_ram_mb}",
        "returnPartialSuccess": "true",
        "fields": (
            "nextPageToken,unreachables,"
            "items/*/machineTypes(name,zone,guestCpus,memoryMb)"
        ),
    }
    rows: list[dict] = []
    unreachables: list[str] = []
    while True:
        data = await api.get_json(path, query)
        for scoped in data.get("items", {}).values():
            rows.extend(scoped.get("machineTypes", []))
        unreachables.extend(data.get("unreachables", []))
        token = data.get("nextPageToken")
        if not token:
            return rows, unreachables
        query["pageToken"] = token
