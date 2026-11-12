#!/bin/sh
# Install Force, the code cockpit, for the current user. No root, no packages.
#
#   curl -fsSL @BASE_URL@/install.sh | sh
#   curl -fsSL @BASE_URL@/install.sh | sh -s -- --no-fonts
#
# Downloads Force @VERSION@ for this machine, checks its SHA-256 (pinned below), unpacks it into
# ~/.local/share/force, and runs its setup: the `force` command in ~/.local/bin, the background services,
# Force's own Python if this system's is older than 3.11, and the icon font on a desktop.
# Options (after `sh -s --`): --no-fonts  --no-services  --version  --help
set -eu

VERSION="@VERSION@"
BASE_URL="${FORCE_DOWNLOAD_BASE:-@BASE_URL@}"
if [ -n "${FORCE_RELEASES_URL:-}" ]; then
  RELEASES_URL="$FORCE_RELEASES_URL"
elif [ -n "${FORCE_DOWNLOAD_BASE:-}" ]; then
  RELEASES_URL="$FORCE_DOWNLOAD_BASE/releases"
else
  RELEASES_URL="@RELEASES_URL@"
fi
SHA_x86_64="@SHA_x86_64@"
SHA_aarch64="@SHA_aarch64@"
SHA_darwin_arm64="@SHA_darwin_arm64@"
# Python for systems without 3.11+ (python-build-standalone 20261013, CPython 3.12.15, pinned)
PY_TAG="20261013"
PY_VER="3.12.15"
PY_SHA_x86_64="178d3d83b6e8e99285a65d3fce9c16b87952cc5efdb4d1a0e3ff3be18ff0fc9c"
PY_SHA_aarch64="9467a99bacc7d90b64b02de7fa9ecfc99b72905e64e74f7ff6b460ad95cfed5a"
PY_SHA_darwin_arm64="9866e11cf2ae9caa1e671383786f248859a52dd8bfb702fb4c0dc2c65f1d36de"

DATA="${FORCE_HOME:-$HOME/.local/share/force}"
export FORCE_HOME="$DATA"

say()  { printf '\033[1;38;2;217;119;87mforce\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mforce: %s\033[0m\n' "$*" >&2; exit 1; }

SETUP_ARGS=""
for a in "$@"; do
  case "$a" in
    --no-fonts|--no-services) SETUP_ARGS="$SETUP_ARGS $a" ;;
    --version) echo "$VERSION"; exit 0 ;;
    -h|--help)
      # with `curl | sh` $0 is `sh`, so we cannot read our own header: use a heredoc.
      cat <<'HELP'
Usage: curl -fsSL <base>/install.sh | sh [-s -- OPTIONS]

Installs Force @VERSION@ for this user: downloads and verifies the release, unpacks it into
~/.local/share/force, and runs its setup -- the `force` command in ~/.local/bin, the background
services, Force's own Python if this system's is older than 3.11, and the icon font on a desktop.

Options (after `sh -s --`):
  --no-fonts       skip installing the icon font
  --no-services    do not create or start the background services (the `force` command still works)
  --version        print the version and exit
  --help          this help
HELP
      exit 0 ;;
    *) fail "unknown option: $a" ;;
  esac
done

[ "$(id -u)" != 0 ] || fail "install Force as your own user, not as root (it installs into your home)"
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Linux)
    case "$ARCH" in
      x86_64|amd64) ARCH=x86_64; SHA="$SHA_x86_64"; PY_SHA="$PY_SHA_x86_64" ;;
      aarch64|arm64) ARCH=aarch64; SHA="$SHA_aarch64"; PY_SHA="$PY_SHA_aarch64" ;;
      *) fail "no Force build for $ARCH yet (x86_64 and aarch64 only)" ;;
    esac
    PLATFORM="linux-$ARCH"; PY_TRIPLE="$ARCH-unknown-linux-gnu" ;;
  Darwin)
    # macOS on Apple Silicon (SPEC §51); an Intel Mac reports x86_64 (also under Rosetta: check the hardware)
    if [ "$ARCH" != arm64 ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" != 1 ]; then
      fail "Force on macOS needs Apple Silicon (this Mac is Intel)"
    fi
    ARCH=arm64; SHA="$SHA_darwin_arm64"; PY_SHA="$PY_SHA_darwin_arm64"
    PLATFORM="darwin-arm64"; PY_TRIPLE="aarch64-apple-darwin" ;;
  *) fail "Force runs on Linux and macOS (this is $OS)" ;;
esac
case "$SHA" in ""|@*) fail "no Force $VERSION build for $PLATFORM yet" ;; esac

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL --retry 2 -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -q -O "$2" "$1"; }
else
  fail "needs curl or wget"
fi
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else fail "needs sha256sum"; fi
}
command -v tar >/dev/null 2>&1 || fail "needs tar"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

# 1. the release
NAME="force-$VERSION-$PLATFORM.tar.gz"
say "downloading Force $VERSION ($ARCH)"
fetch "$RELEASES_URL/$NAME" "$TMP/$NAME" || fail "download failed: $RELEASES_URL/$NAME"
[ "$(sha256 "$TMP/$NAME")" = "$SHA" ] || fail "checksum mismatch for $NAME: not installing it"
mkdir -p "$DATA/releases"
rm -rf "$TMP/x" && mkdir "$TMP/x" && tar -xzf "$TMP/$NAME" -C "$TMP/x"
REL="$DATA/releases/$VERSION"
if [ ! -d "$REL" ]; then
  mv "$TMP/x/force-$VERSION" "$REL.tmp.$$" && mv "$REL.tmp.$$" "$REL"
fi

# 2. a Python 3.11+ (this system's, else Force's own)
PY=""
for c in python3 python3.13 python3.12 python3.11; do
  p="$(command -v "$c" 2>/dev/null || true)"
  [ -n "$p" ] || continue
  # macOS: /usr/bin/python3 is a stub that opens the "install developer tools" dialog (and is 3.9 at most)
  [ "$OS" = Darwin ] && [ "$p" = /usr/bin/python3 ] && continue
  if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  PYDIR="$DATA/python/$PY_VER"
  if [ ! -x "$PYDIR/bin/python3" ]; then
    say "this system has no Python 3.11+: fetching Force's own (once, ~30 MB)"
    PYF="cpython-$PY_VER+$PY_TAG-$PY_TRIPLE-install_only_stripped.tar.gz"
    fetch "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/$(printf '%s' "$PYF" | sed 's/+/%2B/')" "$TMP/py.tgz" \
      || fail "could not download Python"
    [ "$(sha256 "$TMP/py.tgz")" = "$PY_SHA" ] || fail "checksum mismatch for Python: not installing it"
    rm -rf "$TMP/py" && mkdir "$TMP/py" && tar -xzf "$TMP/py.tgz" -C "$TMP/py"
    mkdir -p "$DATA/python" && rm -rf "$PYDIR" && mv "$TMP/py/python" "$PYDIR"
  fi
  PY="$PYDIR/bin/python3"
fi

# 3. the rest is Force's own setup (launcher, services, PATH, fonts).
# `-I` ignores PYTHONPATH, so splice the release onto sys.path in Python instead.
# shellcheck disable=SC2086
exec "$PY" -I -c 'import runpy, sys; sys.path.insert(0, sys.argv[1]); sys.argv = ["force setup"] + sys.argv[2:]; runpy.run_module("force.setup", run_name="__main__")' "$REL" --release "$REL" --python "$PY" $SETUP_ARGS
