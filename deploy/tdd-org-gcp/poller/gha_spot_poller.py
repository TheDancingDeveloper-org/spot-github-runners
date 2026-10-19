#!/usr/bin/env python3
"""Start the gha-spot runner VM when a matching GitHub job is queued.

Runs on Node B (systemd timer or a Komodo stack). Standard library only; the
service-account JWT is signed with the `openssl` CLI. The VM powers itself off
when idle, so this process only starts it, and stops it when the monthly
running-hour cap is reached.

Environment:
  GH_APP_ID, GH_APP_INSTALLATION_ID   authenticate as the gha-spot-poller GitHub App; the
                      private key is the gh_app_key credential (or GH_APP_KEY). Installation
                      tokens are minted with openssl and reused for 55 minutes
  GH_TOKEN            fallback when the App variables are unset: org "Self-hosted runners:
                      write" + Actions read on REPOS (gh_token credential)
  GCP_SA_KEY_FILE     JSON key for gha-spot-poller@PROJECT.iam.gserviceaccount.com
  GCP_PROJECT, GCP_ZONE, GCP_INSTANCE (default gha-spot-1)
  GH_ORG              default TheDancingDeveloper-org
  REPOS               comma list, default vogt,codex,rustnzb,msp-klaudia
  RUNNER_LABELS       persistent mode: comma list; a job needs one of them and nothing
                      outside them plus self-hosted/linux/x64 (RUNNER_LABEL is the fallback)
  MAX_HOURS_PER_MONTH default 250 (belt and braces under the budget)
  GCP_BUDGET_SUBSCRIPTION default gha-spot-budget-poller (budget Pub/Sub; >=100% stops and locks the VM)
  STATE_FILE          default ~/.local/state/gha-spot-poller.json
  MODE                persistent (start/stop GCP_INSTANCE) or ephemeral (one throwaway VM per job)
  RUNNER_GROUP_ID, MAX_VM_AGE_MIN, VCPU_QUOTA (default 32)   ephemeral mode settings
  {LARGE,SMALL}_TEMPLATE, _LABELS, _VCPUS, _MAX   ephemeral VM classes (an empty
                      template disables the class; GCP_TEMPLATE/RUNNER_LABELS/MAX_VMS
                      are the large class's fallbacks)
  POLL_INTERVAL       seconds between polls, default 60. Without webhooks, GitHub is listed
                      every poll; with them, only GCP housekeeping runs at this pace
  RECONCILE_INTERVAL  seconds between scans of in_progress runs for queued later jobs, and with
                      webhooks on, between reconcile listings of queued runs; default 300
  WEBHOOK_PORT        serve the App's workflow_job webhook on POST /webhook/github; 0 (default)
                      keeps polling only. The secret is the gh_webhook_secret credential (or
                      GH_WEBHOOK_SECRET); unsigned or wrongly signed deliveries are refused
  WEBHOOK_BIND        listen address, default 127.0.0.1 (the reverse proxy forwards one path)
  WEBHOOK_SILENT_MIN  no delivery for this long while jobs are queued drops back to a
                      POLL_INTERVAL listing until deliveries resume, default 15
  IN_PROGRESS_SCAN_MIN  with deliveries flowing, minutes between listings of in_progress runs,
                      which alone show a started run's later queued jobs; default 15
  MISSED_WEBHOOK_S    a queued job the reconcile finds that no delivery announced, older than
                      this, counts as a missed webhook, default 120
  RATE_LIMIT_FLOOR    pause GitHub calls until X-RateLimit-Reset below this many left, default 200
  LOG_LEVEL           info (default) or debug (adds the remaining GitHub quota once per poll)
  MONITOR_PORT, ...   monitoring endpoints and runner inventory: see runner_monitor.py
  --once              one poll then exit (for a systemd timer); default loops every POLL_INTERVAL
"""
import base64
import collections
import datetime as dt
import hashlib
import hmac
import http.server as http_server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ORG = os.environ.get("GH_ORG", "TheDancingDeveloper-org")
REPOS = os.environ.get("REPOS", "vogt,codex,rustnzb").split(",")
LABELS = [l.strip() for l in os.environ.get("RUNNER_LABELS", os.environ.get("RUNNER_LABEL", "rust-heavy")).split(",") if l.strip()]
assert "publish" not in LABELS, "publish jobs carry release secrets; never serve them here"
PROJECT = os.environ.get("GCP_PROJECT", "")
ZONE = os.environ.get("GCP_ZONE", "australia-southeast1-c")
INSTANCE = os.environ.get("GCP_INSTANCE", "gha-spot-1")
MAX_HOURS = float(os.environ.get("MAX_HOURS_PER_MONTH", "250"))
STATE_FILE = os.path.expanduser(os.environ.get("STATE_FILE", "~/.local/state/gha-spot-poller.json"))
INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "300"))
RATE_LIMIT_FLOOR = int(os.environ.get("RATE_LIMIT_FLOOR", "200"))
DEBUG = os.environ.get("LOG_LEVEL", "info").lower() == "debug"
BUDGET_SUB = os.environ.get("GCP_BUDGET_SUBSCRIPTION", "gha-spot-budget-poller")
# MODE=persistent (default): start/stop the long-lived GCP_INSTANCE.
# MODE=ephemeral: create one throwaway VM per queued job from GCP_TEMPLATE with a
# just-in-time runner registration, and delete it once it has powered off.
MODE = os.environ.get("MODE", "persistent")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "0"))
WEBHOOK_BIND = os.environ.get("WEBHOOK_BIND", "127.0.0.1")
WEBHOOK_PATH = "/webhook/github"
WEBHOOK_MAX_BODY = 1024 * 1024  # workflow_job payloads are tens of KB
WEBHOOK_SILENT_S = int(os.environ.get("WEBHOOK_SILENT_MIN", "15")) * 60
MISSED_WEBHOOK_S = int(os.environ.get("MISSED_WEBHOOK_S", "120"))
# With deliveries flowing, the in_progress listing (later jobs of started runs) runs this often.
IN_PROGRESS_SCAN_S = int(os.environ.get("IN_PROGRESS_SCAN_MIN", "15")) * 60


