// Unit tests for cloudflare/worker.js. Run with: node --test cloudflare/
// Uses only node:test and node:assert, a fake KV, and a fake fetch; no network.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import worker, {
  CLIENT_IP_HEADER,
  MAX_AUTH_BODY_BYTES,
  MAX_DASHBOARD_BODY_BYTES,
  ORIGIN_MARKER_HEADER,
  ORIGIN_MARKER_VALUE,
  SESSION_COOKIE_NAME,
  SESSION_PURPOSE_LABEL,
  UNAVAILABLE_ERROR,
  UNAVAILABLE_MESSAGE,
  classifyOriginResponse,
  classifyRoute,
  deriveSessionKey,
  bytesToHex,
  handleRequest,
  isControlPath,
  parseCookies,
  parseOriginRecord,
  requestBodyLimit,
  sessionNotBefore,
  verifySession,
  verifyTokenSignature,
} from "./worker.js";

const here = dirname(fileURLToPath(import.meta.url));
const vector = JSON.parse(readFileSync(join(here, "..", "tests", "fixtures", "session_vector.json"), "utf8"));
const NOW = vector.valid_session.now;
const TOKEN = vector.valid_session.token;
const encoder = new TextEncoder();

const ORIGIN_RECORD = JSON.stringify({
  slot: "a",
  url: "https://spot-origin-a.swacktech.com",
  vm: "spot-control-a",
  updated_at: 1792000000,
});

function makeEnv(overrides = {}) {
  const store = new Map([["origin:active", ORIGIN_RECORD]]);
  return {
    SESSION_SECRET: vector.secret,
    CF_ACCESS_CLIENT_ID: "access-id.example",
    CF_ACCESS_CLIENT_SECRET: "access-secret-example",
    SPOT_KV: {
      store,
      async get(key) {
        return store.has(key) ? store.get(key) : null;
      },
    },
    ...overrides,
  };
}

function fakeFetch(responder) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return responder(url, init);
  };
  fn.calls = calls;
  return fn;
}

const okFetch = () => fakeFetch(() => new Response('{"ok":true}', { status: 200, headers: { "Content-Type": "application/json" } }));

function req(path, { method = "GET", cookie, headers = {}, body } = {}) {
  const h = new Headers(headers);
  if (cookie) h.set("Cookie", cookie);
  return new Request(`https://spot.swacktech.com${path}`, { method, headers: h, body });
}

