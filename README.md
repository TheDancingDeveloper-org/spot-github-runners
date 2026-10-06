# Spot runner fleet

This repository contains Python tools for finding affordable GCP Spot
VMs, provisioning persistent GitHub Actions runners, and restoring runner
capacity after interruptions. The fleet belongs to the `swack-tools`
organization and uses the repository-scoped `oxidex-m4air` runner group for
`swack-tools/oxidex`.

The runner image includes `zstd` for generated-table artifact compression.
The separate [M4 Air configuration](m4air/README.md) provides Docker access
for the two ARM64 runners through the OrbStack socket.

## How the fleet works

The installed controller can use `--scaling-mode tiered` to choose VM size and
runner density from complete five-minute CPU/RAM windows, compatible queued job
waits, successful job timings and host/container OOM events. It changes one host
at a time. Missing evidence holds optimization. Two hot windows grow capacity;
six low windows and projected peak demand are required before shrinking. A new
host OOM bypasses the debounce; a container-only OOM increases its memory limit
on an idle host before adding compute.

| Tier | vCPU | RAM GiB | Initial / maximum slots | Daily cap including all disks |
| --- | ---: | ---: | ---: | ---: |
| small | 4 | 16 | 4 / 8 | $0.60 |
| starting | 8 | 16 | 8 / 8 | $1.25 |
| expanded | 8 | 32 | 8 / 16 | $1.25 |
| large | 16 | 64 | 20 / 20 | $3.00 |
| bulky | 32 | 128 | 16 / 20 | $4.50 |

Price must be strictly below the tier cap. Normal queue growth follows
16, 16+4, 16+8, 16+16, and repeats the small tail up to five hosts. Measured CPU
or memory pressure can instead expand a 16-vCPU host to 32 vCPU. The fleet stays
within five hosts, 80 vCPU and 100 registered slots, including replacement overlap.
The reverse ladder reaches one 4-vCPU host. Queues with resource headroom increase
slots first. Registered slots are distinct from admitted simultaneous jobs.

All runners share CPU weights. A root-owned memory admission service reserves
space for host processes and listeners, admits each CI job in its pre-job hook,
and shares its ledger with dedicated remote build containers. Docker/cgroup OOM
events are distinct from whole-host OOM; exit 137 alone is insufficient evidence.
The CI containers have no host Docker socket. Docker-specific workflow jobs and
native ARM64 jobs retain their separate placement requirements.

Replacement records retain exact provider VM and disk identities. A replacement
must register its expected runner names and pass two complete healthy windows and
a real builder canary before the source is isolated in a GitHub runner group with
no repository access. Already assigned jobs finish admission and execution. Drain
waits for fresh idle runners, completed tracked jobs, absent runner registrations,
and no active remote build before deleting the exact VM and its attached disks.
A five-host fleet holds replacement until measured remaining capacity permits a
safe drain first. Price-driven replacements follow these same readiness gates.

Use `--migrate-oversized` for gradual conversion of an existing fleet and
`--dry-run --once --scaling-mode tiered` for provider/state read-only planning.
`--scaling-mode resource` retains the former uniform-host CPU/RAM policy;
`--scaling-mode legacy` retains runner-occupancy scaling. The existing CLI default
is `resource`; deployment must explicitly select `tiered`.

Memory telemetry requires the VM service account to have
`roles/monitoring.metricWriter` and the Monitoring write scope. Provisioning
installs the metrics-only Ops Agent when resource monitoring is enabled; CPU
utilization comes from the built-in Compute Engine metric.

## Remote Rust builds

Install Cargo command shims directly from this checkout:

```bash
python3 src/install_remote_commands.py
```

The shims run `src/auto_remote_build.py` and `src/cargo_remote.py` from this
repository. Worker selection is in `src/lib/worker_selection.py`; source sync
and execution are in `src/lib/remote_build.py`. Keep this checkout in place.
`oxidex-ops` stores build evidence, not executable copies of these scripts.


