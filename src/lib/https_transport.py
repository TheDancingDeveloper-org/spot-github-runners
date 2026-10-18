"""Minimal HTTPS transport that returns status, headers and body, and never follows redirects.

The control-plane clients (Cloud Storage state, the GHCR registry, the GitHub API) need response headers, for
example ``x-goog-generation``, ``Date`` and ``Docker-Content-Digest``, and must never forward an
``Authorization`` header to another host. ``urllib``'s default redirect handler copies request headers to the
redirect target, so this transport turns every redirect into a plain response and lets the caller decide.

Standard library only.
"""
from __future__ import annotations

import http.client
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping

DEFAULT_MAX_BYTES = 8 * 1024 * 1024


class TransportError(RuntimeError):
    """The request could not complete (connect, TLS, timeout): the server may or may not have acted on it."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


# (method, url, headers, body, timeout) -> HttpResponse; raises TransportError.
Transport = Callable[[str, str, Mapping[str, str], "bytes | None", float], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None  # urllib then raises HTTPError with the 3xx status, handled below


def _lower(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def https_request(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float,
                  *, max_bytes: int = DEFAULT_MAX_BYTES) -> HttpResponse:
    """One HTTPS request. Non-2xx statuses (including redirects) are returned, not raised."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise TransportError(f"refusing non-HTTPS request to {parsed.netloc or url!r}")
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(_NoRedirect)
    where = f"{method} {parsed.hostname}{parsed.path}"
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - HTTPS enforced above
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise TransportError(f"{where}: response is larger than {max_bytes} bytes")
            return HttpResponse(int(response.status), _lower(dict(response.headers.items())), data)
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read(65536)
        except (OSError, http.client.HTTPException):
            data = b""
        return HttpResponse(int(exc.code), _lower(dict(exc.headers.items()) if exc.headers else {}), data)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise TransportError(f"{where}: {type(exc).__name__}: {exc}") from exc