const authed = (path, opts = {}) => req(path, { ...opts, cookie: `${SESSION_COOKIE_NAME}=${TOKEN}` });
const htmlNav = { Accept: "text/html,application/xhtml+xml", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document" };

async function signPayload(payload, secret = vector.secret) {
  // Mirrors dashboard_auth.sign_token: b64url(compact sorted JSON) "." hex HMAC.
  const sorted = Object.fromEntries(Object.keys(payload).sort().map((k) => [k, payload[k]]));
  const body = Buffer.from(JSON.stringify(sorted)).toString("base64url");
  const keyBytes = await deriveSessionKey(secret);
  const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return `${body}.${bytesToHex(await crypto.subtle.sign("HMAC", key, encoder.encode(body)))}`;
}

// --- Session ---------------------------------------------------------------------------------------

test("purpose label, cookie name, and wait strings match the Python contract", () => {
  assert.equal(SESSION_PURPOSE_LABEL, vector.purpose_label);
  assert.equal(SESSION_COOKIE_NAME, vector.cookie_name);
  assert.equal(SESSION_COOKIE_NAME, "__Host-spot_session");
  assert.equal(UNAVAILABLE_ERROR, "control_vm_unavailable");
  assert.match(UNAVAILABLE_MESSAGE, /control VM is restarting or being replaced/);
});

test("fixture session token verifies at its now", async () => {
  const payload = await verifySession(TOKEN, makeEnv(), NOW);
  assert.deepEqual(payload, vector.valid_session.payload);
  const raw = await verifyTokenSignature(await deriveSessionKey(vector.secret), TOKEN);
  assert.equal(raw.sub, "swackhamer");
});

test("tampered, expired, future, wrong-secret, and malformed tokens fail", async () => {
  const env = makeEnv();
  assert.equal(await verifySession(vector.tampered_session_token, env, NOW), null);
  assert.equal(await verifySession(TOKEN, env, vector.expired_now), null);
  assert.equal(await verifySession(TOKEN, env, vector.valid_session.payload.iat - 61), null);
  assert.ok(await verifySession(TOKEN, env, vector.valid_session.payload.iat - 60));
  assert.equal(await verifySession(TOKEN, makeEnv({ SESSION_SECRET: "other" }), NOW), null);
  for (const bad of [undefined, "", "a.b", "abc", `${TOKEN}.x`, TOKEN.toUpperCase(), "x".repeat(5000)]) {
    assert.equal(await verifySession(bad, env, NOW), null, String(bad).slice(0, 20));
  }
  // A control token is signed under a different purpose key and must not work as a session.
  assert.equal(await verifySession(vector.control.token, env, vector.control.now), null);
});

test("malformed sub or uid fails even when correctly signed", async () => {
  const base = { ...vector.valid_session.payload };
  const env = makeEnv();
  assert.ok(await verifySession(await signPayload(base), env, NOW));
  for (const sub of ["", "-lead", "has space", "a".repeat(40), "bad/login", 619624, null]) {
    assert.equal(await verifySession(await signPayload({ ...base, sub }), env, NOW), null, String(sub));
  }
  for (const uid of [0, -1, 1.5, "619624", true, null, 2 ** 53]) {
    assert.equal(await verifySession(await signPayload({ ...base, uid }), env, NOW), null, String(uid));
  }
  assert.equal(await verifySession(await signPayload({ ...base, v: 2 }), env, NOW), null);
  assert.equal(await verifySession(await signPayload({ ...base, exp: base.iat + 604801 }), env, NOW), null);
  assert.equal(await verifySession(await signPayload({ ...base, tgt: "x" }), env, NOW), null);
});

test("an added administrator's session passes the edge; the origin decides membership", async () => {
  // The Worker only authenticates: which accounts may use the dashboard changes at run time (Admins page)
  // and is enforced by the control VM on every request, so the Worker must not hard-code the owner.
  const base = { ...vector.valid_session.payload, sub: "alice-admin", uid: 4242 };
  const token = await signPayload(base);
  assert.deepEqual(await verifySession(token, makeEnv(), NOW), base);
  const f = fakeFetch(() => new Response('{"error":"Forbidden"}', {
    status: 403, headers: { "Content-Type": "application/json", [ORIGIN_MARKER_HEADER]: ORIGIN_MARKER_VALUE },
  }));
  const res = await handleRequest(req("/api/overview", { cookie: `${SESSION_COOKIE_NAME}=${token}` }), makeEnv(),
    { fetch: f, now: NOW });
  assert.equal(f.calls.length, 1); // forwarded to the origin with the cookie
  assert.match(f.calls[0].init.headers.get("Cookie"), new RegExp(`${SESSION_COOKIE_NAME}=`));
  assert.equal(res.status, 403); // the origin's refusal (for example a removed administrator) passes through
  // Even with stale owner vars from an older wrangler.toml, the Worker no longer filters on them.
  assert.ok(await verifySession(token, makeEnv({ ALLOWED_ADMIN_LOGIN: "swackhamer", ALLOWED_ADMIN_ID: "619624" }), NOW));
});

test("a forged session for any identity still fails without the shared secret", async () => {
  const base = { ...vector.valid_session.payload, sub: "alice-admin", uid: 4242 };
  assert.equal(await verifySession(await signPayload(base, "not-the-shared-secret-0123456789abcdef"), makeEnv(), NOW), null);
});

test("missing bindings fail closed", async () => {
  for (const key of ["SESSION_SECRET"]) {
    assert.equal(await verifySession(TOKEN, makeEnv({ [key]: undefined }), NOW), null, key);
    assert.equal(await verifySession(TOKEN, makeEnv({ [key]: "" }), NOW), null, key);
  }
});

test("the 503 control_vm_unavailable wait still applies to added administrators", async () => {
  const token = await signPayload({ ...vector.valid_session.payload, sub: "alice-admin", uid: 4242 });
  const env = makeEnv();
  env.SPOT_KV.store.delete("origin:active");
  const res = await handleRequest(req("/api/overview", { cookie: `${SESSION_COOKIE_NAME}=${token}` }), env,
    { fetch: okFetch(), now: NOW });
  assert.equal(res.status, 503);
  assert.equal((await res.json()).error, UNAVAILABLE_ERROR);
  const down = fakeFetch(() => new Response("bad gateway", { status: 502 }));
  const res2 = await handleRequest(req("/api/overview", { cookie: `${SESSION_COOKIE_NAME}=${token}` }), makeEnv(),
    { fetch: down, now: NOW });
  assert.equal(res2.status, 503);
  assert.equal((await res2.json()).error, UNAVAILABLE_ERROR);
});

test("cookie parsing takes the first occurrence", () => {
  const c = parseCookies(`a=1; __Host-spot_session=first; __Host-spot_session=second`);
  assert.equal(c.get("__Host-spot_session"), "first");
});

test("an unprefixed spot_session cookie is not a session", async () => {
  const f = okFetch();
  const res = await handleRequest(req("/api/overview", { cookie: `spot_session=${TOKEN}` }), makeEnv(),
    { fetch: f, now: NOW });
  assert.equal(res.status, 401);
  assert.equal(f.calls.length, 0);
});

test("SESSION_NOT_BEFORE revokes older sessions and fails closed when malformed", async () => {
  const iat = vector.valid_session.payload.iat;
  assert.equal(sessionNotBefore(makeEnv()), 0);
  assert.equal(sessionNotBefore(makeEnv({ SESSION_NOT_BEFORE: "" })), 0);
  assert.ok(await verifySession(TOKEN, makeEnv({ SESSION_NOT_BEFORE: String(iat) }), NOW));
  assert.equal(await verifySession(TOKEN, makeEnv({ SESSION_NOT_BEFORE: String(iat + 1) }), NOW), null);
  for (const bad of ["soon", "-1", "1.5", "1e9"]) {
    assert.equal(sessionNotBefore(makeEnv({ SESSION_NOT_BEFORE: bad })), null, bad);
    assert.equal(await verifySession(TOKEN, makeEnv({ SESSION_NOT_BEFORE: bad }), NOW), null, bad);
  }
});

// --- Routing ---------------------------------------------------------------------------------------

test("route table", () => {
  for (const p of ["/auth/github/login", "/auth/github/callback", "/auth/status", "/api/auth/status",
    "/auth/logout", "/api/auth/logout", "/api/health", "/favicon.ico"]) {
    assert.equal(classifyRoute("GET", p), "public", p);
  }
  assert.equal(classifyRoute("POST", "/api/auth/logout"), "public");
  assert.equal(classifyRoute("GET", "/"), "public");
  assert.equal(classifyRoute("POST", "/"), "protected");
  assert.equal(classifyRoute("GET", "/api/overview"), "protected");
  assert.equal(classifyRoute("GET", "/api/health/extra"), "protected");
  assert.equal(classifyRoute("POST", "/api/control/freeze"), "control");
  for (const p of ["/auth/device/code", "/auth/device/token"]) {
    assert.equal(classifyRoute("POST", p), "public", p);
  }
  for (const p of ["/auth/device", "/auth/device/code/extra", "/auth/device/tokens", "/api/auth/device/code"]) {
    assert.equal(classifyRoute("POST", p), "protected", p);
  }
});

test("signed-out device-flow sign-in reaches the backend with the browser Origin intact", async () => {
  for (const path of ["/auth/device/code", "/auth/device/token"]) {
    const f = okFetch();
    const res = await handleRequest(req(path, { method: "POST", body: "{}",
      headers: { "Content-Type": "application/json", Origin: "https://spot.swacktech.com" } }),
    makeEnv(), { fetch: f, now: NOW });
    assert.equal(res.status, 200, path);
    assert.equal(f.calls.length, 1, path);
    assert.equal(f.calls[0].url, `https://spot-origin-a.swacktech.com${path}`);
    // The backend accepts this Origin through DASHBOARD_PUBLIC_ORIGIN, not X-Forwarded-Host.
    assert.equal(f.calls[0].init.headers.get("Origin"), "https://spot.swacktech.com");
  }
});

test("control path block resists encoding and case tricks", () => {
  for (const p of ["/api/control", "/api/control/", "/api/control/freeze", "/API/Control/freeze",
    "/api//control/import", "/api/%63ontrol/snapshot", "/api/%2563ontrol/x", "/x/%2e%2e/api/control/freeze",
    "/api/control;x", "/api/control%2Ffreeze", "/api/%zz"]) {
    assert.equal(isControlPath(p), true, p);
  }
  for (const p of ["/api/controls", "/api/control-panel", "/api/overview", "/"]) {
    assert.equal(isControlPath(p), false, p);
  }
});

test("/api/control is 404 even with a valid session, and never reaches the origin", async () => {
  const f = okFetch();
  const res = await handleRequest(authed("/api/control/freeze", { method: "POST", headers: { Authorization: "Bearer x" } }),
    makeEnv(), { fetch: f, now: NOW });
  assert.equal(res.status, 404);
  assert.equal(f.calls.length, 0);
});

test("unauthenticated: 401 JSON for API and non-GET, 302 to / for HTML GET", async () => {
  const f = okFetch();
  const env = makeEnv();
  let res = await handleRequest(req("/api/overview"), env, { fetch: f, now: NOW });
  assert.equal(res.status, 401);
  assert.deepEqual(await res.json(), { error: "unauthorized" });
  res = await handleRequest(req("/settings", { method: "POST", body: "{}" }), env, { fetch: f, now: NOW });
  assert.equal(res.status, 401);
  res = await handleRequest(req("/settings", { headers: htmlNav }), env, { fetch: f, now: NOW });
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("Location"), "/");
  res = await handleRequest(req("/api/overview", { cookie: `${SESSION_COOKIE_NAME}=${vector.tampered_session_token}` }), env,
    { fetch: f, now: NOW });
  assert.equal(res.status, 401);
  assert.equal(f.calls.length, 0);
});