The configured laptop checkout supports `just build-debug` and
`just build-release`. Both invoke the automatic worker selector, export
`OXIDEX_REMOTE_INSTANCE`/`OXIDEX_REMOTE_ZONE` for that invocation, then run
`cargo remote-build` through SSH. The older external selector considers running
`oxidex-runners-*` and `oxidex-buildbench-*` VMs; OxiDex uses its repository-owned
client, whose runner-only routing is coordinated separately, ranks complete CPU/memory
windows by available capacity (free cores and free RAM at four GiB per core),
and requires both utilizations below 75%. An SSH probe verifies the dedicated
builder is installed and the host is not draining. Job/build counts do not
influence placement. Every attempt refreshes inventory and CPU/RAM metrics.
SSH disconnects, killed containers and draining hosts trigger reselection on
another VM, source resync and a fresh build, with three attempts by default
(`cargo remote-debug --max-attempts 5` overrides this). Compiler and header
validation errors stop immediately. Missing metrics or no eligible VM are
reported explicitly. Selection is not an exclusive capacity reservation.
The downloaded binary is checksum verified and stored in
`target/remote-linux/debug/oxidex` or `target/remote-linux/release/oxidex`.
These are Linux executables. The original local all-features release recipe is
available as `just build-release-local` in the configured OxiDex checkout.


New tiered autoscaler hosts install a separate `oxidex-remote-builder:1.97.1` image by
default (`--no-remote-builder` opts out). Manual provisioning enables it with
`--tier` and `--remote-builder`; resource and legacy modes do not install an
unadmitted builder. It inherits the runner's Linux dependencies and adds Rust
1.97.1, rustfmt, clippy, just, cargo-nextest, cbindgen 0.29.2, uv, Python 3.11,
rsync and GNU time. GNU ld remains available, matching OxiDex's checked-in linker
configuration. Checksum-verified Zig 0.15.2 and cargo-zigbuild 0.23.4 support
x86_64 and ARM64 musl compilation with persistent cross-tool caches. Installation
reports actual tool versions in the bootstrap log. Darwin SDKs are not installed.

SSH lands on the host. The root-owned `/usr/local/bin/oxidex-remote-build`
launcher accepts simple worktree identifiers and an allowlist of build commands,
then runs an unprivileged UID 1001 container without a Docker socket, capabilities
or privilege escalation. CPU is capped at host vCPU minus four (minimum one). On tier hosts the
builder memory cap is 40% of RAM, bounded by the admitted work envelope. The
root-owned `/etc/oxidex-remote-build.json` controls those limits.
Do not set them above observed host capacity.

Source snapshots are replaced before compilation. Cargo downloads and separate worktree targets persist below
`/mnt/runner-data/remote-build/{sources,cargo,targets}`. A shared host lock protects
active builds from utilization scale-down; draining sets a marker under that
lock to prevent new builds. The container also carries a label so an orphaned
build is visible to the drain guard. These safeguards do not prevent Spot loss
or Spot interruptions. Tier price retirement waits for work to drain; the
older resource/legacy modes retain their forced price eviction behavior.

From the laptop:

```bash
python3 src/remote_rust_build.py \
  --source ~/git/oxidex_refactor \
  --instance YOUR_INSTANCE --zone YOUR_ZONE --project homelab-424523 \
  --worktree-id oxidex-refactor-YOUR_BRANCH \
  --evidence-dir ~/oxidex-ops/evidence/remote-build-YOUR_RUN
```

The client packages current tracked working files, excludes credentials, agent
configuration and build caches, transfers with `gcloud compute scp`, verifies
the archive checksum, and fetches dependencies before timing
`cargo build --release --locked --bin oxidex`. Release builds also run the
pinned `just cbindgen-check` inside the container before compiling; a header
mismatch stops the build. Evidence includes source hashes,
Git revision/status, transfer/fetch/build wall times and GNU time output. Use a
new worktree identifier for a cold build; reusing one retains its target cache.
It compiles Linux artifacts, not macOS executables. Git registration and full
ExifTool qualification are independent of this compilation proof.

