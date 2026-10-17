// Cloudflare Worker for https://spot.swacktech.com.
//
// Gates the Spot dashboard with the dashboard's own signed session cookie, then proxies to the control
// VM that the KV key `origin:active` names (a Cloudflare Tunnel hostname behind Cloudflare Access).
// While the control VM is restarting or being replaced, the Worker answers with the shared 503 "wait"
// contract so browsers keep the page open and retry automatically.
//
// ES module Worker with no npm dependencies. The contract lives in src/lib/edge_state.py and
// src/lib/dashboard_auth.py; keep the constants below in sync with them.
//
// Bindings (see wrangler.toml):
//   SPOT_KV                  KV namespace holding `origin:active`.
//   SESSION_SECRET           Secret; same value as DASHBOARD_SESSION_SECRET on the control VM.
//   CF_ACCESS_CLIENT_ID      Secret; Cloudflare Access service token id for the origin hostnames.
//   CF_ACCESS_CLIENT_SECRET  Secret; Cloudflare Access service token secret.
//   ALLOWED_ADMIN_LOGIN      Var; the only GitHub login allowed through (swackhamer).
//   ALLOWED_ADMIN_ID         Var; that login's numeric GitHub id (619624).
//   ORIGIN_HOST_SUFFIX       Optional var; origin hostnames must end with it (default ".swacktech.com").

export const SESSION_COOKIE_NAME = "spot_session";
export const SESSION_PURPOSE_LABEL = "spot-dashboard/session/v1";
export const KV_ORIGIN_KEY = "origin:active";
export const UNAVAILABLE_ERROR = "control_vm_unavailable";
export const UNAVAILABLE_MESSAGE =
  "The control VM is restarting or being replaced. Keep this page open; " +
  "your request will be retried automatically when it is back.";
export const RETRY_AFTER_SECONDS = 10;
export const ORIGIN_TIMEOUT_MS = 30_000;
export const MAX_TOKEN_CHARS = 4096;
export const MAX_SESSION_TTL_SECONDS = 604_800;
export const CLOCK_SKEW_SECONDS = 60;
export const DEFAULT_ORIGIN_HOST_SUFFIX = ".swacktech.com";
const MAX_ERROR_BODY_BYTES = 65_536;
const CONTROL_SLOTS = new Set(["a", "b"]);

// Reachable without a session cookie. "/" is public for GET/HEAD only (it renders the login gate).
export const PUBLIC_PATHS = new Set([
  "/auth/github/login",
  "/auth/github/callback",
  "/auth/status",
  "/api/auth/status",
  "/auth/logout",
  "/api/auth/logout",
  "/api/health",
  "/favicon.ico",
]);

// Upstream statuses that mean "origin unreachable" unless the body is the backend's own wait body.
export function isUnavailableStatus(status) {
  return status === 502 || status === 503 || status === 504 || (status >= 520 && status <= 530);
}

export function unavailablePayload(retryAfter = RETRY_AFTER_SECONDS) {
  return { error: UNAVAILABLE_ERROR, message: UNAVAILABLE_MESSAGE, retry_after_seconds: retryAfter };
}

const encoder = new TextEncoder();

function noStoreHeaders(contentType) {
  return {
    "Content-Type": contentType,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  };
}

export function jsonResponse(status, body, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...noStoreHeaders("application/json"), ...extraHeaders },
  });
}

// --- Session verification -------------------------------------------------------------------------

export function parseCookies(header) {
  const cookies = new Map();
  if (typeof header !== "string") return cookies;
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq <= 0) continue;
    const name = part.slice(0, eq).trim();
    let value = part.slice(eq + 1).trim();
    if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
    if (name && !cookies.has(name)) cookies.set(name, value); // First occurrence wins, like the backend.
  }
  return cookies;
}

function b64urlDecode(text) {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return bytes;
}

export function bytesToHex(bytes) {
  return Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, "0")).join("");
}

