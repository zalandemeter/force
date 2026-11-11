#!/usr/bin/env bash
# Deploy the force working tree to the live cockpit (with FORCE_INSTANCE set: to that development instance,
# SPEC §2.1; ./dev.sh does that).
#
# The live cockpit never runs from this repo: it imports from ~/.local/share/force/app, a symlink to
# an immutable copy under releases/. This script tests the working tree, copies it into a new
# release, checks that the release imports and that its tmux.conf parses, and only then swaps the
# symlink. The running daemon notices the change (dev_reload), re-validates, reloads tmux.conf,
# restarts the sidebars and re-executes itself. Agent panes are never touched.
#
#   ./deploy.sh              test + deploy
#   ./deploy.sh --no-test    deploy without the test suite (still import/parse checks)
#   ./deploy.sh --rollback   switch back to the previous release
#   ./deploy.sh --print-python  print the chosen python venv and exit (changes nothing)
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BASE="$HOME/.local/share/force"
SOCK=force
# FORCE_INSTANCE: deploy into that development instance instead (SPEC §2.1; ./dev.sh sets it)
if [[ -n "${FORCE_INSTANCE:-}" ]]; then
  [[ "$FORCE_INSTANCE" =~ ^[a-z][a-z0-9]{0,15}$ ]] \
    || { echo "deploy: FORCE_INSTANCE must be a short lowercase name (a-z, 0-9)" >&2; exit 2; }
  BASE="$HOME/.local/share/force-$FORCE_INSTANCE"
  SOCK="force-$FORCE_INSTANCE"
fi
REL="$BASE/releases"
APP="$BASE/app"
KEEP=10

# Pick the python venv: $FORCE_PY, else the install.sh venv.
select_python() {
  if [[ -n "${FORCE_PY:-}" ]]; then
    printf '%s\n' "$FORCE_PY"
    return 0
  fi
  if [[ -x "$HOME/.local/share/force/venv/bin/python" ]]; then
    printf '%s\n' "$HOME/.local/share/force/venv/bin/python"
    return 0
  fi
  echo "deploy: no Python venv found, run ./install.sh first" >&2
  return 1
}

# The Rust toolchain: ~/.cargo/bin/cargo, else cargo on PATH.
select_cargo() {
  if [[ -x "$HOME/.cargo/bin/cargo" ]]; then
    printf '%s\n' "$HOME/.cargo/bin/cargo"
    return 0
  fi
  if command -v cargo >/dev/null 2>&1; then
    printf '%s\n' "$(command -v cargo)"
    return 0
  fi
  echo "deploy: cargo not found, run ./install.sh first" >&2
  return 1
}

PY="$(select_python)"

if [[ "${1:-}" == "--print-python" ]]; then
  printf '%s\n' "$PY"
  exit 0
fi

mkdir -p "$REL"

if [[ "${1:-}" == "--rollback" ]]; then
  cur="$(readlink "$APP" || true)"
  prev="$(ls -1d "$REL"/*/ 2>/dev/null | sed 's:/$::' | sort | grep -vxF "$cur" | tail -1)"
  [[ -n "$prev" ]] || { echo "deploy: no previous release" >&2; exit 1; }
  ln -sfn "$prev" "$APP.tmp" && mv -T "$APP.tmp" "$APP"
  echo "deploy: rolled back to $(basename "$prev")"
  exit 0
fi

CARGO="$(select_cargo)"
# where cargo puts the binaries: CARGO_TARGET_DIR when set (scripts/devbox.sh), else each crate's target/
SIDEBAR_TARGET="${CARGO_TARGET_DIR:-$REPO/crates/sidebar/target}"
INTRO_TARGET="${CARGO_TARGET_DIR:-$REPO/crates/intro/target}"

if [[ "${1:-}" != "--no-test" ]]; then
  if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
    echo "deploy: pytest not installed, skipping tests (pip install pytest into the venv)" >&2
  else
    echo "deploy: running tests"
    (cd "$REPO" && bt="$(mktemp -d)" && PYTHONPATH="$REPO" nice -n 10 "$PY" -m pytest -q -p no:cacheprovider --basetemp="$bt"; rc=$?; rm -rf "$bt"; exit $rc)
  fi