def _labels(raw):
    labels = [l.strip() for l in raw.split(",") if l.strip()]
    assert "publish" not in labels, "publish jobs carry release secrets; never serve them here"
    return labels


# Ephemeral VM classes. The project's vCPU quota (32) fits one large VM or four
# small ones, never both, so creation is gated on VCPU_QUOTA below.
CLASSES = {}
for _cls, _prefix, _tpl, _labels_default, _vcpus, _max in (
        ("large", "gha-rh-", os.environ.get("GCP_TEMPLATE", ""), ",".join(LABELS), "30", os.environ.get("MAX_VMS", "1")),
        ("small", "gha-rs-", "", "gcp-rust", "8", "4")):
    _env = _cls.upper()
    _template = os.environ.get(f"{_env}_TEMPLATE", _tpl)
    if _template:
        CLASSES[_cls] = {
            "prefix": _prefix, "template": _template,
            "labels": _labels(os.environ.get(f"{_env}_LABELS", _labels_default)),
            "vcpus": int(os.environ.get(f"{_env}_VCPUS", _vcpus)),
            "max": int(os.environ.get(f"{_env}_MAX", _max))}
VCPU_QUOTA = int(os.environ.get("VCPU_QUOTA", "32"))
RUNNER_GROUP_ID = int(os.environ.get("RUNNER_GROUP_ID", "6"))
MAX_VM_AGE_S = int(os.environ.get("MAX_VM_AGE_MIN", "420")) * 60  # backstop over the VM's own 6 h limit


