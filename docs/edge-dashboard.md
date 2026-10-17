# Edge dashboard on a spot control VM

The web dashboard runs on one small Spot VM, the control VM, behind a Cloudflare Worker at
`https://spot.swacktech.com`. Only GitHub user `swackhamer` can sign in, and only when that account
administers every controlled repository.

## Architecture

| Component | Location | Role |
| --- | --- | --- |
| Cloudflare Worker | `cloudflare/worker.js` | Verifies the session cookie, proxies to the active control VM, and returns a wait response while the control VM is down. |
| Workers KV | Binding `SPOT_KV` | `origin:active` names the active slot. `dashboard:state` holds the latest dashboard state snapshot. |
| Control VM | `spot-control-a` or `spot-control-b` | Runs the dashboard container and a `cloudflared` tunnel. Created by `src/control_vm.py`. |
| Tunnel hostnames | `spot-origin-a.swacktech.com`, `spot-origin-b.swacktech.com` | One Cloudflare Tunnel per slot, protected by a Cloudflare Access service token. |
| Startup script | `cloud-init/control-vm-startup.sh` | Fetches secrets, pulls or builds the image, and starts both containers on every boot. |
| Dashboard image | `dashboard/Dockerfile` | Built on the VM from a pinned commit, or pulled from `ghcr.io/swack-tools/spot-dashboard`. |

The dashboard listens only on `127.0.0.1:8787`. The control VM has no inbound firewall rule for it.

## Sign-in

The dashboard uses a GitHub OAuth 2.0 app with the authorization-code flow and PKCE. Sign-in succeeds only
when all of these are true:

- The GitHub login is `swackhamer` and the numeric user ID is `619624`.
- The user has administrator permission on every controlled target. Targets are the union of
  `DASHBOARD_CONTROLLED_TARGETS`, targets saved in the dashboard, and every `repos/<owner>/<repo>.yaml`
  file in the config repository. The config repository is `--config-repo`, then `SPOT_CONFIG_REPO`,
  then `<org>/spot-runners-config` with the organization from `--org` (default `swack-tools`).
- The dashboard can list the config repository. If the listing fails, sign-in fails closed with a 403.

The session cookie `__Host-spot_session` is HMAC-signed with `DASHBOARD_SESSION_SECRET`. The Worker and
every control VM share that secret, so sessions survive VM replacement. The `__Host-` prefix makes browsers
reject the cookie unless it is `Secure`, has `Path=/`, and has no `Domain`, so another `swacktech.com`
subdomain can't set or shadow it. The sign-in `state` cookie is `__Host-spot_oauth_state`.

A session can't be revoked on its own, and a lost GitHub administrator permission takes effect at the
next sign-in. To sign everyone out, set a cut-off time that rejects every session issued before it:

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
dashboard would refuse to start. Rotating `spot-dashboard-session-secret` together with the Worker's
`SESSION_SECRET` also signs everyone out.

Sign-in routes are rate limited per browser IP and globally, and answer HTTP 429 with `Retry-After` when
over the limit. The Worker passes the browser IP in `X-Spot-Client-Ip` and drops any copy that the
browser sends. The dashboard trusts that header only from a loopback peer, the local `cloudflared`.

### Controlled target changes

`POST /api/auth/targets` changes the targets saved in the dashboard, and only a GitHub session can call
it. The request doesn't change anything yet. It answers HTTP 202 with `"status": "reauth_required"` and
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
- A VM accepts changes only after it reads `origin:active` and confirms that the record names its slot,
  or that no record exists yet. `/api/health` reports `ownership` as `active`, `inactive`, or `unknown`.
  Until ownership is `active`, changes get the wait response.

### Runner patches

The **Runner Patches** tab and `/api/patches` edit the `spot-runner-patch-v1` manifests in the config
repository: `defaults.yaml` for the organization and `repos/<owner>/<repo>.yaml` for each repository.

| Route | Purpose |
| --- | --- |
| `GET /api/patches` | Lists the manifests at the default branch HEAD. |
| `GET /api/patches/manifest?path=...` | Returns a manifest, its blob and base commit, and the effective patch with its sha256. |
| `POST /api/patches/validate` | Returns field errors such as `host.apt_packages`. A repository manifest is an override: checks that depend on inherited settings, such as `hardware.isa` against the architecture, use the settings merged with `defaults.yaml`. |
| `POST /api/patches/preview` | Classifies each runner host as `noop`, `hot_patch`, `recreate_containers`, or `replace_vm`. |
| `POST /api/patches/save` | Opens a pull request. Needs an `Idempotency-Key`. |
| `POST /api/patches/hot-patch` | Hot-patches the recorded runner hosts. Needs `"confirm": true` and an `Idempotency-Key`. |

