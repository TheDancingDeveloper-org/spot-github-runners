# Remote builds and clients

This page describes the host launcher on a dedicated builder, the clients that start a build, and where build
data lives. To create a builder, see [Set up a builder](dedicated-builders.md). To change a builder that already
runs jobs, see [Runtime updates](builder-runtime-updates.md).

## Host launcher

The host launcher is `/usr/local/bin/oxidex-remote-build`. Its configuration is `/etc/oxidex-remote-build.json`.
It checks the project identifier, the allowed command, and the image identity. The Docker admission wrapper
then checks the mounts and limits and takes the admission ledger lease before the container starts. Containers run as UID and GID 1001 without a Docker socket, added capabilities, or
privilege escalation. See the [security model](security.md).

Use the approved uploader account for source transfer. A control account with `sudo` access isn't necessarily
allowed to traverse the source directories. Don't change directory permissions to work around an identity
mismatch.

On a host with an already prepared source project, an example build command is:

```bash
sudo -n /usr/local/bin/oxidex-remote-build oxidex-example \
  cargo build --release --locked --bin oxidex
```

- `prepare` creates the project directories, gives the uploader source access, and gives UID 1001 the
  project's target directory and the shared Cargo directory. It doesn't erase existing source or stop a
  build. It's refused while the builder drains.
- Every launcher call on a dedicated builder resets the shared parent directories to `root:1001` with mode
  `0710` and refuses symbolic links in those paths.
- `cleanup` removes everything in the project's source directory after it checks the cache locks and
  container mounts. It keeps the targets, and it runs during a drain.
- Transfer the source completely before you start the build. Don't prepare or synchronize a project that
  another build is using.
- Cancellation and reconciliation checks in the admission and Docker adapter paths depend on exact run and
  container identities. They don't authorize a broad container stop.

Two Rust compilations can run inside the one aggregate container, but they share its CPU and memory limits.
They aren't two independent 28 GiB reservations. Measure the actual concurrent workload before you claim that
it fits.

## Choose a client

OxiDex's repository-owned `just` recipes and worker registry are maintained in the OxiDex repository. Configure
that client for the approved dedicated host, uploader identity, source, image, and cache before you enable its
route. This repository doesn't update OxiDex's workflow or registry. See
[Simplify remote builds](simplification.md) for the target workflow.

### Explicit client for a selected host

`src/remote_rust_build.py` builds on a host that you select. It works with a compatible legacy host:

```bash
python3 src/remote_rust_build.py \
  --source "$HOME/git/oxidex" \
  --instance YOUR_INSTANCE --zone YOUR_ZONE --project YOUR_PROJECT \
  --worktree-id oxidex-example \
  --profile release \
  --artifact-dir "$HOME/git/oxidex/target/remote-linux/release" \
  --evidence-dir "$HOME/oxidex-ops/evidence/remote-build-example"
```

- Use a new evidence directory for each attempt to keep earlier receipts.
- The client snapshots the working-tree contents of tracked files, so uncommitted edits are included. It skips
  paths under `.git`, `.codex`, `.claude`, `.agents`, `target`, `node_modules`, and `.venv`, files named
  `.env*`, `*.pem`, or `*.key`, and symbolic links. It transfers and verifies the archive, runs the build,
  checks the artifact checksum, and records source and timing evidence. The archive
  (`remote-source.tar.gz`), the transaction log, and the receipt stay in the evidence directory.
- `--artifact-dir` downloads the verified `oxidex` binary into that directory. Without it, the client keeps a
  receipt and doesn't download the binary.
- The client uses `gcloud compute ssh` and `gcloud compute scp`. Verify that the selected account and host
  access match the target. Don't point this client at a dedicated `builder-*` host: those hosts accept only
  the approved uploader over direct SSH. See the [security model](security.md).

### Cargo shims

Install the optional Cargo shims:

```bash
python3 src/install_remote_commands.py
export PATH="$HOME/.cargo/bin:$PATH"
```

The installer writes `cargo-oxidex-remote` and `cargo-oxidex-auto` into `$CARGO_HOME/bin` (or `~/.cargo/bin`),
and `--bin-dir` overrides the location. The shims run scripts from this checkout, so keep the checkout in place.
The command names are `cargo oxidex-remote` and `cargo oxidex-auto`.

- For `cargo oxidex-remote`, the configured OxiDex checkout supplies the remote instance, zone, project, and
  worktree settings through its environment or `.cargo/config.toml`. `cargo oxidex-auto` reads only the project
  and selects the instance and zone itself. Its selection connects to each candidate with `gcloud compute ssh`,
  and it always downloads the artifact into `target/remote-linux/<profile>` in the checkout.
- Use `--profile debug` or `--profile release` with the explicit client.
- `cargo oxidex-auto` picks a lightly used worker and delegates the build over SSH. It makes up to three
  attempts by default (`--max-attempts`). It retries only when the failed attempt's receipt is marked retryable,
  so compiler or header errors stop instead of moving to another host.
- The automatic selector considers running legacy `oxidex-runners-*` and `oxidex-buildbench-*` hosts. It doesn't
  consider the dedicated `builder-` registry.
- The helper calls `cargo remote-build`. That separate alias or command must already exist in the configured
  OxiDex checkout, because the installer creates only the two `oxidex-*` shims. Installing the shims alone
  doesn't configure automatic selection or the dedicated builder registry.

Linux artifacts from these clients don't run natively on macOS. Cross targets and SDK-free framework interfaces
don't replace native artifact validation. A full ExifTool comparison needs its own pinned oracle, fixtures, and
qualification workflow: the standalone Dockerfile doesn't install the full oracle corpus.

## Qualification source admission

Projects with the dedicated qualification contract authenticate signed source, bundle, and run identities before
they import any executable. A bounded importer and supervisor keep protective locks until all descendants are
reaped. The adapter seals executable source separately from writable measurement Git metadata, and it validates
the required mounts and the allowed oracle fetch.

These source controls don't certify an installed deployment. Production qualification still needs evidence from
the actual startup, authority, mounts, process lifetime, disk, network, and workload. Don't mark a worker
qualified from a successful source-only test suite or a bootstrap tool probe.

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
| Exported artifacts (legacy client transactions only, created and removed for each run) | `/mnt/runner-data/remote-build/exports/<run>` |

Mounting only the Docker data root isn't enough if containerd still stores layers on the boot disk. Bootstrap
configures both on attached storage. Caches persist across builds on the same surviving disk. That retention
isn't a backup, and it doesn't prove cache reuse across VMs. Don't format, delete, or detach an existing cache
disk to make a failed bootstrap retry succeed.

Builder drains deny new builds while active builds, units, leases, and cache or upload and export locks finish.
Preserve foreign or uncertain work, and record why a drain is blocked. Runner drains and builder drains have
different completion tests. See [Fleet and autoscaler](fleet-and-autoscaler.md).
