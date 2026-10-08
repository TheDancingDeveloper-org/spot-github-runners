FROM ghcr.io/actions/actions-runner:2.337.0
ARG DEBIAN_FRONTEND=noninteractive
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential clang cmake curl git jq libssl-dev make pkg-config python3 python3-pip \
      perl cpanminus libarchive-zip-perl zlib1g-dev libzstd-dev libarchive-dev ca-certificates unzip zip xz-utils \
      zstd \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o runner -g runner /runner-state /work /opt/runner-rust/cargo /opt/runner-rust/rustup \
    && install -m 0444 /dev/null /run/oxidex-spot-runner
COPY entrypoint.sh /usr/local/bin/gha-runner-entrypoint
COPY job-completed.sh /usr/local/bin/gha-runner-job-completed.sh
COPY job-started.sh /usr/local/bin/gha-runner-job-started.sh
COPY host_admission.py /usr/local/bin/gha-host-admission.py
COPY clean_target.py /usr/local/bin/gha-runner-clean-target.py
RUN chmod 0755 /usr/local/bin/gha-runner-job-started.sh /usr/local/bin/gha-runner-entrypoint /usr/local/bin/gha-runner-job-completed.sh
USER runner
ENV CARGO_HOME=/opt/runner-rust/cargo
ENV RUSTUP_HOME=/opt/runner-rust/rustup
ENV PATH=/opt/runner-rust/cargo/bin:${PATH}
RUN curl --proto '=https' --tlsv1.2 -fsS https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.99.0 \
    && rm /tmp/rustup-init.sh \
    && rustup component add rustfmt clippy --toolchain 1.99.0 \
    && rustup target add --toolchain 1.99.0 x86_64-unknown-linux-musl aarch64-unknown-linux-musl aarch64-apple-darwin x86_64-apple-darwin \
    && rustc --version | grep '^rustc 1.99.0 ' \
    && cargo --version | grep '^cargo 1.99.0 '
USER root
RUN chown -R root:root /opt/runner-rust \
    && chmod -R a-w /opt/runner-rust
USER runner
ENV CARGO_HOME=/work/.cargo
ENV PATH=/opt/runner-rust/cargo/bin:/work/.cargo/bin:${PATH}
RUN test ! -w /opt/runner-rust/cargo/bin/rustup \
    && test ! -w /opt/runner-rust/rustup
WORKDIR /work
ENV RUNNER_STATE=/runner-state
ENV OXIDEX_CLEAN_TARGET=1
ENV ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/bin/gha-runner-job-started.sh
ENV ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/bin/gha-runner-job-completed.sh
ENTRYPOINT ["/usr/local/bin/gha-runner-entrypoint"]
