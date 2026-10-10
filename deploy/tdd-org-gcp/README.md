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
  library and `openssl`. Every 30 seconds it looks for queued jobs whose labels
  the runner serves. When it finds one, it writes a fresh one-hour registration
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
