"""Durable control VM state and project lock in Google Cloud Storage.

``SPOT_CONTROL_STATE_URI=gs://<bucket>[/<prefix>]`` moves the project's control VM state and lock out of the
local ``SPOT_CONTROL_STATE_DIR`` into two objects, so the serverless watchdog (a Cloud Run service) and an operator
workstation share one state and one lock:

* ``<prefix>/control-vm-<project>.json``: the state, wrapped in an envelope (schema, project, writer, write id).
* ``<prefix>/control-vm-<project>.lock``: the lease (owner, token, ttl, expiry).

Every write is a Cloud Storage JSON API upload with an ``ifGenerationMatch`` precondition, so no write is ever
based on a version its writer did not read:

* The lease is taken with ``ifGenerationMatch=0`` (create only). A held lease is renewed with
  ``ifGenerationMatch=<its generation>``. A stale lease is broken only after its expiry plus a grace period, as
  measured by the Cloud Storage server clock (the object's ``updated`` time and the response ``Date``), again
  with ``ifGenerationMatch=<the stale generation>``, so of two contenders exactly one wins.
* The holder trusts its lease only until ``send time of its last successful write + ttl``, measuring the elapsed
  time as the larger of its monotonic and its wall-clock deltas. On macOS ``time.monotonic()`` stops while the
  machine sleeps, so a workstation that slept past the expiry would otherwise wake up still trusting the lease; the
  wall clock keeps counting through a sleep. A wall-clock jump well ahead of the monotonic clock (a suspend) makes
  the next guard renew the lease, and the guard re-reads the lease object at least every
  ``LEASE_REVALIDATE_SECONDS`` to confirm the generation is still its own. :meth:`GcsLease.ensure` renews the lease
  whenever less than ``min_remaining`` seconds are left, so the longest single provider call (``gcloud`` times out
  after 600 s) always completes inside the lease; it raises :class:`LeaseLost` instead of letting the caller act.
* The state is saved with ``ifGenerationMatch=<the generation it loaded or last wrote>``. A save after another
  controller wrote raises, which stops the tick before its next provider call (every step persists its intent
  before it acts).
* A write whose outcome is unknown (timeout, 5xx) is settled by reading the object back and matching a unique
  write id. It never counts as written unless the read-back proves it.

Uses only the standard library: the JSON API over HTTPS with an access token from the GCE metadata server
(``SPOT_GCP_TOKEN_SOURCE=metadata``, set on Cloud Run) or ``gcloud auth print-access-token`` (a workstation).
"""
from __future__ import annotations

import copy
import datetime as _dt
import email.utils
import json
import logging
import os
import re
import socket
import subprocess
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .control_vm import CONTROL_STATE_MOVED_KEY as MOVED_KEY
from .control_vm import ENV_CONTROL_STATE_URI, STATE_KEY, ControlStateConflict, ProjectControlState, project_control_paths
from .https_transport import HttpResponse, Transport, TransportError, https_request

LOG = logging.getLogger("spot_runner_autoscaler.control_vm")

STATE_SCHEMA = "spot-control-vm-state-v1"
LOCK_SCHEMA = "spot-control-vm-lock-v1"
# A local state file that was migrated to Cloud Storage is replaced by {MOVED_KEY: <uri>}; the local backend then
# refuses to run, so a workstation that lost SPOT_CONTROL_STATE_URI fails closed instead of acting on old state.

LEASE_TTL_SECONDS = 1200
# Renew once less than this is left; the guard never returns with less. It exceeds the longest single blocking call a
# tick makes (control_vm.GCLOUD_TIMEOUT_SECONDS, 600 s) plus 60 s of slack, so a call started inside the lease also
# ends inside it.
LEASE_MIN_REMAINING_SECONDS = 660
# Contenders break a lease only this long after its server-clock expiry.
LEASE_STALE_GRACE_SECONDS = 60
# A lease never blocks for longer than this, whatever ttl its object claims.
LEASE_MAX_TTL_SECONDS = 3600
# The guard re-reads the lease object (two reads) when this long passed since it last confirmed its generation.
LEASE_REVALIDATE_SECONDS = 120
# Wall-clock time that ran this far ahead of monotonic time since the last confirmation means the process was
# suspended (or the clock was stepped): renew, which also proves the lease is still ours, before acting.
LEASE_SUSPEND_SLACK_SECONDS = 5
MAX_OBJECT_BYTES = 8 * 1024 * 1024
GCS_API = "https://storage.googleapis.com"
HTTP_TIMEOUT_SECONDS = 20.0
READ_ATTEMPTS = 3

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,220}[a-z0-9]$")
_PREFIX_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class GcsError(RuntimeError):
    """A Cloud Storage call failed definitively (nothing was written)."""


