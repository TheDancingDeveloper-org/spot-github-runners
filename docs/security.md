# Security model

This page describes the trust boundaries in this repository: who can do what, which checks enforce each
boundary, and where secrets live. It summarizes the controls that the code implements. For how to operate
each component, follow the links to the pages that own those topics.

!!! warning "Keep the boundaries intact"
    Don't weaken `host-launcher.py`, `docker-admission.py`, `host_admission.py`, `builder_access.py`, or
    the `qualification_trust` checks to work around a failure. When one of these checks can't confirm an
    identity, a limit, or an owner, it refuses the action and keeps the evidence. Two paths sit outside the
    checks: the Docker admission wrapper passes a `docker run` without the remote-build label straight to
    Docker, which is safe only because the uploader can't reach Docker, and runner hosts without a tier get no
    memory admission at all.

## Trust boundaries

| Component | Where it runs | What it can do | What it can't do |
| --- | --- | --- | --- |
| Operator workstation | Your machine, with `gcloud`, `gh`, and the repository checkout | Provision runner hosts and dedicated builders, run the fleet controller, hot-patch runner hosts with `--patch-live`, and run builder runtime updates through an administrative connection | Nothing on this page grants it more than the cloud and GitHub permissions of the identities you sign in with. |
| Fleet controller | `src/spot_runner_autoscaler.py`, usually as a macOS LaunchAgent on the operator workstation | Create, drain, rotate, and delete runner hosts; cordon a host over an identity-checked SSH command; rerun jobs that lost their runner; manage the control VM with `--manage-control-vm` | List, rotate, or delete control VMs (`spot-control-*`) as runner hosts. Its runner inventory, price rotation, and scaling skip them. |
| Runner host | A Spot virtual machine (VM) named `oxidex-runners-*` | Run the root-owned admission service and the runner containers | Mount the host Docker socket into a runner container. `cloud-init/startup.sh` never does, the runner-patch validator refuses it as a patch mount, and the manager's `--apply` check fails a deployment when a container binds `/var/run/docker.sock:/var/run/docker.sock`. |
| Runner container | One per registered runner, as the image's `runner` user | Run workflow code, which you must treat as partly untrusted | Set the budget of its own lease by supplying one, release another container's lease, or remove the host cordon. Its cordon and admission directories are read-only mounts. See [Owner checks](#owner-checks) for the limits of the admission identity check. |
| Dedicated builder | A Spot VM named `builder-*`, shape `n4-highcpu-16` | Run one admitted build container for the approved uploader | Register GitHub runners, use a VM service account (it launches with `--no-service-account --no-scopes`), or accept OS Login and project-wide SSH keys (`enable-oslogin=FALSE`, `block-project-ssh-keys=TRUE`). |
| Control VM | `spot-control-a` or `spot-control-b` | Serve the dashboard on `127.0.0.1:8787` and run a `cloudflared` tunnel | Run `gcloud` or SSH to runner hosts from the dashboard. The dashboard image contains neither. What the VM's service account can do in the project depends on its IAM roles, so give it a dedicated account with only the access that it needs. |
| Cloudflare Worker | `cloudflare/worker.js` at `https://spot.swacktech.com` | Authenticate the session cookie, cap request bodies, and proxy to the active control VM | Decide who may sign in, or forward `/api/control/*`. It answers that path with HTTP 404, even when the path is encoded or differently cased. |
| Signed-in GitHub users | A browser | Sign in when they're the owner or an administrator that the owner added, and when they pass the target checks | Manage administrators unless they're the owner. |

Runner hosts and dedicated builders have separate lifecycles and separate security controls. A runner host
registers GitHub Actions listeners; a dedicated builder registers none and accepts work only from its
approved uploader. See [Fleet and autoscaler](fleet-and-autoscaler.md) and
[Set up a builder](dedicated-builders.md).

## Keep untrusted pull requests off persistent runners

Runner hosts are persistent: workspaces, Cargo caches, Docker images, and runner state survive from one job
to the next on the same data disk. A workflow from an untrusted fork can leave files, processes, or cache
entries behind for the next job. Keep untrusted external pull requests off persistent self-hosted runners,
and route them to GitHub-hosted runners instead.

The runner labels don't isolate a host by themselves. Every repository-target runner keeps GitHub's
read-only `self-hosted`, operating system, and architecture labels, so a workflow that selects only those labels can still
reach it. Restrict which repositories can use the runner group, and review the workflow triggers in each
repository that can.

## Job admission on runner hosts

Registered listener counts don't equal memory capacity. On hosts that the controller manages in tiered mode,
the root-owned admission service in `src/lib/host_admission.py` reserves memory before a job starts. The
runner image's job-started hook (`runner/job-started.sh`) calls it when `GHA_ADMISSION_REQUIRED=1`.

### Memory leases

- The service listens on the Unix socket `/run/gha-admission/socket`, owned by `root` with group 1001 and
  mode `0660`. Runner containers mount `/run/gha-admission` read-only.
- It identifies each caller from the kernel's peer credentials (`SO_PEERCRED`), never from the request. A
  client can't supply its own budget or release another container's lease.
- It records leases in `/var/lib/gha-admission/leases.json`, in a directory with mode `0700`. It reclaims a
  lease only after it independently confirms that the owner is dead, never while the owner is alive.
- The grant limit is the smaller of the configured work budget and the observed memory envelope of the host.
- A job that GitHub already assigned keeps asking every 5 seconds while the service refuses it or the
  connection drops, so a busy host delays a job instead of failing it. An admission error, such as an
  unauthorized peer, fails the job.

### Owner checks

Before it grants a runner lease, the service checks the actual owner:

- A runner request must come from UID 1001 inside a Docker cgroup, and the container's name must be one of
  the runner names in `/etc/gha-admission.json`. A peer with a UID other than 0 or 1001 gets
  `Unauthorized admission peer`.
- The lease identity binds the container ID, the container start time, and the runner `Runner.Worker`
  process. If the Worker exits before the grant, the service refuses it.
- On a host with a heavy-runner policy, the service also compares the container's actual memory, swap, and
  CPU caps with the reservation, and refuses the grant when they differ.
- A peer with UID 0 is treated as a builder owner, and the service doesn't check a container for it. The
  runner image starts jobs as the `runner` user (UID 1001), but this repository doesn't remove `sudo` from the
  container or set `--cap-drop` or `no-new-privileges` on runner containers. Whether workflow code can become
  root inside a container depends on the base image. Treat any workflow code that runs on a runner host as able
  to talk to the admission service.

### Host cordon

To retire a repository-target host, the controller places the root-owned marker
`/run/gha-runner-cordon/draining`. Every runner container mounts `/run/gha-runner-cordon` read-only, so
workflow code can't remove the marker. While it exists, the job-started hook refuses each new job before
admission and before any job step runs.

The controller places the marker with a root command that first checks the provider instance identity and
holds the lock `/run/gha-runner-cordon.lock`. A host whose job-started hook lacks the cordon capability
can't refuse jobs, so the controller holds its retirement instead of removing its runners. See
[Drain and the host job-start cordon](fleet-and-autoscaler.md#drain-and-the-host-job-start-cordon).

## Dedicated builder access

A dedicated builder accepts builds from one approved uploader account. Three root-owned components enforce
that boundary.

### Root-owned launcher

The uploader runs `/usr/local/bin/oxidex-remote-build`, installed from `builder/dedicated/host-launcher.py`.
Startup writes `/etc/sudoers.d/oxidex-build`, which lets the `oxidex-build` group run only that launcher as
root. The launcher:

- Refuses to run unless it runs as root.
- Accepts only a simple project identifier, never a path, and refuses symlinked builder paths.
- Accepts only `cargo`, `rustc`, `rustfmt`, `clippy-driver`, `just`, `uv`, `python3`, and `cbindgen` as the
  build command.
- Requires that the builder image tag still resolves to the immutable image ID that startup approved.
- Exits with status 75 while the drain marker `/run/oxidex-build-draining` exists, and before the
  bootstrap completion marker exists, except for the bootstrap tool probe and the `prepare` and `cleanup`
  actions. `cleanup` also runs during a drain.

### Build container policy

The launcher starts every build container with fixed options. The CPU and memory values come from the root-owned configuration file `/etc/oxidex-remote-build.json`, and the Docker admission wrapper checks that they match the reservation:

```text
docker run --rm --user=1001:1001 --cpus=16 --memory=28g --memory-swap=28g
  --pids-limit=4096 --cap-drop=ALL --security-opt=no-new-privileges ...
```

The build runs as the unprivileged UID and group ID (GID) 1001, with every Linux capability dropped and
privilege escalation blocked. Memory and swap use the same limit, so the container gets no extra swap. An
ordinary project mounts only its source, its target directory, and the shared Cargo cache. A `qualification-*`
project also mounts the sealed checkout and the other paths that the qualification trust code pins. The
container never receives the Docker socket.

### Docker admission wrapper

On the builder, `/usr/local/bin/docker` is `builder/dedicated/docker-admission.py`. For a remote build, it
parses the `docker run` arguments and refuses any option outside a fixed list, any repeated policy option,
an image other than the reviewed builder image, and resource caps that differ from the reservation. It then
acquires an admission lease before the container starts. Every other Docker command runs the real Docker
binary unchanged, so the wrapper never relaxes a limit. The uploader isn't in the `docker` group and can't
call Docker directly.

### Uploader identity mapping

The renderer and startup accept only an explicit, root-approved mapping with the schema
`root-approved-ssh-mapping-v1`. `src/lib/builder_access.py` never infers the uploader from `getent`, the
client environment, or a key comment. It enforces these rules:

- The mapping has exactly the fields `schema`, `principal`, `username`, `uid`, `identity_receipt_sha256`,
  `public_key`, and `public_key_sha256`, and its canonical SHA-256 matches the value bound into startup.
- The key is one Ed25519 public key whose SHA-256 matches the mapping. No private key is ever read.
- The UID is at least 1000 and isn't 1001, which the build container reserves.
- Only the approved uploader may belong to `oxidex-build` (GID 1001). The uploader must not be in the
  `docker`, `sudo`, `wheel`, or `admin` groups.
- The effective sudo policy of the uploader, including any per-user or `google-sudoers` grant, must allow
  only the launcher. Any other sudo rule stops enrollment.
- Startup never overwrites an existing `authorized_keys` file that differs from the mapping.

See [Set up a builder](dedicated-builders.md) for how to obtain and render the mapping.

### Use direct authenticated SSH for the uploader

Connect as the uploader with direct SSH and SCP. Use the enrolled key, and check the host key that you
established. Don't use `gcloud compute ssh` or `gcloud compute scp` for the uploader. Those commands manage
their own keys and can enroll the account into broader guest permissions, such as the `google-sudoers`
group, which defeats the single-command sudo policy that startup verifies. The renderer's launch command
also disables OS Login and blocks project-wide SSH keys, so the enrolled key is the intended way in.

### Don't broaden permissions

When a transfer or build fails with a permission error, fix the identity, not the permissions. Don't add the
uploader to another group, make directories group- or world-writable, or pass the Docker socket to a
container to get past an uploader or control account mismatch. The launcher sets the ownership and modes of
the shared directories and refuses symbolic links, the wrapper pins the container mounts and limits, and
admission authenticates the calling container, so one account can't read or alter another account's work.

## Qualification trust

Projects named `qualification-*` go through `src/lib/qualification_trust/`, which startup installs under
`/opt/oxidex-builder/qualification_trust`:

- `qualification_source.py` admits an exact source commit only with a maintainer SSH commit signature or a
  detached attestation that a maintainer controller issued after its own GitHub and PGP checks. Spot
  verifies the maintainer SSH signature; it doesn't repeat the controller's checks.
- `host_gate.py` admits qualification sources as root and never runs uploaded code.
- `import_supervisor.py` keeps the admission locks until every imported process is reaped. The unprivileged
  importer never inherits the lock descriptors.
- `mounts.py` pins one mount identity for admission, inspection, cancellation, and cleanup.
- `runtime_git.py` allows one HTTPS exception, the pinned oracle clone, and keeps every other Git transport
  denied.

These checks authenticate sources and process lifetimes. They don't certify an installed deployment. See
[Remote builds and clients](remote-builds.md).

## Dashboard placement

The dashboard doesn't run on runner hosts or builders. Runner containers run workflow code, so a dashboard
there would expose its session key and GitHub tokens to that code. The dashboard runs on a separate control
VM instead. The dashboard image can't manage runner hosts, but that isn't a network boundary:

- The dashboard image has no `gcloud` and no SSH credentials, so the shipped dashboard can't manage hosts. That
  doesn't stop network traffic. Runner hosts use the project's `default` network, the control VM doesn't pick an
  isolated network, and this repository manages no firewall rules, so whether the control VM can reach runner
  hosts depends on your VPC and firewall. Add the isolation yourself, for example a firewall rule that denies
  ingress to runner hosts from the control VM's service account or network tag.
- The VM's service account and network still decide what the VM could reach in the project, so give the VM a dedicated service account that has only Secret Manager access, `compute.instances.list`, and `compute.machineTypes.get`, not a broad default account. Without `compute.machineTypes.get`, a host's vCPU and RAM stay unknown, and the patch preview classifies patches with hardware minimums as `unknown`. Runner hosts can reach the metadata server too.
- The dashboard's hot-patch action therefore answers HTTP 501 with `"code": "hot_patch_unavailable"` on the
  control VM. Hot-patch runner hosts from an operator workstation with the full `--patch-live` command,
  which needs every deployment value the host was provisioned with. See
  [Hot-patch a running host](runner-patches.md#hot-patch-a-running-host).
  That command changes the running host immediately and has no preview, so use the dashboard preview first.
- The dashboard container runs as UID 10001 with `--cap-drop ALL` and `--security-opt no-new-privileges`.
  The `cloudflared` container drops every capability too.

See [Edge hosting and the control VM](edge-dashboard.md) and [Dashboard](dashboard.md).

## Dashboard authentication and authorization

The Worker and the dashboard split the work:

- **Sign-in:** the dashboard uses a GitHub OAuth 2.0 app with the authorization-code flow and Proof Key for
  Code Exchange (PKCE) with `S256`.
- **Session cookie:** `__Host-spot_session` is signed with HMAC-SHA256 (hash-based message authentication
  code) under `DASHBOARD_SESSION_SECRET`, and set with `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`.
  The `__Host-` prefix makes browsers reject the cookie unless it's `Secure`, has `Path=/`, and has no
  `Domain`, so another subdomain can't set or shadow it. Sessions last 12 hours by default.
- **OAuth 2.0 scopes:** the dashboard requests `repo read:org` by default (`GITHUB_OAUTH_SCOPES` overrides it). It
  offers GitHub Device Flow sign-in only when `GITHUB_OAUTH_DEVICE_FLOW=1`.
- **Session lifetime:** sessions are stateless. A lost GitHub administrator permission takes effect at the next
  sign-in, so a session can outlive it for up to its lifetime (12 hours by default, 7 days at most). Removing an
  added administrator ends their sessions at the next request. Only a bulk sign-out cut-off or a rotated session
  secret ends every session.
- **Worker:** the Worker only authenticates the cookie. It checks the signature, the expiry, the bulk
  sign-out cut-off, and the identity fields.
- **Dashboard:** the dashboard authorizes every request from its durable state. Only the owner, GitHub user
  `swackhamer`, matched by login and numeric ID, and the administrators that the owner added may sign in. Added
  administrators are matched by numeric user ID. By default (`DASHBOARD_GLOBAL_ADMINS` unset), the per-target checks
  don't apply to the owner or to added administrators. Set `DASHBOARD_GLOBAL_ADMINS=none` to require every user to
  administer every controlled target. If the dashboard can't list the config repository, sign-in fails
  closed with HTTP 403.
- **Admins page:** only the owner can open it. `/api/admins` answers every other signed-in user with HTTP 403 and
  `"code": "owner_required"`.
- **Same-origin POST:** the dashboard accepts a `POST` only with `Content-Type: application/json`, and it refuses a
  `POST` whose `Origin` header is present and doesn't match its own `Host` or its configured public origin. It refuses `Sec-Fetch-Site:
  cross-site` and a `null` origin, and never trusts `X-Forwarded-Host` for this check.
- **Loopback binding:** the dashboard binds to `127.0.0.1` by default. It refuses a non-loopback address
  without authentication, and without TLS unless you pass `--allow-insecure-non-loopback`. The legacy
  dashboard token works only for a direct loopback client with no proxy headers.
- **Rate limits:** sign-in routes are rate limited per client IP address and globally, and answer HTTP 429
  with `Retry-After`. The dashboard trusts the `X-Spot-Client-Ip` header only from a loopback peer, the
  local `cloudflared`, and the Worker drops any copy that the browser sends.
- **Body caps:** the Worker caps sign-in bodies at 16 KiB and other bodies at 256 KiB before it forwards
  anything. The dashboard enforces its own 256 KiB limit for JSON bodies.

[Edge hosting and the control VM](edge-dashboard.md#sign-in) describes the sign-in rules, bulk sign-out,
and the administrator list in full.

## Control-plane signing

The fleet controller freezes, unfreezes, snapshots, and imports dashboard state through
`/api/control/<op>` on one control VM slot. These calls don't use GitHub sign-in. Each call carries a bearer token
that `src/lib/edge_state.py` signs with a key derived from `DASHBOARD_CONTROL_SECRET`:

- The token names one operation (`freeze`, `unfreeze`, `snapshot`, or `import`) and one slot, and expires
  after 120 seconds when the controller mints it. The dashboard accepts a token with a lifetime of up to 600
  seconds, and the token isn't single-use within its lifetime.
- The dashboard accepts it only for that operation on its own slot, and refuses every control call when no
  control secret is set.
- The Worker never forwards `/api/control/*` from the public hostname.

## Protect the tunnel hostnames

Each control VM slot has its own Cloudflare Tunnel hostname under the domain in
`SPOT_CONTROL_ORIGIN_DOMAIN`. A Cloudflare Access service token protects those hostnames. The Worker adds
the `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers from its secrets and drops any `CF-Access-*`,
`X-Spot-*`, or `X-Forwarded-*` header that the browser sends. The fleet controller sends the same service
token, from `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`, with its control calls. The dashboard binds to
`127.0.0.1`, so its port isn't reachable from the network. The control VM gets an external IPv4 address by
default (`SPOT_CONTROL_EXTERNAL_IP`), and the repository doesn't manage firewall rules for it.

## Secrets

### What counts as a secret

| Secret | Used by | Where it lives |
| --- | --- | --- |
| `GH_TOKEN` and `SPOT_CONFIG_GH_TOKEN` | Manager, controller, and dashboard | `.env` on the operator workstation. The control VM reads a GitHub token from Secret Manager. |
| `GITHUB_OAUTH_CLIENT_SECRET` and the OAuth 2.0 client ID | Dashboard | Secret Manager for the control VM. |
| `DASHBOARD_SESSION_SECRET` | Dashboard and Worker (as `SESSION_SECRET`) | Secret Manager for the control VM, and a Worker secret. |
| `DASHBOARD_CONTROL_SECRET` | Fleet controller and dashboard | `.env` for the controller, and Secret Manager for the control VM. |
| `CLOUDFLARE_KV_API_TOKEN` | Fleet controller and dashboard | `.env` for the controller, and Secret Manager for the control VM. |
| `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` | Fleet controller and Worker | `.env` for the controller, and Worker secrets. |
| Tunnel tokens, one for each slot | `cloudflared` on the control VM | Secret Manager. The startup script writes the slot's token to a root-owned `0600` file. |
| Runner registration token | One runner host deployment | Generated per deployment. See [Runner registration tokens](#runner-registration-tokens). |

Handle them this way:

- Never commit a secret to Git. The `.env` file at the repository root is ignored by Git. Create it with mode
  `0600`. See [Configuration](configuration.md#how-the-environment-file-is-loaded).
- Keep SSH private keys, approved uploader JSON files, rendered launch directories, and operational state
  outside the Git checkout, for example under `$HOME/oxidex-ops`.
- On the control VM, the startup script reads each secret from Secret Manager through the VM service account
  at every boot. It writes them only to root-owned `0600` files under `/etc/spot-dashboard`, never echoes a
  secret, and never enables shell tracing. It passes the GitHub token to `docker login` on standard input
  and to `git` through an environment-supplied header. The token never appears in a URL or an argument.
- Instance metadata holds only values that aren't secret. Examples are the controlled targets and the bulk
  sign-out cut-off.

### Token scopes

- `GH_TOKEN` needs organization runner-management access. Job recovery also needs Actions read and write
  access and Checks read access to each tracked repository.
- The dashboard's GitHub token needs contents and pull-request write access to the config repository to
  save runner patches.
- `CLOUDFLARE_KV_API_TOKEN` needs only Workers KV Storage edit access on the account.
- The control VM uses one GitHub token from Secret Manager for several jobs. The dashboard uses it as `GH_TOKEN`
  and `SPOT_CONFIG_GH_TOKEN`, a Spot VM that builds the image from source uses it to fetch the source, and the startup script signs in to `ghcr.io` with it before it pulls the
  private image `ghcr.io/swack-tools/spot-dashboard`. Pulling a private package needs the `read:packages`
  scope. The standard e2-micro control VM pulls the image whenever the pinned image isn't already on the VM, so a
first start or a new image can't succeed without that scope. See
  [Operations and troubleshooting](operations.md#troubleshooting).

### Runner registration tokens

The manager requests a short-lived runner registration token from GitHub for each deployment and embeds it
in the startup script. After the deployment finishes or fails, the manager removes the `startup-script`
metadata, so the token doesn't stay in instance metadata and a reboot doesn't replay the bootstrap. The same
token configures every runner on the host and stays valid until GitHub expires it, so removing it from
metadata limits exposure but doesn't revoke it. Don't use a registration token as `GH_TOKEN`.

## What the security model doesn't promise

- Source tests don't certify a deployment. Local unit tests, a remote proof run with `just test`, GitHub CI
  checks, rendered startup payloads, and live VM observations prove different things.
- A successful bootstrap tool probe isn't workload qualification. The builder readiness record sets
  `workload_qualified` to `false`.
- A runner label doesn't enforce limits. Verify admission policy and actual container limits before you
  publish a label such as `spot-heavy32`.
- Cloudflare Workers KV is eventually consistent. The dashboard's active-slot checks narrow the race between
  two writers, but the real exclusivity guarantee is the controller's fence. See
  [Edge hosting and the control VM](edge-dashboard.md#snapshot-and-active-slot-checks).