// Constant-time comparison of two strings of equal length (length itself is not secret).
export function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function hmacKey(rawKey) {
  return crypto.subtle.importKey("raw", rawKey, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
}

// key = HMAC-SHA256(secret, "spot-dashboard/session/v1"), matching dashboard_auth.derive_purpose_key.
export async function deriveSessionKey(secret) {
  const outer = await hmacKey(encoder.encode(secret));
  return new Uint8Array(await crypto.subtle.sign("HMAC", outer, encoder.encode(SESSION_PURPOSE_LABEL)));
}

// Returns the payload of a correctly signed token, or null. Never throws on malformed input.
export async function verifyTokenSignature(keyBytes, token) {
  if (typeof token !== "string" || !token || token.length > MAX_TOKEN_CHARS) return null;
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const [body, sig] = parts;
  if (!/^[A-Za-z0-9_-]+$/.test(body) || !/^[0-9a-f]{64}$/.test(sig)) return null;
  const key = await hmacKey(keyBytes);
  const expected = bytesToHex(await crypto.subtle.sign("HMAC", key, encoder.encode(body)));
  if (!constantTimeEqual(sig, expected)) return null;
  try {
    const data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(b64urlDecode(body)));
    return data !== null && typeof data === "object" && !Array.isArray(data) ? data : null;
  } catch {
    return null;
  }
}

function isInt(value) {
  return typeof value === "number" && Number.isSafeInteger(value);
}

// Full session check: signature, v == 1, iat - 60 <= now < exp, bounded TTL, and the single allowed
// admin (login AND numeric id). Fails closed when any binding is missing.
export async function verifySession(token, env, nowSeconds = Math.floor(Date.now() / 1000)) {
  const secret = env && env.SESSION_SECRET;
  const login = env && env.ALLOWED_ADMIN_LOGIN;
  const adminId = env && Number(env.ALLOWED_ADMIN_ID);
  if (!secret || !login || !isInt(adminId) || adminId <= 0) return null;
  let payload;
  try {
    payload = await verifyTokenSignature(await deriveSessionKey(secret), token);
  } catch {
    return null;
  }
  if (!payload || payload.v !== 1) return null;
  const { sub, uid, iat, exp, tgt } = payload;
  if (!isInt(iat) || !isInt(exp) || exp <= iat || exp - iat > MAX_SESSION_TTL_SECONDS) return null;
  if (!(iat - CLOCK_SKEW_SECONDS <= nowSeconds && nowSeconds < exp)) return null;
  if (typeof sub !== "string" || sub !== login) return null;
  if (!isInt(uid) || uid !== adminId) return null;
  if (tgt !== undefined && (!Array.isArray(tgt) || !tgt.every((t) => typeof t === "string"))) return null;
  return payload;
}

// --- Routing --------------------------------------------------------------------------------------

// Decodes percent escapes, resolves dot segments, collapses slashes, and lowercases, so encoded or
// oddly cased spellings of /api/control cannot slip past the block.
export function normalizePathForBlocking(pathname) {
  let decoded = pathname;
  for (let i = 0; i < 3; i++) {
    let next;
    try {
      next = decodeURIComponent(decoded);
    } catch {
      return null; // Malformed escapes: caller blocks.
    }
    if (next === decoded) break;
    decoded = next;
  }
  decoded = decoded.replace(/\\/g, "/").replace(/\/{2,}/g, "/");
  try {
    decoded = new URL(decoded.startsWith("/") ? decoded : `/${decoded}`, "https://normalize.invalid").pathname;
  } catch {
    return null;
  }
  return decoded.replace(/\/{2,}/g, "/").toLowerCase();
}

export function isControlPath(pathname) {
  const normalized = normalizePathForBlocking(pathname);
  if (normalized === null) return true;
  return /^\/api\/control(?![a-z0-9_-])/.test(normalized);
}

// "control" (blocked), "public" (no cookie check), or "protected".
export function classifyRoute(method, pathname) {
  if (isControlPath(pathname)) return "control";
  const m = method.toUpperCase();
  if (pathname === "/" && (m === "GET" || m === "HEAD")) return "public";
  if (PUBLIC_PATHS.has(pathname)) return "public";
  return "protected";
}

export function isHtmlNavigation(request) {
  const method = request.method.toUpperCase();
  if (method !== "GET" && method !== "HEAD") return false;
  const { pathname } = new URL(request.url);
  if (pathname === "/api" || pathname.startsWith("/api/")) return false;
  const mode = request.headers.get("Sec-Fetch-Mode");
  const dest = request.headers.get("Sec-Fetch-Dest");
  if (mode === "navigate" || dest === "document") return true;
  if (mode || dest) return false; // A fetch()/XHR from a modern browser.
  return (request.headers.get("Accept") || "").includes("text/html");
}

export function unauthorizedResponse(request) {
  const method = request.method.toUpperCase();
  const { pathname } = new URL(request.url);
  const isApi = pathname === "/api" || pathname.startsWith("/api/");
  if (isApi || (method !== "GET" && method !== "HEAD")) {
    return jsonResponse(401, { error: "unauthorized" });
  }
  return new Response(null, { status: 302, headers: { Location: "/", "Cache-Control": "no-store" } });
}

