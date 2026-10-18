# Control VM watchdog image: the Cloud Run service whose POST /tick runs one control VM lifecycle step.
#
# Built and pushed to Artifact Registry by .github/workflows/control-watchdog-image.yml. Build from the
# repository root (the .dockerignore allowlist sends only src/, cloud-init/, and the other image inputs):
#   docker build -f watchdog/Dockerfile --build-arg BASE_IMAGE=<google-cloud-cli slim image pinned by digest> .
#
# BASE_IMAGE has no default on purpose: the build fails unless it names an exact digest of
# gcr.io/google.com/cloudsdktool/google-cloud-cli:<version>-slim, which supplies gcloud. The workflow
# reads it from the WATCHDOG_BASE_IMAGE repository variable and refuses anything not pinned by digest.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/watchdog \
    CLOUDSDK_CONFIG=/home/watchdog/.config/gcloud \
    CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
    CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=true

WORKDIR /app

# control_vm.py renders cloud-init/control-vm-startup.sh into every control VM it creates, so the image carries it.
COPY src/ /app/src/
COPY cloud-init/control-vm-startup.sh /app/cloud-init/control-vm-startup.sh

# The control plane needs a system Python 3.11 or newer (newer gcloud releases bundle their own Python and may not
# put python3 on PATH). Root-owned and world-readable, never writable by the runtime user; gcloud keeps its config
# in HOME.
RUN if ! command -v python3 >/dev/null 2>&1; then \
        apt-get update \
        && apt-get install -y --no-install-recommends python3 ca-certificates \
        && rm -rf /var/lib/apt/lists/*; \
    fi \
    && python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 'python3 is older than 3.11')" \
    && chown -R root:root /app \
    && chmod -R u=rwX,go=rX /app \
    && groupadd --system --gid 10001 watchdog \
    && useradd --system --uid 10001 --gid 10001 --home-dir /home/watchdog --create-home --shell /usr/sbin/nologin watchdog \
    && python3 -B -c "import lib.control_vm, lib.gcs_control_state, lib.image_tracking, lib.control_watchdog_server" \
    && gcloud --version >/dev/null

USER 10001:10001

# Listens on $PORT (Cloud Run sets it). For a one-off step: --entrypoint python3 <image> -B /app/src/control_vm.py ...
ENV PORT=8080
# A writable gcloud config directory; gcloud's warning about an unwritable one breaks inventory parsing.
ENV CLOUDSDK_CONFIG=/tmp/gcloud-config
EXPOSE 8080
ENTRYPOINT ["python3", "-B", "/app/src/control_watchdog_server.py"]
