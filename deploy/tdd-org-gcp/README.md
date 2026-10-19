# GCP Spot runner for TheDancingDeveloper-org (prepared, not applied)

One scale-to-zero Spot VM that serves GitHub Actions jobs labelled `gcp-spot`
for `vogt`, `codex`, `rustnzb` and `msp-klaudia`. This is not the swack-tools
fleet controller: that controller hard-codes `ORG = "swack-tools"`, runner group
10 and an `ORG/oxidex` membership check, and its runners have no Docker. See
Vogt WI-1155 for the full evaluation.

## How it works

- `main.tf`: a dedicated VPC with no inbound rules and one Spot VM
  (`c3d-highcpu-16`, 16 vCPU, 32 GiB, `australia-southeast1-c`). The VM has a
  100 GB pd-balanced boot disk that survives stops, `instance_termination_action
  = STOP`, and no service account. Its startup script installs Docker and the
  actions runner on the host (the runner user is in the `docker` group). It
  registers one persistent org runner and sets `RUSTC_WRAPPER=sccache` with a
  40 GiB local cache. After `idle_minutes` with no job, the VM powers itself off.
- `poller/gha_spot_poller.py` runs on Node B and needs only the Python standard
  library and `openssl`. Every 60 seconds (`POLL_INTERVAL`) it lists queued runs
  and looks for queued jobs whose labels the runner serves; runs already in
  progress are rescanned for queued later jobs every 5 minutes
  (`RECONCILE_INTERVAL`). On a GitHub rate-limit response, or when fewer than
  `RATE_LIMIT_FLOOR` (200) calls remain, it makes no GitHub calls until
  `X-RateLimit-Reset`; GCP clean-up carries on. `LOG_LEVEL=debug` logs the
  remaining quota once per poll. Follow-up (WI-1838, not done): trigger on
  the org `workflow_job` webhook (action `queued`) and keep a 300 s reconcile
  poll. When it finds one, it writes a fresh one-hour registration
  token to the VM metadata and starts the VM. It also stops the VM when
  `MAX_HOURS_PER_MONTH` (default 250) is reached.
- `budget.tf`: an A$45 (about US$30) monthly budget with alerts at 50%, 90%
  and 100%. It publishes to a Pub/Sub topic. The poller pulls the
  subscription; at 100% of actual spend it stops the VM and labels it
  `gha-budget-locked=true`, and it won't start a locked VM. Remove the label by
  hand to unlock it. Billing data lags by hours, so the poller's hour cap is
  the first line of defence.

## Prerequisites (operator)

Target: project `email-rs-493900` (business organization), billing account
`01D74B-095DA7-FAE7D2` (AUD). The deployer is deliberately not Owner, because
the project also hosts the email-rs services.

1. Enabled APIs: compute, billingbudgets, iam, cloudresourcemanager,
   cloudbilling, plus **pubsub**.
2. Deployer `gha-spot-deployer@email-rs-493900.iam.gserviceaccount.com`, key in
   Infisical `infrastructure`/prod `GCP_GHA_SPOT_DEPLOYER_KEY`, with these
   roles:
   - `roles/compute.admin`, `roles/iam.serviceAccountUser`,
     `roles/serviceusage.serviceUsageConsumer` on the project;
   - `roles/billing.costsManager` on the billing account;
   - **`roles/pubsub.admin`** on the project. Terraform creates the budget
     topic and subscription, and sets IAM on them: linking a budget needs
     `pubsub.topics.setIamPolicy`, and granting the poller subscriber needs
     `pubsub.subscriptions.setIamPolicy`.
3. Poller service account `gha-spot-poller@email-rs-493900.iam.gserviceaccount.com`
   with **no project roles**. Terraform grants it access to the VM and the
   subscription only. Store its key in Infisical `infrastructure`/prod
   `GCP_GHA_SPOT_POLLER_KEY`.
4. A GitHub credential for `TheDancingDeveloper-org` with org "Self-hosted
   runners: read and write" and "Actions: read" on the repos.
