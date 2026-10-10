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
- `budget.tf`: a $30 monthly budget with alerts at 50%, 90%, 100% and forecast
  100%. Its Pub/Sub topic triggers `budget-guard`, which stops every
  `gha-spot=true` VM when actual spend reaches 100% and labels it
  `gha-budget-locked=true`. The poller will not start a locked VM; remove the
  label by hand to unlock it. Billing data lags by hours, so the poller's hour
  cap is the first line of defence.

## Prerequisites (operator)

1. A GCP project with billing linked. Record the project ID and the billing
   account ID.
2. A deployer identity for `terraform apply`. Compute Admin alone is not
   enough. It needs:
   - `roles/compute.admin` (VM, VPC, firewall)
   - `roles/iam.serviceAccountAdmin`, `roles/iam.roleAdmin`,
     `roles/resourcemanager.projectIamAdmin` (poller and guard service
     accounts, custom role, bindings)
   - `roles/iam.serviceAccountUser` (attach the guard service account to the
     function)
   - `roles/serviceusage.serviceUsageAdmin` (enable APIs)
   - `roles/cloudfunctions.admin`, `roles/run.admin`, `roles/pubsub.admin`,
     `roles/storage.admin` (budget guard)
   - On the **billing account**: `roles/billing.costsManager` (create the
     budget)

   Project Owner plus Billing Account Costs Manager covers all of these.
3. A GitHub credential for `TheDancingDeveloper-org` with org "Self-hosted
   runners: read and write" and "Actions: read" on the four repos. A
   fine-grained PAT or a GitHub App both work. The current `gh` token
   (thedancingdeveloper) gets 403 on the org runner APIs.
4. Optional: a Tailscale auth key, tagged `tag:ci-gcp` and pre-authorised.
   Also an ACL that allows `tag:ci-gcp` to reach only
   `100.92.54.45:6380`.

## Apply (only after approval)

```bash
# Runner group restricted to the four repos (public repos must be allowed):
gh api -X POST orgs/TheDancingDeveloper-org/actions/runner-groups \
  -f name=gcp-spot -f visibility=selected -F allows_public_repositories=true \
  -F 'selected_repository_ids[]=<vogt id>' ...   # ids via gh api repos/ORG/REPO --jq .id

cp terraform.tfvars.example terraform.tfvars   # fill project/billing IDs
terraform init && terraform plan -out plan.bin  # review: 1 VM, budget, function
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

## Costs (Sydney, prices from 2026-10-04)

| Item | Rate | Month |
| --- | --- | --- |
| c3d-highcpu-16 Spot | $0.0677/h running | 80 h = $5.40 |
| Ephemeral IPv4 | $0.005/h running | 80 h = $0.40 |
| 100 GB pd-balanced | $0.135/GB-month, charged while stopped | $13.50 |
| Internet egress (artifacts, sccache writes) | $0.19/GB | 5 GB = $0.95 |
| Budget guard function, Pub/Sub, bucket | free tier | ~$0.10 |
| **Total at 80 VM-hours** | | **~$20** |

At $30, the budget buys about 210 VM-hours a month, or about 290 with a 60 GB
disk.
