# Spot runner fleet and dedicated builders

Provision and operate persistent GitHub Actions runners and dedicated Rust build
VMs on GCP (Google's cloud platform) Spot instances. The repository includes a
price finder, a deployment manager, a fleet controller, remote build clients, and a
web dashboard that runs on a small control VM behind a Cloudflare Worker.

The controller manages runner capacity for a GitHub organization or repository.
Dedicated builders are separate VMs with no GitHub runner registration. SSH
terminates on the host; app builds run as an unprivileged user in a
resource-limited Docker container with persistent Cargo and target caches.

This guide describes the source on `main`. Merging source does not update a
running controller, container, or VM. Inspect the deployed configuration and
actual tool versions before routing work to a host.

## Documentation

The full documentation is a MkDocs site. The **Documentation** workflow
(`.github/workflows/docs.yml`) builds the pages in `docs/` and deploys them to GitHub
Pages. You can also read the Markdown sources directly.

| Topic | Page |
| --- | --- |
| Overview and architecture | [Home](docs/index.md) |
| Prerequisites, `.env`, first read-only commands | [Getting started](docs/getting-started.md) |
| Spot price tools | [Spot pricing and offers](docs/spot-pricing.md) |
| Create or adopt runner hosts, sizing, strict pools | [Provision runner hosts](docs/provisioning.md) |
| Scaling modes, runner floor, price rotation, drain, admission | [Fleet and autoscaler](docs/fleet-and-autoscaler.md) |
| Patch manifests and hot patching | [Runner patches](docs/runner-patches.md) |
| Sign-in, administrators, patch console | [Dashboard](docs/dashboard.md) |
| Worker, tunnels, KV, control VM, free-tier VM | [Edge hosting and control VM](docs/edge-dashboard.md) |
| Dedicated Rust builders | [Set up a builder](docs/dedicated-builders.md), [Remote builds and clients](docs/remote-builds.md), and [Runtime updates](docs/builder-runtime-updates.md) |
| Local ARM64 Docker runners | [Local Docker runners](docs/local-arm64-runners.md) |
| Every environment variable | [Configuration](docs/configuration.md) |
| Every command and flag | [Commands](docs/commands.md) |
| Safety rules and common failures | [Operations and troubleshooting](docs/operations.md) |
| Trust boundaries and secrets | [Security model](docs/security.md) |
| Tests, Vale, and the review workflow | [Development](docs/development.md) |

## Choose a workflow

| Goal | Entry point | Creates or changes resources? |
| --- | --- | --- |
| List Spot compute prices by minimum CPU, RAM, and daily cap | `tools/spot_prices.py` | No |
| Find candidate Spot offers with deployment constraints | `src/spot_finder.py` | No |
| Inspect runners and VMs | `src/runner_fleet.py` | No |
| Preview or provision runner hosts | `src/spot_runner_manager.py` | Only with `--apply`, or `--patch-live` (which changes a running host without a preview) |
| Plan or manage runner capacity | `src/spot_runner_autoscaler.py` | Yes, except a tiered `--dry-run` without `--install-launch-agent` |
| Render a dedicated builder launch | `src/render_builder_startup.py` | Writes local files; the generated launch command creates a VM |
| Build on an explicitly selected legacy host | `src/remote_rust_build.py` | Transfers source and starts a remote build |
| Install Cargo client commands | `src/install_remote_commands.py` | Writes local executable shims |
| Run the Web UI control plane and telemetry dashboard | `src/web_dashboard.py` | Updates the local dashboard state file. With `SPOT_CONFIG_GH_TOKEN` or `GH_TOKEN` set, `POST /api/patches/save` creates a branch, commits, and opens a pull request on the config repository. With `DASHBOARD_HOT_PATCH_EXECUTOR=gcloud`, `/api/patches/hot-patch` changes running hosts. |
| Update an occupied dedicated builder in place | `src/lib/builder_runtime_update.py` | Only with `--apply`, `--finish`, `--recover-finish`, or `--recover-committed` |
| Inspect, plan, or manage the Spot control VM that hosts the dashboard | `src/control_vm.py` | Only with `--apply`; `render-startup` writes a local file |

Keep the distinction between registered listeners and active jobs. Twenty
registered runners do not mean that twenty jobs can each use 32 GiB at once.
Admission reserves memory before starting work; the physical host also needs
space for its operating system, services, and idle listeners.

## Quick start

Install Python 3.11 or later, Git, `gcloud`, and `gh`. The Python entry points use
the standard library only. Then run the read-only commands first:

```bash
git clone https://github.com/swack-tools/spot-github-runners.git
cd spot-github-runners

python3 tools/spot_prices.py --min-cpu 4 --min-ram 8 \
  --price-per-day 5 --list cheapest --results 20

python3 src/runner_fleet.py --org swack-tools --project YOUR_PROJECT \
  --label spot --minimum-runners 1 --runners-per-vm 8
```

For runner operations, copy `.env.example` to `.env`, run `chmod 600 .env`, and set a
renewable `GH_TOKEN`. Git ignores the file. See
[Getting started](docs/getting-started.md) for the full setup.

## Safety rules

- A request to edit code or documentation does not authorize changing live
  resources. Use read-only commands or previews (`--dry-run`, the `plan` and
  `status` subcommands) unless you explicitly want a live operation. An
  autoscaler `--dry-run` still connects with `gcloud compute ssh`, which can add
  your SSH key to project or instance metadata, or to your OS Login profile. It
  also writes the local state lock file and controller log.
- `spot_runner_manager.py --apply` deletes old, unattached, manager-labeled disks
  in the selected zone before it provisions, even if the deployment then fails.
- Do not add `--install-launch-agent` to a preview: it writes and starts the macOS
  LaunchAgent before the dry-run check.
- Never replay fresh-host bootstrap on an occupied runner or builder. Use the staged
  [runtime update workflow](docs/builder-runtime-updates.md) for an occupied builder.
- Never delete or hand-edit controller state, admission lease ledgers, or
  runtime-update journals to bypass a hold or drain.
- Keep untrusted external pull requests off persistent self-hosted runners.

See [Operations and troubleshooting](docs/operations.md) and the
[Security model](docs/security.md).

## Repository layout

| Path | Responsibility |
| --- | --- |
| `src/` | Command-line entry points. Most are thin wrappers around modules in `src/lib/`. |
| `src/lib/runner_manager.py` | Runner provisioning, sizing, bootstrap, and registration |
| `src/lib/spot_runner_autoscaler.py` | Controller entry, persisted operations, and service installation |
| `src/lib/tier_controller.py`, `tier_policy.py` | Tier decisions and migration coordination |
| `src/lib/runner_drain.py`, `vm_recovery.py`, `job_recovery.py` | Drain, loss cleanup, and eligible job retries |
| `src/lib/runner_patch.py`, `patch_console.py` | Runner patch manifests and the dashboard patch console |
| `src/lib/control_vm.py`, `edge_state.py` | Control VM lifecycle and the edge state contract |
| `src/lib/spot_pricing.py`, `billing_catalog.py`, `storage_selection.py` | Compute and storage pricing and compatible disk selection |
| `src/lib/host_admission.py` | Work reservations, leases, and actual-owner checks |
| `src/lib/builder_startup.py`, `builder_access.py` | Dedicated startup, identity enrollment, and readiness |
| `src/lib/qualification_trust/` | Source authentication, sealed import, and qualification mounts |
| `src/lib/dashboard_*.py`, `rust_build_telemetry.py` | Web UI control plane, CPU benchmarks, OOM deep links, and Rust builder telemetry |
| `cloud-init/` | Canonical runner, control VM, and dedicated-builder startup templates |
| `runner/` | Runner image and hooks |
| `builder/standalone/`, `builder/dedicated/` | Standalone Rust 1.99 image and the dedicated host launcher and Docker admission adapter |
| `cloudflare/` | The Cloudflare Worker that fronts the dashboard |
| `dashboard/` | The dashboard container image |
| `m4air/` | Persistent local ARM64 Docker runners |
| `docs/` | The documentation site sources |
| `tests/` | Unit and lifecycle controls |

## Development

Develop on an isolated branch based on current `main`. Run the full Python unit
suite before you commit:

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

Check documentation with Vale and build the site in strict mode:

```bash
vale sync
vale README.md AGENTS.md CLAUDE.md docs/*.md

python3 -m venv "$HOME/oxidex-ops/docs-venv"
"$HOME/oxidex-ops/docs-venv/bin/pip" install -r docs/requirements.txt
"$HOME/oxidex-ops/docs-venv/bin/mkdocs" build --strict --site-dir "$HOME/oxidex-ops/docs-site"
```

See [Development](docs/development.md) for the remote test proof, the embedded asset
rules, and the pull request review workflow.