test("public paths pass through without a cookie", async () => {
  const f = okFetch();
  const res = await handleRequest(req("/api/health"), makeEnv(), { fetch: f, now: NOW });
  assert.equal(res.status, 200);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].url, "https://spot-origin-a.swacktech.com/api/health");
});

// --- Proxy headers ---------------------------------------------------------------------------------

test("Access headers added; client CF-Access-* and X-Spot-* stripped; Authorization kept", async () => {
  const f = okFetch();
  const res = await handleRequest(authed("/api/overview?x=1&y=2", {
    headers: {
      "CF-Access-Client-Id": "evil",
      "cf-access-client-secret": "evil",
      "CF-Access-Jwt-Assertion": "evil",
      "X-Spot-Internal": "1",
      "X-Spot-Client-Ip": "198.51.100.66",
      "CF-Connecting-IP": "203.0.113.7",
      "X-Forwarded-Host": "evil.example",
      Authorization: "Bearer user-supplied",
    },
  }), makeEnv(), { fetch: f, now: NOW });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Cache-Control"), "private, no-store");
  const { url, init } = f.calls[0];
  assert.equal(url, "https://spot-origin-a.swacktech.com/api/overview?x=1&y=2");
  const h = init.headers;
  assert.equal(h.get("CF-Access-Client-Id"), "access-id.example");
  assert.equal(h.get("CF-Access-Client-Secret"), "access-secret-example");
  assert.equal(h.get("CF-Access-Jwt-Assertion"), null);
  assert.equal(h.get("X-Spot-Internal"), null);
  // The client IP comes from Cloudflare's CF-Connecting-IP, never from a client-supplied X-Spot-* header.
  assert.equal(h.get(CLIENT_IP_HEADER), "203.0.113.7");
  assert.equal(h.get("X-Forwarded-Host"), "spot.swacktech.com");
  assert.equal(h.get("X-Forwarded-Proto"), "https");
  assert.equal(h.get("Authorization"), "Bearer user-supplied");
  assert.match(h.get("Cookie"), /__Host-spot_session=/);
  assert.equal(init.redirect, "manual");
  assert.equal(init.cache, "no-store");
  assert.ok(init.signal);
});