In resource/legacy modes, every loop fetches current regional Spot prices and checks each running managed
VM against `--max-daily-usd` (default: $10 per VM per day for compute and attached storage).
Prices are shared within one loop for VMs of the same family and region, then
fetched again on the next loop. The launch price is not a price guarantee.

If a VM's current compute and attached storage price reaches or exceeds the cap, the autoscaler deletes that VM
and its attached disks immediately, even if its runners have active jobs. Those
jobs can fail and need to be rerun. It then searches for affordable replacements
one host at a time and uses the deployment manager to check prices again before launch.
Pending replacements survive process restarts and are retried when no eligible
capacity is available. Utilization scaling waits during replacement work.
If GCP cannot provide a valid price, the autoscaler logs the failed
check, keeps that VM, and retries on the next loop.

## Storage layout

The attached persistent disk mounts at `/mnt/runner-data`. Bootstrap configures
all large and job-related storage there:

| Data | Path |
| --- | --- |
| Docker images and build cache | `/mnt/runner-data/docker` |
| containerd image layers and overlays | `/mnt/runner-data/containerd` |
| Runner configuration and credentials | `/mnt/runner-data/instances/<runner>` |
| Runner workspace and job files | `/mnt/runner-data/work/<runner>` |

Moving only Docker's `data-root` leaves containerd snapshots on the boot disk.
The bootstrap copies existing containerd data to the attached disk, configures
containerd to use that path, restarts Docker, then removes the old default storage.
Each runner's `/` filesystem should report about 98 GiB because its container
overlay uses the attached disk. The host Docker engine runs isolated runner
containers, but its socket is never mounted into a runner. The manager checks
the runner filesystem and socket separation before it reports success. The boot
disk keeps the operating system and packages; increase the data disk only when
its observed free space requires it.

## Runner sizing

Manual hosts without `--tier` retain this sizing. Tier hosts use the table and
shared admission ledger above. For manual hosts, runner count depends on vCPU capacity: one runner per four vCPU. Reserve
8 GiB of host RAM, then divide the remaining memory evenly:

```text
runner_count = host_vCPU // 4
runner_RAM_GiB = (host_RAM_GiB - 8) // runner_count
```

For a 32-vCPU, 128-GiB VM, the calculation is `(128 - 8) / 8 = 15 GiB` per
runner. A 32-vCPU, 256-GiB VM gives 31 GiB per runner. `--fixed-runner-sizing`
uses the explicit values for runner count, vCPU, and RAM.

Each runner's memory is capped. With automatic sizing, its CPU is not. Every runner container
gets an equal CPU weight, so busy runners split the cores evenly, and a job can
use every idle vCPU on the host. Measured over a day, whole-host CPU never
passed 49%, while heavy jobs spent about a quarter of their CPU time throttled
by the former 4-vCPU cap. `--fixed-runner-sizing` keeps `--runner-cpu` as a
hard per-runner vCPU cap.

## Requirements and credentials

Install and authenticate the GCP and GitHub command-line tools:

- `gcloud` needs permission to inspect Compute Engine prices, quotas, VMs, and
disks in project `homelab-424523`.
- `gh` needs permission to manage organization Actions runners in
  `swack-tools`.
- The runner group must be selected-repository scoped and include
  `swack-tools/oxidex`.

The manager obtains a short-lived runner registration token when it deploys a
host. Do not save that registration token in `.env`; it expires quickly. Set
`GH_TOKEN` in the local, ignored `.env` file to a renewable GitHub credential
with the required organization permissions. The manager passes the one-use
registration token through temporary startup metadata, waits for the exact
bootstrap run to finish, then removes the metadata key. It does not write the
token to the image or persistent disk.

Copy `.env.example` to `.env`, then add the GitHub credential. The `.env` file
is ignored by Git. The autoscaler also reads optional `GOOGLE_CLOUD_API_KEY` for
GCP Billing Catalog price lookup. GCP API calls use the active
`gcloud` credentials.

## Preview a deployment

The preview checks the live machine type, Spot compute price, and available CPU
quota. It does not create a disk or VM.

```bash
python3 src/spot_runner_manager.py \
  --max-cost 0.4 \
  --instance-type n2-standard-32 \
  --region asia-south2 \
  --ram 128 --cpu 32
```

