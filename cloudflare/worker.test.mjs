// Unit tests for cloudflare/worker.js. Run with: node --test cloudflare/
// Uses only node:test and node:assert, a fake KV, and a fake fetch; no network.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import worker, {
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
    ALLOWED_ADMIN_LOGIN: "swackhamer",
    ALLOWED_ADMIN_ID: "619624",
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

const authed = (path, opts = {}) => req(path, { ...opts, cookie: `spot_session=${TOKEN}` });
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

test("purpose label and wait strings match the Python contract", () => {
  assert.equal(SESSION_PURPOSE_LABEL, vector.purpose_label);
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

test("wrong sub or uid fails even when correctly signed", async () => {
  const base = { ...vector.valid_session.payload };
  const env = makeEnv();
  assert.ok(await verifySession(await signPayload(base), env, NOW));
  assert.equal(await verifySession(await signPayload({ ...base, sub: "someone" }), env, NOW), null);
  assert.equal(await verifySession(await signPayload({ ...base, sub: "Swackhamer" }), env, NOW), null);
  assert.equal(await verifySession(await signPayload({ ...base, uid: 1 }), env, NOW), null);
  assert.equal(await verifySession(await signPayload({ ...base, uid: "619624" }), env, NOW), null);
  assert.equal(await verifySession(await signPayload({ ...base, v: 2 }), env, NOW), null);
  assert.equal(await verifySession(await signPayload({ ...base, exp: base.iat + 604801 }), env, NOW), null);
});

test("missing bindings fail closed", async () => {
  for (const key of ["SESSION_SECRET", "ALLOWED_ADMIN_LOGIN", "ALLOWED_ADMIN_ID"]) {
    assert.equal(await verifySession(TOKEN, makeEnv({ [key]: undefined }), NOW), null, key);
  }
});

test("cookie parsing takes the first occurrence", () => {
  const c = parseCookies(`a=1; spot_session=first; spot_session=second`);
  assert.equal(c.get("spot_session"), "first");
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
  res = await handleRequest(req("/api/overview", { cookie: `spot_session=${vector.tampered_session_token}` }), env,
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
  assert.equal(h.get("X-Forwarded-Host"), "spot.swacktech.com");
  assert.equal(h.get("X-Forwarded-Proto"), "https");
  assert.equal(h.get("Authorization"), "Bearer user-supplied");
  assert.match(h.get("Cookie"), /spot_session=/);
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
});

test("default export fetch handler wires env through", async () => {
  const res = await worker.fetch(req("/api/control/snapshot"), makeEnv());
  assert.equal(res.status, 404);
});