class GcsPreconditionFailed(GcsError):
    """HTTP 412: the object's generation is not the one the write was conditioned on."""


class GcsAmbiguous(GcsError):
    """The write may or may not have landed (transport failure, 5xx, 429); read the object back to settle it."""


class LeaseLost(RuntimeError):
    """The project lease can no longer be proven held; stop before the next provider call."""


# --------------------------------------------------------------------------------------------
# URIs and tokens
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StateLocation:
    bucket: str
    prefix: str  # "" or "a/b" (no leading or trailing slash)

    @property
    def uri(self) -> str:
        return f"gs://{self.bucket}" + (f"/{self.prefix}" if self.prefix else "")

    def object_name(self, leaf: str) -> str:
        return f"{self.prefix}/{leaf}" if self.prefix else leaf

    def state_object(self, project: str) -> str:
        return self.object_name(f"control-vm-{project.replace(':', '__')}.json")

    def lock_object(self, project: str) -> str:
        return self.object_name(f"control-vm-{project.replace(':', '__')}.lock")


def parse_state_uri(uri: str) -> StateLocation:
    """``gs://bucket[/prefix]`` -> StateLocation; raises ValueError on anything else."""
    text = str(uri or "").strip()
    if not text.startswith("gs://"):
        raise ValueError(f"{ENV_CONTROL_STATE_URI} must be gs://<bucket>[/<prefix>], not {uri!r}")
    rest = text[len("gs://"):].rstrip("/")
    bucket, _, prefix = rest.partition("/")
    if not _BUCKET_RE.match(bucket) or ".." in bucket or bucket.startswith("goog"):
        raise ValueError(f"{ENV_CONTROL_STATE_URI} names an invalid bucket {bucket!r}")
    parts = [p for p in prefix.split("/")] if prefix else []
    if any(not _PREFIX_PART_RE.match(p) or p in (".", "..") for p in parts):
        raise ValueError(f"{ENV_CONTROL_STATE_URI} has an invalid object prefix {prefix!r}")
    return StateLocation(bucket, "/".join(parts))


