# Runner manager

Status: design, 2026-10-18. Vogt: WI-1889. Nothing here is built or deployed.

This design grows `gha-spot-poller` (`deploy/tdd-org-gcp/poller/`, branch
`poller-rate-limit`) into a **runner manager**: one service that sits between
GitHub, Vogt and every self-hosted runner in `TheDancingDeveloper-org`. It
covers four outcomes:

1. **Per-repo priority.** Jobs from high-priority repos start before
   low-priority work.
2. **Placement.** A job goes to the runner class whose labels and capabilities
   fit it: Node B `bigmem`/`docker`/`publish`, GCP `rust-heavy` (large) and
   `gcp-rust` (small) Spot VMs, and the Windows/WSL runner.
3. **Monitoring.** Runner health, utilisation, queue depth, and dead or offline
   runners go to Prometheus/Grafana, a Vogt Runners view, and alerts.
4. **One home for the GCP scripts.** The bake, hook and autoscaler scripts move
   under the service.

## Today

| Piece | Where | What it does |
| --- | --- | --- |
| `gha_spot_poller.py` | Node B, systemd `gha-spot-poller.service`, `DynamicUser`, TPM-sealed credentials | Polls queued runs every 60 s (reconciles in-progress runs every 300 s), creates one throwaway Spot VM per queued job from the `large` or `small` template within a 32-vCPU quota, deletes finished or overdue VMs, enforces the monthly hour cap and budget lock |
| GitHub App `gha-spot-poller` | GitHub | The poller's credential: runner registration tokens and Actions read |
| GitHub App `estate-readonly` | GitHub | Read-only credential for monitoring |
| `image/bake.sh`, `bake-image.sh`, `runtime.sh.tpl` | this repo | Golden image bake and VM runtime startup |
| `ephemeral.tf`, `budget.tf`, `main.tf` | this repo | Templates, budget topic, legacy persistent VM |
| `src/spot_runner_autoscaler.py`, `spot_runner_manager.py`, `runner_fleet.py` | this repo | The older swack-tools fleet controller (hard-coded org) |
| Node B runners | Node B, Komodo/docker | `rust`, `publish` ×2, `bigmem` |
| `wsl2-gha-public-rust` | Windows box `desktop-tlk1gff`, WSL2 | Public Rust jobs |

Known limits: the poller only sees the four repos in `REPOS`; it knows nothing
about Node B or WSL runners; priority is fixed (large before small); and it
shares GitHub's core rate limit with the Vogt forge sweep and `land-pr`
(WI-1838).

## Design decisions

### 1. Events: `workflow_job` webhook first, polling as reconcile

**Decision:** drive the manager from the org-level `workflow_job` webhook
(actions `queued`, `in_progress`, `completed`), delivered to the GitHub App
`gha-spot-poller`. Keep a slow reconcile poll (300 s, queued runs only) to catch
missed deliveries.

- **Why:** webhooks cost no API calls and arrive in about a second. The 60 s
  poll across N repos is the poller's largest share of the WI-1838 budget, and
  each new repo adds to it. A webhook also reports `runner_name` and
  `runner_labels` on `in_progress`, which gives placement feedback and
  monitoring for free.
- **Delivery path:** GitHub → `runner-manager.sprooty.com` on the Caddy edge →
  the tailnet → the manager. Caddy exposes only `POST /webhook/github`. The
  manager verifies `X-Hub-Signature-256` with the App's webhook secret and drops
  anything unsigned. It deduplicates on `X-GitHub-Delivery`.
- **Reconcile:** every 300 s, list `status=queued` runs for the repos the
  manager owns, and compare them with its job table. A job queued for over
  120 s that the manager has not acted on is logged as `missed_webhook` and
  handled. The existing rate-limit floor and `X-RateLimit-Reset` wait stay.
- **Fallback:** if no webhook arrives for 15 minutes while GitHub shows queued
  jobs, the manager drops to a 60 s poll (today's behaviour) and alerts
  `RunnerManagerWebhookSilent`.
- **Budget:** the App has its own 5,000/h (or more) installation limit, apart
  from the PAT shared in WI-1838. Target: under 300 calls/h in steady state.

Rejected: polling only (cost grows with repos; ~60 s latency), and the
Actions Runner Controller (Kubernetes, which the estate does not run).

### 2. Priority policy, with Vogt as the source of truth

**Model:** each repo has a tier: `p0` (release/hotfix), `high`, `normal`,
`low`. The default is `normal`.

- **Source of truth:** the Vogt project's priority field (per project, read
  through the `vogt` MCP/HTTP API using `project_list`/`project_get`). A
  project maps to repos through its forge link. Per-job overrides come from
  the workflow, not from Vogt:
  - a job label `prio-high` or `prio-low` (the manager reads it from
    `labels` and removes nothing);
  - events: `merge_group` (WI-1886) and tags/`release` events rise one tier;
    `schedule` drops to `low`.
