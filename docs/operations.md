# Operations and troubleshooting

This page collects the safety rules for operating the fleet, where each component keeps its logs and state,
the recovery procedures that the code implements, and a troubleshooting table for common failures.

## Safety rules

### Preview before you change anything

A request to edit code or documentation doesn't authorize live changes. Before you create, modify, drain,
restart, or delete a VM, a persistent disk, a GitHub runner registration, a Cloudflare resource, a secret,
or the macOS LaunchAgent, get explicit approval for that operation. Use the read-only previews first:

| Command | What it does without a mutation flag |
| --- | --- |
| `src/spot_runner_manager.py` | Prints the deployment plan. It provisions only with `--apply`, and every `--apply` run that gets a registration token first deletes the zone's unattached, `READY`, manager-labeled disks that are older than six hours, even if the deployment then fails. The exception is `--patch-live`, which hot-patches a running host immediately with no preview. |
| `src/spot_runner_autoscaler.py --dry-run` | Prints one tier decision. It never launches, drains, or deletes, and never writes controller state. It reads host evidence with `gcloud compute ssh`, which can add your SSH key to project or instance metadata, or to your OS Login profile, on the first connection. Locally it creates the state directory and lock file and appends to the controller log, and it takes the same state lock as a running controller. With `--manage-control-vm`, it also prints the control VM plan and creates the project control VM lock file. |
| `src/control_vm.py status` and `src/control_vm.py plan` | Show the control VMs, `origin:active`, health, and transition state, and the next lifecycle step. `ensure` and `rotate` change nothing without `--apply`. |
| `src/lib/builder_runtime_update.py --plan` | Checks a reviewed runtime-update plan, given with its `--plan-sha256`, on the builder. It runs only as root through the authenticated control path with `python3 -I -B`. With `--plan`, it mutates only with `--apply`, `--finish`, `--recover-finish`, or `--recover-committed`. `--capture-assets` is a separate read-only capture. |
| `src/runner_fleet.py` | Reads GitHub runner registrations and VM inventory. It never creates or deletes resources. |