class AccessToken:
    """Cached OAuth access token: the GCE metadata server when ``SPOT_GCP_TOKEN_SOURCE=metadata`` (Cloud Run),
    else ``gcloud auth print-access-token`` (the operator's credentials)."""

    CACHE_SECONDS = 240.0

    def __init__(self, env: Mapping[str, str] | None = None,
                 runner: Callable[..., Any] = subprocess.run,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.env = os.environ if env is None else env
        self.runner = runner
        self.monotonic = monotonic
        self._token: str | None = None
        self._at = 0.0

    def __call__(self) -> str:
        now = self.monotonic()
        if self._token and now - self._at < self.CACHE_SECONDS:
            return self._token
        from .compute_api import metadata_access_token, token_source_is_metadata

        if token_source_is_metadata(dict(self.env)):
            token = metadata_access_token()
        else:
            try:
                result = self.runner(["gcloud", "auth", "print-access-token"], check=False, capture_output=True,
                                     text=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                raise GcsError(f"could not get a gcloud access token: {exc}") from exc
            token = (result.stdout or "").strip()
            if result.returncode != 0 or not token:
                raise GcsError(f"gcloud auth print-access-token failed: {(result.stderr or '').strip()[:200]}")
        self._token, self._at = token, now
        return token


# --------------------------------------------------------------------------------------------
# JSON API client
# --------------------------------------------------------------------------------------------

def _parse_rfc3339(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def _int(value: Any) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass(frozen=True)
class ObjectInfo:
    generation: int
    updated: float | None = None      # server time of the write (RFC 3339 ``updated``)
    server_time: float | None = None  # the response ``Date``


@dataclass(frozen=True)
class GcsObject:
    data: bytes
    generation: int
    server_time: float | None = None


class GcsClient:
    """The Cloud Storage JSON API operations the state store needs, each conditioned on a generation."""

    def __init__(self, bucket: str, *, token: Callable[[], str] | None = None, transport: Transport | None = None,
                 timeout: float = HTTP_TIMEOUT_SECONDS, sleep: Callable[[float], None] = time.sleep) -> None:
        self.bucket = bucket
        self.token = token or AccessToken()
        self.transport = transport or https_request
        self.timeout = timeout
        self.sleep = sleep

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "User-Agent": "spot-control-plane/1.0", **(extra or {})}

    def _object_url(self, name: str) -> str:
        return (f"{GCS_API}/storage/v1/b/{urllib.parse.quote(self.bucket, safe='')}/o/"
                f"{urllib.parse.quote(name, safe='')}")

    def _read(self, url: str) -> HttpResponse:
        """Idempotent GET with a short retry on transport errors, 429 and 5xx."""
        last = ""
        for attempt in range(READ_ATTEMPTS):
            try:
                response = self.transport("GET", url, self._headers(), None, self.timeout)
            except TransportError as exc:
                last = str(exc)
            else:
                if response.status not in (429, 500, 502, 503, 504):
                    return response
                last = f"HTTP {response.status}"
            if attempt + 1 < READ_ATTEMPTS:
                self.sleep(0.5 * (attempt + 1))
        raise GcsError(f"Cloud Storage read failed: {last}")

    def stat(self, name: str) -> ObjectInfo | None:
        response = self._read(self._object_url(name) + "?fields=generation,updated")
        if response.status == 404:
            return None
        if response.status != 200:
            raise GcsError(f"Cloud Storage stat of {name} returned HTTP {response.status}")
        try:
            meta = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GcsError(f"Cloud Storage stat of {name} returned invalid JSON") from exc
        generation = _int(meta.get("generation")) if isinstance(meta, dict) else None
        if generation is None:
            raise GcsError(f"Cloud Storage stat of {name} returned no generation")
        return ObjectInfo(generation, _parse_rfc3339(meta.get("updated")), _parse_http_date(response.header("date")))

    def get(self, name: str, *, generation: int | None = None) -> GcsObject | None:
        """The object's bytes and the generation they belong to, or None when it does not exist."""
        url = self._object_url(name) + "?alt=media" + (f"&generation={generation}" if generation else "")
        response = self._read(url)
        if response.status == 404:
            return None
        if response.status != 200:
            raise GcsError(f"Cloud Storage read of {name} returned HTTP {response.status}")
        if len(response.body) > MAX_OBJECT_BYTES:
            raise GcsError(f"Cloud Storage object {name} is larger than {MAX_OBJECT_BYTES} bytes")
        got = _int(response.header("x-goog-generation"))
        if generation and got not in (None, generation):
            raise GcsError(f"Cloud Storage returned generation {got} of {name}, not {generation}")
        got = got or generation
        if got is None:
            # No generation header: pin the bytes to a generation with stat, then re-read exactly that one.
            info = self.stat(name)
            if info is None:
                return None
            return self.get(name, generation=info.generation)
        return GcsObject(response.body, got, _parse_http_date(response.header("date")))

    def put(self, name: str, data: bytes, *, if_generation_match: int) -> ObjectInfo:
        """Upload ``data`` only if the object's generation is ``if_generation_match`` (0: only if absent)."""
        if not isinstance(if_generation_match, int) or isinstance(if_generation_match, bool) \
                or if_generation_match < 0:
            raise ValueError("if_generation_match must be a generation number or 0")
        url = (f"{GCS_API}/upload/storage/v1/b/{urllib.parse.quote(self.bucket, safe='')}/o?uploadType=media"
               f"&name={urllib.parse.quote(name, safe='')}&ifGenerationMatch={if_generation_match}"
               f"&fields=generation,updated")
        headers = self._headers({"Content-Type": "application/json"})
        try:
            response = self.transport("POST", url, headers, data, self.timeout)
        except TransportError as exc:
            raise GcsAmbiguous(f"Cloud Storage write of {name}: {exc}") from exc
        if response.status == 412:
            raise GcsPreconditionFailed(f"{name} is not at generation {if_generation_match}")
        if response.status in (408, 429) or response.status >= 500:
            raise GcsAmbiguous(f"Cloud Storage write of {name} returned HTTP {response.status}")
        if response.status != 200:
            raise GcsError(f"Cloud Storage write of {name} returned HTTP {response.status}")
        try:
            meta = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            meta = {}
        generation = _int(meta.get("generation")) if isinstance(meta, dict) else None
        if generation is None:
            raise GcsAmbiguous(f"Cloud Storage write of {name} returned no generation")
        return ObjectInfo(generation, _parse_rfc3339(meta.get("updated")), _parse_http_date(response.header("date")))

    def delete(self, name: str, *, if_generation_match: int) -> str:
        """Delete only that generation: "deleted", "missing", or "precondition" (another generation is live)."""
        url = self._object_url(name) + f"?ifGenerationMatch={if_generation_match}"
        try:
            response = self.transport("DELETE", url, self._headers(), None, self.timeout)
        except TransportError as exc:
            raise GcsAmbiguous(f"Cloud Storage delete of {name}: {exc}") from exc
        if response.status in (200, 204):
            return "deleted"
        if response.status == 404:
            return "missing"
        if response.status == 412:
            return "precondition"
        raise GcsError(f"Cloud Storage delete of {name} returned HTTP {response.status}")


# --------------------------------------------------------------------------------------------
# Lease
# --------------------------------------------------------------------------------------------

def default_owner() -> str:
    """A readable owner: the Cloud Run service revision, or the host; always with the process id."""
    revision = os.environ.get("K_REVISION")
    if revision:
        return f"cloud-run:{revision}:{socket.gethostname()}:{os.getpid()}"
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class LockHolder:
    generation: int
    owner: str
    token: str
    expires_at: float      # server clock: when the lease may be broken (expiry plus grace)
    stale: bool
    server_time: float

    def describe(self) -> str:
        left = int(self.expires_at - self.server_time)
        return (f"{self.owner or 'unknown owner'} (generation {self.generation}, "
                + ("stale" if self.stale else f"breakable in {max(left, 0)}s") + ")")


class GcsLease:
    """The project lease: one Cloud Storage object written only with generation preconditions."""

    def __init__(self, client: GcsClient, name: str, *, owner: str | None = None,
                 ttl: float = LEASE_TTL_SECONDS, min_remaining: float = LEASE_MIN_REMAINING_SECONDS,
                 stale_grace: float = LEASE_STALE_GRACE_SECONDS,
                 wall: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic) -> None:
        if not 0 < min_remaining < ttl <= LEASE_MAX_TTL_SECONDS:
            raise ValueError("lease timings must satisfy 0 < min_remaining < ttl <= LEASE_MAX_TTL_SECONDS")
        self.client = client
        self.name = name
        self.owner = owner or default_owner()
        self.token = uuid.uuid4().hex
        self.ttl = float(ttl)
        self.min_remaining = float(min_remaining)
        self.stale_grace = float(stale_grace)
        self.wall = wall
        self.monotonic = monotonic
        self.generation: int | None = None
        self.holder: LockHolder | None = None
        self.broke: LockHolder | None = None
        # (monotonic, wall) when the last successful write was sent: the lease runs ttl from there.
        self._sent = (0.0, 0.0)
        # (monotonic, wall) when the generation was last confirmed as ours (a write or a re-read).
        self._confirmed = (0.0, 0.0)

    @property
    def held(self) -> bool:
        return self.generation is not None

    def _now(self) -> tuple[float, float]:
        return self.monotonic(), self.wall()

    @staticmethod
    def _elapsed(since: tuple[float, float], now: tuple[float, float]) -> float:
        """Suspend-aware elapsed time: the larger of the monotonic and the wall-clock deltas."""
        return max(now[0] - since[0], now[1] - since[1])

    def remaining(self) -> float:
        return (self.ttl - self._elapsed(self._sent, self._now())) if self.held else 0.0

    def _suspended(self, now: tuple[float, float]) -> bool:
        """The wall clock ran well ahead of the monotonic clock since the last confirmation."""
        return (now[1] - self._confirmed[1]) - (now[0] - self._confirmed[0]) > LEASE_SUSPEND_SLACK_SECONDS

    def _content(self, write_id: str, acquired_at: float | None = None) -> bytes:
        now = self.wall()
        return json.dumps({"schema": LOCK_SCHEMA, "owner": self.owner, "token": self.token, "write_id": write_id,
                           "acquired_at": acquired_at or now, "renewed_at": now, "ttl_seconds": self.ttl,
                           "expires_at": now + self.ttl}, sort_keys=True).encode("utf-8")

    def _took(self, generation: int, sent: tuple[float, float]) -> None:
        self.generation = generation
        self._sent = sent
        self._confirmed = self._now()
        self.holder = None

    def _read_lock(self) -> tuple[ObjectInfo, dict[str, Any]] | None:
        info = self.client.stat(self.name)
        if info is None:
            return None
        obj = self.client.get(self.name, generation=info.generation)
        if obj is None:
            return None  # replaced or deleted between the two reads; the caller looks again
        try:
            content = json.loads(obj.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            content = {}
        return info, content if isinstance(content, dict) else {}

    def inspect(self) -> LockHolder | None:
        """The current lock object as a LockHolder, or None when there is none."""
        read = self._read_lock()
        if read is None:
            return None
        info, content = read
        server_now = info.server_time if info.server_time is not None else self.wall()
        updated = info.updated if info.updated is not None else server_now
        ttl = content.get("ttl_seconds")
        ttl = float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 \
            else LEASE_MAX_TTL_SECONDS  # unreadable lock: wait the longest a lease may last
        ttl = min(ttl, LEASE_MAX_TTL_SECONDS)
        expiry = updated + ttl
        claimed = content.get("expires_at")
        if isinstance(claimed, (int, float)) and not isinstance(claimed, bool):
            # Never earlier than the server-clock expiry; never later than the longest lease.
            expiry = max(expiry, min(float(claimed), updated + LEASE_MAX_TTL_SECONDS))
        breakable = expiry + self.stale_grace
        return LockHolder(info.generation, str(content.get("owner") or ""), str(content.get("token") or ""),
                          breakable, server_now >= breakable, server_now)

    def _confirm(self, write_id: str, sent: tuple[float, float]) -> bool:
        """After an ambiguous write: True (and held) only if the live lock object is exactly that write."""
        try:
            read = self._read_lock()
        except GcsError as exc:
            LOG.warning("Control VM lock: could not read back an ambiguous write: %s", exc)
            return False
        if read is None:
            return False
        info, content = read
        if content.get("token") == self.token and content.get("write_id") == write_id:
            self._took(info.generation, sent)
            return True
        return False

    def _write(self, if_generation_match: int, acquired_at: float | None = None) -> bool | None:
        """True: written and held. False: the precondition failed. None: unknown and not confirmed."""
        write_id = uuid.uuid4().hex
        sent = self._now()
        try:
            info = self.client.put(self.name, self._content(write_id, acquired_at),
                                   if_generation_match=if_generation_match)
        except GcsPreconditionFailed:
            return False
        except GcsAmbiguous as exc:
            LOG.warning("Control VM lock: write outcome unknown (%s); reading it back", exc)
            return True if self._confirm(write_id, sent) else None
        self._took(info.generation, sent)
        return True

    def acquire(self) -> bool:
        """Take the lease. False when another live holder has it (``holder`` says who)."""
        if self.held:
            return True
        for _ in range(3):
            if self._write(0):
                return True
            current = self.inspect()
            if current is None:
                continue  # released between our create and our read: try the create again
            if current.token == self.token:
                # An earlier ambiguous create of ours landed after all (its read-back missed it). Its send time is
                # unknown, so it is trusted only after a renewal conditioned on that generation.
                self.generation, self._sent = current.generation, (float("-inf"), float("-inf"))
                try:
                    self.renew()
                except LeaseLost:
                    return False
                return True
            self.holder = current
            if not current.stale:
                return False
            # Break the stale lease atomically: only if it is still exactly the generation found stale.
            outcome = self._write(current.generation)
            if outcome:
                self.broke = current
                LOG.warning("Control VM lock: broke the stale lease of %s", current.describe())
                return True
            # Another contender broke or renewed it first (False), or the outcome is unknown (None): look again.
        return False

    def renew(self) -> None:
        """Extend the lease from now; raises LeaseLost when it is no longer ours or can't be proven."""
        if not self.held:
            raise LeaseLost("the control VM lease is not held")
        try:
            outcome = self._write(self.generation)
        except GcsError as exc:
            self.generation = None
            raise LeaseLost(f"could not renew the control VM lease: {exc}") from exc
        if not outcome:
            self.generation = None
            raise LeaseLost("the control VM lease was taken over or could not be renewed; stopping before the "
                            "next provider call")

    def revalidate(self) -> None:
        """Re-read the lease object; raises LeaseLost unless it is still exactly this holder's generation."""
        if not self.held:
            raise LeaseLost("the control VM lease is not held")
        try:
            read = self._read_lock()
        except GcsError as exc:
            self.generation = None
            raise LeaseLost(f"could not re-read the control VM lease: {exc}") from exc
        if read is None or read[0].generation != self.generation or read[1].get("token") != self.token:
            self.generation = None
            raise LeaseLost("the control VM lease object is no longer this controller's; stopping before the next "
                            "provider call")
        self._confirmed = self._now()

    def ensure(self, min_remaining: float | None = None) -> None:
        """Guard before any provider call or state write.

        Renews when less than ``min_remaining`` is left (suspend-aware), or when the process looks suspended since
        the last confirmation; otherwise re-reads the lease once ``LEASE_REVALIDATE_SECONDS`` have passed.
        """
        need = self.min_remaining if min_remaining is None else min_remaining
        if not self.held:
            raise LeaseLost("the control VM lease is not held")
        now = self._now()
        if self.remaining() >= need and not self._suspended(now):
            if self._elapsed(self._confirmed, now) >= LEASE_REVALIDATE_SECONDS:
                self.revalidate()
            # Checked again after the re-read, which can be slow or retried: on return at least ``need`` is left.
            if self.remaining() >= need:
                return
        self.renew()
        if self.remaining() < need:
            raise LeaseLost("the renewed control VM lease is shorter than the next call may take")

    def release(self) -> None:
        """Delete the lock only if it is still our generation; a lost lease is left alone and logged."""
        if not self.held:
            return
        generation, self.generation = self.generation, None
        try:
            result = self.client.delete(self.name, if_generation_match=generation)
        except GcsError as exc:
            LOG.warning("Control VM lock: release failed (%s); the lease expires by itself", exc)
            return
        if result == "precondition":
            LOG.error("Control VM lock: another controller took over the lease while this one held it; check "
                      "the control VM history for overlapping actions")


# --------------------------------------------------------------------------------------------
# Project state and lock on Cloud Storage
# --------------------------------------------------------------------------------------------

def _envelope(project: str, state: Mapping[str, Any], writer: str, write_id: str, now: float) -> bytes:
    return json.dumps({"schema": STATE_SCHEMA, "project": project, "writer": writer, "write_id": write_id,
                       "written_at": now, "state": state}, sort_keys=True).encode("utf-8")


def _parse_envelope(data: bytes, project: str, where: str) -> dict[str, Any]:
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ControlStateConflict(f"control VM state {where} is not JSON; repair it by hand") from exc
    if not isinstance(envelope, dict) or envelope.get("schema") != STATE_SCHEMA:
        raise ControlStateConflict(f"control VM state {where} is not a {STATE_SCHEMA} object; repair it by hand")
    if envelope.get("project") != project:
        raise ControlStateConflict(f"control VM state {where} belongs to project {envelope.get('project')!r}, "
                                   f"not {project!r}")
    state = envelope.get("state")
    if not isinstance(state, dict) or not isinstance(state.get(STATE_KEY, {}), dict):
        raise ControlStateConflict(f"control VM state {where} holds no state object; repair it by hand")
    return envelope


class GcsProjectControlState(ProjectControlState):
    """The project's control VM state object. Saves need the lease and the generation they loaded."""

    def __init__(self, client: GcsClient, location: StateLocation, project: str,
                 lease: GcsLease | None = None, *, wall: Callable[[], float] = time.time) -> None:
        super().__init__(Path(location.state_object(project)))
        self.client = client
        self.location = location
        self.project = project
        self.object_name = location.state_object(project)
        # adopt() and error messages name self.path; here it is the gs:// URI of the state object.
        self.path = f"{location.uri}/{self.object_name.rsplit('/', 1)[-1]}"  # type: ignore[assignment]
        self.lease = lease
        self.guard = lease.ensure if lease is not None else None
        self.generation = 0
        self.exists = False
        self.wall = wall

    def load(self) -> "GcsProjectControlState":
        obj = self.client.get(self.object_name)
        if obj is None:
            self.state, self.generation, self.exists = {}, 0, False
            return self
        self.state = _parse_envelope(obj.data, self.project, self.path)["state"]
        self.generation, self.exists = obj.generation, True
        return self

    def save(self) -> None:
        if self.lease is None:
            raise RuntimeError(f"control VM state {self.path} was opened read-only")
        self.lease.ensure()
        write_id = uuid.uuid4().hex
        data = _envelope(self.project, self.state, self.lease.owner, write_id, self.wall())
        try:
            info = self.client.put(self.object_name, data, if_generation_match=self.generation)
        except GcsPreconditionFailed as exc:
            raise ControlStateConflict(
                f"control VM state {self.path} changed since this controller read it (generation "
                f"{self.generation}); another controller acted, so this check stops") from exc
        except GcsAmbiguous as exc:
            obj = self.client.get(self.object_name)
            if obj is not None:
                try:
                    if json.loads(obj.data.decode("utf-8")).get("write_id") == write_id:
                        self.generation, self.exists = obj.generation, True
                        return
                except (ValueError, UnicodeDecodeError, AttributeError):
                    pass
            raise ControlStateConflict(f"could not confirm the write of control VM state {self.path}: {exc}") \
                from exc
        self.generation, self.exists = info.generation, True


def project_state(client: GcsClient, location: StateLocation, project: str,
                  lease: GcsLease | None = None) -> GcsProjectControlState:
    """A GcsProjectControlState (read-only without ``lease``)."""
    return GcsProjectControlState(client, location, project, lease)


class GcsProjectControlLock:
    """Like ProjectControlLock: ``__enter__`` returns the loaded state, or None when another controller holds the
    lease (``holder`` then describes it).

    With ``require_existing`` (the default) a missing state object is an error, not an empty state: a controller
    that starts from nothing while an unmigrated local state file still holds a transition would fork the state.
    ``migrate-state --apply`` creates the object, even when there is no local state.
    """

    def __init__(self, project: str, uri: str, *, client: GcsClient | None = None,
                 lease: GcsLease | None = None, require_existing: bool = True) -> None:
        project_control_paths(project, ".")  # validates the project id
        self.require_existing = require_existing
        self.project = project
        self.location = parse_state_uri(uri)
        self.client = client or GcsClient(self.location.bucket)
        self.lease = lease or GcsLease(self.client, self.location.lock_object(project))
        self.holder: LockHolder | None = None

    def __enter__(self):
        if not self.lease.acquire():
            self.holder = self.lease.holder
            return None
        try:
            state = project_state(self.client, self.location, self.project, self.lease).load()
            if self.require_existing and not state.exists:
                raise ControlStateConflict(
                    f"control VM state {state.path} does not exist; run `src/control_vm.py migrate-state --apply` "
                    f"once with {ENV_CONTROL_STATE_URI} set (it uploads the local state, or creates an empty one)")
            return state
        except BaseException:
            self.lease.release()
            raise

    def __exit__(self, *exc: Any) -> None:
        self.lease.release()

    def describe_holder(self) -> str:
        return self.holder.describe() if self.holder else "another controller"


def load_state(project: str, uri: str, *, client: GcsClient | None = None):
    """Read the project state without the lease (previews and status)."""
    location = parse_state_uri(uri)
    return project_state(client or GcsClient(location.bucket), location, project).load()


# --------------------------------------------------------------------------------------------
# One-time migration from the local state file
# --------------------------------------------------------------------------------------------

def read_local(path: Path) -> tuple[str, Any]:
    """("absent", None), ("moved", uri), or ("state", dict) for a local project state file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        backups = sorted(path.parent.glob(f"{path.name}.migrated-*"))
        if backups:
            # Migrated once, and the marker is gone: never "no local state" (ProjectControlState.load refuses too).
            return "orphaned", backups[-1].name
        return "absent", None
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"local control VM state {path} is not JSON; repair it before migrating") from exc
    if isinstance(data, dict) and MOVED_KEY in data:
        return "moved", data.get(MOVED_KEY)
    if not isinstance(data, dict):
        raise RuntimeError(f"local control VM state {path} is not a JSON object; repair it before migrating")
    return "state", data


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_moved_marker(path: Path, uri: str, now: float) -> Path | None:
    """Fence the local state file. Returns the backup path (None when there was no local file).

    Crash-safe: the original is first copied to ``<name>.migrated-<UTC>`` and flushed, then a flushed marker is
    renamed over the original in one atomic step. At every instant the path holds either the original state or
    the marker, never nothing, and the backup exists before the original disappears.
    """
    import shutil
    import tempfile

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    backup: Path | None = None
    if path.exists():
        backup = path.with_name(f"{path.name}.migrated-{stamp}")
        shutil.copy2(path, backup)
        with backup.open("rb") as handle:
            os.fsync(handle.fileno())
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", suffix=".moving", delete=False,
                                     encoding="utf-8") as temp:
        json.dump({MOVED_KEY: uri, "moved_at": now, "backup": backup.name if backup else None}, temp)
        temp.flush()
        os.fsync(temp.fileno())
    try:
        os.replace(temp.name, path)
    except BaseException:
        Path(temp.name).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)
    return backup


def _marker_backup(path: Path) -> tuple[bool, Any]:
    """For a moved marker: (True, state) from the backup it names ({} when it names none), or (False, reason)."""
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"marker {path} is unreadable: {exc}"
    if not isinstance(marker, dict) or "backup" not in marker:
        return False, f"marker {path} doesn't say whether it kept a backup"
    name = marker["backup"]
    if name is None:
        return True, {}  # write_moved_marker found no local file: the state was empty
    if not isinstance(name, str) or "/" in name or not name.startswith(f"{path.name}.migrated-"):
        return False, f"marker {path} names an invalid backup {name!r}"
    kind, state = read_local(path.with_name(name))
    if kind != "state":
        return False, f"backup {name} next to {path} is {kind}"
    return True, state


def migrate_local_state(project: str, local_dir: Path | str | None, uri: str, *, apply: bool,
                        client: GcsClient | None = None, wall: Callable[[], float] = time.time) -> tuple[int, str]:
    """Move the local project state to Cloud Storage once. Returns (exit code, message).

    Under the local flock and the Cloud Storage lease, the local file is fenced (see write_moved_marker) BEFORE the
    Cloud Storage object is created, so the remote state never exists while the local file could still be used:

    * Local state, no remote state: fence the local file, then upload its backup (create-only).
    * No local file, no remote state: fence (a marker without backup), then create an empty remote state.
    * Remote state equal to the local state, or no local file: fence the local file only.
    * Remote state that differs from the local state: refuse (exit 1) and change nothing.
    * A marker whose remote state is missing (a run that stopped after fencing): upload the backup it names.
    Without ``apply`` nothing is written, and the message says what would happen.
    """
    import fcntl

    location = parse_state_uri(uri)
    client = client or GcsClient(location.bucket)
    local_path, local_lock = project_control_paths(project, local_dir)
    target = f"{location.uri}/{location.state_object(project).rsplit('/', 1)[-1]}"

    def same_location(marked: Any) -> bool:
        try:
            return parse_state_uri(str(marked or "")).uri == location.uri
        except ValueError:
            return False

    def decide(kind: str, local: Any, remote_exists: bool, remote_state: Any) -> tuple[int, str, str]:
        if kind == "orphaned":
            return 1, "refuse", (f"{local_path} is missing but {local} next to it shows an earlier migration; restore "
                                 "the marker or the state file by hand before migrating")
        if kind == "moved":
            if not same_location(local):
                return 1, "refuse", (f"{local_path} is fenced for {local}, not {location.uri}; set "
                                     f"{ENV_CONTROL_STATE_URI} to the location it names, or reconcile them by hand")
            if remote_exists:
                return 0, "none", f"already migrated: {local_path} points at {local}"
            ok, backup = _marker_backup(local_path)
            if not ok:
                return 1, "refuse", (f"{local_path} is fenced but {target} does not exist, and {backup}; restore the "
                                     "state from the .migrated-* backup by hand")
            return 0, "resume", f"{local_path} is fenced but {target} does not exist; uploading the fenced backup"
        if kind == "absent":
            if remote_exists:
                return 0, "fence", f"no local state at {local_path}; {target} is the state; fencing the local path"
            return 0, "create", f"no local state at {local_path}; fencing it and creating an empty {target}"
        if remote_exists:
            if remote_state == local:
                return 0, "fence", f"{target} already equals {local_path}; fencing the local file"
            return 1, "refuse", (f"both {local_path} and {target} exist and differ; refusing to choose. Compare "
                                 "them, keep the current one, and move the other aside by hand")
        return 0, "upload", f"fencing {local_path}, then uploading it to {target}"

    if not apply:
        kind, local = read_local(local_path)
        remote = load_state(project, uri, client=client)
        code, _action, message = decide(kind, local, remote.exists, remote.state)
        return code, "dry run, nothing changed: " + message

    local_lock.parent.mkdir(parents=True, exist_ok=True)
    with local_lock.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 1, f"a local controller holds {local_lock}; stop it (or wait for its check) and run again"
        lock = GcsProjectControlLock(project, uri, client=client, require_existing=False)
        with lock as remote:
            if remote is None:
                return 1, f"{lock.describe_holder()} holds the Cloud Storage lease; run again later"
            kind, local = read_local(local_path)
            code, action, message = decide(kind, local, remote.exists, remote.state)
            if action in ("upload", "create", "fence"):
                write_moved_marker(local_path, uri, wall())  # the fence comes first, durably
            if action in ("upload", "create", "resume"):
                if action == "resume":
                    _ok, upload = _marker_backup(local_path)
                else:
                    upload = local if action == "upload" else {}
                remote.state = copy.deepcopy(upload)
                try:
                    remote.save()  # create-only: the generation it loaded is 0
                except (ControlStateConflict, GcsError) as exc:
                    return 1, f"upload failed: {exc}; the local file stays fenced, run migrate-state again"
            if action == "none" or code != 0:
                return code, message
            return 0, message + "; done"
