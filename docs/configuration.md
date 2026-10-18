# Configuration reference

This page lists every environment variable that the repository reads, grouped by what it configures. Each
entry gives the default from the code, the meaning, and the entry point that reads it. A short table at the
end covers configuration that doesn't use environment variables.

For the sample values that you copy into a file, see
[`.env.example`](https://github.com/swack-tools/spot-github-runners/blob/main/.env.example). When a sample
value differs from the code default, this page states both and which one wins.

## How the environment file is loaded

Three entry points call `load_env` in `src/lib/environment.py` on `<repository root>/.env` before they read
any variable:

- `src/spot_runner_manager.py`
- `src/spot_runner_autoscaler.py`, including the macOS LaunchAgent that it installs, because the code finds the file from the location of its
  own source tree
- `src/control_vm.py`, except for the `render-startup` command, which exits before it loads the file

No other entry point reads `.env`. For example, `src/spot_finder.py` and `src/runner_fleet.py` read only the
process environment, so export the variables that they use. The dashboard also reads only the process
environment; on the control VM, Docker supplies it with `--env-file`.

The parser is deliberately small:

- It reads the file line by line and strips each line. It skips blank lines, lines that start with `#`, and
  lines without an `=`.
- It splits each remaining line at the first `=`. It strips whitespace from the name and the value, and then
  strips every leading and trailing single or double quote character from the value.
- It doesn't support an `export` prefix, escape sequences, variable expansion, or trailing comments. A
  trailing `# comment` becomes part of the value.
- It never overrides the process environment. It uses `os.environ.setdefault`, so a variable that the
  process already defines wins, even when its value is empty.

The file is ignored by Git. Create it with owner-only permissions:

```bash
cp .env.example .env
chmod 600 .env
```

Two rules apply to the values:

- An empty value, such as `GITHUB_OAUTH_CLIENT_ID=`, counts as set in the file but as unset for the settings
  on this page, because the code treats an empty string like a missing value. The one exception is a process
  variable that is already set to an empty string, which blocks the same name in `.env`.
- Several commands default `--project` to a project ID that the repository owner uses. Always pass
  `--project` explicitly.

## Secrets

The tables mark a variable as **Secret** when it holds a credential or a signing key. Keep secret values in
`.env` (mode `0600`) or in your secret manager, never in a Git checkout that you commit, a shell history, or
instance metadata. The one exception is the one-use runner registration token, which the manager places in the
startup script metadata for one deployment and then removes. This page never prints example secret values. On the control VM, the startup script reads
the secrets from Secret Manager at each boot and writes them to a root-owned environment file. See
[Edge hosting and the control VM](edge-dashboard.md).

## Credentials and sign-in

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `GH_TOKEN` | None. | The `gh` command-line tool, which the manager, autoscaler, and fleet tools call. The dashboard and `runner_patch.py` also use it as a fallback. | **Secret.** A renewable GitHub credential with organization runner-management access. The autoscaler's `--github-user ACCOUNT` option overwrites it in the process with `gh auth token --user ACCOUNT`. Don't use a short-lived runner registration token here. |
| `SPOT_CONFIG_GH_TOKEN` | Empty, which falls back to `GH_TOKEN`. | The dashboard (config repository listing and the Runner Patches tab) and `runner_patch.py` (manager and autoscaler). | **Secret.** An optional token for the central config repository. When set, it replaces `GH_TOKEN` for those calls. Read access is enough for listing and previews. Saving a patch from the Runner Patches tab creates a branch, writes contents, and opens a pull request, so it then needs contents and pull-request write access. |
| `SPOT_CONFIG_REPO` | `<org>/spot-runners-config`, where `<org>` is the `--org` value (default `swack-tools`). | The dashboard (the `--config-repo` flag wins) and `control_vm.py`. | The central runner configuration repository, as `owner/repo`. The dashboard treats every `repos/<owner>/<repo>.yaml` file in it as a controlled target. `control_vm.py` passes it to each new control VM as the `dashboard-config-repo` metadata value. The manager doesn't read this variable, and the autoscaler reads it only with `--manage-control-vm` and no `--config-repo`. Otherwise they use `--config-repo`, which defaults to `<org>/spot-runners-config`. The autoscaler passes its `--config-repo` value on to the control VM. |
| `GITHUB_OAUTH_CLIENT_ID` | Empty, which turns GitHub sign-in off. | The dashboard (`src/web_dashboard.py`). | The client ID of the GitHub OAuth 2.0 App. It must be 1 to 128 characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, and `-`. Setting it turns on sign-in and makes `DASHBOARD_SESSION_SECRET` mandatory. |
| `GITHUB_OAUTH_CLIENT_SECRET` | Empty. | The dashboard. | **Secret.** The OAuth 2.0 App client secret. Setting it without a client ID is an error. Without it, the browser authorization-code flow is off and only the Device Flow can sign users in. |
| `GITHUB_OAUTH_DEVICE_FLOW` | Unset, which turns the Device Flow off. | The dashboard. | Set to exactly `1` to also offer GitHub Device Flow sign-in. The OAuth 2.0 App must have Device Flow enabled. |
| `GITHUB_OAUTH_SCOPES` | `repo read:org`. | The dashboard. | OAuth 2.0 scopes to request. Separate them with a space or a comma. |
| `GITHUB_OAUTH_REDIRECT_URI` | Derived. See the meaning. | The dashboard (the `--oauth-redirect-uri` flag wins). | The absolute `http` or `https` callback URL, without a fragment. When unset, the dashboard uses the public origin plus `/auth/github/callback` if one is known, and otherwise builds the URL from the request `Host` header. Set it for any internet-facing deployment, for example `https://spot.swacktech.com/auth/github/callback`. On a control VM, the startup script sets it from the public URL. |

## Dashboard session and sign-in policy

For the behavior behind these settings, see [Sign-in](edge-dashboard.md#sign-in).

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `DASHBOARD_SESSION_SECRET` | None. | The dashboard. The Worker reads the same value as `SESSION_SECRET`. | **Secret.** Required when `GITHUB_OAUTH_CLIENT_ID` is set. The code has no generated or built-in fallback. It must hold at least 32 bytes and at least 8 distinct byte values. Use the same value on every dashboard VM and edge Worker so sessions survive VM rotation. Generate one with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`. |
| `DASHBOARD_SESSION_TTL_SECONDS` | `43200` (12 hours). | The dashboard. | The session lifetime in seconds. The accepted range is 60 to 604800. Any other value stops the dashboard at startup. |
| `DASHBOARD_SESSION_NOT_BEFORE` | `0`, which means no cut-off. | The dashboard and `control_vm.py`. The Worker reads the same value as `SESSION_NOT_BEFORE`. | A Unix time in seconds. The dashboard rejects every session issued before it. It must be a non-negative integer and can't be more than 60 seconds in the future, because a future cut-off would also reject new sessions. `control_vm.py` passes it to new control VMs as the `dashboard-session-not-before` metadata value. |
| `DASHBOARD_GLOBAL_ADMINS` | Empty, which means the owner's entry (`swackhamer:<owner-id>`). | The dashboard and `control_vm.py`. | Global administrators skip the per-target administrator check. Entries are `login:numeric_id`, with commas or whitespace between them, and the only entry that the code accepts is the owner. `none` turns global administrators off, so everyone, including administrators that the owner added, must administer every controlled target. `control_vm.py` passes it as the `dashboard-global-admins` metadata value, with entries separated by semicolons. |
| `DASHBOARD_CONTROLLED_TARGETS` | Empty, which means none. | The dashboard (the `--controlled-targets` flag wins) and `control_vm.py`. | Organizations and `owner/repo` targets that signed-in users must administer. Separate entries with a comma or whitespace. The dashboard adds targets from the state file and from the config repository. `control_vm.py` passes the list as the `dashboard-controlled-targets` metadata value, with entries separated by semicolons, and refuses to create a control VM when an entry is malformed. |
| `DASHBOARD_PUBLIC_ORIGIN` | The origin of `GITHUB_OAUTH_REDIRECT_URI`. | The dashboard (the `--public-origin` flag wins). | The browser-facing origin, such as `https://spot.swacktech.com`, that the dashboard accepts on same-origin `POST` requests when a proxy forwards them under a different `Host`. On a control VM, the startup script derives it from the public URL. |

## Dashboard runtime and serving

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `DASHBOARD_AUTH_TOKEN` | Empty. | The dashboard (the `--auth-token` flag wins). | **Secret.** A legacy bearer token. The dashboard honors it only for a direct loopback client, never through a proxy. Binding to a non-loopback address requires either this token or configured GitHub OAuth 2.0. |
| `DASHBOARD_TLS_CERTFILE`, `DASHBOARD_TLS_KEYFILE` | Empty. | The dashboard (the `--tls-certfile` and `--tls-keyfile` flags win). | Paths to a PEM certificate and its private key. Set both or neither. Binding to a non-loopback address requires TLS unless you allow insecure serving. |
| `DASHBOARD_ALLOW_INSECURE_NON_LOOPBACK` | Empty, which means off. | The dashboard (the `--allow-insecure-non-loopback` flag wins). | One of `1`, `true`, `yes`, or `on` (case-insensitive) lets the dashboard serve plain HTTP on a non-loopback address. Use it only behind a trusted proxy that terminates TLS. |
| `DASHBOARD_HOT_PATCH_EXECUTOR` | Empty, which means unavailable. | The dashboard. | Set to `gcloud` to let the Runner Patches tab apply a live hot patch. Set it only where `gcloud` and operator credentials exist. Leave it empty on the control VM, where the action answers HTTP 501. If you set `gcloud` but `gcloud` isn't installed, the action stays unavailable. |
| `DASHBOARD_FLEET_INVENTORY` | Empty. | The dashboard. | Set to `gce` (case-insensitive) to list runner hosts and their recorded `spot-runner-patch` metadata from Compute Engine for the Runner Patches preview. The control VM startup script sets it. The service account needs `compute.instances.list` and `compute.machineTypes.get`. Without the second permission, host capacity stays unknown. |
| `SPOT_GCP_TOKEN_SOURCE` | Empty, which means use `gcloud`. | The dashboard and any code that calls `compute_api.get_auth`. | Set to `metadata` (case-insensitive) to take the access token and project from the GCE metadata server instead of `gcloud`. The control VM startup script sets it. If `gcloud` isn't on `PATH`, the code also uses the metadata server. |

## Edge and control VM settings

These variables are read by `src/control_vm.py` and by `src/spot_runner_autoscaler.py` with
`--manage-control-vm`. The dashboard reads the first four (the Workers KV settings and the control secret)
when you start it with `--control-slot`. See [Edge hosting and the control VM](edge-dashboard.md).

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | None. | `control_vm.py`, the autoscaler, and the dashboard. | The Cloudflare account that owns the Workers KV namespace. Must be alphanumeric. The deploying shell also uses it for `wrangler`. The dashboard uses KV only when all three KV variables are set and `--control-slot` is passed. |
| `CLOUDFLARE_KV_NAMESPACE_ID` | None. | `control_vm.py`, the autoscaler, and the dashboard. | The Workers KV namespace that holds `origin:active` and the `dashboard:state` snapshot. Must be alphanumeric. |
| `CLOUDFLARE_KV_API_TOKEN` | None. | `control_vm.py`, the autoscaler, and the dashboard. | **Secret.** A Cloudflare API token limited to Workers KV Storage edit on that account. |
| `DASHBOARD_CONTROL_SECRET` | None. | `control_vm.py`, the autoscaler, and the dashboard. | **Secret.** A random secret, with at least 32 bytes recommended, that the autoscaler and every control VM share. It signs the freeze, snapshot, and import calls during make-before-break rotation. Without it, the control VM manager reports a runtime problem and pauses lifecycle changes, and the dashboard serves no `/api/control/*` routes. |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | None. | `control_vm.py` and the autoscaler. The Worker reads the same names as secrets. | **Secret.** The Cloudflare Access service token that protects the per-slot tunnel hostnames. The controller needs it for health probes. |
| `SPOT_CONTROL_STATE_DIR` | The directory that holds the autoscaler's default state file. | `control_vm.py` (the `--control-state-dir` flag wins) and the autoscaler. | The directory of the project-wide control VM lock and state, `control-vm-<project>.lock` and `control-vm-<project>.json`. Every `--manage-control-vm` controller in a project must share it. |

## Provisioning settings

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `SPOT_CONTROL_PROVISIONING` | `spot`. The sample in `.env.example` is the same. | `control_vm.py` (the `--provisioning` flag wins) and the autoscaler. | `spot` creates the cheapest Spot shape under `SPOT_CONTROL_MAX_DAILY_USD` and rotates for price. `standard` runs the always-on free-tier `e2-micro`. The value is case-insensitive. Any other value is a runtime problem that pauses every lifecycle change. Every `--manage-control-vm` controller in the project must use the same value. See [Use the free-tier control VM](edge-dashboard.md#use-the-free-tier-control-vm). |
| `SPOT_CONTROL_REGIONS` | `us-central1,us-east1,us-west1` for `spot`, and `us-central1` for `standard`. The sample in `.env.example` lists all three. | `control_vm.py` (the `--regions` flag wins) and the autoscaler. | Allowed control VM regions, separated by commas. With `standard`, every entry must be one of the three free-tier regions. |
| `SPOT_CONTROL_MAX_DAILY_USD` | `0.50`. The sample in `.env.example` is the same. | `control_vm.py` (the `--max-daily-usd` flag wins) and the autoscaler. | The daily compute plus boot-disk cap for each control VM, in dollars. |
| `SPOT_CONTROL_EXTERNAL_IP` | `1`. The sample in `.env.example` is the same. | `control_vm.py` (the `--no-external-ip` flag wins) and the autoscaler. | `0`, `false`, `no`, or `off` (case-insensitive) creates the VM without an external address, which needs a NAT gateway for egress. Any other value keeps the address. |
| `SPOT_CONTROL_SERVICE_ACCOUNT` | Empty, which means the Compute Engine default service account. | `control_vm.py` (the `--service-account` flag wins) and the autoscaler. | The service account email for the control VM. |
| `SPOT_CONTROL_ORIGIN_DOMAIN` | A domain that the repository owner controls. Set your own. The sample in `.env.example` is the same value. | `control_vm.py` (the `--origin-domain` flag wins) and the autoscaler. | The domain of the per-slot tunnel hostnames. It must match the Worker's `ORIGIN_HOST_SUFFIX`. |

## Dashboard image

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `SPOT_DASHBOARD_IMAGE` | Empty. | `control_vm.py` (the `--dashboard-image` flag wins) and the autoscaler. | A digest-pinned dashboard image, `name@sha256:<64 hex>`. The Dashboard image workflow prints it. The free-tier `standard` VM also accepts a 40-hex commit tag, `name:<sha>`. |
| `SPOT_DASHBOARD_SOURCE_REF` | Empty. | `control_vm.py` (the `--dashboard-source-ref` flag wins) and the autoscaler. | A 40-hex commit. A Spot VM builds the image from it when no image is set. The `standard` VM pulls the published tag of that commit instead, and only for the default repository. |
| `SPOT_DASHBOARD_REPO` | `swack-tools/spot-github-runners`. The sample in `.env.example` is the same. | `control_vm.py` (the `--dashboard-repo` flag wins) and the autoscaler. | The repository that a Spot control VM builds the dashboard from. Must be `owner/name`. |
| `SPOT_DASHBOARD_PUBLIC_URL` | `https://spot.swacktech.com`. The sample in `.env.example` is the same. | `control_vm.py` (the `--public-url` flag wins) and the autoscaler. | The public dashboard URL: `https://host[:port]` with an optional trailing slash. A path, query, fragment, or user info stops control VM creation. |
| `SPOT_CLOUDFLARED_IMAGE` | A `cloudflare/cloudflared` image pinned to a version tag and digest in `src/lib/control_vm.py`. | `control_vm.py` (the `--cloudflared-image` flag wins) and the autoscaler. | The `cloudflared` image for the tunnel. It must be pinned to a version tag or digest, and `:latest` is refused. |

`control_vm.py` sends the non-secret settings to the VM as instance metadata. The startup script
(`cloud-init/control-vm-startup.sh`) reads the metadata at every boot and rewrites the dashboard environment
file.

| Setting | Instance metadata key |
| --- | --- |
| Slot (`a` or `b`) | `spot-control-slot` |
| `SPOT_DASHBOARD_IMAGE` | `dashboard-image` |
| `SPOT_DASHBOARD_REPO` | `dashboard-repo` |
| `SPOT_DASHBOARD_SOURCE_REF` | `dashboard-source-ref` |
| `SPOT_DASHBOARD_PUBLIC_URL` | `dashboard-public-url` |
| `CLOUDFLARE_ACCOUNT_ID` | `cloudflare-account-id` |
| `CLOUDFLARE_KV_NAMESPACE_ID` | `cloudflare-kv-namespace-id` |
| `SPOT_CLOUDFLARED_IMAGE` | `cloudflared-image` |
| `DASHBOARD_SESSION_NOT_BEFORE` | `dashboard-session-not-before` |
| `SPOT_CONFIG_REPO` | `dashboard-config-repo` |
| `DASHBOARD_CONTROLLED_TARGETS` | `dashboard-controlled-targets` (separated by semicolons) |
| `DASHBOARD_GLOBAL_ADMINS` | `dashboard-global-admins` (separated by semicolons) |

The startup script also writes `DASHBOARD_PUBLIC_ORIGIN`, `GITHUB_OAUTH_REDIRECT_URI`,
`SPOT_GCP_TOKEN_SOURCE=metadata`, and `DASHBOARD_FLEET_INVENTORY=gce` into the dashboard environment file,
and it passes the same GitHub token as both `SPOT_CONFIG_GH_TOKEN` and `GH_TOKEN`. It fetches the OAuth 2.0
client ID and secret, the session secret, the control secret, the KV token, the GitHub token, and the
slot's tunnel token from Secret Manager. None of those secrets travel as instance metadata.

## Pricing and billing

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `GOOGLE_CLOUD_API_KEY` | None. | `src/spot_finder.py` (the `--billing-api-key` flag wins) and the dashboard. | **Secret.** An optional billing catalog API key. Without it, `spot_finder.py` can't compare against public on-demand rates and turns off its 95 percent discount alerts. The dashboard also accepts the two alternate names in the next row. |
| `GCP_BILLING_API_KEY`, `GOOGLE_CLOUD_BILLING_API_KEY` | None. | The dashboard's live pricing lookup. | **Secret.** Alternate names for the same key. The dashboard tries `GOOGLE_CLOUD_API_KEY`, then `GCP_BILLING_API_KEY`, then `GOOGLE_CLOUD_BILLING_API_KEY`. |
| `GCP_ACCESS_TOKEN`, `CLOUDSDK_AUTH_ACCESS_TOKEN` | None. | The dashboard's live pricing lookup. | **Secret.** An OAuth 2.0 access token for the pricing lookup, tried in that order. Without a token, the dashboard tries the active `gcloud` credentials, even when an API key is set, and then uses the `gcloud` project. With neither a key nor a token, it falls back to its static catalog baseline. |
| `GCP_BILLING_EXPORT_TABLE` | None. | `src/spot_runner_manager.py` and the autoscaler (the `--billing-export-table` flag wins). | A standard or detailed billing usage export table, `project.dataset.table`, used for project-wide cost logs. It needs `gcloud` credentials that can query the table through the BigQuery API. No `bq` command-line tool is involved. |
| `GCP_ENABLE_LIVE_PRICING` | None. | The dashboard's pricing module. | A test gate only. Under `unittest`, the value `1` lets the pricing code discover `gcloud` credentials. It has no effect in normal runs. |

## Runner host and container variables

These variables configure the runner containers that the host startup script (`cloud-init/startup.sh`)
creates. You don't normally set them by hand. The startup script passes them to `docker run`. The manager
renders the script from the deployment settings, which come from the command-line flags documented in
[Fleet and autoscaler](fleet-and-autoscaler.md).

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `RUNNER_STATE` | `/runner-state`. | `runner/entrypoint.sh` | The directory that holds the runner installation and its registration (`.runner`). |
| `RUNNER_CONFIGURE_ONLY` | `0`. | `runner/entrypoint.sh` | When `1`, the container registers the runner and exits, or exits at once if `.runner` already exists. Otherwise it runs `./run.sh` and fails if the runner isn't registered. |
| `GITHUB_RUNNER_URL` | None. Required when `RUNNER_CONFIGURE_ONLY=1`. | `runner/entrypoint.sh` | The GitHub URL to register against, such as the organization URL. |
| `GITHUB_RUNNER_TOKEN` | None. Required for the first registration. | `runner/entrypoint.sh` | **Secret.** A short-lived registration token. The manager renders it into the VM's `startup-script` metadata for the deployment and removes that metadata key afterward. The startup script writes it to a `0600` file under `/run`, passes it with `--env-file`, and removes the file after registration. |
| `RUNNER_NAME` | None. Required when registering. | `runner/entrypoint.sh` and `runner/docker-wrapper.py` | The runner name, `<prefix>-<hostname>-<n>`. |
| `RUNNER_LABELS` | None. Required when registering. | `runner/entrypoint.sh` | Comma-separated runner labels. |
| `RUNNER_GROUP_NAME` | Empty, which uses the default group. | `runner/entrypoint.sh` | The runner group to register into. |
| `GHA_ADMISSION_REQUIRED` | `0`. The startup script sets `1` on tiered hosts only. | `runner/job-started.sh` and `runner/job-completed.sh` | When `1`, the job-started hook acquires a memory lease from `host_admission.py` before the job runs, and the job-completed hook releases it before it cleans the target directory. The container also mounts the admission socket directory read-only. |
| `OXIDEX_CLEAN_TARGET` | `1`, set in `runner/Dockerfile`. | `runner/clean_target.py` | Must equal `1` for the job-completed hook to remove the completed job's default Cargo `target` directory. The cleanup also requires that `GITHUB_REPOSITORY` is `swack-tools/oxidex` and that `GITHUB_WORKSPACE` is the expected workspace under `/work`. GitHub Actions sets those two variables. |
| `ACTIONS_RUNNER_HOOK_JOB_STARTED`, `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` | The two hook scripts under `/usr/local/bin`, set in `runner/Dockerfile`. | The GitHub Actions runner | The runner calls these scripts before and after each job. |
| `GHA_REAL_DOCKER` | `/usr/bin/docker`. | `runner/docker-wrapper.py` | The Docker command-line binary that the service-container reachability wrapper calls. |
| `GHA_DOCKER_WRAPPER_STATE_DIR` | `/tmp/gha-docker-wrapper`. | `runner/docker-wrapper.py` | Where the wrapper keeps its per-job state. |
| `GHA_DOCKER_WRAPPER_HANDSHAKE_TIMEOUT` | `15.0` (seconds). | `runner/docker-wrapper.py` | How long the wrapper waits for a service-container handshake. |
| `GHA_RUNNER_CONTAINER_ID` | Detected. | `runner/docker-wrapper.py` | Overrides detection of the runner's own container ID. Without it, the wrapper inspects `HOSTNAME`, then `RUNNER_NAME`. |

The Dockerfile also sets `CARGO_HOME`, `RUSTUP_HOME`, and `PATH` so job steps find the pinned Rust toolchain.
`runner/compose.yaml` is a reference Compose file, not something that the controller deploys. It requires
`RUNNER_IMAGE` and reads optional `RUNNER_MEMORY_GB` (default `27`), `RUNNER_GROUP_ID` (with a built-in default group ID),
`RUNNER_LABELS` (default `oxidex-spot-7x27,oxidex,spot`), and `RUNNER_NAME` (default `oxidex-runner`). The entrypoint reads `RUNNER_GROUP_NAME`, not `RUNNER_GROUP_ID`, so the Compose value of `RUNNER_GROUP_ID` has no effect.

For the memory lease behavior and the files that `host_admission.py` uses, see
[Operations](operations.md) and [Security](security.md).

### Local Docker runner variables

The `m4air/` Compose project passes these variables to its two runner containers. `m4air/runner-entrypoint.sh`
reads them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RUNNER_URL` | The organization URL `https://github.com/swack-tools`. | The GitHub URL to register against. |
| `RUNNER_NAME` | None. Required. | The runner name. Compose sets `oxidex-m4air-1` and `oxidex-m4air-2`. |
| `RUNNER_GROUP` | `oxidex-m4air`. | The runner group to register into. |
| `RUNNER_LABELS` | `oxidex-m4air` in the script, and `oxidex-m4air,docker` in Compose. | Custom labels. Compose wins. They apply only to new registrations. |
| `RUNNER_WORKDIR` | `/runner-state/_work`. | The job work directory. |
| `RUNNER_HOME` | `/runner-state`, set in `m4air/Dockerfile`. | The runner installation and registration directory. |
| `RUNNER_TOKEN` | None. Required for the first registration. | **Secret.** Read from the temporary `/run/secrets/runner.env` mount, and unset before the listener starts. |
| `DOCKER_GID` | `0`, read by Compose. | The numeric group ID of the Docker socket inside the container. |

## Builder and remote-build client variables

The dedicated builder startup script takes its settings from a JSON configuration file, not from environment
variables. Its container shape, 16 CPUs and 28 GiB, is fixed. See
[Set up a builder](dedicated-builders.md). The variables below configure the build client on your workstation.

| Variable | Default | Read by | Meaning |
| --- | --- | --- | --- |
| `OXIDEX_REMOTE_INSTANCE` | None. | `src/cargo_remote.py` | The builder VM name. You can also set it in the `[env]` table of the checkout's `.cargo/config.toml`. The process environment wins. `auto_remote_build.py` sets it for the child build to the worker that it selected. |
| `OXIDEX_REMOTE_ZONE` | None. | `src/cargo_remote.py` | The builder zone. Same sources and precedence as the instance. |
| `OXIDEX_REMOTE_PROJECT` | The active `gcloud` project. | `src/cargo_remote.py` and `src/auto_remote_build.py` | The GCP project of the builder. |
| `OXIDEX_REMOTE_WORKTREE` | `oxidex-` plus the first 12 hex characters of the SHA-256 of the source checkout path. | `src/cargo_remote.py` | The worktree identity on the builder. |
| `OXIDEX_REMOTE_INSTANCE_ID`, `OXIDEX_REMOTE_SSH_USER`, `OXIDEX_REMOTE_SSH_KEY`, `OXIDEX_REMOTE_SSH_KNOWN_HOSTS` | None. | Not read by this repository. | Settings for the OxiDex build client. `render_builder_startup.py` writes the SSH user into `cargo-remote-env.toml` and lists the other three there as comments for you to complete. |
| `OXIDEX_CONTROL_REPO` | None. | The `justfile` (`just test`) | The path to a reviewed OxiDex control checkout that has the `test-infra-ci-remote` recipe. `just test` stops with an error when it is unset or the recipe is missing. |
| `CARGO_HOME` | `~/.cargo` | `src/install_remote_commands.py` | The shim install directory defaults to `$CARGO_HOME/bin`. The `--bin-dir` flag wins. |

## Variables that the code sets for child processes

You don't configure these. They appear in process listings and logs, so they are listed for completeness.

| Variable | Set by | Value and purpose |
| --- | --- | --- |
| `CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR` | `control_vm.py` and `runner_fleet.py`, for `gcloud compute` calls | `false`, so a partial listing error fails the call instead of hiding hosts. |
| `OXIDEX_ACTIVE_LOCK_FD` | The dashboard's embedded builder runtime-update script | The descriptor of the held cache lock, which a child transaction inherits. |
| `RUSTC`, `RUSTDOC`, `RUSTUP_TOOLCHAIN`, `PATH`, `CARGO_ZIGBUILD_ZIG_PATH`, `CARGO_ZIGBUILD_CACHE_DIR`, `ZIG_GLOBAL_CACHE_DIR`, `ZIG_LOCAL_CACHE_DIR`, `OXIDEX_DARWIN_FRAMEWORK_DIR` | The builder entrypoints (`builder/entrypoint.py`, `builder/standalone/standalone-entrypoint.py`) | The verified compiler pin and cache locations inside the builder container. |
| `OXIDEX_OPS_DIR`, `OXIDEX_TARGET_ROOT`, `GIT_CONFIG_*`, `PYTHONDONTWRITEBYTECODE` | `lib/qualification_trust` | A fixed, sanitized environment for qualification workloads. The code clears other inherited variables. |

## Worker variables and bindings

The edge Worker (`cloudflare/worker.js`) reads these names from its environment. `cloudflare/wrangler.toml`
declares the plain variables and the KV binding. Set secrets with `wrangler secret put`, never in
`[vars]`. The sample file uses a placeholder for the KV namespace ID, which you replace with your own.

| Name | Type | Default | Meaning |
| --- | --- | --- | --- |
| `SPOT_KV` | KV namespace binding | None. The `wrangler.toml` `id` is a placeholder. | The namespace that holds the `origin:active` record. Without it, the Worker can't find an origin and answers with the 503 wait response. |
| `SESSION_SECRET` | Secret | None. | **Secret.** The same value as `DASHBOARD_SESSION_SECRET`. Without it, every session fails verification. |
| `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` | Secrets | None. | **Secret.** The Access service token. The Worker sends it as `CF-Access-Client-Id` and `CF-Access-Client-Secret` to the origin tunnel hostname. |
| `ORIGIN_HOST_SUFFIX` | Variable | A dot plus a domain that the repository owner controls, in both the code and `wrangler.toml`. Set your own. | The suffix that origin hostnames in the KV record must end with. Use your own domain with a leading dot. It must match `SPOT_CONTROL_ORIGIN_DOMAIN`. |
| `SESSION_NOT_BEFORE` | Variable | Empty in `wrangler.toml`, which means no cut-off. | The same bulk sign-out cut-off as `DASHBOARD_SESSION_NOT_BEFORE`: 1 to 15 digits of Unix seconds. A malformed value makes the Worker reject all sessions. |

The Worker's name, `spot-dashboard-edge`, and its route are in `wrangler.toml`. Set the account ID in the
deploying shell with `CLOUDFLARE_ACCOUNT_ID` rather than in the file.

## Configuration that doesn't use environment variables

| What | Where | Notes |
| --- | --- | --- |
| Central runner configuration | The config repository (`SPOT_CONFIG_REPO` or `--config-repo`), files `defaults.yaml` and `repos/<owner>/<repo>.yaml` | Per-organization defaults plus per-repository runner patches. The dashboard also treats each `repos/<owner>/<repo>.yaml` as a controlled target. See [Runner patches](runner-patches.md). |
| Builder configuration | A JSON file that you pass to `render_builder_startup.py --config` | The keys are documented in [Set up a builder](dedicated-builders.md). |
| Autoscaler state | `~/Library/Application Support/Spot Runner Autoscaler/state.json` (`--state-file`) | Use a separate state file for each `--target`. Don't edit it by hand. |
| Autoscaler log | `~/spot-ghrunner.log` (`--log-file`) | The LaunchAgent also writes standard output and error to `~/Library/Logs/spot-runner-autoscaler.log`. |
| LaunchAgent | `~/Library/LaunchAgents/com.swacktools.spot-runner-autoscaler.plist` | Written and bootstrapped by `--install-launch-agent`. |
| Control VM state and lock | `control-vm-<project>.json` and `.lock` in `SPOT_CONTROL_STATE_DIR` (`--control-state-dir`) | The default directory is the one that holds the autoscaler's default state file. |
| Dashboard state | The `--state-file` path; `/var/lib/spot-dashboard/state.json` on the control VM | Without `--state-file`, the dashboard writes no state file. |
| Dashboard environment file | `/etc/spot-dashboard/dashboard.env` on the control VM | Rewritten at every boot. Don't edit it by hand. |
| Remote build evidence | `~/oxidex-ops/evidence/` | `cargo_remote.py` and `auto_remote_build.py` write timestamped evidence folders here. |
| Runner admission settings and leases | `/etc/gha-admission.json` and `/var/lib/gha-admission/leases.json` on a tiered runner host | The startup script writes the configuration. Never edit the lease ledger to bypass a hold or drain. |
