# Runner manager

Status: design only. Nothing here is built or deployed.

This design grows the ephemeral Spot poller (`deploy/tdd-org-gcp/poller/`)
into a **runner manager**. The manager is one service that sits between GitHub,
the work tracker and every self-hosted runner in the organization. It has four
goals:

1. **Per-repository priority.** Jobs from high-priority repositories start
   before lower-priority work.
2. **Placement.** Each job goes to the runner class whose labels and
   capabilities fit it. The classes are the static Docker host's
   big-memory, Docker and publish runners, the GCP large and small Spot VMs,
   and a Windows Subsystem for Linux (WSL) runner on a workstation.
3. **Monitoring.** Runner health, utilisation, queue depth, and offline
   runners go to Prometheus, Grafana, the work tracker's Runners view, and
   alerts.
4. **One home for the GCP scripts.** The bake, hook and autoscaler scripts
   listed in [Scripts in scope](#scripts-in-scope) move under the service, or
   stage 6 records why they stay where they are.

Placeholders used below: `<org>` is the GitHub organization, `<control-host>`
is the always-on Linux host that runs the poller today, `<webhook-host>` is
the public hostname for GitHub webhooks, and `<poller-app>` and
`<readonly-app>` are the two GitHub Apps.

## Today

| Piece | Where | What it does |
| --- | --- | --- |
| Poller (`poller/gha_spot_poller.py`) | `<control-host>`, systemd unit `poller/gha-spot-poller.service`, `DynamicUser`, TPM-sealed credentials | Polls queued runs every 60 s and reconciles in-progress runs every 300 s. Creates one throwaway Spot VM per queued job from the large or small template within the vCPU quota. Deletes finished or overdue VMs. Enforces the monthly hour cap and the budget lock |
| `<poller-app>` | GitHub | The poller's credential: runner registration and Actions read |
| `<readonly-app>` | GitHub | Read-only credential for monitoring |
| Terraform (`main.tf`, `ephemeral.tf`, `budget.tf`) | this directory | VM templates, the budget topic, and the legacy persistent VM |
| Static runners | `<control-host>` (Docker) | `rust`, two `publish`, `bigmem` |
| WSL runner | Windows workstation | Public Rust jobs, available only when the workstation is on |

### Scripts in scope

| Script | Kind | Role |
| --- | --- | --- |
| `deploy/tdd-org-gcp/image/bake.sh` | bake | Installs packages and the runner into the golden image, then powers off |
| `deploy/tdd-org-gcp/image/bake-image.sh` | bake | Creates the bake VM and turns its disk into an image |
| `deploy/tdd-org-gcp/image/runtime.sh.tpl` | hook | Throwaway VM startup: reads the JIT config from metadata, sets the idle and lifetime power-off timers, runs one job, powers off |
| `deploy/tdd-org-gcp/startup.sh.tpl` | hook | Persistent VM startup. Writes the `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` hook `gha-job-completed.sh` (disk clean-up after each job) and the `gha-idle-check` power-off timer |
| `runner/job-started.sh`, `runner/job-completed.sh` | hook | Admission hooks for the upstream Docker runner image; listed so stage 6 decides whether the manager reuses them |
| `deploy/tdd-org-gcp/poller/gha_spot_poller.py` | autoscaler | The poller itself |
| `src/spot_runner_autoscaler.py`, `src/spot_runner_manager.py`, `src/runner_fleet.py` | autoscaler | The upstream fleet controller for another organization |

Known limits: the poller sees only the repositories in `REPOS`. It knows
nothing about the static or WSL runners. Its priority is fixed (large before
small). It shares GitHub's core rate limit with other estate tools.

## Design decisions

### 1. Events: `workflow_job` webhook first, polling as reconcile

**Decision:** drive the manager from the organization's `workflow_job`
webhook (actions `queued`, `in_progress`, `completed`), delivered to
`<poller-app>`. Keep a slow reconcile poll (300 s, queued runs only) to catch
missed deliveries.

- **Why:** webhooks cost no API calls and arrive in about a second. The 60 s
  poll across N repositories is the poller's largest share of the rate limit,
  and each new repository adds to it. The `in_progress` event also reports
  `runner_name` and `runner_labels`, which gives placement feedback and
  monitoring data without extra calls.
- **Delivery path:** GitHub, then `<webhook-host>` on the reverse proxy, then
  the private network, then the manager. The proxy exposes only
  `POST /webhook/github`. The manager verifies `X-Hub-Signature-256` with the
  App's webhook secret, drops anything unsigned, and deduplicates on
  `X-GitHub-Delivery`.
- **Reconcile:** every 300 s, list `status=queued` runs for the repositories
  the manager owns and compare them with its job table. A job queued for more
  than 120 s that the manager has not acted on is logged as `missed_webhook`
  and handled. The existing rate-limit floor and the wait for
  `X-RateLimit-Reset` stay.
- **Fallback:** if no webhook arrives for 15 minutes while GitHub shows queued
  jobs, the manager drops to a 60 s poll (today's behaviour) and raises
  `RunnerManagerWebhookSilent`.
- **Budget:** the App has its own installation rate limit, separate from
  shared personal tokens. Target: fewer than 300 calls an hour in steady state.

Rejected: polling only, because its cost grows with repositories and it adds
about 60 s of latency. Also rejected: Actions Runner Controller, which needs
Kubernetes.

### 2. Priority policy, with the work tracker as the source of truth

**Model:** each repository has a tier: `p0` (release or hotfix), `high`,
`normal`, or `low`. The default is `normal`.

- **Source of truth:** the work tracker project's priority field, read through
  its API. A project maps to repositories through its forge link.
- **Effective tier per job:** the repository tier, adjusted by the event:
  - `merge_group` runs and tag or `release` runs rise one tier, with a
    floor of `high`;
  - `schedule` runs drop to `low`;
  - a `prio-low` job label drops one tier. No label can raise a tier.
- **Cache:** the manager refreshes the repository-to-tier map every
  10 minutes and writes it to its state file. If the tracker does not answer,
  the manager keeps the last map with no expiry and raises
  `RunnerTierPolicyStale` (section 5). If no map exists, it uses
  `policy.default.toml`, committed in this repository and editable by hand.
  **CI never waits on the work tracker.**
- **Use of tier:** the effective tier sets the order in which waiting jobs
  get capacity, which runner group and labels the job's VM gets (section 3),
  and whether a job may pre-empt. Tier never stops a job from running.
- **Fairness:** within a tier, oldest first. A `low` job that waits more than
  30 minutes is promoted to `normal`, so nothing starves.

### 3. Skipping GitHub's queue

GitHub has no job priority. It gives a queued job to any idle runner in an
allowed runner group whose labels include all the labels the job asks for. So
skipping the queue means **controlling which capacity exists and which jobs
can match it**.

- **Separate priority labels.** Priority capacity carries its own class
  labels, such as `gcp-rust-prio` instead of `gcp-rust`, and never the normal
  ones. A job asking for `gcp-rust` can't match a `gcp-rust-prio` runner,
  because a runner must have every label the job asks for. A superset label
  such as `gcp-rust` plus `prio` would not work, because a plain `gcp-rust`
  job would match it.
- **Workflows select the label by effective tier.** `runs-on` accepts
  expressions, so a normal-tier repository's merge-queue job can ask for
  priority capacity without becoming high tier:

  ```yaml
  runs-on: ${{ fromJSON(contains(fromJSON('["merge_group","release"]'), github.event_name)
    && '["self-hosted","gcp-rust-prio"]' || '["self-hosted","gcp-rust"]') }}
  ```

  High-tier repositories ask for the `-prio` labels on every event.
- **Two runner groups.** `ci-priority` holds runners with `-prio` labels and
  `ci-general` holds the rest. Both allow every repository the manager
  serves, so merge-queue and release jobs from normal-tier repositories reach
  `ci-priority` runners. Labels, not group membership, do the separation.
- **The manager checks the request.** When a job asks for a `-prio` label,
  the manager computes the job's effective tier. If it is `high` or above, it
  creates the VM with the `-prio` labels and registers it in `ci-priority`
  with a JIT config. If it is lower (a workflow that asks for priority it
  isn't entitled to), the manager creates nothing, leaves the job queued,
  logs it, and counts it in `runner_manager_priority_refused_total`. The
  repository's policy check (`github-policy`) flags the same workflow at
  review time.
- **Quota ordering.** When the vCPU quota is full, the next VM goes to the
  waiting job with the highest effective tier (today: large always first). A
  large job waiting for quota holds back new small VMs only for jobs of the
  same tier or lower.
- **Pre-warmed capacity (off by default).** Keep `PREWARM_SMALL=1` stopped,
  baked `gcp-rust-prio` VM during working hours, then start it when a
  priority job queues. Cost is in section 8.
- **Pre-emption (opt-in, off by default).** If a `p0` job has waited more
  than 10 minutes for quota, the manager may cancel the youngest running
  `low` job on a GCP VM and re-run it afterwards. It never pre-empts `normal`
  or higher, merge-queue runs, or a job past 80% of its usual duration. Every
  pre-emption is logged and counted.
- **Not used:** cancelling queued jobs to reorder them. That loses the run
  and confuses required checks.

### 4. Placement

Placement means making capacity exist that matches the labels a job's
`runs-on` asks for. The manager never rewrites `runs-on`. It keeps an
inventory and a routing table that the policy check also reads.

| Class | Labels | Capabilities | Capacity | Who may use it |
| --- | --- | --- | --- | --- |
| GitHub-hosted | `ubuntu-24.04` | 4 vCPU, 16 GB, ephemeral | unlimited for public repositories | default for public repositories and fork pull requests |
| GCP large | `rust-heavy`, `bigmem` (`-prio` variants for priority) | 30 vCPU, 59 GB, Docker, sccache, ephemeral | 1 (32 vCPU quota) | the heaviest Rust builds |
| GCP small | `gcp-rust` (`gcp-rust-prio`) | 8 vCPU, 16 GB, Docker, ephemeral | 4 | routed heavy Rust jobs |
| Static big-memory | `bigmem`, renamed to `static-bigmem` in stage 3 | large RAM, private network, shares a production host | 1 | private repositories that need memory and the private network |
| Static Docker and publish | `publish`, `docker` | registry and deploy secrets, private network | 2 | publish and deploy jobs only |
| Static Rust | `rust` | persistent cache | 1 | private Rust |
| WSL | the WSL runner's label | Windows host, Rust, workstation hours | 1 | public Rust, best effort |

Rules:

1. A class label is exclusive. `gcp-rust` exists only on GCP small VMs, so
   a job that asks for it can't land on a static runner.
2. `bigmem` is ambiguous today, because GCP large VMs and the static host both
   carry it. Stage 3 renames the static one so each label names one class.
3. Secrets-bearing classes (`publish`) accept only `push`, `release`, and
   `workflow_dispatch` events from the default branch. The manager flags any
   other use, and the policy check fails it.
4. Bare `[self-hosted]` jobs never start a GCP VM.
5. Fork pull requests never get self-hosted runners.
6. When a static class is offline, the manager does not reroute. It marks the
   job `waiting_on_offline_class` and alerts after 10 minutes. Rerouting would
   mean editing workflows.
7. The policy check (`runner_policy.py`) reads the routing table
   (`placement.toml`) instead of its own label list, so placement rules are
   written once.

### 5. Monitoring

**Endpoints**, served only on the private network:

- `GET /metrics` (Prometheus):
  - `runner_manager_jobs_queued{repo,tier,class}`
  - `runner_manager_job_wait_seconds` histogram `{tier,class}` (queued to
    in progress)
  - `runner_manager_job_duration_seconds` histogram `{repo,class}`
  - `runner_manager_runners{class,state="idle|busy|offline"}`
  - `runner_manager_vms{class,status}`, `runner_manager_vcpu_used`,
    `runner_manager_vcpu_quota`
  - `runner_manager_month_vm_hours`, `runner_manager_budget_locked`
  - `runner_manager_github_rate_remaining{credential}`
  - `runner_manager_webhook_last_seconds`,
    `runner_manager_missed_webhooks_total`
  - `runner_manager_tier_policy_age_seconds`,
    `runner_manager_tier_policy_source{source="tracker|cache|default"}`
  - `runner_manager_priority_refused_total{repo}`,
    `runner_manager_preemptions_total{repo}`
  - `runner_manager_build_info{sha}`
- `GET /healthz`: liveness. Returns 200 if the loop ran in the last three
  intervals.
- `GET /api/v1/runners` and `GET /api/v1/queue`: JSON for the work tracker's
  Runners view. Runners show class, labels, state, current job, and last
  seen. Queued jobs show effective tier, wait, and planned class.

**Data sources:** webhook events and the reconcile poll for jobs. For
runners, `GET /orgs/<org>/actions/runners` every 120 s through
`<readonly-app>`, which keeps monitoring reads off the poller App's budget.
Host metrics for the static and WSL hosts come from the existing Netdata
scrape.

**Prometheus and Grafana:** add a `runner-manager` scrape target to the
estate Prometheus configuration. Commit a "CI runners" dashboard under
`deploy/runner-manager/grafana/`.

**Alerts**, through the existing Alertmanager route:

| Alert | Condition |
| --- | --- |
| `RunnerOffline` | a static runner offline for more than 15 minutes |
| `RunnerQueueStuck` | any job queued for more than 20 minutes, or more than 5 minutes for `high` and `p0` |
| `RunnerManagerDown` | `/healthz` failing, or the scrape absent for more than 5 minutes |
| `RunnerManagerWebhookSilent` | no webhook for 15 minutes while jobs are queued |
| `RunnerTierPolicyStale` | `tier_policy_age_seconds` over 1 hour, or the source is `default` |
| `RunnerPriorityRefused` | `priority_refused_total` increased in the last hour |
| `GcpRunnerBudgetLocked` | `budget_locked == 1` |
| `GcpRunnerHoursHigh` | month hours over 80% of the cap |
| `GcpOrphanVm` | a VM alive longer than `MAX_VM_AGE_MIN` |
| `GithubRateLow` | fewer than 500 calls left on any credential |

**Runners view:** the work tracker pulls `/api/v1/runners` and
`/api/v1/queue`, so the manager never depends on it. Building the view is
work tracker work; this design fixes only the API.

### 6. Where it runs

**Decision:** keep a **systemd unit on `<control-host>`** (today's unit), not a
container stack.

- It needs no container runtime and keeps `LoadCredentialEncrypted`
  (TPM-sealed secrets). A container stack would move secrets into the stack's
  environment.
- It must keep running while the host's containers are redeployed. CI deploys
  go through the container stack controller, so a manager running inside it
  would depend on itself.
- Cost: no GitOps drift detection. To make up for it, add a `just
  deploy-manager` recipe that installs a pinned commit, plus the
  `runner_manager_build_info{sha}` metric, which the tracker's drift check
  can compare with `main`.
- Revisit this if `<control-host>` is retired. The service is standard-library
  Python, so moving it means copying it and a unit file.

The webhook route is a new reverse-proxy virtual host, limited to the path
`/webhook/github`.

### 7. Failure behaviour

CI must never depend on the work tracker, and a failure should make CI slower,
not stop it:

| Failure | Behaviour |
| --- | --- |
| Work tracker down | Last cached tiers, then `policy.default.toml`. `RunnerTierPolicyStale` after 1 hour |
| Webhooks lost or proxy down | Reconcile poll at 300 s. After 15 silent minutes, 60 s poll and an alert |
| Manager down | Static, WSL and GitHub-hosted runners keep working. GCP-only jobs wait. Live VMs still power themselves off (lifetime limit, idle timer) |
| GitHub rate limit | No GitHub calls until reset (existing). Webhooks are still accepted and queued in memory |
| GCP API error or quota refusal | The job stays queued and is retried next cycle with backoff. Never exceeds `VCPU_QUOTA` |
| Spot pre-emption | The VM terminates and the job fails as runner lost. The manager re-runs it once if it is less than 1 hour old, and counts it |
| State file lost | Rebuilt from GitHub (runners, queued runs) and GCP (VM list) on start. Month hours are re-derived from VM uptimes, or set to 50% of the cap if they can't be |
| Budget at 100% | Existing lock: delete VMs, refuse creates until unlocked by hand |

### 8. Cost controls

Kept: the monthly hour cap (`MAX_HOURS_PER_MONTH=250`), the GCP budget with
the Pub/Sub lock, `VCPU_QUOTA`, `MAX_VM_AGE_MIN`, and deleting powered-off
VMs.

Added:

- **Per-tier hour shares:** `low` jobs may use at most 30% of the monthly
  cap. Once that share is used, new `low` GCP jobs wait until the next month
  or until the operator raises the share. Move repositories that hit it often
  to GitHub-hosted runners by policy.
- **Pre-warm cost**, from the costs table in `deploy/tdd-org-gcp/README.md`
  (live Sydney catalog, 2026-10-10). A stopped pre-warm VM pays only for its
  disk: 100 GB of pd-balanced at US$0.135 per GB-month is **US$13.50 a
  month**, whether or not it runs. Running time is billed at the small class's
  Spot rate. The README gives c3d-highcpu-16 at US$0.0677 an hour, so
  c3d-highcpu-8 is about US$0.034 an hour. That figure is derived by halving,
  so re-check it with `tools/spot_prices.py` before enabling. Pre-warm is
  switched off automatically when month hours pass 70% of the cap.
- **Per-repository spend metric:** VM-seconds by repository, so the work
  tracker can show cost per project.
- **Quota increases stay operator decisions** (README, "Quota increase").

### 9. Related work

- **Shared GitHub rate limit:** stage 1 removes the poller's polling load
  from the shared limit. The `github_rate_remaining` metric measures the
  effect for each credential.
- **Merge queue and path-filtered checks:** merge-queue runs get at least
  `high` effective tier and can reach `ci-priority` runners (section 3), so a
  queued merge doesn't wait behind pull request builds. Path filters cut the
  number of jobs, which lowers queue depth before priority matters.
  Merge-queue runs are never pre-empted, and they are re-run after a Spot
  loss, or the merge queue would eject the pull request.

### 10. Code layout after the move

```
runner_manager/            # was deploy/tdd-org-gcp/poller/
  manager.py               # loop, webhook server, reconcile
  gcp.py  github.py  tracker.py  policy.py  metrics.py
  policy.default.toml  placement.toml
  image/                   # bake.sh, bake-image.sh, runtime.sh.tpl
deploy/runner-manager/
  runner-manager.service   # replaces gha-spot-poller.service
  grafana/  alerts/
deploy/tdd-org-gcp/        # Terraform stays here
```

Standard-library Python only, as today, with `unittest` tests under `tests/`.

## Plan

Each stage can ship alone and leaves CI working. Its exit gate must be true
before the next dependent stage starts. Deploying any stage is an operator
action.

| # | Stage | Exit gate |
| --- | --- | --- |
| 1 | **Webhook intake.** `workflow_job` handler with signature check and deduplication. The poll becomes a 300 s reconcile with the 15-minute fallback. Proxy route and App webhook settings | After a week live, `missed_webhooks_total` is under 1% of jobs, and the poller's GitHub calls an hour are at least 80% below the 60 s baseline |
| 2 | **Monitoring.** `/metrics`, `/healthz`, `/api/v1/runners`, `/api/v1/queue`. Runner inventory through `<readonly-app>`, covering static, WSL and GCP runners. Prometheus target, dashboard, alerts | The dashboard shows every organization runner, and each alert has fired once in a drill |
| 3 | **Placement table.** `placement.toml`, rename the static `bigmem` label, the policy check reads the table, offline-class detection | Every workflow passes the policy check against the table, and no label maps to two classes |
| 4 | **Priority from the tracker.** Tier map with cache and default file, effective tier per job, tier-ordered quota, starvation promotion, `RunnerTierPolicyStale` | With the tracker stopped, scheduling continues from the cache and the stale alert fires after 1 hour. In a test, a high job queued behind three normal jobs gets the next VM |
| 5 | **Queue skipping.** `-prio` labels, `ci-priority` and `ci-general` groups, effective-tier check before creating a priority VM, optional pre-warm, opt-in pre-emption, re-run after Spot loss | In a week with contention, the median wait for `high` jobs is under 2 minutes. A merge-queue job from a normal-tier repository ran on a `ci-priority` runner. A `-prio` request from an ineligible job was refused and alerted. No merge-queue run was pre-empted |
| 6 | **Consolidation.** Move the scripts, rename the unit, add the per-repository cost metric and tier hour shares | See the stage 6 gate below |

**Stage 6 exit gate.** All of these must hold:

1. This document's [Scripts in scope](#scripts-in-scope) table has a
   **Decision** column. Each script there is marked as moved (with its new
   path), kept in place (with a reason, for example "upstream fleet
   controller, not ours to move"), or retired (with the commit that removes
   it).
2. Every moved script has a unit test or a parity test under `tests/`, and
   the full suite passes with
   `PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py'`.
3. A golden image baked from the new `runner_manager/image/` path has served
   a throwaway VM job end to end.
4. `runner-manager.service` has run for 7 days with no missed jobs, and only
   then is `gha-spot-poller.service` disabled and removed.
5. The README and this document point only to the new paths.

Stages 1 and 2 can run in parallel. Stage 3 needs stage 2 (the inventory).
Stage 4 needs stage 1. Stage 5 needs stages 3 and 4. Stage 6 can start after
stage 1 but finishes last.

## Open questions for the operator

- Is the tracker's project priority field the right tier signal, or should
  the tracker get a dedicated CI tier field?
- Is pre-emption wanted at all? The design ships it off.
- Should `<webhook-host>` be public (GitHub webhooks need that) or sit behind
  a relay? Default: public, limited to one path, and signature-checked.