- **Cache:** the manager refreshes the repo→tier map every 10 minutes and
  writes it to its state file. If Vogt does not answer, it keeps the last map
  (no expiry). If there is no map at all, it uses `policy.default.toml`,
  committed in this repo, which the operator can edit by hand. **CI never waits
  on Vogt.**
- **Use of tier:** tier sets the order in which waiting jobs get capacity, the
  pre-warm pool (section 3), and whether a job may pre-empt (section 3). It
  never blocks a job from running.
- **Fairness:** within a tier, oldest first. A `low` job waiting more than
  30 minutes is promoted to `normal`, so nothing starves.

### 3. Skipping GitHub's queue

GitHub has no job priority. A queued job goes to any idle runner whose labels
match, in no order the manager controls. So "skip the queue" must mean
**controlling which capacity exists, and for whom**:

- **Runner groups per tier.** Make two org runner groups: `ci-priority`
  (restricted to high/p0 repos) and `ci-general` (all repos). Runners in
  `ci-priority` are invisible to normal repos, so normal jobs can never take
  them. The manager moves repos between groups' `selected_repository_ids` when
  Vogt tiers change; moving is one API call and takes effect on the next job.
- **Ephemeral VMs per job.** Throwaway GCP VMs register with a one-time JIT
  config (`generate-jitconfig`). A workflow can't ask for a per-job label, so
  the manager uses the group instead: a VM created for a high-tier job
  registers into `ci-priority`, so a normal job can't take it.
- **Pre-warmed capacity.** Keep `PREWARM_SMALL=1` stopped-but-baked `gcp-rust`
  VM in `ci-priority` during working hours (08:00–22:00 AEST), suspended
  (`SUSPEND`, not delete) when idle. Resume takes ~20 s instead of ~90 s for a
  fresh create. Off by default; cost in section 8.
- **Quota ordering.** When the 32-vCPU quota is full, the next VM goes to the
  highest-tier waiting job (today: large always first). A large job waiting
  for quota still holds back new small VMs, but only for jobs of the same or
  lower tier.
- **Pre-emption (opt-in, off by default).** If a `p0` job has waited more than
  10 minutes for quota, the manager may cancel the youngest running `low` job
  on a GCP VM (`POST .../runs/{id}/cancel`) and re-run it afterwards
  (`rerun-failed-jobs`). Never pre-empt `normal` or higher, merge-queue runs,
  or a job past 80% of its usual duration. Every pre-emption is logged and
  counted.
- **Not used:** cancelling queued jobs to reorder them (it loses the run and
  confuses required checks).

### 4. Placement

Placement means picking the label set a job's `runs-on` asks for and making
matching capacity exist. The manager does not rewrite `runs-on`; it maintains
an inventory and a routing table that `github-policy` checks.

| Class | Labels | Capabilities | Capacity | Who may use it |
| --- | --- | --- | --- | --- |
| GitHub-hosted | `ubuntu-24.04` | 4 vCPU, 16 GB, ephemeral | unlimited for public repos | default for public repos and fork PRs |
| GCP large | `rust-heavy`, `bigmem` | 30 vCPU, 59 GB, Docker, sccache, ephemeral | 1 (quota 32) | codex fork-ci/fork-release, any `rust-heavy` |
| GCP small | `gcp-rust` | 8 vCPU, 16 GB, Docker, ephemeral | 4 | rustnzb, rustTorrent, vogt heavy Rust |
| Node B bigmem | `bigmem`, `nodeb` | big RAM, tailnet, shares production host | 1 | private repos needing memory and tailnet |
| Node B docker/publish | `publish`, `docker` | registry and deploy secrets, tailnet | 2 | publishing and deploy jobs only |
| Node B rust | `rust` | persistent cache | 1 | private Rust |
| WSL2 | `wsl2-gha-public-rust` | Windows host, Rust, workstation hours | 1 | public Rust; best-effort |

Rules:

1. A class label is exclusive. `gcp-rust` exists only on GCP small VMs, so a
   job asking for it can't land on Node B (today's behaviour, kept).
2. `bigmem` is ambiguous today (GCP large and Node B both carry it). Rename
   the Node B one to `nodeb-bigmem` in stage 3, so each label names one class.
3. Secrets-bearing classes (`publish`) accept only `push`/`release`/
   `workflow_dispatch` events from the default branch. The manager flags (and
   `github-policy` fails) any other use.
4. Bare `[self-hosted]` jobs never wake a GCP VM (kept).
5. Fork PRs never get self-hosted runners (kept).
6. When a static class is offline (WSL box asleep, Node B runner dead), the
   manager does not reroute: it marks the job `waiting_on_offline_class` and
   alerts after 10 minutes. Rerouting would mean editing workflows.
