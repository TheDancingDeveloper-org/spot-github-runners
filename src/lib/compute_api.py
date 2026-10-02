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


def get_auth(project_arg: str | None) -> Auth:
    """Get active project and user access token from the logged-in gcloud command-line tool."""
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
        raise RuntimeError("gcloud is required for Application Default Credentials") from exc
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
