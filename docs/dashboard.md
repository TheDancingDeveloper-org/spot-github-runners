# Dashboard

The dashboard is a web user interface (UI) for the Spot runner fleet and the dedicated Rust builders. It combines telemetry (cost, capacity, qualifying Spot offers, scaling signals, build records) with controls for policy, templates, regions, administrators, and runner patches. A single Python process serves the page and the JSON API, and it needs no runtime dependency beyond the Python standard library.

The public deployment runs at `https://spot.swacktech.com`, behind a Cloudflare Worker, on a small control VM. This page describes what the dashboard does and how to run it locally. For the edge hosting, state durability, outages, and the free-tier control VM, see the [edge dashboard reference](edge-dashboard.md).

## Pages

The dashboard is one page with tabs. The **Admins** tab appears only for a signed-in owner. The page also shows a summary row of cards (fleet burn rate, online runners, online builders, qualifying offers, and scaling alerts) and a budget bar.

| Tab | What it does |
| --- | --- |
| World Map and Fleet Inspector | Shows data center pins by region. You can inspect running runners, scale a region, and copy the `gcloud compute ssh` command. |
| Instances and Cost History | Lists running and deleted instances with sorting, search, a time window, and a total-price column. |
| Sizing and Benchmark Qualifier | Sets per-runner vCPU and RAM, minimum counts, the CPU benchmark floor, disk types, and templates for runners and for Rust builders. Shows which machine offers currently qualify at the Spot price. |
| Scaling and OOM Deep-Links | Edits the autoscaling triggers and budget caps. Lists out-of-memory (OOM) events with deep links to the exact GitHub Actions step and log line. |
| `Cloud-Init` and VM templates | Edits and validates `#cloud-config` templates and saves VM templates from running instances. |
| Regions and Latency Filter | Enables or disables regions by latency, continent, machine families, CPU platforms, and disk types. Its latency probe opens a TCP connection to a regional Google API endpoint in each catalog region. |
| Rust Builders Telemetry and IP Intel | Records and summarizes remote build runs: cache hits, peak RAM, CPU time, CPU points, and an origin IP lookup. |
| Runner Patches | Edits the patch manifests in the configuration repository. See [The patch console](#the-patch-console). |
| Admins | Owner only. Manages the added administrators. See [Manage administrators](#manage-administrators). |

!!! note "The dashboard keeps its own state"
    A new dashboard starts from built-in example data (sample instances, templates, and policies) and stores everything you change in its own state. The handlers for instance delete, region scaling, and policy edits update the dashboard's state record and make no Compute Engine call that creates, changes, or deletes a VM. Price calculations can still read Compute Engine Spot prices when credentials exist. The autoscaler and `spot_runner_manager.py` are the tools that create and delete VMs. The one dashboard feature that can change running hosts is the opt-in patch hot-patch executor.

## Run the dashboard locally

Run the server from the repository root. It binds to loopback (`127.0.0.1`) by default and needs no authentication on loopback:

```bash
python3 -B src/web_dashboard.py
```

Open `http://127.0.0.1:8787`. The server doesn't read the `.env` file, so export any environment variables in your shell.

| Option | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. |
| `--port` | `8787` | HTTP port. |
| `--project` | A project ID that the repository owner uses | Project ID for pricing lookups and the project label. Always pass your own project ID. |
| `--org` | `swack-tools` | GitHub organization. Sets the default configuration repository. |
| `--auth-token` | None | Legacy bearer token, honored only from a direct loopback client. Also read from `DASHBOARD_AUTH_TOKEN`. |
| `--tls-certfile`, `--tls-keyfile` | None | PEM certificate and key for HTTPS. Also read from `DASHBOARD_TLS_CERTFILE` and `DASHBOARD_TLS_KEYFILE`. You must pass both or neither. |
| `--allow-insecure-non-loopback` | Off | Allows plain HTTP on a non-loopback host behind a trusted TLS-terminating proxy. Also read from `DASHBOARD_ALLOW_INSECURE_NON_LOOPBACK`. |
| `--controlled-targets` | None | Comma-separated organizations and `owner/repo` targets that signed-in users must administer. Overrides `DASHBOARD_CONTROLLED_TARGETS`. |
| `--oauth-redirect-uri` | The public origin plus `/auth/github/callback`, else derived from the request `Host` header | Absolute GitHub OAuth 2.0 callback URL. Also read from `GITHUB_OAUTH_REDIRECT_URI`. |
| `--config-repo` | `<org>/spot-runners-config` | Runner configuration repository. Also read from `SPOT_CONFIG_REPO`. |
| `--public-origin` | The origin of the OAuth 2.0 redirect URI | Browser-facing origin that the server accepts on `POST` when a proxy forwards under another host. Also read from `DASHBOARD_PUBLIC_ORIGIN`. |
| `--state-file` | None (state stays in memory) | Path of the JSON state file. The server writes it atomically with mode `0600`. |
| `--control-slot` | None | Control VM slot, `a` or `b`. Turns on Cloudflare KV hydration and write-behind when the KV credentials are set, and `/api/control/*` when `DASHBOARD_CONTROL_SECRET` is set. |
| `--dump-overview` | Off | Prints the initial overview JSON to standard output and exits without serving. With `--state-file`, it still creates or rewrites that file. |

The server refuses to start in these cases:

- You bind a non-loopback `--host` with neither an auth token nor GitHub sign-in configured.
- You bind a non-loopback `--host` without TLS and without `--allow-insecure-non-loopback`.
- You pass only one of the TLS certificate and key.

To keep state between runs, add `--state-file PATH`. To inspect the data model without serving, run `python3 -B src/web_dashboard.py --dump-overview`. The overview calculates prices, and it uses your `gcloud` credentials (or `GCP_ACCESS_TOKEN`, or an API key in `GOOGLE_CLOUD_API_KEY`) for read-only Billing Catalog and Compute Engine price lookups when they exist. Without credentials, it falls back to a built-in price baseline.

## Sign in

When `GITHUB_OAUTH_CLIENT_ID` is set, the dashboard signs users in with GitHub. This section is a summary. The [sign-in reference](edge-dashboard.md#sign-in) has the full detail, including the bulk sign-out procedure.

- **Flows.** The web flow is the OAuth 2.0 authorization-code flow with Proof Key for Code Exchange (PKCE, `S256`). It needs `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET`. The device flow is off unless you set `GITHUB_OAUTH_DEVICE_FLOW=1`. The default scopes are `repo read:org`; `GITHUB_OAUTH_SCOPES` overrides them.
- **Who can sign in.** The owner is GitHub user `swackhamer`, matched by login and numeric user ID, both fixed in code. The owner can add administrators on the Admins page. Added administrators match by numeric user ID only.
- **Per-target checks.** A user must administer every controlled target: the union of `DASHBOARD_CONTROLLED_TARGETS` (or `--controlled-targets`), the targets saved in the dashboard (at most 32), and every `repos/<owner>/<repo>.yaml` file in the configuration repository. A global administrator skips these checks. `DASHBOARD_GLOBAL_ADMINS` defaults to the owner's `login:numeric_id` entry, which is also the only identity it can list. While it lists the owner, added administrators are global administrators too. Set it to `none` to require per-target administrator permission from everyone.
- **Fail closed.** If the dashboard can't list the configuration repository, sign-in answers HTTP 403. If it can't load the stored target policy, GitHub sessions and sign-ins answer HTTP 500 with `controlled_targets_unavailable`.
- **Session cookie.** The cookie `__Host-spot_session` is `HttpOnly`, `Secure`, and `SameSite=Lax`. Its payload (login, user ID, issue time, expiry, verified targets) carries an HMAC-SHA256 signature from a key derived from `DASHBOARD_SESSION_SECRET`, which must be at least 32 bytes. Sessions last 12 hours by default. `DASHBOARD_SESSION_TTL_SECONDS` accepts 60 to 604,800 seconds. The dashboard rejects a cookie that is unsigned, expired, or issued before `DASHBOARD_SESSION_NOT_BEFORE`.
- **Bulk sign-out.** Set `DASHBOARD_SESSION_NOT_BEFORE` to a Unix time, and set the matching `SESSION_NOT_BEFORE` on the Worker, to reject every older session. Rotating `DASHBOARD_SESSION_SECRET` also signs everyone out.
- **Per-request membership check.** The dashboard re-checks the list of added administrators on every request, so removing an administrator ends that user's sessions on their next request.
- **Rate limits.** Sign-in routes allow 10 start requests per client IP and 30 overall per 60 seconds. Completion routes allow 60 per IP and 240 overall. Over the limit, the dashboard answers HTTP 429 with `Retry-After`.
- **Local token.** `--auth-token` enables a bearer token (`Authorization: Bearer` or `X-Dashboard-Token`). The dashboard honors it only from a direct loopback connection that carries no `Cf-Connecting-Ip`, `Cf-Ray`, `X-Forwarded-For`, or `X-Spot-Client-Ip` header. It never works through the proxy.

`POST /api/auth/targets` updates the saved controlled targets. When the submitted list, after case-insensitive de-duplication, is identical to the saved list in the same order, it answers HTTP 200 with `"status": "unchanged"` and the current target lists, and nothing changes. Any other list doesn't commit at once. It answers HTTP 202 with `"status": "reauth_required"`, and the change commits only after a fresh GitHub sign-in confirms that the same account administers every target. See [Controlled target changes](edge-dashboard.md#controlled-target-changes).

## Manage administrators

The **Admins** tab and the `/api/admins` routes manage the dashboard administrators. Only an owner with a GitHub session can use them. Every other signed-in caller gets HTTP 403 with `"code": "owner_required"`. That includes added administrators and the local token. A signed-out caller gets HTTP 401.

| Route | Purpose |
| --- | --- |
| `GET /api/admins` | Lists the owners, the added administrators, the last 20 changes, `max_admins`, and whether added administrators skip per-target checks. |
| `POST /api/admins/add` | Adds `{"login": "<github username>", "confirm": true}`. Needs an `Idempotency-Key`. |
| `POST /api/admins/remove` | Removes `{"uid": <numeric user ID>, "confirm": true}`. Needs an `Idempotency-Key`. |

- The owners are fixed in code and can't be added or removed. A request to add an owner answers HTTP 409, and a request to remove one answers HTTP 400.
- At most 50 administrators can be added. An add over the limit answers HTTP 409.
- An add resolves the login with `GET /users/<login>` on the GitHub API, using `SPOT_CONFIG_GH_TOKEN` or `GH_TOKEN` when set. The dashboard stores the login, the numeric user ID, who added it, and when. An invalid login, or an account that isn't a user (an organization, for example), answers HTTP 400. An unknown login answers HTTP 404, and a user who is already an administrator answers HTTP 409.
- When GitHub fails, the add answers HTTP 424 with `"retry_same_key": true` and changes nothing. A lookup that returns a different account also answers HTTP 424, without `retry_same_key`.
- A remove of an ID that isn't an added administrator answers HTTP 404.
- A change without `"confirm": true` answers HTTP 400.
- Each change writes a line to the `spot_dashboard.auth` audit log and appends to a history of the last 50 changes.
- The list lives in the dashboard state, so snapshots, freezes, and imports carry it. If the dashboard can't read its state, only owners can sign in.

## The patch console

The **Runner Patches** tab edits the [runner patch](runner-patches.md) manifests in the configuration repository, previews their effect on the running hosts, and can hot-patch those hosts. It talks to the `/api/patches` routes. The [edge dashboard reference](edge-dashboard.md#runner-patches) documents the host metadata, receipts, and recovery behavior in depth. This section covers the console flow.

| Route | Purpose |
| --- | --- |
| `GET /api/patches` | Lists `defaults.yaml` and the repository manifests at the default branch head, and reports whether a hot-patch executor is available. |
| `GET /api/patches/manifest?path=...` | Returns one manifest with its `base_sha`, blob SHA, any validation errors, and the effective patch with its SHA-256. |
| `POST /api/patches/validate` | Validates `{"path", "content"}` and returns field errors such as `host.apt_packages`. |
| `POST /api/patches/preview` | Classifies each runner host of `{"repository"}` as `noop`, `hot_patch`, `recreate_containers`, `replace_vm`, or `unknown`. Optional `content` previews an unsaved draft. |
| `POST /api/patches/save` | Opens a pull request for `{"path", "content", "base_sha", "message"}`. Needs an `Idempotency-Key`. |
| `POST /api/patches/hot-patch` | Applies the saved effective patch to the recorded hosts. Needs `{"repository", "patch_sha256", "confirm": true}` and an `Idempotency-Key`. |

`path` must be `defaults.yaml` or `repos/<owner>/<repo>.yaml`. A manifest is at most 256 KiB.

### Edit, validate, and save

1. Select **Load manifests**, then pick `defaults.yaml` or a repository in the list.
2. Edit the manifest in the editor. A change marks the draft as unsaved.
3. Select **Validate** to see field errors and the effective patch. A repository manifest is an override, so checks that depend on inherited settings use the manifest merged with `defaults.yaml`. Unknown keys are errors.
4. Optionally set a pull request title, and select **Save as pull request**.

Saving creates a `spot-dashboard/...` branch from the `base_sha` that the editor loaded, commits the file, and opens a pull request. It never writes to the default branch. If the default branch moved, or GitHub reports that the file changed, the dashboard answers HTTP 409, and you reload the manifest. A save of an unchanged manifest answers HTTP 400. The token in `SPOT_CONFIG_GH_TOKEN` or `GH_TOKEN` needs contents and pull-request write access to the configuration repository.

A save of `defaults.yaml` also validates every `repos/<owner>/<repo>.yaml` file merged with the new defaults. If any would be invalid, the save opens no pull request and answers HTTP 400 with errors that name each repository. If the dashboard can't list or read those files, it answers HTTP 424 with `"retry_same_key": true`.

### Preview and hot-patch

1. Select **Preview rollout impact** on a repository manifest. The table lists each running runner host with its current patch and its classification.
2. A preview of an unsaved draft is informational. Save the draft, merge the pull request, and reload before you hot-patch.
3. The **Hot-patch running fleet** button stays off until the preview allows it, the editor has no unsaved edits, and you select the confirmation box. The preview allows a hot patch only for the saved manifest (not a draft), with an executor available, every host `hot_patch` or `noop`, and at least one host `hot_patch`.

The route applies the patch only when every host is `hot_patch` or `noop`, an executor is available, and the `patch_sha256` from the preview still matches the current effective patch. Otherwise the route answers HTTP 409: `refused` with the per-host table when a host is blocked, `stale_patch` when the patch changed, or an error when no host is recorded. A result per host reports `applied`, `noop`, `refused`, or `failed`, and the overall status is `applied`, `noop`, `partial`, or `failed`. A host that is already at the patch also has its GitHub labels reconciled, and a refused or failed reconcile makes that host `refused` or `failed`.

A local dashboard preview lists runner hosts from the dashboard state. With `DASHBOARD_FLEET_INVENTORY=gce`, which the control VM startup script always sets, it lists them from Compute Engine, which also finds hosts that predate patch recording as `unknown`. With a hot-patch executor, the preview also reads each host's receipt over `gcloud compute ssh`, which can add your SSH key to project or instance metadata, or to your OS Login profile.

### When hot-patching is unavailable

The control VM image has no `gcloud` and no SSH path to the runner hosts. By default, the hot-patch route therefore answers HTTP 501 with `"code": "hot_patch_unavailable"`, and the preview and list report the reason. The 501 status keeps this failure distinct from the control VM wait response.

To apply a patch from outside the dashboard, use an operator workstation with `src/spot_runner_manager.py --patch-live`. See [Apply a patch](runner-patches.md#apply-a-patch).

Set `DASHBOARD_HOT_PATCH_EXECUTOR=gcloud` only where `gcloud` and operator credentials exist. If `gcloud` isn't on `PATH`, the executor reports that it is unavailable. The executor takes every deployment value (zone, machine type, region, runner label, runner prefix, runner count, patch repository, registration scope, and for organizations the runner group) from the host's recorded metadata, and it refuses a host that lacks any of them. It also passes the heavy pool size when the host records one. It never falls back to the `spot_runner_manager.py` defaults, which would rewrite the host's GitHub labels.

## Changes and idempotency

Every change from the browser carries an `Idempotency-Key` header. The page generates a UUID for each `POST`, so a retry after a lost response, a timeout, or a control VM restart can't apply a change twice.

| Rule | Behavior |
| --- | --- |
| Where the key is required | `/api/patches/save`, `/api/patches/hot-patch`, `/api/admins/add`, and `/api/admins/remove` answer HTTP 400 without one. Other change routes accept it and run without replay protection when you omit it. |
| Key format | 8 to 128 characters from `A-Za-z0-9._:-`. Anything else answers HTTP 400. |
| Stored responses | For a state change, the dashboard stores the response with the change, in the same state write. For a patch save or hot patch, it stores the response right after the GitHub or host action completes; if that write fails, the response carries a `warning` and the record stays in memory. A retry with the same key replays the stored response and adds `Idempotent-Replay: true`. Records last 24 hours, up to 500 keys, and a stored body larger than 64 KiB is replaced by a short placeholder. |
| Mismatch | The same key with a different method, path, or body answers HTTP 422. |
| Still running | A retry of a patch save or hot patch that is still running answers HTTP 202 with `"status": "pending"` and `Retry-After`. The browser keeps asking with the same key until it gets the stored result. |
| Unconfirmed GitHub outcome | A save whose GitHub result is unknown answers HTTP 424 with `"retry_same_key": true`. The dashboard never stores that answer, so the retry runs again and reconciles with GitHub instead of opening a second pull request. |
| Other answers | A final answer below status 500, including a 4xx error, is stored and replayed. A response with status 500 or higher isn't stored. |

The browser retries automatically:

- On a network error, or on the control VM wait response (HTTP 503, `"error": "control_vm_unavailable"`), it shows a banner and polls `/api/health` every 5 seconds until it answers 200 (see the note on `state_conflict` under Health). Then it resends the same request with the same key.
- On HTTP 202 `pending`, it waits for `retry_after_seconds` (1 to 30 seconds) and asks again.
- On HTTP 424 with `retry_same_key`, it retries up to three times, after 2, 5, and 10 seconds.

If you select **Save** again for the same change, the panel reuses the key until a final result arrives. A changed manifest gets a new key.

### Freeze and wait responses

While the dashboard is hydrating, frozen for a control VM handover, not reconciled with Cloudflare KV, or not the active slot, it answers every `POST` with the wait response, which is HTTP 503 with `Retry-After: 10` and a body of `{"error": "control_vm_unavailable", "message": "...", "retry_after_seconds": 10}`. That includes `POST` routes that only read or compute, such as validation and previews, and sign-in and sign-out. `GET` requests work while frozen, but every request except `/api/health` and `/api/control/*` waits until the dashboard has loaded its state. A freeze waits up to 45 seconds for running saves and hot patches to finish. See [State durability](edge-dashboard.md#state-durability) and [Outages and recovery](edge-dashboard.md#outages-and-recovery).

## Other API behavior

### Health

`GET /api/health` needs no sign-in. It returns the fields `status`, `hydrated`, `frozen`, `reconciled`, `active`, `ownership`, `state_version`, `slot`, and `kv_writer`, plus `reason` when something blocks the dashboard. The HTTP status is 200 when the dashboard is hydrated, not frozen, and reconciled. Otherwise it is 503 with the wait body merged with these fields.

| `status` | Meaning |
| --- | --- |
| `ok` | The dashboard loaded its state and accepts changes. |
| `starting` | The dashboard hasn't finished hydrating or reconciling. |
| `frozen` | A control VM handover froze the dashboard. |
| `state_unavailable` | The state couldn't be read or verified, or a recovery hold exists. The response includes `reason`, and `state_backup` when a recovery hold names a backup. |
| `state_conflict` | A published snapshot didn't read back as this slot's. Changes wait until KV shows this slot's write. |

`ownership` is `active`, `inactive`, or `unknown`, and changes need `active` when KV is configured. `state_conflict` can still answer HTTP 200 when the dashboard is hydrated, reconciled, and not frozen, even though changes wait. Check `status` and `ownership`, not only the HTTP status, before you assume that changes are accepted.

### Request rules

- Every `POST` must send `Content-Type: application/json` (otherwise HTTP 415). A cross-site `POST` answers HTTP 403. When a `POST` carries an `Origin` header, it must match the request `Host` or the public origin, and `null` is refused (HTTP 403). The dashboard never trusts `X-Forwarded-Host` for this check.
- A request body is at most 262,144 bytes (HTTP 413), and 4,096 bytes on the sign-in and sign-out routes. `/api/control/import` accepts up to 25 MiB plus 64 KiB. A request without `Content-Length` counts as an empty body. The server refuses `Transfer-Encoding` and a malformed or negative `Content-Length`.
- `PUT`, `PATCH`, and `DELETE` answer HTTP 405. An unknown route answers HTTP 404.
- Every response from the dashboard's routes carries `X-Spot-Origin: dashboard`. The Worker passes a marked 5xx response through unchanged unless its body is the wait response. It treats an unmarked 502, 503, 504, or 520 to 530 response, a failed connection, a timeout, or a missing origin record as an outage.
- GitHub and Compute Engine failures in the patch console answer HTTP 424 with `"code": "upstream_error"`, never 502.
- `/api/control/*` (`snapshot`, `freeze`, `unfreeze`, `import`) is for the control VM controller. It uses a short-lived control token instead of a session, and the Worker doesn't route it.

### Routes

The Access column describes the dashboard itself. The Worker keeps its own list of routes that work without a session cookie. See the [architecture](edge-dashboard.md#architecture).

| Route | Method | Access | Purpose |
| --- | --- | --- | --- |
| `/`, `/index.html` | `GET` | Public | The page, or the sign-in page when signed out. |
| `/auth/status`, `/api/auth/status` | `GET` | Public | Sign-in state, the user, whether the user is an owner, and which flows are enabled. |
| `/auth/github/login`, `/auth/github/callback` | `GET` | Public | Web flow start and callback. |
| `/auth/device/code`, `/auth/device/token` | `POST` | Public | Device flow start and poll. |
| `/auth/logout`, `/api/auth/logout` | `POST` | Public | Clears the session cookie. |
| `/api/auth/login` | `POST` | Public | Local token sign-in from a direct loopback client. |
| `/api/health` | `GET` | Public | Health, as described in the preceding section. |
| `/api/overview`, `/api/instances` | `GET` | Signed in | Fleet, cost, and telemetry data. Query parameters: `include_deleted`, `window_hours`, `include_disk_and_tax`, `sort_by`, `sort_dir`, `search`, `role`. |
| `/api/rust-builds` | `GET`, `POST` | Signed in | Summarizes or records Rust build telemetry. |
| `/api/auth/targets` | `GET`, `POST` | Signed in; `POST` needs GitHub | Reads or proposes the saved controlled targets. |
| `/api/budget`, `/api/scaling`, `/api/regions` | `POST` | Signed in | Edits budget caps, scaling policy, and enabled regions. |
| `/api/profile/cicd_runner`, `/api/profile/rust_builder` | `POST` | Signed in | Edits the sizing profile of a role. |
| `/api/qualify` | `POST` | Signed in | Computes qualifying instances for a profile. Changes no state. |
| `/api/cloud-init/save`, `/api/cloud-init/parse` | `POST` | Signed in | Saves a template, or parses and validates cloud-init text. Parsing changes no state. |
| `/api/vm-templates/from-instance` | `POST` | Signed in | Creates a VM template from an instance record. |
| `/api/instances/delete`, `/api/instances/scale-region` | `POST` | Signed in | Marks an instance record deleted, or sets the instance count in a region. |
| `/api/ip-intel` | `POST` | Signed in | Classifies an IP address and host name. Changes no state. |
| `/api/admins` (`GET`), `/api/admins/add`, `/api/admins/remove` (`POST`) | `GET`, `POST` | Owner | The Admins page. |
| `/api/patches`, `/api/patches/manifest`, `/api/patches/validate`, `/api/patches/preview`, `/api/patches/save`, `/api/patches/hot-patch` | `GET`, `POST` | Signed in | The patch console. |