5. Optional: a Tailscale auth key tagged `tag:ci-gcp`, with an ACL limited to
   `100.92.54.45:6380`.

## Apply (only after approval)

```bash
# Runner group restricted to the four repos (public repos must be allowed):
gh api -X POST orgs/TheDancingDeveloper-org/actions/runner-groups \
  -f name=gcp-spot -f visibility=selected -F allows_public_repositories=true \
  -F 'selected_repository_ids[]=<vogt id>' ...   # ids via gh api repos/ORG/REPO --jq .id

cp terraform.tfvars.example terraform.tfvars   # fill project/billing IDs
terraform init && terraform plan -out plan.bin  # review: 8 to add (VPC, VM, budget, Pub/Sub, IAM)
terraform apply plan.bin

# Poller key, kept out of Terraform state; store it in Infisical, not on disk in Git:
gcloud iam service-accounts keys create /dev/stdout \
  --iam-account gha-spot-poller@PROJECT.iam.gserviceaccount.com | secret set ...
```

Before relying on the runner, verify the price with the repo's own tool:
`python3 tools/spot_prices.py --project PROJECT --min-cpu 16 --min-ram 30
--price-per-day 3 --results 20`. Spot prices can change up to once every 30
days.

Then opt jobs in, starting with one workflow (`codex` fork-ci):

```yaml
runs-on: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository && 'ubuntu-24.04' || fromJSON('["self-hosted","gcp-spot"]') }}
```

Fork PRs stay on GitHub-hosted runners. All four repos are public, so also
keep "Require approval for all outside collaborators" on for fork workflows.

## Costs (Sydney, live catalog 2026-10-10: c3d-highcpu-30 $0.1265/h, c3d-highcpu-16 $0.0677/h)

| Item | Rate | Month |
| --- | --- | --- |
| c3d-highcpu-16 Spot | $0.0677/h running | 80 h = $5.40 |
| Ephemeral IPv4 | $0.005/h running | 80 h = $0.40 |
| 100 GB pd-balanced | $0.135/GB-month, charged while stopped | $13.50 |
| Internet egress (artifacts, sccache writes) | $0.19/GB | 5 GB = $0.95 |
| Pub/Sub budget topic and subscription | free tier | ~$0 |
| **Total at 80 VM-hours** | | **~$20** |

At $30, the budget buys about 210 VM-hours a month, or about 290 with a 60 GB
disk.

## Disk size and drift (2026-10-10)

The boot disk was resized in place from 100 GB to 160 GB with
`gcloud compute disks resize gha-spot-1 --size=160GB`, because changing
`boot_disk.initialize_params.size` in Terraform forces a VM replacement.
`main.tf` ignores that attribute, so Terraform state still records the
creation-time size (100) while the live disk is 160 GB. Ubuntu's cloud-init
grew the root filesystem automatically on the next boot (154 GB usable). The
budget was raised from A$45 to A$60 through Terraform to match.

## Throwaway VM classes (2026-10-18)

The poller (`MODE=ephemeral`) creates one Spot VM per queued job from one of two
instance templates (`ephemeral_classes` in `ephemeral.tf`):

| Class | Shape | Disk | Labels | Max | Serves |
| --- | --- | --- | --- | --- | --- |
| large | c3d-highcpu-30 (30 vCPU, 59 GB) | 160 GB | `rust-heavy`, `bigmem` | 1 | codex fork-ci and fork-release |
| small | c3d-highcpu-8 (8 vCPU, 16 GB) | 100 GB | `gcp-rust` | 4 | routed heavy Rust jobs (rustnzb, rustTorrent; vogt once the image has its packages) |

The project's vCPU quota is 32 (`CPUS_ALL_REGIONS` and the C3D family in
australia-southeast1), so one large VM or four small ones run at a time, never
both. The poller counts the vCPUs of every VM that is not stopped. A large job is
served first: while it waits for vCPUs, no new small VM starts, so the running
ones drain. It never asks GCP for a VM that would exceed `VCPU_QUOTA`, and a
refused create leaves the job queued for the next poll.

