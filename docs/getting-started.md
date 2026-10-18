# Getting started

This page takes you from a fresh checkout to your first read-only fleet inspection. Run the read-only commands
first. Nothing on this page creates, changes, or deletes a VM, disk, or GitHub resource until you add `--apply`
to a manager command or run the autoscaler without `--dry-run`. One exception: a tiered `--dry-run` connects
with `gcloud compute ssh`, which can add your SSH key to project or instance metadata, or to your OS Login
profile, the first time it connects.

## Prerequisites

On the operator machine, install:

- Python 3.11 or later. The control-plane scripts use only the Python standard library, so there is nothing to
  install with `pip`.
- Git, the GCP command-line tool (`gcloud`), and the GitHub command-line tool (`gh`).
- SSH and file transfer tools if you plan to run remote builds.
- Vale if you plan to change the documentation. See [Development](development.md).

Your accounts need these permissions:

- **GCP**: access to Compute Engine, Billing Catalog prices, regional quotas, disks, instance metadata, and
  the selected network. Enable the required APIs in your project.
- **Memory telemetry** (`resource` and `tiered` modes): the runner VMs' service account needs
  `roles/monitoring.metricWriter`, and the VMs need the `monitoring.write` or `cloud-platform` access scope, so
  the Ops Agent can publish `agent.googleapis.com/memory/percent_used`. The controller's identity needs
  monitoring read access. Without complete metrics, tiered mode holds, and resource mode waits.
- **GitHub**: organization runner-management access to deploy runners. Job recovery also needs Actions read and
  write access and Checks read access on each tracked repository.

For an organization target, `--apply` requires a selected-repository runner group that has access to
`ORG/oxidex`, where `ORG` is the target organization. Repository targets skip this check. These scripts are built around OxiDex: changing the example repository names doesn't
change that verification contract.

Dedicated builder rendering needs no GitHub runner token. It needs an approved public SSH identity mapping for
the uploader account. See [Set up a builder](dedicated-builders.md).

### Verify the runner group

Controller-created hosts for an organization target use a fixed runner group ID that the source sets as the
`--runner-group-id` default in `src/lib/runner_manager.py`, and the tier evidence counts only jobs in that same
group. Repository targets have no runner groups. Before you enable automatic organization launches, check that
the runner group your controller uses exists in your organization and permits access to `ORG/oxidex`. Replace
`<group-id>` with that group's ID:

```bash
gh api 'orgs/swack-tools/actions/runner-groups/<group-id>'
```

The manual manager accepts `--runner-group-id` for another qualifying group, but that option doesn't change
controller launches or tier evidence. Supporting a different automatic group needs a reviewed source change.

## Clone and sign in

```bash
git clone https://github.com/swack-tools/spot-github-runners.git
cd spot-github-runners

gcloud auth login
gcloud config set project YOUR_PROJECT

gh auth login
gh auth status
```

## Create the environment file

The manager, autoscaler, and control VM commands read `.env` from the root of the checkout. Variables that you
already export in your shell take precedence, because the loader never overrides the process environment.

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set a renewable `GH_TOKEN` with the permissions from the previous section. Git ignores the file.
Don't use a short-lived runner registration token as the renewable credential: the manager obtains a
registration token during deployment and removes its temporary startup metadata after verification.

Everything else in the file is optional. The most common settings are:

| Setting | Purpose |
| --- | --- |
| `GOOGLE_CLOUD_API_KEY` | Billing Catalog API key for [price discovery](spot-pricing.md) in `spot_finder.py`, including its 95 percent discount alerts, and for the dashboard's live pricing. Neither of those reads `.env`, so export the key in your shell. |
| `GCP_BILLING_EXPORT_TABLE` | Project spend from a billing export, as `PROJECT.DATASET.TABLE`. Needs `gcloud` credentials that can query the table through the BigQuery API. |

The dashboard and control VM settings are covered in [Dashboard](dashboard.md) and
[Edge hosting and the control VM](edge-dashboard.md). The [configuration reference](configuration.md) lists every
variable.

