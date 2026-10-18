# Fleet and autoscaler

The fleet controller keeps a set of GCP Spot virtual machines (VMs)
registered as GitHub Actions runners. It reads provider and
GitHub state, chooses a host topology, and changes one host at a time. This page
explains how the controller decides, how it retires a host without losing work,
and which guards protect running jobs.

!!! warning "Live operations"
    Only `src/runner_fleet.py`, `src/spot_runner_manager.py` without `--apply` or
    `--patch-live`, and `src/spot_runner_autoscaler.py --dry-run` without
    `--install-launch-agent` leave VMs, disks, runner registrations, and controller
    state unchanged. A dry run still connects with `gcloud compute ssh`,
    which can add your SSH key to project or instance metadata, or to your OS
    Login profile. Every other controller run can create, drain, or delete VMs and
    runner registrations. Read [Dry run, once, and the state lock](#dry-run-once-and-the-state-lock)
    before you run the controller.

## What the controller does

Four entry points manage runner hosts. Run each from the repository root.

| Entry point | Purpose | Changes resources? |
| --- | --- | --- |
| `src/runner_fleet.py` | Prints GitHub runner registrations, GCP VM counts, and the scale-out plan that restores a runner floor. | No |
| `src/spot_runner_manager.py` | Previews or provisions one runner host, or a strict heavy-runner pool. | With `--apply`, and with `--patch-live`, which changes a running VM and its runner containers at once without `--apply` |
| `src/spot_runner_autoscaler.py` | Runs the control loop that launches, drains, rotates, and deletes hosts. | Yes (see the warning at the top of this page) |
| `src/runner_floor.py` | Keeps one minimal floor host per target; the watchdog runs it. See [Runner floor keeper](#runner-floor-keeper). | With `tick --apply` and `replace-unmanaged --apply` |

The autoscaler runs the same loop in every mode. Each check does the following:

1. List the GitHub runners and the `oxidex-runners-*` VMs that belong to the
   controller's target.
2. Poll tracked repositories for jobs, so a lost host can be traced to its jobs.
3. Record observed VMs and write the state file.
4. Finish any pending host operation, such as a launch, drain, or delete,
   before it starts new work.
5. Clean up after lost hosts.
6. Apply the scaling mode: observe demand, then launch, replace, or retire at
   most one host.

The autoscaler launches new hosts by calling `src/spot_runner_manager.py
--apply` as a child process, so each launch also deletes the zone's
unattached, `READY` runner data disks that carry the manager's label and are
older than six hours. See [Apply the reviewed plan](provisioning.md#apply-the-reviewed-plan).
It names each host
`oxidex-runners-<region without hyphens>-<zone letter>-<target tag>-<time and random suffix>`
and gives its runners names of the form `gha-<host name>-<slot>`.

### Registered listeners and concurrent capacity

A runner host registers several GitHub listeners, but the listener count is not
the number of jobs that can run at once. Registered listeners only decide how
many jobs GitHub can assign to the host. On a tiered host, the job-started hook
asks the host admission service to reserve memory before the job runs. The
reservation, not the listener count, limits concurrent work. See
[Admission and memory leases](#admission-and-memory-leases).

## Choose a scaling mode

Select the mode with `--scaling-mode`. The command-line default is `resource`.
Pass `--scaling-mode tiered` explicitly for production use.

| Mode | Evidence | Behavior |
| --- | --- | --- |
| `tiered` | Per-host CPU and memory windows, input/output (IO) pressure, queue delay, job timings, and out-of-memory (OOM) events | Chooses host shapes and listener density from the tier ladder. Missing or incomplete evidence holds the current fleet. |
| `resource` | Per-host CPU and memory only | Adds or removes uniform hosts after two five-minute checks. Launches use one fixed shape class (32 vCPU and at least 110 GiB of RAM, with 8 listeners). This mode is the default. |
| `legacy` | Runner occupancy (online and busy listener counts) | Adds a host while more than half the listeners are busy and the online runner count is at or below `--minimum-runners`, and removes one after two low checks. |

Use tiered mode. Resource and legacy modes can delete an overpriced host even
while jobs are active, unless you enable
[make-before-break rotation](#price-rotation-and-make-before-break). Tiered mode
normally launches the replacement first and drains the old host only after the
replacement is registered and healthy. When the five-host, 80-vCPU, or
100-slot ceiling leaves no room for the overlap, it drains the old host first,
and only when the remaining hosts can carry current demand.

### Polling and host-count rules

- `resource` and `tiered` modes require `--interval-seconds 300` (the default).
  The controller raises an error for any other value. `legacy` mode accepts
  any positive interval.
- `--maximum-hosts` defaults to 5 and must be positive. In `tiered` mode it
  cannot exceed 5.
- Resource-mode growth and shrinkage need two consecutive five-minute checks.
  A check that has no metrics breaks the streak.
- In `tiered` mode the controller sleeps for the interval minus the time the
  check spent after sampling, so successive samples stay about five minutes
  apart.
- CPU and memory metrics come from GCP monitoring. Runner provisioning in
  `resource` and `tiered` modes installs the metrics-only Ops Agent. The runner
  VMs' service account needs `roles/monitoring.metricWriter` and the VMs need the
  `monitoring.write` or `cloud-platform` scope, or the agent can't publish memory
  metrics. Without complete metrics, tiered mode holds, and resource mode waits.

### Tiered mode

A tier is an allowed host shape with a listener range and a strict total daily
cost cap. The cap covers compute plus boot and data storage.

| Tier | vCPU | RAM, GiB | Initial / maximum listeners | Daily cap, USD | Concurrent jobs at the 15 GiB default reservation |
| --- | ---: | ---: | ---: | ---: | --- |
| `small` | 4 | 16 | 4 / 8 | 0.60 | Does not fit |
| `starting` | 8 | 16 | 8 / 8 | 1.25 | Does not fit |
| `expanded` | 8 | 32 | 8 / 16 | 1.25 | 1 |
| `large` | 16 | 64 | 20 / 20 | 3.00 | 3 |
| `bulky` | 32 | 128 | 16 / 20 | 4.50 | 8 |

The last column comes from the controller's own planning check, which assumes
2 GiB of host overhead and the initial listener count. The tier table does not
quote current offers. A tier also needs an affordable offer with SSD-backed data
storage (`pd-balanced`, `pd-ssd`, or `hyperdisk-balanced`). When the job
reservation does not fit a tier, the controller skips that tier and moves up the
ladder. Pass `--tier-job-memory-gib` to match your workload.

**Topology limits.** A tiered fleet has at most five hosts, 80 vCPU, and 100
listener slots in total, counting each host's tier maximum and including any
replacement that overlaps a source. `starting` is valid only as the sole host.
In a fleet of more than one host, every host before the last is `large` or
`bulky`, and the last host is `small`, `expanded`, `large`, or `bulky`: a
multi-host fleet ends in a `small` or `expanded` tail host or in another
`large` or `bulky` host. An unaffordable, unavailable, or incompatible
target holds the operation instead of selecting an arbitrary fallback.

**Growth and shrinkage.** The planner reads absolute demand, not fleet averages:

- One host over 75 percent CPU or memory, or a compatible queue delay or
  job-duration regression, grows the fleet when the signal holds in two
  distinct windows. Only a host OOM grows it after a single observation, and
  only when the job inventory and the host events from every host are
  complete.
- Six low windows, no pending compatible jobs, healthy IO, and projected peaks
  that fit the smaller topology shrink the fleet. The projection keeps CPU peaks
  below 60 percent and RAM peaks (plus 25 percent and 2 GiB per host) within
  the target RAM.
- A completed tier migration starts a 900-second cooldown. A host OOM bypasses the cooldown.
- A queue delay with CPU and memory headroom first raises listener density
  before it adds a host: it replaces the tail host with one of the same tier
  that registers four more listeners, up to the tier maximum. A `starting` tail
  is already at its maximum, so it can't use this step.
- Incomplete job inventory, missing metrics, or unhealthy IO pressure holds
  optimization.

A tier change runs as a migration. The controller launches the replacement,
waits for two healthy complete windows, and drains the source only when fresh
demand fits the target. Pass `--migrate-oversized` to let the controller
gradually replace existing hosts whose shape is not a tier.

## Set the runner floor

`--minimum-runners` sets a lower bound that scale-down must preserve for the
controller's target. It does not make the controller launch hosts to reach the
floor. It defaults to 1 in both
`src/spot_runner_autoscaler.py` and `src/runner_fleet.py`, and the autoscaler
rejects values below 1. Earlier versions defaulted to a larger floor, so pass
the flag explicitly if you relied on the old default.

How each mode uses the floor:

- **`resource` and `legacy`:** the controller records the floor and derives the
  fewest hosts whose expected listener slots reach it. A scale-down removes a
  host only if the remaining hosts still hold at least that many listener slots
  and the host count stays over the derived minimum. In `legacy` mode the
  floor also bounds growth: the controller adds a host only while the online
  runner count is at or below the floor and more than half the listeners are
  busy.
- **`tiered`:** the controller accepts and validates the flag, and the LaunchAgent
  stores it, but the tiered planner does not read it. A tiered fleet never
  shrinks below one host, and an empty fleet launches a first host. In `resource` and `legacy` modes, an empty or all-offline fleet doesn't trigger a launch by itself: only lost-host restoration relaunches hosts.

The floor is per controller, and each controller serves one target. A target is
either an organization (for example `swack-tools`) or a repository
(`owner/repo`):

- `--target` selects the target. Without it, the controller uses `--org`
  (default `swack-tools`).
- Launched hosts carry a `spot-target` label. A controller counts, scales, and
  rotates only the hosts that carry its own target label. Unlabeled hosts from
  earlier versions belong to the organization target named by `--org`.
- The controller binds its state file to one target and refuses a state file
  that another target created. Use a separate `--state-file` for each target.
- Organization targets register runners in the runner group your controller
  uses, which is the `--runner-group-id` default that `spot_runner_manager.py`
  uses. The manager requires the group to be
  repository-scoped and enabled for `<org>/oxidex`. Repository targets have no
  runner groups.

The [dashboard](dashboard.md) has its own scaling profile with a separate
minimum-runner setting. Changing it does not change `--minimum-runners`.

To preview the floor against live registrations without changing anything, run
`src/runner_fleet.py` with the same `--minimum-runners` and `--runners-per-vm`
values. It prints the online and registered runner counts and the scale-out
plan.

`--minimum-runners` only limits scale-down. To keep a host running for a target
without a workstation, use the [runner floor keeper](#runner-floor-keeper).

## Runner floor keeper

The runner floor keeper, `src/runner_floor.py`, keeps at least one minimal Spot
runner host for each target in `SPOT_RUNNER_FLOOR_TARGETS`. The
[control VM watchdog](edge-dashboard.md#watchdog) runs one floor step after its
control VM step on every tick, so the floor holds while no operator workstation
runs. The keeper only guarantees the floor. Scaling beyond it, drains,
rotations, and lost-host cleanup stay with the autoscaler.

### Floor targets

`SPOT_RUNNER_FLOOR_TARGETS` lists the targets, separated by semicolons. Each
entry is a target, a colon, and comma-separated options:

```text
swack-tools:secret=spot-floor-github-<account>,account=<account>,group=<runner group ID>;<org>:secret=spot-floor-github-<other account>,account=<other account>;<owner>/<repo>:secret=spot-floor-github-<other account>,account=<other account>,labels=ubuntu-latest
```

| Option | Meaning |
| --- | --- |
| `secret` | Required. The Secret Manager secret that holds the GitHub token of the account that administers the target. It names a secret and never holds a token: the parser refuses a value that looks like one. Targets that one account administers share its secret. |
| `account` | The GitHub account that `gh auth token --user` reads on a workstation, for `status` and `replace-unmanaged`. |
| `group` | Organizations only. The runner group that the floor host's runner joins. The keeper requires a group with selected-repository access, and it refuses the Default group. Without `group`, the runner joins the Default group. Personal accounts support only repository runners, so a repository target has no group. |
| `labels` | Extra scheduling labels joined with `+`, such as `ubuntu-latest`. Every floor runner also gets `spot`, its machine type, and its region. The keeper records the extra labels in the host metadata, so a later live patch keeps them. Labels compare without case, as on GitHub: the parser refuses a label listed twice in another case, and one that repeats a label every runner gets. At most 16 extra labels, the most the host metadata records. |
| `ram` | The least RAM in GiB, from 4 to 32. The default is 16. |

The configuration is an environment variable of the watchdog service, not an
object in the state bucket. Each change is a new service revision that the deploy
dry run shows for review, and the deploy needs the target-to-secret map anyway
to mount each secret and grant access to it. The watchdog's own account can
write the state bucket, so a configuration stored there could be changed by
the process that it configures.

### Floor host shape and price

A runner gets 4 vCPU (`runner_count = host_vCPU // 4`; see
[Runner sizing](provisioning.md#runner-sizing)), so the smallest host that holds
one runner has 4 vCPU. The keeper prices every 4-vCPU shape of a supported x86
family with at least the target's RAM in the regions of
`SPOT_RUNNER_FLOOR_REGIONS`, from the billing catalog, the same source as the
manager's price guard. The total is compute plus a 40 GiB boot disk and a
100 GiB data disk, with the cheapest SSD-backed data disk that the manager
would choose. The keeper takes the cheapest offer strictly below
`SPOT_RUNNER_FLOOR_MAX_DAILY_USD` (default 1.50 dollars a day). The manager's
live price and quota check then runs again before anything exists.

To reduce correlated preemptions, the keeper spreads targets across zones. A
zone that no other floor target's host uses wins when it costs at most 1.5
times the cheapest offer, and among those, a region that no other floor target
uses wins. A zone where a launch failed is skipped for six hours.

A floor host runs one runner, named `gha-<host name>-1`. It has no tier and
no admission service. It runs with no service account and no access scopes, so
CI jobs on it can't get Google credentials from the metadata server. Its name,
labels, and disks match the autoscaler's launches: `oxidex-runners-*`, the
target's `spot-target` label, and a `runner-data-*` data disk. An autoscaler
for the same target therefore counts it as one of its own hosts.

### What the keeper decides

Each tick plans one step for each target from the Compute Engine inventory,
the target's runner registrations, and the target's floor record:

| Situation | Step |
| --- | --- |
| A managed host of the target is provisioning, staging, or repairing, or it's running and has a runner online or started less than 30 minutes ago | Nothing |
| A managed host is stopping or suspending | Wait for it to settle |
| A launch or start is in progress | Advance it (see below) |
| Earlier launches failed | Wait: 5 minutes after the first failure, doubling up to an hour |
| The keeper's own floor host is stopped, and every start guard holds | Start it |
| Anything else, including no host | Launch a floor host |

A running host whose runners all stayed offline for 30 minutes doesn't meet
the floor: the keeper launches another host next to it and leaves the broken
one for the autoscaler or an operator. A step that only waits takes no lease
and writes nothing, so a steady floor costs a few reads a tick.

Floor hosts, like every autoscaler launch, use
`--instance-termination-action=DELETE`, so a Spot preemption deletes the VM and
its auto-delete disks, and the next tick launches a replacement. A stopped
floor host comes from a guest shutdown or a host error, not a preemption. The
keeper starts it only when all of these hold:

- It's the host that the keeper recorded, with the same instance ID.
- Its `startup-script` metadata is gone. The manager removes the script, and
  its one-use registration token, after a completed bootstrap, so a start can't
  replay the fresh-host bootstrap. With the script still present, a start would
  replay it.
- Every runner of the host is still registered, offline, idle, and carries its
  scheduling labels. A drain deregisters runners or removes their labels, and
  the host job-start cordon marker lives in `/run`, which a reboot clears. A
  start would quietly return a host that was being retired to service.
- No earlier start of that instance failed to bring its runners back within
  15 minutes.

Otherwise the keeper launches a new host and leaves the stopped one in place
for the autoscaler's lost-host cleanup or for an operator. A stopped host
still bills its disks.

### Launch steps

A launch spans several ticks, so no tick waits for a bootstrap:

1. **Creating.** The keeper picks the offer, runs the manager's price and quota
   check, and checks the runner group. It saves the launch, with the host and
   disk names, before the registration token exists. Then it creates the data
   disk and the VM through `runner_manager`. The startup script carries the
   token, and the token never reaches the floor record.
2. **Booting.** Once the VM exists, the keeper records its instance ID. Later
   ticks wait until GitHub lists the host's runner online. No SSH is needed:
   the startup script itself refuses to register runners unless Docker and
   containerd run on the data disk.
3. **Ready.** The keeper first removes the `startup-script` metadata and the
   token. Then it repairs the runner's labels if needed and records the host's
   patch as `none`.

A launch that has no runner online 30 minutes after the create is abandoned,
unless its runner is running a job: the keeper deletes that VM, only if the
VM still has the recorded instance ID and a fresh runner list, read right
before the delete, shows no runner of it online or busy. Then it removes the
launch's own offline registrations. A VM that never appears within 20 minutes of the create, or one
that disappears during the bootstrap, ends the launch the same way. Each
failure starts the backoff. A create that was given up can still land, for
example after a call timed out. When its VM appears within an hour, the keeper
adopts it and finishes or abandons it like any other launch, so its token
never stays in the metadata. If another host already serves the floor by then,
the late VM is excess: the keeper records its instance ID, removes its runner
registrations, and deletes it, but only while none of its runners is online or
busy. Otherwise it holds and logs why, and after the hour it leaves the VM for
the autoscaler or an operator.

### Coordinate with the autoscaler

Each target has its own floor state object and its own lease in the state
bucket, next to the control VM state, with the same generation-checked writes and lease
rules as the [control VM lease](edge-dashboard.md#shared-state-and-lease). The
keeper takes a target's lease only for a step that acts, and skips the target
while someone else holds it.

Set `SPOT_RUNNER_FLOOR_STATE_URI` in the environment of an autoscaler that
manages a floor target, to the same `gs://<bucket>/runner-floor` location that
the watchdog uses. The autoscaler then holds its target's floor lease while it
runs and checks it every 5 seconds, so the keeper leaves that target alone. While
the autoscaler runs, it alone keeps the target's floor: `tiered` mode launches
a first host into an empty fleet, but `resource` and `legacy` modes only
restore lost hosts. The lease covers the controller's whole run, not each
check, because both controllers would otherwise replace one lost floor host:


- At start-up, the autoscaler waits for the lease, checking at each interval.
  It also waits while the keeper has a launch or start in progress on the
  target, so that the keeper can finish it and remove its registration token.
  With `--once`, it logs why and exits instead. A running keeper ends every
  such operation by its own deadline. Once that deadline has passed by 15
  minutes, for example because the target left `SPOT_RUNNER_FLOOR_TARGETS`, the
  floor is off, or the watchdog is paused, the autoscaler stops waiting. It
  treats the host as an ordinary managed host, removes a leftover registration
  token from the recorded instance, and changes nothing else: it deletes
  nothing and leaves the floor record to the keeper.
- Every fleet mutation first proves that the lease is still held. When it
  can't, for example after a workstation slept past the lease, the mutation is
  held and the controller exits with status 75 at its next check, so a restart
  takes the lease again before it acts.
- A launch runs `src/spot_runner_manager.py` in its own process group, fenced
  by the lease. The first check that can't prove the lease kills that whole
  group at once, including its `gcloud` processes, and the launch stops. The
  lease keeps at least 11 minutes left while it's proven, and nobody can break
  it until a minute after it expires, so the manager is gone before another
  controller may act. The controller's other provider calls time out within
  10 minutes.
- Every child process that can change something, `gcloud` other than reads,
  `gh` other than reads, `ssh`, and the deployment manager, proves the lease
  immediately before it starts, whichever part of the controller starts it.
  An unknown command counts as a change.
- `--dry-run` takes no lease.

An autoscaler without `SPOT_RUNNER_FLOOR_STATE_URI` doesn't coordinate. Two
controllers then act on one target at once, and both can launch a first host
into an empty fleet.

The floor host shape matches the `small` tier, but a floor host has no
admission service. A tiered autoscaler for a floor target replaces it only
through its own tier migrations.

### Commands

Show each target's plan, floor record, hosts, and runners. With `--offers`,
it also prices the cheapest floor host offers. Both read only:

```bash
PYTHONPATH=src python3 src/runner_floor.py status --offers
```

Plan one step for each target without a lease or a write, then run it:

```bash
PYTHONPATH=src python3 src/runner_floor.py tick
PYTHONPATH=src python3 src/runner_floor.py tick --apply --target '<owner>/<repo>'
```

On a workstation, the keeper reads each target's token with `gh auth token
--user <account>`, or with `--github-user` for every target. It never reads a
token from an environment variable.

### Replace an unmanaged host

`src/runner_floor.py replace-unmanaged` retires a runner host that was created
outside these tools, make before break. It runs only when an operator starts
it, never from the watchdog, and it supports repository targets only. Without
`--apply`, it reads only and prints the plan:

```bash
PYTHONPATH=src python3 src/runner_floor.py replace-unmanaged \
  --target '<owner>/<repo>' --instance '<old VM>' --zone '<zone>' \
  --old-runner '<runner 1>' --old-runner '<runner 2>'
```

The preview lists runner names that look like the old host's, the scheduling
labels the managed host must carry, the labels that don't move, and the steps.
The command attributes a runner to the old VM when its name contains the VM
name or its labels name the VM's machine type and region, whatever the flags
say. `--old-runner` must name exactly that set: every attributed runner, and
no other. The preview refuses when it doesn't, when the target's `labels=`
lacks a label that the old runners carry, or when the old VM is a managed
host. The old machine-type and region labels don't move; check that no
workflow selects them, then pass `--accept-dropped-labels`.

With `--apply`, the command holds the target's floor lease, so the watchdog
skips the target, and saves each phase before it acts, so a rerun resumes:

1. It provisions the managed floor host with the keeper's launch steps, unless
   the target already has a running managed host.
2. It verifies that every runner of the managed host is online with every
   required label, and it records the old runners' labels.
3. It places the host job-start cordon on the old host over
   `gcloud compute ssh`. Then `drain_runners` removes the old runners' custom
   labels, waits until they're idle, and deregisters each one after a fresh
   idle read.
4. When a fresh read shows every old registration gone, no other runner is
   attributed to the VM, and the managed host is still ready, it deletes the
   old VM,
   if the VM still has the instance ID recorded at the start, with its
   auto-delete disks. It keeps every other disk, and the preview names both
   kinds. Right before the delete, it reads the runners, the managed host, and
   the old VM again. It refuses while any runner attributed to the VM is still
   registered, or when the VM is now another instance. A VM that's already gone
   counts as deleted. Then it waits until the VM is gone.

A rerun with the same `--instance` resumes the recorded phase, even after the
old runners or the old VM are gone. It refuses instead when the target's
`labels=` no longer includes a label that the old runners carry. If the old VM
disappears during the drain while its registrations remain, the command
confirms with a fresh read that the VM is gone, then removes each registration
that is offline and idle on a fresh read. It holds while any of them is online
or busy. Before every drain step, the command checks
the managed host again. If the managed host loses a runner before any old
runner is deregistered, the command releases the cordon, then restores the old
runners' labels, and stops. Until the release is confirmed, the old runners
stay without labels and the command retries, because a host that is still
cordoned refuses every job that its labels would route to it. If the managed host stops being ready after
an old runner is deregistered, during the drain or before the delete, the
command keeps the old VM, because it may be the only host that can recover. It
provisions the managed floor host again with the keeper's launch steps, then
continues where it stopped. The command saves the cordon's owner token before it
calls the cordon, so a rollback also releases a cordon whose result was lost,
for example to an SSH timeout. After a deregistration, the old host can't be
put back, so the command waits for the managed host and then goes on. A
deregistration whose result was never saved is settled from a fresh runner
list first: a runner that is gone counts as deregistered. A rerun resumes such
a replacement, even one recorded as aborted.

An old host whose runner containers lack the read-only cordon mount or the
cordon-aware job-started hook can't refuse jobs at job start. The command then
stops before it changes anything. `--label-strip-drain` instead drains by
removing the old runners' custom labels and waiting until they've been idle
for two minutes. Until the old runners are deregistered, a job that selects
only the built-in labels, such as `self-hosted`, can still start on the old
host, so check the workflows before you pass it.

## Price rotation and make-before-break

The controller checks each running Spot host against the daily cap. A host is
overpriced when its current compute plus storage cost reaches `--max-daily-usd`
(default 10 dollars). If the controller cannot verify a price, it keeps the
VM and retries on the next check.

What happens next depends on the mode:

| Situation | Behavior |
| --- | --- |
| `tiered` mode | The tier cap, not `--max-daily-usd`, defines overpriced. The controller starts a "tier price replacement" migration when its evidence is complete. It never uses delete-first eviction, and `--make-before-break` has no effect on runner hosts. |
| `resource` or `legacy`, `--make-before-break` | The controller rotates the host with the sequence below. Passing `--target` also selects rotation. |
| `resource` or `legacy`, default (`--no-make-before-break`) | The controller deletes the overpriced VM and its disks immediately, including any active jobs, and then launches a replacement. |

`--make-before-break` and `--no-make-before-break` set one option, so when you
pass both, the last one wins. The default is off. The `--manage-control-vm` option rotates the dashboard control
VM with its own sequence, which the
[edge hosting reference](edge-dashboard.md) describes.

In `resource` and `legacy` modes, a host that runs a
[runner patch](runner-patches.md) stays in service over the cap unless this
controller has a matching `--patch-repo`, because a replacement without the
patch would pass readiness while missing packages, mounts, and labels. The
controller logs the hold on both the rotation and the delete-first path. The
`tiered` mode has no such hold: a replacement gets the patch only when this
controller has `--patch-repo`.

### Rotation sequence

The outgoing host keeps running jobs until the replacement registers every
listener.

```mermaid
flowchart LR
    A["select: pick an offer under the cap"] --> B["provision: launch replacement"]
    B --> C["boot: wait for RUNNING"]
    C --> D["verify_readiness: all listeners online"]
    D --> E["cordon_drain: cordon old host, wait for jobs"]
    E --> F["deregister: remove old listeners"]
    F --> G["delete: delete old VM and disks"]
    C -. timeout or failure .-> X["abort: keep old host"]
    D -. timeout or failure .-> X
    E -. replacement lost .-> X
```

1. **Select.** The controller picks an offer that meets the cap. With no
   offer, it keeps the old host and tries again on the next check. The
   rotation starts at the provision step, and it returns to selection only
   after failures.
2. **Provision.** It launches the replacement. Three failed attempts return the
   rotation to selection with a different offer. A launch that misses its
   deadline (600 seconds) is abandoned: the controller deletes the VM, its disks, and the runner records.
3. **Boot.** It waits up to 600 seconds for the replacement to reach `RUNNING`.
4. **Verify readiness.** It waits up to 900 seconds until every expected
   listener of the replacement is online. The expected count comes from the
   launch sizing.
5. **Cordon and drain.** It cordons the old host (see the next section) and
   waits for busy runners and tracked jobs to finish. After 3600 seconds, the
   controller advances to deregistration. It still refuses to deregister while
   any runner is busy or a tracked job is active, and it returns to draining
   with a fresh 3600-second timer. The host cordon and remote build quiesce
   checks never time out.
6. **Deregister.** It re-reads each old runner and deletes the registration only
   if the runner is freshly idle. Any work sends the rotation back to the drain
   step.
7. **Delete.** It records the intentional deletion and deletes the old VM and
   its disks. Then it removes the temporary drain group.

During a rotation the maximum host count allows one extra host for the overlap.

### Rollback

The controller re-checks the replacement on every retirement step. If the
replacement is absent, replaced under the same name, not running, or has fewer
online listeners than expected (for example after a preemption), the controller
stops retiring the old host and restores it:

1. It restores each cordoned runner's labels and runner group, and deletes the
   temporary drain group.
2. It releases the host job-start cordon marker that the rotation placed, and
   any builder drain marker it placed. It removes only its own marker.
3. It aborts the rotation. It deletes the replacement only if the replacement
   is idle and offline; otherwise it schedules a drain and then a delete for
   the surplus host.

If the old host has already lost listeners to deregistration, the controller
cannot restore it. The rotation stays pending, keeps the old host cordoned, and
waits for the replacement to come back or provisions a new replacement.

## Drain and the host job-start cordon

A drain quarantines a host's runners, waits for CI work to finish, and then
removes the host. GitHub keeps the read-only `self-hosted`, `linux`, and `X64`
labels on every runner, so removing custom labels never stops a job that selects
only those labels. The controller isolates a host in one of two ways, depending
on the target.

### Repository targets

Personal repositories have no runner groups, so the controller cordons the host:

1. Over `gcloud compute ssh`, a lock-guarded root program first checks the VM's
   provider instance ID against the instance metadata.
2. The program checks every runner container. Each one must mount
   `/run/gha-runner-cordon` read-only, run the cordon-aware job-started hook,
   and not override the hook in its runner state.
3. The program writes the marker file `/run/gha-runner-cordon/draining` (a
   root-owned file) that holds the owner token.
4. The job-started hook (`runner/job-started.sh`) refuses every new job while
   the marker exists. It runs before admission and before any job step.

The controller deregisters runners only after a later observation finds the
marker in place and no `Runner.Worker` process on the host. The first
observation that places the marker never deregisters.

An idle listener can still accept an assignment during the drain. That job
fails before its first step with the annotation **Runner host is being
retired**. GitHub reports the failure and does not reassign the job, so re-run
it to schedule it on another runner.

A host whose containers lack the read-only mount or the cordon-aware hook (the
hook text carries the `GHA_RUNNER_CORDON_V1` capability string) cannot refuse
jobs. The controller holds its retirement and logs `Retirement held`. The host
keeps its labels and registrations until a cordon-capable host replaces it.
Rollback releases a marker only if its content matches the owner token that the
operation recorded.

### Organization targets

An organization drain moves the host's runners into a temporary runner group
named `spot-drain-<host name>-<instance ID>`. The group has `selected`
visibility, no repositories, and no public repository access. The controller
rejects a group that does not meet those conditions, strips the custom labels,
and deletes the group after the host disappears.

The fallback paths are:

- A rotation that cannot create or verify the isolated group cordons the host
  the same way as for a repository target, and holds the rotation if the host
  cannot refuse jobs.
- A drain that starts in `label_only` mode, as `legacy` scale-down does, also
  cordons the host first. If the host cannot refuse jobs, the drain switches to
  an isolated runner group. In `resource` and `tiered` modes, an organization
  `label_only` drain converts to an isolated group.

### Finishing a drain

A drain completes only when all of these conditions hold. The controller
re-checks them before it deletes anything.

- Every drained runner is online and idle, and the recorded runner identities
  match.
- The tracked CI jobs on the host are completed.
- Remote builds on the host are quiesced.
- A fresh read of each runner shows it idle, and the delete requests for the
  runners succeed.
- A fresh inventory shows the runner registrations absent.

The controller then deletes the VM with its attached disks and waits until the
VM and the runner registrations are gone. In `tiered` mode it first checks the
recorded disk identities, and a tier migration also confirms that each recorded
disk is gone. Stopping the controller does not drain a VM or
stop a job.

For operator procedures such as finding a stuck drain, see
[Operations and troubleshooting](operations.md).

## Admission and memory leases

Tiered runner hosts run a root-owned admission service (`host_admission.py`).
A runner container asks it for memory before a job starts, and Unix peer
credentials decide which lease belongs to which caller. A client cannot supply
its own budget or release another container's lease.

- **Budget.** The service reserves `max(2, host overhead) + 0.25 × listeners`
  GiB for the host, and the rest of the RAM is work memory. Concurrent jobs are
  the smaller of the listener count and the work memory divided by the job
  reservation.
- **Reservation size.** `--tier-job-memory-gib` sets the per-job cap and
  reservation. The default is 15 GiB. The help text calls it a verified
  compatible workload cap, not a measured peak. Use job measurements and OOM
  evidence to choose a value.
- **Runner hooks.** On a tiered host the job-started hook waits and retries
  every five seconds until the service grants the lease. The job-completed hook
  releases the lease, retrying until the service acknowledges, and then cleans
  the job target. If the Worker has already ended, the completed hook skips
  cleanup and exits with status 75.
- **Builder admission.** A remote build that the service refuses exits with
  status 75. The `reconcile-builders` command also exits with 75 while builders
  run.
- **Ledger.** The lease ledger is `/var/lib/gha-admission/leases.json` on the
  host. It is for reference only. Never edit or delete it to bypass a hold or a
  drain.

### Heavy-runner pools

`spot_runner_manager.py --heavy-runner-pool 2` or `3` provisions a strict
CI-only pool. Each runner gets 8 vCPU and 32 GiB with no extra swap, the pool
uses the `spot-heavy32` label, and the admission work budget equals 32 GiB per
runner. The service checks the actual container memory, swap, and CPU limits
before it grants a lease. The runner group must allow public repositories and
select only the organization's `oxidex` repository. A label alone does not
enforce these limits.

The controller protects a host that has any `spot-heavy32` runner. While a
heavy route has no verified replacement, tier changes, price replacement, and
automatic launches hold, and a retirement logs `Retirement held: live heavy
route has no verified replacement`.

### Out-of-memory handling

The controller reads kernel and cgroup events from each host and keeps
separate OOM obligations for the host, runner, builder, and unknown cases. Exit
status 137 alone proves neither a host nor a container OOM. The planner reacts
as follows:

| Evidence | Reaction |
| --- | --- |
| Host OOM | Grow the fleet after one observation and reset listener density. |
| Container OOM with host headroom | Double the reservation for future jobs, up to 95 percent of the host's observed memory envelope, and raise the cap only on containers that hold no lease. A busy container keeps its cap until it is lease-free. If the new cap exceeds the host envelope, migrate to a larger tier. |
| Container OOM with host memory over 75 percent | Treat it as host pressure. |
| Unattributed OOM | Hold until identity evidence arrives. |

`oom_disposition.py` is a maintainer-only helper. The controller never runs it.

## Recover lost hosts and failed jobs

A managed host that disappears without an intentional deletion is a loss. The
controller records it and then cleans up before it restores capacity:

- It verifies the recorded VM and disk identities, deletes the stopped VM and
  its detached disks, and checks that the disks are gone. A foreign, recreated,
  attached, or unrecognized disk keeps recovery pending. Incomplete inventory is
  never proof of absence.
- It removes stale offline, idle runner registrations of the lost host.
- Then it restores capacity. In `resource` and `legacy` modes, it launches
  replacements up to the saved restoration target. In `tiered` mode, the tier
  migration restores the previous committed topology, using allowed tiers.

The controller tracks jobs so it can rerun work that a host loss killed.
`--job-repository OWNER/REPO` selects the repositories to track and can repeat.
The default is the repository target itself, or `<org>/oxidex` for an
organization target. The controller reruns a job only when the job failed on a
lost VM and GitHub reports a runner-loss error, such as a lost connection, or a
runner that stopped responding. Successful jobs, cancellations, and ordinary
code failures never qualify. The controller records each request before it sends
it, and it checks an uncertain response for a newer attempt before it submits
again. A loss that stays unconfirmed for a day expires without a rerun.

## State, locks, and logs

### State and lock files

- `--state-file` defaults to `~/Library/Application Support/Spot Runner
  Autoscaler/state.json`. The controller writes it atomically.
- The controller holds an exclusive lock on `<state file>.lock`. A second
  controller on the same state file stops with `Another controller owns state`
  and the path.
- State survives restarts and holds host operations, rotations, migrations,
  recovery obligations, job tracking, and OOM obligations. A registration,
  drain, cleanup, or uncertain prior operation blocks another host change.
- If the controller can't read the state file, it logs `Ignoring unreadable
  autoscaler state` and starts from empty state, which drops pending host
  operations, loss obligations, and job tracking on the next write. Keep a
  backup before you restart a controller after a disk problem.
- Never delete or hand-edit the state file, the lease ledger, or a runtime
  update journal to bypass a hold or a drain.

### Logs

- `--log-file` defaults to `~/spot-ghrunner.log`. The controller appends to it.
- The log records a spend rate and project cost summary about once an hour. Pass
  `--billing-export-table` or set `GCP_BILLING_EXPORT_TABLE` to include billing
  export totals.
- The LaunchAgent writes standard output and standard error to
  `~/Library/Logs/spot-runner-autoscaler.log`.

The controller loads `.env` from the repository root. See
[Configuration](configuration.md) for the variables, and
[Commands](commands.md) for the complete option list.

### Dry run, once, and the state lock

- `--dry-run` prints a read-only tier decision as JSON: the decision, resource
  evidence, workload signals, candidate offers, and any pending migration. It
  reads provider, GitHub job, and metrics data, and it reads kernel and
  cgroup events from each runner host over `gcloud compute ssh`, which can add
  SSH keys to your project or OS Login settings on first use. It runs the tier
  planner whatever `--scaling-mode` says. It never launches, drains, or
  deletes, never runs job recovery, and never writes controller state. It still
  creates the state directory and the state lock file, appends to the log file,
  and lets `gcloud` create its local SSH key and known-hosts files. With
  `--manage-control-vm`, it also prints that VM's plan as a text line before the
  JSON and creates the project control VM lock file.
- A dry run takes the same exclusive state lock as a running controller, so it
  fails with `Another controller owns state` while the LaunchAgent controller
  uses that state file. Pass a separate `--state-file` to preview next to a
  running controller. A new state file has no saved evidence history.
- `--once` runs one check and exits. Without `--dry-run`, that check can
  provision or retire resources.
- `--install-launch-agent` is not a dry-run. The controller writes and
  bootstraps the LaunchAgent before it reaches the dry-run branch.

### Install the launch agent { #run-as-a-macos-launchagent }

`--install-launch-agent` writes
`~/Library/LaunchAgents/com.swacktools.spot-runner-autoscaler.plist` and runs
`launchctl bootstrap` for the current user. It then exits without running a
check. The agent starts at login and restarts with `KeepAlive`.

!!! warning
    Installing the agent starts the live controller loop. The plist records the
    options you pass at install time, such as the mode, project, target, floor,
    tier memory, builder selection, tracked repositories, and
    `--make-before-break`. It omits `--dry-run` and `--once`, so a dry-run
    preview that includes `--install-launch-agent` still installs a live agent.
    The installer ignores the `launchctl` exit status, so confirm the service
    state yourself.

The plist points to this checkout and the Python interpreter that installed it.
Editing the source or changing a command-line default does not update an
installed plist. Linux has no installer; run the foreground controller under
your service manager. For stop, restart, and replacement steps, see
[Operations and troubleshooting](operations.md).

## Related pages

- [Runner patches](runner-patches.md): packages, mounts, and labels that every
  launched host receives.
- [Dashboard](dashboard.md) and [Edge hosting](edge-dashboard.md): the web view
  of the fleet and the control VM that hosts it.
- [Commands](commands.md): option reference for every entry point.
- [Configuration](configuration.md): environment variables and `.env` files.
- [Security model](security.md): admission, cordon, and access boundaries.
- [Operations and troubleshooting](operations.md): runbooks for drains,
  rotations, and recovery.