Only these VMs carry `gcp-rust`, so jobs that request it can't be taken by an
idle Node B runner. Bare `[self-hosted]` jobs never wake a VM. A live VM can still
pick one up, because GitHub gives a job to any idle runner whose labels match.

The systemd unit is `poller/gha-spot-poller.service`. Its template names come from
`terraform output runner_templates`.

### Webhook intake (built, not enabled)

Stage 1 of [the runner manager](../../docs/runner-manager.md) lets the poller
take the gha-spot-poller App's `workflow_job` webhook instead of listing every
repository each minute. With `WEBHOOK_PORT` set:

- The poller serves `POST /webhook/github` on `WEBHOOK_BIND:WEBHOOK_PORT`.
  It checks `X-Hub-Signature-256` against the `gh_webhook_secret` credential,
  refuses unsigned or wrongly signed deliveries with 401, and drops repeated
  `X-GitHub-Delivery` ids. A `queued` delivery for a served job wakes the loop
  at once; `in_progress` and `completed` take the job off the queue.
- GitHub is listed every `RECONCILE_INTERVAL` (300 s), queued runs only, and
  every 15 minutes (`IN_PROGRESS_SCAN_MIN`) in-progress runs as well, which
  alone show a started run's later queued jobs. Only that fuller listing
  removes jobs from the queue. A queued job the listing finds that no
  delivery announced, older than 120 s, is logged as `missed_webhook` and
  counted.
- The fallback starts only after a listing has seen queued jobs, so in a
  webhook outage that begins while nothing is queued, a new job can wait up
  to one reconcile interval (300 s) before the 60 s polling resumes.
- If no delivery arrives for 15 minutes while jobs are queued, the poller
  lists every `POLL_INTERVAL` (60 s), with the in-progress scan, until
  deliveries resume, and logs the change both ways.
- The hourly heartbeat logs GitHub calls since the last heartbeat, jobs seen,
  missed webhooks and refused deliveries. Counters persist in the state file.

To enable it (operator steps, none done yet):

1. Generate a secret, store it in Infisical, and seal it on the control host
   as `/etc/credstore.encrypted/gha-spot-poller.gh_webhook_secret`.
2. Add `poller/webhook.caddy` to the edge proxy with the public hostname and
   the poller's address as the proxy sees it. The unit binds `127.0.0.1`,
   which only a proxy on the same host's network can reach; a proxy in a
   container or on another host needs `WEBHOOK_BIND` set to an address it can
   reach, and that address should not be open to other clients, since only
   the signature then guards it.
3. In the App settings, set the webhook URL to
   `https://<webhook-host>/webhook/github` with that secret, and subscribe to
   **Workflow job** events.
4. Uncomment the two webhook lines in `poller/gha-spot-poller.service`, then
   reload and restart the unit. The App's recent deliveries page should show
   the ping answered with 200.

Exit gate: after a week live, missed webhooks stay under 1% of jobs seen, and
GitHub calls an hour are at least 80% below the 60 s polling baseline.

### Quota increase (prepared, not submitted)

To run a large and small VMs together, raise both limits to 64 vCPU (needs
`gcloud components install alpha`; quota IDs checked against the Cloud Quotas API):

```bash
gcloud alpha quotas preferences create --project=email-rs-493900 \
  --service=compute.googleapis.com --quota-id=CPUS-PER-VM-FAMILY-per-project-region \
  --dimensions=region=australia-southeast1,vm_family=C3D --preferred-value=64 \
  --justification="Spot CI runners: one c3d-highcpu-30 and four c3d-highcpu-8 concurrently" \
  --email=sprooty@sprooty.com --preference-id=gha-c3d-64
gcloud alpha quotas preferences create --project=email-rs-493900 \
  --service=compute.googleapis.com --quota-id=CPUS-ALL-REGIONS-per-project \
  --preferred-value=64 --justification="Spot CI runners (see C3D request)" \
  --email=sprooty@sprooty.com --preference-id=gha-all-regions-64
```

Then set `VCPU_QUOTA=64` in the unit.
