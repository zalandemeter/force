#!/usr/bin/env bash
# Work on Force with nothing installed but Docker: run this checkout in the development image
# (ghcr.io/zalandemeter/force-dev, build/dev/Dockerfile) with Python, pytest, tmux, Rust and the shell tools.
#
#   scripts/devbox.sh                          a shell in the image, in this checkout
#   scripts/devbox.sh python3 -m pytest -q     any command: the tests ...
#   scripts/devbox.sh ./dev.sh                 ... or the sandbox (then: scripts/devbox.sh force-dev)
#   scripts/devbox.sh --build ...              build the image from build/dev/Dockerfile instead of pulling it
#
# The checkout is mounted at /work and the container runs as you, so files it writes are yours. Its home (the
# sandbox's data, cargo's crates and its Rust builds: CARGO_HOME, CARGO_TARGET_DIR) is the Docker volume force-devbox-home:
# host and container builds never mix, and `docker volume rm force-devbox-home` starts over.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${FORCE_DEV_IMAGE:-ghcr.io/zalandemeter/force-dev:latest}"
command -v docker >/dev/null || { echo "devbox: needs Docker (https://docs.docker.com/engine/install/)" >&2; exit 1; }

if [[ "${1:-}" == "--build" ]]; then
  shift
  IMAGE="force-dev:local"
  docker build -q -t "$IMAGE" "$REPO/build/dev" >/dev/null
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker pull -q "$IMAGE" >/dev/null || {
    echo "devbox: cannot pull $IMAGE; building it here instead" >&2
    IMAGE="force-dev:local"
    docker build -q -t "$IMAGE" "$REPO/build/dev" >/dev/null
  }
fi

TTY=(-i); [[ -t 0 && -t 1 ]] && TTY=(-it)
exec docker run --rm "${TTY[@]}" --init \
  --user "$(id -u):$(id -g)" \
  -v "$REPO:/work" -w /work \
  -v force-devbox-home:/home/dev \
  -e USER="$(id -un)" -e LOGNAME="$(id -un)" \
  -e CARGO_HOME=/home/dev/.cargo -e CARGO_TARGET_DIR=/home/dev/target -e FORCE_PY=/usr/bin/python3 \
  -e PATH="/home/dev/.local/bin:/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  "$IMAGE" sh -c '
    # an account for this user id (Force reads its user name, as on any real machine)
    getent passwd "$(id -u)" >/dev/null || echo "$USER:x:$(id -u):$(id -g)::/home/dev:/bin/bash" >> /etc/passwd
    # the crates both Cargo.lock files name, once (the notices test reads them offline); quiet when cached
    for c in crates/sidebar crates/intro; do cargo fetch -q --locked --manifest-path "$c/Cargo.toml" 2>/dev/null; done
    exec "$@"' devbox "${@:-bash}"
