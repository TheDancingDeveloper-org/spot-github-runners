# Dedicated remote builders

Dedicated hosts have `builder-` names and register no GitHub runners. The reviewed
shape is `n4-highcpu-16`: 16 vCPU, 32 GiB RAM, one unprivileged Docker builder
limited to an aggregate 16 CPUs and 28 GiB, with no additional swap. Two OxiDex
worktrees can compile concurrently inside that container, sharing its limits and
Cargo downloads while using separate target directories.

The image installs Rust 1.99.0 (full compiler commit
`b940084d7eb6a299eb4bfeb8e34901bc051e7ac4`), rustfmt, clippy, Zig 0.15.2,
cargo-zigbuild 0.23.4, cbindgen 0.29.2, Just, nextest, Python, uv, Perl, rsync,
LLVM, and native build dependencies. App builds run as UID/GID 1001 and
receive no Docker socket. SSH lands on the host; the uploader can sudo only the
fixed `/usr/local/bin/oxidex-remote-build` launcher.

## Render once, launch with one command

Prepare the private JSON configuration below and a separate explicitly approved
public uploader mapping file. Do not use a private SSH key. The mapping must
contain exactly `schema` (`root-approved-ssh-mapping-v1`),
`principal`, `username`, numeric `uid` (2000 is the deployment example),
`identity_receipt_sha256`, the complete single-line Ed25519 `public_key`, and
`public_key_sha256` (SHA-256 of that complete key string). The renderer validates
the selected mapping and computes its canonical checksum. Selecting the file
does not approve a new account or key; use the mapping you already approved.
Identity receipts and key mappings are deployment inputs, not repository defaults.
The renderer supplies the fixed host, container, and baseline disk settings.
Explicit settings still pass through the same strict policy checks.

```json
{
  "instance_name": "builder-your-unique-name",
  "project": "your-project",
  "zone": "asia-east2-a"
}
```

Render locally with the approved mapping file:

```bash
python3 src/render_builder_startup.py --config /private/builder-config.json \
  --uploader-mapping /private/approved-uploader.json \
  --output-directory /private/builder-payload
bash /private/builder-payload/launch-command.sh
```

The deployment identity defaults to `instance_name` in the configuration. Use
`--deployment-id` only when you need a distinct identity in the startup receipt.
Existing configurations can continue embedding `authorized_uploader` and its
canonical `authorized_uploader_sha256` instead. The renderer refuses a mapping
file combined with either embedded field, so it cannot silently replace an identity.

The second command executes **one `gcloud compute instances create` invocation**.
It passes the complete embedded payload as `startup-script` metadata. The Debian
13 GCE image runs this startup mechanism; a separate cloud-init agent is not
required. `cloud-config.yaml` contains the equivalent payload for images with a
cloud-init agent, but that alternative execution path has not been live-tested.

Startup verifies fresh-host identity and the absence of existing runners/work,
installs Docker, mounts storage before starting Docker/containerd, builds, and pins
the immutable image, starts aggregate admission, checks actual tool/cgroup/cache
observations, enrolls the approved restricted uploader, and writes a completion
marker. No manual SSH installation or repository checkout is needed on the host.
Rendering refuses an existing output directory; fresh-host startup must not be
replayed to upgrade an occupied VM. Replacements need a new name and matching
rendered payload.

## Configure the build client

The renderer also writes `cargo-remote-env.toml` with the project, instance name,
zone, and uploader username. Merge those entries into the existing `[env]` table
in OxiDex's `.cargo/config.toml`; do not replace unrelated settings.

After startup, add the actual numeric GCE instance ID and the absolute paths to
your private SSH key and trusted known-hosts file using the commented entries.
These values cannot be inferred from a launch plan. The snippet contains no key
material and does not claim the new VM is ready. OxiDex verifies the identity,
SSH trust, installed launcher, and resource admission before executing work.
Then use `just build`; use `just build-local` for an explicit local build.

## Storage, access, and draining

The launch command uses a 40 GiB Hyperdisk Balanced boot disk and a 100 GiB
Hyperdisk Balanced cache disk through NVMe, with 3,000 IOPS and 140 MiB/s baseline
provisioning. The renderer also accepts the explicitly reviewed 4,000 IOPS /
250 MiB/s pair; price that pair before launching. Compute and both disks were
quoted at $2.573746849/day in asia-east2-a on 2026-10-05; this is a historical
quote, not a promise of future pricing or Spot availability.

Cargo downloads and per-project targets persist on `/mnt/runner-data`. The cache
disk has `auto-delete=no`, so it survives instance deletion and continues to incur
storage charges. Recovery must verify the disk and ownership; startup refuses to
format an existing unidentified filesystem. Use direct authenticated SSH/SCP with
the established host key for `oxidex-uploader`. `gcloud compute ssh/scp` can enroll
the account into broader guest permissions and is not the restricted uploader
transport.