7. `github-policy`'s `runner_policy.py` reads the routing table
   (`placement.toml` in this repo) instead of its own label list, so placement
   rules are written once.

### 5. Monitoring

**Endpoints** (on the manager, tailnet only):

- `GET /metrics` (Prometheus):
  - `runner_manager_jobs_queued{repo,tier,class}`
  - `runner_manager_job_wait_seconds` histogram `{tier,class}` (queued →
    in_progress)
  - `runner_manager_job_duration_seconds` histogram `{repo,class}`
  - `runner_manager_runners{class,state="idle|busy|offline"}`
  - `runner_manager_vms{class,status}`, `runner_manager_vcpu_used`,
    `runner_manager_vcpu_quota`
  - `runner_manager_month_vm_hours`, `runner_manager_budget_locked`
  - `runner_manager_github_rate_remaining{credential}`
  - `runner_manager_webhook_last_seconds`, `runner_manager_missed_webhooks_total`
  - `runner_manager_vogt_policy_age_seconds`
  - `runner_manager_preemptions_total{repo}`
- `GET /healthz`: liveness, 200 if the loop ran in the last 3 intervals.
- `GET /api/v1/runners`, `/api/v1/queue`: JSON for the Vogt Runners view
  (runner, class, labels, state, current job, last seen; queued jobs with tier,
  wait and planned class).