!!! warning "`--install-launch-agent` isn't a preview"
    `src/spot_runner_autoscaler.py --install-launch-agent` writes
    `~/Library/LaunchAgents/com.swacktools.spot-runner-autoscaler.plist`, bootstraps it with `launchctl`,
    and exits before the dry-run branch. The installed agent's arguments never include `--dry-run`, so
    `--dry-run --install-launch-agent` installs a live controller. See
    [Install the launch agent](fleet-and-autoscaler.md#run-as-a-macos-launchagent).

Several commands default `--project` to a project ID that the repository owner uses. Always pass
`--project` explicitly.

### Never replay fresh-host bootstrap

`cloud-init/startup.sh` and `cloud-init/builder-startup.sh` are for fresh hosts only. Don't run them again on
an occupied runner host or builder:

- The manager removes the `startup-script` metadata from a runner host after each deployment, so a reboot
  doesn't replay it.
- `builder-startup.sh` exits with status 75 when `/var/lib/oxidex-builder-bootstrap.complete` already
  exists, and its fresh-host guard holds startup when it finds runner processes or services.

To change the runtime of an occupied dedicated builder, use the staged, lock-guarded workflow in
[Runtime updates](builder-runtime-updates.md): capture, check, apply, client pin handoff, and finish.

### Never delete or hand-edit protective state

Don't delete or hand-edit these files to get past a hold, a drain, or a recovery:

- The controller state file and its lock, and the project control VM state `control-vm-<project>.json`.
- The admission lease ledger `/var/lib/gha-admission/leases.json` on a runner host or builder.
- Builder runtime-update records: the `journal.jsonl` in the update's backup directory and
  `/var/lib/oxidex-builder/runtime-update-pending.json`.
- The dashboard's recovery hold `state.json.recovery-pending` and the freeze marker `state.json.frozen`.

A hold records why it stopped. Fix that cause, and let the component that placed the hold remove it.

### Keep runner and builder lifecycles separate

| | Runner host | Dedicated builder |
| --- | --- | --- |
| Name | `oxidex-runners-*` | `builder-*` |
| GitHub runners | Registers listeners. Controller-managed hosts join the fixed runner group your controller uses. | Registers none. |
| Data disk | Created with `auto-delete=yes`, so deleting the VM deletes the disk. | Cache disk created with `auto-delete=no`, so it survives VM deletion and keeps incurring storage charges. |
| Drain | Repository targets: cordon the host, wait until no `Runner.Worker` process runs, then deregister. Organization targets in `resource` and `tiered` modes: move the runners into an isolated drain group, wait until they're idle and the tracked jobs finish, then deregister. | Refuse new builds, then wait for active builds, leases, and cache, upload, and export locks. |

GitHub runner idleness never proves that a builder is idle, and a builder drain never applies to runner
hosts. See [Fleet and autoscaler](fleet-and-autoscaler.md) and [Set up a builder](dedicated-builders.md).

### Classify out-of-memory exits with evidence

Exit status 137 means a process received `SIGKILL`. It doesn't prove an out-of-memory (OOM) event or say
whose memory ran out. Join kernel or cgroup evidence to the job's actual time interval before you assign a
cause:

- A kernel `oom-kill:constraint=CONSTRAINT_NONE` decision, or `Out of memory: Killed process`, is a host
  OOM.
- `oom-kill:constraint=CONSTRAINT_MEMCG`, or `Memory cgroup out of memory: Killed process`, is a container
  OOM.

See [Out-of-memory handling](fleet-and-autoscaler.md#out-of-memory-handling) for how the controller reacts.

### Label evidence accurately

Local unit tests, remote proof runs with `just test`, GitHub CI checks, merged commits on `main`, rendered
startup payloads, and live VM or container observations prove different things. When you report a result,
state the exact command and environment. Don't claim a live deployment or a workload qualification from a
source-only test run or a bootstrap tool probe.

## Logs and state locations

| Component | Logs | State and markers |
| --- | --- | --- |
| Fleet controller | `~/spot-ghrunner.log` (`--log-file`). The LaunchAgent writes standard output and standard error to `~/Library/Logs/spot-runner-autoscaler.log`. | `~/Library/Application Support/Spot Runner Autoscaler/state.json` (`--state-file`) and `state.json.lock`. Control VM state and lock: `control-vm-<project>.json` and `control-vm-<project>.lock` in `SPOT_CONTROL_STATE_DIR`, which defaults to the same directory. |
| Runner host | `/var/log/gha-runner-bootstrap.log` | `/var/lib/gha-runner-bootstrap.complete` (deployment ID), `/etc/gha-admission.json`, `/var/lib/gha-admission/leases.json`, the cordon marker `/run/gha-runner-cordon/draining`, and the patch records `/var/lib/gha-runner-patch/pending.json` and `/var/lib/gha-runner-patch/state.json`. |
| Dedicated builder | `/var/log/oxidex-builder-bootstrap.log` | `/var/lib/oxidex-builder-bootstrap.complete`, `/var/lib/oxidex-builder-readiness.json`, `/etc/oxidex-remote-build.json`, `/etc/gha-admission.json`, `/var/lib/gha-admission/leases.json`, and the drain marker `/run/oxidex-build-draining`. |
| Control VM | The startup script's lines start with `spot-control-startup:` in the `google-startup-scripts.service` journal. Containers: `spot-dashboard` and `spot-cloudflared`. | `/var/lib/spot-dashboard/state.json` and its `.frozen`, `.recovery-pending`, and `.corrupt-<UTC time>` siblings. |

The controller log has no automatic rotation. Configure retention without deleting active state or evidence.
See [State, locks, and logs](fleet-and-autoscaler.md#state-locks-and-logs) for what the controller records.

### Inspect a host

Follow the controller log:

```bash
tail -f "$HOME/spot-ghrunner.log"
```

On a runner host or builder, connect through an approved administrative connection and read the records
without changing them:

```bash
sudo journalctl -u google-startup-scripts.service --no-pager
sudo cat /var/lib/oxidex-builder-readiness.json
sudo cat /var/lib/gha-admission/leases.json
sudo docker ps -a --no-trunc
```

Compare a container's actual limits with the admission policy:

```bash
sudo docker inspect --format \
  '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' \
  CONTAINER_ID
```

Memory values are bytes, and `NanoCpus` is in billionths of a CPU. A zero CPU field isn't proof of an
uncapped container. Check the other CPU quota fields and the cgroup files `cpu.max`, `memory.max`, and
`memory.swap.max`.

Inspect the control VM from the operator workstation:

```bash
PYTHONPATH=src python3 src/control_vm.py status --project YOUR_PROJECT
PYTHONPATH=src python3 src/control_vm.py plan --project YOUR_PROJECT
```

## Recovery procedures

The controller recovers most failures by itself on later checks. Your job is to read why it holds and fix
that cause.

- **Lost hosts:** the controller verifies the recorded VM and disk identities, deletes the stopped VM and
  its disks, confirms that the disks are gone, and removes stale offline, idle runner registrations. An
  attached, recreated, foreign, or unverifiable disk keeps recovery pending. See
  [Recover lost hosts and failed jobs](fleet-and-autoscaler.md#recover-lost-hosts-and-failed-jobs).
- **Jobs that lost their runner:** the controller reruns a job only when GitHub reports a runner-loss error
  for a job on a lost VM. When a rerun's outcome is uncertain, it checks GitHub for a newer attempt. If it
  finds neither a receipt nor a newer attempt, it logs that the rerun needs a manual GitHub check and
  holds automatic resubmission. Check the run in GitHub, and rerun it yourself if needed.
- **Retained boot disks:** when the controller can't confirm that a control VM's state reached its
  successor, it deletes the VM but keeps the boot disk and records it under `retained_disks`. Recover the
  state from the disk, then delete the disk by hand. See
  [Outages and recovery](edge-dashboard.md#outages-and-recovery).
- **Control VM cutover states:** `status` reports `old_state_unconfirmed` when the controller imported state
  from an old VM that never confirmed a freeze, and `carry_back_unconfirmed` when a rollback couldn't confirm
  that the replacement's state reached the old VM. Both keep a boot disk for you to recover from.
- **`state_conflict`:** `/api/health` reports it when a KV write read back as another slot's snapshot. The
  dashboard stops accepting changes until KV shows this slot's write. See
  [Snapshot and active slot checks](edge-dashboard.md#snapshot-and-active-slot-checks).
- **Unreadable dashboard state:** the dashboard renames the file to `state.json.corrupt-<UTC time>`, keeps
  `state.json.recovery-pending` until recovered state is on disk, and reports `state_unavailable`. See
  [Unreadable state file](edge-dashboard.md#unreadable-state-file).
- **Conflicting control VM state:** when a controller's own state file and the project file hold different
  transitions or retained disks, the controller logs that it must `resolve which one is current` and
  doesn't manage the control VM. Decide which state is current before you continue.
- **Interrupted builder runtime update:** use the recovery modes in
  [Runtime updates](builder-runtime-updates.md#recover-an-interrupted-finish).

## Troubleshooting

| Symptom | Cause and next action |
| --- | --- |
| A job fails before its first step with `Runner host is being retired` | The controller cordoned the host after GitHub assigned the job. GitHub doesn't reassign it. Re-run the job, and GitHub schedules it on another runner. |
| The controller logs `Retirement held for <host>: the host cannot refuse new jobs at job start` | The host's runner containers lack the read-only `/run/gha-runner-cordon` mount or the cordon-aware job-started hook. The host stays in service until a cordon-capable host replaces it. Don't remove its runners by hand. |
| The controller logs `Retirement held: live heavy route has no verified replacement` | The controller protects a live heavy-runner route until a verified replacement exists. Check the replacement host's registration and limits. |
| Control VM rotation aborts with `slot <slot> not healthy after 1200s` | The new VM didn't report healthy within 20 minutes. Its health requires HTTP 200 with `status` `ok`, `hydrated` true, and `frozen` false. Read its startup journal, then check the image pull, secrets, and the tunnel. The old VM keeps serving. |
| The controller logs `replacement <name> reports state_version <n>, below the transferred state_version <m>` | The replacement restarted and loaded an older KV snapshot. The controller imports the state again, up to 5 times, and continues only after fresh health confirms the version. The old VM and its disk stay until then. |
| The controller logs `replacement not ready <n>s after cutover` | The replacement stayed unready for 5 minutes after the `origin:active` switch while the old VM ran, so the controller rolled back to the old VM. If the old VM isn't running, the controller waits and deletes nothing. Check the replacement's health detail in the log. |
| The active control VM is `running but not healthy` | The controller waits. After 15 minutes unhealthy, or 10 minutes stopped or missing, it creates the other slot. A stopped standard VM is restarted instead, and replaced only after a start fails. |
| The control VM startup stops with `could not pull ghcr.io/swack-tools/spot-dashboard...`, or `docker pull` reports access denied or unauthorized | The image is private, and the GitHub token in Secret Manager lacks the `read:packages` scope. Give that token the scope, or store a token that has it as a new version of the secret. The startup script reads the latest version at every boot, so reboot the VM or let the controller retry. The standard e2-micro control VM never builds the image, so it can't start without the pull. |
| `control_vm.py plan` shows `rotate refused: ...` | A setting that the new VM needs is missing or invalid, such as the dashboard image. Fix the named setting. |
| A dashboard sign-in fails with 403 and `could not list controlled repositories from <repo>` | The dashboard couldn't list the config repository, so it denies access. Give `SPOT_CONFIG_GH_TOKEN` or `GH_TOKEN` read access to that repository. |
| Hot-patching from the dashboard answers 501 with `hot_patch_unavailable` | The dashboard image has no `gcloud` and no SSH credentials. Preview in the dashboard, then run the full `--patch-live` command from an operator workstation, with every deployment value the host was provisioned with, including `--max-cost`. See [Hot-patch a running host](runner-patches.md#hot-patch-a-running-host). That command changes the host immediately. See [When hot-patching is unavailable](dashboard.md#when-hot-patching-is-unavailable). |
| The browser shows the waiting page, or an API call gets 503 with `control_vm_unavailable` | The Worker can't reach the active control VM. Keep the page open, because it retries when `/api/health` returns 200. Recovery depends on the controller running with `--manage-control-vm`. Check `control_vm.py status`. |
| No affordable offer | Confirm complete current compute and disk prices, zone compatibility, and quota. Keep the hold reason that the controller logs. |
| No memory observations | Check the host's Ops Agent, that the runner VM's service account has `roles/monitoring.metricWriter` and the VM has the `monitoring.write` or `cloud-platform` scope, the controller identity's monitoring access, the sample timestamps, and the boot identity. Runner hosts install the agent only with `--resource-monitoring`. |
| Registration timeout: `Startup script did not complete deployment <id>` | The bootstrap didn't write this deployment's marker within `--registration-timeout-seconds` (default 1200). Read `/var/log/gha-runner-bootstrap.log`, then check the token scope, the runner group, and the expected runner names. The manager removes the token from instance metadata either way, so rerun to get a fresh one. |
| `Only <n>/<m> expected runners are online`, or `Some runners did not come online; the one-use token was removed` | The first message is the usual one. The second appears when the count drops between the manager's two final checks. Both are the manager's own wording: it removes the token from instance metadata, but GitHub keeps the registration token valid until it expires. Rerun the deployment to bootstrap the missing runners with a fresh token. |
| Admission denied, or a build exits with status 75 | The builder is draining, its bootstrap isn't complete, or admission refused the lease. Check `/run/oxidex-build-draining`, the owned leases, the limits, and the full container inventory. Wait for active work to finish. |
| Transfer permission denied | Verify the approved uploader identity, its key, and its `oxidex-build` membership. Don't broaden permissions. See [Dedicated builder access](security.md#dedicated-builder-access). |
| Exit 137 | Join kernel or cgroup OOM evidence to the job interval before you assign a cause. See [Classify out-of-memory exits with evidence](#classify-out-of-memory-exits-with-evidence). |
| Compiler or header failure | Repair the source or toolchain inputs. Don't retry on another VM as a capacity workaround. |
| Replacement stuck | Inspect the persisted operation and the exact disk and runner identities. Don't clear state to bypass a hold. |