test("mutating requests forward method, body, and Idempotency-Key", async () => {
  const f = okFetch();
  await handleRequest(authed("/api/targets", { method: "POST", body: '{"a":1}',
    headers: { "Content-Type": "application/json", "Idempotency-Key": "k-1" } }), makeEnv(), { fetch: f, now: NOW });
  const { init } = f.calls[0];
  assert.equal(init.method, "POST");
  assert.equal(new TextDecoder().decode(init.body), '{"a":1}');
  assert.equal(init.headers.get("Idempotency-Key"), "k-1");
});

// --- Bounded request bodies -----------------------------------------------------------------------

// An endless (or long) chunked body with no Content-Length; counts how much the Worker pulled.
function chunkedRequest(path, { chunk = 16_384, total = Infinity, cookie } = {}) {
  const state = { pulled: 0, cancelled: false };
  const stream = new ReadableStream({
    pull(controller) {
      if (state.pulled >= total) return controller.close();
      const size = Math.min(chunk, total - state.pulled);
      state.pulled += size;
      controller.enqueue(new Uint8Array(size).fill(0x61));
    },
    cancel() {
      state.cancelled = true;
    },
  });
  const headers = new Headers({ "Content-Type": "application/json" });
  if (cookie) headers.set("Cookie", cookie);
  const request = new Request(`https://spot.swacktech.com${path}`, { method: "POST", headers, body: stream, duplex: "half" });
  return { request, state };
}

