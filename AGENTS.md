# AI agent guidelines and model routing

This file defines subagent model routing for Claude, Gemini, and Codex harnesses, followed by repository-wide engineering, verification, operational safety, and Git review rules for `swack-tools/spot-github-runners`.

## Claude subagent model routing

These model rules apply when launching Claude subagents. Keep the human-selected main-session model unchanged. Delegate bounded, independent tasks when parallel research or isolated implementation is useful; handle small, coupled changes directly.

- Use Opus 5.5 (`opus-5.5`) at High (`high`) effort for more complex coding tasks, ambiguous architecture, security and container admission boundaries, and VM or storage lifecycle safety.
- Use Sonnet 5.5 (`sonnet-5.5`) at High (`high`) effort for cross-module debugging, state-machine or concurrency reasoning, autoscaler and tier policy analysis, and multi-file refactoring.
- Use Sonnet 5.5 (`sonnet-5.5`) at Medium (`medium`) effort for bounded implementation, unit test additions, documentation updates, and focused code reviews with explicit contracts.
- Use Haiku 5.5 (`haiku-5.5`) at High (`high`) effort for basic code identification and grepping scenarios, fast file or symbol discovery, log summarization, and read-only configuration checks.

| Delegated task | Model | Model ID | Effort |
| --- | --- | --- | --- |
| Basic code identification and grepping scenarios, file/symbol lookup, log summarization, and read-only checks | Haiku 5.5 | `haiku-5.5` | High (`high`) |
| Bounded implementation, unit test additions, or focused review with clear contracts | Sonnet 5.5 | `sonnet-5.5` | Medium (`medium`) |
| Cross-module debugging, lock/lease concurrency reasoning, and tier or pricing policy tracing | Sonnet 5.5 | `sonnet-5.5` | High (`high`) |
| More complex coding tasks, ambiguous architecture, admission/security boundaries, and VM/disk recovery safety | Opus 5.5 | `opus-5.5` | High (`high`) |

### Claude delegation rules

- Pass both `model` and `effort` explicitly on subagent launches (`--model` and `--effort` for command-line children) with a self-contained brief covering the goal, file paths, constraints, owned files, and verification commands.
- Select the route by failure impact and state complexity rather than diff size. Escalate with a concrete explanation when a task exceeds its initial tier.
- Verify that `CLAUDE_CODE_EFFORT_LEVEL` or global subagent model overrides do not silently downgrade the requested route; report any mismatch instead of claiming the requested effort was applied.

## Gemini subagent model routing

These model rules apply when launching Gemini subagents in Google Antigravity, the Gemini command-line tool, or the Google GenAI and Antigravity SDKs. Keep the human-selected main-session model (such as Gemini 4 Argon at High effort) unchanged.

- Use Gemini 4 Argon (`gemini-4-argon`, Antigravity `pro` or `inherit` tier) at High (`high`) effort for more complex coding tasks, ambiguous architecture, security and container admission boundaries, fleet/VM/disk recovery invariants, and final pre-PR verification.
- Use Gemini 3.8 Flash (`gemini-3.8-flash`, Antigravity `flash` tier) at High (`high`) effort for cross-module debugging, multi-file feature or test implementation, pricing and storage catalog logic, and state-transition tracing.
- Use Gemini 3.8 Flash (`gemini-3.8-flash`, Antigravity `flash` tier) at Medium (`medium`) effort for bounded implementation, straightforward unit tests, documentation updates, and focused code reviews with clear contracts.
- Use Gemini 3.5 Flash-Lite (`gemini-3.5-flash-lite`, Antigravity `flash_lite` tier) at High (`high`) effort for basic code identification and grepping scenarios, fast file or symbol inventory, log filtering, and read-only repository lookups.