- Saving creates a `spot-dashboard/...` branch from the `base_sha` that the editor loaded, commits the
  file there, and opens a pull request. It never writes to the default branch. If the default branch
  moved, or GitHub reports that the file changed, the dashboard answers HTTP 409. The token in
  `SPOT_CONFIG_GH_TOKEN` or `GH_TOKEN` needs contents and pull-request write access to the config
  repository.
- `src/spot_runner_manager.py` records the patch on each runner host as instance metadata. It does this
  when it provisions the host, and after a live patch, but only once the host confirms
  `LIVE_PATCH_APPLIED:<sha256>`. The keys are:
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
- On the control VM, `DASHBOARD_FLEET_INVENTORY=gce` makes the preview list runner hosts from Compute
  Engine. A runner host carries the `spot-target` label or the patch metadata. The control VM's
  service account needs `compute.instances.list`. Elsewhere the preview uses the host records in the
  dashboard state.
- The preview checks each host's observed capacity against every hardware constraint of the patch first:
  `hardware.min_vcpu`, `hardware.min_ram_gib`, the architecture, and the CPU platform for `hardware.isa`. A
  host that can't meet them is `replace_vm` with the reason, and a host whose capacity is unknown is
  `unknown`. The hot-patch executor and `--patch-live` refuse such hosts before they change anything.
- The preview then classifies each host from its recorded manifest, after checking it against the recorded
  sha256. Hosts provisioned before patch recording show as `unknown`, with a reason. They stay that way
  until they're reprovisioned or hot-patched once with `--patch-live`.
- Hot-patching applies the saved effective patch, never a draft. It runs only when every host is
  `hot_patch` or `noop` and the `patch_sha256` from the preview is still current. It reports a result per
  host. A repeated request with the same `Idempotency-Key` returns the stored result without touching
  the hosts again. Like every change, it gets the wait response while the dashboard is frozen,
  unhydrated, or not the active slot.
- The control VM can't reach runner hosts. Its image has no `gcloud` and no SSH access, so hot-patching
  there answers HTTP 501 with `"code": "hot_patch_unavailable"`. Run
  `src/spot_runner_manager.py --patch-repo OWNER/REPO --patch-live` from an operator workstation instead. A
  dashboard that runs where `gcloud` and operator credentials exist can set
  `DASHBOARD_HOT_PATCH_EXECUTOR=gcloud`. That path takes the region, runner label, repository, runner
  count, and heavy pool size from the host's recorded metadata. It refuses a host that's missing any
  of them, because the live patch would otherwise reset the host's GitHub scheduling labels to the
  `spot_runner_manager.py` defaults. An org-registered host also needs its runner group.
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
- GitHub and Compute Engine failures answer HTTP 424 with `"code": "upstream_error"`. Every dashboard
  response carries `X-Spot-Origin: dashboard`. The Worker passes a marked 5xx response through
  unchanged, and treats only unmarked 5xx responses and timeouts as an outage.
- A freeze waits up to 45 seconds for running saves and hot-patches to finish, so the handover snapshot
  includes their results. If they're still running after that, the freeze answers HTTP 409 with
  `"retryable": true`, and the autoscaler tries again.

When `GITHUB_OAUTH_DEVICE_FLOW=1`, the Worker also passes `/auth/device/code` and `/auth/device/token`
through without a session cookie. The dashboard still refuses them unless device login is enabled. The
GitHub device code stays on the control VM behind a random handle, and the dashboard enforces the polling
interval. A VM replacement ends a device sign-in in progress, so request a new code.

The Worker reaches the control VM through its tunnel hostname, so the request `Host` differs from the
browser `Origin`. The dashboard accepts a `POST` only when `Origin` matches its own `Host` or its public
origin, and it never trusts `X-Forwarded-Host` for this check. The public origin comes from
`--public-origin`, then `DASHBOARD_PUBLIC_ORIGIN`, then the origin of `GITHUB_OAUTH_REDIRECT_URI`. The
startup script writes `DASHBOARD_PUBLIC_ORIGIN` from the `dashboard-public-url` metadata value.

## State durability

- The dashboard writes state atomically to `/var/lib/spot-dashboard/state.json` on the boot disk.
  Control VMs use the `STOP` termination action, so a preemption keeps the boot disk.
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
- The browser sends an `Idempotency-Key` header with every change. The dashboard stores the response in
  the same atomic save as the change, so a retried request never applies twice.
