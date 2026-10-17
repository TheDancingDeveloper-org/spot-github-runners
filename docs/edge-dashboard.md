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

The session cookie is HMAC-signed with `DASHBOARD_SESSION_SECRET`. The Worker and every control VM share
that secret, so sessions survive VM replacement.

When `GITHUB_OAUTH_DEVICE_FLOW=1`, the Worker also passes `/auth/device/code` and `/auth/device/token`
through without a session cookie. The dashboard still refuses them unless device login is enabled.

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
  exists.
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