`--max-cost` is the strict hourly compute limit for one VM. The manager refuses
to provision at a price greater than or equal to that limit. Disk charges are separate.

## Deploy a host

```bash
python3 src/spot_runner_manager.py \
  --max-cost 0.208333333 \
  --instance-type n2-standard-32 \
  --region northamerica-northeast2 --zone northamerica-northeast2-a \
  --ram 128 --cpu 32 \
  --disk-size-gb 100 \
  --instance-name oxidex-runners-nane2-example \
  --disk-device-name runner-data-example \
  --runner-prefix oxidex-example \
  --runner-label spot \
  --apply
```

The manager verifies current Spot rates and quota before creating resources. It
uses a Spot VM with a 40-GiB boot disk and a 100-GiB data disk configured to
delete with its VM. C4, C4D, N4, and N4D use Hyperdisk Balanced boot and data
disks with NVMe and gVNIC. Both disks explicitly provision the included
3,000 IOPS and 140 MiB/s throughput baseline. N2, N2D, C2D, and E2 use
`pd-standard` boot/data defaults. Manual invocations can explicitly choose a
compatible data disk with `--disk-type`; `--cheapest-storage` selects disks by
their current regional prices. An incompatible existing data disk or explicit disk
type is rejected before attachment. New Spot VMs use the `DELETE` preemption
action, with automatic deletion explicitly enabled for both boot and data
disks. Adopting a VM also enables automatic deletion on its boot and runner
data disks. An ordinary stop preserves disks; existing VMs that use the
`STOP` preemption action retain the controller's loss cleanup behavior.
Deleting a VM removes its boot and runner data disks. Before deployment,
the manager also removes unattached disks carrying its ownership label in the
target zone when they are at least six hours old. This avoids racing an active
provisioning operation. It never deletes attached disks or disks without its
ownership label. GCE startup metadata installs Docker and Compose, configures Docker and
containerd storage, builds the runner image, and registers the persistent
containers. The manager waits for a deployment-specific completion marker and
checks that each runner root uses the data disk and has at least 20 GiB free,
then verifies Docker access is limited to the Docker pool.

Pass `--existing-instance` only when adopting a Spot VM with a supported machine
type and disk configuration. The manager restarts the host to apply the current
bootstrap script. It waits for that script's unique completion marker before
removing startup metadata, so old runner registrations cannot masquerade as a
completed update.

## Inspect fleet status

```bash
python3 src/runner_fleet.py \
  --org swack-tools \
  --project homelab-424523 \
  --label spot \
  --minimum-runners 25 \
  --runners-per-vm 8
```

The `src/lib/runner_fleet.py` module exposes functions to list and count online
organization runners, list and count running GCP VMs, observe both
systems, and calculate scale-out capacity. These functions do not create or
delete resources.

## Run the autoscaler

Run one check and scale pass:

```bash
python3 src/spot_runner_autoscaler.py --once
```

For the older resource/legacy modes, run continuously with a target of 25 online runners across the Spot and Docker
pools, two Docker hosts, a five-minute interval, and a $10 per-VM daily compute and storage
cap:

```bash
python3 src/spot_runner_autoscaler.py
```

