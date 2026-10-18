# Command reference

This page documents every command-line entry point in the repository: what it
does, whether it changes anything, and each flag with its type, default, and
meaning. The flag tables come from `--help` output and from the `argparse`
definitions in the source, because many defaults appear only in the source.

Run all commands from the repository root with Python 3.11 or later. The scripts
under `src/` and `tools/` use only the Python standard library.

## Read-only and mutating commands

Before you run a command that you have not used before, find it in this table.
The last column names the exact flag or action that makes it change something.

| Command | Default effect | What makes it change something |
| --- | --- | --- |
| [`tools/spot_prices.py`](#spot-prices) | Reads prices and inventory | `--csv` writes a local file |
| [`src/spot_finder.py`](#spot-finder) | Reads prices and inventory | Nothing in the cloud. A quota error writes a local cooldown marker, `gcp-spot-finder-quota-<project>.timestamp`, in the temporary directory, and later runs wait out that cooldown |
| [`src/runner_fleet.py`](#runner-fleet) | Reads runners and VMs | Nothing |
| [`src/spot_runner_manager.py`](#spot-runner-manager) | Prints a plan after read-only checks | `--apply`, which also deletes old unattached manager-labeled disks in the zone, or `--patch-live` (which needs no `--apply`) |
| [`src/spot_runner_autoscaler.py`](#spot-runner-autoscaler) | Manages the fleet | `--dry-run` leaves VMs, disks, registrations, and controller state unchanged, but its `gcloud compute ssh` can add your SSH key; `--install-launch-agent` installs the macOS agent |
| [`src/control_vm.py`](#control-vm) | `status` and `plan` read only | `ensure --apply`, `rotate --apply`; `render-startup` always writes its `--output` file |
| [`src/web_dashboard.py`](#web-dashboard) | Serves HTTP | `--state-file` persists state, including with `--dump-overview`; served POST routes can change state. With `SPOT_CONFIG_GH_TOKEN` or `GH_TOKEN` set, `POST /api/patches/save` opens a pull request on the config repository, and with `DASHBOARD_HOT_PATCH_EXECUTOR=gcloud`, `/api/patches/hot-patch` changes running hosts |
| [`src/render_builder_startup.py`](#render-builder-startup) | Writes a local directory | Running the rendered `launch-command.sh` creates a billable VM |
| [`src/lib/builder_runtime_update.py`](#builder-runtime-update) | Captures and checks only | `--apply`, `--finish`, `--recover-committed`, `--recover-finish` |
| [`src/remote_rust_build.py`](#remote-rust-build) | Uploads source and builds | Always changes the selected host |
| [`src/cargo_remote.py`](#cargo-remote) | Wraps `remote_rust_build.py` | Always changes the selected host |
| [`src/auto_remote_build.py`](#auto-remote-build) | Selects a host and builds | Always changes the selected host |
| [`src/install_remote_commands.py`](#install-remote-commands) | Writes two shims | Always writes local files |
| [Proof runners](#proof-runners) | Run the test suite in CI | Write proof files under `/target` |
| [`just` recipes](#just-recipes) | `just` lists recipes | `just test` starts remote work |

A live call to GCP or GitHub needs authenticated `gcloud`
and `gh` command-line tools. A read-only command still needs those credentials.

Several commands accept `--project` with a default that is a real GCP project
identifier (ID) of the repository owner. Always pass `--project` explicitly. The
tables below mark the default of these flags as the owner's project.

## `tools/spot_prices.py` {#spot-prices}

Lists current GCP Spot compute prices for machine types that meet a minimum CPU
count and RAM and cost no more than a daily cap. It reads the official GCP
Billing Catalog and the machine-type inventory of all zones, and prints CSV to
standard output with progress on standard error.

- **Effect.** Read-only. It creates no VM and reserves no capacity.
- **Prices.** Compute only. Disks, GPUs, networking, and taxes are excluded.
- **Omissions.** Shared-core shapes and shapes with missing catalog prices are left out and counted in a
  diagnostic message. Conflicting catalog prices get their own message. Accelerator and bare-metal shapes are
  skipped silently.
- **Credentials.** The active `gcloud` account supplies an access token. The
  project comes from `--project` or from `gcloud config`.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--min-cpu` | Positive integer | Required | Minimum vCPU count. |
| `--min-ram` | Positive number | Required | Minimum RAM in GiB. |
| `--price-per-day` | Positive number | Required | Maximum USD per 24 hours, compute only. |
| `--list` | `cheapest`, `expensive` | `cheapest` | Sort order of the result. |
| `--results` | `all` or a positive integer | `all` | Number of rows to print. |
| `--project` | String | Active `gcloud` project | Project for the machine-type inventory. |
| `--csv` | Path | None | Also write all displayed rows to this CSV file. |

The CSV columns are `region`, `machine_type`, `vCPU`, `RAM_GiB`, `USD_hour`,
`USD_day`, and `zones`.

```bash
python3 tools/spot_prices.py --min-cpu 4 --min-ram 8 \
  --price-per-day 5 --list cheapest --results 20
```

## `src/spot_finder.py` {#spot-finder}

Scans all active regions for the cheapest Spot offers that meet a minimum vCPU
count and RAM, then prints a table with hourly, daily, monthly, and per-runner
prices. It optionally prints an alert for offers that are at least 95 percent
below the on-demand price.

- **Effect.** Read-only in the cloud. Results are candidates, not reservations. A quota error writes the
  local cooldown marker `gcp-spot-finder-quota-<project>.timestamp` in the temporary directory.
- **Prices.** Compute only. Disk, network, operating system licensing, and
  capacity are excluded.
- **Exit codes.** `0` on success, `1` on an authentication or API error, `2`
  when no offer matches. If some regional price requests fail but offers
  remain, the scan is partial: the command prints a warning and the first
  five errors on stderr, still lists the remaining offers, and exits `0`. If every
  request fails, no offer remains and the command exits `2`.
- **Alerts.** The 95 percent alert needs either `--list-price` or a Billing
  Catalog API key from `--billing-api-key` or the `GOOGLE_CLOUD_API_KEY`
  environment variable. Without either, the command prints a notice that the
  alerts are off. The command does not read `.env`, so export the variable in
  your shell.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--provider` | `GCP` (case-insensitive) | `GCP` | Provider. Only GCP exists. |
| `--project` | String | Active `gcloud` project | GCP project ID. |
| `--min-vcpu` | Positive integer | `32` | Minimum vCPU count. |
| `--min-ram` | Positive number, optional `GB` or `GiB` suffix | `128` | Minimum RAM in GiB. |
| `--runners` | Positive integer | `1` for hosts below 4 vCPUs or with at most 8 GiB of RAM, otherwise vCPU `// 4` | Runner count per instance, used for per-runner prices. |
| `--top` | Positive integer | `10` | Number of cheapest results. |
| `--concurrency` | Positive integer | `600` | Concurrent API requests. |
| `--list-price` | Positive number | None | On-demand hourly price baseline in USD for the 95 percent alerts. The finder applies this one value to every offer, whatever its shape. |
| `--billing-api-key` | String | `GOOGLE_CLOUD_API_KEY` | Billing Catalog API key for automatic 95 percent alerts. |
| `-v` | Repeatable flag | Off | More diagnostic detail. Repeat as `-v`, `-vv`, or `-vvv`. |

```bash
python3 src/spot_finder.py --project YOUR_PROJECT \
  --min-vcpu 16 --min-ram 32 --top 10
```

## `src/runner_fleet.py` {#runner-fleet}

Prints the state of the GitHub self-hosted runners and the Compute Engine VMs,
and the scale-out plan that would restore an online-runner floor. It calls the
`gh` and `gcloud` command-line tools and creates or deletes nothing.

- **Effect.** Read-only.
- **Output.** Online, registered, and busy runner counts; running, listed, Spot,
  and stopped VM counts; and the scale-out plan.
- **Exit codes.** `0` on success, `2` on a `gh`, `gcloud`, or parsing error.
- **Scope.** `--label` filters only the runner counts. The VM list covers every
  VM in the project unless you pass `--vm-name-prefix`.
- **Evidence.** The counts do not show workload readiness, free memory, or an
  exclusive build reservation.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--org` | String | `swack-tools` | GitHub organization. |
| `--target` | `owner/repo` or organization | Value of `--org` | Target repository or organization that the runners registered with. |
| `--project` | String | Owner's project | GCP project that holds the VMs. |
| `--label` | String | None | Count only runners that have this label. |
| `--vm-name-prefix` | String | None | List only VMs whose name starts with this prefix. |
| `--minimum-runners` | Integer | `1` | Online-runner floor for the plan. Must be at least 1. |
| `--runners-per-vm` | Integer | `8` | Runners per VM for the plan. Must be at least 1. |
| `--gh-binary` | Path or name | `gh` | GitHub command-line tool to run. |
| `--gcloud-binary` | Path or name | `gcloud` | `gcloud` command-line tool to run. |

```bash
python3 src/runner_fleet.py --org swack-tools --project YOUR_PROJECT \
  --label spot --minimum-runners 1 --runners-per-vm 8
```

## `src/spot_runner_manager.py` {#spot-runner-manager}

Provisions one Spot VM that hosts persistent GitHub Actions runner containers,
or previews that deployment. The command verifies the machine shape, the live
Spot price, compatible storage, and quota before it changes anything. It loads
`.env` from the repository root for credentials such as `GH_TOKEN`.

- **Effect.** Without `--apply`, the command exits after read-only checks and
  prints a plan. With `--apply`, it creates a runner registration token,
  provisions the VM, passes a startup script through instance metadata,
  registers the runners, waits for them to come online, and then removes the
  whole `startup-script` metadata key, which holds the one-use token. It also
  connects with `gcloud compute ssh` to wait for bootstrap and check storage,
  which can add your SSH key to project or instance metadata, or to your OS
  Login profile. It rewrites the GitHub labels of the host's runners, records
  the applied patch in instance metadata and labels, writes the
  `--deployment-receipt` file when you pass one, and, with a billing export
  table, queries BigQuery for project costs.
- **Disk cleanup.** Every `--apply` run that passes the GitHub checks and
  obtains a registration token deletes orphaned runner data disks before it
  creates or adopts the VM. It deletes every disk in the selected zone and
  project that is labeled `managed-by=spot-runner-manager`, is `READY`, is
  attached to no VM, and is older than six hours. The deletion happens even
  when the deployment then fails, and a deleted disk can't be recovered.
  Controller launches run the same cleanup.
- **Live patch.** `--patch-live` changes a running VM and its runner containers
  over SSH, rewrites the runners' GitHub labels, and records the patch in
  instance metadata, even when the host already runs that patch. It does this
  without `--apply` and before the price and quota checks, so treat it as a
  mutating flag.
- **Sizing.** `--runner-count`, `--runner-cpu`, and `--runner-memory-gb` take
  effect with `--fixed-runner-sizing`. A `--runner-count` of 1 is also honored without it. Without it, the command derives
  one runner per 4 vCPU from `--cpu` and `--ram`. A host below 4 vCPUs or with
  at most 8 GiB of RAM gets one runner, and so does a host where that count
  would leave a runner less than 2 GiB of memory. A tier or strict pool
  overrides these values.
- **Name limit.** The runner name `<prefix>-<instance>-<count>` can have at most
  64 characters.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--project` | String | Owner's project | GCP project. |
| `--org` | String | `swack-tools` | GitHub organization. |
| `--target` | `owner/repo` or organization | Value of `--org` | Target repository or organization. Overrides `--org`. |
| `--max-cost` | Decimal | Required | Compute price cap in USD per hour for this VM. The live price must be strictly below it. |
| `--max-total-daily-usd` | Decimal | None | Compute plus boot and data storage cost cap in USD per day. The total must be strictly below it. A tier caps it at the tier maximum. |
| `--cheapest-storage` | Flag | Off | Choose compatible storage from current regional prices. |
| `--billing-export-table` | `project.dataset.table` | `GCP_BILLING_EXPORT_TABLE` | Billing export table for project-wide cost logs during `--apply`. |
| `--instance-type` | String | `n2-standard-32` | Machine type. |
| `--min-cpu-platform` | String | Preferred platform for the machine family | Explicit minimum CPU platform. |
| `--region` | String | `asia-south2` | GCP region. |
| `--zone` | String | `<region>-a` | GCP zone. |
| `--cpu` | Integer | `32` | Expected vCPU count. Must match what Compute Engine reports for the machine type. |
| `--ram` | Integer | `128` | Expected RAM in GiB. Must match what Compute Engine reports. |
| `--instance-name` | String | `oxidex-spot-runners-asia` | VM name. |
| `--disk-size-gb` | Integer | `100` | Data disk size in GiB. |
| `--disk-type` | String | Compatible type for the machine family | Data disk type. |
| `--disk-device-name` | String | `runner-data` | Name of the data disk resource in the zone, and its device name. An unattached disk with that name and a matching type, size, and performance is reused. |
| `--heavy-runner-pool` | `2`, `3` | None | Strict CI-only pool of that many runners, each with 8 vCPU and 32 GiB and no extra swap. |
| `--runner-count` | Integer | `4` | Runner count. Takes effect with `--fixed-runner-sizing`. |
| `--runner-cpu` | Integer | `4` | vCPU cap per runner. Takes effect with `--fixed-runner-sizing`. |
| `--runner-memory-gb` | Integer | `15` | Memory cap per runner in GiB. Takes effect with `--fixed-runner-sizing`. |
| `--fixed-runner-sizing` | Flag | Off | Use the explicit runner sizing values. |
| `--runner-label` | String | `oxidex-spot-7x27` | Primary custom runner label. Runners also get the machine type, the region, and any patch labels as labels. A strict pool forces `spot-heavy32`. |
| `--runner-group-id` | Integer | The runner group ID that controller launches use | Organization runner group ID. Applies to organization targets. |
| `--runner-prefix` | String | `oxidex-asia` | Runner name prefix. |
| `--existing-instance` | Flag | Off | Adopt and bootstrap an existing Spot VM instead of creating one. With `--apply`, it labels the VM, attaches the data disk or turns on its auto-delete, writes startup metadata, and resets a running VM or starts a stopped one. |
| `--deployment-receipt` | Path | None | Save the created VM identity before waiting for runner registration. |
| `--registration-timeout-seconds` | Integer | `1200` | Fail when bootstrap has not finished this long after deployment starts. |
| `--apply` | Flag | Off | Run provisioning and startup-script deployment. |
| `--remote-builder` | Flag | Off | Install the legacy shared-host Rust builder on a tier host. See the note after this table. |
| `--resource-monitoring` | Flag | Off | Install metrics-only Ops Agent memory monitoring. |
| `--tier` | `small`, `starting`, `expanded`, `large`, `bulky` | None | Tier host with shared memory admission. The `--cpu` and `--ram` values must match the tier shape. |
| `--tier-slots` | Integer | Tier's initial slot count | Number of runner listeners on a tier host. |
| `--tier-job-memory-gib` | Number | `15` | Per-job memory cap and reservation. `15` is a verified compatible workload cap, not a measured peak. |
| `--host-overhead-gib` | Number | `2` | Host memory for the operating system and services. A tier host reserves the larger of this value and 2 GiB, plus 0.25 GiB for each listener slot. Without a tier, it applies only to hosts with 8 GiB of RAM or less. |
| `--config-repo` | `owner/repo` | `<org>/spot-runners-config` | Central runner configuration repository. |
| `--patch-repo` | `owner/repo` | None | Repository whose patch manifest the command loads from the configuration repository. |
| `--patch-file` | Path | None | Local `spot-runner-patch-v1` YAML or JSON manifest. Choose at most one of `--patch-file` and `--patch-repo`. |
| `--patch-live` | Flag | Off | Hot-patch a running VM and its active runner containers in place. Needs `--patch-repo` or `--patch-file`. |

The `--remote-builder` help text says that the flag installs a dedicated Rust
1.97.1 remote builder. The flag does build the `oxidex-remote-builder:1.97.1` image from
`builder/Dockerfile`, but that image is the legacy shared-host builder, not a
dedicated builder. Dedicated `builder-*` VMs use Rust 1.99.0 and come from
[`src/render_builder_startup.py`](#render-builder-startup). The flag requires
`--tier`, and a strict pool rejects it. Remote builds on a legacy host go through
[`src/remote_rust_build.py`](#remote-rust-build).

A strict pool needs an organization target, `--runner-group-id`, and host
headroom of 2 vCPU and 16 GiB beyond the pool. A pool is incompatible with
`--tier` and `--remote-builder`.

This preview prints the plan and creates nothing:

```bash
python3 src/spot_runner_manager.py \
  --project YOUR_PROJECT --org swack-tools \
  --max-cost 0.125 --max-total-daily-usd 3 \
  --instance-type n4-standard-16 --region asia-east2 --zone asia-east2-b \
  --cpu 16 --ram 64 --tier large --tier-slots 20 \
  --tier-job-memory-gib 32 --instance-name oxidex-runners-example
```

For the patch flags, see [Runner patches](runner-patches.md).

## `src/spot_runner_autoscaler.py` {#spot-runner-autoscaler}

Runs a controller that keeps an online floor of cost-capped runners. Each check
reads GitHub and GCP, decides whether to grow, shrink, or rotate hosts, and
launches hosts through [`src/spot_runner_manager.py`](#spot-runner-manager). It
persists operations in a state file so that a restart resumes an interrupted
operation. See [Fleet and autoscaler](fleet-and-autoscaler.md) for the policies.

- **Effect.** Without `--dry-run`, the controller creates, drains, rotates, and
  deletes VMs and runner registrations, and reruns eligible crashed jobs.
- **Dry run.** `--dry-run` prints one JSON tier plan and changes no VM, disk,
  runner registration, or controller state. It reads host evidence with
  `gcloud compute ssh`, which can add your SSH key to project or instance
  metadata, or to your OS Login profile, on the first connection. It evaluates the tier policy whatever
  `--scaling-mode` says. It still creates the log file, the state directory, and
  the state lock file on your machine. It takes the same lock as a running
  controller, so it fails while a controller uses that state file: pass another
  `--state-file` to preview next to a running controller. With
  `--manage-control-vm`, it also prints a `Control VM plan (dry run):` text line
  before the JSON and creates the project control VM lock file.
- **LaunchAgent.** `--install-launch-agent` writes
  `~/Library/LaunchAgents/com.swacktools.spot-runner-autoscaler.plist`, runs
  `launchctl bootstrap`, and exits. The agent has `RunAtLoad` and `KeepAlive`,
  so a live controller starts at once, and its arguments never include
  `--dry-run`. It runs before the `--dry-run` branch, so `--dry-run` does not
  make it safe. The command doesn't check the `launchctl` exit status.
- **Single controller.** An exclusive lock on the sibling `<state file>.lock` allows one
  controller for each state file. A state file binds to one target. Use a
  separate `--state-file` for each `--target`.
- **Polling limits.** The `resource` and `tiered` modes need
  `--interval-seconds 300`. The `tiered` mode needs `--maximum-hosts` of 5 or
  less.
- **Remote builder.** The controller passes `--remote-builder` to the manager
  only when it launches a tiered host. In `resource` and `legacy` modes the
  flag has no effect.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--org` | String | `swack-tools` | GitHub organization. |
| `--target` | `owner/repo` or organization | Value of `--org` | Target repository or organization. |
| `--make-before-break`, `--no-make-before-break` | Flag pair | Off | Run zero-downtime make-before-break rotation on price spikes. A controller with `--target` always rotates this way, whatever this flag says. The flag has no effect in `tiered` mode, which replaces an overpriced host through its own guarded tier price replacement. |
| `--github-user` | String | None | Use an existing `gh` account for this process without saving its credential. |
| `--project` | String | Owner's project | GCP project. |
| `--minimum-runners` | Integer | `1` | Online runner floor for the `resource` and `legacy` modes. Must be at least 1. The `tiered` mode ignores it. |
| `--label` | String | `spot` | Runner label that the controller manages. |
| `--max-daily-usd` | Decimal | `10` | Maximum daily compute plus boot and data storage cost per VM. A tier uses its own cap. |
| `--interval-seconds` | Integer | `300` | Seconds between checks. |
| `--scaling-mode` | `tiered`, `resource`, `legacy` | `resource` | Scaling policy. `resource` uses CPU and memory over two 5-minute checks at the 75 percent threshold. `legacy` uses runner occupancy. `tiered` uses tier evidence. |
| `--maximum-hosts` | Integer | `5` | Maximum number of Spot hosts for `resource` scale-up and tier migrations, plus one host of overlap during a rotation or migration. The `legacy` scale-up doesn't check it. |
| `--tier-job-memory-gib` | Positive finite number | `15` | Per-job memory cap and reservation. `15` is a verified compatible workload cap, not a measured peak. |
| `--dry-run` | Flag | Off | Tier decision only; changes no VM, disk, registration, or controller state. Its `gcloud compute ssh` can add your SSH key. |
| `--manage-control-vm` | Flag | Off | Also manage the control VM that hosts the dashboard. With `--dry-run`, print only its plan. |
| `--migrate-oversized` | Flag | Off | Gradually replace existing oversized owned hosts. |
| `--remote-builder`, `--no-remote-builder` | Flag pair | Enabled | Install the legacy shared-host Rust builder on new tiered hosts. |
| `--disk-size-gb` | Integer | `100` | Data disk size for new hosts. |
| `--billing-export-table` | `project.dataset.table` | `GCP_BILLING_EXPORT_TABLE` | Billing export table for launch cost logs. |
| `--log-file` | Path | `~/spot-ghrunner.log` | Append-only controller log. |
| `--state-file` | Path | `~/Library/Application Support/Spot Runner Autoscaler/state.json` | Controller state file. |
| `--install-launch-agent` | Flag | Off | Install the macOS LaunchAgent and exit. |
| `--patch-repo` | `owner/repo` | None | Repository whose patch manifest every launched host gets. Needed to rotate hosts that run a patch. |
| `--config-repo` | `owner/repo` | `<org>/spot-runners-config` | Central runner configuration repository. |
| `--job-repository` | `owner/repo`, repeatable | The repository target itself, or `<target>/oxidex` for an organization target | Repository whose crashed jobs the controller tracks and reruns. |
| `--once` | Flag | Off | Check and scale once, then exit. |

This preview reads providers and prints one plan:

```bash
python3 src/spot_runner_autoscaler.py \
  --project YOUR_PROJECT --org swack-tools \
  --github-user YOUR_GITHUB_ACCOUNT \
  --scaling-mode tiered --tier-job-memory-gib 32 \
  --no-remote-builder --dry-run --once
```

## `src/control_vm.py` {#control-vm}

Inspects and manages the control VM, either a Spot VM or the always-on free-tier
`e2-micro`, that hosts the web dashboard. The VM exists as two fixed slots,
`spot-control-a` and `spot-control-b`, for make-before-break replacement. See
[Edge hosting and control VM](edge-dashboard.md) for the design.

| Subcommand | Effect |
| --- | --- |
| `status` | Prints control VMs, the active origin, health, and transition state as JSON. Read-only. |
| `plan` | Prints the next lifecycle action. Read-only. |
| `render-startup` | Writes the static control VM startup script to `--output`. Always writes that file. |
| `ensure` | Runs one lifecycle step: bootstrap, start, recover, or price rotation. Prints a plan unless you pass `--apply`. |
| `rotate` | Runs a make-before-break rotation to the other slot now. Prints a plan unless you pass `--apply`. |

The top-level help says that nothing is written without `--apply`. That is
accurate for the control VM, the disks, and the state files, but
`render-startup` writes its `--output` file without `--apply`. Only `ensure` and
`rotate` accept `--apply`. With `--apply`, the command takes the project-wide
control VM lock, creates, starts, stops, or deletes VMs, writes Cloudflare
Workers KV entries, and saves the project control VM state. It also moves any
control VM state saved in the autoscaler `--state-file` into the project state
and rewrites that file. Without it, the command plans on an in-memory copy of the state.

### Shared options

Every subcommand accepts these options. A flag overrides its environment
variable.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--project` | String | Owner's project | GCP project. |
| `--state-file` | Path | `~/Library/Application Support/Spot Runner Autoscaler/state.json` | Autoscaler state file whose control VM state, saved before project scoping, moves to the project state file. |
| `--control-state-dir` | Path | Directory of the default state file; `SPOT_CONTROL_STATE_DIR` | Directory of the project-scoped control VM state and lock. |
| `--provisioning` | `spot`, `standard` | `spot`; `SPOT_CONTROL_PROVISIONING` | `spot` picks the cheapest Spot shape under the daily cap. `standard` runs the free-tier `e2-micro` in `us-central1`, `us-east1`, or `us-west1`. |
| `--regions` | Comma-separated regions | `us-central1`, `us-east1`, `us-west1` (`us-central1` in `standard`); `SPOT_CONTROL_REGIONS` | Allowed regions. |
| `--max-daily-usd` | Decimal | `0.50`; `SPOT_CONTROL_MAX_DAILY_USD` | Daily compute plus boot-disk cap per control VM. |
| `--no-external-ip` | Flag | External IP on; `SPOT_CONTROL_EXTERNAL_IP` set to `0`, `false`, `no`, or `off` also turns it off | Create the VM without an external IP. Needs a NAT gateway for egress. |
| `--service-account` | String (email address) | None; `SPOT_CONTROL_SERVICE_ACCOUNT` | VM service account. |
| `--dashboard-image` | Digest-pinned image reference | None; `SPOT_DASHBOARD_IMAGE` | Dashboard image. |
| `--dashboard-repo` | `owner/repo` | `swack-tools/spot-github-runners`; `SPOT_DASHBOARD_REPO` | Repository that a `spot` VM builds when no image is set. A `standard` VM never builds: it pulls the published image for `--dashboard-source-ref`, and only when this is the default repository. |
| `--dashboard-source-ref` | 40-character commit hash | None; `SPOT_DASHBOARD_SOURCE_REF` | Commit to build. |
| `--public-url` | URL | `https://spot.swacktech.com`; `SPOT_DASHBOARD_PUBLIC_URL` | Public dashboard URL. |
| `--cloudflared-image` | Image reference with a version tag or digest (`:latest` is refused) | A pinned `cloudflared` release; `SPOT_CLOUDFLARED_IMAGE` | Tunnel client image. |
| `--origin-domain` | Domain | A domain of the repository owner; `SPOT_CONTROL_ORIGIN_DOMAIN` | Domain of the per-slot tunnel hostnames. Always set it explicitly. |
| `--config-repo` | `owner/repo` | None; `SPOT_CONFIG_REPO` | Dashboard runner configuration repository. |
| `--controlled-targets` | Comma-separated targets | None; `DASHBOARD_CONTROLLED_TARGETS` | Organizations and `owner/repo` targets on which the dashboard requires administrator rights. |
| `--global-admins` | `login:numeric_id` entries, or `none` | Dashboard default; `DASHBOARD_GLOBAL_ADMINS` | Users that skip per-target administrator checks. |

Additional options:

| Subcommand | Flag | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `render-startup` | `--output` | Path | Required | File to write the startup script to, with mode `0644`. |
| `ensure`, `rotate` | `--apply` | Flag | Off | Execute the step instead of printing the plan. |

Health probes and freeze or import steps need the Cloudflare Access service
token (`CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`) and the control
secret (`DASHBOARD_CONTROL_SECRET`). When they are missing, the manager reports
the problem and pauses lifecycle changes. The command loads `.env` from the
repository root, except for `render-startup`.

```bash
python3 src/control_vm.py plan --project YOUR_PROJECT
```

## `src/web_dashboard.py` {#web-dashboard}

Runs the HTTP server for the fleet and Rust builder dashboard, or prints its
initial overview. See [Dashboard](dashboard.md) for the interface and
[Edge hosting and control VM](edge-dashboard.md) for sign-in and state
durability.

- **Effect.** The server writes local files only when you pass `--state-file`:
  the state file, plus freeze markers, recovery markers, and renamed copies of
  an unreadable state file next to it. `--dump-overview` with `--state-file`
  still creates or rewrites that file, so omit `--state-file` for a dump
  without local writes. The server also exposes POST routes, so treat a
  running dashboard as able to change state. `/api/instances/delete` and
  `/api/instances/scale-region` change only the dashboard's own records, not
  VMs. When `SPOT_CONFIG_GH_TOKEN` or `GH_TOKEN` is set in the server's
  environment, `POST /api/patches/save` creates a branch, commits, and opens a
  pull request on the config repository, which is `--config-repo`,
  `SPOT_CONFIG_REPO`, or `<org>/spot-runners-config`. With
  `DASHBOARD_HOT_PATCH_EXECUTOR=gcloud`, `/api/patches/hot-patch` changes
  running hosts.
- **Binding.** The default bind address is loopback. A non-loopback `--host`
  needs authentication (`--auth-token` or GitHub OAuth 2.0) and either TLS or
  `--allow-insecure-non-loopback` behind a trusted TLS-terminating proxy. The
  server refuses to start otherwise.
- **KV.** Cloudflare KV hydration and write-behind need `--control-slot` plus the
  `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_KV_NAMESPACE_ID`, and
  `CLOUDFLARE_KV_API_TOKEN` environment variables. Without `--control-slot`, the
  server logs a warning when those credentials exist and keeps state local.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--host` | String | `127.0.0.1` | Bind address. |
| `--port` | Integer | `8787` | HTTP port. |
| `--project` | String | Owner's project | GCP project ID. |
| `--org` | String | `swack-tools` | GitHub organization. |
| `--auth-token` | String | `DASHBOARD_AUTH_TOKEN` | Legacy bearer token. The dashboard accepts it only from a direct loopback client with no forwarding headers, such as an SSH-forwarded connection. Binding a non-loopback address requires it or GitHub OAuth 2.0, but direct non-loopback clients can authenticate only with OAuth 2.0. |
| `--tls-certfile` | Path | `DASHBOARD_TLS_CERTFILE` | PEM TLS certificate. Pair it with `--tls-keyfile`. |
| `--tls-keyfile` | Path | `DASHBOARD_TLS_KEYFILE` | PEM TLS private key. |
| `--allow-insecure-non-loopback` | Flag | `DASHBOARD_ALLOW_INSECURE_NON_LOOPBACK` set to `1`, `true`, `yes`, or `on` | Allow plaintext HTTP on a non-loopback host behind a trusted TLS-terminating proxy. |
| `--control-slot` | `a`, `b` | None | Control VM slot. Enables KV hydration and write-behind when the KV credentials exist, and `/api/control/*` when `DASHBOARD_CONTROL_SECRET` is set. |
| `--controlled-targets` | Comma-separated targets | `DASHBOARD_CONTROLLED_TARGETS`, else none | Organizations and `owner/repo` targets on which OAuth 2.0 users must be administrators, in addition to every repository in the configuration repository. |
| `--oauth-redirect-uri` | Absolute URL | `GITHUB_OAUTH_REDIRECT_URI`, else the public origin plus `/auth/github/callback`, else derived from the `Host` header | GitHub OAuth 2.0 callback URL. |
| `--config-repo` | `owner/repo` | `SPOT_CONFIG_REPO`, else `<org>/spot-runners-config` | Repository whose `repos/<owner>/<repo>.yaml` files are controlled targets. |
| `--public-origin` | Origin URL | `DASHBOARD_PUBLIC_ORIGIN`, else the origin of the OAuth 2.0 redirect URI | Browser-facing origin that POSTs accept when a proxy forwards under a different `Host`. |
| `--state-file` | Path | None | Persist dashboard state JSON to this path. Freeze and recovery markers go next to it. |
| `--dump-overview` | Flag | Off | Print the initial JSON overview and exit without serving. With `--state-file`, it still creates or rewrites that file. |

```bash
python3 src/web_dashboard.py --dump-overview
```

## `src/render_builder_startup.py` {#render-builder-startup}

Validates a dedicated builder configuration and an approved uploader identity,
then renders a self-contained startup payload and a single `gcloud compute
instances create` command. Dedicated builders have a fixed shape (`n4-highcpu-16`
with 16 vCPU and 32 GiB). See [Dedicated builders](dedicated-builders.md) for the
design and the full launch procedure.

- **Effect.** Writes four files into a new directory: `startup.sh`,
  `cloud-config.yaml`, `cargo-remote-env.toml`, and `launch-command.sh`. It
  makes no cloud call. Running `launch-command.sh` creates a billable VM and a
  persistent cache disk with `auto-delete=no`.
- **New directory only.** The command creates `--output-directory` with mode
  `0700` and refuses an existing directory. The parent directory must already exist. The rendered files aren't
  executable, so run the launch command with `bash`. Fresh-host startup must not replay on
  an occupied builder. Use
  [`builder_runtime_update.py`](#builder-runtime-update) for an occupied builder.
- **Output.** Prints the path of `launch-command.sh`.
- **Configuration file.** A JSON file with `instance_name` (must start with
  `builder-`), `project`, and `zone`. The renderer fills in the fixed shape,
  disk, and container settings. Explicit values still pass the strict policy
  checks. Without `--uploader-mapping`, the file must embed `authorized_uploader`
  and `authorized_uploader_sha256`.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--config` | Path | Required | JSON configuration file. |
| `--uploader-mapping` | Path | None | Approved public uploader mapping JSON. The renderer computes its checksum. Do not combine it with embedded `authorized_uploader` fields. |
| `--deployment-id` | String | `instance_name` from the configuration | Deployment identity in the startup receipt. |
| `--output-directory` | Path | Required | New directory for the rendered files. |

```bash
python3 src/render_builder_startup.py --config /private/builder-config.json \
  --uploader-mapping /private/approved-uploader.json \
  --output-directory /private/builder-payload
```

Review the files before you run `launch-command.sh`.

## `src/lib/builder_runtime_update.py` {#builder-runtime-update}

Updates the runtime files of an idle, occupied dedicated builder in place. It
does not provision a VM or rebuild an image. See
[Update a builder runtime](builder-runtime-updates.md) for the procedure and
recovery details.

The program refuses to run unless it runs as root on Linux with Python isolated
mode and no bytecode (`python3 -I -B`). Run it through the existing authenticated
root control path on the builder. The `--help` text works anywhere.

The workflow has these stages:

1. **Capture.** `--capture-assets` records the live VM, boot, runtime files, and
   admission process, and prints a plan. This stage is read-only.
2. **Check.** `--plan` with `--plan-sha256` and no action flag checks a reviewed
   plan and writes nothing. It returns `CHECKED_ONLY`, or `CHECKED_FINISH_ONLY` when you also pass `--client-launcher-sha256`.
3. **Apply.** `--apply` takes the build lock, refuses active builds, creates an
   owned drain, backs up files, and replaces the fixed runtime file set. It
   returns `INSTALLED_DRAINED`. The route stays closed.
4. **Client pin handoff.** Coordinate the exact OxiDex launcher pin out of band.
   The helper does not change clients.
5. **Finish.** Capture a new plan with `--finish-nonce` and
   `--client-launcher-sha256`, check it, then run it with `--finish`. It reopens
   the route and returns `REOPENED_UNQUALIFIED`. Qualification readiness stays
   false until real build checks pass.

The recovery modes are `--recover-finish` for an interrupted finish on the same
boot and `--recover-committed` for a committed release that a reboot
interrupted. Never replay `--apply` to recover.

Choose exactly one of `--plan` and `--capture-assets`. Choose at most one of the
four action flags.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--plan` | Path | One of `--plan` or `--capture-assets` is required | Reviewed plan file. Needs `--plan-sha256`. |
| `--capture-assets` | Path | One of `--plan` or `--capture-assets` is required | Root-owned asset JSON file with the reviewed runtime targets. Needs `--assets-sha256` and `--source-main`. |
| `--plan-sha256` | SHA-256 hex digest | None | Digest of the plan file. The plan must match it. |
| `--assets-sha256` | SHA-256 hex digest | None | Digest of the asset file. |
| `--source-main` | Commit hash | None | Reviewed source commit for the capture. |
| `--apply` | Flag | Off | Apply the reviewed plan. Without it, a plan is only checked. |
| `--finish` | Flag | Off | Reopen an installed route. Needs `--client-launcher-sha256`. |
| `--recover-committed` | Flag | Off | Recover a committed release after a reboot. Needs `--client-launcher-sha256`. |
| `--recover-finish` | Flag | Off | Restore an interrupted finish to the held state. Needs `--client-launcher-sha256` on the plan stage and `--finish-nonce` on capture. |
| `--finish-nonce` | 32 hex characters | None | Installation nonce for a capture of fresh observations. Valid only with `--capture-assets`. |
| `--client-launcher-sha256` | SHA-256 hex digest | None | Paired client launcher pin. It selects the finish checks, and you cannot combine it with `--apply`. |

A capture with `--finish-nonce`, `--recover-finish`, or `--recover-committed`
needs `--client-launcher-sha256`, and `--recover-finish` and `--recover-committed` need the nonce too.
The `--apply` and `--finish` flags are invalid on a capture. A committed-release error exits with status `75` after it prints the result as
JSON.

This capture is read-only:

```bash
python3 -I -B builder_runtime_update.py --capture-assets assets.json \
  --assets-sha256 ASSET_FILE_SHA256 --source-main REVIEWED_SOURCE_COMMIT
```

## `src/remote_rust_build.py` {#remote-rust-build}

Snapshots a local OxiDex checkout, uploads it to an explicitly selected legacy
shared-host builder, builds the `oxidex` binary there, verifies the checksum,
and records evidence. It uses `gcloud compute scp` and `gcloud compute ssh`, so
use it only for legacy hosts that were installed with the manager's
`--remote-builder` flag. Do not use it for dedicated `builder-*` VMs. Those use
direct authenticated SSH from the OxiDex `just build` recipe. See
[Remote builds and clients](remote-builds.md).

- **Effect.** Always transfers source and starts a remote build. It writes
  `remote-build.json`, `remote-source.tar.gz`, and `transaction.log` to the
  evidence directory.
- **Snapshot.** The archive holds only files that `git ls-files` lists. It skips
  the `.git`, `.codex`, `.claude`, `.agents`, `target`, `node_modules`, and
  `.venv` directories, files whose names start with `.env`, files with a `.pem`
  or `.key` extension, and symbolic links.
- **Cleanup.** When the command exits, it removes its upload and export
  directory from the host and records whether the cleanup succeeded.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--source` | Path | Required | Local checkout to snapshot. |
| `--instance` | String | Required | VM name of the builder. |
| `--zone` | String | Required | Zone of the builder. |
| `--project` | String | Owner's project | GCP project. |
| `--worktree-id` | 1 to 64 letters, digits, underscores, or hyphens; the first character is a letter or digit | Required | Identifier of the remote project directory. |
| `--evidence-dir` | Path | Required | Directory for the receipt and logs. |
| `--profile` | `debug`, `release` | `release` | Cargo profile. |
| `--artifact-dir` | Path | None | Download the built `oxidex` binary here and verify its checksum. |

Retryable failures are exit codes `75`, `137`, `143`, and `255`; the receipt
records a `retryable` flag. Because this command always builds, no read-only
example exists. Use `--help` to check the options:

```bash
python3 src/remote_rust_build.py --help
```

## `src/cargo_remote.py` {#cargo-remote}

Adapts `cargo oxidex-remote` to [`remote_rust_build.py`](#remote-rust-build) for a
configured OxiDex checkout. It runs inside the current Git repository, reads the
`[env]` table of `.cargo/config.toml` at the repository root, and then calls
`remote_rust_build.py` with the repository as `--source`. It fails when the file
is missing. After you install the shims with
[`install_remote_commands.py`](#install-remote-commands), run it as
`cargo oxidex-remote`.

The command resolves each setting from the process environment first, then from
the `[env]` table:

| Setting | Required | Fallback |
| --- | --- | --- |
| `OXIDEX_REMOTE_INSTANCE` | Yes | None |
| `OXIDEX_REMOTE_ZONE` | Yes | None |
| `OXIDEX_REMOTE_PROJECT` | No | `gcloud config get-value project` |
| `OXIDEX_REMOTE_WORKTREE` | No | `oxidex-` plus the first 12 hex characters of the SHA-256 of the checkout path |

The adapter sets `--evidence-dir` to a new directory named
`cargo-remote-<UTC timestamp>` under `~/oxidex-ops/evidence/`. It passes the rest
of your arguments, such as `--profile` and `--artifact-dir`, to
`remote_rust_build.py`, so those options match the table in the previous
section. Like that command, it always builds. Its `--help` output is the `remote_rust_build.py` parser's, so it lists
`--source`, `--instance`, `--zone`, `--worktree-id`, and `--evidence-dir` as required even though the adapter
supplies them from the checkout and the environment. Options that you pass are appended after the adapter's and
override them. Run `--help` to see the options:

```bash
python3 src/cargo_remote.py --help
```

## `src/auto_remote_build.py` {#auto-remote-build}

Chooses a lightly used legacy builder host and delegates the build to it. After
you install the shims, run it as `cargo oxidex-auto`.

1. It reads the project from `OXIDEX_REMOTE_PROJECT` (environment or the
   `[env]` table of `.cargo/config.toml`), else from `gcloud config`.
2. It lists running VMs whose names start with `oxidex-runners-` or
   `oxidex-buildbench-`, reads CPU and memory utilization from GCP Monitoring,
   and keeps hosts below 75 percent on both.
3. It probes each candidate over SSH for `/etc/oxidex-remote-build.json` and no
   `/run/oxidex-build-draining`, and picks the best-ranked match.
4. It runs `cargo remote-build` with the chosen instance and zone, writes
   `selection.json` and the build evidence under
   `~/oxidex-ops/evidence/auto-remote-<UTC timestamp>/<attempt>`, and downloads
   the artifact into `target/remote-linux/<profile>` in the checkout.
5. After a retryable failure, it excludes that host and selects again, up to
   `--max-attempts` times.

The OxiDex checkout must provide the `cargo remote-build` command, because this
repository installs `cargo-oxidex-remote` and `cargo-oxidex-auto` but no
`cargo-remote-build`.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--profile` | `debug`, `release` | Required | Cargo profile. |
| `--max-attempts` | Positive integer | `3` | Maximum worker selections after retryable failures. |

The command always builds on a remote host. Check its options with:

```bash
python3 src/auto_remote_build.py --help
```

## `src/install_remote_commands.py` {#install-remote-commands}

Writes two executable shell shims that run scripts from this checkout:
`cargo-oxidex-auto` (runs `auto_remote_build.py`) and `cargo-oxidex-remote`
(runs `cargo_remote.py`). Each shim embeds the absolute path of this checkout, so
reinstall the shims if you move it. The command overwrites existing shims and
creates the directory if needed. It prints one `path -> script` line per shim.

| Flag | Type or choices | Default | Meaning |
| --- | --- | --- | --- |
| `--bin-dir` | Path | `$CARGO_HOME/bin`, else `~/.cargo/bin` | Directory for the shims. |

Preview the result in a throwaway directory:

```bash
python3 src/install_remote_commands.py --bin-dir "$(mktemp -d)"
```

## Proof runners {#proof-runners}

`tools/infra_python_test_proof.py` and `tools/infra_python_complete_proof.py` are
continuous integration (CI) runners that measure the unit test suite inside the
admitted Linux builder container. Neither accepts flags, and you do not run them
by hand. For local test runs, see [Development](development.md).

- **Inputs.** Both expect a read-only `/src` mount with the source tree, a
  `fleet-source-head` file, and an `infra-python-test-manifest.json`
  manifest, plus a writable `/target` directory. The runners don't verify a
  signature themselves: they require the manifest's `source_commit` to equal
  `fleet-source-head` and each test module to match its SHA-256 in the
  manifest.
- **Outputs.** They write `infra-python-test-proof.json` and
  `infra-python-test.log` to `/target` with exclusive creation, and refuse to run
  when either file exists.
- **Exit codes.** `0` for a pass, `1` for a failed or incomplete measurement,
  and `2` when the runner refuses the input.
- **Producer.** `infra_python_test_proof.py` verifies that the test modules match
  the manifest, runs `unittest` discovery, and enforces floors for test cases
  (`MIN_CASES`) and modules (`MIN_MODULES`). A skipped test, an expected
  failure, an unexpected success, or a module with no test cases fails the run.
- **Complete proof.** `infra_python_complete_proof.py` loads the producer only if
  its SHA-256 equals the pinned `PRODUCER_SHA`, replaces the producer's floors
  with `max(774, module count)` cases and `max(76, module count)` modules,
  requires the executed test IDs to equal the discovered IDs, and adds digests of the discovered and executed test IDs to
  the proof. After a reviewed change to the producer, update `PRODUCER_SHA` and
  rerun the proof tests.

## `just` recipes {#just-recipes}

The `justfile` defines three recipes. Run `just` with no arguments to list them.

| Recipe | Effect |
| --- | --- |
| `just test` | Submits this checkout to the reviewed OxiDex remote route. Needs `OXIDEX_CONTROL_REPO` to point to an OxiDex control checkout whose `justfile` has a `test-infra-ci-remote` recipe, and runs that recipe with this checkout's path. Exit status `2` if either is missing. |
| `just infra-python-complete-tests` | Runs `PYTHONPATH=src python3 -B tools/infra_python_complete_proof.py`. It refuses to run unless you are a non-root user on Linux inside the admitted builder container (the `/run/oxidex-build-container` file must exist). |
| `just` | Lists the recipes. Read-only. |

Do not report `just test` as run when you ran only the local `unittest`
discovery. The two prove different things.
