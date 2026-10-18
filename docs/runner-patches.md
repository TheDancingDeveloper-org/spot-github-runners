# Runner patches

A runner patch is a declarative customization of the runner hosts and runner containers that serve one repository. The patch is a manifest in the `spot-runner-patch-v1` format. It installs packages, writes files, sets kernel parameters (sysctl), adds container mounts, and sets hardware requirements such as a minimum vCPU count or a CPU instruction set. The tools render the manifest into the host startup script, the runner image, and the `docker run` flags, so a reviewed manifest replaces hand edits on a host.

This page covers the manifest, how the tools classify a change, the receipts that prove a patch, GitHub label sync, and the operator procedures. For the dashboard console that edits manifests, see [Dashboard](dashboard.md) and [Runner patches in the edge dashboard reference](edge-dashboard.md#runner-patches).

!!! warning "Operator procedures change live resources"
    The commands on this page create, patch, or replace Compute Engine virtual machines (VMs) and relabel GitHub runners. A request to edit code or documentation doesn't authorize any of them. Run each one only when you intend the live change, and complete the preview step first.

## Where manifests live

Manifests live in a central configuration repository, `<org>/spot-runners-config` by default. The `--config-repo` option overrides the repository, and `--org` sets the organization (default `swack-tools`).

| File | Purpose |
| --- | --- |
| `defaults.yaml` | Settings that every repository inherits. Optional. |
| `repos/<owner>/<repo>.yaml` | The override for one repository. Required for a repository that uses `--patch-repo`. |

When you pass `--patch-repo OWNER/REPO`, the tools resolve the default branch of the configuration repository, pin its head commit, and read both files at that commit. A missing `defaults.yaml` is treated as empty. A missing repository file is an error.

A manifest that declares a `repository` must name the target repository, compared without regard to case. A manifest that names another repository is refused, and so is a merged result that does.

The `--patch-file PATH` option loads one local file instead. It skips `defaults.yaml` and the merge, and `repository` can be empty. You can pass `--patch-repo` or `--patch-file`, but not both.

### Supported formats

The loader accepts JSON (text that starts with `{`), TOML, and a restricted YAML subset. A parser in the Python standard library reads the YAML subset, so no third-party package affects the result. The subset supports nested mappings, lists, `|` and `|-` block scalars, flow lists such as `[a, b]`, flow mappings, quoted strings, and comments. Anchors aren't supported. A `>` folded scalar isn't supported either, and it parses without an error as the literal text `>`, so use `|` or `|-` for multi-line text.

## Manifest schema

All keys are optional except where noted. A missing `schema` defaults to `spot-runner-patch-v1`, and any other value is an error.

!!! note "Unknown keys"
    The dashboard rejects unknown top-level keys and unknown keys inside a section, so a typo shows up as a field error. The loader that `spot_runner_manager.py` uses ignores unknown keys silently. Validate a manifest in the dashboard before you rely on it.

### Top-level keys

| Key | Type and limits |
| --- | --- |
| `schema` | The string `spot-runner-patch-v1`. |
| `repository` | `OWNER/REPO`, matching `[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+`. The dashboard forbids it in `defaults.yaml`. |
| `hardware` | Mapping. See the next table. |
| `host` | Mapping. See the next table. |
| `container` | Mapping. See the next table. |

### The `hardware` section

| Key | Type and limits |
| --- | --- |
| `arch` | `x86_64` (aliases `x64`, `amd64`) or `arm64` (alias `aarch64`). Case-insensitive. Default `x86_64`. |
| `isa` | List of instruction set architecture (ISA) names. Names are lowercased, `_` becomes `-`, and `avx-512` becomes `avx512`. Each name must match `arch`. Duplicates are removed. The combination must leave at least one Compute Engine CPU platform. |
| `extra_labels` | List of extra GitHub runner labels. Each matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`. |
| `min_vcpu` | Positive integer. Booleans are refused. |
| `min_ram_gib` | Positive integer, in GiB. |
| `qualify_command` | Shell script text. It runs as the last step of the host script. The image build and the container steps come after it. A failure stops the patch from completing. |

The supported ISA names, the `/proc/cpuinfo` flags that the host script requires, and the Compute Engine machine families and CPU platforms that provide them are:

| ISA | Architecture | Required CPU flags | Machine family and CPU platform |
| --- | --- | --- | --- |
| `avx2` | `x86_64` | `avx2` | `c4d` and `n4d` on AMD Turin; `c4` on Intel Granite Rapids and Emerald Rapids; `n4` on Emerald Rapids; `c2d` and `n2d` on AMD Milan; `n2` on Intel Ice Lake and Cascade Lake |
| `avx512` | `x86_64` | `avx512f`, `avx512bw`, `avx512vl` | The `avx2` list without `c2d` and `n2d` |
| `avx512-vbmi2` | `x86_64` | `avx512f`, `avx512_vbmi2` | `c4d` and `n4d` on AMD Turin; `c4` on Granite Rapids and Emerald Rapids; `n4` on Emerald Rapids; `n2` on Ice Lake only |
| `amx` | `x86_64` | `amx_tile`, `amx_bf16`, `amx_int8` | `c4` on Granite Rapids and Emerald Rapids; `n4` on Emerald Rapids |
| `avx10.1` | `x86_64` | `avx512f`, `avx512_vbmi2`, `amx_tile` | `c4` on Granite Rapids |
| `neon` | `arm64` | `asimd` | `c4a` on Google Axion; `t2a` on Ampere Altra |
| `sve` | `arm64` | `asimd`, `sve` | `c4a` on Google Axion |
| `sve2` | `arm64` | `asimd`, `sve`, `sve2` | `c4a` on Google Axion |

When a manifest lists several ISA names, a host needs a platform that appears in every list.

### The `host` section

| Key | Type and limits |
| --- | --- |
| `apt_packages` | List of package names matching `^[a-z0-9][a-z0-9+.-]*(=[0-9A-Za-z.+:~-]+)?$`. Append `=VERSION` to pin a version. Duplicates are removed. |
| `sysctl` | Mapping of kernel parameter keys, such as `vm.max_map_count`, to values. A key matches `^[a-z0-9_]+(\.[a-z0-9_-]+)+$`. A value is a string or number, never a boolean, and it must be a single non-empty line. |
| `files` | List of mappings with `path`, `content`, `mode`, and `owner`. `path` is an absolute normalized path that isn't `/` and has no `//` or control characters. `content` is a string (default empty). `mode` is 3 or 4 octal digits (default `0644`). `owner` is `user:group` (default `root:root`). Duplicate paths are refused. |
| `boot_mkdirs` | List of absolute normalized directory paths to create. Duplicates are removed. |
| `boot_script` | Shell script text that runs as root with `bash -euo pipefail` at provisioning, and again on every live apply of the patch, so write it to be safe to repeat. |

### The `container` section

| Key | Type and limits |
| --- | --- |
| `apt_packages` | Same rules as `host.apt_packages`. Installed into the runner image. |
| `sysctl` | Same rules as `host.sysctl`. Passed to the container as `--sysctl` flags. |
| `files` | Same rules as `host.files`. Written into the runner image. |
| `mounts` | List of mappings with `source`, `target`, and `read_only` (boolean, default `false`). Both paths are absolute and normalized, and neither contains `:`. Duplicate targets are refused. |
| `dockerfile_run` | Shell script text that runs during the image build, and on a live apply also inside each running runner container. |

A mount source can't be the Docker socket (`/var/run/docker.sock` or `/run/docker.sock`), `/run/gha-admission`, `/var/lib/gha-admission`, `/etc`, `/root`, `/proc`, `/sys`, or `/dev`. The check also refuses any path inside one of those paths, and any parent directory of one, such as `/var/lib`.

## Merge and override rules

For a repository, the loader merges `defaults.yaml` with the repository file. The repository file wins on conflicts, and the merge works per key.

| Setting | Merge rule |
| --- | --- |
| `schema`, `repository` | The repository file's value, else the default's. |
| `hardware.arch`, `min_vcpu`, `min_ram_gib` | The repository file's value when the key is present, else the default's. |
| `hardware.qualify_command` | The repository file's value when it isn't empty, else the default's. |
| `isa`, `extra_labels`, `apt_packages`, `boot_mkdirs` | The union, with default entries first, and duplicates removed. |
| `sysctl` | Mapping merge. The repository file wins for a key that both define. |
| `files` (host and container) | Merged by `path`. The repository file replaces a default entry for the same path. |
| `mounts` | Merged by `target`. |
| `boot_script`, `dockerfile_run` | Concatenated with a newline, default first, skipping empty parts. |

After the merge, the result passes the same validation as a single manifest. A repository file that is valid alone can still fail after the merge. For example, an `isa` entry that is incompatible with an inherited `arch` fails. The dashboard validates each repository file as merged.

### Canonical JSON and the SHA-256

The identity of a patch is the SHA-256 digest of its canonical JSON. The tools build that JSON from the validated patch without `source_commit` and `source_branch`, sort the keys, and use `,` and `:` separators with no whitespace. Two manifests that differ only in format, mapping key order, or comments have the same digest. The order of list entries is part of the digest, so reordering a list changes it. A change to `repository` changes the digest. The dashboard shows the digest as the effective patch SHA-256, and every receipt and metadata key on this page refers to it.

## How a patch applies

A patch reaches a host in one of three ways, from least to most disruptive.

| Strategy | When it applies | Effect |
| --- | --- | --- |
| Hot patch | The host runs, and the change only adds or rewrites. | `--patch-live` runs a script on the running VM. It rebuilds the runner image and executes the container steps inside each running runner container. The script doesn't restart the containers. |
| Container recreate | The change adds or alters a container mount. | Containers must be drained and recreated. `--patch-live` refuses this case. |
| VM replacement | The host can't reach the patch in place. | Provision a new VM with `--patch-repo`. The autoscaler can rotate hosts with the same source. |

### Classification

The classification function compares the last completed patch on a host (from its receipt) with the target patch, and returns one of four values. The dashboard adds a fifth value, `unknown`, for hosts it can't classify.

| Value | Meaning |
| --- | --- |
| `noop` | The host already runs the target. The digests match. |
| `hot_patch` | An in-place live patch can apply the change. |
| `recreate_containers` | Only the container mounts differ, so containers must be recreated. |
| `replace_vm` | The host needs a different VM. |
| `unknown` | The dashboard can't establish what the host runs. It is never `noop` and blocks a dashboard hot patch. |

The rules apply in this order:

1. **Unfinished attempt at another patch.** If the host has a pending marker or pending metadata that names a patch other than the target, the result is `replace_vm`. The partial effects of that attempt are unknown, and only a new VM removes them. A pending attempt at the target itself is re-applied. A pending marker or pending metadata whose value is `none` doesn't force this result.
2. **A completed patch exists.**
    1. If the digests match, the result is `noop`.
    2. If the architecture differs, or the target adds an ISA that the current patch doesn't have, the result is `replace_vm`.
    3. If the destructive-diff rule finds a drop, the result is `replace_vm`.
    4. If the container mounts differ, the result is `recreate_containers`.
    5. Otherwise the result is `hot_patch`.
3. **No completed patch exists.** If the target isn't `x86_64` or lists any ISA, the result is `replace_vm`. If the target has container mounts, the result is `recreate_containers`. Otherwise the result is `hot_patch`.

#### The destructive-diff rule

The live script only installs and writes. It never uninstalls a package, deletes a file or directory, resets a sysctl value, or reverses a script. A host can therefore move only to a target whose effects include every applied effect. Any of the following forces `replace_vm`, and the reason names what the target drops:

- A package that `host.apt_packages` or `container.apt_packages` drops.
- A sysctl key that `host.sysctl` or `container.sysctl` drops.
- A path that `host.files` or `container.files` drops.
- A directory that `host.boot_mkdirs` drops.
- A change to a non-empty `host.boot_script`, because that script already ran.
- A change to a non-empty `container.dockerfile_run`, because that script already ran.

A target can still add packages, files, directories, and sysctl keys, and it can rewrite a file or a sysctl value in place. The same rules apply when you revert to an earlier patch. The `hardware` section is excluded from this rule because labels sync in full, and the tools check size and ISA separately.

#### Hardware constraint checks

Before any change, the tools check the observed host against every hardware constraint of the target:

- The machine family must match `hardware.arch`.
- `hardware.min_vcpu` and `hardware.min_ram_gib` must not exceed the host's observed vCPU count and RAM.
- For `hardware.isa`, the machine family and CPU platform must appear in the preceding ISA table.

A violation means the host can never run the patch, so the dashboard classifies it as `replace_vm`. A value that the tools can't observe, such as a missing CPU platform, is `unknown`. Both outcomes block `--patch-live` before it touches the host.

#### Additional dashboard rules

The dashboard classifies each host in this order and stops at the first match:

1. A host found only by its name prefix, with no recorded patch metadata, is `unknown`. The host predates patch recording.
2. A hardware violation is `replace_vm`, and a constraint that the dashboard can't verify is `unknown`.
3. A host with pending patch metadata is `unknown`, even when the applied keys match.
4. Where a hot-patch executor can reach the host, the dashboard reads the host receipt and classifies from it. When the host names the target but has no completion receipt from its current boot, the result changes from `noop` to `hot_patch`, so a live apply writes a proof.
5. Without an executor, as on the control VM, the dashboard classifies from the patch recorded in instance metadata. The result is `unknown` when that record is missing, fails its SHA-256 check, has no boot binding, or names a boot other than the instance's current `lastStartTimestamp`. A manifest that was too large to record is `unknown` unless its digest equals the target's. The reason says to reprovision the host or run `--patch-live` again.

## Receipts and metadata

Four records exist for a patch. Only the first proves what runs on the host.

| Record | Location | Meaning |
| --- | --- | --- |
| Completion receipt | `/var/lib/gha-runner-patch/state.json` on the host | The last patch that fully applied, with its boot ID. |
| Pending marker | `/var/lib/gha-runner-patch/pending.json` on the host | A patch whose apply started and didn't finish. |
| Instance metadata | The VM's Compute Engine metadata | The patch that the manager recorded after it confirmed the apply. |
| `spot-runner-patch` label | The VM's Compute Engine labels | A filter aid only. It isn't proof. |

### Host receipt and pending marker

Each patch step sequence first writes the pending marker, then runs, and writes the completion receipt only after every step succeeds. The tools replace both files by atomic rename.

- **Pending marker:** a JSON mapping with `receipt_schema` and `pending_patch_sha256`.
- **Completion receipt:** a JSON mapping with `boot_id` (from `/proc/sys/kernel/random/boot_id`) and a `receipt` that holds `receipt_schema` (`gha-runner-patch-receipt-v2`), `schema`, `repository`, `source_commit`, `patch_sha256`, and the canonical `manifest`. The tools remove the pending marker last.

For a provisioned host, the pending marker is written before the host patch script runs, and the receipt is written after every runner container starts. For a live patch, the steps include the image build and every container.

Only a completion receipt from the current boot, with no pending marker, proves which patch a host runs. A receipt written for an earlier boot, or by an earlier version of the tooling (without `receipt_schema`), doesn't count as proof. A failed attempt leaves the earlier receipt and the marker in place.

### Instance metadata keys

The manager writes the applied keys and the label only after the host confirms the patch. A provisioned host confirms it when its bootstrap writes the completion marker for this deployment, every runner is online, GitHub labels sync, and the storage check passes. A live patch confirms it when the host prints `LIVE_PATCH_APPLIED:<sha256>`.

| Key | Content |
| --- | --- |
| `spot-runner-patch` | The canonical manifest JSON. Omitted when larger than 64 KiB, and then only the digest is recorded. |
| `spot-runner-patch-sha256` | The digest, or `none` for a host without a patch. |
| `spot-runner-patch-target` | The `OWNER/REPO` the patch belongs to. |
| `spot-runner-patch-boot` | The `lastStartTimestamp` of the boot in which the manager confirmed the patch. A later boot makes the record stale. |
| `spot-runner-patch-pending-sha256` | The digest (or `none`) of a patch that was requested and not yet confirmed. |
| `spot-runner-patch-pending` | The manifest of the pending patch, or a placeholder when it exceeds the size limit. |
| `spot-runner-prefix`, `spot-runner-count`, `spot-runner-label`, `spot-runner-region`, `spot-runner-heavy-pool` | Deployment details that a live patch needs to find the host's runners and labels. |
| `spot-runner-registration-target`, `spot-runner-group-id` | The GitHub scope the runners registered with (an organization or `OWNER/REPO`) and, for organizations, the runner group. |

The `spot-runner-patch` label holds the first 16 characters of the digest, or `none`.

While a startup script applies a patch, or after it fails, the requested patch sits under the pending keys, and the applied keys keep their earlier value. A live patch writes only the two pending keys, right before it runs the live script, so a live patch that fails part-way leaves the host pending. After success, the manager writes the applied keys and the label, and it removes the pending keys last, so a host never looks confirmed before its applied record is complete.

## Label sync

Each runner registers with a label list, and the manager keeps that list in sync on GitHub. The desired custom labels are, in order and without duplicates:

1. The runner label (`--runner-label`).
2. The machine type (`--instance-type`).
3. The region (`--region`).
4. The patch labels: the `hardware.isa` names, then `hardware.extra_labels`.

GitHub's built-in labels (`self-hosted`, `Linux`, `X64`, `Windows`, `macOS`, `ARM64`, and `ARM`) are excluded from the comparison. For each expected runner (`<prefix>-<vm>-<n>`), a mismatch triggers a `PUT` that replaces the runner's custom labels, and the manager reads the result back and checks it.

Label sync runs at three points:

- **After provisioning.** It runs once every expected runner is online and before the storage check and the patch record. A failure fails the deployment, and the host stays pending.
- **After a live patch.** It runs after `LIVE_PATCH_APPLIED` and before the applied record. A failure leaves the host pending, and running `--patch-live` again re-applies the patch and retries the sync.
- **For a host already at the patch.** A `--patch-live` run on a verified `noop` host syncs labels before it records the patch. A dashboard hot patch only syncs the labels of each `noop` host, and it doesn't record the patch.

Before a live patch changes anything, it checks everything that label sync needs: the runner group, strict-pool membership, and that GitHub lists every expected runner. Label sync therefore can't fail because of missing inputs after the host changed. Label sync raises an error when GitHub doesn't list an expected runner or when the labels read back differ.

## Apply a patch

Commit the manifest to the configuration repository first. In the dashboard, you save a manifest as a pull request, and you review and merge it like any other change. The tools read the default branch, so an unmerged manifest has no effect.

### Provision with a patch

Add `--patch-repo OWNER/REPO` (or `--patch-file`) to `src/spot_runner_manager.py`. Without `--apply`, the manager resolves and validates the patch, checks the `--cpu` and `--ram` values against `hardware.min_vcpu` and `hardware.min_ram_gib`, checks price and capacity, logs the plan, and creates nothing. Run that preview first:

```bash
PYTHONPATH=src python3 -B src/spot_runner_manager.py \
  --project <project> --max-cost <usd-per-hour> \
  --instance-type <machine-type> --cpu <vcpus> --ram <gib> \
  --patch-repo OWNER/REPO
```

When the plan is correct, add `--apply` to the same command. The manager renders the patch into the startup script, the runner image, and the container flags, writes the pending metadata, waits for the bootstrap, syncs labels, and records the applied metadata. The `src/spot_runner_manager.py --help` output lists the remaining options.

### Hot-patch a running host

`--patch-live` applies a patch to a running VM and its runner containers. The command has no preview mode and doesn't need `--apply`: it acts as soon as the checks pass. Preview the effect first in the dashboard, which classifies every recorded host for a repository and changes no host. With a hot-patch executor, the preview reads each host's receipt over `gcloud compute ssh`, which can add your SSH key to project or instance metadata, or to your OS Login profile. See [Preview and hot-patch](dashboard.md#the-patch-console).

You must pass the same deployment values that the host was provisioned with, because the live patch derives the runner names, labels, and runner group from them. The values include `--instance-name`, `--instance-type`, `--region`, `--zone`, `--runner-prefix`, `--runner-count`, `--runner-label`, `--target`, and `--runner-group-id`.

The manager also selects the host's sizing before it applies the patch, so replay the sizing mode the host was provisioned with, whichever applies:

- For a tier host, pass `--tier`, and add `--tier-slots`, `--tier-job-memory-gib`, and `--host-overhead-gib` when you changed them from their defaults.
- For fixed sizing, pass `--fixed-runner-sizing` with `--runner-cpu`, `--runner-memory-gb`, and `--runner-count`.
- For a strict heavy pool, pass `--heavy-runner-pool` with the same pool size (`2` or `3`).

A mismatch with the observed VM, such as a different `--instance-type`, is refused.

```bash
PYTHONPATH=src python3 -B src/spot_runner_manager.py \
  --project <project> --max-cost <usd-per-hour> \
  --instance-name <vm-name> --instance-type <machine-type> \
  --region <region> --zone <zone> --cpu <vcpus> --ram <gib> \
  --runner-prefix <prefix> --runner-count <count> --runner-label <label> \
  --target <org-or-owner/repo> --runner-group-id <group-id> \
  --patch-repo OWNER/REPO --patch-live
```

The example doesn't include a sizing mode. Add the `--tier`, `--fixed-runner-sizing`, or `--heavy-runner-pool` flags that match your host. A strict heavy pool also replaces `--runner-count` and `--runner-label` with its own values.

The command refuses, and changes no VM, metadata, or GitHub label, in these cases. The receipt check and the later checks run after a receipt read over `gcloud compute ssh`, which can add your SSH key to project or instance metadata, or to your OS Login profile:

- The VM isn't running, or its machine type differs from `--instance-type`.
- The observed host violates a hardware constraint, or a constraint can't be verified.
- The host's receipt is unreadable, or the classification is `replace_vm` or `recreate_containers`.
- Label sync can't run because the runner group, a strict pool, or a listed runner is missing.

When all checks pass, the manager writes the pending metadata, runs the live script over SSH, requires `LIVE_PATCH_APPLIED:<sha256>` in the output, syncs labels, and then records the applied metadata and clears the pending keys. For a host that is already at the patch, the command syncs labels and records the patch without running the live script.

### Keep the autoscaler consistent

The autoscaler launches replacement hosts through `spot_runner_manager.py`. Pass `--patch-repo OWNER/REPO` (and `--config-repo` when you don't use the default) to `src/spot_runner_autoscaler.py`, so that every host it launches gets the same verified patch. The option is required to rotate any host that runs a patch.

A controller holds a rotation, and keeps the running host over the price cap, when the host runs a patch and either of these is true:

- The controller has no `--patch-repo`.
- The host's recorded `spot-runner-patch-target` names a different repository than the controller's `--patch-repo`.

A host counts as patched when its applied or pending digest is recorded and isn't `none`. Both the make-before-break rotation and the delete-first price eviction skip a held host and log a warning. For the rest of the autoscaler, see [Fleet and autoscaler](fleet-and-autoscaler.md).

!!! note "The LaunchAgent copies the option"
    Installing the macOS LaunchAgent with `--install-launch-agent` copies `--patch-repo` and `--config-repo` into the agent's arguments. Installing also writes and bootstraps the live daemon, so treat it as a live change.

## Source files

| Topic | Source |
| --- | --- |
| Schema, merge, classification, receipts | [`src/lib/runner_patch.py`](https://github.com/swack-tools/spot-github-runners/blob/main/src/lib/runner_patch.py) |
| Label sync, metadata, `--patch-*` options, live apply | [`src/lib/runner_manager.py`](https://github.com/swack-tools/spot-github-runners/blob/main/src/lib/runner_manager.py) |
| Metadata key names | [`src/lib/runner_fleet.py`](https://github.com/swack-tools/spot-github-runners/blob/main/src/lib/runner_fleet.py) |
| Rotation hold, option pass-through | [`src/lib/spot_runner_autoscaler.py`](https://github.com/swack-tools/spot-github-runners/blob/main/src/lib/spot_runner_autoscaler.py) |
| Dashboard classification, validation, hot-patch gates | [`src/lib/patch_console.py`](https://github.com/swack-tools/spot-github-runners/blob/main/src/lib/patch_console.py) |