| Delegated task | Model | Model ID / Antigravity tier | Effort (`thinking_level`) |
| --- | --- | --- | --- |
| Basic code identification and grepping scenarios, file/symbol inventory, log filtering, and read-only lookups | Gemini 3.5 Flash-Lite | `gemini-3.5-flash-lite` (`flash_lite`) | High (`high`) |
| Bounded implementation, unit test additions, documentation updates, and focused review with clear contracts | Gemini 3.8 Flash | `gemini-3.8-flash` (`flash`) | Medium (`medium`) |
| Cross-module debugging, multi-file changes, pricing/storage catalog logic, and state-machine tracing | Gemini 3.8 Flash | `gemini-3.8-flash` (`flash`) | High (`high`) |
| More complex coding tasks, ambiguous architecture, admission/security boundaries, and VM/disk recovery safety | Gemini 4 Argon | `gemini-4-argon` (`pro` / `inherit`) | High (`high`) |

### Gemini delegation rules

- When invoking Antigravity subagents (`invoke_subagent`), select the matching `Model` tier (`flash_lite`, `flash`, `pro`, or `inherit` when the parent session already runs `gemini-4-argon` at `high`) and set `Workspace` to `inherit` for read-only tasks or `share`/`branch` when isolating concurrent writers.
- When configuring Gemini API or SDK subagents directly, set both `model` and `thinking_level` (`low`, `medium`, or `high`) explicitly. Never use deprecated `gemini-2.5-*`, `gemini-2.0-*`, or `gemini-1.5-*` models.
- Give each subagent a self-contained prompt with explicit target paths, invariants to preserve, and verification steps. Keep concurrent writers on disjoint file sets or isolated workspaces.

## Codex subagent and review routing

When delegating to OpenAI Codex subagents or running local Codex reviews, apply the equivalent task routing and keep the human-selected main-session model unchanged:

| Delegated task | Model | Effort |
| --- | --- | --- |
| Basic code identification, file/symbol inventory, grepping scenarios, and mechanical edits | `gpt-6-luna` | `high` |
| Bounded implementation or focused correctness review with clear contracts | `gpt-6.1-sol` | `medium` |
| Cross-module debugging, concurrency/lease reasoning, or PR verification review | `gpt-6.1-sol` | `high` |
| More complex coding tasks, ambiguous architecture, security boundaries, and storage/release safety | `gpt-6-astra` | `high` |

## Repository overview and architecture

