"""In-memory Cloud Storage JSON API for tests: generation preconditions, server clock, injected failures."""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import urllib.parse

from lib.https_transport import HttpResponse, TransportError

BUCKET = "test-bucket"
TOKEN = "tok"


class FakeGcsServer:
    """Behaves like the JSON API for the calls GcsClient makes. ``clock`` is a one-element list (server seconds)."""

    def __init__(self, clock):
        self.clock = clock
        self.objects: dict[str, tuple[bytes, int, float]] = {}  # name -> (data, generation, updated)
        self.next_generation = 1_000
        self.calls: list[tuple[str, str, dict]] = []
        # Each entry: (method, mode). mode "drop" raises before applying, "lost" applies then raises,
        # an int returns that status without applying. Consumed by the next matching request.
        self.faults: list[tuple[str, object]] = []
        self.before = None  # callable(method, name, query) run before a request is handled (interleaving)
        self.generation_header = True
        self.date_header = True

    # -- helpers ---------------------------------------------------------------------------------
    def put_raw(self, name: str, data: bytes) -> int:
        self.next_generation += 1
        self.objects[name] = (data, self.next_generation, self.clock[0])
        return self.next_generation

    def json(self, name: str):
        return json.loads(self.objects[name][0])

    def writes(self, method: str = "POST") -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if c[0] == method]

    def _headers(self, extra=None):
        headers = {"content-type": "application/json"}
        if self.date_header:
            headers["date"] = email.utils.formatdate(self.clock[0], usegmt=True)
        headers.update(extra or {})
        return headers

    def _fault(self, method: str):
        for index, (m, mode) in enumerate(self.faults):
            if m == method:
                del self.faults[index]
                return mode
        return None

    # -- transport -------------------------------------------------------------------------------
    def __call__(self, method, url, headers, body, timeout):
        assert headers.get("Authorization") == f"Bearer {TOKEN}", headers
        parsed = urllib.parse.urlsplit(url)
        assert parsed.scheme == "https" and parsed.hostname == "storage.googleapis.com", url
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == f"/upload/storage/v1/b/{BUCKET}/o":
            name = query["name"]
        else:
            prefix = f"/storage/v1/b/{BUCKET}/o/"
            assert parsed.path.startswith(prefix), url
            name = urllib.parse.unquote(parsed.path[len(prefix):])
        self.calls.append((method, name, query))
        if self.before:
            self.before(method, name, query)
        fault = self._fault(method)
        if fault == "drop":
            raise TransportError("connection reset")
        if isinstance(fault, int):
            return HttpResponse(fault, self._headers(), b"{}")
        response = self._handle(method, name, query, body)
        if fault == "lost":
            raise TransportError("timed out after the server applied it")
        return response

    def _precondition_ok(self, name, query) -> bool:
        if "ifGenerationMatch" not in query:
            return True  # like Cloud Storage: no precondition, unconditional
        wanted = int(query["ifGenerationMatch"])
        current = self.objects.get(name)
        return current is None if wanted == 0 else (current is not None and current[1] == wanted)

    def _handle(self, method, name, query, body):
        current = self.objects.get(name)
        if method == "POST":
            if not self._precondition_ok(name, query):
                return HttpResponse(412, self._headers(), b'{"error": {"code": 412}}')
            generation = self.put_raw(name, body)
            return HttpResponse(200, self._headers(), json.dumps(
                {"generation": str(generation), "updated": self._rfc3339(self.clock[0])}).encode())
        if method == "DELETE":
            if current is None:
                return HttpResponse(404, self._headers(), b"{}")
            if not self._precondition_ok(name, query):
                return HttpResponse(412, self._headers(), b"{}")
            del self.objects[name]
            return HttpResponse(204, self._headers(), b"")
        assert method == "GET"
        if current is None:
            return HttpResponse(404, self._headers(), b"{}")
        data, generation, updated = current
        if "generation" in query and int(query["generation"]) != generation:
            return HttpResponse(404, self._headers(), b"{}")  # no versioning: older generations are gone
        if query.get("alt") == "media":
            extra = {"x-goog-generation": str(generation)} if self.generation_header else {}
            return HttpResponse(200, self._headers(extra), data)
        return HttpResponse(200, self._headers(), json.dumps(
            {"generation": str(generation), "updated": self._rfc3339(updated)}).encode())

    @staticmethod
    def _rfc3339(seconds: float) -> str:
        return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