The launcher prepares a project, executes the allowed build command under one
aggregate lease/cache lock, exports artifacts, and cleans only owned source.
Draining refuses new builds and lets actual active builds, leases and cache/upload
locks finish before replacement. Persistent Cargo downloads and targets are not
deleted after each build. GitHub runner drains are separate from this lifecycle.

## Observed validation

On 2026-10-05, the dedicated host completed concurrent cold Linux native command-line
builds on OxiDex source `429d6f26ec08e703cba2e5a3712b098998d6778d`: debug
45.15 seconds and release 189.80 seconds, total 190.16 seconds, aggregate charged
memory peak 7.13 GiB, zero OOM kills, and zero throttled CPU periods. Both artifacts
were independently hashed. The ordinary `just build-debug-remote` recipe also
passed source synchronization, compilation, verified download, and source cleanup
with targets retained on client `01c5b1fcbb30e2938de244addb979e127288a7c8`.

A later monolithic expression-catalog compiler exceeded the 28 GiB cap; its
chunked-function repair passed the same 608 expressions and 16,821-probe
population at 6.21 GiB with zero OOM events. These are exact workload proofs,
not evidence that all workloads fit. The initial launch required
Debian service/sudo parser and target-parent fixes included in the current
payload. A second untouched launch of that final payload was not performed.

The canceled 24-hour runner monitoring study is not part of this provisioning.

## Runtime updates

Use the standalone `src/lib/builder_runtime_update.py` helper through the existing
authenticated root control path for an occupied dedicated builder. Its default
mode checks a reviewed plan without writing files. Capture mode records the
actual VM, boot, runtime files, and admission process; plans expire after
120 seconds. It does not provision a VM or rebuild an image.

The maintainer supplies a root-owned asset JSON file containing exactly the
fifteen runtime targets and their reviewed bytes and hashes. Capture a fresh
plan after source review and builder handback:

```bash
python3 -I -B builder_runtime_update.py --capture-assets assets.json \
  --assets-sha256 ASSET_FILE_SHA256 --source-main REVIEWED_SOURCE_COMMIT
```

Save the returned plan as a private root-owned file and record its SHA-256.
Check it before applying:

```bash
python3 -I -B builder_runtime_update.py --plan plan.json \
  --plan-sha256 PLAN_FILE_SHA256
python3 -I -B builder_runtime_update.py --plan plan.json \
  --plan-sha256 PLAN_FILE_SHA256 --apply
```

Applying takes the existing build lock, refuses active builds and uploads,
creates an owned drain, backs up old files, and replaces only the fixed runtime
file set. It restarts admission alone. It preserves the image, build limits,
source worktrees, and caches. A partial failure retains the drain and backups
for explicit recovery. Success verifies installed bytes and the new admission
process while retaining the owned drain.

Coordinate the exact OxiDex launcher pin before this update. The helper does not
change clients or declare qualification ready. Run the reviewed Linux gate and
ordinary build checks before routing qualification work to the updated host. A successful installation
returns `INSTALLED_DRAINED`; it keeps the route off until an independently
verified client-pin handoff authorizes removal of the exact owned drain. The
updater never reopens the route automatically.

The update also installs a fixed admission-service boot guard and a persistent
pending marker. A private permit in `/run` allows only the current maintenance
boot to restart admission while that marker exists. Reboot removes the permit,
so admission stays off until explicit recovery or the verified handoff. The
pending marker and volatile drain are separate from build leases.

Preparation only writes private backups and leaves the live runtime unchanged.
The first live cutover removes launcher execution permission and persists that
mode before runtime replacement. Both the previous and installed launcher stay
readable but non-executable (`0644`) through partial updates and reboot. Restoring
`0755` is part of the separately verified client-pin handoff, not automatic
installation success. Administrative controlled proofs use the authenticated
root control path; the uploader cannot bypass this hold.


### Finish an installed update

After controlled source checks and coordination of the exact client launcher
pin, capture a new observation with the original installation nonce:

```sh
python3 -I -B builder_runtime_update.py --capture-assets assets.json \
  --assets-sha256 "$ASSETS_SHA256" --source-main "$SOURCE_MAIN" \
  --finish-nonce "$INSTALLED_NONCE" \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256" > finish-plan.json
```

Keep the original installation plan and journal. This capture has its own
observation time and does not extend or rewrite the original plan. Inspect the
new plan and select its exact digest through the authenticated root control path.
First check the selected plan without changing the route:

```sh
python3 -I -B builder_runtime_update.py --plan finish-plan.json \
  --plan-sha256 "$FINISH_PLAN_SHA256" \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256"
```

Then explicitly finish:

