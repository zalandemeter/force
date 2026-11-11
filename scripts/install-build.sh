#!/usr/bin/env bash
# Try this checkout on a machine that runs Force: build the release tarball here and install it there beside its
# releases, then make it the live one the way `force update` does (sessions keep running).
#
#   scripts/install-build.sh HOST            build and install (HOST: anything ssh reaches without a password)
#   scripts/install-build.sh HOST --back     switch HOST back to the newest published release installed there
#   scripts/install-build.sh HOST --no-build install the last build again (build/out/install-build)
#
# The build is installed as releases/<version>-<branch>-<commit>. Its version stays the last published one, so the
# next `force update` on HOST replaces it with the next real release. Needs Docker here (scripts/build-release.sh).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:-}"
[[ -n "$HOST" && "$HOST" != -* ]] || { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
shift
MODE=install
BUILD=1
for a in "$@"; do
  case "$a" in
    --back) MODE=back ;;
    --no-build) BUILD=0 ;;
    *) echo "install-build: unknown option $a" >&2; exit 2 ;;
  esac
done
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 "$HOST")

# On HOST: Force's own setup step on a release folder, with the Python its launcher uses (as install.sh does)
# shellcheck disable=SC2016  # expanded on HOST
SETUP='set -eu
REL="$1"
PY=$(head -1 "$HOME/.local/bin/force" | sed "s/^#!//; s/ -I\$//")
[ -x "$PY" ] || PY=$(command -v python3)
"$PY" -I -c "import runpy, sys; sys.path.insert(0, sys.argv[1]); sys.argv = [\"force setup\"] + sys.argv[2:]; runpy.run_module(\"force.setup\", run_name=\"__main__\")" "$REL" --release "$REL" --python "$PY" --no-fonts | tail -1
sleep 3
"$HOME/.local/bin/force" --version
"$HOME/.local/bin/force" doctor || true'

if [[ "$MODE" == back ]]; then
  # shellcheck disable=SC2016
  "${SSH[@]}" 'set -eu; D="$HOME/.local/share/force/releases"
    R=$(ls -1 "$D" | grep -E "^[0-9]+\.[0-9]+\.[0-9]+$" | sort -V | tail -1)
    [ -n "$R" ] || { echo "install-build: no published release installed there" >&2; exit 1; }
    echo "install-build: back to $R"; sh -s "$D/$R"' <<<"$SETUP"
  exit 0
fi

ROOT="$REPO/build/out/install-build"
DIST="$ROOT/dist/releases"
if [[ "$BUILD" == 1 ]]; then
  echo "install-build: building this checkout"
  FORCE_BUILD_ROOT="$ROOT" FORCE_BUILD_CACHE="$REPO/build/out/cache" nice -n 10 "$REPO/scripts/build-release.sh" | tail -1
fi
TAR="$(find "$DIST" -maxdepth 1 -name "force-*-linux-*.tar.gz" -printf "%T@ %p\n" 2>/dev/null | sort -nr | head -1 | cut -d" " -f2-)"
[[ -n "$TAR" ]] || { echo "install-build: no build in $DIST (run without --no-build)" >&2; exit 1; }
VERSION="$(basename "$TAR" | sed -E 's/^force-([0-9.]+)-linux-.*/\1/')"
BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD | tr -c 'A-Za-z0-9.\n' '-')"
COMMIT="$(git -C "$REPO" rev-parse --short HEAD)"
[[ -z "$(git -C "$REPO" status --porcelain)" ]] || COMMIT="$COMMIT-dirty"
NAME="$VERSION-$BRANCH-$COMMIT"

echo "install-build: installing $NAME on $HOST"
REMOTE_TMP="$("${SSH[@]}" 'mktemp -d')"
scp -q -o BatchMode=yes "$TAR" "$TAR.sha256" "$HOST:$REMOTE_TMP/"
# shellcheck disable=SC2016
"${SSH[@]}" "set -eu; T='$REMOTE_TMP'; NAME='$NAME'; VERSION='$VERSION'"'
  cd "$T" && sha256sum -c --quiet ./*.sha256
  D="$HOME/.local/share/force/releases"; mkdir -p "$D"
  mkdir x && tar -xzf ./force-*.tar.gz -C x
  rm -rf "$D/$NAME" && mv "x/force-$VERSION" "$D/$NAME"
  cd / && rm -rf "$T"
  sh -s "$D/$NAME"' <<<"$SETUP"
echo "install-build: $HOST runs $NAME (back: scripts/install-build.sh $HOST --back)"