- A freeze writes the marker file `state.json.frozen` next to the state file. If the container restarts
  while frozen, the dashboard starts frozen and `/api/health` reports `frozen`. The marker stays until an
  explicit unfreeze or a successful import.

## Outages and recovery

While the control VM is down, the Worker answers with HTTP 503 and the body
`{"error": "control_vm_unavailable", ...}`. Page loads get a waiting page that polls `/api/health`.
The dashboard page shows a banner, keeps its in-memory state, and retries the failed request with the
same idempotency key after `/api/health` returns 200.

The autoscaler handles the control VM when you run it with `--manage-control-vm`:

- **Preempted VM**: the autoscaler starts it again. If the start keeps failing, it creates the other slot,
  which loads state from KV, then points `origin:active` at it. If the old VM still answers but reports
  `frozen`, the autoscaler freezes it again, or reads its snapshot, and imports that state before the
  switch. If the old VM is down, the autoscaler imports the latest KV snapshot into the new VM and
  waits until the new VM reports at least that version before it updates `origin:active`.
- **Price rotation**: the autoscaler creates the other slot, waits for health, freezes the old VM, copies
  its snapshot into the new VM, writes `origin:active` with read-back verification, waits for KV to
  propagate, then deletes the old VM. A failed origin write unfreezes the old VM and deletes the new one.
- **Replacement check before deletion**: the autoscaler deletes the old VM only after a fresh check shows
  the new VM is `RUNNING`, healthy, and reporting its own slot. If the new VM disappears, or stays unready
  for 5 minutes while the old VM still runs, the autoscaler rolls back. It freezes the new VM, imports
  the newer of its snapshot and the KV snapshot into the old VM, unfreezes the old VM, points
  `origin:active` back at it, and deletes the new VM. Without a running old VM, it waits and deletes
  nothing.
- **Uncertain creates**: if `gcloud` reports a failed create without a capacity or quota error, the
  autoscaler checks that zone for the instance before it tries another zone, and adopts the instance if
  it exists. If it can't tell, it waits.
- **Duplicate slots**: if two VMs carry the same slot, the autoscaler logs an error and changes nothing,
  including `origin:active`, until an operator deletes the extra VM.
- **Retained boot disks**: when the autoscaler can't confirm that a VM's state reached its successor,
  it deletes the VM with `--keep-disks=boot`. The boot disk keeps the VM's name. The autoscaler logs an
  error and records the disk under `retained_disks` in its controller state, and later creates for that
  slot prefer other zones. Recover the state from the disk, then delete the disk by hand. This happens
  when a rollback can't freeze the new VM or can't confirm the import, and when a recovery switched
  slots without importing from the old VM.

Recovery depends on the autoscaler running. If the workstation that runs it is off, the dashboard stays
down until the autoscaler runs again.

## Configuration

Set these in the autoscaler's environment file outside the Git checkout. See `.env.example` for the full
list.

| Variable | Purpose |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_KV_NAMESPACE_ID`, `CLOUDFLARE_KV_API_TOKEN` | KV access for `origin:active` and snapshots. |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | Access service token for the tunnel hostnames. |
| `DASHBOARD_CONTROL_SECRET` | Signs control calls (`freeze`, `unfreeze`, `snapshot`, `import`). |
| `SPOT_DASHBOARD_IMAGE` or `SPOT_DASHBOARD_SOURCE_REF` | A digest-pinned image, or a 40-character commit to build on the VM. |

The control VM reads its secrets from Secret Manager at boot: `spot-dashboard-session-secret`,
`spot-dashboard-oauth-client-id`, `spot-dashboard-oauth-client-secret`, `spot-dashboard-control-secret`,
`spot-dashboard-cloudflare-kv-token`, `spot-dashboard-github-token`, `spot-dashboard-tunnel-token-a`, and
`spot-dashboard-tunnel-token-b`.

## Commands

Preview the next lifecycle step without changing anything:

```bash
PYTHONPATH=src python3 src/control_vm.py plan
```

Write the startup script for review:

```bash
PYTHONPATH=src python3 src/control_vm.py render-startup --output "$HOME/oxidex-ops/control-vm-startup.sh"
```

Run one lifecycle step. This command creates, starts, or deletes VMs:

```bash
PYTHONPATH=src python3 src/control_vm.py ensure --apply
```

Run the Worker tests:

```bash
node --test cloudflare/
```
