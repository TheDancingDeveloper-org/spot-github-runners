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
  RUNNER_LABEL        default gcp-spot
  MAX_HOURS_PER_MONTH default 250 (belt and braces under the $30 budget)
  STATE_FILE          default ~/.local/state/gha-spot-poller.json
  --once              one poll then exit (for a systemd timer); default loops every 30 s
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
REPOS = os.environ.get("REPOS", "vogt,codex,rustnzb,msp-klaudia").split(",")
LABEL = os.environ.get("RUNNER_LABEL", "gcp-spot")
PROJECT = os.environ.get("GCP_PROJECT", "")
ZONE = os.environ.get("GCP_ZONE", "australia-southeast1-c")
INSTANCE = os.environ.get("GCP_INSTANCE", "gha-spot-1")
MAX_HOURS = float(os.environ.get("MAX_HOURS_PER_MONTH", "250"))
STATE_FILE = os.path.expanduser(os.environ.get("STATE_FILE", "~/.local/state/gha-spot-poller.json"))
INTERVAL = 30


def log(msg):
    print(f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


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
    key = json.load(open(os.environ["GCP_SA_KEY_FILE"]))
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64url(json.dumps({
        "iss": key["client_email"], "aud": key["token_uri"], "iat": now, "exp": now + 3600,
        "scope": "https://www.googleapis.com/auth/compute"}).encode())
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


# ---------------------------------------------------------------- GitHub
def gh(method, path, body=None):
    return http(method, f"https://api.github.com/{path}", os.environ["GH_TOKEN"], body)


def queued_jobs():
    """Queued jobs whose runs-on labels are all served by the gcp-spot runner."""
    served = {"self-hosted", "linux", "x64", LABEL, f"{LABEL}-16"}
    found = []
    for repo in REPOS:
        for status in ("queued", "in_progress"):
            runs = gh("GET", f"repos/{ORG}/{repo}/actions/runs?status={status}&per_page=50")
            for run in runs.get("workflow_runs", []):
                jobs = gh("GET", f"repos/{ORG}/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
                for job in jobs.get("jobs", []):
                    labels = set(job.get("labels", []))
                    if job["status"] == "queued" and LABEL in labels and labels <= served:
                        found.append(f"{repo}#{job['id']}")
    return found


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
    labels = inst.get("labels", {})
    now = time.time()
    if status == "RUNNING" and state.get("last_poll"):
        state["running_seconds"] += min(now - state["last_poll"], 10 * INTERVAL)
    state["last_poll"] = now
    hours = state["running_seconds"] / 3600

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


def main():
    if not PROJECT:
        sys.exit("GCP_PROJECT is required")
    once = "--once" in sys.argv
    while True:
        state = load_state()
        try:
            poll(state)
        except (urllib.error.URLError, subprocess.CalledProcessError, KeyError) as error:
            log(f"poll failed: {error}")
        save_state(state)
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
