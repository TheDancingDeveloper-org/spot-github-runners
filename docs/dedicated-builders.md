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

Prepare a private JSON configuration containing the following shape and your
explicitly approved uploader identity/public key. Do not use a private SSH key.
`authorized_uploader` must contain exactly `schema` (`root-approved-ssh-mapping-v1`),
`principal`, `username`, numeric `uid` (2000 is the deployment example),
`identity_receipt_sha256`, the complete single-line Ed25519 `public_key`, and
`public_key_sha256` (SHA-256 of that complete key string). Set
`authorized_uploader_sha256` to SHA-256 of its sorted, compact JSON representation.
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

Add the two uploader fields described in the preceding section, then render locally:

```bash
python3 src/render_builder_startup.py --config /private/builder-config.json \
  --deployment-id builder-your-unique-name --output-directory /private/builder-payload
bash /private/builder-payload/launch-command.sh
```

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
