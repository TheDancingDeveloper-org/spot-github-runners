"""Monitoring for the Spot poller: Prometheus metrics, health, and the runner and queue JSON (WI-1892).

Stage 2 of docs/runner-manager.md. The poller passes itself in as `poller`, so
this module imports nothing from it and stays standard-library only.

Environment:
  MONITOR_PORT        serve GET /metrics, /healthz, /api/v1/runners and /api/v1/queue; 0
                      (default) turns monitoring off. Never route this port through the
                      public proxy: it is for the private network only
  MONITOR_BIND        listen address, default 127.0.0.1
  RO_GH_APP_ID, RO_GH_APP_INSTALLATION_ID   the read-only App that lists the org's
                      runners; its key is the ro_gh_app_key credential (or RO_GH_APP_KEY).
                      Unset, the runner inventory is off and only GCP and job data is served
  INVENTORY_INTERVAL  seconds between runner listings, default 120
  BUILD_SHA           commit the installed poller came from, for runner_manager_build_info
"""
import bisect
import json
import os
import threading
import time
import urllib.error
import urllib.request
import http.server as http_server

MONITOR_PORT = int(os.environ.get("MONITOR_PORT", "0") or 0)
MONITOR_BIND = os.environ.get("MONITOR_BIND", "127.0.0.1")
INVENTORY_INTERVAL = int(os.environ.get("INVENTORY_INTERVAL", "120"))
BUILD_SHA = os.environ.get("BUILD_SHA", "unknown")
RO_GH_APP_ID = os.environ.get("RO_GH_APP_ID", "")
RO_GH_APP_INSTALLATION_ID = os.environ.get("RO_GH_APP_INSTALLATION_ID", "")

WAIT_BUCKETS = (10, 30, 60, 120, 300, 600, 1200, 1800, 3600)
DURATION_BUCKETS = (60, 300, 600, 1200, 1800, 3600, 7200, 14400, 21600)

# Static classes by their exclusive label, checked after the GCP classes (docs/runner-manager.md §4).
STATIC_CLASSES = (("publish", "static-publish"), ("docker", "static-docker"), ("bigmem", "static-bigmem"),
                  ("static-bigmem", "static-bigmem"), ("rust", "static-rust"))
BASE_LABELS = {"self-hosted", "linux", "x64", "windows", "macos", "arm64"}