This repository provisions and operates persistent GitHub Actions runners and dedicated Rust 1.99 build VMs on GCP (Google's cloud platform) Spot instances, primarily serving `swack-tools/oxidex`, plus persistent local ARM64 Docker runners (`m4air/`).

### Primary entry points

| Entry point | Purpose | Creates or changes resources? |
| --- | --- | --- |
| `tools/spot_prices.py` | Query official GCP billing catalog and machine inventory for Spot compute prices by CPU, RAM, and daily cap | No |
| `src/spot_finder.py` | Find candidate Spot VM offers with deployment constraints | No |
| `src/runner_fleet.py` | Inspect GitHub runner registrations and GCP VM fleet state | No |
| `src/spot_runner_manager.py` | Preview or provision Spot runner hosts and strict heavy pools (`--heavy-runner-pool`) | Only with `--apply` |
| `src/spot_runner_autoscaler.py` | Plan or manage runner capacity (`tiered`, `resource`, or `legacy` mode) or install the macOS LaunchAgent | Yes, except a tiered `--dry-run` without `--install-launch-agent` (leaves provider resources and controller state unchanged; `--install-launch-agent` writes and bootstraps the LaunchAgent before the dry-run check) |
| `src/render_builder_startup.py` | Validate configuration and approved uploader identity, then render `startup.sh`, `cloud-config.yaml`, `cargo-remote-env.toml`, and `launch-command.sh` | Writes local output directory; running `launch-command.sh` creates a billable VM |
| `src/remote_rust_build.py` | Snapshot source, transfer, compile, verify checksums, and record evidence on a selected legacy host | Transfers source and starts a remote build |
| `src/install_remote_commands.py` | Install `cargo-oxidex-remote` and `cargo-oxidex-auto` shims into `~/.cargo/bin` | Writes local executable shims |
| `src/lib/builder_runtime_update.py` | Capture, verify, apply, finish, or recover an in-place runtime update on an occupied dedicated builder | Capture and default `--plan` check are read-only; mutates only with `--apply`, `--finish`, `--recover-finish`, or `--recover-committed` |

### Key directories and modules

| Path | Responsibility |
| --- | --- |
| `src/lib/` | Core library modules covering runner provisioning (`runner_manager.py`), fleet autoscaling (`spot_runner_autoscaler.py`, `tier_controller.py`, `tier_policy.py`), pricing and storage compatibility (`spot_pricing.py`, `billing_catalog.py`, `storage_selection.py`, `price_guard.py`), host admission and memory leases (`host_admission.py`), drain and recovery (`runner_drain.py`, `vm_recovery.py`, `job_recovery.py`, `oom_disposition.py`, `oom_obligations.py`), and dedicated builder startup and runtime updates (`builder_startup.py`, `builder_access.py`, `builder_runtime_update.py`, `builder_fresh_guard.py`). |
| `src/lib/qualification_trust/` | Signed source authentication, sealed executable import, protective supervision locks, and mount verification for qualification workloads. |
| `cloud-init/` | Canonical startup scripts for runner hosts (`startup.sh`) and standalone dedicated builders (`builder-startup.sh`). |
| `runner/` | Docker runner image (`Dockerfile`), entrypoint (`entrypoint.sh`), job admission hooks (`job-started.sh`, `job-completed.sh`), and safe descriptor-relative target cleanup (`clean_target.py`). |
| `builder/standalone/` and `builder/dedicated/` | Standalone Rust 1.99.0 builder image, Node/Playwright website audit tools, toolchain probe, worktree command runner, root-owned host launcher (`host-launcher.py`), and Docker admission wrapper (`docker-admission.py`). |
| `tools/` | Standalone Spot price command-line tool (`spot_prices.py`) and signed-source remote test proof runners (`infra_python_test_proof.py`, `infra_python_complete_proof.py`). |
| `tests/` | Comprehensive `unittest` suite (`test_*.py`) covering pricing, storage, admission, draining, OOM classification, builder startup, runtime updates, and recovery invariants. |

## Codebase invariants and synchronization rules

### Embedded asset parity

Several modules embed or load canonical scripts and Dockerfiles that are tested for exact parity against their tracked files on disk:

- `src/lib/runner_assets.py` embeds the contents of `runner/clean_target.py`, `runner/job-completed.sh`, `runner/Dockerfile`, `runner/entrypoint.sh`, and `cloud-init/startup.sh`. When modifying any of those files, update the corresponding constant in `src/lib/runner_assets.py` in the same commit.
- `src/lib/builder_assets.py` embeds the legacy shared-host builder files (`builder/Dockerfile`, `builder/entrypoint.py`, `builder/host-launcher.py`). Keep both in sync whenever legacy shared-host builder assets change.
- `src/lib/builder_only_assets.py` reads standalone dedicated builder assets directly from `builder/standalone/`, `builder/dedicated/`, and `cloud-init/builder-startup.sh`. Keep file lists, permissions, and runtime-update target manifests (`src/lib/builder_runtime_update.py`) aligned when modifying dedicated builder files.

### Remote test proof contracts

- `tools/infra_python_complete_proof.py` pins the exact SHA-256 (`PRODUCER_SHA`) of `tools/infra_python_test_proof.py`. Any reviewed change to `tools/infra_python_test_proof.py` requires updating `PRODUCER_SHA` and verifying the proof tests (`tests/test_infra_python_test_proof.py`).
- `tools/infra_python_test_proof.py` enforces minimum test case (`MIN_CASES`) and test module (`MIN_MODULES`) floors and verifies that every `tests/test_*.py` module matches the signed manifest without duplicate test identities. Never delete or consolidate test modules below the floor without a reviewed contract update.

### Allowlisted `.gitignore` and `.vale.ini`

- The root `.gitignore` uses a deny-by-default (`*`) rule at the top and explicitly un-ignores tracked paths (`!README.md`, `!AGENTS.md`, `!CLAUDE.md`, `!*.py`, and so on). When adding a new root documentation file or non-standard asset path, add a matching `!` allowlist entry in `.gitignore` so Git tracks it cleanly without `git add -f`.
- Register new Markdown documentation files in `.vale.ini` under `BasedOnStyles = Google` and verify them with Vale before committing.

## Operational safety and infrastructure guardrails

- **No unauthorized cloud or live-fleet mutations**: a prompt to edit code or documentation does not authorize creating, modifying, draining, restarting, or deleting GCP VMs, persistent disks, GitHub runner registrations, or the local macOS LaunchAgent (`com.swacktools.spot-runner-autoscaler`). Always use read-only commands or `--dry-run` previews without `--install-launch-agent` (since `--install-launch-agent` writes and bootstraps the live daemon before the dry-run branch executes) unless the user explicitly authorizes a live operational action.
- **Keep runner and dedicated-builder lifecycles separate**:
  - Runner hosts (`oxidex-runners-*`) register GitHub Actions listeners (controller-managed hosts use runner group ID 10 for `swack-tools/oxidex`) and use auto-deleting data disks. Registered listener counts do not equal concurrent memory capacity; `host_admission.py` reserves memory before a job starts.
  - Dedicated builders (`builder-*`) fix the shape to `n4-highcpu-16` (16 vCPU, 32 GiB RAM), register zero GitHub runners, run builds inside one unprivileged UID/GID 1001 container capped at 16 CPUs and 28 GiB with no extra swap, and attach a persistent cache disk with `auto-delete=no`. Deleting a builder VM does not delete its cache disk.
- **Preserve admission, security, and recovery boundaries**:
  - Never weaken `host-launcher.py`, `docker-admission.py`, `host_admission.py`, `builder_access.py`, or `qualification_trust` checks. Do not pass the Docker socket into builder containers, broaden directory permissions to bypass an uploader/control account mismatch, or replace direct authenticated SSH for `oxidex-uploader` with `gcloud compute ssh/scp`.
  - Never replay fresh-host bootstrap (`startup.sh` / `builder-startup.sh`) on an occupied runner or builder VM. Occupied dedicated builders must use the staged, lock-guarded `builder_runtime_update.py` workflow (`capture` -> `check` -> `apply` -> client pin handoff -> `finish`).
  - Never delete or hand-edit controller state files, admission lease ledgers (`/var/lib/gha-admission/leases.json`), or runtime-update journals to bypass a hold or drain.
  - Distinguish host OOM from container OOM and require kernel or cgroup evidence joined to the active job interval before classifying exit code 137.
- **Label evidence states accurately**: local unit tests, remote builder proof runs (`just test`), GitHub CI checks, merged commits on `main`, rendered startup payloads, and live VM/container observations prove different things. State the exact command and environment for every verification claim, and never claim a live deployment or workload qualification from a source-only test run.

## Development and verification workflow

### Environment and dependencies

- Control-plane scripts target Python 3.11+ on macOS and Linux using the Python standard library. Do not add third-party runtime dependencies without explicit user approval.
- Keep secrets, `.env`, SSH private keys, approved uploader JSON files, rendered launch directories, and operational state outside the Git checkout (for example under `$HOME/oxidex-ops`). If a change adds or renames an environment variable, document it in `.env.example`.

### Running tests and checks

1. **Focused and full Python unit test suite**. Run targeted tests while iterating, and run the full discovery suite before committing:
   ```bash
   PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v
   ```
2. **Reviewed remote CI proof**. When `OXIDEX_CONTROL_REPO` points to a reviewed OxiDex checkout with `test-infra-ci-remote`, run `just test` to execute the signed-source suite inside the admitted remote Linux builder container. Do not claim `just test` ran if only local `unittest` discovery was executed.
3. **Documentation and prose lint**. Documentation follows the Google developer documentation style guide (sentence-case headings, active voice, direct instructions, explicit prerequisites). Synchronize and run Vale before committing documentation changes:
   ```bash
   vale sync
   vale README.md AGENTS.md CLAUDE.md docs/*.md
   ```
   Also verify command-line flags in documentation examples against `--help` output from the corresponding script.

## Git workflow and review gates

A human task prompt authorizes agents to work on an isolated feature branch, commit task changes, push the branch, open a pull request against `main`, resolve review comments, and squash-merge the pull request into `main` once all review and CI gates pass. It does not authorize force-pushing, changing branch protections, or mutating live cloud resources.

### Branch, identity, and account setup

- Develop on a dedicated branch or isolated Git worktree based on the latest `origin/main`. Never commit or push directly to `main`.
- Ensure commits are authored and SSH-signed as `swackhamer` (`user.name=swackhamer`, `user.email=swackhamer@users.noreply.github.com`, `commit.gpgsign=true`). Verify local commit signature headers (`git cat-file commit HEAD` or `git -c gpg.ssh.allowedSignersFile=... log -n 1 --show-signature`) and verify GitHub's verification status on the actual pushed pull request commits (`gh api repos/swack-tools/spot-github-runners/commits/<sha> --jq '.commit.verification'`). A synthetic unsigned commit created inside an ephemeral review sandbox (for example `Codex <codex@openai.com>`) does not establish the published pull request commit's author or signature status.
- When multiple GitHub command-line tool accounts are logged in on the workstation, export the `swackhamer` token for the shell session or prefix every `gh` command (including `gh pr create`, `gh pr view`, `gh pr comment`, `gh pr checks`, and `gh pr merge`) so no operation falls back to another stored account:
  ```bash
  export GH_TOKEN="$(gh auth token --user swackhamer)"
  gh pr status --repo swack-tools/spot-github-runners
  ```
- Stage task files by explicit path (`git add <path>`) so scratch files, local `.vale/styles/`, and unrelated edits are never committed.

### Local review and pull request review loop

1. **Local verification and review before opening a pull request**:
   - Run the full unit test suite and `vale` checks on all modified documentation files.
   - Obtain an independent local review of the complete diff against `origin/main` from Antigravity (via an independent Antigravity subagent or session) or Codex (for example via `codex exec --model gpt-6.1-sol -c model_reasoning_effort=high -c service_tier=default --sandbox read-only --ephemeral review --base origin/main`) and resolve any findings before opening the pull request.
2. **Pull request review loop**:
   - Push the feature branch and open a pull request targeting `main` in `swack-tools/spot-github-runners`.
   - Obtain an independent Antigravity review on the pull request or trigger the GitHub Codex review (`@codex review`).
   - Inspect every review comment against the codebase. Fix every valid issue, rerun unit tests and Vale checks, push the signed commit, reply to the review comment with the fix and verification evidence, and resolve the review thread.
   - After resolving review comments, obtain a fresh Antigravity review or comment `@codex rereview` (or `@codex review` if required by the connector trigger) on the pull request and wait for the updated review on the new head commit.
   - Repeat this resolve-and-rerequest cycle until the reviewer (Antigravity or Codex) reports no remaining issues (gives the go-ahead / `gtg` or 👍 reaction on the current head commit), all review threads are resolved, and required GitHub checks (such as CodeQL) pass.
3. **Squash and merge**:
   - Confirm that `origin/main` has not advanced in a conflicting way and that the reviewed head commit matches the pull request head, then squash-merge the pull request into `main` while pinning the reviewed head commit so the review and CI gate cannot be raced:
     ```bash
     GH_TOKEN="$(gh auth token --user swackhamer)" gh pr merge --repo swack-tools/spot-github-runners \
       --squash --delete-branch --match-head-commit "$REVIEWED_HEAD_SHA"
     ```
