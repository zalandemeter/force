#!/usr/bin/env bash
# Build a Force release tarball (SPEC §46): everything a machine needs, prebuilt and static.
#
#   scripts/build-release.sh [VERSION]      VERSION defaults to pyproject.toml's; output in $FORCE_BUILD_OUT
#                                           (default $FORCE_BUILD_ROOT/dist; the root defaults to build/out)
#
# Produces force-VERSION-linux-ARCH.tar.gz and its .sha256. Inside (force-VERSION/): the Python package (force/),
# share/ (scripts, tmux configs, bin/ = static sidebar, header, dialog, intro; tmux-bin/tmux = Force's own
# static tmux), terminfo/, systemd/, install.sh, VERSION, LICENSE, THIRD-PARTY-NOTICES.md, licenses/
# (tmux, libevent, ncurses).
# The binaries are built in the force-builder container (build/Dockerfile, Alpine/musl): no toolchain on the host.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml" | head -1)}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "build-release: version must be X.Y.Z, got '$VERSION'" >&2; exit 2; }
ARCH="$(uname -m)"
case "$ARCH" in x86_64|aarch64) ;; *) echo "build-release: unsupported arch $ARCH" >&2; exit 2 ;; esac
DEFAULT_OUT="${FORCE_BUILD_ROOT:-$REPO/build/out}"
OUT="${FORCE_BUILD_OUT:-$DEFAULT_OUT/dist}"
CACHE="${FORCE_BUILD_CACHE:-$DEFAULT_OUT/cache}"
IMAGE="force-builder:$(sha256sum "$REPO/build/Dockerfile" | cut -c1-12)"
mkdir -p "$OUT" "$CACHE" "$OUT/releases"

# $OUT keeps the install.sh web root layout (install.sh at its top, tarballs under releases/), but the
# published tarballs are fetched from GitHub Releases; the site serves only install.sh. The download base
# (install.sh's own URL) is https://useforce.cc unless FORCE_DOWNLOAD_BASE says otherwise (a test server, a mirror).
BASE_URL="${FORCE_DOWNLOAD_BASE:-https://useforce.cc}"
if [[ -n "${FORCE_RELEASES_URL:-}" ]]; then
  RELEASES_URL="$FORCE_RELEASES_URL"
elif [[ -n "${FORCE_DOWNLOAD_BASE:-}" ]]; then
  RELEASES_URL="$BASE_URL/releases"
else
  RELEASES_URL="https://github.com/zalandemeter/force/releases/download/v$VERSION"
fi

# pinned C sources (the Dockerfile checks them again)
declare -A SRC=(
  [tmux-3.4.tar.gz]="https://github.com/tmux/tmux/releases/download/3.4/tmux-3.4.tar.gz 551ab8dea0bf505c0ad6b7bb35ef567cdde0ccb84357df142c254f35a23e19aa"
  [libevent-2.1.12-stable.tar.gz]="https://github.com/libevent/libevent/releases/download/release-2.1.12-stable/libevent-2.1.12-stable.tar.gz 92e6de1be9ec176428fd2367677e61ceffc2ee1cb119035037a27d346b0403bb"
  [ncurses-6.5.tar.gz]="https://ftp.gnu.org/gnu/ncurses/ncurses-6.5.tar.gz 136d91bc269a9a5785e5f9e980bc76ab57428f604ce3e5a5a90cebc767971cc6"
)
mkdir -p "$REPO/build/src"
for f in "${!SRC[@]}"; do
  read -r url sha <<<"${SRC[$f]}"
  if ! echo "$sha  $REPO/build/src/$f" | sha256sum -c --quiet - 2>/dev/null; then
    echo "build-release: fetching $f"
    curl -fsSL -o "$REPO/build/src/$f.tmp" "$url"
    echo "$sha  $REPO/build/src/$f.tmp" | sha256sum -c --quiet - || { echo "build-release: checksum mismatch: $f" >&2; exit 1; }
    mv "$REPO/build/src/$f.tmp" "$REPO/build/src/$f"
  fi