def log(msg):
    print(f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def debug(msg):
    if DEBUG:
        log(f"debug: {msg}")


def http(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "Content-Type": "application/json", "User-Agent": "gha-spot-poller"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------- GCP auth
_gcp = {"token": None, "exp": 0}


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def rs256_sign(pem, data):
    """RS256 signature of `data` with the PEM key, which reaches openssl only through a pipe."""
    keyfd_r, keyfd_w = os.pipe()
    os.write(keyfd_w, pem.encode())
    os.close(keyfd_w)
    try:
        return subprocess.run(["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{keyfd_r}"],
                              input=data, capture_output=True, check=True, pass_fds=(keyfd_r,)).stdout
    finally:
        os.close(keyfd_r)


def gcp_token():
    if _gcp["token"] and time.time() < _gcp["exp"] - 120:
        return _gcp["token"]
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    path = os.path.join(d, "gcp_sa_key") if d and os.path.exists(os.path.join(d, "gcp_sa_key")) else os.environ["GCP_SA_KEY_FILE"]
    key = json.load(open(path))
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64url(json.dumps({
        "iss": key["client_email"], "aud": key["token_uri"], "iat": now, "exp": now + 3600,
        "scope": "https://www.googleapis.com/auth/cloud-platform"}).encode())
    sig = rs256_sign(key["private_key"], f"{header}.{claims}".encode())
    assertion = f"{header}.{claims}.{b64url(sig)}"
    body = f"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion={assertion}".encode()
    req = urllib.request.Request(key["token_uri"], data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.load(resp)
    _gcp.update(token=tok["access_token"], exp=now + int(tok["expires_in"]))
    return _gcp["token"]


def compute(method, suffix="", body=None):
    url = f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances/{INSTANCE}{suffix}"
    return http(method, url, gcp_token(), body)


def budget_exhausted():
    """Drain budget notifications; True if any reports actual spend >= budget."""
    url = f"https://pubsub.googleapis.com/v1/projects/{PROJECT}/subscriptions/{BUDGET_SUB}"
    resp = http("POST", url + ":pull", gcp_token(), {"maxMessages": 50})
    msgs = resp.get("receivedMessages", [])
    hit = False
    for m in msgs:
        data = json.loads(base64.b64decode(m["message"].get("data", "")) or b"{}")
        if float(data.get("costAmount", 0)) >= float(data.get("budgetAmount", float("inf"))):
            hit = True
    if msgs:
        http("POST", url + ":acknowledge", gcp_token(), {"ackIds": [m["ackId"] for m in msgs]})
    return hit


def lock_and_stop(inst):
    labels = dict(inst.get("labels", {}), **{"gha-budget-locked": "true"})
    compute("POST", "/setLabels", {"labels": labels, "labelFingerprint": inst["labelFingerprint"]})
    if inst["status"] in ("RUNNING", "PROVISIONING", "STAGING"):
        compute("POST", "/stop")
    log(f"budget reached: {INSTANCE} stopped and labelled gha-budget-locked=true")


# ---------------------------------------------------------------- GitHub
def cred(env_name, cred_name):
    """Secret from $CREDENTIALS_DIRECTORY (systemd LoadCredentialEncrypted) or the environment."""
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    if d and os.path.exists(os.path.join(d, cred_name)):
        return open(os.path.join(d, cred_name)).read().strip()
    return os.environ[env_name]


# GitHub App installation token (GH_APP_ID + GH_APP_INSTALLATION_ID + the
# gh_app_key credential). GitHub issues it for an hour; reuse it for 55 minutes.
GH_APP_ID = os.environ.get("GH_APP_ID", "")
GH_APP_INSTALLATION_ID = os.environ.get("GH_APP_INSTALLATION_ID", "")
GH_APP_TOKEN_TTL = 55 * 60
_gh_app = {"token": None, "exp": 0}


def gh_app_jwt(pem, app_id, now=None):
    now = int(time.time()) if now is None else now
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    # iat 60 s back for clock skew; GitHub rejects exp more than 10 minutes out.
    claims = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    return f"{header}.{claims}.{b64url(rs256_sign(pem, f'{header}.{claims}'.encode()))}"


def gh_token():
    """Bearer token for GitHub: the App installation token, or the old gh_token PAT
    when GH_APP_ID / GH_APP_INSTALLATION_ID are unset."""
    if not (GH_APP_ID and GH_APP_INSTALLATION_ID):
        return cred("GH_TOKEN", "gh_token")
    if _gh_app["token"] and time.time() < _gh_app["exp"]:
        return _gh_app["token"]
    now = int(time.time())
    jwt = gh_app_jwt(cred("GH_APP_KEY", "gh_app_key"), GH_APP_ID, now)
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{GH_APP_INSTALLATION_ID}/access_tokens", data=b"",
        method="POST", headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json",
                                "User-Agent": "gha-spot-poller"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.load(resp)
    _gh_app.update(token=tok["token"], exp=now + GH_APP_TOKEN_TTL)
    log(f"github: minted installation token for app {GH_APP_ID} (reused for {GH_APP_TOKEN_TTL // 60} min)")
    return _gh_app["token"]


class RateLimited(Exception):
    """GitHub's quota is spent or nearly so; no GitHub calls until `until`."""

    def __init__(self, until):
        super().__init__(f"GitHub rate limit: paused until {dt.datetime.fromtimestamp(until, dt.timezone.utc):%H:%M:%SZ}")
        self.until = until


# The last X-RateLimit-* seen, and when GitHub calls may resume.
_rate = {"remaining": None, "limit": None, "reset": 0, "paused_until": 0}
# GitHub calls since the last heartbeat, the measure for the webhook stage's exit gate.
_calls = {"count": 0}


def _note_rate(headers):
    if headers.get("X-RateLimit-Remaining") is None:
        return
    _rate.update(remaining=int(headers["X-RateLimit-Remaining"]), limit=headers.get("X-RateLimit-Limit"),
                 reset=int(headers.get("X-RateLimit-Reset") or 0))


def gh(method, path, body=None):
    """A GitHub API call that stops at the rate limit instead of retrying into it."""
    if time.time() < _rate["paused_until"]:
        raise RateLimited(_rate["paused_until"])
    _calls["count"] += 1
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://api.github.com/{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {gh_token()}", "Accept": "application/vnd.github+json",
        "Content-Type": "application/json", "User-Agent": "gha-spot-poller"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _note_rate(resp.headers)
            raw = resp.read()
    except urllib.error.HTTPError as error:
        _note_rate(error.headers)
        if error.code in (403, 429) and (_rate["remaining"] == 0 or error.headers.get("Retry-After")):
            retry_after = int(error.headers.get("Retry-After") or 0)
            until = time.time() + retry_after if retry_after else _rate["reset"] + 5
            _rate["paused_until"] = max(until, time.time() + INTERVAL)
            raise RateLimited(_rate["paused_until"]) from error
        raise
    if _rate["remaining"] is not None and _rate["remaining"] < RATE_LIMIT_FLOOR:
        # Leave the rest of the shared quota to other users of the token.
        _rate["paused_until"] = _rate["reset"] + 5
    return json.loads(raw) if raw else {}


BASE_LABELS = {"self-hosted", "linux", "x64"}


def job_class(labels, classes):
    """The class whose labels cover a job's runs-on labels, or None.

    Bare [self-hosted] jobs match no class and are left to the on-prem fleet."""
    extra = {l.lower() for l in labels} - BASE_LABELS
    for name, cls in classes.items():
        if extra and extra <= {l.lower() for l in cls["labels"]}:
            return name
    return None


_last_reconcile = [0.0]


def gh_time(stamp):
    """Epoch seconds from a GitHub ISO timestamp; 0 if absent or malformed."""
    try:
        return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def scan_jobs(classes, statuses):
    """(ref, class, created) for queued jobs, in runs of the given statuses, that a class serves."""
    found = []
    for repo in REPOS:
        for status in statuses:
            runs = gh("GET", f"repos/{ORG}/{repo}/actions/runs?status={status}&per_page=50")
            for run in runs.get("workflow_runs", []):
                jobs = gh("GET", f"repos/{ORG}/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
                for job in jobs.get("jobs", []):
                    cls = job_class(job.get("labels", []), classes) if job["status"] == "queued" else None
                    if cls:
                        found.append((f"{repo}#{job['id']}", cls, gh_time(job.get("created_at"))))
    return found


def queued_jobs(classes=None):
    """Queued jobs whose runs-on labels are all served by the GCP runners.

    Each poll lists only queued runs. A later job in a run that is already
    in_progress is found by the slower reconcile scan, every RECONCILE_INTERVAL.
    With `classes`, returns (ref, class) pairs; otherwise refs served by LABELS."""
    classes = classes or {"persistent": {"labels": LABELS}}
    statuses = ["queued"]
    if time.time() - _last_reconcile[0] >= RECONCILE_INTERVAL:
        statuses.append("in_progress")
    found = [(ref, cls) for ref, cls, _ in scan_jobs(classes, statuses)]
    if "in_progress" in statuses:
        _last_reconcile[0] = time.time()
    return found if "persistent" not in classes else [ref for ref, _ in found]


# ---------------------------------------------------------------- webhook intake
def verify_signature(secret, body, header):
    """True if `header` is GitHub's X-Hub-Signature-256 for `body` under `secret`."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret, body, hashlib.sha256).hexdigest().encode()
    got = header[len("sha256="):].strip().lower().encode("utf-8", "replace")
    return hmac.compare_digest(want, got)


class JobTable:
    """Queued jobs announced by workflow_job deliveries, corrected by the reconcile listing.

    The webhook thread writes it and the poll loop reads it, so every method
    takes the lock. `known` remembers each job's last state for a day, which
    keeps a late `queued` delivery from reviving a started job and lets the
    reconcile tell a missed delivery from one that arrived."""

    DELIVERY_MEMORY = 10000
    KNOWN_TTL_S = 24 * 3600

    def __init__(self, classes, now=None):
        self.classes = classes
        self.lock = threading.Lock()
        self.queued = {}      # ref -> {"cls", "created", "noted"}
        self.known = {}       # ref -> (state, noted)
        self.deliveries = collections.OrderedDict()
        self.last_delivery = self.started = time.time() if now is None else now
        self.stats = collections.Counter()
        self.baseline = {}
        self.observer = None  # called as observer(action, ref, class, job) outside the lock

    def count(self, name):
        with self.lock:
            self.stats[name] += 1

    def restore(self, stats):
        with self.lock:
            self.stats.update({k: int(v) for k, v in (stats or {}).items()})
            self.baseline = dict(self.stats)

    def snapshot(self):
        with self.lock:
            return dict(self.stats)

    def forget_delivery(self, delivery_id):
        """Let a redelivery through after its first copy failed to apply."""
        with self.lock:
            self.deliveries.pop(delivery_id, None)

    def first_delivery(self, delivery_id, now=None):
        """True the first time a delivery id is seen; GitHub redelivers with the same id."""
        with self.lock:
            self.last_delivery = time.time() if now is None else now
            self.stats["deliveries_total"] += 1
            if delivery_id in self.deliveries:
                self.stats["duplicate_deliveries_total"] += 1
                return False
            self.deliveries[delivery_id] = True
            while len(self.deliveries) > self.DELIVERY_MEMORY:
                self.deliveries.popitem(last=False)
            return True

    def apply(self, payload, now=None):
        """Apply one workflow_job payload. True if a served job was newly queued."""
        now = time.time() if now is None else now
        job = payload.get("workflow_job") or {}
        repo_obj = payload.get("repository") or {}
        owner = (repo_obj.get("owner") or {}).get("login", "")
        repo = next((r for r in REPOS if r.lower() == str(repo_obj.get("name", "")).lower()), None)
        if owner.lower() != ORG.lower() or repo is None or "id" not in job:
            return False
        cls = job_class(job.get("labels", []), self.classes)
        if cls is None:
            return False
        ref = f"{repo}#{job['id']}"
        action = payload.get("action")
        with self.lock:
            self._prune(now)
            previous = self.known.get(ref, (None, 0))[0]
            if ref not in self.known:
                self.stats["jobs_seen_total"] += 1
            if action == "queued":
                if previous in ("in_progress", "completed"):
                    return False  # deliveries can arrive out of order
                self.known[ref] = ("queued", now)
                fresh = ref not in self.queued
                self.queued[ref] = {"cls": cls, "created": gh_time(job.get("created_at")) or now, "noted": now}
                return fresh
            if action not in ("in_progress", "completed") or previous == "completed":
                return False  # a late in_progress must not mark a finished job running
            self.known[ref] = (action, now)
            self.queued.pop(ref, None)
        if self.observer is not None:
            self.observer(action, ref, cls, job, previous)
        return False

    def reconcile(self, found, started, now=None, complete=True):
        """Take the reconcile listing, begun at `started`, as the truth for queued jobs.

        Only a `complete` listing (in_progress runs included) can show a job has
        gone: a queued-runs listing never holds the later jobs of a started run.

        Returns the refs no delivery announced that have been queued longer
        than MISSED_WEBHOOK_S; each one is counted once."""
        now = time.time() if now is None else now
        missed = []
        with self.lock:
            self._prune(now)
            listed = {ref for ref, _, _ in found}
            for ref, job in list(self.queued.items()):
                # A delivery newer than the listing may announce a job the listing missed.
                if complete and ref not in listed and job["noted"] < started:
                    del self.queued[ref]
            for ref, cls, created in found:
                state = self.known.get(ref, (None, 0))[0]
                if state in ("in_progress", "completed"):
                    continue
                if state is None:
                    self.stats["jobs_seen_total"] += 1
                    state = "listed"
                if state == "listed":
                    # Jobs queued before the intake started had no delivery to miss; a young
                    # one is checked again by the next listing.
                    if created >= self.started and now - created > MISSED_WEBHOOK_S:
                        self.stats["missed_webhooks_total"] += 1
                        missed.append(ref)
                        state = "missed"
                    self.known[ref] = (state, now)
                self.queued.setdefault(ref, {"cls": cls, "created": created or now, "noted": now})
        return missed

    def pending(self):
        """(ref, class) for queued jobs, oldest first."""
        with self.lock:
            return [(ref, j["cls"]) for ref, j in sorted(self.queued.items(), key=lambda kv: kv[1]["created"])]

    def _prune(self, now):
        for ref, (_, noted) in list(self.known.items()):
            if now - noted > self.KNOWN_TTL_S and ref not in self.queued:
                del self.known[ref]


_refusal_log = [0.0]


class WebhookHandler(http_server.BaseHTTPRequestHandler):
    """POST /webhook/github only. Set `table`, `secret` and `wake` on a subclass."""

    table = None
    secret = b""
    wake = None
    timeout = 10  # a stalled or trickling client loses its thread after this many seconds
    server_version = "gha-spot-poller"
    sys_version = ""

    def _reply(self, code, text):
        body = (text + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != WEBHOOK_PATH:
            return self._reply(404, "not found")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(411, "length required")
        if length < 0 or length > WEBHOOK_MAX_BODY:
            return self._reply(413, "too large")
        body = self.rfile.read(length)
        if not verify_signature(self.secret, body, self.headers.get("X-Hub-Signature-256")):
            self.table.count("rejected_deliveries_total")
            now = time.time()
            if now - _refusal_log[0] >= 60:  # anyone can send these; keep the journal quiet
                _refusal_log[0] = now
                log("webhook: refused a delivery with a missing or wrong signature (counted in the heartbeat)")
            return self._reply(401, "bad signature")
        delivery = self.headers.get("X-GitHub-Delivery", "")
        if not delivery:
            return self._reply(400, "no delivery id")
        if not self.table.first_delivery(delivery):
            return self._reply(200, "duplicate")
        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            log("webhook: ping received")
            return self._reply(200, "pong")
        if event != "workflow_job":
            return self._reply(202, "ignored")
        try:
            payload = json.loads(body)
        except ValueError:
            return self._reply(400, "bad json")
        if not isinstance(payload, dict):
            return self._reply(400, "bad json")
        try:
            fresh = self.table.apply(payload)
        except (AttributeError, TypeError, ValueError):
            self.table.forget_delivery(delivery)
            return self._reply(400, "bad payload")
        if fresh and self.wake is not None:
            self.wake.set()
        return self._reply(200, "ok")

    def do_GET(self):
        self._reply(404, "not found")

    do_PUT = do_DELETE = do_PATCH = do_GET

    def log_message(self, fmt, *args):
        debug("webhook: " + fmt % args)


def start_webhook_server(table, secret, wake, bind=None, port=None):
    """Serve the webhook on a daemon thread; returns the server (port 0 picks a free one)."""
    handler = type("Handler", (WebhookHandler,), {"table": table, "secret": secret, "wake": wake})
    server = http_server.ThreadingHTTPServer((bind or WEBHOOK_BIND, WEBHOOK_PORT if port is None else port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="webhook", daemon=True).start()
    log(f"webhook: listening on {server.server_address[0]}:{server.server_address[1]}{WEBHOOK_PATH}")
    return server


def reconcile_cadence(now, last_listing, last_delivery, queued_seen):
    """(listing due, webhooks silent). With deliveries flowing GitHub is listed every
    RECONCILE_INTERVAL; after WEBHOOK_SILENT_S without one while jobs are queued, every
    POLL_INTERVAL, as before webhooks."""
    silent = queued_seen and now - last_delivery >= WEBHOOK_SILENT_S
    return now - last_listing >= (INTERVAL if silent else RECONCILE_INTERVAL), silent


# The webhook job table (None: polling only) and the reconcile's own clock.
_intake = {"table": None, "last_listing": 0.0, "last_in_progress": 0.0, "queued_seen": False, "silent": False}


def pending_jobs(classes):
    """(ref, class) for queued jobs: the GitHub listing every poll without webhooks,
    or the webhook job table, corrected by a reconcile listing, with them."""
    table = _intake["table"]
    if table is None:
        found = queued_jobs(classes)
        return [(ref, next(iter(classes))) for ref in found] if "persistent" in classes else found
    now = time.time()
    due, silent = reconcile_cadence(now, _intake["last_listing"], table.last_delivery, _intake["queued_seen"])
    if silent != _intake["silent"]:
        _intake["silent"] = silent
        log(f"webhook: no delivery for {WEBHOOK_SILENT_S // 60} min while jobs are queued; listing GitHub "
            f"every {INTERVAL} s" if silent else f"webhook: deliveries resumed; reconciling every {RECONCILE_INTERVAL} s")
    if due:
        statuses = ["queued"]
        # Later jobs in an already running run show up only under in_progress; deliveries
        # cover them, so that listing runs every IN_PROGRESS_SCAN_S, or every
        # RECONCILE_INTERVAL while the webhooks are silent (as before webhooks).
        if now - _intake["last_in_progress"] >= (RECONCILE_INTERVAL if silent else IN_PROGRESS_SCAN_S):
            statuses.append("in_progress")
        try:
            found = scan_jobs(classes, statuses)
        except RateLimited as error:
            log(f"reconcile skipped: {error}")  # deliveries keep the table current meanwhile
        else:
            _intake.update(last_listing=now, queued_seen=bool(found))
            if "in_progress" in statuses:
                _intake["last_in_progress"] = now
            for ref in table.reconcile(found, now, complete="in_progress" in statuses):
                log(f"missed_webhook: {ref} was queued with no workflow_job delivery")
    return table.pending()


# ---------------------------------------------------------------- state
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def poll(state):
    month = dt.date.today().strftime("%Y-%m")
    if state.get("month") != month:
        state.update(month=month, running_seconds=0)
    inst = compute("GET")
    status = inst["status"]
    heartbeat(state, status)
    labels = inst.get("labels", {})
    now = time.time()
    if status == "RUNNING" and state.get("last_poll"):
        state["running_seconds"] += min(now - state["last_poll"], 10 * INTERVAL)
    state["last_poll"] = now
    hours = state["running_seconds"] / 3600

    if labels.get("gha-budget-locked") != "true" and budget_exhausted():
        lock_and_stop(inst)
        return

    if status == "RUNNING" and hours >= MAX_HOURS:
        log(f"monthly cap {MAX_HOURS}h reached ({hours:.1f}h); stopping {INSTANCE}")
        compute("POST", "/stop")
        return
    if status not in ("TERMINATED", "STOPPED"):
        return  # running, starting or stopping: the VM manages its own idle power-off
    jobs = [ref for ref, _ in pending_jobs({"persistent": {"labels": LABELS}})]
    if not jobs:
        return
    if labels.get("gha-budget-locked") == "true":
        log(f"{len(jobs)} queued but VM is budget-locked; not starting")
        return
    if hours >= MAX_HOURS:
        log(f"{len(jobs)} queued but monthly cap {MAX_HOURS}h reached; not starting")
        return
    # Fresh one-hour registration token; the VM uses it only if not yet registered.
    reg = gh("POST", f"orgs/{ORG}/actions/runners/registration-token")["token"]
    items = [i for i in inst.get("metadata", {}).get("items", []) if i["key"] != "gha-reg-token"]
    items.append({"key": "gha-reg-token", "value": reg})
    compute("POST", "/setMetadata", {"fingerprint": inst["metadata"]["fingerprint"], "items": items})
    compute("POST", "/start")
    log(f"started {INSTANCE} for {', '.join(jobs[:5])} ({hours:.1f}h used this month)")


# ---------------------------------------------------------------- ephemeral mode
def zone_api(method, suffix="", body=None):
    url = f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}{suffix}"
    return http(method, url, gcp_token(), body)


def ephemeral_vms():
    resp = zone_api("GET", "/instances?filter=labels.gha-ephemeral%3Dtrue&maxResults=50")
    return resp.get("items", [])


def vm_vcpus(vm):
    """vCPUs from the machine type (c3d-highcpu-8 -> 8); 0 if it has no count."""
    suffix = vm.get("machineType", "").rsplit("-", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0


def vm_class(vm):
    """A VM's class from its template label, or for older VMs from its vCPUs."""
    if "gha-class" in vm.get("labels", {}):
        return vm["labels"]["gha-class"]
    return next((n for n, c in CLASSES.items() if c["vcpus"] == vm_vcpus(vm)), "large")


def template_metadata_items(template):
    name = template.rsplit("/", 1)[-1]
    tpl = http("GET", f"https://compute.googleapis.com/compute/v1/projects/{PROJECT}/global/instanceTemplates/{name}",
               gcp_token())
    return tpl["properties"].get("metadata", {}).get("items", []), name


_monitor = [None]  # runner_monitor.Monitor when MONITOR_PORT is set
_recent_creates = {}  # VM name -> (class, insert time), until the VM list shows it


def create_vm(job_ref, cls_name):
    """Create a VM of the class for a job. False (no raise) if GCP refuses it."""
    cls = CLASSES[cls_name]
    name = cls["prefix"] + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
    items, tpl_name = template_metadata_items(cls["template"])
    jit = gh("POST", f"orgs/{ORG}/actions/runners/generate-jitconfig", {
        "name": name, "runner_group_id": RUNNER_GROUP_ID,
        "labels": ["self-hosted", "Linux", "X64", *cls["labels"]], "work_folder": "_work"})["encoded_jit_config"]
    # An instance body's metadata replaces the template's, so carry its items over.
    items = [i for i in items if i["key"] != "gha-jitconfig"] + [{"key": "gha-jitconfig", "value": jit}]
    try:
        zone_api("POST", f"/instances?sourceInstanceTemplate=projects/{PROJECT}/global/instanceTemplates/{tpl_name}",
                 {"name": name, "metadata": {"items": items}})
    except urllib.error.HTTPError as error:
        # Quota or capacity refusals: the job stays queued and the next poll retries.
        # The unused JIT runner registration expires on its own.
        log(f"could not create {cls_name} VM {name} for {job_ref}: HTTP {error.code} {error.read()[:300]!r}")
        return False
    _recent_creates[name] = (cls_name, time.time())
    log(f"created {cls_name} VM {name} for {job_ref}")
    return True


def delete_vm(name, why):
    zone_api("DELETE", f"/instances/{name}")
    log(f"deleting {name} ({why})")


def poll_ephemeral(state):
    month = dt.date.today().strftime("%Y-%m")
    if state.get("month") != month:
        state.update(month=month, running_seconds=0)
    now = time.time()
    vms = ephemeral_vms()
    live = [v for v in vms if v["status"] not in ("TERMINATED", "STOPPING", "STOPPED", "SUSPENDED")]
    # STOPPING VMs still hold their vCPUs against the quota.
    used = sum(vm_vcpus(v) or CLASSES.get(vm_class(v), {"vcpus": VCPU_QUOTA})["vcpus"]
               for v in vms if v["status"] not in ("TERMINATED", "STOPPED", "SUSPENDED"))
    if state.get("last_poll"):
        state["running_seconds"] += len([v for v in live if v["status"] == "RUNNING"]) * min(now - state["last_poll"], 10 * INTERVAL)
    state["last_poll"] = now
    hours = state["running_seconds"] / 3600
    heartbeat(state, f"{len(live)} live / {len(vms)} total throwaway VMs, {used}/{VCPU_QUOTA} vCPU")
    if _monitor[0] is not None:
        _monitor[0].set_gcp(
            vms=[{"class": vm_class(v), "status": v["status"],
                  "age": now - dt.datetime.fromisoformat(v["creationTimestamp"]).timestamp()} for v in vms],
            vcpu_used=used, month_hours=hours, budget_locked=bool(state.get("budget_locked")))

    for v in vms:  # finished (powered off) or overdue VMs go, with their disks
        created = dt.datetime.fromisoformat(v["creationTimestamp"]).timestamp()
        if v["status"] == "TERMINATED":
            delete_vm(v["name"], "finished")
        elif now - created > MAX_VM_AGE_S:
            delete_vm(v["name"], f"older than {MAX_VM_AGE_S // 60} min")

    if not state.get("budget_locked") and budget_exhausted():
        state["budget_locked"] = True
        for v in live:
            delete_vm(v["name"], "budget reached")
        log("budget reached: throwaway VMs deleted; set budget_locked=false in the state file to unlock")
        return
    if hours >= MAX_HOURS:
        for v in live:
            delete_vm(v["name"], f"monthly cap {MAX_HOURS}h reached")
        return

    jobs = pending_jobs(CLASSES)
    if not jobs:
        state.pop("waiting", None)
        return
    if state.get("budget_locked"):
        log(f"{len(jobs)} queued but budget-locked; not creating VMs")
        return
    # A VM inserted in the last two minutes may not be listed yet; count it as live so a
    # wake-up seconds later does not create a second one for the same job.
    listed = {v["name"] for v in vms}
    for name, (cls_name, at) in list(_recent_creates.items()):
        if name in listed or now - at > 120:
            del _recent_creates[name]
    pending = [c for c, _ in _recent_creates.values()]
    used += sum(CLASSES[c]["vcpus"] for c in pending if c in CLASSES)
    plan, waiting = plan_vms(jobs, [vm_class(v) for v in live] + pending, used, CLASSES, VCPU_QUOTA)
    if waiting and waiting != state.get("waiting"):
        log(waiting)  # once per change, not every poll
    state["waiting"] = waiting
    for job_ref, cls_name in plan:
        create_vm(job_ref, cls_name)


def plan_vms(jobs, live_classes, used, classes, quota):
    """Which (job, class) VMs to create now, within the vCPU quota.

    A VM that has not yet picked up its job still counts against its class's
    queue. Large jobs go first: while one waits for vCPUs, no new small VMs
    start, so the running small ones drain and free the quota."""
    plan, waiting = [], ""
    for name in ("large", "small"):
        if name not in classes or (waiting and name == "small"):
            continue
        cls = classes[name]
        queued = [ref for ref, c in jobs if c == name]
        want = min(len(queued), cls["max"]) - live_classes.count(name)
        for ref in queued[:max(want, 0)]:
            if used + cls["vcpus"] > quota:
                waiting = (f"{name} job {ref} waits: needs {cls['vcpus']} vCPU, "
                           f"{used}/{quota} in use")
                break
            plan.append((ref, name))
            used += cls["vcpus"]
    return plan, waiting


_last_heartbeat = [0.0]


def heartbeat(state, status):
    """Log the first successful poll, then once an hour, so the journal proves liveness."""
    now = time.time()
    if now - _last_heartbeat[0] >= 3600:
        _last_heartbeat[0] = now
        log(f"poll ok: {INSTANCE} {status}, {state.get('running_seconds', 0) / 3600:.1f}h running this month, "
            f"{_calls['count']} GitHub calls since the last heartbeat")
        _calls["count"] = 0
        if _intake["table"] is not None:
            stats = _intake["table"].snapshot()
            log(f"webhook: {stats.get('jobs_seen_total', 0)} jobs seen, "
                f"{stats.get('missed_webhooks_total', 0)} missed, "
                f"{stats.get('rejected_deliveries_total', 0)} refused deliveries")


def main():
    if not PROJECT:
        sys.exit("GCP_PROJECT is required")
    if MODE == "ephemeral" and not CLASSES:
        sys.exit("LARGE_TEMPLATE (or GCP_TEMPLATE) or SMALL_TEMPLATE is required in ephemeral mode")
    once = "--once" in sys.argv
    wake = threading.Event()
    if WEBHOOK_PORT and not once:
        try:
            secret = cred("GH_WEBHOOK_SECRET", "gh_webhook_secret").encode()
        except KeyError:
            secret = b""
        if not secret:
            sys.exit("WEBHOOK_PORT is set but the webhook secret is empty")
        table = JobTable(CLASSES if MODE == "ephemeral" else {"persistent": {"labels": LABELS}})
        table.restore(load_state().get("webhook_stats"))
        _intake["table"] = table
        start_webhook_server(table, secret, wake)
    if os.environ.get("MONITOR_PORT", "0") not in ("", "0") and not once:
        try:  # monitoring must never stop the poller from serving jobs
            import runner_monitor  # installed beside this file
            _monitor[0] = runner_monitor.Monitor(sys.modules[__name__])
            runner_monitor.start(_monitor[0], sys.modules[__name__])
        except (ImportError, OSError, ValueError) as error:
            log(f"monitor: not started: {error}")
            _monitor[0] = None
        if _monitor[0] is not None and _intake["table"] is not None:
            _intake["table"].observer = _monitor[0].job_event
    while True:
        state = load_state()
        ok = False
        try:
            (poll_ephemeral if MODE == "ephemeral" else poll)(state)
            ok = True
        except RateLimited as error:
            log(str(error))
        except (urllib.error.URLError, subprocess.CalledProcessError, KeyError) as error:
            log(f"poll failed: {error}")
        if _intake["table"] is not None:
            state["webhook_stats"] = _intake["table"].snapshot()
        save_state(state)
        if _monitor[0] is not None:
            _monitor[0].loop_done(ok)
        debug(f"GitHub quota {_rate['remaining']}/{_rate['limit']} remaining, resets "
              f"{dt.datetime.fromtimestamp(_rate['reset'], dt.timezone.utc):%H:%M:%SZ}")
        if once:
            return
        # GCP clean-up carries on; gh() itself waits out a rate-limit pause. A delivery that
        # queues a served job wakes the loop early, at most once every 5 s.
        started = time.time()
        wake.wait(INTERVAL)
        wake.clear()
        time.sleep(max(0.0, 5 - (time.time() - started)))


if __name__ == "__main__":
    main()
