#!/usr/bin/env bash
# Build the macOS release tarball (SPEC §51) on a Mac with Apple Silicon: no container, everything native.
#
#   scripts/build-darwin.sh [VERSION]      VERSION defaults to the newest release tag's; output in $FORCE_BUILD_OUT
#                                          (default $FORCE_BUILD_ROOT/dist; the root defaults to build/out-darwin)
#   scripts/build-darwin.sh --tmux-only    only Force's tmux (cached); prints the folder of its binary (CI puts it
#                                          first on PATH, so the tests run the tmux Force ships, not Homebrew's)
#
# Produces force-VERSION-darwin-arm64.tar.gz and its .sha256, with the same layout as the Linux tarball
# (scripts/build-release.sh; keep the two in step): the Python package, share/ (bin/ = sidebar, header, dialog,
# intro; tmux-bin/tmux = Force's own tmux), terminfo/, install.sh, VERSION, LICENSE, THIRD-PARTY-NOTICES.md,
# licenses/ (tmux, libevent, ncurses, utf8proc). macOS links its system library dynamically only (no fully static
# binaries there): libevent, ncurses and utf8proc are linked in, and the check below refuses any other dylib.
# Needs: Xcode command line tools (clang, make), pkg-config, cargo, python3, curl.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMUX_ONLY=0
if [[ "${1:-}" == --tmux-only ]]; then TMUX_ONLY=1; shift; fi
VERSION="${1:-$("$REPO/scripts/version.sh")}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "build-darwin: version must be X.Y.Z, got '$VERSION'" >&2; exit 2; }
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo "build-darwin: run on macOS, Apple Silicon" >&2; exit 2; }
DEFAULT_OUT="${FORCE_BUILD_ROOT:-$REPO/build/out-darwin}"
OUT="${FORCE_BUILD_OUT:-$DEFAULT_OUT/dist}"
CACHE="${FORCE_BUILD_CACHE:-$DEFAULT_OUT/cache}"
BASE_URL="${FORCE_DOWNLOAD_BASE:-https://useforce.cc}"
JOBS="$(sysctl -n hw.ncpu)"
mkdir -p "$OUT/releases" "$CACHE" "$REPO/build/src"
sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

# pinned C sources: the Linux build's three (build-release.sh) plus utf8proc, which tmux needs on macOS for
# character widths (the system's wcwidth is wrong for many symbols, and the sidebar draws with them)
SRC=(
  "tmux-3.4.tar.gz https://github.com/tmux/tmux/releases/download/3.4/tmux-3.4.tar.gz 551ab8dea0bf505c0ad6b7bb35ef567cdde0ccb84357df142c254f35a23e19aa"
  "libevent-2.1.12-stable.tar.gz https://github.com/libevent/libevent/releases/download/release-2.1.12-stable/libevent-2.1.12-stable.tar.gz 92e6de1be9ec176428fd2367677e61ceffc2ee1cb119035037a27d346b0403bb"
  "ncurses-6.5.tar.gz https://ftp.gnu.org/gnu/ncurses/ncurses-6.5.tar.gz 136d91bc269a9a5785e5f9e980bc76ab57428f604ce3e5a5a90cebc767971cc6"
  "utf8proc-2.12.0.tar.gz https://github.com/JuliaStrings/utf8proc/releases/download/v2.12.0/utf8proc-2.12.0.tar.gz a393fbef160835fb315bc3e91ba8d86f7a73a7cec9e6198b6c60b848b498bfeb"
)
for line in "${SRC[@]}"; do
  read -r f url sha <<<"$line"
  if [[ ! -f "$REPO/build/src/$f" || "$(sha256 "$REPO/build/src/$f")" != "$sha" ]]; then
    echo "build-darwin: fetching $f" >&2
    curl -fsSL --retry 2 -o "$REPO/build/src/$f.tmp" "$url"
    [[ "$(sha256 "$REPO/build/src/$f.tmp")" == "$sha" ]] || { echo "build-darwin: checksum mismatch: $f" >&2; exit 1; }
    mv "$REPO/build/src/$f.tmp" "$REPO/build/src/$f"
  fi
done

