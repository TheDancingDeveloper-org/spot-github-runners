# Edge hosting and the control VM

The web dashboard runs on one small VM, the control VM, behind a Cloudflare Worker at
`https://spot.swacktech.com`. The control VM is a Spot VM by default, or the always-on free-tier e2-micro
described in [Use the free-tier control VM](#use-the-free-tier-control-vm). The owner, GitHub user `swackhamer`, can sign in, and so can the
administrators that the owner adds on the **Admins** page. Each of them must administer every
controlled repository unless global administrators are enabled, which they are by default.

## Architecture

| Component | Location | Role |
| --- | --- | --- |
| Cloudflare Worker | `cloudflare/worker.js` | Verifies the session cookie's signature and expiry, proxies to the active control VM, and returns a wait response while the control VM is down. |
| Workers KV | Binding `SPOT_KV` | `origin:active` names the active slot. `dashboard:state` holds the latest dashboard state snapshot. |
| Control VM | `spot-control-a` or `spot-control-b` | Runs the dashboard container and a `cloudflared` tunnel. Created by `src/control_vm.py` as a Spot VM or a free-tier e2-micro. |
| Tunnel hostnames | One per slot, under the domain in `SPOT_CONTROL_ORIGIN_DOMAIN` | One Cloudflare Tunnel per slot, protected by a Cloudflare Access service token. |
| Startup script | `cloud-init/control-vm-startup.sh` | Fetches secrets, pulls or builds the image, and starts both containers on every boot. |
| Dashboard image | `dashboard/Dockerfile` | Built on the VM from a pinned commit, or pulled from `ghcr.io/swack-tools/spot-dashboard`. |
| Watchdog | `watchdog/Dockerfile`, `src/control_watchdog_deploy.py` | A serverless HTTP service that runs one lifecycle step of `src/control_vm.py` every 3 minutes against shared state in a storage bucket. See [Watchdog](#watchdog). |

The dashboard listens only on `127.0.0.1:8787`. The control VM has no inbound firewall rule for it.

## Sign-in

The dashboard uses a GitHub OAuth 2.0 app with the authorization-code flow and PKCE. Sign-in succeeds only
when all of these are true:

- The user is the owner, GitHub login `swackhamer` with the numeric user ID fixed in code, or an administrator
  that the owner added. Added administrators are matched by numeric user ID only, so a renamed
  account keeps access and a new account that takes an old login gets none. See
  [Dashboard administrators](#dashboard-administrators).
- The user is a global administrator, or has administrator permission on every controlled target.
  `DASHBOARD_GLOBAL_ADMINS` lists global administrators as `login:numeric_id` entries and defaults to
  the owner's `login:numeric_id` entry, which is also the only identity it may list. While it lists the owner, added
  administrators are global administrators too. A global administrator skips the per-target checks, and
  those skipped checks are never recorded in its session, so setting `none` takes effect for existing
  sessions too. On a managed control VM it travels as the `dashboard-global-admins` metadata, like the
  other target policy settings. Set it to `none` to require administrator permission on every target from
  everyone, added administrators included. Targets are the union of
  `DASHBOARD_CONTROLLED_TARGETS`, targets saved in the dashboard, and every `repos/<owner>/<repo>.yaml`
  file in the config repository. The config repository is `--config-repo`, then `SPOT_CONFIG_REPO`,
  then `<org>/spot-runners-config` with the organization from `--org` (default `swack-tools`).
- The dashboard can list the config repository. If the listing fails, sign-in fails closed with a 403.

The session cookie `__Host-spot_session` is HMAC-signed with `DASHBOARD_SESSION_SECRET`. The Worker and
every control VM share that secret, so sessions survive VM replacement. The `__Host-` prefix makes browsers
reject the cookie unless it is `Secure`, has `Path=/`, and has no `Domain`, so another subdomain of
the same domain can't set or shadow it. The sign-in `state` cookie is `__Host-spot_oauth_state`.

The Worker only authenticates the cookie: it checks the signature, the expiry, the cut-off below, and
that the payload holds a well-formed GitHub login and numeric ID. It doesn't decide who may sign in. The
dashboard decides that on every request from its durable state, because the list of added
administrators changes at run time. A copy at the edge would lag behind, since Workers KV is eventually
consistent. Only the dashboard mints sessions, and only after it authorized that identity.

Removing an added administrator ends its sessions on their next request, because the dashboard checks
membership on every request. Otherwise a session can't be revoked on its own, and a lost GitHub
administrator permission takes effect at the next sign-in. To sign everyone out, set a cut-off time that
rejects every session issued before it:

1. Choose the cut-off, for example `NOW="$(date +%s)"`. It can't be in the future.
2. Set `SESSION_NOT_BEFORE = "<NOW>"` in `cloudflare/wrangler.toml` and deploy the Worker.
3. Set `DASHBOARD_SESSION_NOT_BEFORE=<NOW>` in the autoscaler's environment file. Control VMs that the
   autoscaler creates from then on get it as the `dashboard-session-not-before` instance metadata value.
4. Add the same metadata value to the running control VM, then reboot it. The startup script reads the
   value at every boot and writes `DASHBOARD_SESSION_NOT_BEFORE` to the dashboard environment file:

   ```bash
   gcloud compute instances add-metadata spot-control-a --zone=ZONE \
     --metadata=dashboard-session-not-before="$NOW"
   ```

The startup script stops with an error when the value isn't Unix seconds or is in the future, because the
dashboard would refuse to start. Rotating the dashboard session secret in Secret Manager together with the Worker's
`SESSION_SECRET` also signs everyone out.

Sign-in routes are rate limited per browser IP and globally, and answer HTTP 429 with `Retry-After` when
over the limit. The Worker passes the browser IP in `X-Spot-Client-Ip` and drops any copy that the
browser sends. The dashboard trusts that header only from a loopback peer, the local `cloudflared`.

The Worker reads every request body with a hard cap before it looks up the origin or forwards anything.
Sign-in routes under `/auth/` and `/api/auth/` allow 16 KiB, and every other route allows 256 KiB, the
dashboard's `MAX_JSON_BODY_BYTES`. A `Content-Length` over the cap gets HTTP 413 before the body is read.
A body without a length, such as a chunked upload, is counted as it streams and cut off with HTTP 413
past the cap. A malformed `Content-Length` gets HTTP 400. The Worker forwards the body it read
unchanged, so a retry with the same `Idempotency-Key` sends the same bytes.

### Dashboard administrators

The **Admins** tab appears only for the owner, and `/api/admins` answers HTTP 403 with
`"code": "owner_required"` to every other signed-in caller. That includes added administrators and the
local dashboard token. A signed-out caller gets HTTP 401. The tab lists the owners, which are read-only, and the added administrators, each with a
**Remove** button.

| Route | Purpose |
| --- | --- |
| `GET /api/admins` | Lists the owners, the added administrators, and the last 20 changes. |
| `POST /api/admins/add` | Adds `{"login": "<github username>", "confirm": true}`. Needs an `Idempotency-Key`. |
| `POST /api/admins/remove` | Removes `{"uid": <numeric user ID>, "confirm": true}`. Needs an `Idempotency-Key`. |

- Owners are fixed in code and can't be added or removed here. At most 50 administrators can be
  added.
- Adding a user looks up the login with `GET https://api.github.com/users/<login>`, using
  `SPOT_CONFIG_GH_TOKEN` or `GH_TOKEN` when set. The dashboard stores the login, the numeric user ID,
  who added it, and when. It refuses an invalid login and an account that isn't a user, such as an
  organization, with HTTP 400. An unknown login gets HTTP 404, and a user who is already an owner or an
  administrator gets HTTP 409.
- When GitHub fails, the add answers HTTP 424 with `"retry_same_key": true` and changes nothing, so the
  browser retries with the same key. A lookup that returns a different account also answers HTTP 424,
  without `retry_same_key`.
- A retry with the same `Idempotency-Key` replays the stored answer without a second lookup. The same
  key with a different body gets HTTP 422.
- Every change is logged to the `spot_dashboard.auth` audit log and kept in a history of the last 50
  changes.
- The list lives in dashboard state, so it's part of every KV snapshot, freeze, and import, and it
  survives a control VM cutover. Like every change, it gets the wait response while the dashboard is
  frozen, unhydrated, or not the active slot.
- If the state can't be read, only owners can sign in.

### Controlled target changes

`POST /api/auth/targets` changes the targets saved in the dashboard, and only a GitHub session can call
it. A list that, after case-insensitive de-duplication, is identical to the saved list in the same order
answers HTTP 200 with `"status": "unchanged"` and starts no sign-in. A reordered list counts as a change.
Otherwise the request doesn't change anything yet. It answers HTTP 202 with `"status": "reauth_required"` and
either an `authorize_url` or a device code. The change commits only after that new GitHub sign-in shows
that the same GitHub account administers every static, config repository, current, and proposed target.
That check skips the cache of earlier administrator checks.

- A change can't remove a target that you no longer administer, and it can't add one that you don't.
- The change commits only if the saved list still matches the list it started from. Otherwise the
  dashboard answers HTTP 409 and you submit it again.
- The commit goes through the same freeze, reconcile, and active-slot checks as every other change.
- Each committed change is logged and kept in a history of the last 50 changes.
- A list whose session cookie would exceed browser limits is refused with HTTP 400. At most 32
  targets can be saved.

### Unreadable state file

If `/var/lib/spot-dashboard/state.json` exists but can't be read or parsed, the dashboard first writes the
recovery hold `state.json.recovery-pending` (mode 0600, flushed to disk). It then renames the state file to
`state.json.corrupt-<UTC time>` with mode 0600 and never deletes it. While the hold exists, every start fails
closed, even when the state path is empty after a restart. The dashboard removes the hold only after the
recovered state is written to disk. It then recovers from the latest KV
snapshot that passes the same checks as a normal start, or from a control-plane import, and keeps that
snapshot's `state_version`.

Until then the dashboard fails closed:

- `/api/health` returns 503 with `"status": "state_unavailable"`, `"hydrated": false`, a `reason`, and
  the backup path in `state_backup`. The autoscaler treats the VM as unhealthy and replaces it after
  its grace period, and the Worker shows the waiting page.
- Every other request gets the wait response, nothing is written to the state path, and freeze and
  snapshot calls are refused.
- If the file can't be renamed, it stays in place and nothing replaces it. Repair or move it by hand,
  then restart the dashboard container.
- Without KV, or when KV has no snapshot or one that fails verification, the dashboard keeps
  retrying and never starts from defaults.

### Snapshot and active slot checks

- A new VM without local state starts from defaults only when KV positively reports that no snapshot
  exists. A snapshot that exists but fails verification, or whose state fails validation, keeps the
  dashboard unhydrated with a `reason` in `/api/health`.
- With local state and an unverifiable KV snapshot, the dashboard serves local state read-only. It
  never overwrites that KV value, because it may be newer.
- Before each KV write, the dashboard reads `origin:active` again and requires the snapshot to name this
  slot. It never overwrites a newer snapshot, or one at the same version with different content. After the
  write it reads the snapshot back. If the snapshot isn't this slot's, the dashboard stops accepting
  changes and `/api/health` reports `"status": "state_conflict"` with a reason until KV shows this slot's
  write.
- Workers KV is eventually consistent and has no compare-and-swap, so these checks narrow the race between
  two writers but can't rule it out. The real exclusivity guarantee is the autoscaler's fence: it stops the
  old control VM and confirms that it stopped before any recovery cutover. A Durable Object holding the
  active-slot pointer and the snapshot version is the planned upgrade.
- A VM accepts a change only after it reads `origin:active` again for that change and confirms that the
  record names its slot, or that no record exists yet. A record that exists but can't be parsed counts as
  unknown ownership, and so does a failed read. `/api/health` reports `ownership` as `active`, `inactive`, or `unknown`.
  Until ownership is `active`, changes get the wait response.

### Runner patches

The **Runner Patches** tab and `/api/patches` edit the `spot-runner-patch-v1` manifests in the config
repository: `defaults.yaml` for the organization and `repos/<owner>/<repo>.yaml` for each repository.

| Route | Purpose |
| --- | --- |
| `GET /api/patches` | Lists the manifests at the default branch HEAD. |
| `GET /api/patches/manifest?path=...` | Returns a manifest, its blob and base commit, and the effective patch with its sha256. |
| `POST /api/patches/validate` | Returns field errors such as `host.apt_packages`. A repository manifest is an override: checks that depend on inherited settings, such as `hardware.isa` against the architecture, use the settings merged with `defaults.yaml`. |
| `POST /api/patches/preview` | Classifies each runner host as `noop`, `hot_patch`, `recreate_containers`, `replace_vm`, or `unknown`. |
| `POST /api/patches/save` | Opens a pull request. Needs an `Idempotency-Key`. |
| `POST /api/patches/hot-patch` | Hot-patches the recorded runner hosts. Needs `"confirm": true` and an `Idempotency-Key`. |

- Saving creates a `spot-dashboard/...` branch from the `base_sha` that the editor loaded, commits the
  file there, and opens a pull request. It never writes to the default branch. If the default branch
  moved, or GitHub reports that the file changed, the dashboard answers HTTP 409. The token in
  `SPOT_CONFIG_GH_TOKEN` or `GH_TOKEN` needs contents and pull-request write access to the config
  repository.
- Every repository manifest overrides `defaults.yaml`, so saving `defaults.yaml` also reads each
  `repos/<owner>/<repo>.yaml` at the same `base_sha` and checks it merged with the new defaults. If any
  of them would be invalid, the save opens no pull request and answers HTTP 400 with field errors that
  name each affected repository. If the dashboard can't list or read those manifests, the save answers
  HTTP 424 with `"retry_same_key": true`, because the check didn't run. `POST /api/patches/validate`
  reports the same errors for `defaults.yaml`, or a `warning` when it can't read the manifests.
- `src/spot_runner_manager.py` records the patch on each runner host as instance metadata, but only after
  the host confirms it. A provisioned host confirms it when its bootstrap writes the completion marker
  for this deployment, every runner is online, the GitHub labels sync, and the storage check passes. A live patch confirms it
  when the host reports `LIVE_PATCH_APPLIED:<sha256>`. The keys are:
  - `spot-runner-patch-boot`: the GCE `lastStartTimestamp` of the boot that the confirmation belongs to.
    The manager observes the boot before it waits for the bootstrap marker. If the VM restarts at any
    point after that, including after the marker was read, nothing is recorded and the host stays pending.
  - `spot-runner-patch`: the canonical manifest JSON. It's left out when larger than 64 KiB, and then
    only the sha256 is recorded.
  - `spot-runner-patch-sha256`: the sha256, or `none` when the host has no patch.
  - `spot-runner-patch-target`: the repository.
  - `spot-runner-prefix`, `spot-runner-count`, `spot-runner-label`, `spot-runner-region`, and
    `spot-runner-heavy-pool`: the deployment details that a live patch needs.
  - `spot-runner-registration-target` and `spot-runner-group-id`: the GitHub scope that the runners
    registered with, and its runner group. The scope can be an organization such as `swack-tools`,
    even when the patch belongs to `swack-tools/oxidex`.
- The `spot-runner-patch` label holds the first 16 characters of the sha256, or `none`, for filtering.
- Until then, provisioning records the requested patch as pending: `spot-runner-patch-pending-sha256`
  holds the sha256 or `none`, and `spot-runner-patch-pending` holds the manifest. A VM runs before its
  bootstrap finishes, and it keeps running when the package install or patch step fails. If the bootstrap
  fails, the pending keys stay, and the applied keys stay unset or keep their earlier value. Confirmation
  writes the applied keys and the label, then removes the pending keys. `--patch-live` on a pending host
  applies the patch again, even when its receipt already matches, so `LIVE_PATCH_APPLIED` confirms it.
- A live patch writes only the same two pending keys. It writes them after every check passes, right
  before it runs the live script. A live patch that fails part-way leaves the host pending, so the
  preview never trusts the earlier applied record. That holds even after the desired patch reverts to
  the earlier one. Refused live patches write no metadata.
- After `LIVE_PATCH_APPLIED`, the live patch syncs the runners' GitHub labels first. Only then does it
  write the applied keys and remove the pending keys. When the label sync fails, the host stays pending,
  and `--patch-live` again re-applies the patch and retries the sync. A `--patch-live` run on a host that's
  already at the patch also syncs its labels before it records the patch. A dashboard hot-patch syncs
  the labels of each `noop` host too. When that sync fails, the host's result is `refused` or `failed`.
- On the host, the patch steps first write the pending marker `/var/lib/gha-runner-patch/pending.json`.
  Only after every step succeeds do they write the completion receipt `state.json`, with an atomic
  rename, and then remove the marker. For a live patch, the steps include the image build and every
  runner container. At startup, the receipt waits until every runner container has started. The
  receipt records the boot ID it was written in. A failed attempt leaves the earlier receipt and the
  marker in place. Only a completion receipt from the current boot, with no pending marker, proves the
  patch. `--patch-live` skips the live script only on such proof. The console's hot-patch executor
  classifies a host whose unproven receipt names the target as `hot_patch`, so the live script runs
  again and writes a completion receipt. Receipts written before this change don't count as proof.
- The control VM startup script always sets `DASHBOARD_FLEET_INVENTORY=gce`, which makes the preview
  list runner hosts from Compute Engine. A runner host carries the `spot-target` label or the patch metadata. The preview also lists
  a VM that has neither, when its name starts with a runner host prefix such as `oxidex-runners-`. Such a
  host was provisioned before patch recording, so its repository and patch are unknown. It shows as
  `unknown` in the preview of every repository and blocks hot-patching until it's reprovisioned or
  patched once with `--patch-live`. VMs with other names, and control VMs, aren't listed. The control
  VM's service account needs `compute.instances.list` and `compute.machineTypes.get`; without the second, a host's vCPU and RAM stay unknown, and a patch with hardware minimums classifies it as `unknown`. Elsewhere the preview uses the host records in
  the dashboard state.
- The preview checks each host's observed capacity against every hardware constraint of the patch first:
  `hardware.min_vcpu`, `hardware.min_ram_gib`, the architecture, and the CPU platform for `hardware.isa`. A
  host that can't meet them is `replace_vm` with the reason, and a host whose capacity is unknown is
  `unknown`. The hot-patch executor and `--patch-live` refuse such hosts before they change anything.
- The live script only installs and writes. It never removes a package, file, directory, or sysctl
  setting, and it can't undo a script that already ran. So a target that drops anything the applied patch
  has is `replace_vm`, and the reason names what it drops. A target can still add packages, files,
  directories, and sysctl keys, and rewrite a file or sysctl value in place. It can't change a non-empty
  `host.boot_script` or `container.dockerfile_run`. The same applies to a revert to an earlier patch.
  When the host's pending marker names an attempt at another patch that never completed, that attempt's
  partial changes are unknown, so the host is `replace_vm` too. `--patch-live` refuses such hosts before
  it changes anything, and that includes pending metadata that names another patch.
- The preview then classifies each host from its recorded manifest, after checking it against the recorded
  sha256. Hosts provisioned before patch recording show as `unknown`, with a reason. They stay that way
  until they're reprovisioned or hot-patched once with `--patch-live`. A host with a pending patch is
  also `unknown`, never `noop`. That holds even when the applied keys match, and even when the receipt
  matches. The reason names the pending patch.
- A reboot can bring the runners back without a live patch's changes. When a host's current
  `lastStartTimestamp` differs from the recorded `spot-runner-patch-boot`, the preview shows it as
  `unknown`, with a reason that says to reprovision it or run `--patch-live` again. A host with no
  recorded boot is `unknown` too, and that includes every host confirmed before boot recording.
- Hot-patching applies the saved effective patch, never a draft. It runs only when every host is
  `hot_patch` or `noop` and the `patch_sha256` from the preview is still current. It reports a result per
  host. A repeated request with the same `Idempotency-Key` returns the stored result without touching
  the hosts again. Like every change, it gets the wait response while the dashboard is frozen,
  unhydrated, or not the active slot.
- The control VM's image has no `gcloud` and no SSH credentials, so the shipped dashboard can't manage
  runner hosts. That isn't a network boundary: on a shared VPC the VMs can still reach each other unless you
  add firewall rules. Hot-patching there answers HTTP 501 with `"code": "hot_patch_unavailable"`. Run
  `src/spot_runner_manager.py --patch-repo OWNER/REPO --patch-live` from an operator workstation instead. A
  dashboard that runs where `gcloud` and operator credentials exist can set
  `DASHBOARD_HOT_PATCH_EXECUTOR=gcloud`. That path takes the zone, machine type, region, runner label,
  runner prefix, runner count, patch repository, and registration scope from the host's recorded
  metadata. It refuses a host that's missing any of them, because the live patch would otherwise reset
  the host's GitHub scheduling labels to the `spot_runner_manager.py` defaults. An org-registered host
  also needs its runner group. The heavy pool size is passed when the host records one.
- A live patch first checks everything that label sync needs: the runner group, strict-pool membership,
  and that GitHub lists every expected runner. Only then does it change the host, so label sync can't
  fail after the patch applied.
- Saving and hot-patching survive a lost response:
  - The save branch name comes from the `Idempotency-Key` and the manifest path. A retry finds that branch
    and returns its pull request instead of opening another, even on a replacement VM. It completes an
    attempt that stopped halfway, and answers HTTP 422 when the same key is reused for a different
    change.
  - When GitHub's answer is lost or is a server error, the save never deletes its branch. It looks up
    the branch, its commit, and any pull request first. If GitHub didn't confirm the step, the save
    answers HTTP 424 with `"retry_same_key": true`. It deletes the branch only after GitHub rejects the
    commit outright while the branch still holds no commit.
  - Any failed lookup while the save reconciles an earlier attempt, and any lost or 5xx GitHub answer
    during a save, answers HTTP 424 with `"retry_same_key": true`. Only definite answers are final:
    validation errors, conflicts, and a GitHub 4xx rejection of a plain read or of a write.
  - Results marked for a same-key retry (`"retry_same_key": true`, or the pending 202) are never stored
    as final. A retry with the same key runs again and reconciles; final results are stored and replayed.
  - The browser retries such a result automatically up to three times with the same key, after 2, 5,
    and 10 seconds. If you click **Save** again for the same change, the panel reuses that key until a
    final result arrives. A changed manifest gets a new key.
  - A hot-patch retry classifies each host again from its receipt and recorded patch. Hosts already at
    the patch report `noop` and aren't patched again.
- A retry with the same `Idempotency-Key` while the first request still runs gets HTTP 202 with
  `"status": "pending"` and `Retry-After`. That happens, for example, after the Worker's 30-second
  timeout. The browser keeps asking with the same key until it gets the stored result.
- GitHub and Compute Engine failures answer HTTP 424 with `"code": "upstream_error"`. Every response from
  the dashboard's routes carries `X-Spot-Origin: dashboard`. The Worker passes a marked 5xx response
  through unchanged unless its body is the wait response. It treats an unmarked 502, 503, 504, or 520
  to 530 response, a failed connection, a timeout, or a missing origin record as an outage.
- A freeze waits up to 45 seconds for running saves and hot-patches to finish, so the handover snapshot
  includes their results. If they're still running after that, the freeze answers HTTP 409 with
  `"retryable": true`, and the autoscaler tries again.

The Worker always passes `/auth/device/code` and `/auth/device/token` through without a session cookie.
The dashboard refuses them unless `GITHUB_OAUTH_DEVICE_FLOW=1` turns device login on. The
GitHub device code stays on the control VM behind a random handle, and the dashboard enforces the polling
interval. A VM replacement ends a device sign-in in progress, so request a new code.

The Worker reaches the control VM through its tunnel hostname, so the request `Host` differs from the
browser `Origin`. When a `POST` carries an `Origin` header, the dashboard accepts it only when `Origin` matches its own
`Host` or its public origin, and it never trusts `X-Forwarded-Host` for this check. The public origin comes from
`--public-origin`, then `DASHBOARD_PUBLIC_ORIGIN`, then the origin of `GITHUB_OAUTH_REDIRECT_URI`. The
startup script writes `DASHBOARD_PUBLIC_ORIGIN` from the `dashboard-public-url` metadata value.

## Instance data

The instance list, the region map, and the fleet telemetry show only VMs that the live Compute Engine
inventory lists. The dashboard doesn't ship demo or seed instances.

- With `DASHBOARD_FLEET_INVENTORY=gce`, which the control VM sets, the dashboard lists the project's
  instances every 2 minutes. The listing is one aggregated read-only call, plus one aggregated disk
  listing and a cached machine type lookup for each shape. It keeps the VMs whose names start with
  a runner host prefix (`oxidex-runners-`, or an earlier fleet's `newznab-spot-runners-`,
  `spot-runners-`, or `oxidex-spot-runners-`) or `builder-` (dedicated builders). Control VMs and other VMs aren't
  listed. The control VM's service account needs `compute.instances.list` and
  `compute.machineTypes.get`. With `compute.disks.list` the records also show disk types; without it
  (HTTP 403) a VM keeps the types and disk IDs stored from an earlier listing (across any number of
  denied listings) and a new VM shows `pd-balanced`,
  but both are marked unverified and incompletely priced, and neither can be a template source. Any other failed or partial disk listing fails the whole refresh,
  so known disk types are never replaced by the default. An attached disk the listing doesn't cover is
  treated as an unknown type. On a VM's first listing, each disk is billed from its own creation time,
  so a disk attached later isn't billed back to the VM's creation; when a creation time is missing, disk
  history starts at that listing, and the earlier time is billed once a later listing reports every
  creation time. A machine type whose capacity can't
  be read also fails the refresh, so no VM is priced without its memory.
- A listed VM shows its Compute Engine status: `RUNNING`, or `TERMINATED`, `STOPPED`, or `SUSPENDED`
  after a Spot preemption or a manual stop. A suspended VM's compute ends at its
  `lastSuspendedTimestamp`, also when it resumed between two listings. A stopped VM stops adding compute cost at its
  `lastStopTimestamp`, but its attached disks keep adding cost until the VM is deleted, and its row shows
  that disk-only rate, and a deleted VM's row shows no current rate. When a VM starts again, its earlier running intervals stay in its runtime and
  window spend. When its machine type or disks change, or its prices change, the time before the change
  keeps the earlier rates; each disk added to a listed VM starts billing at its own creation time. A stop that's reported late ends the run at the reported stop time. Each data-disk type is priced at its own rate, and fleet totals add unrounded costs. Only `RUNNING` VMs count
  toward the runner and builder telemetry, but the disks of stopped VMs count against the scaling
  budgets. Hiding deleted instances doesn't hide stopped VMs, because they still exist. The listing
  carries no CPU utilization, so inventory VMs never trigger a CPU-based scale-down recommendation.
- A VM that was listed before and is no longer in the inventory becomes `DELETED`. Its deletion time is
  the time of the first listing that missed it. Deleted VMs and earlier runs stay in the history for 31
  days, and cost windows are limited to 744 hours (31 days) to match. To keep the state and its KV
  snapshot small, history past 2,000 records, or past 200 runs or disk segments of one VM, is folded
  into hourly cost totals. If the instance data still exceeds 6 MiB serialized, the oldest deleted
  records are folded next, then all but the newest 10 runs and disk segments of each live VM, then all
  of them, and then each live VM's folded totals are merged into daily totals. A listing whose records
  still don't fit is refused, and the inventory reads as stale. Window totals still include it to within an hour, though a search filter
  can't match folded records. A VM that's
  recreated with the same name gets a new record, because the record follows the instance ID.
  Template creation and deletion plans require `instance_id` when a name matches more than one zonal
  or historical record. A supplied ID takes priority over the name and never falls back to another VM.
- Spend covers each VM's attached disks while the VM exists. A fleet-named disk that no VM uses, such
  as a dedicated builder's cache disk (`auto-delete=no`) after its VM is deleted, gets its own
  `DETACHED` record that bills storage until the disk is deleted (shown as present storage). A disk the instance listing still shows
  attached is billed only by its VM's record, and a disk attached to a fleet VM that the instance listing
  doesn't show yet is billed on its own until that VM appears. A disk attached to several fleet VMs is
  billed once, while every VM keeps the full attachment metadata. Detached runner data disks named
  `runner-data` or `runner-data-*` are included too; where fleet users are present, their role identifies
  which fleet owns the disk. VM templates cannot copy shared
  attachments. Disk users are matched by zone and instance name. Disks are listed even when no fleet VM is left, and a disk that moves between records (a VM and its `DETACHED` record, or a replacement VM)
  is billed by the new record only from the handoff, never again from its creation. Disks are matched
  by their immutable disk ID, so a new disk that reuses a name is billed from its own creation. While
  disks can't be listed, existing `DETACHED` records are kept, not marked deleted, unless a listed VM
  shows the disk attached (its record takes it over), and a builder that
  disappears keeps billing the storage of the disks that outlive it (auto-delete off, such as its cache
  disk; all its disks when the listing didn't report auto-delete), `disk_liability_pending`, until a disk
  listing shows whether they still exist; its row shows that storage rate. That storage is repriced like other records. A disk that's
  detached again keeps its earlier standalone interval and restarts billing at the new handoff.
- Without `compute.disks.list`, detached disks can't be seen, so scaling recommendations hold and
  regional scale plans are refused even when the listing is otherwise current. Grant the permission for
  scaling to work. For a disk newly attached or resized during this outage, the dashboard defers its cost rather
  than assigning a fallback rate. Once disk metadata is available, it fills that disk's history from
  its creation or known handoff time at the verified rate, preserving observed size-change boundaries.
  Deferred intervals follow a disk when its billing owner changes and survive detachment until metadata
  can resolve them. Pending disk liabilities remain visible
  when the deleted-instance filter is off. A complete project disk listing must confirm a resource is
  missing before its unresolved history can expire. Once that confirmed absence is outside the 31-day
  window, it no longer holds current budgets; `lifetime_cost_complete` stays false if the old amount
  couldn't be recovered. A newer disk with the same source name cannot resolve the old uncertainty.
- A builder name identifies the host's role, but its fixed build capacity is counted only after the
  machine inventory verifies `n4-highcpu-16`, 16 vCPUs, and 32 GiB of host RAM. Other shapes and unknown
  capacities have no known container limits and can't be copied into a VM template.
- Instance metadata records how many runners a host registered, but not the per-runner limits, which
  depend on the tier and admission. Runner host records show those limits as not recorded, and a VM
  template can't copy them from such a host; the dashboard hides the template action for such records.
  The "Runners online" figure counts only confirmed runner slots and names hosts not yet confirmed. A VM with data disks of several types can't be a template
  source or the shape of a scale plan, since those hold one data-disk type. Neither can a VM with
  Local SSD, regional disks, or provisioned Hyperdisk performance, which templates don't describe. A
  scale-up plan whose new VMs can't be fully priced is refused. When a running host has no recorded runner count, or
  hasn't confirmed its bootstrap in its current boot (an applied patch record for this boot with
  nothing pending), the runner total is unknown: the runner scaling recommendation holds and names the host, and scale-up
  plans are refused.
- A VM in a region outside the dashboard's region catalog is priced with no tax, marked
  `tax_rate_known: false`, rather than with another region's default tax, and its pricing is marked
  incomplete, since the catalog has no trusted compute or disk rates for that region either. When the budget being
  checked or the VM's own budget includes taxes, without a tax override on the VM's profile, that
  unknown tax holds scale-ups like other unpriced costs. A VM first listed while
  its profile overrides the regional tax is priced with the override from the start. Each inventory
  refresh, and each budget or profile change, reprices the listed VMs, stopped ones included, but a page read
  doesn't, so a tax override also applies to
  their disks, and the rate change is saved with the state.
- A standard (non-Spot) VM is priced from the Billing Catalog's on-demand rates. Without them, for a
  `SUSPENDED` VM, whose preserved memory Compute Engine also bills, for a VM with Local SSD (counted
  as `local_ssd_gb`, not as a persistent data disk), for a VM with a regional (replicated) persistent
  disk or an Extreme Persistent Disk (`pd-extreme`, whose provisioned IOPS bill separately), for a custom or extended machine shape, which predefined rates don't price, for a machine family
  without a known rate (when no live price was found), for a disk type without a live or static rate, and for a VM whose
  Hyperdisk bills provisioned IOPS or throughput (Hyperdisk Throughput, or Hyperdisk Balanced provisioned past 3,000 IOPS or 140 MiBps, or with
  no reported performance), the record is marked
  `pricing_complete: false`. Under a shared budget, the other role's VMs and disks, including runner VMs the CI evaluation would add,
  are priced with the disk and tax settings of the budget being checked. While the role's fleet (both roles under a shared
  budget) has such a VM,
  scale-up recommendations hold and scale-up plans are refused, because their budget check would pass
  on a cost that's too low.
- A suspended VM's memory-storage charge isn't priced here. Its unpriced interval remains in
  `unpriced_history_intervals` after it resumes or is deleted. A first listing that already shows a
  restart also records unknown earlier compute history. These records keep `lifetime_cost_complete`
  false. `window_cost_complete` reports whether the selected cost window overlaps missing charges;
  scale-up budgets hold while applicable missing charges remain in the supported 31-day window.
  Expiry from that window doesn't make lifetime history complete. Closing a compute or disk interval
  at an incomplete fallback price keeps that history marked incomplete even after current rates
  become trusted. Retained cache-disk prices preserve each disk's pricing-completeness flags.
- A VM in a transitional state, such as `PROVISIONING`, `STAGING`, `STOPPING`, or `REPAIRING`, counts
  at its full rate in burn and budgets. While the role has one, scaling recommendations hold and
  regional scale plans are refused, including scale-down and no-op plans, because its capacity hasn't settled. For a newly provisioned VM, creation
  is a provisional boundary until GCE reports its first start. Repricing and state reloads don't turn
  creation-to-first-start time into a completed compute run; a VM with an earlier proven run retains
  that history through a restart. Restart staging keeps a completed run's known stop separate from
  `transitional_since_epoch`, so the stopped gap isn't charged as compute. If a newer stop before the
  latest start proves an unobserved intervening run whose start can't be recovered, its bounded
  compute-history uncertainty remains explicit instead of being silently omitted.
- A listing with an unreachable zone, or one that fails, changes nothing. The instance view's
  `fleet_inventory` field reports the last successful listing and the last error. Until the next
  complete listing, and after a restart until the first one, `fleet_inventory.fresh` is false and
  `fleet_inventory.stale_reason` says why: the rows
  stay visible, but scaling recommendations hold and all regional scale plans are refused, including scale-down plans. A listing that can't be
  saved, for example on a full disk, also leaves the inventory stale, and so does a refresh that hasn't
  completed for three refresh intervals (6 minutes), for example a hung API connection.
- Only the active slot reconciles. A store that is frozen, not hydrated, not reconciled with KV, or
  inactive makes no Compute Engine calls and keeps the records the active slot last published.
- The first successful listing is a baseline, recorded as `fleet_inventory_baseline_at`. The VMs it finds
  already existed, so they don't mark OOM events handled. A runner OOM event that's active for scaling keeps
  the ready runner slots running when it happened (`baseline_runners`; a late report excludes capacity
  that started after it), and OOM state is reconciled on every listing, even one with no fleet change, and it's handled only when a runner host
  first listed after the baseline is `RUNNING` with its bootstrap confirmed and the ready slots reach
  the event's target. A restart of existing capacity, or a same-size replacement, handles nothing. New
  capacity stays pending through the OOM lookback, so an OOM reported late can still be answered by it.
  Without a dated readiness transition, only a new identity created after the incident can satisfy it.
  A poll time or missing bootstrap metadata cannot date readiness, so uncertain incidents stay active.
  GitHub CI entries enter baseline, active-event, and acknowledgement handling only when the shared
  classifier identifies an OOM on this self-hosted GCP runner fleet. Timeouts, external-runner failures,
  and non-OOM records keep their original incident history. Actual log evidence and runner identity
  take priority over stale cached classification flags.
  A later restart of the same identity cannot satisfy a newer incident.
  Hosts first listed together with an OOM that were already running before it count toward its baseline,
  and only ready runner slots count as current runners. Builder OOM events are never handled from the
  inventory, including by the builder scaling evaluation: a builder has no service account, so its
  instance record can't show that its build container started. Acknowledge them with `POST /api/scaling`
  and `acknowledge_oom_id` (or `acknowledge_all_ooms`).
  A held recommendation projects the cost of the fleet it keeps, including the storage its stopped VMs
  and detached disks still bill. A listing that changes nothing writes
  nothing, so a quiet fleet doesn't publish a new snapshot.
- Without `DASHBOARD_FLEET_INVENTORY=gce`, for example on a local dashboard, the instance list stays
  empty, the instances tab says that the inventory isn't configured, and scaling recommendations hold
  and regional scale plans are refused, because the real fleet is unknown. Inventory records loaded from
  a state file or snapshot are dropped too, since nothing would keep them current, and so is the
  baseline: a later re-enabled inventory starts a new one.
- On load, from the state file, a KV snapshot, or a handover import, the dashboard drops every instance
  record that didn't come from the inventory. These are the demo instances that earlier versions seeded,
  such as `oxidex-runners-dallas-01`, and records that the old in-dashboard actions invented.
- The delete and regional scale controls, `POST /api/instances/delete` and
  `POST /api/instances/scale-region`, are plans only. They check the request against budgets and caps
  and report what would launch or retire. The budget projection includes the disks of stopped VMs.
  They don't change the instance list or any VM, and they run like reads: each request is computed
  from the current state, an `Idempotency-Key` stores nothing, and a frozen or unreconciled slot still
  answers them. The autoscaler
  (`src/spot_runner_autoscaler.py`) drains and retires runner hosts, `src/spot_runner_manager.py`
  provisions them, and `src/render_builder_startup.py` renders a dedicated builder.

## Build and scaling telemetry

The Rust build history and the scaling telemetry start empty. The dashboard doesn't ship demo builds,
GitHub CI failure runs, journal OOM events, or job counters.

- Rust builds appear only after a caller records them with `POST /api/rust-builds`. GitHub CI failure
  runs, journal OOM events, and the job counters (`jobs_ran_past_hour`, `provisioned_jobs`, and
  `queued_jobs`) change only through `POST /api/scaling`. A new state starts every counter at 0.
- When the dashboard loads a state that an earlier version wrote, from the state file, a KV snapshot, or
  a handover import, it drops the demo records that version seeded and logs how many it dropped. It
  then marks the state with `demo_telemetry_migration`, so this runs once and later loads keep every
  reported record and counter. A state with a marker of 1 or higher, including one a later version
  wrote, is left as it is.
- A record is dropped only when its ID and the fields that every seeded version gave it all match, so a
  real report merged into a seeded row stays:
  - The Rust builds `rb-20261015-001` through `rb-20261015-005`, on `builder-oxidex-turin-01` and
    `builder-oxidex-eu-01`. `POST /api/rust-builds` merges a partial update into an existing build, so
    these match only when every field the seed set is unchanged, including the seeded gaps between the
    requested, finished, and delivered times and their original whole-second UTC format. Fractional timing
    corrections stay even when all three times shift together. The original startup time must be confirmed
    independently by at least two unchanged saved preset templates. Shifted builds cannot provide their own
    time evidence; without agreeing saved presets, they stay untouched.
  - The GitHub CI runs `gh-oom-37192272062`, `gh-oom-37192994059`, and `gh-timeout-38596411473`, and
    the older IDs of the same seeds, `gh-oom-38596411473` and `gh-oom-38601924810`, matched with their
    complete seeded report content, including logs, links, and failure times.
  - The journal OOM event `oom-journal-01`, matched with its complete seeded report content. Its generated
    observation time and older CI failure times are checked against the original startup time confirmed
    by the unchanged saved presets. If that time cannot be recovered, these events stay rather than risking a
    reported correction. Additional source evidence, such as a log hash or cgroup path, also keeps the
    event; only known classification and handling fields may be added to an unchanged seed.
- The migration preserves all saved job counters, including the old demo tuple of 14 jobs in the past
  hour, 7 provisioned, and 3 queued. Earlier versions stored no provenance for counters: a fresh partial
  report could produce that tuple while leaving old demo events untouched. There is no way to prove the
  saved counters are still demo data. New states start at 0; later reports replace the saved counters.
- The migration keeps the loaded `state_version` and sets `demo_telemetry_migration_pending` in the saved
  state, so a restart doesn't lose it. It does this even when no seeded record matched, because adding the
  marker alone changes the state. The active slot then commits the migrated state as the next
  `state_version` through the same checks as any change, and publishes it to KV. Any new version clears
  the flag. A standby slot waits, so its copy never shadows the active slot's next change. A rollback then
  sees the migrated version as newer than the old slot's copy and carries it back.
- A handover import keeps the snapshot's `state_version`, because the control plane records the version
  that an import reports as the floor for any later re-import. The replacement commits the migration once
  it confirms that it's the active slot, within one writer check (30 seconds) of cutover. A rollback inside
  that window leaves the old slot's demo telemetry in place until the next rotation migrates it again.

## State durability

- The dashboard writes state atomically to `/var/lib/spot-dashboard/state.json` on the boot disk.
  Spot control VMs use the `STOP` termination action, so a preemption keeps the boot disk. A standard
  control VM is never preempted, and a stop keeps its boot disk too.
- Every save increments `state_version`. The active slot copies each save to KV about one second later.
- At startup the dashboard loads the newer of the local file and the KV snapshot, then reports
  `hydrated`. A new VM with no local file waits while KV is unreachable: `/api/health` returns 503 and
  the dashboard retries with backoff. It starts from defaults only when KV reports that no snapshot
  exists, never when a snapshot exists but fails verification.
- A VM that starts from its local file while KV is unreachable serves pages but answers changes with
  the wait response, and `/api/health` reports `starting` with `"reconciled": false`. When KV answers, a
  KV snapshot whose version is equal to or higher than the starting version replaces the local copy. Otherwise the dashboard
  publishes its local copy. Changes resume after that.
- An import fails with HTTP 500 and `"imported": false` when the dashboard can't save it to the local
  state file. The previous state stays in place.
- KV writes run one at a time in version order, and a freeze waits for any write in progress. The
  active slot never writes a snapshot older than its last write. Right before each write it checks that
  it isn't frozen, that `origin:active` still names its slot, and that KV doesn't hold a newer
  snapshot. If any check fails, it skips the write.
- A VM that becomes the active slot compares its state with KV before it accepts its first change or
  writes to KV, and adopts a newer KV snapshot. If it can't read KV, it answers changes with the wait
  response.
- Stored idempotent responses are independent copies, so later changes never alter a replay.
- A control VM whose last successful read of `origin:active` names the other slot keeps serving pages
  and reports `"active": false` in `/api/health`, which can still return 200. It answers changes with the
  wait response, because a Worker with a cached origin could otherwise send it changes that never reach
  the active dashboard.
- When the local file is newer than the KV copy at startup, for example because a preemption cut off a
  write after a local save, the active slot publishes the local copy.
- The browser sends an `Idempotency-Key` header with every change. For a state change, the dashboard
  stores the response in the same atomic save as the change, so a retried request never applies twice.
  A patch save or hot patch stores its response right after the GitHub or host action, and a retry
  reconciles with GitHub or the hosts instead of acting twice.
- A freeze writes the marker file `state.json.frozen` next to the state file. If the container restarts
  while frozen, the dashboard starts frozen and `/api/health` reports `frozen`. The marker stays until an
  explicit unfreeze or a successful import.

## Outages and recovery

While the control VM is down, the Worker answers with HTTP 503 and the body
`{"error": "control_vm_unavailable", ...}`. Page loads get a waiting page that polls `/api/health`.
The dashboard page shows a banner, keeps its in-memory state, and retries the failed request with the
same idempotency key after `/api/health` returns 200.

The autoscaler handles the control VM when you run it with `--manage-control-vm`:

- **One manager per project**: the `spot-control-a` and `spot-control-b` slots belong to the project, and
  more than one `--target` controller can enable `--manage-control-vm` there. Each check first takes an
  exclusive lock on `control-vm-<project>.lock`, or the shared lease in a storage bucket when
  `SPOT_CONTROL_STATE_URI` is set (see [Watchdog](#watchdog)). While another controller or `src/control_vm.py --apply`
  holds it, the autoscaler skips its control VM step for that check and logs why. Runner management
  continues either way.
- **Shared control VM state**: the transition, retained boot disks, and history live in
  `control-vm-<project>.json`, not in each controller's `--state-file`. If one controller crashes during a
  transition, the next controller that takes the lock resumes the same transition. Both files live in
  `SPOT_CONTROL_STATE_DIR`, which defaults to the autoscaler's default state directory. Every controller
  in the project must use the same directory, and the lock only serializes controllers on one machine.
  `SPOT_CONTROL_STATE_URI` moves the state and the lock to a storage bucket, so controllers on different
  machines, such as the watchdog and a workstation, share them.
- **Moving earlier state**: control VM state that an older version saved in a controller's `--state-file`
  moves to the project file on that controller's first check. An idle copy is dropped. When both files
  hold different transitions or retained disks, that controller refuses to manage the control VM and
  logs an error until an operator decides which state is current.
- **Stopped standard VM**: the autoscaler starts a stopped free-tier VM again and never treats it as lost
  Spot capacity. It replaces the VM with the other slot only after a start fails and the VM has stayed
  down for 10 minutes. A start that succeeds restarts that grace period.
- **Preempted VM**: the autoscaler starts it again. If the start keeps failing, it creates the other slot,
  which loads state from KV, then points `origin:active` at it. If the old VM still answers but reports
  `frozen`, the autoscaler freezes it again, or reads its snapshot, and imports that state before the
  switch. If the old VM is down, the autoscaler imports the latest KV snapshot into the new VM and
  waits until the new VM reports at least that version before it updates `origin:active`.
  Only a confirmed freeze makes the imported state final. If the old VM answers but never freezes, the
  autoscaler imports its unfrozen snapshot and stops the old VM before the switch. The old VM may accept
  changes until it stops, so the autoscaler treats that state as provisional. It logs an error, reports
  the reason as `old_state_unconfirmed` in the `status` output, and keeps the old boot disk when it
  deletes the old VM. An old VM that the autoscaler stopped, rather than froze, gets the same treatment.
- **Price rotation**: price never rotates away from a standard control VM. For a Spot control VM, the
  autoscaler creates the other slot, waits for health, freezes the old VM, copies
  its snapshot into the new VM, writes `origin:active` with read-back verification, waits for KV to
  propagate, then deletes the old VM. A failed origin write unfreezes the old VM and deletes the new one.
- **Instance identity**: each transition records the provider instance IDs of the old and new VMs and
  checks them before every step. If a slot's VM was recreated under the same name, the transition holds
  for an operator instead of stopping, freezing, or deleting the wrong VM.
- **Replacement check before deletion**: the autoscaler deletes the old VM only after a fresh check shows
  the new VM is `RUNNING`, healthy, and reporting its own slot. The new VM must also report a
  `state_version` no lower than the version it confirmed after the import or KV catch-up. The same check
  runs right before the `origin:active` switch. If the new VM reports less, the autoscaler imports that
  state again, up to 5 times, and continues only after fresh health confirms the version. This can
  happen when the new VM restarts and loads an older KV snapshot. Until then the old VM and its disk
  stay. If the new VM disappears, or stays unready for 5 minutes while the old VM still runs, the
  autoscaler rolls back. It first fences the new VM: a confirmed freeze, or, when the freeze fails, a
  stop confirmed by fresh inventory. Next it reads the old VM's current `state_version` from fresh health
  and compares it with the newer of the new VM's snapshot and the KV snapshot. The old VM can hold less
  when it restarted and loaded an older KV snapshot. In that case the autoscaler imports the newer
  snapshot into the old VM and confirms it with fresh health. If it can't confirm the transfer, it keeps
  the new VM's boot disk and reports the reason as `carry_back_unconfirmed` in the `status` output. Then
  it unfreezes the old VM, points `origin:active` back at it, and deletes the new VM. Without a running
  old VM, it waits and deletes nothing.
- **Uncertain creates**: if `gcloud` reports a failed create without a capacity or quota error, the
  autoscaler checks that zone for the instance before it tries another zone, and adopts the instance if
  it exists. If it can't tell, it waits.
- **Capacity failures**: the autoscaler records a failed create for that machine type in that zone only,
  so a stockout of the cheapest machine type never hides another affordable machine type in the same
  zone. It retries a failed pair after 30 minutes, and starts the search over only after every affordable
  pair failed. Zone-only records from older autoscaler versions still exclude the whole zone until they
  expire.
- **Duplicate slots**: if two VMs carry the same slot, the autoscaler logs an error and changes nothing,
  including `origin:active`, until an operator deletes the extra VM.
- **Retained boot disks**: when the autoscaler can't confirm that a VM's state reached its successor,
  it deletes the VM with `--keep-disks=boot`. The boot disk keeps the VM's name. The autoscaler logs an
  error and records the disk under `retained_disks` in its controller state, and later creates for that
  slot prefer other zones. Recover the state from the disk, then delete the disk by hand. This happens
  when a rollback can't freeze the new VM or can't confirm the import, when a recovery switched
  slots without importing from the old VM, and when a recovery imported state from an old VM that never
  confirmed a freeze. The `reason` field in `retained_disks` says which case applies.

Recovery depends on a controller running. The [watchdog](#watchdog) runs one check every 3 minutes
without any workstation. Without it, the dashboard stays down while the workstation that runs the
autoscaler is off.

## Use the free-tier control VM

`SPOT_CONTROL_PROVISIONING=standard`, or `--provisioning standard` on `src/control_vm.py`, runs the
control VM as a free, always-on, non-preemptible e2-micro inside the Compute Engine free tier. The default,
`spot`, keeps the Spot behavior that the preceding sections describe.

### Free-tier constraints

Google's free tier covers, per billing account and month:

- One non-preemptible e2-micro VM in `us-west1`, `us-central1`, or `us-east1`.
- 30 GB-months of standard persistent disk (`pd-standard`).
- 1 GB of outbound data transfer from North America to every destination except China and Australia.

The standard mode enforces what it can:

- The only shape is `e2-micro`, created with `--provisioning-model=STANDARD`,
  `--maintenance-policy=MIGRATE`, and `--no-restart-on-failure`, and with no Spot flags or termination
  action. Host maintenance live-migrates the VM. After a crash or a host failure, the manager restarts it,
  rather than Compute Engine, so a fenced control VM stays stopped until the manager starts it. The manager
  also turns automatic restart off on any control VM that has it. See [Restart policy](#restart-policy).
- The boot disk is a 15 GB `pd-standard` disk. It holds Debian 12, Docker, the 2 GiB swap file, both
  images, and the capped container logs. Two such disks, during a cutover or with a retained boot disk,
  still fit in 30 GB-months.
- Every region in `SPOT_CONTROL_REGIONS` must be a free-tier region. Any other region is a configuration
  error that pauses every control VM change until you fix it. Without `SPOT_CONTROL_REGIONS`, the standard
  mode uses `us-central1` only.
- No price lookup runs. The offer is fixed at $0 a day, and a capacity failure moves on to the next
  free-tier zone of the same region, then to the next listed region.
- Price never rotates away from a standard VM. The autoscaler also never switches a standard VM back to
  Spot by itself. To move back, set `SPOT_CONTROL_PROVISIONING=spot` and run `src/control_vm.py rotate --apply`.

The free tier doesn't cover these costs:

- The external IPv4 address, at about $0.005 an hour, or about $3.65 a month. The VM keeps it, because
  GitHub has no IPv6 endpoints and a NAT gateway costs more. The $0 offer doesn't include it.
- A second e2-micro or a second boot disk beyond the free amounts. During a cutover or a recovery, both
  slots run for a few minutes up to about an hour, and those extra hours are billed at the e2-micro rate.
- Outbound data transfer over 1 GB a month. The tunnel, health checks, and KV snapshots normally stay well
  below that.
- Any other e2-micro in the same billing account, which uses up the same free allowance.

### Dashboard image

The e2-micro never builds the dashboard image. It pulls the image that
`.github/workflows/dashboard-image.yml` publishes to `ghcr.io/swack-tools/spot-dashboard` for every push to
`main` that changes the image's inputs. The workflow tags each image `:<commit>` and `:main`, and its run
summary prints the digest.

- Preferred: set `SPOT_DASHBOARD_IMAGE` to the digest, such as
  `ghcr.io/swack-tools/spot-dashboard@sha256:<64 hex>`. A digest can't move.
- Also accepted: an image tagged with a 40-character commit, such as
  `ghcr.io/swack-tools/spot-dashboard:<commit>`. When only `SPOT_DASHBOARD_SOURCE_REF` is set and
  `SPOT_DASHBOARD_REPO` is the default, the standard mode pulls that commit's tag. The tag exists only for a
  commit that the workflow published.
- The package is private. The startup script signs in to `ghcr.io` with the GitHub token from Secret Manager
  and runs `docker pull`. That token needs the `read:packages` scope. Without it the pull fails with an
  access-denied error and the startup script stops with `could not pull <image>`. See [Operations and troubleshooting](operations.md).
- Without either, the autoscaler and `src/control_vm.py` refuse to create the VM, and they don't begin the
  switch from Spot. The startup script also stops with an error on an e2-micro without a pinned image, and
  it never installs `git` there.

A Spot control VM is unchanged: it accepts a digest or builds `SPOT_DASHBOARD_SOURCE_REF` on the VM. The
published image is the recommended path there too, because it boots faster than a build.

### Memory on the e2-micro

The e2-micro has 1 GB of RAM. On that shape, the startup script reads the machine type from the metadata
server and sets up two swap areas before it installs packages or pulls images:

- Compressed swap in RAM: the `zram` kernel module's `/dev/zram0`, half of `MemTotal`, with `zstd`
  compression, or `lz4` when the kernel lacks `zstd`, or else the kernel default. It has swap priority
  100, so the kernel uses it first. When the module or the device is missing, or any `zram` step fails, the
  script logs why and continues with the swap file alone.
- The swap file `/swapfile`: 2 GiB, owned by root, mode 0600, at swap priority 10.

Both steps run at every boot and are idempotent. Active swap areas stay as they are, and the script creates the
swap file only once and turns it on again on later boots. The script also limits the dashboard container to
320 MB of RAM (640 MB with swap) and `cloudflared` to 160 MB (320 MB with swap). Every shape caps container
logs at three 10 MB files.

### Switch to the free-tier VM

When the mode is `standard` and the active control VM is a healthy Spot VM, the next check replaces it. It
uses the same make-before-break rotation as a price rotation: create the e2-micro in the other slot, wait for
health, freeze the Spot VM, import its snapshot, check the version floor, write `origin:active`, wait for KV
to propagate, and delete the Spot VM. Every fence and version check applies. A failed step aborts the
switch, the Spot VM keeps serving, and the switch is retried after the 6-hour rotation cooldown. Every
controller that runs `--manage-control-vm` in the project must use the same mode. A controller configured for
the other mode never creates the replacement of a switch in progress, and the switch aborts instead.

Prerequisites:

- No other non-preemptible e2-micro runs in the billing account.
- The control VM service account, secrets, and Cloudflare settings that the Spot VM uses.
- A published dashboard image. Copy its digest from the summary of the latest **Dashboard image**
  workflow run, or read it with
  `docker buildx imagetools inspect ghcr.io/swack-tools/spot-dashboard:<commit>`.

To switch:

!!! warning "This changes live resources"
    The restart in step 3 starts a live make-before-break switch of the control VM.
    Run the preview in step 2 first, and make sure no other change to the control VM is in progress.

1. Add these settings to the autoscaler's environment file:

   ```bash
   SPOT_CONTROL_PROVISIONING=standard
   SPOT_CONTROL_REGIONS=us-central1
   SPOT_DASHBOARD_IMAGE=ghcr.io/swack-tools/spot-dashboard@sha256:DIGEST
   ```

1. Preview the switch. These commands change nothing:

   ```bash
   PYTHONPATH=src python3 src/control_vm.py status
   PYTHONPATH=src python3 src/control_vm.py plan
   ```

   The plan reads `rotate slot=a target=b offer=e2-micro@us-central1-a $0.0000/day` and names the switch to
   the free-tier VM. A `wait` plan whose reason starts with `rotate refused` names the setting to fix, such
   as a missing image.

1. Restart the autoscaler, which runs with `--manage-control-vm`, so that it reads the new settings. It then
   runs the switch, one step a check:

   ```bash
   launchctl kickstart -k "gui/$(id -u)/com.swacktools.spot-runner-autoscaler"
   ```

   To drive the steps yourself, run `PYTHONPATH=src python3 src/control_vm.py ensure --apply` until
   `status` shows no `transition`. While the autoscaler holds the project lock, the command stops with an
   error, and you can run it again.

1. Confirm that `status` lists only the new slot with `"provisioning_model": "STANDARD"` and that
   `origin:active` names it.

<!-- Cloud Run, Cloud Storage, and Cloud Scheduler are product names, not the old "Cloud" shorthand for GCP. -->
<!-- vale Google.WordList = NO -->

## Watchdog

The watchdog keeps the control VM alive and up to date without an operator workstation. Every 3 minutes, a
Cloud Scheduler job sends `POST /tick` to a Cloud Run service, `src/control_watchdog_server.py`. Each request runs
one lifecycle step in a child process and answers 200 with a JSON summary, or 500 when the step failed:

```bash
control_vm.py ensure --apply --skip-if-idle --skip-if-locked --project=PROJECT
```

With `SPOT_RUNNER_FLOOR_TARGETS` set, the same tick then runs one [runner floor](#runner-floor-step) step,
which keeps at least one minimal Spot runner host for each listed target:

```bash
runner_floor.py --project=PROJECT tick --apply
```

The watchdog is a service, not a Cloud Run job, because of billing. Jobs use instance-based billing with a
one-minute minimum per execution: every 2 minutes, that's about 1.3 million vCPU-seconds, or about $19 a month.
A service with request-based billing pays only while a request runs, in 100 ms steps.

| Part | Name (defaults) | Role |
| --- | --- | --- |
| Cloud Scheduler job | `spot-control-watchdog` | Sends `POST https://spot-control-watchdog-<project number>.<region>.run.app/tick` every 3 minutes with an OIDC identity token of `spot-watchdog-invoker`, whose audience is that URL. No retries. |
| Cloud Run service | `spot-control-watchdog` | No unauthenticated access, concurrency 1, 0 to 1 instances, request-based billing (`--cpu-throttling`), 1 vCPU, 512 MiB (1 GiB with floor targets, because a floor launch holds the billing catalog), 15-minute request timeout. Runs as `spot-control-watchdog`. |
| Watchdog image | `<region>-docker.pkg.dev/<project>/spot-control/control-watchdog` | `watchdog/Dockerfile`: the slim Google Cloud command-line tool image, pinned by digest, plus `src/`, the control VM startup script, and the two runner files that the runner floor renders into runner hosts. |
| State bucket | `gs://<project>-spot-control-state` | Holds `control-vm/control-vm-<project>.json` (the state) and `control-vm/control-vm-<project>.lock` (the lease). With the runner floor, also `runner-floor/floor-<project>-<target key>.json` and `.lock` for each target. |
| Secret Manager | See [Watchdog IAM](#watchdog-iam) | The service's secrets, passed as references in the service spec, never as values. |

The service answers only these requests:

| Request | Answer |
| --- | --- |
| `POST /tick` with the invoker's identity token | 200 or 500 with `{"status", "exit_code", "result", "seconds"}`, and `steps` with each step's exit code and result when the floor step runs; 429 with `Retry-After` while another tick runs |
| `POST /tick` without it | 403, and no step runs |
| `GET /health` | 200 |
| Anything else | 404 or 405 |

The path is `/health`, because Cloud Run reserves some paths that end in `z`, such as `/healthz`.

Cloud Run IAM authenticates every request: the service allows no unauthenticated callers, and only
`spot-watchdog-invoker` holds `roles/run.invoker` on it. The server checks the token again without any
app-level secret. It needs an RS256 signature by a key from Google's published certificates, a Google
issuer, the service URL as audience, an unexpired token, and the verified email of the invoker account.
`--no-verify-signature` keeps the claim checks but skips the signature, for the case where a request reaches
the container with the signature removed.

Each tick step runs in its own process group. A tick whose steps together run longer than 14 minutes answers
500, and the server stops the running step's whole group, including `gcloud` processes that the step started. It sends `SIGTERM`, waits 10
seconds, then sends `SIGKILL`. The server does the same when Cloud Run sends it `SIGTERM` at shutdown, and from then on it starts no further step, so no floor step starts after `SIGTERM`. The group is gone within
855 seconds of the tick's start. No other controller can take the lease earlier than 1,260 seconds after the
start (its 20-minute time to live plus the 60-second grace), so nothing that the tick started still runs when
another controller acts. The lease guard returns only while more than 11 minutes of the lease remain, even
when its own re-read of the lease was slow, which covers the longest single `gcloud` call of 10 minutes.

Each tick works in this order:

1. It reads the state from Cloud Storage without the lease and plans the step read-only.
2. When the step changes nothing, it exits. A noop or wait plan with no maintenance action and no state change
   is idle. Idle executions take no lease and write nothing.
3. Otherwise it takes the lease, reads the state again, and runs the step. Every save is conditioned on the
   state object's generation, and the lease guards every `gcloud` call and every HTTP request.
4. When another controller holds the lease, the tick changes nothing and answers 200.

A tick that fails answers 500, which Cloud Scheduler and Cloud Run report as a failed request.

### Shared state and lease

`SPOT_CONTROL_STATE_URI=gs://<bucket>[/<prefix>]` selects the Cloud Storage backend for `src/control_vm.py`
and for `src/spot_runner_autoscaler.py --manage-control-vm`. Without it, both keep the local file and the
`flock` lock. The backend uses the Cloud Storage JSON API with the standard library only. On Cloud Run the
token comes from the metadata server (`SPOT_GCP_TOKEN_SOURCE=metadata`); on a workstation it comes from
`gcloud auth print-access-token`.

Every write carries an `ifGenerationMatch` precondition, so no write is ever based on a version that its
writer didn't read:

- **Take**: the lease object is created with `ifGenerationMatch=0`. Of two contenders, exactly one create
  succeeds; the other reads the lease and stops.
- **Hold**: the lease records its owner, a token, a 20-minute time to live, and its expiry. Its holder trusts
  it only until the send time of its last successful write plus the time to live. It measures the time since
  then as the larger of its monotonic and its wall-clock deltas, because on macOS the monotonic clock stops
  while the machine sleeps. Before every `gcloud` call, HTTP request, and state save, the holder renews the
  lease once less than 11 minutes are left. A single `gcloud` call times out after 10 minutes, so a call that
  starts inside the lease also ends inside it. A renewal is conditioned on the lease's generation. When it
  fails, the step stops before its next provider call.
- **Suspend**: when the wall clock ran more than 5 seconds ahead of the monotonic clock since the holder last
  confirmed the lease, the process was suspended, so the next guard renews before it acts. A workstation that
  slept past the expiry finds the lease taken and stops. Without such a jump, the guard still reads the lease
  object again every 2 minutes and stops when its generation is no longer the holder's.
- **Break**: another controller breaks a lease only 60 seconds after its expiry. The expiry comes from the
  Cloud Storage server clock: the lease object's `updated` time plus its time to live, or the expiry it
  records when that's later, up to one hour. The break is a write conditioned on the stale generation, so of
  two controllers that break it at once, one wins. A holder that comes back after a break can't renew,
  save, or delete the new lease, because its generation no longer matches.
- **Save**: the state is written with `ifGenerationMatch` set to the generation it loaded or last wrote. A
  save after another controller wrote stops the step.
- **Lost answers**: a write whose answer is lost, for example after a timeout, counts as written only when
  reading the object back shows its unique write ID.
- **Release**: the lease is deleted on exit, conditioned on its generation. A holder that crashed leaves the
  lease in place, and the next controller waits up to 21 minutes before it breaks it.

The state object wraps the same JSON as the local file in an envelope with a schema, the project, and the
writer. An object for another project, or one that isn't that schema, stops every controller until it's
repaired. A missing state object also stops `ensure --apply`, `rotate --apply`, and the autoscaler. That
check keeps a controller from starting over from empty state while a workstation's local file still holds a
transition. `migrate-state --apply` creates the object.

The bucket keeps the default soft delete policy, so overwritten and deleted state versions stay
recoverable for 7 days.

### Runner floor step

The floor step runs after the control VM step, in the time that is left of the same 14 minutes. The server
passes the tick's deadline to every step in `SPOT_TICK_DEADLINE_EPOCH`, and the floor step keeps 20 seconds
of margin. It starts a launch only with at least 5 minutes left, and any other action with at least 2
minutes left, so every call ends before the server stops the group; a hard deadline also stops a hung call.
A launch that doesn't fit waits for the next tick. A launch's bootstrap takes several ticks, and no tick waits
for it. See [Runner floor keeper](fleet-and-autoscaler.md#runner-floor-keeper) for what each step decides.

Each target has its own state object and lease under `SPOT_RUNNER_FLOOR_STATE_URI`, which the deploy sets to
`gs://<bucket>/runner-floor`, with the same lease rules as the control VM. A floor lease taken at any point
in a tick can't be broken before 1,260 seconds after that point, and the tick is gone within 855 seconds of
its start. A step that only waits takes no lease and writes nothing. A target whose lease another controller
holds, such as an operator's autoscaler or the replacement command, is skipped; its result is `locked`, and
the tick still answers 200. A target that fails doesn't stop the others, and makes the tick answer 500.

Each GitHub account's token is its own Secret Manager secret. The service mounts each one as a file at
`/secrets/runner-floor/<secret>/token` and gets `SPOT_RUNNER_FLOOR_TOKEN_SOURCE=secret-files`. The floor step
reads tokens only from those files, never from an environment variable, and the deploy refuses a service
variable whose value looks like a GitHub token. A token needs, for an organization target, permission to
manage the organization's self-hosted runners and read its runner groups (the classic `admin:org` scope), and
for a repository target, administration of that repository (the classic `repo` scope).

The classic OAuth 2.0 token that `gh` holds reaches every organization and repository that its account administers. Two narrower
alternatives need no code change, because the keeper only reads a token from the secret:

- A fine-grained personal access token for each account, with the organization permission **Self-hosted
  runners** set to **Read and write** for each organization target, or the repository permission
  **Administration** set to **Read and write** for each repository target, and nothing else. The organization must allow fine-grained tokens.
  Fine-grained tokens expire, so set a reminder to rotate them: add a new secret version, and the next tick
  reads it.
- A GitHub App installed on each organization and repository with the same permissions. Its installation
  tokens last one hour, so this needs a small token-minting step in front of the secret. The keeper doesn't
  mint App tokens yet.

### Container and registry

The watchdog image is built on the slim Google Cloud command-line tool image, which supplies `gcloud`. The
build adds Debian's `python3` when the image doesn't put one on `PATH`, and it fails unless Python is 3.11 or
newer. The
`.github/workflows/control-watchdog-image.yml` workflow pushes it to Artifact Registry in the project. The
workflow authenticates with Workload Identity Federation, so no service account key exists. Cloud Run can't
pull the private GitHub Container Registry package without a stored credential, and an image in the same
project needs no pull credential at all.

The alternative, a stock image that downloads a pinned repository archive at start-up, was rejected. It
would make every recovery depend on GitHub being reachable, and it would need its own checksum
verification. The custom image fixes the code and its dependencies in one digest, and the Cloud Run service runs
exactly the digest that the deploy pinned. A pushed image changes nothing until you deploy its digest.

The workflow stays inert until four repository variables are set, which the deploy dry run prints:
`WATCHDOG_IMAGE`, `WATCHDOG_PUBLISHER_SA`, `WATCHDOG_WIF_PROVIDER`, and `WATCHDOG_BASE_IMAGE`. The workflow
refuses a `WATCHDOG_BASE_IMAGE` that isn't `gcr.io/google.com/cloudsdktool/google-cloud-cli:<version>-slim`
pinned by digest. The OIDC provider admits only that workflow on `main` of `swack-tools/spot-github-runners`.

### Watchdog IAM

`src/control_watchdog_deploy.py` grants each role on the narrowest resource that works:

| Member | Role | Resource | Why |
| --- | --- | --- | --- |
| `spot-control-watchdog` | `roles/compute.instanceAdmin.v1` | Project, with the IAM condition `spot-control-vms-only` | List, describe, create, start, stop, set scheduling on, and delete the control VMs and their boot disks; list machine types; use the subnetwork and an external IP. The condition limits instances and disks to names that start with `spot-control-`. |
| `spot-control-watchdog` | `roles/compute.instanceAdmin.v1` | Project, with the IAM condition `spot-runner-floor-hosts`; only with floor targets | Create, describe, start, set metadata and labels on, and delete runner floor hosts and their disks. The condition limits instances to names that start with `oxidex-runners-`, and disks to those names and names that start with `runner-data`. The control VM binding stays as it is, so removing this binding turns the floor's compute access off. |
| `spot-control-watchdog` | `roles/iam.serviceAccountUser` | The control VM service account only | Create control VMs that run as that account. Floor hosts run with no service account, so the floor needs no `actAs` on any account. |
| `spot-control-watchdog` | `roles/storage.objectAdmin` | The state bucket only | Read and conditionally write the state and lease objects. |
| `spot-control-watchdog` | `roles/secretmanager.secretAccessor` | Each referenced secret only | Read the service's secrets when an instance starts. |
| `spot-watchdog-invoker` | `roles/run.invoker` | The Cloud Run service only | Let Cloud Scheduler call this one service. No other member, and never `allUsers`. |
| `spot-watchdog-publisher` | `roles/artifactregistry.writer` | The `spot-control` repository only | Let the image workflow push the watchdog image. |
| The `swack-tools/spot-github-runners` principal set | `roles/iam.workloadIdentityUser` | The publisher service account only | Let the image workflow on `main` impersonate the publisher. |

The service references these secrets:

| Service variable | Secret | Note |
| --- | --- | --- |
| `CLOUDFLARE_KV_API_TOKEN` | `spot-dashboard-cloudflare-kv-token` | The control VMs already use it. |
| `DASHBOARD_CONTROL_SECRET` | `spot-dashboard-control-secret` | The control VMs already use it. |
| `CF_ACCESS_CLIENT_ID` | `spot-control-access-client-id` | New: the Access service token for the tunnel hostnames. |
| `CF_ACCESS_CLIENT_SECRET` | `spot-control-access-client-secret` | New. |
| `SPOT_DASHBOARD_IMAGE_TRACK_TOKEN` | `spot-control-watchdog-github-token` | New, only with image tracking. |
| None: a file at `/secrets/runner-floor/<secret>/token` | One `secret=` of `SPOT_RUNNER_FLOOR_TARGETS` for each GitHub account | New, only with floor targets. Each account's secret is separate, and none may back another service variable. The deploy labels it `spot-runner-floor=token`, and removes the access again once no target names it. |

`--secret ENV=SECRET` references another secret for a variable. The deploy never writes a secret value. It
stops before any IAM change when a referenced secret has no enabled version.

The IAM condition uses `resource.name.extract()`. A mistake in a condition only shows when the
watchdog needs that permission, which can be during an outage, so check the create and delete permissions
once after the deploy:

```bash
for permission in compute.instances.create compute.instances.delete compute.disks.create; do
  gcloud policy-intelligence troubleshoot-policy iam \
    "//compute.googleapis.com/projects/PROJECT/zones/us-central1-a/instances/spot-control-a" \
    --principal-email="spot-control-watchdog@PROJECT.iam.gserviceaccount.com" --permission="$permission"
done
```

With floor targets, check the floor condition the same way against a runner host name, such as
`.../instances/oxidex-runners-check` and `.../disks/runner-data-check` with `compute.disks.delete`.

If a check reports the access as denied, deploy again with `--no-compute-condition`, which grants the role on
the project without either condition.

### Deploy the watchdog

Prerequisites:

- The control VM environment file that `src/control_vm.py` uses, sourced in your shell. The deploy copies its
  non-secret settings into the service: `SPOT_CONTROL_*`, `SPOT_DASHBOARD_*`, `CLOUDFLARE_ACCOUNT_ID`,
  `CLOUDFLARE_KV_NAMESPACE_ID`, `SPOT_CONFIG_REPO`, and the dashboard target and session settings.
- Owner access to the project, for the first deploy.

To deploy:

1. Create the new secrets from standard input, so no value appears in a command line or the shell history:

   ```bash
   printf '%s' "$CF_ACCESS_CLIENT_ID" | gcloud secrets create spot-control-access-client-id \
     --data-file=- --replication-policy=automatic --project=PROJECT
   printf '%s' "$CF_ACCESS_CLIENT_SECRET" | gcloud secrets create spot-control-access-client-secret \
     --data-file=- --replication-policy=automatic --project=PROJECT
   ```

1. Preview the infrastructure. This command runs nothing:

   ```bash
   PYTHONPATH=src python3 src/control_watchdog_deploy.py --project=PROJECT --dry-run
   ```

1. Create the bucket, the service accounts, the repository, the IAM bindings, and the identity provider.
   Every step is idempotent, so you can run it again:

   ```bash
   PYTHONPATH=src python3 src/control_watchdog_deploy.py --project=PROJECT
   ```

1. Set the four repository variables that the dry run printed, then run the image workflow and copy the
   digest from its summary:

   ```bash
   gh workflow run control-watchdog-image.yml --repo swack-tools/spot-github-runners --ref main
   ```

1. Move the control VM state to Cloud Storage, as described in
   [Migrate from the workstation state](#migrate-from-the-workstation-state).

1. Deploy the service without the scheduler, call it once as the invoker account, and read its answer:

   ```bash
   IMAGE=us-central1-docker.pkg.dev/PROJECT/spot-control/control-watchdog@sha256:DIGEST
   PYTHONPATH=src python3 src/control_watchdog_deploy.py --project=PROJECT --image="$IMAGE" --skip-scheduler --dry-run
   PYTHONPATH=src python3 src/control_watchdog_deploy.py --project=PROJECT --image="$IMAGE" --skip-scheduler
   URL="https://spot-control-watchdog-$(gcloud projects describe PROJECT --format='value(projectNumber)').us-central1.run.app"
   TOKEN="$(gcloud auth print-identity-token --audiences="$URL" --include-email \
     --impersonate-service-account=spot-watchdog-invoker@PROJECT.iam.gserviceaccount.com)"
   curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "$URL/tick"
   ```

   Impersonation needs `iam.serviceAccounts.getOpenIdToken` on the invoker account, which a project owner has.
   A 403 from the server, with `Watchdog tick refused` in the service log, names the claim that failed.

1. Deploy again without `--skip-scheduler` to create the scheduler job.

To update the watchdog later, run the image workflow and deploy the new digest with `--image`.

### Turn on the runner floor

The floor needs a watchdog image built from a commit that has `src/runner_floor.py`. To turn it on:

1. Create one secret for each GitHub account that administers a floor target, from standard input. For
   example, with the account's token in the shell variable `TOKEN`:

   ```bash
   printf '%s' "$TOKEN" | gcloud secrets create spot-floor-github-ACCOUNT \
     --data-file=- --replication-policy=automatic --project=PROJECT
   ```

1. Add `SPOT_RUNNER_FLOOR_TARGETS` to the environment file and source it. Optionally add
   `SPOT_RUNNER_FLOOR_REGIONS` and `SPOT_RUNNER_FLOOR_MAX_DAILY_USD`. See
   [Floor targets](fleet-and-autoscaler.md#floor-targets).
1. Preview the deploy. The dry run shows the second compute binding, each secret mount, and the floor
   settings:

   ```bash
   PYTHONPATH=src python3 src/control_watchdog_deploy.py --project=PROJECT --image="$IMAGE" --dry-run
   ```

1. Preview the floor from a workstation. Both commands read only, and use `gh` for each target's
   `account=`:

   ```bash
   SPOT_RUNNER_FLOOR_STATE_URI=gs://PROJECT-spot-control-state/runner-floor \
     PYTHONPATH=src python3 src/runner_floor.py status --offers
   SPOT_RUNNER_FLOOR_STATE_URI=gs://PROJECT-spot-control-state/runner-floor \
     PYTHONPATH=src python3 src/runner_floor.py tick
   ```

1. Deploy without `--dry-run`. From the next tick on, each target without a running managed host gets one.
1. Set `SPOT_RUNNER_FLOOR_STATE_URI` to the same location in the environment of every autoscaler that manages
   a floor target, so that it holds the target's floor lease while it runs.

To turn the floor off, remove `SPOT_RUNNER_FLOOR_TARGETS`, preview the deploy with `--dry-run`, and deploy
again. Every deploy removes the grants the floor no longer needs, and skips those already gone:

- With no floor target, it removes the `spot-runner-floor-hosts` compute binding.
- The deploy labels each floor token secret `spot-runner-floor=token` when it grants access to it. It then
  removes the runtime account's `roles/secretmanager.secretAccessor` binding, and the label, from every
  labeled secret that the service no longer references. That covers every former floor token secret,
  including one that only left a target's `secret=`.
- After it updates the service, it removes every secret volume that no container mounts. `--set-secrets`
  drops the mount of a former floor token but keeps its volume, which still names the secret. The removal
  creates one more revision, and only when such a volume exists.

Removing the floor never deletes a secret or a VM. Floor hosts that exist keep running until the autoscaler or
an operator retires them.

### Migrate from the workstation state

The migration uploads the local `control-vm-<project>.json` once. It refuses when both the local file and the
Cloud Storage object exist and differ, and then changes nothing.

1. Stop every local controller from managing the control VM. Remove `--manage-control-vm` from the
   autoscaler's LaunchAgent, or wait until you finish the migration before you restart the agent.
1. Add `SPOT_CONTROL_STATE_URI=gs://<project>-spot-control-state/control-vm` to the environment file and
   source it.
1. Preview, then migrate:

   ```bash
   PYTHONPATH=src python3 src/control_vm.py migrate-state
   PYTHONPATH=src python3 src/control_vm.py migrate-state --apply
   ```

   The command holds the local lock and the Cloud Storage lease while it works. It fences the local file
   before the state object exists. First it copies the file to
   `control-vm-<project>.json.migrated-<UTC time>` and flushes the copy. Then it renames a flushed marker
   over the original in one atomic step, so the path always holds the state or the marker. Only then does it
   upload the state, or create an empty state object when there was no local file. From then on, a controller
   without `SPOT_CONTROL_STATE_URI` refuses to run instead of acting on the old file. A missing state file next to
   a `.migrated-*` backup also refuses.

   If the upload fails, the local file stays fenced, and running the command again uploads the backup that the
   marker names. When there is no local file but the state object exists, the command only installs the marker.
   It refuses a marker that names another `SPOT_CONTROL_STATE_URI` (a trailing slash doesn't count), and a
   missing state file next to a `.migrated-*` backup, rather than creating an empty state object.
1. Check that `PYTHONPATH=src python3 src/control_vm.py status` shows the same `transition` and
   `retained_disks` as before.

A workstation that keeps `--manage-control-vm` must use the same `SPOT_CONTROL_STATE_URI`. The watchdog and
the workstation then share one state and one lease, and a check that finds the lease taken skips its
control VM step. Every controller in the project needs the same `SPOT_CONTROL_PROVISIONING` and
`SPOT_DASHBOARD_IMAGE_TRACK`.

### Pause the watchdog

Pause the scheduler job. A tick in progress finishes its step:

```bash
gcloud scheduler jobs pause spot-control-watchdog --location=us-central1 --project=PROJECT
gcloud scheduler jobs resume spot-control-watchdog --location=us-central1 --project=PROJECT
```

While it's paused, `src/control_vm.py ensure --apply` with the same `SPOT_CONTROL_STATE_URI` still works from a
workstation. Pausing the scheduler also pauses the runner floor; `src/runner_floor.py tick --apply` with the same
`SPOT_RUNNER_FLOOR_STATE_URI` still works from a workstation.

### Image tracking

`SPOT_DASHBOARD_IMAGE_TRACK=main` rolls the control VM to the image that
`ghcr.io/swack-tools/spot-dashboard:main` names. On each check the manager resolves that tag's digest with
`SPOT_DASHBOARD_IMAGE_TRACK_TOKEN`, a classic GitHub token with `read:packages`. The token also needs `repo`
while the repository is private. The manager starts a rollout only when all of these hold:

- No transition is in progress, the active VM is running and healthy, and the other slot is empty.
- The rotation cooldown has passed: 6 hours since the last completed or aborted transition.
- The digest differs from the active VM's image, and no earlier rollout to it aborted.
- The image's `org.opencontainers.image.revision` label names a commit whose **Dashboard image** workflow
  run on `main` concluded `success`.
- GitHub's compare API reports that commit `ahead` of the active image's commit. The manager never rolls
  to an older, equal, or diverged commit, or when either commit is unknown.

The rollout is the make-before-break rotation that a price rotation uses: create the other slot with the
new digest, wait for health, freeze, import, check the version floor, write `origin:active`, wait for
propagation, and delete the old VM. A rollout that aborts backs off that digest. The manager tries again
only after `:main` names another digest and the cooldown has passed.

The state's `image` section records the active VM's image, read once from its `dashboard-image` metadata
or written when a transition completes. It also records the digests that failed and cached workflow answers.
`status` shows it. While tracking is on, a recovery recreates the recorded image, not `SPOT_DASHBOARD_IMAGE`,
so a recovery never moves to an older image. Every transition records the image it creates, and a controller
that resumes the transition creates that image whatever its own settings.

A rollout never changes the provisioning mode. When the active VM's model differs from
`SPOT_CONTROL_PROVISIONING`, the manager holds the rollout and logs why. For example, a standard VM while Spot
is configured, or a Spot VM or one of unknown model while standard is configured. The planner's own reason, such
as the explicit `rotate` needed to move back to Spot, stays in the plan.

The manager reads the registry and GitHub only. A registry or GitHub failure never starts a rollout and never
blocks recovery.

### Restart policy

Control VMs keep `--no-restart-on-failure`, and the manager turns automatic restart off on any control VM
that has it, with `gcloud compute instances set-scheduling --no-restart-on-failure`. With the watchdog, a VM
that crashed is started again within about 3 minutes.

Compute Engine's automatic restart acts only after a crash or a host error, never after an API stop, so a fence
that stops a VM isn't affected by it. But the recovery fence and the rollback fence accept any `TERMINATED`
VM as fenced, and they can't tell a stopped VM from one that crashed. With automatic restart, a crashed VM
can start again by itself after the fence passed, while `origin:active`, which is eventually consistent,
may still name it. A frozen VM would come back frozen, because the freeze marker is on its boot disk. A VM
that was fenced by stopping it, or one that crashed before it could freeze, has no such marker.

So before either fence accepts a stopped VM, its inventory must show `automaticRestart` off. When it's on,
the manager turns it off and waits for a fresh inventory. Turning it off doesn't restart the VM.

### Cost

The estimate assumes `us-central1`, the default schedule of every 3 minutes (14,400 requests a month), and about
8 seconds of CPU time per tick at 1 vCPU and 512 MiB. Request-based billing charges only while a request runs,
plus instance start-up, in 100 ms steps. Its free tier is 180,000 vCPU-seconds, 360,000 GiB-seconds, and 2
million requests a month. Check it against current pricing.

| Item | Use | Monthly cost |
| --- | --- | --- |
| Cloud Run service CPU | About 115,000 vCPU-seconds, 64 percent of the free tier; it runs out at about 12.5 seconds a tick | $0 |
| Cloud Run service memory | About 58,000 GiB-seconds, 16 percent of the free tier | $0 |
| Cloud Run requests | 14,400, inside the 2 million free | $0 |
| Cloud Scheduler | 1 job; 3 jobs per billing account are free | $0 |
| Secret Manager access | 4 or 5 secrets at each instance start: at most about 58,000 accesses, when every request starts an instance; 10,000 are free | $0 to $0.15 |
| Secret Manager versions | 2 or 3 new secret versions beyond the 6 free ones | $0.12 to $0.18 |
| Cloud Storage | A few KB; about 14,400 reads, inside the 50,000 free; writes only on ticks that act | $0 |
| Artifact Registry | Under the 0.5 GB of free storage until many versions accumulate; then $0.10 per GB | $0 to $0.05 |
| Logging and network | A few log lines per tick; KB of egress | $0 |

The total is about $0.12 to $0.40 a month, mostly Secret Manager.

The floor step adds about 1 second of CPU time a tick when it only checks: one Compute Engine listing, one
runner listing per target, and one state read per target. That's about 14,000 vCPU-seconds, so the service
stays inside the free tier. With floor targets the service has 1 GiB, which doubles the memory use to about
116,000 GiB-seconds, 32 percent of the free tier. The deploy also turns on the Cloud Billing API, which the
floor needs to price offers. The extra reads, about 43,000 a month for three targets, can pass the 50,000 free
Cloud Storage reads, at $0.004 per 10,000. Each tick also reads the mounted token secrets, about 43,000
accesses a month for three targets, or about $0.13. A launch tick also downloads the billing catalog.

The floor hosts cost far more than the watchdog. Each is a 4-vCPU Spot VM with a 40 GiB boot disk and a
100 GiB data disk, at less than `SPOT_RUNNER_FLOOR_MAX_DAILY_USD` (default $1.50 a day, about $45 a month) for
compute and disks. Its ephemeral external IPv4 address adds about $1.80 a month at the Spot rate. Three floor
targets therefore cost at most about $140 a month, and usually less: `src/runner_floor.py status --offers` prints
the current totals.

`--schedule='*/2 * * * *'` makes 21,600 requests a month. At 8 seconds a tick that's about 173,000 vCPU-seconds,
96 percent of the free tier, and beyond about 8.3 seconds a tick it costs $0.000024 per extra vCPU-second, for
example about $0.86 a month at 10 seconds. A Cloud Run job at the same 2-minute cadence would bill at least 60
seconds per execution: about 1.3 million vCPU-seconds, or about $19 a month after its free tier.

<!-- vale Google.WordList = YES -->

## Configuration

Set these in the autoscaler's environment file outside the Git checkout. See `.env.example` for the full
list.

| Variable | Purpose |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_KV_NAMESPACE_ID`, `CLOUDFLARE_KV_API_TOKEN` | KV access for `origin:active` and snapshots. |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | Access service token for the tunnel hostnames. |
| `DASHBOARD_CONTROL_SECRET` | Signs control calls (`freeze`, `unfreeze`, `snapshot`, `import`). |
| `SPOT_DASHBOARD_IMAGE` or `SPOT_DASHBOARD_SOURCE_REF` | A digest-pinned image, or a 40-character commit to build on the VM. |
| `SPOT_CONTROL_PROVISIONING` | `spot` (default) or `standard`, the free-tier e2-micro, which needs a published image. See [Use the free-tier control VM](#use-the-free-tier-control-vm). |
| `SPOT_CONTROL_STATE_URI` | Optional `gs://<bucket>[/<prefix>]` bucket location for the control VM state and lease. See [Watchdog](#watchdog). |
| `SPOT_RUNNER_FLOOR_TARGETS` | Optional runner floor targets, each with the Secret Manager secret of its account. See [Floor targets](fleet-and-autoscaler.md#floor-targets). |
| `SPOT_RUNNER_FLOOR_STATE_URI` | The `gs://<bucket>/runner-floor` location of the floor state and leases. The deploy sets it on the service; set it for an autoscaler that manages a floor target. |
| `SPOT_RUNNER_FLOOR_REGIONS`, `SPOT_RUNNER_FLOOR_MAX_DAILY_USD` | Optional floor host regions (default `us-central1,us-east1,us-west1`) and daily cap per host (default `1.50`). |
| `SPOT_DASHBOARD_IMAGE_TRACK`, `SPOT_DASHBOARD_IMAGE_TRACK_TOKEN` | Optional `main` to roll out the published `:main` image, and the GitHub token that reads it. See [Image tracking](#image-tracking). |
| `SPOT_CONFIG_REPO` | Optional config repository, as `owner/repo`, whose `repos/<owner>/<repo>.yaml` files are controlled targets. The autoscaler's `--config-repo` takes precedence. Default: `<org>/spot-runners-config`. |
| `DASHBOARD_CONTROLLED_TARGETS` | Optional comma-separated organizations and `owner/repo` targets that the signed-in user must administer. Default: none. |

The autoscaler passes `SPOT_CONFIG_REPO` and `DASHBOARD_CONTROLLED_TARGETS` to new control VMs as the
`dashboard-config-repo` and `dashboard-controlled-targets` instance metadata values. Neither is a secret.
The targets travel separated by semicolons, because a `gcloud --metadata` list can't hold commas. At every
boot, the startup script writes both to the dashboard environment file, with the targets separated by
commas. The autoscaler and `src/control_vm.py` refuse to create a control VM when either value is malformed.
The config repository must be `owner/repo`, and every target must be an organization or `owner/repo`, as the
dashboard parses them. The startup script applies the same checks and stops with an error on a malformed
value. A running control VM keeps its creation-time values. To change them on that VM, update its metadata
and then reboot it. Rotating to the other slot also applies the new values.

The control VM reads its secrets from Secret Manager at boot: the session key, the OAuth 2.0 client ID and secret,
the control secret, the Workers KV token, a GitHub token, and one tunnel token for each slot. The control VM's
service account needs access to those secrets. Keep their values out of instance metadata and out of Git.

## Commands

Preview the next lifecycle step without changing anything:

```bash
PYTHONPATH=src python3 src/control_vm.py plan
```

Write the startup script for review:

```bash
PYTHONPATH=src python3 src/control_vm.py render-startup --output "$HOME/oxidex-ops/control-vm-startup.sh"
```

Pass `--provisioning standard` to any command to try the free-tier mode without changing the environment file.

Run one lifecycle step. This command creates, starts, or deletes VMs:

```bash
PYTHONPATH=src python3 src/control_vm.py ensure --apply
```

Move the control VM state to `SPOT_CONTROL_STATE_URI` once. Without `--apply` it only reports what it would do:

```bash
PYTHONPATH=src python3 src/control_vm.py migrate-state --apply
```

Preview the watchdog deployment. Without `--dry-run`, this command creates or updates the resources:

```bash
PYTHONPATH=src python3 src/control_watchdog_deploy.py --dry-run
```

Run the Worker tests:

```bash
node --test cloudflare/
```