function countingEnv() {
  const env = makeEnv();
  const get = env.SPOT_KV.get.bind(env.SPOT_KV);
  env.kvReads = 0;
  env.SPOT_KV.get = async (key) => {
    env.kvReads += 1;
    return get(key);
  };
  return env;
}

async function assertTooLarge(res, limit) {
  assert.equal(res.status, 413);
  assert.equal(res.headers.get("Cache-Control"), "no-store");
  assert.deepEqual(await res.json(), { error: "payload_too_large", max_bytes: limit });
}

test("body limits: small for sign-in routes, the backend's JSON maximum elsewhere", () => {
  assert.equal(MAX_AUTH_BODY_BYTES, 16_384);
  assert.equal(MAX_DASHBOARD_BODY_BYTES, 262_144);
  for (const path of ["/auth/device/code", "/auth/device/token", "/auth/logout", "/api/auth/login", "/api/auth/logout",
    "/AUTH/device/code", "/auth/%64evice/code", "/api//auth/login"]) {
    assert.equal(requestBodyLimit(path), MAX_AUTH_BODY_BYTES, path);
  }
  for (const path of ["/api/targets", "/api/patches/swack-tools/oxidex", "/api/authx", "/"]) {
    assert.equal(requestBodyLimit(path), MAX_DASHBOARD_BODY_BYTES, path);
  }
});

test("an oversized Content-Length is refused before the body is read or anything is forwarded", { timeout: 10_000 }, async () => {
  for (const [path, limit, cookie] of [["/auth/device/code", MAX_AUTH_BODY_BYTES, undefined],
    ["/api/targets", MAX_DASHBOARD_BODY_BYTES, `${SESSION_COOKIE_NAME}=${TOKEN}`]]) {
    const { request, state } = chunkedRequest(path, { cookie });
    request.headers.set("Content-Length", String(limit + 1));
    const f = okFetch();
    const env = countingEnv();
    await assertTooLarge(await handleRequest(request, env, { fetch: f, now: NOW }), limit);
    assert.equal(f.calls.length, 0, path);
    assert.equal(env.kvReads, 0, path); // refused before the origin lookup
    assert.ok(state.pulled <= 16_384, `${path} pulled ${state.pulled}`); // at most the stream's first prefetch
  }
  const bad = req("/auth/device/code", { method: "POST", body: "{}", headers: { "Content-Length": "12abc" } });
  const res = await handleRequest(bad, makeEnv(), { fetch: okFetch(), now: NOW });
  assert.equal(res.status, 400);
  assert.equal((await res.json()).error, "invalid_content_length");
});

test("a chunked body with no length is cut off at the limit, not buffered whole", { timeout: 10_000 }, async () => {
  for (const [path, limit, cookie] of [["/auth/device/code", MAX_AUTH_BODY_BYTES, undefined],
    ["/api/targets", MAX_DASHBOARD_BODY_BYTES, `${SESSION_COOKIE_NAME}=${TOKEN}`]]) {
    const { request, state } = chunkedRequest(path, { cookie, chunk: 4096 }); // endless
    const f = okFetch();
    await assertTooLarge(await handleRequest(request, makeEnv(), { fetch: f, now: NOW }), limit);
    assert.equal(f.calls.length, 0, path);
    assert.ok(state.cancelled, path);
    assert.ok(state.pulled <= limit + 3 * 4096, `${path} pulled ${state.pulled}`);
  }
});

