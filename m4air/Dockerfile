FROM docker:29-cli@sha256:018edbc908e08fcc9dbf029c812c34251e9b4719e6f71ca0e5eae2a987d014ca AS docker-cli
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG RUNNER_VERSION=2.337.0
ARG RUNNER_SHA256=9b1dc70626422526e3c94767cf024896beb15da5342a3f4819bf2feac13e0393

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git jq tar gzip zstd unzip sudo \
       perl cpanminus libarchive-zip-perl \
       python3 python3-venv \
       build-essential pkg-config libssl-dev libzstd-dev \
       libicu74 libkrb5-3 liblttng-ust1 libcurl4 libunwind8 libuuid1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash runner \
    && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner

RUN install -d -o runner -g runner /runner-state /home/runner/.cargo \
    && curl --fail --silent --show-error --location \
       "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz" \
       --output /tmp/actions-runner.tar.gz \
    && echo "${RUNNER_SHA256}  /tmp/actions-runner.tar.gz" | sha256sum --check --status \
    && mkdir -p /opt/actions-runner \
    && tar -xzf /tmp/actions-runner.tar.gz -C /opt/actions-runner \
    && cd /opt/actions-runner \
    && ./bin/installdependencies.sh \
    && rm -f /tmp/actions-runner.tar.gz

USER runner
ENV HOME=/home/runner \
    PATH=/home/runner/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    RUNNER_HOME=/runner-state \
    RUNNER_WORKDIR=/runner-state/_work

RUN curl --proto '=https' --tlsv1.2 --fail --silent --show-error https://sh.rustup.rs \
      | sh -s -- -y --default-toolchain 1.97.1 --profile minimal \
    && rustup component add rustfmt --toolchain 1.97.1 \
    && rustc --version --verbose \
    && cargo --version

COPY --chown=runner:runner runner-entrypoint.sh /usr/local/bin/runner-entrypoint.sh
RUN chmod 0755 /usr/local/bin/runner-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/runner-entrypoint.sh"]

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
