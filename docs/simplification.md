# Simplify remote builds

## User workflow

The intended interface is `just build` for a remote build and
`just build-local` for an explicit local build. Ordinary build and test recipes
should use the same configured remote builder. OxiDex owns those recipes; this repository
provides the VM startup payload and restricted host launcher.

## Setup workflow

1. Select an affordable Spot offer that meets the workload's CPU and memory
   requirements. Include the boot disk and persistent cache disk in the price.
2. Fill one builder configuration with the project, zone, instance name,
   storage settings, and public uploader identity.
3. Render the startup payload and run its generated launch command once.
4. Verify startup completed, then put the authenticated builder endpoint and
   actual image identity in OxiDex's remote configuration.
5. Run `just build` and verify that the remote build exports its artifact.

See [Dedicated builders](dedicated-builders.md) for the current configuration
schema and exact render command. The generated command creates the VM with its
startup payload. Installing dependencies or copying a repository manually over
SSH is not part of the normal setup.

## Keep one control path

Use the existing restricted launcher for source preparation, execution,
artifact export, and owned source cleanup. Keep Cargo downloads and each
worktree's target directory on the persistent cache disk. Keep resource
admission on the host, where it can account for actual active builds.

Start with one explicitly configured builder. Do not require a second queue,
claim-token service, registry, or fleet controller to use that builder. Add
multi-builder selection only when measured demand requires it and the normal
single-builder path remains usable.

Keep GitHub runner scheduling separate from dedicated builder scheduling.
Draining a builder denies new builds and waits for its actual builds and locks
to finish. Registered GitHub runner counts do not describe builder capacity.

## Work still required

This document describes the simplification target, not a completed rollout.
Verify the current OxiDex configuration and recipe implementation before
claiming all commands use this path. Make the configuration and launch inputs
easier to create, remove duplicate client paths, and validate a fresh launch
and ordinary remote build. Preserve active workloads and caches during changes.