**Data sources:** webhook events and the reconcile poll for jobs; for runners,
`GET /orgs/{org}/actions/runners` every 120 s using `estate-readonly`
(it separates monitoring reads from the poller App's write budget); Node B and
WSL host metrics come from the existing Netdata `netdata-hosts` scrape.

**Prometheus:** add a `runner-manager` scrape target in the ops repo's Node B
Prometheus config. Grafana dashboard "CI runners" committed under
`deploy/runner-manager/grafana/`.

**Alerts** (Alertmanager → Telegram, existing route):

| Alert | Condition |
| --- | --- |
| `RunnerOffline` | a static runner offline > 15 min |
| `RunnerQueueStuck` | any job queued > 20 min (high/p0: > 5 min) |
| `RunnerManagerDown` | `/healthz` failing or scrape absent > 5 min |
| `RunnerManagerWebhookSilent` | no webhook in 15 min while jobs are queued |
| `GcpRunnerBudgetLocked` | `budget_locked == 1` |
| `GcpRunnerHoursHigh` | month hours > 80% of the cap |
| `GcpOrphanVm` | a VM alive > `MAX_VM_AGE_MIN` |
| `GithubRateLow` | remaining < 500 on any credential |

**Vogt Runners view:** Vogt reads `/api/v1/runners` and `/api/v1/queue`
(pull, so the manager never depends on Vogt). Building the view is Vogt work;
this design only fixes the API.

### 6. Where it runs

**Decision:** keep **Node B systemd** (today's unit), not a Komodo stack.

- It needs no container runtime and keeps `LoadCredentialEncrypted` (TPM-sealed
  secrets). A Komodo stack would move secrets into Komodo env.
- It must keep running when Komodo or Docker on Node B is being redeployed;
  CI deploys go through Komodo, so a manager inside Komodo would sit in its own
  dependency loop.
- Cost: no GitOps drift detection. Mitigation: a `just deploy-manager` recipe
  that installs a pinned commit, and a `runner_manager_build_info{sha}` metric
  that Vogt drift can compare with `main`.
- Revisit if Node B is retired; the service is stdlib Python, so moving it is
  a copy plus a unit file.

The webhook route is a new Caddy vhost (`caddy-route` skill), path-limited to
`/webhook/github`.

### 7. Failure behaviour

CI must never depend on Vogt, and should degrade to "slower", not "stopped":

| Failure | Behaviour |
| --- | --- |
| Vogt down | last cached tiers; then `policy.default.toml`. No alert below 1 h stale |
| Webhooks lost or edge down | reconcile poll at 300 s; after 15 min silent, 60 s poll + alert |
| Manager down | static runners (Node B, WSL) and GitHub-hosted keep working; GCP-only jobs wait; live VMs still self-terminate (6 h VM limit, idle power-off) |
| GitHub rate-limited | no GitHub calls until reset (existing); webhooks still accepted and queued in memory |
| GCP API errors or quota refusal | job stays queued, retried next cycle with backoff; never exceeds `VCPU_QUOTA` |
| Spot pre-emption | VM terminates; job fails as runner lost; the manager re-runs it once (`rerun-failed-jobs`) if under 1 h old, and counts it |
| State file lost | rebuilt from GitHub (runners, queued runs) and GCP (VM list) on start; month hours re-derived from VM uptimes in GCP or set conservatively to the cap's 50% |
| Budget at 100% | existing lock: delete VMs, refuse creates until unlocked by hand |

### 8. Cost controls

Kept: the monthly hour cap (`MAX_HOURS_PER_MONTH=250`), the GCP budget with the
Pub/Sub lock, `VCPU_QUOTA`, `MAX_VM_AGE_MIN`, deleting powered-off VMs.

Added:

- **Per-tier hour shares:** `low` jobs may use at most 30% of the monthly cap;
  once that share is used, new `low` GCP jobs wait until the next month or
  until the operator raises the share. Repos that hit it often should move
  to GitHub-hosted runners by policy.
- **Pre-warm costs** about A$0.10/h suspended disk + memory; capped by
  `PREWARM_HOURS` and dropped automatically when month hours pass 70%.
- **Per-repo spend metric:** VM-seconds by repo, so Vogt can show cost per
  project.
- **Quota requests stay operator decisions** (README "Quota increase").

### 9. Interactions

- **WI-1838 (GitHub core limit exhausted):** the webhook stage removes the
  poller's polling load from the shared token; the poller already uses its own
  App. The manager publishes `github_rate_remaining` per credential, so the
  WI-1838 fix can be measured. Stage 1 below is a partial fix for WI-1838.
- **WI-1886 (merge queue and path-filtered checks):** `merge_group` events rise
  one tier, so a queued merge isn't stuck behind PR builds. Path filtering cuts
  job count, which lowers queue depth before priority matters. The manager
  must treat `merge_group` runs as never pre-emptible and must re-run them on a
  Spot loss, or the merge queue ejects the PR.

### 10. Code layout after the move

```
runner_manager/            # was deploy/tdd-org-gcp/poller/
  manager.py               # loop, webhook server, reconcile
  gcp.py  github.py  vogt.py  policy.py  metrics.py
  policy.default.toml  placement.toml
deploy/runner-manager/
  runner-manager.service   # replaces gha-spot-poller.service
  grafana/  alerts/
deploy/tdd-org-gcp/        # Terraform stays here
  image/                   # bake scripts move to runner_manager/image/
src/spot_runner_*.py       # swack-tools controller: retire or keep, decided in stage 6
```

Stdlib Python only, as today; tests with `unittest` under `tests/`.

## Plan

Each stage is shippable alone and leaves CI working. "Exit gate" is what must
be true before the next stage starts. Deployment of each stage is an operator
action.

| # | Stage | Exit gate |
| --- | --- | --- |
| 1 | **Webhook intake.** Add `workflow_job` handler with signature check and dedupe; keep the poll as 300 s reconcile with the 15-min fallback; Caddy route; App webhook config | A week live with `missed_webhooks_total` < 1% of jobs and poller GitHub calls/h down at least 80% against the 60 s baseline |
| 2 | **Monitoring.** `/metrics`, `/healthz`, `/api/v1/runners`, `/api/v1/queue`; runner inventory from `estate-readonly` covering Node B, WSL and GCP; Prometheus target, dashboard, alerts | Dashboard shows every org runner; each alert fired once in a drill (stop a runner, queue a job with no capacity) |
| 3 | **Placement table.** `placement.toml`; rename Node B `bigmem` to `nodeb-bigmem`; `github-policy` reads the table; offline-class detection | All workflows pass `github-policy` against the table; no label maps to two classes |
| 4 | **Priority from Vogt.** Tier map from Vogt projects with cache and default file; tier-ordered quota allocation; merge-queue and release raises; starvation promotion | With Vogt stopped, the manager keeps scheduling on the cache; in a test, a high job queued behind three normal jobs gets the next VM |
| 5 | **Queue skipping.** `ci-priority` and `ci-general` runner groups; JIT config into the right group; optional pre-warm; opt-in pre-emption; Spot-loss re-run | A high job waits < 2 min median in a week with contention; pre-emption off unless the operator enabled it; zero merge-queue runs pre-empted |
| 6 | **Consolidation.** Move poller, bake and runtime scripts into `runner_manager/`; rename the unit; decide the fate of `src/spot_runner_*.py`; per-repo cost metric and tier hour shares | One unit on Node B; old unit removed; README points here; tests pass |

Stages 1 and 2 can run in parallel. 3 needs 2 (inventory). 4 needs 1. 5 needs 3
and 4. 6 can start after 1 but finishes last.

## Open questions for the operator

- Is the Vogt project priority field the right tier signal, or should Vogt get
  a dedicated "CI tier" field?
- Is pre-emption wanted at all? The design ships it off.
- Should `runner-manager.sprooty.com` be public (needed for GitHub webhooks) or
  go through a relay such as smee/Cloudflare? Default: public, path-limited,
  signature-checked.
