# CI runner recommendations: TheDancingDeveloper-org

Date: 2026-10-10. Vogt: WI-1155. Status: proposal; nothing has been changed or applied.

## Summary

Run public-repo CI on free GitHub-hosted runners. Add one on-demand GCP Spot VM
for heavy Rust builds. Shrink the self-hosted fleet from 9 runners to 4 and keep
those 4 for private, tailnet and publishing jobs.

| Workload | Runs on | Cost |
|---|---|---|
| Public repos (default) | GitHub-hosted `ubuntu-24.04` (4 vCPU, 16 GB) | Free, no minute limit |
| Heavy Rust (codex, rustnzb, vogt build/ci) | GCP Spot VM, 30–32 cores, started on demand | About $20–30/month, hard stop at $30 |
| Private repos, tailnet access, Docker publish | Node B (3 runners) and msp-platform (1 runner) | Existing hardware |

## Current state (last 30 days, approximate)

- **Self-hosted fleet: 9 runners.**
  - Node B: rust, publish ×2, bigmem.
  - node-f: 3 runners, on a Hyper-V VM on the desktop workstation.
  - Proxmox LXC 109: 1 runner. Its Komodo stack state is "unknown".
  - msp-platform: 1 runner.
- **Job-hours:**
  - Self-hosted: about 390 h (189 h public repos, 202 h private).
  - GitHub-hosted: about 86 h, mostly codex and msp-klaudia.
- **Utilisation:**
  - Node B: 24% average CPU, p95 64%. It also runs production.
  - node-f: 17% average, p95 44%.
- **Hosted billing:** private repos used about 180 of the Team plan's 3,000
  included minutes. Public-repo minutes are free.
- **Slowest jobs:** codex fork-ci takes 100–130 min cold on hosted runners and
  about 15 min warm. rustnzb CI has a median of 24 min and a maximum of 68 min.
- **Policy:** `github-policy` makes every job pick a `self-hosted` label. The
  only hosted exceptions are dated entries in `runner-exceptions.json`
  (cadastre, FarmEggs).

## Recommendations

### 1. Public repos default to GitHub-hosted runners

- **Why:** hosted runners are free for public repos with no minute limit, and
  they're ephemeral. Fork PRs then stop running on persistent root runners on
  the tailnet. The cadastre exception already names that risk.
- **Change:** update `github-policy/scripts/runner_policy.py` so public repos may
  select `ubuntu-*`. Keep self-hosted for jobs that need tailnet access or
  publishing secrets.
- **Keep:** org setting "Require approval for all outside collaborators" for
  fork-PR workflows.

### 2. Heavy Rust builds go to one GCP Spot VM

- **Shape:** c3d-highcpu-30 in australia-southeast1-c (30 vCPU, 59 GB), Spot
  at $0.1265/h on 2026-10-04.
  - Fallback: c2d-highcpu-32 in australia-southeast1-a.
  - Cheapest overall: c2d-highcpu-32 in us-west3, $0.1298/h, but much farther
    from the tailnet sccache.
- **Runners on the VM:** one heavy runner plus two small ones.
  - Heavy: `rust-heavy`, 22 cores, about 42 GB.
  - Small: 2 × `rust-small`, 4 cores and about 7 GB each.
  - Use CPU weights of 6 : 1 : 1 rather than hard caps, and set
    `CARGO_BUILD_JOBS` to 22 / 4 / 4.
- **Run on demand:**
  - A poller on Node B starts the VM when a `rust-heavy` job is queued.
  - The VM powers itself off after 15 idle minutes.
  - Let `rust-small` jobs wake the VM only once a few weeks of numbers show
    headroom.
- **Spend controls:**
  - A $30 billing budget triggers a function that stops and locks the VM.
  - The poller caps running time per month.
  - Billing data lags by hours, so the hour cap acts first.
- **Cost:** a 100 GB disk is $13.50/month, charged even while the VM is
  stopped. Running cost is about $0.13/h. $30 buys about 118 VM-hours; expected
  use is 25–40 h for heavy jobs only.
- **Prepared:** Terraform in this directory on local branch
  `tdd-gcp-spot-deploy`, not pushed and not applied. It still defines a single
  16-core runner; the 30-core, three-runner layout is not built yet.

### 3. Self-hosted fleet goes from 9 runners to 4

| Runner | Action | Reason |
|---|---|---|
| Node B publish ×2 (Docker sidecars) | Keep | Docker image publishing, tailnet |
| Node B rust (Docker sidecar) | Keep | Private repos, container tests |
| msp-platform | Keep | msp-agent and msp-customer jobs need it |
| Node B bigmem | Keep for now, then review | CodeQL for vogt; it may move to hosted runners once vogt is public-hosted |
| node-f ×3 | Retire | Frees the desktop workstation's VM; work moves to hosted runners or GCP |
| Proxmox LXC 109 | Retire | Stack state "unknown"; little load |