test("a body exactly at the limit is forwarded whole; one byte more is refused", async () => {
  const cookie = `${SESSION_COOKIE_NAME}=${TOKEN}`;
  for (const [path, limit, c] of [["/api/targets", MAX_DASHBOARD_BODY_BYTES, cookie],
    ["/auth/device/code", MAX_AUTH_BODY_BYTES, undefined]]) {
    const f = okFetch();
    const { request } = chunkedRequest(path, { cookie: c, chunk: 5000, total: limit });
    const res = await handleRequest(request, makeEnv(), { fetch: f, now: NOW });
    assert.equal(res.status, 200, path);
    assert.equal(f.calls[0].init.body.byteLength, limit, path);
    const sized = req(path, { method: "POST", cookie: c, body: "a".repeat(limit),
      headers: { "Content-Length": String(limit) } });
    assert.equal((await handleRequest(sized, makeEnv(), { fetch: okFetch(), now: NOW })).status, 200, path);
    const { request: over } = chunkedRequest(path, { cookie: c, chunk: 5000, total: limit + 1 });
    await assertTooLarge(await handleRequest(over, makeEnv(), { fetch: okFetch(), now: NOW }), limit);
  }
});

test("sign-in routes get the small limit even when the dashboard limit would allow the body", async () => {
  const body = "a".repeat(MAX_AUTH_BODY_BYTES + 1);
  // Public device-flow routes, and the protected /api/auth/login (which a signed-out POST can't even reach).
  for (const [path, opts] of [["/auth/device/code", {}], ["/auth/device/token", {}],
    ["/api/auth/login", { cookie: `${SESSION_COOKIE_NAME}=${TOKEN}` }]]) {
    const f = okFetch();
    const res = await handleRequest(req(path, { method: "POST", body, headers: { "Content-Type": "application/json" }, ...opts }),
      makeEnv(), { fetch: f, now: NOW });
    await assertTooLarge(res, MAX_AUTH_BODY_BYTES);
    assert.equal(f.calls.length, 0, path);
  }
  const signedOut = await handleRequest(req("/api/auth/login", { method: "POST", body }), makeEnv(),
    { fetch: okFetch(), now: NOW });
  assert.equal(signedOut.status, 401); // the session check still comes first
  const f = okFetch();
  const res = await handleRequest(authed("/api/targets", { method: "POST", body }), makeEnv(), { fetch: f, now: NOW });
  assert.equal(res.status, 200);
  assert.equal(f.calls[0].init.body.byteLength, body.length);
});

test("an Idempotency-Key retry forwards the same bounded body each time", async () => {
  const f = okFetch();
  const body = JSON.stringify({ content: "x".repeat(100_000) });
  for (let attempt = 0; attempt < 2; attempt++) {
    const res = await handleRequest(authed("/api/patches/swack-tools/oxidex", { method: "POST", body,
      headers: { "Content-Type": "application/json", "Idempotency-Key": "save-1" } }), makeEnv(), { fetch: f, now: NOW });
    assert.equal(res.status, 200);
  }
  assert.equal(f.calls.length, 2);
  for (const { init } of f.calls) {
    assert.equal(init.headers.get("Idempotency-Key"), "save-1");
    assert.equal(new TextDecoder().decode(init.body), body);
  }
});

test("backend redirects and origin cache headers pass through untouched", async () => {
  const f = fakeFetch(() => new Response(null, { status: 302, headers: { Location: "https://github.com/login/oauth/authorize?x", "Cache-Control": "no-cache" } }));
  const res = await handleRequest(req("/auth/github/login"), makeEnv(), { fetch: f, now: NOW });
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("Location"), "https://github.com/login/oauth/authorize?x");
  assert.equal(res.headers.get("Cache-Control"), "no-cache");
});

// --- Outage handling ---------------------------------------------------------------------------------

