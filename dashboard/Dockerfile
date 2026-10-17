# Spot dashboard image for the control VM.
#
# Build from the repository root:
#   docker build -f dashboard/Dockerfile -t spot-dashboard .
# Run (on the control VM; cloud-init does this):
#   docker run -d --name spot-dashboard --restart unless-stopped --network host \
#     --env-file /etc/spot-dashboard/dashboard.env -v /var/lib/spot-dashboard:/state <image> \
#     --host 127.0.0.1 --port 8787 --state-file /state/state.json --control-slot <slot>
#
# The dashboard is standard-library Python. It optionally shells out to gcloud (workstation
# credentials; on GCE the GCP token comes from the metadata server instead), ruby (YAML checks), and
# bash (shell syntax checks). All degrade gracefully when absent, so the image ships only Python,
# bash (from the base image), and CA certificates.
FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 dashboard \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin dashboard \
    && install -d -m 0750 -o 10001 -g 10001 /state

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime code plus the tracked deployment assets that some src/lib modules read from disk by path
# relative to the repository root (builder_only_assets, runner_manager).
COPY --chown=root:root src/ /app/src/
COPY --chown=root:root cloud-init/ /app/cloud-init/
COPY --chown=root:root runner/ /app/runner/
COPY --chown=root:root builder/ /app/builder/

USER 10001:10001

VOLUME ["/state"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python3", "-c", "import sys, urllib.request, urllib.error\ntry:\n    urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=4)\nexcept urllib.error.HTTPError as e:\n    sys.exit(0 if e.code == 503 else 1)\nexcept Exception:\n    sys.exit(1)"]

ENTRYPOINT ["python3", "-B", "/app/src/web_dashboard.py"]