Private repos (msp-agent, FarmEggs, myaiagent, msp-customer-*) stay
self-hosted. Hosted runners for private repos are an option within the
3,000-minute allowance, but that allowance is smaller than private CI's current
usage of about 200 h a month.

### 4. Fixes found along the way

- contextkeeper CI requests `self-hosted, publish`, and its jobs never get a
  runner.
- codex `blocking-ci` requests `codex-windows-x64`, `codex-linux-x64` and
  `macos-15-xlarge`, which are the upstream repo's own runner labels. Those
  jobs never get a runner either; disable them in the fork.

## Rollout order

1. Change `github-policy` to allow hosted runners for public repos, then move
   msp-klaudia and the small public repos.
2. Move vogt and rustnzb quality jobs to hosted runners, and watch durations for
   a week.
3. Approve GCP spend and provide credentials (below), apply the Terraform, and
   route codex fork-ci to `rust-heavy`. Then add rustnzb and vogt build.
4. Retire the node-f and Proxmox runners once their queues stay empty.
5. Review bigmem and decide whether to let `rust-small` wake the VM.

## Needed from the operator

- A decision on this split.
- Approval for the `github-policy` change.
- For GCP:
  - Project ID and billing account ID.
  - A deployer with Project Owner plus Billing Account Costs Manager. Compute
    Admin alone can't create the budget, IAM objects or the stop function.
  - A GitHub token or App with org self-hosted runner admin and Actions read.
  - Optional: a Tailscale key tagged `tag:ci-gcp` for the estate sccache.

## Risks

- **Spot preemption:** Google can reclaim the VM at any time with 30 seconds'
  notice, and the running job must be re-run. All three runners on the VM go
  down together.
- **Budget cap:** jobs on GCP labels don't fall back to hosted runners when the
  cap is hit. They queue until GitHub fails them after 24 hours.
- **Cold start:** about 1–2 minutes from a job queueing to the VM picking it up.
- **Single zone:** c3d capacity in Sydney is in one zone only.
- **Hosted runner speed:** 4 vCPU hosted runners are slower for big Rust builds,
  which is why heavy builds go to GCP.
- **Prices:** Spot prices can change up to once every 30 days. Re-check with
  `tools/spot_prices.py` once a GCP project exists.

## Provider comparison (about 30–32 vCPU, 2026-10-10)

Prices are compute only, in USD per hour. Disk, IP and egress come on top.

- **GCP:** gcloud-compute.com catalog snapshot dated 2026-10-04.
- **AWS:** public Spot feed `spot.js`, which tends to lag the live market.
- **Azure:** Retail Prices API.
- **Oracle (OCI):** public price list. OCI charges the same price in every
  region, and its preemptible VMs are 50% off.
- **Hetzner:** list prices quoted from memory, not fetched. Treat them as
  approximate.

| Provider | Shape | Region | $/h | Hours $30 buys | Notes |
|---|---|---|---|---|---|
| **GCP Spot** | c3d-highcpu-30 (30 vCPU, 59 GB) | Sydney | **0.127** | **~118** (after the $13.50 disk) | Cheapest per hour. Preemptible. |
| Azure Spot | F32s_v2 (32 vCPU, 64 GB) | australiaeast | 0.328 | ~90 | Older Intel shape. Arm D32pls_v6 is 0.229. |
| OCI preemptible | E5 Flex, 16 OCPU = 32 vCPU, 64 GB | Sydney | ~0.30 | ~100 | Arm A1 with 32 OCPU is about 0.21. |
| AWS Spot | c6a.8xlarge (32 vCPU, 64 GB) | ap-southeast-2 | 0.725 | ~40 | us-west-2 is 0.58 (c7g Arm: 0.35). |
| Hetzner Cloud | CCX53 (32 dedicated vCPU) | EU, US, Singapore | ~0.40 | ~75 | Billed while stopped; must be deleted and recreated. No Australia region. |
| Hetzner dedicated | Auction Ryzen 16 cores / 32 threads, 64–128 GB | EU | flat ~€40–70/month | Unlimited | Always on, no preemption. EU latency is irrelevant to CI with a local cache. |
| OCI Always Free | A1 Arm, 4 OCPU / 24 GB | Any | 0 | Unlimited | Arm only; free capacity is hard to obtain. |

**Takeaways:**
- **On demand:** GCP Spot in Sydney is the cheapest hourly option by a factor of
  2–5. Keep it as the recommendation.
- **Always on:** if you'd rather pay a flat fee with no preemption, cold start
  or budget cap, a Hetzner auction server (16 cores / 32 threads, about
  €40–70/month) gives unlimited heavy-Rust hours. It costs roughly twice the
  budget, and the estate already has a Hetzner account.
- **Free extra:** OCI's Always Free Arm instance could host a free small runner
  for Arm-compatible jobs, if capacity can be obtained.