async function assertWaitJson(res) {
  assert.equal(res.status, 503);
  assert.equal(res.headers.get("Retry-After"), "10");
  assert.equal(res.headers.get("Cache-Control"), "no-store");
  assert.equal(res.headers.get("Content-Type"), "application/json");
  assert.deepEqual(await res.json(), { error: UNAVAILABLE_ERROR, message: UNAVAILABLE_MESSAGE, retry_after_seconds: 10 });
}

async function assertWaitHtml(res) {
  assert.equal(res.status, 503);
  assert.equal(res.headers.get("Retry-After"), "10");
  assert.equal(res.headers.get("Cache-Control"), "no-store");
  assert.match(res.headers.get("Content-Type"), /^text\/html/);
  const html = await res.text();
  assert.match(html, /\/api\/health/);
  assert.match(html, /5000/);
  assert.match(html, /location\.reload\(\)/);
  assert.match(html, /restarting or being replaced/);
  assert.doesNotMatch(html, /<script[^>]+src=/);
}

test("no origin:active record -> wait JSON / waiting HTML", async () => {
  const env = makeEnv();
  env.SPOT_KV.store.clear();
  const f = okFetch();
  await assertWaitJson(await handleRequest(authed("/api/overview"), env, { fetch: f, now: NOW }));
  await assertWaitHtml(await handleRequest(authed("/", { headers: htmlNav }), env, { fetch: f, now: NOW }));
  assert.equal(f.calls.length, 0);
});

test("invalid or non-https origin records are rejected", async () => {
  assert.equal(parseOriginRecord(ORIGIN_RECORD).origin, "https://spot-origin-a.swacktech.com");
  const bad = [
    "not json",
    JSON.stringify({ slot: "a", url: "http://spot-origin-a.swacktech.com", vm: "v", updated_at: 1 }),
    JSON.stringify({ slot: "c", url: "https://spot-origin-a.swacktech.com", vm: "v", updated_at: 1 }),
    JSON.stringify({ slot: "a", url: "https://evil.example", vm: "v", updated_at: 1 }),
    JSON.stringify({ slot: "a", url: "https://u:p@spot-origin-a.swacktech.com", vm: "v", updated_at: 1 }),
    JSON.stringify({ slot: "a", url: "https://spot-origin-a.swacktech.com", vm: "v", updated_at: "1" }),
  ];
  for (const raw of bad) assert.equal(parseOriginRecord(raw), null, raw);
  const env = makeEnv();
  env.SPOT_KV.store.set("origin:active", bad[3]);
  const f = okFetch();
  await assertWaitJson(await handleRequest(authed("/api/overview"), env, { fetch: f, now: NOW }));
  assert.equal(f.calls.length, 0, "Access credentials must never be sent to an unexpected host");
});

test("KV read failure -> wait", async () => {
  const env = makeEnv({ SPOT_KV: { async get() { throw new Error("kv down"); } } });
  await assertWaitJson(await handleRequest(req("/api/health"), env, { fetch: okFetch(), now: NOW }));
});