// --- Origin selection -----------------------------------------------------------------------------

export function parseOriginRecord(raw, hostSuffix = DEFAULT_ORIGIN_HOST_SUFFIX) {
  if (typeof raw !== "string" || !raw) return null;
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (data === null || typeof data !== "object" || Array.isArray(data)) return null;
  const { slot, url, vm, updated_at: updatedAt } = data;
  if (!CONTROL_SLOTS.has(slot) || typeof url !== "string" || !url.startsWith("https://")) return null;
  if (typeof vm !== "string" || !isInt(updatedAt)) return null;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || !parsed.hostname) return null;
  const host = parsed.hostname.toLowerCase();
  if (hostSuffix && !host.endsWith(hostSuffix.toLowerCase())) return null;
  return { slot, url, vm, updated_at: updatedAt, origin: parsed.origin };
}

export async function readOrigin(env) {
  if (!env || !env.SPOT_KV || typeof env.SPOT_KV.get !== "function") return null;
  const raw = await env.SPOT_KV.get(KV_ORIGIN_KEY);
  return parseOriginRecord(raw, env.ORIGIN_HOST_SUFFIX ?? DEFAULT_ORIGIN_HOST_SUFFIX);
}

// --- Proxying -------------------------------------------------------------------------------------

const HOP_BY_HOP = ["connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer"];

export function buildOriginHeaders(request, env) {
  const incoming = new URL(request.url);
  const headers = new Headers();
  for (const [name, value] of request.headers) {
    const lower = name.toLowerCase();
    if (lower.startsWith("cf-access-") || lower.startsWith("x-spot-") || lower.startsWith("x-forwarded-")) continue;
    if (lower === "host" || lower === "content-length" || HOP_BY_HOP.includes(lower)) continue;
    headers.append(name, value);
  }
  if (env.CF_ACCESS_CLIENT_ID) headers.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID);
  if (env.CF_ACCESS_CLIENT_SECRET) headers.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET);
  headers.set("X-Forwarded-Host", incoming.host);
  headers.set("X-Forwarded-Proto", "https");
  return headers;
}

export function isWaitBody(text) {
  if (typeof text !== "string" || !text) return false;
  try {
    const data = JSON.parse(text);
    return data !== null && typeof data === "object" && data.error === UNAVAILABLE_ERROR;
  } catch {
    return false;
  }
}

// "pass": forward untouched; "backend_wait": the backend's own wait body; "unavailable": origin down.
export function classifyOriginResponse(status, contentType, bodyText) {
  if (!isUnavailableStatus(status)) return "pass";
  const json = typeof contentType === "string" && contentType.toLowerCase().includes("application/json");
  return json && isWaitBody(bodyText) ? "backend_wait" : "unavailable";
}

async function readBoundedText(response) {
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  while (total <= MAX_ERROR_BODY_BYTES) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.byteLength;
  }
  reader.cancel().catch(() => {});
  const joined = new Uint8Array(Math.min(total, MAX_ERROR_BODY_BYTES + 1));
  let offset = 0;
  for (const chunk of chunks) {
    const take = Math.min(chunk.byteLength, joined.length - offset);
    joined.set(chunk.subarray(0, take), offset);
    offset += take;
    if (offset >= joined.length) break;
  }
  return total > MAX_ERROR_BODY_BYTES ? "" : new TextDecoder().decode(joined);
}

function withNoStore(response) {
  const out = new Response(response.body, response);
  if (!out.headers.has("Cache-Control")) out.headers.set("Cache-Control", "private, no-store");
  return out;
}

// --- Wait responses -------------------------------------------------------------------------------

