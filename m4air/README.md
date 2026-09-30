# Docker runners

These files configure two persistent ARM64 runners on the M4 Air host with
OrbStack. Each runner has the `self-hosted`, `Linux`, `ARM64`, `oxidex-m4air`,
and `docker` labels. These runners do not count toward the GCP fleet target.

The image includes the Docker command-line tool, Buildx, and Compose from a pinned Docker
image. Compose mounts `/var/run/docker.sock` from the OrbStack Linux host into
each container. The supplementary group must match the socket's numeric group
ID. OrbStack currently uses group `0`; set `DOCKER_GID` if your host differs.

To update the existing deployment, copy `Dockerfile`, `docker-compose.yml`,
`runner-entrypoint.sh`, and `.dockerignore` to `/Users/allen/gha` on the M4 Air.
When both runners are idle, run these commands in that directory:

```sh
docker compose build runner-1
docker compose up -d --no-build
docker exec oxidex-gha-runner-1 docker version
docker exec oxidex-gha-runner-2 docker version
```

Compose preserves the named volumes that contain the runner registrations and
workspaces. Keep the existing Compose project name and volume names. Existing
registrations do not need another registration token. For new registrations,
provide the token through a temporary read-only `/run/secrets/runner.env` mount,
then remove the mount before starting jobs. Never commit that file or `.env`.

Changing `RUNNER_LABELS` affects new registrations. Update labels through the
GitHub API for runners that are already registered.

Verification on September 30, 2026 confirmed that both containers could access
the Docker server as the `runner` user and build an image with `docker buildx
build --load` using both the default and container drivers. The Docker client reported version 29.8.1, and the OrbStack
server reported version 29.4.0.