```sh
python3 -I -B builder_runtime_update.py --plan finish-plan.json \
  --plan-sha256 "$FINISH_PLAN_SHA256" --finish \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256"
```

Finish verifies the successful installation journal and current host under the
existing build lock. It keeps the launcher held through final checks and records a durable release
decision before restoring execution. It then removes its persistent pending
marker and removes the owned drain last. It returns
`REOPENED_UNQUALIFIED`, with qualification readiness false. Run one actual
ordinary OxiDex Just build and the qualification checks afterward. The client
selector requires an executable launcher and no drain, so this ordinary build
cannot be a pre-finish check. Do not treat reopening as proof of an app build.

If finish fails, inspect its journal and actual files before recovery. Its
failure handler attempts to close execution only on the exact installed
launcher while the build lock remains held. It never rolls back runtime bytes,
clears leases, stops builds, or modifies unrelated services. A changed or foreign
file needs explicit recovery.

### Recover an interrupted finish

For an interrupted finish on the same boot and unchanged admission process,
capture with the recovery action as well as the original nonce and client pin:

```sh
python3 -I -B builder_runtime_update.py --capture-assets assets.json \
  --assets-sha256 "$ASSETS_SHA256" --source-main "$SOURCE_MAIN" \
  --finish-nonce "$INSTALLED_NONCE" --recover-finish \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256" > recovery-plan.json
```

Inspect the new observation and select its digest. Recovery is explicit:

```sh
python3 -I -B builder_runtime_update.py --plan recovery-plan.json \
  --plan-sha256 "$RECOVERY_PLAN_SHA256" --recover-finish \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256"
```

Recovery checks the successful installation and known finish stages under the
existing exclusive lock. It closes execution only on the selected launcher,
restores missing owned update markers, and retains the drain. It appends a
`FINISH_RECOVERED_HELD` record; it does not rewrite the original journal or
observation time. Capture a fresh finish plan afterward. Never replay apply or
remove journal entries to retry.

A changed boot, admission process, runtime file, or foreign marker is refused.
Those cases need a separately reviewed recovery scope. This command does not
restart services, reopen builds, or declare qualification readiness.

### Complete a committed release after reboot

If reboot interrupted an already committed release, use `--recover-committed`
in the capture command instead of `--recover-finish`. The new schema-2
observation preserves the original journal and boot history. It records either
an exactly inactive unit or the verified PID and start ticks of its running
admission process. It refuses changed runtime files, image, configuration,
client pin, or unknown service state.

```sh
python3 -I -B builder_runtime_update.py --capture-assets assets.json \
  --assets-sha256 "$ASSETS_SHA256" --source-main "$SOURCE_MAIN" \
  --finish-nonce "$INSTALLED_NONCE" --recover-committed \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256" > reboot-plan.json
```

After inspecting that fresh observation and selecting its exact digest:

```sh
python3 -I -B builder_runtime_update.py --plan reboot-plan.json \
  --plan-sha256 "$REBOOT_PLAN_SHA256" --recover-committed \
  --client-launcher-sha256 "$CLIENT_LAUNCHER_SHA256"
```

Recovery rechecks the durable release decision under the build lock. It retains
the persistent pending marker and uses an owned current-boot permit to start
only an inactive admission unit. After verifying its process, socket, and current
host state, it records a durable checkpoint, removes its markers, and releases
the drain. A missing pending marker is recreated before startup.
If the exact process is already running after a partial attempt, recovery checks
it without another start command. A failed start or postflight retains the persistent marker and owned
drain; another reboot removes the permit and leaves admission held. Successful recovery remains unqualified until actual OxiDex build and
gate checks pass.

After reboot, recovery initializes an absent volatile build lock without
replacing an existing inode. A held post-commit launcher remains recoverable:
the saved release decision must match every file, with only its execution mode
held. Recovery restores execution through the verified descriptor while the
owned drain remains. It rechecks freshness after publishing its checkpoint and
before drain release.

Permission transitions publish durable intent before changing the selected
launcher. Recovery checks both sides of that transition against the same inode
and bytes. Slow recovery observations must pass the selected plan's freshness
check again before any permission change.

The release journal distinguishes the observed held files from the authorized
executable state. A committed decision is permission to complete that transition,
not evidence that it already happened. A completed re-hold keeps that authority
for checked recovery after another reboot.

If unlink succeeded but directory sync failed, the helper reports
`RELEASE_UNLINKED_SYNC_UNCONFIRMED` and exits75. It states that the drain was
removed and does not claim a protective hold or close launcher execution.
Inspect actual jobs, leases and files before any further recovery; do not replay
apply or treat this result as a held installation.

The transaction pins the authorized journal identity and complete prefix. Only
verified writes by that transaction advance the pin. Unexpected replacement or
appended history is preserved and refused, including during failure holding.
Selected reboot plans must match the complete schema before recovery effects.
