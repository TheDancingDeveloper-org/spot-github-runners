# Local Docker runners

The `m4air/` directory configures two persistent ARM64 runners on a local Apple silicon host that runs
OrbStack. These runners are separate from the GCP fleet. They don't count toward any fleet target, and the
autoscaler never provisions, drains, or retires them.

Use them for native ARM64 Linux Docker work. Route native macOS work to native hosts instead.

## What the files define

| File | Purpose |
| --- | --- |
| `m4air/Dockerfile` | An Ubuntu 24.04 image with the GitHub Actions runner (a pinned version and SHA-256 for `linux-arm64`), the Docker command-line tool, Buildx, and Compose from a pinned Docker image, Rust 1.97.1 with rustfmt, and Perl and build dependencies. |
| `m4air/docker-compose.yml` | Two services, `runner-1` and `runner-2`, with a named state volume each. |
| `m4air/runner-entrypoint.sh` | Registers a runner on first start, then runs the listener. |

Each runner registers at the organization level with the runner group `oxidex-m4air` and the custom labels
`oxidex-m4air` and `docker`. GitHub also adds the built-in `self-hosted`, `Linux`, and `ARM64` labels.

## Trust boundary

Compose mounts `/var/run/docker.sock` from the OrbStack Linux host into each runner container. A job on these
runners can therefore control that Docker daemon. This differs from the GCP runner hosts and the dedicated
builders, which never pass the Docker socket into job containers. Keep untrusted pull requests off these
runners. See the [security model](security.md).

The supplementary group in the container must match the socket's numeric group ID. OrbStack currently uses
group `0`. Set `DOCKER_GID` if your host differs.

## Update an existing deployment

Copy `Dockerfile`, `docker-compose.yml`, `runner-entrypoint.sh`, and `.dockerignore` to the working directory of
the deployment on the host. When both runners are idle, run these commands in that directory:

```bash
docker compose build runner-1
docker compose up -d --no-build
docker exec oxidex-gha-runner-1 docker version
docker exec oxidex-gha-runner-2 docker version
```

Compose preserves the named volumes that hold the runner registrations and workspaces. Keep the existing Compose
project name and volume names. Existing registrations don't need another registration token.

## Register a new runner

The entrypoint configures a runner only when its state volume has no `.runner` file. To register a new runner:

1. Put a registration token in a private `.env` file as `RUNNER_TOKEN`.
1. Mount that file read-only at `/run/secrets/runner.env` for the first start. Without it, the container exits
   with the message that the runner isn't configured.
1. Remove the mount before you start jobs. The entrypoint unsets `RUNNER_TOKEN` before it starts the listener.
   Pass `--register-only` to register and exit.

Never commit that file or any `.env` file. Changing `RUNNER_LABELS` affects only new registrations. Update the
labels of runners that are already registered through the GitHub API.

## Verification record

The repository README for this directory records a check on September 30, 2026. Both containers reached the
Docker server as the `runner` user and built an image with `docker buildx build --load` on both the default
and container drivers. That check is a historical observation of one host. Repeat it after any change.