The resource/legacy autoscaler considers supported x86 Spot offers with exactly 32 vCPU and
at least 110 GiB of RAM that cost less than the daily total cost cap. It prefers
C4D (AMD Turin), N4D (AMD Turin), C4 (Intel Granite Rapids),
C4 (Intel Emerald Rapids), N4 (Intel Emerald Rapids), C2D (AMD Milan),
N2D (AMD Milan), N2 (Intel Ice Lake), then N2 (Intel Cascade Lake).
Within each family/platform preference it chooses the cheapest regional offer,
including 40 GiB of boot storage and the configured data disk size. The cap
applies to that total: a $9/day VM with $1.50/day of storage does not qualify
under a $10/day cap.
These are provisional hardware estimates; Rust build performance is unmeasured.
E2 hosts remain recognized, but automatic launches exclude E2 because GCP does
not allow requesting a CPU platform for that family. C4's 120 GiB and C4D's 124 GiB qualify; ARM, Local SSD,
and bare-metal variants are excluded. Boot and data disks use the cheapest
supported baseline storage. Older families use Standard, Balanced, or SSD
Persistent Disk, and C4/C4D/N4/N4D use Hyperdisk Balanced. Data disks of at least 2 TiB
also compare Hyperdisk Throughput where supported, including its minimum
provisioned throughput charge. Hyperdisk Balanced uses its included 3,000 IOPS
and 140 MiB/s throughput. Monthly SKU storage prices use Google's 730-hour month for the
daily estimate. The running-VM price guard includes actual attached disk sizes
and provisioned performance; missing prices or disk details pause price-based
decisions. Every deployment verifies fresh regional prices using
the selected host's actual CPU and RAM counts. New runner names use the short `gha`
prefix and are checked against GitHub's 64-character name limit. The autoscaler
stores its idle-loop counters, restoration targets, and pending host operation in
`~/Library/Application Support/Spot Runner Autoscaler/state.json`.

Each scan prices every qualifying shape from one authenticated Billing Catalog
snapshot. The manager and price guard use the same source. Shapes without a
catalog Spot price are excluded because the manager cannot deploy them. The scan
then expands each offer into one candidate per zone and keeps only zones that
pass a launch preflight:

- The zone is `UP` and advertises the requested CPU platform. Small C4 Granite
  Rapids launches are also limited to Google's documented minimum-platform zones.
- The zone offers the machine type and the selected boot and data disk types.
- Regional CPU, family CPU, instance, and disk quotas, and the global CPU quota,
  have room for one more host. Spot VMs use preemptible CPU quota when the
  project has any.

The controller logs the scan totals, the top 10 ranked candidates, and why each
offer without a launchable zone was skipped.

Each launch passes an explicit `--min-cpu-platform` request to GCP. The manager
checks that the observed platform meets the requested minimum before confirming
a running host, and the controller preserves that request across restarts. The
controller tries each candidate once per search. After every failed launch, it
removes that launch's VM, disks, and runner records, rescans prices and
preflight, then tries the next untried candidate. When every candidate has
failed, it logs that the list is exhausted and starts a new search on the next
check. A successful launch also resets the search, so the next host starts from
the top of the list. One check stops starting new attempts after 10 minutes so
price checks and recovery keep running; the next check continues with the next
untried candidate. Logs include the requested platform, observed platform,
preference rank, and compute/storage prices.
For manual provisioning, `--min-cpu-platform` can select a compatible platform;
omitting it defaults to the family's first preference.

Before provisioning, the manager logs project-wide costs before and after
credits for the rolling last 24 hours, 7 days, and 30 days. This requires
a standard or detailed [GCP Billing usage export to BigQuery](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-setup).
Set `GCP_BILLING_EXPORT_TABLE=project.dataset.table` in `.env`, or pass
`--billing-export-table project.dataset.table` to the controller or manager.
The current `gcloud` identity needs BigQuery table read access and permission to
run query jobs in the managed project. Queries use the table's location and a
1 GiB billed-bytes limit. The lookup uses the BigQuery REST API without requiring
the `bq` command-line tool. Missing configuration, empty history, or failed lookups produce an
unavailable warning and do not prevent provisioning. Export timestamps and
partial-history warnings accompany totals because reporting can lag and a new
export may not contain the complete 30-day window. SKU rates cannot substitute
for historical project spend.

To install a per-user macOS LaunchAgent that starts at login and restarts the
process if it exits, run:

```bash
python3 src/spot_runner_autoscaler.py --install-launch-agent
```

The LaunchAgent needs the Mac to be powered on, awake, and logged in. It uses
the current user's `gcloud` and `gh` credentials. Review its status with
`launchctl print gui/$(id -u)/com.swacktools.spot-runner-autoscaler` and its log
at `~/Library/Logs/spot-runner-autoscaler.log`.