export const WAITING_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Spot dashboard is restarting</title>
<style>
  :root { color-scheme: light dark; --bg: #f6f7f9; --fg: #1d232b; --muted: #5b6573; --card: #ffffff; --accent: #2f6fde; }
  @media (prefers-color-scheme: dark) { :root { --bg: #12161c; --fg: #e6e9ee; --muted: #9aa4b2; --card: #1b2129; --accent: #6b9cf0; } }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--fg);
         font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 16px; box-sizing: border-box; }
  main { max-width: 34rem; background: var(--card); border-radius: 12px; padding: 28px; box-shadow: 0 1px 4px rgba(0,0,0,.12); }
  h1 { font-size: 1.25rem; margin: 0 0 12px; }
  p { margin: 0 0 12px; }
  .status { color: var(--muted); font-size: .9rem; display: flex; align-items: center; gap: 10px; margin: 0; }
  .spinner { width: 14px; height: 14px; border-radius: 50%; border: 2px solid var(--muted); border-top-color: var(--accent);
             animation: spin 1s linear infinite; flex: none; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }
</style>
</head>
<body>
<main>
  <h1>The Spot dashboard is coming back</h1>
  <p>The control VM is restarting or being replaced, for example after a Spot preemption or a planned move to a cheaper machine.</p>
  <p>Keep this page open. Work resumes automatically: this page reloads as soon as the dashboard answers again, and saved dashboard state carries over to the new VM.</p>
  <p class="status" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span><span id="status">Checking every 5 seconds...</span></p>
</main>
<script>
(function () {
  var statusEl = document.getElementById("status");
  var attempts = 0;
  function check() {
    attempts += 1;
    fetch("/api/health", { cache: "no-store", credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (res) {
        if (res.status === 200) { statusEl.textContent = "Back online. Reloading..."; window.location.reload(); return; }
        statusEl.textContent = "Still waiting (check " + attempts + ", HTTP " + res.status + "). Checking every 5 seconds...";
        setTimeout(check, 5000);
      })
      .catch(function () {
        statusEl.textContent = "Still waiting (check " + attempts + "). Checking every 5 seconds...";
        setTimeout(check, 5000);
      });
  }
  setTimeout(check, 5000);
})();
</script>
</body>
</html>
`;

const WAITING_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; " +
  "base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

export function waitResponse(request) {
  const retry = { "Retry-After": String(RETRY_AFTER_SECONDS) };
  if (request && isHtmlNavigation(request)) {
    return new Response(request.method.toUpperCase() === "HEAD" ? null : WAITING_HTML, {
      status: 503,
      headers: { ...noStoreHeaders("text/html; charset=utf-8"), ...retry, "Content-Security-Policy": WAITING_CSP },
    });
  }
  return jsonResponse(503, unavailablePayload(), retry);
}

// --- Request handler ------------------------------------------------------------------------------

export async function handleRequest(request, env, options = {}) {
  const fetchImpl = options.fetch || ((input, init) => fetch(input, init));
  const nowSeconds = options.now ?? Math.floor(Date.now() / 1000);
  const timeoutMs = options.timeoutMs ?? ORIGIN_TIMEOUT_MS;
  const url = new URL(request.url);

  const route = classifyRoute(request.method, url.pathname);
  if (route === "control") return jsonResponse(404, { error: "not_found" });
  if (route === "protected") {
    const token = parseCookies(request.headers.get("Cookie")).get(SESSION_COOKIE_NAME);
    if (!(await verifySession(token, env, nowSeconds))) return unauthorizedResponse(request);
  }

  let origin;
  try {
    origin = await readOrigin(env);
  } catch {
    origin = null;
  }
  if (!origin) return waitResponse(request);

  const method = request.method.toUpperCase();
  const init = {
    method,
    headers: buildOriginHeaders(request, env),
    redirect: "manual",
    cache: "no-store",
  };
  if (method !== "GET" && method !== "HEAD") init.body = await request.arrayBuffer();

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  init.signal = controller.signal;
  let upstream;
  try {
    upstream = await fetchImpl(origin.origin + url.pathname + url.search, init);
  } catch {
    clearTimeout(timer);
    return waitResponse(request);
  }

  try {
    if (!isUnavailableStatus(upstream.status)) return withNoStore(upstream);
    const contentType = upstream.headers.get("Content-Type") || "";
    const text = await readBoundedText(upstream);
    const kind = classifyOriginResponse(upstream.status, contentType, text);
    if (kind === "backend_wait" && !isHtmlNavigation(request)) {
      const headers = new Headers(upstream.headers);
      headers.set("Cache-Control", "no-store");
      if (!headers.has("Retry-After")) headers.set("Retry-After", String(RETRY_AFTER_SECONDS));
      headers.delete("Content-Length");
      return new Response(method === "HEAD" ? null : text, { status: upstream.status, headers });
    }
    // Origin down, or the backend's wait body on a top-level page load: show the waiting page / JSON.
    return waitResponse(request);
  } catch {
    return waitResponse(request);
  } finally {
    clearTimeout(timer);
  }
}

export default {
  async fetch(request, env) {
    try {
      return await handleRequest(request, env);
    } catch {
      return waitResponse(request);
    }
  },
};