test("fetch throws or times out -> wait", async () => {
  const throwing = fakeFetch(() => { throw new TypeError("network"); });
  await assertWaitJson(await handleRequest(authed("/api/overview"), makeEnv(), { fetch: throwing, now: NOW }));
  await assertWaitHtml(await handleRequest(authed("/", { headers: htmlNav }), makeEnv(), { fetch: throwing, now: NOW }));
  const hanging = fakeFetch((url, init) => new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  await assertWaitJson(await handleRequest(authed("/api/overview"), makeEnv(), { fetch: hanging, now: NOW, timeoutMs: 20 }));
});

test("530 / 502 / 504 from the tunnel or edge -> wait", async () => {
  for (const status of [502, 504, 520, 523, 530]) {
    const f = fakeFetch(() => new Response("<html>error 1033</html>", { status, headers: { "Content-Type": "text/html" } }));
    await assertWaitJson(await handleRequest(authed("/api/overview"), makeEnv(), { fetch: f, now: NOW }));
  }
  const f = fakeFetch(() => new Response("error code: 1033", { status: 530 }));
  await assertWaitHtml(await handleRequest(authed("/", { headers: htmlNav }), makeEnv(), { fetch: f, now: NOW }));
});

test("plain 503 without a wait body -> wait", async () => {
  const f = fakeFetch(() => new Response('{"error":"other"}', { status: 503, headers: { "Content-Type": "application/json" } }));
  await assertWaitJson(await handleRequest(authed("/api/overview"), makeEnv(), { fetch: f, now: NOW }));
});

test("backend's own 503 wait body passes through for API clients", async () => {
  const body = JSON.stringify({ error: UNAVAILABLE_ERROR, message: "hydrating", retry_after_seconds: 10,
    status: "starting", hydrated: false, frozen: false, state_version: 3, slot: "b", kv_writer: false });
  const f = fakeFetch(() => new Response(body, { status: 503,
    headers: { "Content-Type": "application/json", "Retry-After": "10", "X-Backend": "1" } }));
  const res = await handleRequest(req("/api/health"), makeEnv(), { fetch: f, now: NOW });
  assert.equal(res.status, 503);
  assert.equal(res.headers.get("X-Backend"), "1");
  assert.equal(res.headers.get("Cache-Control"), "no-store");
  assert.equal(await res.text(), body);
  // Top-level page loads get the waiting page instead of raw JSON.
  await assertWaitHtml(await handleRequest(authed("/", { headers: htmlNav }), makeEnv(), { fetch: f, now: NOW }));
});

test("dashboard-marked 5xx application errors pass through instead of becoming an outage", async () => {
  const marked = { "Content-Type": "application/json", [ORIGIN_MARKER_HEADER]: ORIGIN_MARKER_VALUE };
  for (const [status, body] of [[502, '{"error":"GitHub device authorization is unavailable"}'],
                                [503, '{"status":"retry","retryable":true}'], [504, '{"error":"x"}']]) {
    const f = fakeFetch(() => new Response(body, { status, headers: marked }));
    const res = await handleRequest(authed("/api/patches/save", { method: "POST", body: "{}" }), makeEnv(),
      { fetch: f, now: NOW });
    assert.equal(res.status, status);
    assert.equal(await res.text(), body);
    assert.equal(res.headers.get("Cache-Control"), "no-store");
  }
  // The dashboard's own wait body still means "wait", and an unmarked 502 (cloudflared, edge) is still an outage.
  const wait = fakeFetch(() => new Response(JSON.stringify({ error: UNAVAILABLE_ERROR }), { status: 503, headers: marked }));
  const res = await handleRequest(authed("/api/overview"), makeEnv(), { fetch: wait, now: NOW });
  assert.equal((await res.json()).error, UNAVAILABLE_ERROR);
  const bare = fakeFetch(() => new Response('{"error":"x"}', { status: 502, headers: { "Content-Type": "application/json" } }));
  await assertWaitJson(await handleRequest(authed("/api/overview"), makeEnv(), { fetch: bare, now: NOW }));
  const spoofed = fakeFetch(() => new Response("<html>", { status: 502, headers: { [ORIGIN_MARKER_HEADER]: "other" } }));
  await assertWaitJson(await handleRequest(authed("/api/overview"), makeEnv(), { fetch: spoofed, now: NOW }));
});

test("classifyOriginResponse", () => {
  const wait = JSON.stringify({ error: UNAVAILABLE_ERROR });
  assert.equal(classifyOriginResponse(200, "application/json", ""), "pass");
  assert.equal(classifyOriginResponse(500, "text/html", "boom"), "pass");
  assert.equal(classifyOriginResponse(404, "application/json", wait), "pass");
  assert.equal(classifyOriginResponse(503, "application/json", wait), "backend_wait");
  assert.equal(classifyOriginResponse(503, "application/json; charset=utf-8", wait), "backend_wait");
  assert.equal(classifyOriginResponse(503, "text/html", wait), "unavailable");
  assert.equal(classifyOriginResponse(530, "text/html", "<html>"), "unavailable");
  assert.equal(classifyOriginResponse(502, "application/json", "{"), "unavailable");
  assert.equal(classifyOriginResponse(502, "application/json", "{}", true), "application_error");
  assert.equal(classifyOriginResponse(503, "application/json", wait, true), "backend_wait");
});

test("default export fetch handler wires env through", async () => {
  const res = await worker.fetch(req("/api/control/snapshot"), makeEnv());
  assert.equal(res.status, 404);
});