The controller appends timestamped events to `~/spot-ghrunner.log` on each run.
It records startup, shutdown, live prices, scaling decisions, VM deletion attempts
and completions, deployment attempts and completions, and changes in VM status.
Every event, including errors and tracebacks, occupies one physical line.
Google API errors show their HTTP code, message, and reason instead of an error
JSON dump. VM events include the instance name, zone, machine type, vCPU, RAM,
root and data disk sizes and types, storage interfaces such as NVMe, and GPUs
when present. Specifications come from provider inventory; unavailable fields
are marked unknown. The controller saves this inventory to keep specifications
available when a VM disappears after a restart.
A missing VM does not prove Spot preemption; check the earlier deletion events
to identify controller actions. The log reports an unconfirmed cause for other
disappearances or terminated VMs.

The controller also tracks CI jobs assigned to managed Spot VMs. It saves job IDs,
runner names, attempts, and job links in the same state file before a VM disappears.
The default repository is `swack-tools/oxidex`; use `--job-repository OWNER/REPO`
(repeat the flag for multiple repositories) to change the tracked set. These
repositories require Actions read/write and Checks read permissions through `gh`.

When a VM disappears or reaches `TERMINATED`, the controller logs the jobs that
were running on it. It waits for GitHub to report a failed job with a runner-loss
error before logging that the VM loss caused a CI crash. Successful jobs, user
cancellations, ordinary code failures, and controller-initiated deletions do not
trigger automatic crash retries. Job failures completed before the last observed
VM presence are also excluded. A one-hour startup discovery window and retained
in-progress run IDs (including workflows with queued jobs) cover jobs that finish
between controller polls. Each repository retains its own discovery watermark.

After the workflow run completes, it requests an individual crashed-job rerun.
GitHub may also rerun jobs that depend on it. Multiple crashed jobs from the same
run are submitted one at a time. The controller recognizes newer job attempts
submitted manually, logs accepted requests and eventual results, and saves its
request intent before calling GitHub. A shared state-file lock prevents concurrent
controllers from submitting the same requests. If a request loses its response,
the controller checks for a newer job attempt and holds automatic resubmission;
its log requests a manual GitHub check when the receipt remains uncertain. A
confirmed HTTP rejection can be retried on the next loop. Unconfirmed crash
investigations expire after one day; job records are retained for up to 30 days.

When a managed VM terminates or disappears, the controller logs its replacement
search and checks its saved disk attachments before launching replacement
capacity. For a terminated host that still exists, it verifies the instance ID
and current attachments before deleting the host with its configured disk
auto-delete behavior. It then deletes any surviving recorded persistent drive
in the same project only when its ID still matches and it is ready and
unattached, regardless of its prior auto-delete setting. It verifies that all
recorded drives are gone before launching a replacement. Attached, recreated,
foreign, or unverifiable drives, incomplete legacy inventories, and failed API
calls leave recovery pending with a reason in the log. Cleanup and replacement
work survive restarts. Intentional scale-down and price evictions have their own
saved records, so they do not trigger an extra interruption replacement.

The controller saves each host operation's name, zone, and observed instance ID
before cloud actions. Pending registration, draining, or deregistration blocks all other
host changes, including price eviction and interruption replacement, across
restarts. Drain progress, group identity, and individual deregistration requests
survive restarts and uncertain API responses. A preemption before the controller
requests deletion remains eligible for interruption recovery. Deletion retries
target the same host; stale runner removal after loss requires
fresh VM absence and offline, idle runner records.

A launch must register its expected runner count (eight on legacy hosts) within 10 minutes of starting.
Healthy hosts take 5 to 8 minutes. The manager stops waiting for bootstrap at
that deadline. If the controller still sees fewer than its expected count of online runners, or
observes a CPU platform below the requested minimum, it deletes the VM with its
disks and deregisters its runner records. It then records the candidate as failed
and moves on. A host whose manager already confirmed the expected online runner count is
exempt, so a runner restart later cannot delete a busy host. If the manager hangs
past the deadline, the controller keeps the host when GitHub shows the expected runner names
runners online. When a launch request times out and no VM exists, the controller
waits until the deadline before trusting that absence. Launches saved before
this deadline existed expire 10 minutes after the controller restarts. A leftover
disk that is still attached keeps cleanup pending.