# --- C dependencies and tmux (cached by the pins and this script) -------------------------------------------
KEY="$(sha256 "$0" | cut -c1-12)"
DEPS="$CACHE/deps-$KEY"
TMUX_PREFIX="$CACHE/tmux-$KEY"
B="$CACHE/src-$KEY"
if [[ ! -x "$TMUX_PREFIX/bin/tmux" ]]; then
  rm -rf "$DEPS" "$TMUX_PREFIX" "$B"
  mkdir -p "$B"
  for line in "${SRC[@]}"; do read -r f _ <<<"$line"; tar -xzf "$REPO/build/src/$f" -C "$B"; done
  export MACOSX_DEPLOYMENT_TARGET=13.0
  echo "build-darwin: ncurses" >&2
  # terminfo: macOS's own database, Homebrew's, then Force's (TERMINFO_DIRS set by Force wins)
  (cd "$B/ncurses-6.5" && ./configure --prefix="$DEPS" --without-shared --without-debug --without-ada \
      --without-cxx-binding --without-manpages --without-tests --enable-widec --enable-pc-files \
      --with-pkg-config-libdir="$DEPS/lib/pkgconfig" \
      --with-terminfo-dirs=/usr/share/terminfo:/opt/homebrew/share/terminfo:/usr/local/share/terminfo \
      --with-default-terminfo-dir=/usr/share/terminfo >/dev/null \
    && make -j"$JOBS" >/dev/null \
    && make install.libs install.includes install.progs >/dev/null)   # not install.data: /usr/share is read-only
  echo "build-darwin: libevent" >&2
  (cd "$B/libevent-2.1.12-stable" && ./configure --prefix="$DEPS" --disable-shared --enable-static \
      --disable-openssl --disable-samples --disable-libevent-regress >/dev/null \
    && make -j"$JOBS" >/dev/null && make install >/dev/null)
  echo "build-darwin: utf8proc" >&2
  (cd "$B/utf8proc-2.12.0" && make -j"$JOBS" prefix="$DEPS" >/dev/null && make install prefix="$DEPS" >/dev/null)
  rm -f "$DEPS"/lib/*.dylib                         # only the static archives are left for the linker
  echo "build-darwin: tmux" >&2
  (cd "$B/tmux-3.4" && PKG_CONFIG_LIBDIR="$DEPS/lib/pkgconfig" PKG_CONFIG_PATH="" ./configure \
      --prefix="$TMUX_PREFIX" --enable-utf8proc \
      CFLAGS="-I$DEPS/include -I$DEPS/include/ncursesw -O2" LDFLAGS="-L$DEPS/lib" >/dev/null \
    && make -j"$JOBS" >/dev/null && make install >/dev/null)
  strip "$TMUX_PREFIX/bin/tmux"
fi
"$TMUX_PREFIX/bin/tmux" -V >&2
if [[ "$TMUX_ONLY" == 1 ]]; then echo "$TMUX_PREFIX/bin"; exit 0; fi

# --- the Rust programs ---------------------------------------------------------------------------------------
echo "build-darwin: Rust programs"
for crate in sidebar intro; do
  CARGO_TARGET_DIR="$CACHE/target-$crate" MACOSX_DEPLOYMENT_TARGET=13.0 \
    cargo build --release --locked -q --manifest-path "$REPO/crates/$crate/Cargo.toml"
done

# --- the tarball (same layout as build-release.sh) -----------------------------------------------------------
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
NAME="force-$VERSION"
R="$STAGE/$NAME"
mkdir -p "$R"
cp -a "$REPO/force" "$REPO/share" "$REPO/systemd" "$REPO/install.sh" "$REPO/LICENSE" "$REPO/THIRD-PARTY-NOTICES.md" \
  "$REPO/README.md" "$REPO/docs/SPEC.md" "$R/"
find "$R" -name '__pycache__' -prune -exec rm -rf {} +
python3 "$REPO/scripts/support-pack.py" --out "$R/share/support"
mkdir -p "$R/share/bin" "$R/share/tmux-bin" "$R/share/terminfo" "$R/licenses"
for b in force-sidebar force-header force-dialog; do cp "$CACHE/target-sidebar/release/$b" "$R/share/bin/"; done
cp "$CACHE/target-intro/release/force-intro" "$R/share/bin/"
strip "$R"/share/bin/force-*
cp "$TMUX_PREFIX/bin/tmux" "$R/share/tmux-bin/tmux"
# terminfo for what Force's tmux tells programs inside it (TERM=tmux-256color): macOS has no tmux entries
tar -xzf "$REPO/build/src/ncurses-6.5.tar.gz" -C "$STAGE" ncurses-6.5/misc/terminfo.src
"$DEPS/bin/tic" -x -o "$R/share/terminfo" -e tmux,tmux-256color "$STAGE/ncurses-6.5/misc/terminfo.src"
rm -rf "$STAGE/ncurses-6.5"
for pair in "tmux-3.4.tar.gz tmux-3.4/COPYING tmux.txt" "libevent-2.1.12-stable.tar.gz libevent-2.1.12-stable/LICENSE libevent.txt" \
            "ncurses-6.5.tar.gz ncurses-6.5/COPYING ncurses.txt" "utf8proc-2.12.0.tar.gz utf8proc-2.12.0/LICENSE.md utf8proc.txt"; do
  read -r f member dest <<<"$pair"
  tar -xzOf "$REPO/build/src/$f" "$member" > "$R/licenses/$dest"
done
# every binary links only the system's own libraries
for f in "$R"/share/bin/* "$R/share/tmux-bin/tmux"; do
  bad="$(otool -L "$f" | tail -n +2 | awk '{print $1}' | grep -v -E '^/usr/lib/|^/System/Library/' || true)"
  [[ -z "$bad" ]] || { echo "build-darwin: $f links $bad" >&2; exit 1; }
done
printf '%s\n' "$VERSION" > "$R/VERSION"
printf '%s\n' "$BASE_URL" > "$R/share/download-base"

TAR="$OUT/releases/$NAME-darwin-arm64.tar.gz"
# a reproducible tarball (owner root, sorted) without GNU tar
python3 - "$STAGE" "$NAME" "$TAR.tmp" <<'PY'
import os, sys, tarfile
stage, name, out = sys.argv[1:]
def reset(ti):
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti
with tarfile.open(out, "w:gz", format=tarfile.PAX_FORMAT) as tar:
    paths = [name]
    for root, dirs, files in os.walk(os.path.join(stage, name)):
        dirs.sort()
        rel = os.path.relpath(root, stage)
        paths += [os.path.join(rel, d) for d in dirs] + [os.path.join(rel, f) for f in sorted(files)]
    for p in sorted(paths):
        tar.add(os.path.join(stage, p), arcname=p, recursive=False, filter=reset)
PY
mv "$TAR.tmp" "$TAR"
(cd "$OUT/releases" && shasum -a 256 "$(basename "$TAR")" > "$(basename "$TAR").sha256")
echo "build-darwin: $TAR ($(du -h "$TAR" | cut -f1)) sha256 $(cut -c1-16 "$TAR.sha256")…"
