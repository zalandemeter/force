# The Force development image (ghcr.io/zalandemeter/force-dev): everything needed to test, build and try Force
# from a checkout, so a contributor (or Chuck's fix mode) needs only Docker. scripts/devbox.sh runs the checkout
# in it; .devcontainer/ uses it for VS Code and Codespaces. Built and published by .github/workflows/dev-image.yml.
# (The release binaries are built elsewhere: build/Dockerfile, static musl.)
FROM ubuntu:24.04

ARG RUST_VERSION=1.99.0
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

# Python 3.12 with pytest (Force itself needs no packages), tmux 3.4 (the version Force ships), the shell tools
# the scripts and tests use, and git
RUN apt-get update -q \
 && apt-get install -yq --no-install-recommends \
      python3 python3-pytest tmux git curl ca-certificates bash shellcheck procps less locales fontconfig \
      build-essential pkg-config \
 && rm -rf /var/lib/apt/lists/*

# Rust, pinned; the cargo home is writable so any user id can fetch crates
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --no-modify-path --profile minimal --default-toolchain "$RUST_VERSION" \
 && chmod -R a+rwX "$CARGO_HOME" "$RUSTUP_HOME" \
 && rustc --version && cargo --version

# a home for whatever user id runs the container (scripts/devbox.sh mounts a volume over it), and an account
# for it: scripts/devbox.sh adds the caller's line to /etc/passwd at start, as every real machine has one
RUN mkdir -p /home/dev && chmod 1777 /home/dev && chmod 666 /etc/passwd
ENV HOME=/home/dev
WORKDIR /work
CMD ["bash"]