Every check logs a one-line fleet summary. It lists online, busy, and offline
GitHub runners, Spot VMs grouped by status, machine type, CPU platform, and zone,
and any pending host operation with its elapsed time.

Once an hour the controller logs the fleet's spend rate from live compute and
storage prices, for example `Spend rate: 2 Spot VMs = $10.31/day ($0.43/hour)`.
It then logs reported project spend for the last 24 hours, 7 days, and 30 days
from the billing export configured by `GCP_BILLING_EXPORT_TABLE`.

Launch logs show candidate specifications and failure details for each zone.
The manager logs the first observation of an online runner and elapsed seconds
from starting VM deployment. This measures the first GitHub poll that observes
registration; it does not claim the exact provider-side registration instant.
The controller separately logs elapsed time through complete deployment,
registration, label, and storage verification. Manager diagnostics are included
as individual one-line events in the controller log.

Use `--log-file PATH` to change the event log location. The LaunchAgent preserves
this option. Existing log content is retained across restarts. To follow events:

```bash
tail -f ~/spot-ghrunner.log
```

Graceful exits and `SIGTERM` or `SIGINT` signals write shutdown events. Power loss
and `SIGKILL` cannot write a final event. The event log has no automatic rotation;
service diagnostics also remain in the LaunchAgent log.

## Repository layout

- `src/spot_runner_manager.py` is the provisioning command-line entry point.
- `src/spot_runner_autoscaler.py` is the continuous scale-out entry point.
- `src/runner_fleet.py` is the fleet status command-line entry point.
- `src/spot_finder.py` is the Spot offer search command-line entry point.
- `src/lib/` contains reusable fleet, pricing, provisioning, and autoscaling methods.
- `src/lib/runner_assets.py` embeds the startup script and runner image files.
- The runner image enables a completed-job hook that removes only
  `/work/oxidex/oxidex/target` after OxiDex jobs. It checks the repository,
  workspace, and symlinks before removal, and logs free disk bytes before and
  after. Set `OXIDEX_CLEAN_TARGET=0` in the runner container to turn it off.
  Custom `CARGO_TARGET_DIR` paths and old outputs outside this directory are
  outside this cleanup boundary.
- `tests/` contains unit tests for pricing and capacity rules.

Run the Python checks from the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m py_compile src/*.py src/lib/*.py
```

## Safety and workflow routing

The runner group is limited to the OxiDex repository. Keep untrusted pull
request code off persistent self-hosted runners. Route macOS signing and release
jobs to macOS hosts; the Spot VMs in this fleet run Linux. Before changing CI to use
`runs-on: [self-hosted, linux, x64, spot]`, keep at least 25 online runners
across both pools for 10 minutes, and verify that runner filesystem checks pass.
Docker build jobs should use
`runs-on: [self-hosted, linux, x64, docker]`.

## Tier deployment and cache lifetime

Run `python3 src/spot_runner_autoscaler.py --scaling-mode tiered --migrate-oversized`
with the existing organization, repository list, state and log arguments. Add
`--dry-run --once` first to inspect evidence and qualifying offers. The launch
manager rechecks the exact shape and all-disk cost before creation; receipts carry
the tier, expected runner count, memory envelope and verified price breakdown.

The existing persistent data block device holds Docker storage and the dedicated
`remote-build` Cargo downloads/targets. Worktree target reuse avoids deleting
cache on each build; source synchronization must be serialized for a stable
worktree identifier. This disk persists between builds on that VM. Cache is lost
when the VM and its auto-deleted disks are retired or interrupted; no cross-VM
cache survival is claimed. A separate additional cache disk would need to fit
the same all-disk price cap.

## Dedicated OxiDex builders

For the runner-free Rust 1.99 Spot host, complete startup payload and single-command launch, see [dedicated builder provisioning](docs/dedicated-builders.md). Dedicated builder capacity and cache/draining are separate from GitHub runner hosts.
