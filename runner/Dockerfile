FROM ghcr.io/actions/actions-runner:2.337.0
ARG DEBIAN_FRONTEND=noninteractive
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential clang cmake curl git jq libssl-dev make pkg-config python3 python3-pip \
      perl cpanminus libarchive-zip-perl zlib1g-dev libzstd-dev libarchive-dev ca-certificates unzip zip xz-utils \
      docker.io zstd \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o runner -g runner /runner-state /work
COPY entrypoint.sh /usr/local/bin/gha-runner-entrypoint
RUN chmod 0755 /usr/local/bin/gha-runner-entrypoint
USER runner
WORKDIR /work
ENV RUNNER_STATE=/runner-state
ENTRYPOINT ["/usr/local/bin/gha-runner-entrypoint"]