If you have several `gh` accounts, the autoscaler's `--github-user ACCOUNT` option selects an existing account.
It reads that account's token with `gh auth token` and doesn't enroll a new identity. Price queries use the
active `gcloud` identity.

Keep real secrets out of Git. Don't commit `.env`, SSH private keys, uploader JSON files, rendered launch
directories, or operational state. Store deployment output under a directory outside the checkout, such as
`$HOME/oxidex-ops`.

## Run the read-only commands

Run these commands from the repository root. Each one reads state and prints a result. None of them
creates, changes, or deletes a VM, disk, or runner registration. The autoscaler preview can enroll your SSH
key, and some commands write local files, as the notes after each command say.

Check that every entry point starts, and read its options:

```bash
python3 src/spot_finder.py --help
python3 src/runner_fleet.py --help
python3 src/spot_runner_manager.py --help
python3 src/spot_runner_autoscaler.py --help
python3 src/render_builder_startup.py --help
python3 src/remote_rust_build.py --help
python3 src/web_dashboard.py --help
```

List Spot prices that meet a minimum CPU and RAM within a daily cap:

```bash
python3 tools/spot_prices.py --min-cpu 4 --min-ram 8 --price-per-day 5 --list cheapest --results 20
```

Find candidate offers for a runner host:

```bash
python3 src/spot_finder.py --project YOUR_PROJECT --min-vcpu 16 --min-ram 32 --top 10
```

A quota error writes a cooldown marker file in your temporary directory, and later runs wait out the
cooldown.

Inspect the runner fleet:

```bash
python3 src/runner_fleet.py \
  --org swack-tools \
  --project YOUR_PROJECT \
  --label spot \
  --minimum-runners 1 \
  --runners-per-vm 8
```

`--label` filters the runner counts, and `--vm-name-prefix` restricts VM names. These counts don't establish
workload readiness, free memory, or an exclusive build reservation.

Preview one controller check:

```bash
python3 src/spot_runner_autoscaler.py \
  --project YOUR_PROJECT \
  --org swack-tools \
  --github-user YOUR_GITHUB_ACCOUNT \
  --scaling-mode tiered \
  --tier-job-memory-gib 32 \
  --no-remote-builder \
  --dry-run --once
```

A `--dry-run` leaves VMs, disks, runner registrations, and controller state unchanged. It reads
host evidence with `gcloud compute ssh`, which can add your SSH key to project or instance metadata, or
to your OS Login profile, the first time it connects. On your machine it creates the state directory and
its lock file and appends to the controller log. It takes the same state lock as a running controller, so
while one runs, pass a separate `--state-file`. Don't add
`--install-launch-agent` to a preview: that option writes and starts the macOS LaunchAgent before the dry-run
check runs. See [Fleet and autoscaler](fleet-and-autoscaler.md#run-as-a-macos-launchagent).

Preview a runner deployment. Without `--apply`, the manager checks the shape, compatible storage, price, and
quota, and then prints a plan:

```bash
python3 src/spot_runner_manager.py \
  --project YOUR_PROJECT \
  --org swack-tools \
  --max-cost 0.125 \
  --max-total-daily-usd 3 \
  --instance-type n4-standard-16 \
  --region asia-east2 --zone asia-east2-b \
  --cpu 16 --ram 64 \
  --tier large --tier-slots 20 \
  --tier-job-memory-gib 32 \
  --runner-group-id '<group-id>' \
  --runner-label spot \
  --runner-prefix oxidex \
  --instance-name oxidex-runners-example
```

The price limits are acceptance limits, not quoted prices. A price must be strictly below the applicable cap:
an equal price doesn't qualify.

## Next steps

- Read [Fleet and autoscaler](fleet-and-autoscaler.md) before you run the controller continuously.
- Read [Operations and troubleshooting](operations.md) for the safety rules that apply to a live fleet.
- To build Rust on a dedicated VM, follow [Set up a builder](dedicated-builders.md).
- To host the dashboard, read the [edge hosting guide](edge-dashboard.md).
