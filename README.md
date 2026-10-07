# Spot runner fleet and dedicated builders

Provision and operate persistent GitHub Actions runners and dedicated Rust build
VMs on GCP (Google's cloud platform) Spot instances. The repository includes a
price finder, a deployment manager, a fleet controller, and remote build clients.

The controller manages runner capacity for a GitHub organization. Dedicated
builders are separate VMs with no GitHub runner registration. SSH terminates on
the host; app builds run as an unprivileged user in a resource-limited
Docker container with persistent Cargo and target caches.

This guide describes the source on `main`. Merging source does not update a
running controller, container, or VM. Inspect the deployed configuration and
actual tool versions before routing work to a host.

## Start a dedicated builder

Use the [dedicated builder setup](docs/dedicated-builders.md): fill the deployment
identity and public SSH mapping, render the startup payload, and run the generated
launch command once. Startup installs the toolchain and restricted build launcher.
Configure the verified endpoint in OxiDex, then use `just build`.

For the simplification target and remaining work, see
[Simplify remote builds](docs/simplification.md).

## Choose a workflow

| Goal | Entry point | Creates or changes resources? |
| --- | --- | --- |
| Find candidate Spot offers | `src/spot_finder.py` | No |
| Inspect runners and VMs | `src/runner_fleet.py` | No |
| Preview or provision runner hosts | `src/spot_runner_manager.py` | Only with `--apply` |
| Plan or manage runner capacity | `src/spot_runner_autoscaler.py` | Yes, except a tiered `--dry-run` |
| Render a dedicated builder launch | `src/render_builder_startup.py` | Writes local files; the generated launch command creates a VM |
| Build on an explicitly selected legacy host | `src/remote_rust_build.py` | Transfers source and starts a remote build |
| Install Cargo client commands | `src/install_remote_commands.py` | Writes local executable shims |

Keep the distinction between registered listeners and active jobs. Twenty
registered runners do not mean that twenty jobs can each use 32 GiB at once.
Admission reserves memory before starting work; the physical host also needs
space for its operating system, services, and idle listeners.

## Prerequisites

On the operator machine, install:

- Python 3.11 or later. The Python entry points use the standard library.
- Git, the GCP command-line tool (`gcloud`), and the GitHub command-line tool (`gh`).
- SSH and file transfer tools for remote builds.
- Vale for documentation checks. See [Check documentation](#check-documentation).

Provisioning requires access to Compute Engine, GCP billing catalog prices,
regional quotas, disks, instance metadata, and the selected network. Resource
monitoring also requires access to GCP monitoring. Enable the required APIs in
your project and grant the operator the corresponding permissions.

Runner deployment requires GitHub organization runner-management access. Job
recovery also requires Actions read/write and Checks read access to each tracked
repository. The manager requires a selected-repository runner group with access
to `ORG/oxidex`, where `ORG` is the configured organization. These scripts are
built around OxiDex; changing example repository names does not change that
verification contract. Replace project, region, and zone values for your
environment. See [Runner-group restrictions](#runner-group-restrictions) before
changing organization or group settings.

Dedicated builder rendering does not require a GitHub runner token. It requires
an approved public SSH identity mapping for its uploader account.

### Runner-group restrictions

Controller-created runner hosts currently use group ID 10: the controller does
not pass a configurable group ID to the deployment manager, and tier evidence
also queries group 10. Before enabling automatic launches, verify that group 10
exists in the selected organization and permits access to `ORG/oxidex`.

The manual manager accepts `--runner-group-id` for another qualifying group, but
that option does not update controller launches or tier evidence. Supporting a
different automatic group requires a reviewed source change. The controller's
`--job-repository` option changes job tracking. Runner placement and the
manager's required repository stay unchanged.

Inspect the required group without changing it:

```bash
gh api orgs/swack-tools/actions/runner-groups/10
```

### Clone and authenticate

```bash
git clone https://github.com/swack-tools/spot-github-runners.git
cd spot-github-runners

gcloud auth login
gcloud config set project YOUR_PROJECT

gh auth login
gh auth status
```

For runner operations, copy the credential template:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set a renewable `GH_TOKEN` with the required permissions. The file
is ignored by Git. Do not use a short-lived runner registration token as the
renewable credential: the manager obtains a registration token during deployment
and removes its temporary startup metadata after verification.

Optional settings:

| Setting | Purpose |
| --- | --- |
| `GOOGLE_CLOUD_API_KEY` | GCP billing catalog lookup |
| `GCP_BILLING_EXPORT_TABLE` | Project spend from a billing export, in `PROJECT.DATASET.TABLE` form |

The billing export also requires the `bq` command-line tool and permission to query its table.
Price queries use the active `gcloud` identity. The controller's
`--github-user ACCOUNT` option selects an existing `gh` account when several
accounts are configured; it does not enroll a new identity.

### Check command options

Run these commands from the repository root:

```bash
python3 src/spot_finder.py --help
python3 src/runner_fleet.py --help
python3 src/spot_runner_manager.py --help
python3 src/spot_runner_autoscaler.py --help
python3 src/render_builder_startup.py --help
python3 src/remote_rust_build.py --help
```

## Find and inspect capacity

Search for offers without launching resources:

```bash
python3 src/spot_finder.py \
  --project YOUR_PROJECT \
  --min-vcpu 16 \
  --min-ram 32 \
  --top 10
```

Search results are candidates, not reservations. Confirm zone availability,
compatible storage, quota, and the current total cost before provisioning.
Compute prices can change after launch. Attached boot and data disks also cost
money; an hourly compute limit alone is not a total daily budget.

Inspect the fleet:

```bash
python3 src/runner_fleet.py \
  --org swack-tools \
  --project YOUR_PROJECT \
  --label spot \
  --minimum-runners 25 \
  --runners-per-vm 8
```

`--label` filters runner counts. Use `--vm-name-prefix` to restrict VM names.
These counts do not establish workload readiness, free memory, or an exclusive
build reservation.

## Provision runner hosts

### Preview a runner deployment

The manager checks the requested shape, compatible storage, price, and quota.
Without `--apply`, it prints a plan instead of creating the VM.

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
  --disk-size-gb 100 \
  --runner-group-id 10 \
  --runner-label spot \
  --runner-prefix oxidex \
  --instance-name oxidex-runners-example
```

The example uses controller-compatible group 10. The hourly and daily limits
are acceptance limits, not quoted prices. Price must be strictly
below the applicable cap; an equal price does not qualify.

### Apply the reviewed plan

Run the same command with `--apply` after verifying the plan. Bootstrap installs
Docker, configures attached storage, builds the runner image, and registers the
expected runner names. The manager waits for the deployment-specific completion
marker and checks registration, storage placement, and container isolation.

Runner VMs use a 40-GiB boot disk and, by default, a 100-GiB data disk. Supported
newer families use Hyperdisk Balanced with NVMe; older families have different
compatible Persistent Disk options. `--disk-type` selects a compatible data disk,
and `--cheapest-storage` compares supported storage prices. Runner disks use
auto-delete, so deleting or losing a Spot VM can also remove its caches.

`--existing-instance` is an adoption and bootstrap path, not a harmless probe.
It can restart an existing host. Drain active work and verify the adoption scope
before using it. Do not use the fresh dedicated builder bootstrap on an occupied
runner or benchmark host.

### Understand runner limits

For manual deployments without `--tier`, automatic sizing uses:

```text
runner_count = host_vCPU // 4
runner_memory_GiB = (host_memory_GiB - 8) // runner_count
```

For example, a 32-vCPU, 128-GiB VM gets eight runners with a 15-GiB memory cap.
Automatic sizing uses equal CPU weights rather than a hard per-runner CPU quota.
A job can use otherwise idle host cores. `--fixed-runner-sizing` instead applies
`--runner-count`, `--runner-cpu`, and `--runner-memory-gb`, with checks against
host capacity.

Tier deployments override manual sizing. `--tier-slots` controls the registered
listener count; `--tier-job-memory-gib` controls the job cap and reservation.
The current command-line default is 15 GiB. That default is not proof that a heavy workload
fits: use actual job measurements and out-of-memory (OOM) evidence.

The separate `spot-heavy32` route requires a verified per-runner policy with an
8-vCPU quota, 32-GiB memory cap, and no extra swap. A label alone does not enforce
those limits. Verify admission policy, actual container limits, and controller
route protection before publishing the label. The controller protects both
planned and live heavy routes from incompatible retirement or replacement.

## Operate the fleet controller

The controller combines provider inventory, GitHub runner and job state, pricing,
resource observations, and persisted operations. It changes one host at a time.
Registration, draining, cleanup, or an uncertain prior operation blocks another
host change. State survives process restarts.

### Select a scaling mode

| Mode | Evidence | Behavior |
| --- | --- | --- |
| `tiered` | CPU, memory, queue delay, job timings, and OOM evidence | Chooses host shapes and listener density; missing evidence holds optimization |
| `resource` | Host CPU and memory observations | Uniform-host optimization; this is the command-line default |
| `legacy` | Runner occupancy | Retains the older occupancy policy |

Use tiered mode explicitly. Resource and legacy price eviction can remove an
over-budget VM even while jobs are active. Tiered replacement uses drain and
readiness guards. Do not treat the modes as interchangeable.

### Preview one tiered check

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

Tiered `--dry-run` leaves provider resources and controller state unchanged.
`--once` without `--dry-run` can provision or retire resources.
`--no-remote-builder` keeps new runner hosts separate from remote builders.
Enabling the shared builder option uses the older shared-host builder path;
it does not create a dedicated `builder-` VM.

### Run continuously

Remove `--dry-run --once` from the reviewed tiered command. Set explicit paths
when you want a separate operational state and log:

```bash
python3 src/spot_runner_autoscaler.py \
  --project YOUR_PROJECT --org swack-tools \
  --github-user YOUR_GITHUB_ACCOUNT \
  --scaling-mode tiered --tier-job-memory-gib 32 \
  --no-remote-builder --maximum-hosts 5 \
  --state-file "$HOME/oxidex-ops/runner-state.json" \
  --log-file "$HOME/oxidex-ops/runner-events.log"
```

Use an existing writable parent directory for these paths. Keep one controller
for a state file. Its exclusive state lock prevents concurrent controllers from
submitting the same operation or job-recovery request.

The resource and tiered modes require a 300-second polling interval. Tiered
policy uses complete, fresh observations: two hot windows can grow capacity;
six low windows plus projected peak demand are required for shrinking. Host OOM
and container OOM have different dispositions. Exit status 137 alone does not
establish either cause.

Current tier definitions are:

| Tier | vCPU | RAM, GiB | Initial / maximum listeners | Total daily cap, USD |
| --- | ---: | ---: | ---: | ---: |
| `small` | 4 | 16 | 4 / 8 | 0.60 |
| `starting` | 8 | 16 | 8 / 8 | 1.25 |
| `expanded` | 8 | 32 | 8 / 16 | 1.25 |
| `large` | 16 | 64 | 20 / 20 | 3.00 |
| `bulky` | 32 | 128 | 16 / 20 | 4.50 |

These policy definitions do not quote current offers. A shape must also fit
the configured job reservation. The topology limits are five hosts,
80 vCPU, and 100 logical slots, including replacement overlap. An unaffordable,
unavailable, or incompatible target holds the operation instead of authorizing
an arbitrary fallback. `--migrate-oversized` enables gradual migration of an
existing owned fleet.

Memory telemetry requires the VM service account to have
`roles/monitoring.metricWriter` and the Monitoring write scope. Runner
provisioning with resource monitoring installs the metrics-only Ops Agent;
Compute Engine provides the host CPU metric.

### Run as a macOS LaunchAgent

Add `--install-launch-agent` to the chosen controller command. This writes and
bootstraps:

```text
~/Library/LaunchAgents/com.swacktools.spot-runner-autoscaler.plist
```

The agent starts at login and uses `KeepAlive`. Its arguments retain mode, memory
reservation, builder selection, account, state, log, and tracked repositories.
The plist points to this checkout and interpreter; keep both paths available.
Installing source or changing a command-line default does not update an existing plist.

Inspect the loaded service:

```bash
launchctl print "gui/$(id -u)/com.swacktools.spot-runner-autoscaler"
```

Stop it with the exact installed plist:

```bash
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.swacktools.spot-runner-autoscaler.plist"
```

Stopping the controller does not stop runner jobs or drain a VM. Before replacing
a running controller, preserve its state, verify its actual process and arguments,
and wait for its current operation to finish. On Linux, run the foreground
controller under your service manager; this repository's installer creates a
macOS LaunchAgent.

### Recover interrupted jobs and hosts

The controller tracks `swack-tools/oxidex` by default. Repeat
`--job-repository OWNER/REPO` to select other repositories. It records the job,
runner, workflow, and provider identities before a host disappears.

A retry requires a confirmed runner-loss failure and a completed workflow.
Successful jobs, user cancellations, ordinary code failures, and intentional
retirement do not qualify. Requests are persisted before submission. An uncertain
response is checked for a newer attempt instead of blindly submitting again.

Lost-host cleanup verifies the saved VM and disk identities, waits for recorded
resources to disappear, and removes stale offline, idle runner registrations.
Attached, recreated, foreign, or unverifiable disks keep recovery pending. The
controller does not treat incomplete inventory as proof of absence.

Tiered replacements must establish target registration and health before draining
the source. Repository scheduling is isolated during runner drain, while already
assigned jobs finish. Dedicated builder drain follows active builds, admission
leases, source/cache locks, and export activity; it does not use GitHub runner
idleness as proof that a build is finished.

## Provision a dedicated Rust builder

The dedicated renderer fixes the host to `n4-highcpu-16`: 16 vCPU and 32 GiB RAM.
It registers zero GitHub runners and permits one aggregate builder reservation
of 16 vCPU and 28 GiB without extra swap. Host services share the physical CPU;
the remaining memory provides host overhead.

Two Rust compilations can be launched inside that one aggregate container, but
they share its CPU and memory limits. They are not two independent 28-GiB
reservations. Measure the actual concurrent workload before claiming that it fits.

### Provide an approved uploader identity

Obtain an operator-approved `authorized-uploader.json` with these exact fields:

| Field | Required value or meaning |
| --- | --- |
| `schema` | `root-approved-ssh-mapping-v1` |
| `principal` | Approved operator identity |
| `username` | Restricted uploader account, for example `oxidex-uploader` |
| `uid` | Approved UID; UID 1001 is reserved for the container build user |
| `identity_receipt_sha256` | SHA-256 of the actual approved identity evidence |
| `public_key` | One approved Ed25519 public SSH key |
| `public_key_sha256` | SHA-256 of that exact public-key string |

Do not substitute a test key or invented receipt for deployment evidence. The
renderer validates the mapping and its digest. Startup enrolls the account with
restricted build-group access and an allowlisted launcher; it does not grant
Docker, general sudo, or an SSH server inside the build container.

### Render startup and the launch command

Store deployment files outside the Git checkout:

```bash
export OXIDEX_OPS_DIR="${OXIDEX_OPS_DIR:-$HOME/oxidex-ops}"
mkdir -p "$OXIDEX_OPS_DIR/launches"
```

Create the builder configuration using the approved mapping:

```bash
PYTHONPATH=src python3 - <<'PY'
import json
import os
from pathlib import Path
from lib.builder_access import approved_mapping, canonical_sha

root = Path(os.environ['OXIDEX_OPS_DIR'])
mapping = approved_mapping(json.loads((root / 'authorized-uploader.json').read_text()))
config = {
    'role': 'builder', 'cpu': 16, 'ram': 32, 'runner_count': 0,
    'builder_cpus': 16, 'builder_memory_gib': 28,
    'disk_size_gb': 100, 'disk_type': 'hyperdisk-balanced',
    'disk_device_name': 'builder-cache',
    'data_disk_iops': 3000, 'data_disk_throughput': 140,
    'boot_disk_size_gb': 40, 'boot_disk_iops': 3000,
    'boot_disk_throughput': 140,
    'instance_name': 'builder-oxidex-example',
    'project': 'YOUR_PROJECT', 'zone': 'asia-east2-a',
    'authorized_uploader': mapping,
    'authorized_uploader_sha256': canonical_sha(mapping),
}
(root / 'builder-config.json').write_text(json.dumps(config, indent=2) + '\n')
PY
```

Replace `YOUR_PROJECT` with your lowercase GCP project ID before rendering.
Builder names must start with `builder-`. Cache performance can use either
3,000 IOPS / 140 MiB/s or the reviewed 4,000 IOPS / 250 MiB/s pair. Price both boot
and cache storage, including provisioned performance, before choosing a pair.

```bash
python3 src/render_builder_startup.py \
  --config "$OXIDEX_OPS_DIR/builder-config.json" \
  --deployment-id builder-example-001 \
  --output-directory "$OXIDEX_OPS_DIR/launches/builder-example-001"
```

The output directory must not exist. The renderer produces:

- `startup.sh`: self-contained GCE startup metadata.
- `cloud-config.yaml`: equivalent cloud-init payload for a cloud-init environment.
- `launch-command.sh`: one `gcloud compute instances create` command using the
  generated `startup.sh`.

The generated GCE command uses Debian 13, Spot provisioning, delete-on-preemption,
a 40-GiB Hyperdisk Balanced boot disk, and a 100-GiB persistent cache disk with
NVMe. It disables the VM service account and project SSH keys; uploader access
comes from the approved mapping.

The renderer validates configuration. Check current pricing and quota separately. After
confirming the selected zone, quota, network access, and total price, launch:

```bash
bash "$OXIDEX_OPS_DIR/launches/builder-example-001/launch-command.sh"
```

This command creates billable resources. The dedicated cache disk has
`auto-delete=no`; deletion of the builder VM does not delete that cache disk.
Record its provider identity and manage its retention separately. Runner data
disks use a different auto-delete policy.

### What startup installs and verifies

Startup first verifies a fresh host and empty workload state. It mounts storage
before configuring Docker and containerd, installs canonical launcher/admission
assets, builds the image, records its immutable image ID, and runs an admitted
tool-only probe with the actual container limits.

The standalone image includes:

- Rust 1.99.0, rustfmt, clippy, and Linux musl and Darwin cross targets.
- Build tools, Clang, CMake, pkg-config, OpenSSL, and archive development libraries.
- Git, SSH, GnuPG, Perl, Python 3.11, rsync, GNU time, and compression tools.
- `just`, cargo-nextest, cbindgen 0.29.2, Zig 0.15.2, cargo-zigbuild 0.23.4,
  `uv`, and LLVM tools.

Rust is pinned to compiler commit
`b940084d7eb6a299eb4bfeb8e34901bc051e7ac4`. Some auxiliary tool installs resolve
versions during the image build; use the actual tool receipt and immutable image
ID rather than assuming every dependency has a fixed version.

The older shared-host image in `builder/Dockerfile` still pins Rust 1.97.1.
Ordinary runner images do not establish the standalone builder's toolchain:
workflows select their Rust version separately. Provisioning a new standalone
builder does not upgrade existing runner containers.

Readiness records are:

```text
/var/lib/oxidex-builder-bootstrap.complete
/var/lib/oxidex-builder-readiness.json
```

The readiness JSON joins the deployment, boot, source hashes, image/container,
actual cgroup limits, tool results, and mounted cache identity. It sets
`workload_qualified` to false: tool installation is not compilation, cache-reuse,
concurrency, or full qualification proof. Run the intended workload and retain
its result before declaring the host ready for that workload.

## Run and inspect remote builds

The host launcher is `/usr/local/bin/oxidex-remote-build`; its configuration is
`/etc/oxidex-remote-build.json`. It checks project identifiers, allowed commands,
image identity, mounts, limits, and the admission ledger. Containers run as
UID/GID 1001 without a Docker socket, added capabilities, or privilege escalation.

Use the approved uploader account for source transfer. A control account with
sudo access is not necessarily allowed to traverse the source directories.
Do not change directory permissions to work around an identity mismatch.

On a host with an already prepared source project, an example build command is:

```bash
sudo -n /usr/local/bin/oxidex-remote-build oxidex-example \
  cargo build --release --locked --bin oxidex
```

`prepare` creates the project directories and assigns source access to the
uploader. It does not erase existing source or stop a build. `cleanup` removes
owned source content after cache-lock and container-mount checks, retaining
targets. Transfer source completely before starting the build; do not prepare or
synchronize a project another build is using. Cancellation and reconciliation
checks in the admission and Docker adapter paths depend on exact run and
container identities; they do not authorize a broad container stop.

### Use the appropriate client

OxiDex's repository-owned `just` recipes and worker registry are maintained in
the OxiDex repository. Configure that client for the approved dedicated host,
uploader identity, source, image, and cache before enabling its route. This
repository does not automatically update OxiDex's workflow or registry.

The older client can build on an explicitly selected compatible host:

```bash
python3 src/remote_rust_build.py \
  --source "$HOME/git/oxidex" \
  --instance YOUR_INSTANCE --zone YOUR_ZONE --project YOUR_PROJECT \
  --worktree-id oxidex-example \
  --profile release \
  --artifact-dir "$HOME/git/oxidex/target/remote-linux/release" \
  --evidence-dir "$HOME/oxidex-ops/evidence/remote-build-example"
```

Use a new evidence directory for each attempt to preserve earlier receipts.
The client snapshots tracked files, transfers and verifies the archive, runs the build,
checks the artifact checksum, and records source and timing evidence.
`--artifact-dir` downloads the verified `oxidex` binary into that directory;
without it, the client retains a receipt without downloading the binary. It uses
`gcloud compute ssh/scp`; verify that its selected account and host access match
the target. Its automatic selector considers legacy `oxidex-runners-*` and
`oxidex-buildbench-*` hosts, not the new `builder-` registry.

Install the optional Cargo shims:

```bash
python3 src/install_remote_commands.py
export PATH="$HOME/.cargo/bin:$PATH"
```

They execute scripts from this checkout; keep the checkout in place. The command
names are `cargo oxidex-remote` and `cargo oxidex-auto`. The configured OxiDex
checkout supplies remote instance, zone, project, and worktree settings through
its environment or `.cargo/config.toml`. Use `--profile debug` or
`--profile release` with the explicit client. Automatic selection defaults to
three attempts; compiler or header errors stop instead of moving to another host.
The legacy automatic helper also calls `cargo remote-build`; that separate alias
or command must already exist in the configured OxiDex checkout. The installer
creates the two `oxidex-*` shims, not that alias. Do not assume installing the
shims alone configures automatic selection or the dedicated-builder registry.

Linux artifacts from these clients do not run natively on macOS. Cross targets
and SDK-free framework interfaces do not replace native artifact validation.
Full ExifTool comparison needs its own pinned oracle, fixtures, and qualification
workflow; the standalone Dockerfile does not install the full oracle corpus.

### Qualification source admission

Projects with the dedicated qualification contract authenticate signed source,
bundle, and run identities before executable import. A bounded importer and
supervisor retain protective locks until descendants are reaped. Executable
source is sealed separately from writable measurement Git metadata, and the
adapter validates the required mounts and allowed oracle fetch.

These source controls do not certify an installed deployment. Production
qualification still needs actual startup, authority, mount, process-lifetime,
disk, network, and workload evidence. Do not mark a worker qualified from a
successful source-only test suite or a bootstrap tool probe.

## Storage and cache lifecycle

| Data | Host path |
| --- | --- |
| Docker images and cache | `/mnt/runner-data/docker` |
| containerd layers | `/mnt/runner-data/containerd` |
| Runner state | `/mnt/runner-data/instances/<runner>` |
| Runner workspaces | `/mnt/runner-data/work/<runner>` |
| Build source | `/mnt/runner-data/remote-build/sources/<project>` |
| Cargo downloads | `/mnt/runner-data/remote-build/cargo` |
| Per-project targets | `/mnt/runner-data/remote-build/targets/<project>` |
| Exported artifacts | `/mnt/runner-data/remote-build/exports/<run>` |

Mounting only Docker's data root is insufficient if containerd still stores
layers on the boot disk. Bootstrap configures both on attached storage. Caches
persist across builds on the same surviving disk; retention is not a backup or
cross-VM cache-reuse proof. Do not format, delete, or detach an existing cache
disk to make a failed bootstrap retry succeed.

Builder drains deny new builds while active builds, units, leases, and cache or
upload/export locks finish. Preserve foreign or uncertain work and record why
drain is blocked. Runner drain and builder drain have different completion tests.

## Logs and troubleshooting

Controller defaults:

```text
Event log: ~/spot-ghrunner.log
State: ~/Library/Application Support/Spot Runner Autoscaler/state.json
Agent diagnostics: ~/Library/Logs/spot-runner-autoscaler.log
```

Follow the event log:

```bash
tail -f "$HOME/spot-ghrunner.log"
```

Fleet summaries include VM shape, CPU platform, zone, runner counts, and pending
operations. Hourly spend events estimate compute and attached storage from
current prices; billing-export totals are reported separately. The log has no
automatic rotation. Configure retention without deleting active state or evidence.

Inspect the builder bootstrap journal and readiness files through an
approved administrative connection:

```bash
sudo journalctl -u google-startup-scripts.service --no-pager
sudo cat /var/lib/oxidex-builder-readiness.json
sudo cat /etc/oxidex-remote-build.json
sudo cat /var/lib/gha-admission/leases.json
sudo docker ps -a --no-trunc
```

Inspect actual container limits:

```bash
sudo docker inspect --format \
  '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' \
  CONTAINER_ID
```

Memory values are bytes; `NanoCpus` is in billionths of a CPU. A zero quota field
requires checking the other CPU quota fields or cgroup files; it is not proof of
an enforced 8-vCPU cap. Compare the result with admission policy and the actual
cgroup `cpu.max`, `memory.max`, and `memory.swap.max` values.

| Symptom | Check and next action |
| --- | --- |
| No affordable offer | Confirm complete current compute and disk prices, zone compatibility, and quota; retain the hold reason |
| No memory observations | Check service-account role/scope, Ops Agent status, timestamps, and boot identity |
| Registration timeout | Inspect the exact deployment marker, startup log, token scope, group, and expected runner names |
| Admission denied or exit 75 | Check draining state, owned leases, limits, and full container inventory; wait for active work |
| Transfer permission denied | Verify the approved uploader identity and group; do not broaden permissions |
| Exit 137 | Join kernel/cgroup OOM evidence to the actual job interval before assigning a cause |
| Compiler/header failure | Repair source or toolchain inputs; do not retry on another VM as a capacity workaround |
| Replacement stuck | Inspect persisted operation and exact disk/runner identities; do not clear state to bypass a hold |

For runner job cleanup, the completed-job hook removes only the validated OxiDex
workspace target directory. `OXIDEX_CLEAN_TARGET=0` disables that hook. Custom
target directories and dedicated builder targets follow their own lifecycle.
Keep untrusted external pull requests off persistent self-hosted runners. Route
native macOS work to native hosts; see the separate [M4 Air guide](m4air/README.md).

## Repository layout

| Path | Responsibility |
| --- | --- |
| `src/lib/runner_manager.py` | Runner provisioning, sizing, bootstrap, and registration |
| `src/lib/spot_runner_autoscaler.py` | Controller entry, persisted operations, and service installation |
| `src/lib/tier_controller.py`, `tier_policy.py` | Tier decisions and migration coordination |
| `src/lib/runner_drain.py`, `vm_recovery.py`, `job_recovery.py` | Drain, loss cleanup, and eligible job retries |
| `src/lib/spot_pricing.py`, `billing_catalog.py`, `storage_selection.py` | Compute/storage pricing and compatible disk selection |
| `src/lib/host_admission.py` | Work reservations, leases and actual-owner checks |
| `src/lib/builder_startup.py`, `builder_access.py` | Dedicated startup, identity enrollment, and readiness |
| `cloud-init/` | Canonical runner and dedicated-builder startup templates |
| `runner/` | Runner image and hooks |
| `builder/standalone/` | Standalone Rust 1.99 image, entry point, and probe |
| `builder/dedicated/` | Dedicated host launcher and Docker admission adapter |
| `src/lib/qualification_trust/` | Source authentication, sealed import, and qualification mounts |
| `tests/` | Unit and lifecycle controls |

## Development and validation

Use an isolated branch based on current `main`, keep one pull request active at a
time, and preserve dirty checkouts. Run Cargo compilation and full Python suites
on an eligible remote Spot worker through the reviewed build route. Local work
is limited to edits, Git, formatting, bounded metadata/control checks, and prose
lint. Do not fall back to local compilation when the remote route is unavailable.

Inside the admitted remote test container, run:

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

Retain exact source, VM, image, exit, cleanup, and lease evidence. Source tests,
CI success, merge, deployment, and actual workload acceptance are separate results.

### Check documentation

Documentation follows the [Google developer documentation style guide](https://developers.google.com/style).
Use sentence case headings, active voice, direct instructions, and explicit
prerequisites. Preserve command names and technical meaning when fixing prose.

Install Vale using its [installation guide](https://docs.vale.sh/topics/installation),
then synchronize and run the configured Google rules:

```bash
vale sync
vale README.md
```

A clean Vale result covers the configured rules, not every editorial requirement
in the Google guide. Check command examples against command-line help and source before
submitting the documentation PR.