done

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "build-release: building $IMAGE"
  nice -n 10 docker build -q -t "$IMAGE" "$REPO/build" >/dev/null
fi

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
NAME="force-$VERSION"
R="$STAGE/$NAME"
mkdir -p "$R" "$STAGE/bins"
echo "build-release: static binaries"
nice -n 10 docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$REPO:/src:ro" -v "$CACHE:/cache" -v "$STAGE/bins:/out" "$IMAGE" /src/build/inside.sh

cp -a "$REPO/force" "$REPO/share" "$REPO/systemd" "$REPO/install.sh" "$REPO/LICENSE" "$REPO/THIRD-PARTY-NOTICES.md" \
  "$REPO/README.md" "$REPO/docs/SPEC.md" "$R/"
find "$R" -name '__pycache__' -prune -exec rm -rf {} +
# Chuck's context pack (SPEC §50): the docs, the command reference and CONTRIBUTING, made from this tree
python3 "$REPO/scripts/support-pack.py" --out "$R/share/support"
mkdir -p "$R/share/bin"
cp -a "$STAGE/bins/bin/." "$R/share/bin/"
# Force's own static tmux ships alone in share/tmux-bin/ (the systemd units and cli put it first on PATH).
mkdir -p "$R/share/tmux-bin"
mv "$R/share/bin/tmux" "$R/share/tmux-bin/tmux"
# and for development deploys on this machine (deploy.sh ships it when present; gitignored)
mkdir -p "$REPO/build/tmux-bin" && cp -a "$R/share/tmux-bin/tmux" "$REPO/build/tmux-bin/tmux"
rm -f "$R/share/bin/tmux"
cp -a "$STAGE/bins/terminfo" "$R/share/terminfo"
cp -a "$STAGE/bins/licenses" "$R/licenses"
printf '%s\n' "$VERSION" > "$R/VERSION"
# where release updates/installs download from later (force update reads this)
printf '%s\n' "$BASE_URL" > "$R/share/download-base"

TAR="$OUT/releases/$NAME-linux-$ARCH.tar.gz"
tar -C "$STAGE" --owner=0 --group=0 --numeric-owner --sort=name -czf "$TAR.tmp" "$NAME"
mv "$TAR.tmp" "$TAR"
(cd "$OUT/releases" && sha256sum "$(basename "$TAR")" > "$(basename "$TAR").sha256")

# install.sh = share/get-force.sh with the placeholders filled: this arch's pinned SHA, the other arch's kept
# from an already-built tarball if one exists (else empty the other->"no build yet"), the download base, and
# the GitHub Releases URL the tarball is fetched from.
SHA_THIS="$(cut -d' ' -f1 "$TAR.sha256")"
OTHER_ARCH="aarch64"; [[ "$ARCH" == "aarch64" ]] && OTHER_ARCH="x86_64"
OTHER_SHA=""
if [[ -f "$OUT/releases/$NAME-linux-$OTHER_ARCH.tar.gz.sha256" ]]; then
  OTHER_SHA="$(cut -d' ' -f1 "$OUT/releases/$NAME-linux-$OTHER_ARCH.tar.gz.sha256")"
fi
if [[ "$ARCH" == x86_64 ]]; then SHA_x86_64="$SHA_THIS"; SHA_aarch64="$OTHER_SHA"
else SHA_aarch64="$SHA_THIS"; SHA_x86_64="$OTHER_SHA"; fi
python3 "$REPO/scripts/fill-installer.py" "$REPO/share/get-force.sh" "$OUT/install.sh" \
  "$VERSION" "$BASE_URL" "$RELEASES_URL" "$SHA_x86_64" "$SHA_aarch64"
echo "build-release: $TAR ($(du -h "$TAR" | cut -f1)) sha256 $(cut -c1-16 "$TAR.sha256")…"
