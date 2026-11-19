#!/bin/bash
# Runs inside force-builder (build/Dockerfile): static Rust binaries + tmux + terminfo + licences into /out.
# /src = the repo (read-only), /cache = cargo target dirs and registry (kept between builds).
set -euo pipefail
export CARGO_HOME_CACHE=/cache/cargo-home
mkdir -p /out/bin /out/terminfo /out/licenses "$CARGO_HOME_CACHE"
cp -r /opt/cargo/bin "$CARGO_HOME_CACHE/" 2>/dev/null || true
export CARGO_HOME="$CARGO_HOME_CACHE" PATH="$CARGO_HOME_CACHE/bin:/opt/cargo/bin:$PATH"
for crate in sidebar intro link; do
  CARGO_TARGET_DIR="/cache/target-$crate" cargo build --release --locked -q --manifest-path "/src/crates/$crate/Cargo.toml"
done
for b in force-sidebar force-header force-dialog; do cp "/cache/target-sidebar/release/$b" /out/bin/; done
cp /cache/target-intro/release/force-intro /out/bin/
cp /cache/target-link/release/force-link /out/bin/
cp /opt/force-tmux/bin/tmux /out/bin/tmux
strip /out/bin/force-* || true
# terminfo for what Force's tmux tells programs inside it (TERM=tmux-256color): missing on minimal systems
/opt/deps/bin/tic -x -o /out/terminfo -e tmux,tmux-256color /tmp/b/ncurses-*/misc/terminfo.src
cp /tmp/b/tmux-*/COPYING /out/licenses/tmux.txt
cp /tmp/b/libevent-*/LICENSE /out/licenses/libevent.txt
cp /tmp/b/ncurses-*/COPYING /out/licenses/ncurses.txt
for f in /out/bin/*; do file "$f" 2>/dev/null | grep -q "statically linked\|static-pie" || { echo "inside: $f is not static" >&2; exit 1; }; done
# the direct link (SPEC §56.2; iroh with ring, built against musl here) runs
/out/bin/force-link version >/dev/null || { echo "inside: force-link does not run" >&2; exit 1; }
