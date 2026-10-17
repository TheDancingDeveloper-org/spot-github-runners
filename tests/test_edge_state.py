"""Tests for the shared control-VM contract module (edge_state) as used by the dashboard."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import unittest
import urllib.parse

from lib.edge_state import (
    KV_ORIGIN_KEY,
    KV_STATE_KEY,
    SNAPSHOT_SCHEMA,
    UNAVAILABLE_ERROR,
    CloudflareKv,
    KvConfig,
    KvError,
    OriginRecord,
    build_snapshot,
    control_token,
    parse_snapshot,
    read_origin,
    unavailable_payload,
    verify_control_token,
    write_origin,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "session_vector.json"


class FakeKvRequester:
    """In-memory Workers KV REST stand-in; never touches the network."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail = False
        self.fail_puts = False
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        key = urllib.parse.unquote(url.rsplit("/values/", 1)[1])
        with self.lock:
            self.calls.append((method, key))
            if self.fail:
                raise KvError("simulated KV outage")
            if method == "GET":
                if key not in self.values:
                    return 404, b'{"success":false}'
                return 200, self.values[key].encode("utf-8")
            if method == "PUT":
                if self.fail_puts:
                    return 500, b'{"success":false}'
                self.values[key] = body.decode("utf-8")
                return 200, b'{"success":true}'
        return 405, b""

    def count(self, method: str, key: str) -> int:
        with self.lock:
            return sum(1 for c in self.calls if c == (method, key))


def make_kv(fake: FakeKvRequester) -> CloudflareKv:
    return CloudflareKv(KvConfig("acct", "ns", "kv-token"), requester=fake)


class EdgeStateTests(unittest.TestCase):
    def test_unavailable_payload_shape(self):
        body = unavailable_payload()
        self.assertEqual(body["error"], UNAVAILABLE_ERROR)
        self.assertEqual(body["retry_after_seconds"], 10)
        self.assertTrue(body["message"])

    def test_kv_config_from_env(self):
        self.assertIsNone(KvConfig.from_env({}))
        self.assertIsNone(KvConfig.from_env({"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_KV_NAMESPACE_ID": "n"}))
        cfg = KvConfig.from_env({"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_KV_NAMESPACE_ID": "n",
                                 "CLOUDFLARE_KV_API_TOKEN": "t"})
        self.assertEqual(cfg, KvConfig("a", "n", "t"))

    def test_kv_roundtrip_and_origin(self):
        fake = FakeKvRequester()
        kv = make_kv(fake)
        self.assertIsNone(kv.get(KV_STATE_KEY))
        kv.put(KV_STATE_KEY, "hello")
        self.assertEqual(kv.get(KV_STATE_KEY), "hello")
        self.assertIsNone(read_origin(kv))
        self.assertTrue(write_origin(kv, OriginRecord.for_slot("b", now=5)))
        self.assertEqual(read_origin(kv).slot, "b")
        fake.fail = True
        with self.assertRaises(KvError):
            kv.get(KV_ORIGIN_KEY)
        self.assertFalse(kv.put_verified(KV_ORIGIN_KEY, "x"))

    def test_snapshot_build_and_parse(self):
        env = build_snapshot({"a": 1}, 7, "a", now=10)
        self.assertEqual(env["schema"], SNAPSHOT_SCHEMA)
        self.assertEqual(parse_snapshot(json.dumps(env)), env)
        self.assertEqual(parse_snapshot(env), env)
        for bad in (None, "nope", {**env, "schema": "x"}, {**env, "state_version": -1},
                    {**env, "state_version": True}, {**env, "writer_slot": "c"}, {**env, "state": []}):
            self.assertIsNone(parse_snapshot(bad), bad)

    def test_control_token_vector_and_rules(self):
        vector = json.loads(FIXTURE.read_text(encoding="utf-8"))
        control = vector["control"]
        secret = vector["secret"]
        self.assertTrue(verify_control_token(secret, control["token"], control["op"], control["slot"],
                                             now=control["now"]))
        self.assertFalse(verify_control_token(secret, control["token"], "import", control["slot"], now=control["now"]))
        self.assertFalse(verify_control_token(secret, control["token"], control["op"], "b", now=control["now"]))
        self.assertFalse(verify_control_token(secret, control["token"], control["op"], control["slot"],
                                              now=control["now"] + 1000))
        self.assertFalse(verify_control_token(secret + "x", control["token"], control["op"], control["slot"],
                                              now=control["now"]))
        minted = control_token(secret, "freeze", "a", now=1792000000)
        self.assertEqual(minted, control["token"])


if __name__ == "__main__":
    unittest.main()
