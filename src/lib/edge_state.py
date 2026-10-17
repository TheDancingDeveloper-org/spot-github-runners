"""Shared contract between the control-VM dashboard, the autoscaler, and the Cloudflare Worker.

The dashboard runs on one Spot "control" VM at a time (slot ``a`` or ``b``), reachable only through a
Cloudflare Tunnel hostname per slot. The Worker on ``spot.swacktech.com`` proxies to whichever slot the
KV key ``origin:active`` names. Dashboard state is mirrored to the KV key ``dashboard:state`` so a
replacement VM can hydrate after an unexpected preemption.

Standard library only. Network access goes through an injectable requester so tests never touch
Cloudflare.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from lib.dashboard_auth import derive_purpose_key, sign_token, verify_token

KV_STATE_KEY = "dashboard:state"
KV_ORIGIN_KEY = "origin:active"
SNAPSHOT_SCHEMA = "spot-dashboard-snapshot-v1"
CONTROL_SLOTS = ("a", "b")
CONTROL_VM_PREFIX = "spot-control-"
ORIGIN_HOST_PREFIX = "spot-origin-"
DEFAULT_ORIGIN_DOMAIN = "swacktech.com"
DEFAULT_PUBLIC_URL = "https://spot.swacktech.com"

UNAVAILABLE_ERROR = "control_vm_unavailable"
UNAVAILABLE_MESSAGE = (
    "The control VM is restarting or being replaced. Keep this page open; "
    "your request will be retried automatically when it is back."
)
DEFAULT_RETRY_AFTER_SECONDS = 10

CONTROL_TOKEN_PURPOSE = "control"
CONTROL_TOKEN_TTL_SECONDS = 120
CONTROL_OPS = frozenset({"freeze", "unfreeze", "snapshot", "import"})

ENV_ACCOUNT_ID = "CLOUDFLARE_ACCOUNT_ID"
ENV_NAMESPACE_ID = "CLOUDFLARE_KV_NAMESPACE_ID"
ENV_API_TOKEN = "CLOUDFLARE_KV_API_TOKEN"
ENV_CONTROL_SECRET = "DASHBOARD_CONTROL_SECRET"

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
MAX_KV_VALUE_BYTES = 25 * 1024 * 1024

# requester(method, url, headers, body, timeout) -> (http_status, response_body_bytes)
Requester = Callable[..., "tuple[int, bytes]"]


class KvError(RuntimeError):
    """A Cloudflare KV request failed or returned an unexpected status."""


def unavailable_payload(retry_after: int = DEFAULT_RETRY_AFTER_SECONDS) -> dict[str, Any]:
    """Body for every 503 that tells the browser to wait for the control VM and retry."""
    return {"error": UNAVAILABLE_ERROR, "message": UNAVAILABLE_MESSAGE, "retry_after_seconds": int(retry_after)}


def validate_slot(slot: str) -> str:
    if slot not in CONTROL_SLOTS:
        raise ValueError(f"control slot must be one of {CONTROL_SLOTS}, got {slot!r}")
    return slot


def other_slot(slot: str) -> str:
    return "b" if validate_slot(slot) == "a" else "a"


def control_vm_name(slot: str) -> str:
    return f"{CONTROL_VM_PREFIX}{validate_slot(slot)}"


def origin_url_for_slot(slot: str, domain: str = DEFAULT_ORIGIN_DOMAIN) -> str:
    return f"https://{ORIGIN_HOST_PREFIX}{validate_slot(slot)}.{domain}"


def default_requester(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                      timeout: float) -> tuple[int, bytes]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise KvError(f"refusing non-HTTPS request to {parsed.netloc}")
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - HTTPS enforced above
            return response.status, response.read(MAX_KV_VALUE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise KvError(f"{method} {parsed.netloc}{parsed.path}: {exc}") from exc


@dataclass(frozen=True)
class KvConfig:
    account_id: str
    namespace_id: str
    api_token: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> KvConfig | None:
        env = os.environ if env is None else env
        values = (env.get(ENV_ACCOUNT_ID, ""), env.get(ENV_NAMESPACE_ID, ""), env.get(ENV_API_TOKEN, ""))
        return cls(*values) if all(values) else None


class CloudflareKv:
    """Minimal Workers KV REST client."""

    def __init__(self, config: KvConfig, requester: Requester | None = None, timeout: float = 10.0) -> None:
        self.config = config
        self.requester = requester or default_requester
        self.timeout = timeout

    def _url(self, key: str) -> str:
        quoted = urllib.parse.quote(key, safe="")
        return (f"{CLOUDFLARE_API_BASE}/accounts/{self.config.account_id}/storage/kv/namespaces/"
                f"{self.config.namespace_id}/values/{quoted}")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_token}", "User-Agent": "spot-control-plane/1.0"}

    def get(self, key: str) -> str | None:
        """Return the value, None when the key does not exist; raise KvError on any other failure."""
        status, body = self.requester("GET", self._url(key), self._headers(), None, self.timeout)
        if status == 404:
            return None
        if status != 200:
            raise KvError(f"KV get {key!r} returned HTTP {status}")
        return body.decode("utf-8")

    def put(self, key: str, value: str) -> None:
        data = value.encode("utf-8")
        if len(data) > MAX_KV_VALUE_BYTES:
            raise KvError(f"KV value for {key!r} is {len(data)} bytes, over the 25 MiB limit")
        headers = {**self._headers(), "Content-Type": "text/plain; charset=utf-8"}
        status, body = self.requester("PUT", self._url(key), headers, data, self.timeout)
        if status != 200:
            raise KvError(f"KV put {key!r} returned HTTP {status}")
        try:
            ok = json.loads(body.decode("utf-8")).get("success") is True
        except (ValueError, AttributeError, UnicodeDecodeError):
            ok = False
        if not ok:
            raise KvError(f"KV put {key!r} did not report success")

    def put_verified(self, key: str, value: str) -> bool:
        """Write and read back. True only when the stored value is confirmed; never raises."""
        try:
            self.put(key, value)
            return self.get(key) == value
        except KvError:
            return False


@dataclass(frozen=True)
class OriginRecord:
    slot: str
    url: str
    vm: str
    updated_at: int

    def to_json(self) -> str:
        return json.dumps({"slot": self.slot, "url": self.url, "vm": self.vm, "updated_at": self.updated_at},
                          separators=(",", ":"), sort_keys=True)

    @classmethod
    def for_slot(cls, slot: str, *, domain: str = DEFAULT_ORIGIN_DOMAIN, now: float | None = None) -> OriginRecord:
        return cls(slot=validate_slot(slot), url=origin_url_for_slot(slot, domain), vm=control_vm_name(slot),
                   updated_at=int(time.time() if now is None else now))

    @classmethod
    def from_json(cls, raw: str | None) -> OriginRecord | None:
        try:
            data = json.loads(raw or "")
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        slot, url, vm, updated_at = data.get("slot"), data.get("url"), data.get("vm"), data.get("updated_at")
        if slot not in CONTROL_SLOTS or not isinstance(url, str) or not url.startswith("https://"):
            return None
        if not isinstance(vm, str) or not isinstance(updated_at, int) or isinstance(updated_at, bool):
            return None
        return cls(slot=slot, url=url, vm=vm, updated_at=updated_at)


def read_origin(kv: CloudflareKv) -> OriginRecord | None:
    """Return the active origin, or None only when the key does not exist.

    Raises KvError when KV cannot be read or when ``origin:active`` exists but is malformed: an
    unreadable pointer must hold lifecycle changes, never look like an unset one that allows a
    bootstrap without freezing or importing the previous writer.
    """
    raw = kv.get(KV_ORIGIN_KEY)
    if raw is None:
        return None
    record = OriginRecord.from_json(raw)
    if record is None:
        raise KvError(f"{KV_ORIGIN_KEY} exists but is not a valid origin record")
    return record


def write_origin(kv: CloudflareKv, record: OriginRecord) -> bool:
    """Point the Worker at ``record``. True only when the write is read back intact."""
    return kv.put_verified(KV_ORIGIN_KEY, record.to_json())


def build_snapshot(state: Mapping[str, Any], state_version: int, writer_slot: str,
                   now: float | None = None) -> dict[str, Any]:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "state_version": int(state_version),
        "writer_slot": validate_slot(writer_slot),
        "written_at": int(time.time() if now is None else now),
        "state": dict(state),
    }


def parse_snapshot(raw: str | bytes | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate a snapshot envelope. Returns it, or None when malformed."""
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        data: Any = dict(raw)
    else:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
    if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA:
        return None
    version = data.get("state_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        return None
    if data.get("writer_slot") not in CONTROL_SLOTS or not isinstance(data.get("state"), dict):
        return None
    return data


def control_token(secret: str | bytes, op: str, slot: str, *, ttl: int = CONTROL_TOKEN_TTL_SECONDS,
                  now: float | None = None) -> str:
    """Short-lived bearer token the autoscaler sends to ``/api/control/<op>`` on one slot."""
    if op not in CONTROL_OPS:
        raise ValueError(f"unknown control op {op!r}")
    issued = int(time.time() if now is None else now)
    payload = {"v": 1, "op": op, "slot": validate_slot(slot), "iat": issued, "exp": issued + int(ttl)}
    return sign_token(derive_purpose_key(secret, CONTROL_TOKEN_PURPOSE), payload)


def verify_control_token(secret: str | bytes, token: str | None, op: str, slot: str, *,
                         now: float | None = None) -> bool:
    if not secret or not token:
        return False
    payload = verify_token(derive_purpose_key(secret, CONTROL_TOKEN_PURPOSE), token)
    if not payload or payload.get("v") != 1 or payload.get("op") != op or payload.get("slot") != slot:
        return False
    exp, iat = payload.get("exp"), payload.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int) or isinstance(exp, bool) or isinstance(iat, bool):
        return False
    current = time.time() if now is None else now
    return iat - 60 <= current < exp and exp - iat <= 600
