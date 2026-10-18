# Development

This page covers how to change the repository: the environment, tests, documentation checks, the embedded
asset rules, and the review and merge workflow. For safety rules that apply to a live fleet, see
[Operations and troubleshooting](operations.md).

## Environment

- The control-plane scripts target Python 3.11 or later on macOS and Linux and use only the standard library.
  Don't add a third-party runtime dependency without explicit approval.
- The documentation build is the only exception. `docs/requirements.txt` pins MkDocs and its theme for the
  site build. Nothing in `src/`, `tools/`, or the Docker images imports them.
- Keep secrets, `.env`, SSH private keys, approved uploader JSON files, rendered launch directories, and
  operational state outside the Git checkout when you can, for example under `$HOME/oxidex-ops`.
- When you add or rename an environment variable, document it in `.env.example` and in the
  [configuration reference](configuration.md).

## Run the tests

Run focused tests while you iterate, and run the full discovery suite before you commit:

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

Run Cargo compilation and the full Python suite on an eligible remote Spot worker through the reviewed build
route. Local work is limited to edits, Git, formatting, bounded metadata and control checks, and prose lint.

The reviewed remote proof runs the complete signed-source suite inside the admitted builder container. Set
`OXIDEX_CONTROL_REPO` to a reviewed OxiDex checkout that has the `test-infra-ci-remote` recipe, then run:

```bash
just test
```

The fixed CI-only profile verifies module coverage, discovered and executed test identities, actual test
results, and artifact hashes. There is no local full-suite fallback for `just test`. Don't claim that
`just test` ran if you ran only local `unittest` discovery. Local tests, remote proof runs, GitHub checks,
merged commits, rendered startup payloads, and live observations each prove something different, so state the
exact command and environment for every verification claim.

### Remote proof contracts

- `tools/infra_python_complete_proof.py` pins the SHA-256 of `tools/infra_python_test_proof.py` in
  `PRODUCER_SHA`. After a reviewed change to the producer, update `PRODUCER_SHA` and run
  `tests/test_infra_python_test_proof.py`. That test checks the producer's behavior, not the pin: only
  `tools/infra_python_complete_proof.py` itself, run through `just test` or
  `just infra-python-complete-tests` in the admitted builder, detects a stale `PRODUCER_SHA`.
- `tools/infra_python_test_proof.py` enforces minimum counts for test cases (`MIN_CASES`, 854) and test
  modules (`MIN_MODULES`, 82). The complete proof replaces those floors with `max(774, module count)` cases
  and `max(76, module count)` modules. The producer also checks that every `tests/test_*.py` module matches
  its digest in the manifest, with no duplicate test identities. Don't delete or merge test modules below those floors without a reviewed contract
  update.

## Keep embedded assets in sync

Some modules embed scripts that tests compare byte for byte with the files on disk:

| Module | Embeds or reads |
| --- | --- |
| `src/lib/runner_assets.py` | `runner/clean_target.py`, `runner/docker-wrapper.py`, `runner/job-completed.sh`, `runner/Dockerfile`, `runner/entrypoint.sh`, and `cloud-init/startup.sh`. Update the constant in the same commit as the file. |
| `src/lib/builder_assets.py` | The legacy shared-host builder files: `builder/Dockerfile`, `builder/entrypoint.py`, and `builder/host-launcher.py`. |
| `src/lib/builder_only_assets.py` | Dedicated builder files from `builder/standalone/`, `builder/dedicated/`, and `cloud-init/builder-startup.sh`. Keep the file lists, permissions, and runtime-update target manifests in `src/lib/builder_runtime_update.py` aligned. |

## Write and check documentation

