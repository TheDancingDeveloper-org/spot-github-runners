#!/usr/bin/env python3
"""Start the gha-spot runner VM when a matching GitHub job is queued.

Runs on Node B (systemd timer or a Komodo stack). Standard library only; the
service-account JWT is signed with the `openssl` CLI. The VM powers itself off
when idle, so this process only starts it, and stops it when the monthly
running-hour cap is reached.

Environment:
  GH_TOKEN            org "Self-hosted runners: write" + Actions read on REPOS
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
  POLL_INTERVAL       seconds between polls, default 60
  RECONCILE_INTERVAL  seconds between scans of in_progress runs for queued later jobs, default 300
  RATE_LIMIT_FLOOR    pause GitHub calls until X-RateLimit-Reset below this many left, default 200
  LOG_LEVEL           info (default) or debug (adds the remaining GitHub quota once per poll)
  --once              one poll then exit (for a systemd timer); default loops every POLL_INTERVAL
"""
import base64
import datetime as dt
import json
import os
import subprocess
import sys
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
    signing_input = f"{header}.{claims}".encode()
    keyfd_r, keyfd_w = os.pipe()
    os.write(keyfd_w, key["private_key"].encode())
    os.close(keyfd_w)
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{keyfd_r}"],
                         input=signing_input, capture_output=True, check=True, pass_fds=(keyfd_r,)).stdout
    os.close(keyfd_r)
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


class RateLimited(Exception):
    """GitHub's quota is spent or nearly so; no GitHub calls until `until`."""

    def __init__(self, until):
        super().__init__(f"GitHub rate limit: paused until {dt.datetime.fromtimestamp(until, dt.timezone.utc):%H:%M:%SZ}")
        self.until = until


# The last X-RateLimit-* seen, and when GitHub calls may resume.
_rate = {"remaining": None, "limit": None, "reset": 0, "paused_until": 0}


def _note_rate(headers):
    if headers.get("X-RateLimit-Remaining") is None:
        return
    _rate.update(remaining=int(headers["X-RateLimit-Remaining"]), limit=headers.get("X-RateLimit-Limit"),
                 reset=int(headers.get("X-RateLimit-Reset") or 0))


def gh(method, path, body=None):
    """A GitHub API call that stops at the rate limit instead of retrying into it."""
    if time.time() < _rate["paused_until"]:
        raise RateLimited(_rate["paused_until"])
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://api.github.com/{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {cred('GH_TOKEN', 'gh_token')}", "Accept": "application/vnd.github+json",
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


def queued_jobs(classes=None):
    """Queued jobs whose runs-on labels are all served by the GCP runners.

    Each poll lists only queued runs. A later job in a run that is already
    in_progress is found by the slower reconcile scan, every RECONCILE_INTERVAL.
    With `classes`, returns (ref, class) pairs; otherwise refs served by LABELS."""
    classes = classes or {"persistent": {"labels": LABELS}}
    found = []
    statuses = ["queued"]
    if time.time() - _last_reconcile[0] >= RECONCILE_INTERVAL:
        statuses.append("in_progress")
    for repo in REPOS:
        for status in statuses:
            runs = gh("GET", f"repos/{ORG}/{repo}/actions/runs?status={status}&per_page=50")
            for run in runs.get("workflow_runs", []):
                jobs = gh("GET", f"repos/{ORG}/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
                for job in jobs.get("jobs", []):
                    cls = job_class(job.get("labels", []), classes) if job["status"] == "queued" else None
                    if cls:
                        found.append((f"{repo}#{job['id']}", cls))
    if "in_progress" in statuses:
        _last_reconcile[0] = time.time()
    return found if "persistent" not in classes else [ref for ref, _ in found]


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
    jobs = queued_jobs()
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

    jobs = queued_jobs(CLASSES)
    if not jobs:
        state.pop("waiting", None)
        return
    if state.get("budget_locked"):
        log(f"{len(jobs)} queued but budget-locked; not creating VMs")
        return
    plan, waiting = plan_vms(jobs, [vm_class(v) for v in live], used, CLASSES, VCPU_QUOTA)
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
        log(f"poll ok: {INSTANCE} {status}, {state.get('running_seconds', 0) / 3600:.1f}h running this month")


def main():
    if not PROJECT:
        sys.exit("GCP_PROJECT is required")
    if MODE == "ephemeral" and not CLASSES:
        sys.exit("LARGE_TEMPLATE (or GCP_TEMPLATE) or SMALL_TEMPLATE is required in ephemeral mode")
    once = "--once" in sys.argv
    while True:
        state = load_state()
        try:
            (poll_ephemeral if MODE == "ephemeral" else poll)(state)
        except RateLimited as error:
            log(str(error))
        except (urllib.error.URLError, subprocess.CalledProcessError, KeyError) as error:
            log(f"poll failed: {error}")
        save_state(state)
        debug(f"GitHub quota {_rate['remaining']}/{_rate['limit']} remaining, resets "
              f"{dt.datetime.fromtimestamp(_rate['reset'], dt.timezone.utc):%H:%M:%SZ}")
        if once:
            return
        time.sleep(INTERVAL)  # GCP clean-up carries on; gh() itself waits out a rate-limit pause


if __name__ == "__main__":
    main()
