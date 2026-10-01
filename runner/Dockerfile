FROM ghcr.io/actions/actions-runner:2.337.0
ARG DEBIAN_FRONTEND=noninteractive
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential clang cmake curl git jq libssl-dev make pkg-config python3 python3-pip \
      perl cpanminus libarchive-zip-perl zlib1g-dev libzstd-dev libarchive-dev ca-certificates unzip zip xz-utils \
      zstd \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o runner -g runner /runner-state /work
COPY entrypoint.sh /usr/local/bin/gha-runner-entrypoint
COPY job-completed.sh /usr/local/bin/gha-runner-job-completed.sh
COPY clean_target.py /usr/local/bin/gha-runner-clean-target.py
RUN chmod 0755 /usr/local/bin/gha-runner-entrypoint /usr/local/bin/gha-runner-job-completed.sh
USER runner
WORKDIR /work
ENV RUNNER_STATE=/runner-state
ENV OXIDEX_CLEAN_TARGET=1
ENV ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/bin/gha-runner-job-completed.sh
ENTRYPOINT ["/usr/local/bin/gha-runner-entrypoint"]