Documentation follows the [Google developer documentation style guide](https://developers.google.com/style):
sentence-case headings, active voice, direct instructions, and explicit prerequisites. Preserve command names
and technical meaning when you fix prose. Check command examples against `--help` output and the source.

Install Vale from its [installation guide](https://docs.vale.sh/topics/installation). Then synchronize the
configured Google rules and lint every documentation file:

```bash
vale sync
vale README.md AGENTS.md CLAUDE.md docs/*.md
```

A clean Vale result covers the configured rules, not every editorial requirement of the style guide.

When you add a Markdown file:

1. Register it in `.vale.ini` under `BasedOnStyles = Google`.
1. Add it to the `nav` section of `mkdocs.yml`. The site build runs in strict mode, so an unlisted page, a
   broken link, or a missing anchor fails the build.
1. Allowlist any new root file or asset path in `.gitignore`. The file starts with `*`, so Git ignores anything
   that isn't un-ignored with a `!` entry. `docs/*.md` and `docs/requirements.txt` are already allowed.

### Build the site locally

Create a virtual environment outside the checkout, install the pinned build dependencies, and build in strict
mode:

```bash
python3 -m venv "$HOME/oxidex-ops/docs-venv"
"$HOME/oxidex-ops/docs-venv/bin/pip" install -r docs/requirements.txt
"$HOME/oxidex-ops/docs-venv/bin/mkdocs" build --strict --site-dir "$HOME/oxidex-ops/docs-site"
```

`mkdocs serve` previews the site at `http://127.0.0.1:8000`. The `site/` directory is ignored by Git. The
**Documentation** workflow (`.github/workflows/docs.yml`) runs the same strict build on pull requests that
change the documentation. On pushes to `main` that change the documentation it also deploys the site to GitHub Pages through the
`github-pages` environment. In the repository settings, set **Pages** > **Source** to **GitHub Actions** once.

!!! warning "The site is public"
    A GitHub Pages site can be readable by anyone, even when the repository is private. Describing the
    architecture, including the edge and tunnel design, is fine. Don't put deployment identifiers in the
    documentation: project, account, namespace, runner group, or tunnel IDs, internal hostnames, tokens, or
    secret values. Resource names fixed in the code, such as the `spot-control-a` and `spot-control-b` slots,
    may appear. Use placeholders such as `YOUR_PROJECT`.

## Git and review workflow

A task prompt authorizes you to work on an isolated feature branch, commit the task changes, push the branch,
open a pull request against `main`, resolve review comments, and squash-merge once every review and CI gate
passes. It doesn't authorize force-pushing, changing branch protections, or changing live cloud resources.

### Branch, identity, and accounts

- Develop on a dedicated branch or an isolated Git worktree based on the latest `origin/main`. Never commit or
  push directly to `main`.
- Author and SSH-sign commits as `swackhamer` (`user.name=swackhamer`,
  `user.email=swackhamer@users.noreply.github.com`, `commit.gpgsign=true`). Verify the local signature with
  `git cat-file commit HEAD`, and verify GitHub's verification status on the pushed commits.
- When several `gh` accounts are logged in, export the `swackhamer` token for the shell session so that no
  command falls back to another stored account:

  ```bash
  export GH_TOKEN="$(gh auth token --user swackhamer)"
  gh pr status --repo swack-tools/spot-github-runners
  ```

- Stage files by explicit path with `git add <path>`, so scratch files, local `.vale/styles/`, and unrelated
  edits never reach a commit.

### Review loop

1. Run the full unit test suite and Vale on every changed documentation file.
1. Get an independent local review of the full diff against `origin/main`, and resolve its findings before you
   open the pull request.
1. Push the branch and open a pull request that targets `main`. Request an independent review on the pull
   request.
1. Check each review comment against the code. Fix every valid issue, rerun the tests and Vale, push a signed
   commit, reply with the fix and the verification evidence, and resolve the thread.
1. Request a fresh review on the new head commit. Repeat until the reviewer reports no remaining issues, every
   thread is resolved, and the required GitHub checks pass.
1. Confirm that `main` hasn't advanced in a conflicting way and that the reviewed head commit matches the pull
   request head. Then squash-merge, pinning the reviewed commit so that the review and CI gate can't be raced:

   ```bash
   gh pr merge --repo swack-tools/spot-github-runners \
     --squash --delete-branch --match-head-commit "$REVIEWED_HEAD_SHA"
   ```