fi

id="$(date +%Y%m%d-%H%M%S)-$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo nogit)"
[[ -z "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]] || id="$id-dirty"
dest="$REL/$id"
mkdir -p "$dest"
cp -a "$REPO/force" "$REPO/share" "$REPO/systemd" "$REPO/docs/SPEC.md" "$REPO/README.md" "$REPO/install.sh" \
  "$REPO/LICENSE" "$REPO/THIRD-PARTY-NOTICES.md" "$dest/"
find "$dest" -name '__pycache__' -prune -exec rm -rf {} +
# Chuck's context pack (SPEC §50): the docs, the command reference and CONTRIBUTING, made from this tree
"$PY" "$REPO/scripts/support-pack.py" --out "$dest/share/support" \
  || { echo "deploy: Chuck's context pack does not build, not deployed" >&2; rm -rf "$dest"; exit 1; }

# The Rust sidebar (SPEC §22): built here, shipped as share/bin/force-sidebar in the release.
if [[ -f "$REPO/crates/sidebar/Cargo.toml" ]]; then
  echo "deploy: building the Rust sidebar"
  (cd "$REPO/crates/sidebar" && nice -n 10 "$CARGO" build --release -q) \
    || { echo "deploy: Rust sidebar does not build, not deployed" >&2; rm -rf "$dest"; exit 1; }
  mkdir -p "$dest/share/bin"
  cp "$SIDEBAR_TARGET/release/force-sidebar" "$dest/share/bin/force-sidebar"
  for b in force-header force-dialog; do
    if [[ -x "$SIDEBAR_TARGET/release/$b" ]]; then
      cp "$SIDEBAR_TARGET/release/$b" "$dest/share/bin/$b"
    fi
  done
fi

# The startup intro (SPEC §31): built from crates/intro/ and shipped as share/bin/force-intro.
if [[ -f "$REPO/crates/intro/Cargo.toml" ]]; then
  echo "deploy: building the startup intro"
  (cd "$REPO/crates/intro" && nice -n 10 "$CARGO" build --release -q) \
    || { echo "deploy: intro does not build, not deployed" >&2; rm -rf "$dest"; exit 1; }
  mkdir -p "$dest/share/bin"
  cp "$INTRO_TARGET/release/force-intro" "$dest/share/bin/force-intro"
fi

# Force's own static tmux (SPEC §46), when scripts/build-release.sh has built it here: the release then runs
# the same tmux as a user install (without it, the system tmux)
if [[ -x "$REPO/build/tmux-bin/tmux" ]]; then
  mkdir -p "$dest/share/tmux-bin"
  cp "$REPO/build/tmux-bin/tmux" "$dest/share/tmux-bin/tmux"
fi

echo "deploy: checking release $id"
(cd /tmp && PYTHONPATH="$dest" "$PY" -c "import importlib, pkgutil, force
for m in pkgutil.iter_modules(force.__path__): importlib.import_module('force.' + m.name)") \
  || { echo "deploy: release does not import, not deployed" >&2; rm -rf "$dest"; exit 1; }
if tmux -L "$SOCK" has-session 2>/dev/null; then
  tmux -L "$SOCK" source-file -n "$dest/share/tmux.conf" \
    || { echo "deploy: tmux.conf does not parse, not deployed" >&2; rm -rf "$dest"; exit 1; }
  tmux -L "$SOCK" source-file -n "$dest/share/tmux-ui.conf" \
    || { echo "deploy: tmux-ui.conf does not parse, not deployed" >&2; rm -rf "$dest"; exit 1; }
fi

ln -sfn "$dest" "$APP.tmp" && mv -T "$APP.tmp" "$APP"
echo "deploy: ${FORCE_INSTANCE:-live} -> $id (the daemon reloads within a few seconds)"

# keep the newest $KEEP releases, never the live one
ls -1d "$REL"/*/ | sed 's:/$::' | sort | head -n -"$KEEP" | while read -r old; do
  [[ "$old" == "$(readlink "$APP")" ]] || rm -rf "$old"
done