def num(value):
    """Exact sample text: ints as written, floats round-trip (":g" keeps only 6 digits)."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def esc(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def labels_text(labels):
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in sorted(labels.items())) + "}"


class Histogram:
    def __init__(self, buckets):
        self.buckets = tuple(buckets)
        self.series = {}  # label tuple -> [counts per bucket, sum, count]

    def observe(self, labels, value):
        key = tuple(sorted(labels.items()))
        counts, total, n = self.series.get(key, ([0] * len(self.buckets), 0.0, 0))
        idx = bisect.bisect_left(self.buckets, value)
        for i in range(idx, len(self.buckets)):
            counts[i] += 1
        self.series[key] = (counts, total + value, n + 1)

    def render(self, name):
        lines = []
        for key, (counts, total, n) in sorted(self.series.items()):
            base = dict(key)
            for bound, c in zip(self.buckets, counts):
                lines.append(f"{name}_bucket{labels_text(dict(base, le=bound))} {c}")
            lines.append(f"{name}_bucket{labels_text(dict(base, le='+Inf'))} {n}")
            lines.append(f"{name}_sum{labels_text(base)} {num(total)}")
            lines.append(f"{name}_count{labels_text(base)} {n}")
        return lines


def runner_class(name, labels, gcp_classes):
    """The class of a runner: a GCP class by its VM name prefix (`bigmem` is on both a
    GCP class and the static host until stage 3), else a static class by label, wsl, or other."""
    for cls, spec in gcp_classes.items():
        if spec.get("prefix") and str(name).startswith(spec["prefix"]):
            return f"gcp-{cls}"
    names = {l.lower() for l in labels} - BASE_LABELS
    for label, cls in STATIC_CLASSES:
        if label in names:
            return cls
    if any("wsl" in n for n in names):
        return "wsl"
    return "other"


def runner_state(runner):
    if runner.get("status") != "online":
        return "offline"
    return "busy" if runner.get("busy") else "idle"


class Monitor:
    """Everything the endpoints serve. The poll loop, the webhook thread and the
    inventory thread write it; the HTTP threads read it; one lock covers it all."""

    def __init__(self, poller, clock=time.time):
        self.poller = poller
        self.clock = clock
        self.lock = threading.Lock()
        self.started = clock()
        self.last_loop = 0.0
        self.last_ok = 0.0
        self.gcp = {}           # vms, vcpu_used, vcpu_quota, month_hours, hours_cap, budget_locked, max_vm_age_s
        self.runners = []       # org runners from the read-only App
        self.inventory_at = 0.0
        self.inventory_error = ""
        self.ro_rate = None
        self.running = {}       # runner name -> job ref, from in_progress deliveries
        self.wait = Histogram(WAIT_BUCKETS)
        self.duration = Histogram(DURATION_BUCKETS)

    # ------------------------------------------------------------ writers
    def loop_done(self, ok=True):
        with self.lock:
            self.last_loop = self.clock()
            if ok:
                self.last_ok = self.last_loop

    def set_gcp(self, **values):
        with self.lock:
            self.gcp.update(values)

    def job_event(self, action, ref, cls, job, previous=None):
        """Called by the job table for each served workflow_job delivery."""
        repo = ref.split("#", 1)[0]
        created = self.poller.gh_time(job.get("created_at"))
        started = self.poller.gh_time(job.get("started_at"))
        done = self.poller.gh_time(job.get("completed_at"))
        runner = job.get("runner_name") or ""
        cls = f"gcp-{cls}"  # the runner metrics' vocabulary, so the two can be joined
        with self.lock:
            if action == "in_progress" and previous != "completed":
                if created and started >= created:
                    self.wait.observe({"tier": "normal", "class": cls}, started - created)
                if runner:
                    self.running[runner] = ref
            elif action == "completed":
                if started and done >= started:
                    self.duration.observe({"repo": repo, "class": cls}, done - started)
                for name, running_ref in list(self.running.items()):
                    if running_ref == ref:
                        del self.running[name]

    def set_runners(self, runners, rate=None):
        with self.lock:
            self.runners = runners
            self.inventory_at = self.clock()
            self.inventory_error = ""
            if rate is not None:
                self.ro_rate = rate

    def inventory_failed(self, error):
        with self.lock:
            self.inventory_error = str(error)[:200] or type(error).__name__

    def inventory_fresh(self):
        with self.lock:
            return bool(self.inventory_at) and self.clock() - self.inventory_at <= 3 * INVENTORY_INTERVAL

    # ------------------------------------------------------------ readers
    def healthy(self):
        """The loop finished within the last three intervals (or is still in its first)."""
        with self.lock:
            last = self.last_loop or self.started
        return self.clock() - last <= 3 * self.poller.INTERVAL + 30

    def queue(self):
        table = self.poller._intake.get("table")
        now = self.clock()
        if table is None:
            return {"source": "polling", "jobs": []}
        with table.lock:
            jobs = [{"ref": ref, "repo": ref.split("#", 1)[0], "class": j["cls"], "tier": "normal",
                     "planned_class": j["cls"], "wait_seconds": round(max(0.0, now - j["created"]))}
                    for ref, j in sorted(table.queued.items(), key=lambda kv: kv[1]["created"])]
        return {"source": "webhook", "jobs": jobs}

    def runner_rows(self):
        with self.lock:
            runners, running, at = list(self.runners), dict(self.running), self.inventory_at
            # A runner the inventory shows idle has no job, whatever deliveries were missed.
            busy = {r.get("name") for r in runners if r.get("busy")}
            for name in [n for n in self.running if n not in busy and at]:
                del self.running[name]
            running = {n: ref for n, ref in running.items() if n in busy or not at}
        classes = self.poller.CLASSES
        return [{"name": r.get("name"), "class": runner_class(r.get("name", ""), [l.get("name", "") for l in r.get("labels", [])], classes),
                 "labels": [l.get("name") for l in r.get("labels", [])], "state": runner_state(r),
                 "current_job": running.get(r.get("name")), "last_seen": round(at) if at else None}
                for r in runners]

    def metrics(self):
        p = self.poller
        now = self.clock()
        out = []

        def gauge(name, help_text, samples, kind="gauge"):
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {kind}")
            for labels, value in samples:
                out.append(f"{name}{labels_text(labels)} {num(value)}")

        queue = self.queue()["jobs"]
        counts = {}
        oldest = {}
        for job in queue:
            key = (job["repo"], job["class"])
            counts[key] = counts.get(key, 0) + 1
            oldest[job["class"]] = max(oldest.get(job["class"], 0), job["wait_seconds"])
        gauge("runner_manager_jobs_queued", "Queued jobs the manager serves.",
              [({"repo": r, "tier": "normal", "class": f"gcp-{c}"}, n) for (r, c), n in sorted(counts.items())])
        gauge("runner_manager_job_oldest_wait_seconds", "Wait of the oldest queued job per class.",
              [({"tier": "normal", "class": f"gcp-{c}"}, w) for c, w in sorted(oldest.items())])
        with self.lock:
            wait_lines = self.wait.render("runner_manager_job_wait_seconds")
            duration_lines = self.duration.render("runner_manager_job_duration_seconds")
            gcp = dict(self.gcp)
            inventory_at, inventory_error, ro_rate = self.inventory_at, self.inventory_error, self.ro_rate
            last_loop, last_ok = self.last_loop, self.last_ok
        out += ["# HELP runner_manager_job_wait_seconds Queued to in progress.",
                "# TYPE runner_manager_job_wait_seconds histogram"] + wait_lines
        out += ["# HELP runner_manager_job_duration_seconds In progress to completed.",
                "# TYPE runner_manager_job_duration_seconds histogram"] + duration_lines

        rows = self.runner_rows()
        states = {}
        for row in rows:
            states[(row["class"], row["state"])] = states.get((row["class"], row["state"]), 0) + 1
        for cls in {row["class"] for row in rows}:
            for state in ("idle", "busy", "offline"):
                states.setdefault((cls, state), 0)
        gauge("runner_manager_runners", "Organization runners by class and state.",
              [({"class": c, "state": s}, n) for (c, s), n in sorted(states.items())])
        gauge("runner_manager_runner_offline", "1 if the runner is offline.",
              [({"name": r["name"], "class": r["class"]}, int(r["state"] == "offline")) for r in rows])
        gauge("runner_manager_runner_info", "One series per organization runner.",
              [({"name": r["name"], "class": r["class"], "state": r["state"]}, 1) for r in rows])
        gauge("runner_manager_inventory_age_seconds", "Seconds since the runner inventory last succeeded.",
              [({}, round(now - inventory_at) if inventory_at else -1)])
        gauge("runner_manager_inventory_up", "1 if the last runner listing succeeded.",
              [({}, int(self.inventory_fresh() and not inventory_error))])

        vms = gcp.get("vms", [])
        vm_counts = {}
        for vm in vms:
            vm_counts[(vm["class"], vm["status"])] = vm_counts.get((vm["class"], vm["status"]), 0) + 1
        gauge("runner_manager_vms", "Throwaway GCP VMs by class and status.",
              [({"class": c, "status": s}, n) for (c, s), n in sorted(vm_counts.items())])
        gauge("runner_manager_vm_oldest_age_seconds", "Age of the oldest throwaway VM.",
              [({}, round(max((vm["age"] for vm in vms), default=0)))])
        gauge("runner_manager_vm_max_age_seconds", "MAX_VM_AGE_MIN in seconds.", [({}, p.MAX_VM_AGE_S)])
        gauge("runner_manager_vcpu_used", "vCPUs held by throwaway VMs.", [({}, gcp.get("vcpu_used", 0))])
        gauge("runner_manager_vcpu_quota", "VCPU_QUOTA.", [({}, p.VCPU_QUOTA)])
        gauge("runner_manager_month_vm_hours", "VM running hours this month.", [({}, round(gcp.get("month_hours", 0), 3))])
        gauge("runner_manager_month_vm_hours_cap", "MAX_HOURS_PER_MONTH.", [({}, p.MAX_HOURS)])
        gauge("runner_manager_budget_locked", "1 while the budget lock holds.", [({}, int(bool(gcp.get("budget_locked"))))])

        rate = [({"credential": "poller"}, p._rate["remaining"])] if p._rate.get("remaining") is not None else []
        if ro_rate is not None:
            rate.append(({"credential": "readonly"}, ro_rate))
        gauge("runner_manager_github_rate_remaining", "X-RateLimit-Remaining last seen per credential.", rate)

        table = p._intake.get("table")
        if table is not None:
            # Process-lifetime counts: the restored totals lag up to one loop behind, and a
            # counter that steps back on restart reads as a huge increase().
            stats = {k: v - table.baseline.get(k, 0) for k, v in table.snapshot().items()}
            gauge("runner_manager_webhook_last_seconds", "Seconds since the last signed delivery.",
                  [({}, round(now - table.last_delivery))])
            gauge("runner_manager_webhook_silent", "1 while the poller has fallen back to 60 s polling.",
                  [({}, int(bool(p._intake.get("silent"))))])
            for name in ("missed_webhooks_total", "jobs_seen_total", "deliveries_total",
                         "duplicate_deliveries_total", "rejected_deliveries_total"):
                gauge(f"runner_manager_{name}", name.replace("_", " ") + ".", [({}, stats.get(name, 0))], "counter")
        gauge("runner_manager_loop_age_seconds", "Seconds since the poll loop last finished.",
              [({}, round(now - last_loop) if last_loop else -1)])
        gauge("runner_manager_poll_ok_age_seconds", "Seconds since a poll last finished without error.",
              [({}, round(now - last_ok) if last_ok else -1)])
        gauge("runner_manager_build_info", "Installed commit.", [({"sha": BUILD_SHA}, 1)])
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------- runner inventory
_ro = {"token": None, "exp": 0}


def ro_token(poller, now=None):
    """Installation token for the read-only App, reused for 55 minutes."""
    now = int(time.time()) if now is None else now
    if _ro["token"] and now < _ro["exp"]:
        return _ro["token"]
    jwt = poller.gh_app_jwt(poller.cred("RO_GH_APP_KEY", "ro_gh_app_key"), RO_GH_APP_ID, now)
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{RO_GH_APP_INSTALLATION_ID}/access_tokens", data=b"",
        method="POST", headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json",
                                "User-Agent": "gha-spot-poller"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        _ro.update(token=json.load(resp)["token"], exp=now + 55 * 60)
    return _ro["token"]


def list_runners(poller, token, opener=urllib.request.urlopen):
    """Every org runner (paged), and the X-RateLimit-Remaining of the last page."""
    runners, page, remaining = [], 1, None
    while True:
        req = urllib.request.Request(
            f"https://api.github.com/orgs/{poller.ORG}/actions/runners?per_page=100&page={page}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "User-Agent": "gha-spot-poller"})
        with opener(req, timeout=30) as resp:
            if resp.headers.get("X-RateLimit-Remaining") is not None:
                remaining = int(resp.headers["X-RateLimit-Remaining"])
            body = json.load(resp)
        batch = body.get("runners", [])
        runners += batch
        if len(batch) < 100 or page >= 20:
            return runners, remaining
        page += 1


def inventory_loop(monitor, poller, stop):
    while not stop.is_set():
        try:
            runners, remaining = list_runners(poller, ro_token(poller))
            monitor.set_runners(runners, remaining)
        except Exception as error:  # noqa: BLE001 - the thread must outlive any one failure
            monitor.inventory_failed(error)
            poller.log(f"inventory: runner listing failed: {error}")
        stop.wait(INVENTORY_INTERVAL)


# ---------------------------------------------------------------- HTTP
class MonitorHandler(http_server.BaseHTTPRequestHandler):
    monitor = None
    timeout = 10
    server_version = "gha-spot-poller"
    sys_version = ""

    def _reply(self, code, body, ctype):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        m = self.monitor
        if path == "/metrics":
            return self._reply(200, m.metrics(), "text/plain; version=0.0.4")
        if path == "/healthz":
            ok = m.healthy()
            return self._reply(200 if ok else 503, "ok\n" if ok else "stale\n", "text/plain")
        if path == "/api/v1/runners":
            with m.lock:
                meta = {"inventory": "on" if inventory_configured() else "off", "error": m.inventory_error}
            return self._reply(200, json.dumps(dict(meta, runners=m.runner_rows())), "application/json")
        if path == "/api/v1/queue":
            return self._reply(200, json.dumps(m.queue()), "application/json")
        return self._reply(404, "not found\n", "text/plain")

    def log_message(self, fmt, *args):
        pass


def inventory_configured():
    return bool(RO_GH_APP_ID and RO_GH_APP_INSTALLATION_ID)


def start(monitor, poller, bind=None, port=None, inventory=None):
    """Serve the endpoints, and list runners if the read-only App is configured."""
    handler = type("Handler", (MonitorHandler,), {"monitor": monitor})
    server = http_server.ThreadingHTTPServer((bind or MONITOR_BIND, MONITOR_PORT if port is None else port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="monitor", daemon=True).start()
    poller.log(f"monitor: listening on {server.server_address[0]}:{server.server_address[1]}")
    stop = threading.Event()
    if inventory if inventory is not None else inventory_configured():
        threading.Thread(target=inventory_loop, args=(monitor, poller, stop), name="inventory", daemon=True).start()
    return server, stop
