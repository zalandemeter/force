#!/usr/bin/env bash
# Try a release before it exists: build the tarball from this checkout, then run the real one-line installer
# against it in a fresh container, as a new user would. Nothing is published and nothing on this machine's
# own Force changes.
#
#   scripts/try-release.sh                     build, install into ubuntu:24.04, run the checks
#   scripts/try-release.sh --image debian:12   another distro (any image with apt-get, or curl already in it)
#   scripts/try-release.sh --shell             after the checks, a shell in the container (try `force`)
#   scripts/try-release.sh --no-build          reuse the last build in build/out/try
#
# Needs Docker. The build runs in the builder container (scripts/build-release.sh); its output goes to
# build/out/try, apart from real release builds. In the container the installer fetches the tarball from that
# folder (FORCE_RELEASES_URL=file://...), so its pinned SHA-256 is checked exactly as in a real install. It still
# downloads Force's own Python from the internet when the image has no Python 3.11+, as a real install does.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="ubuntu:24.04"
SHELL_AFTER=0
BUILD=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="${2:?--image needs a value}"; shift 2 ;;
    --shell) SHELL_AFTER=1; shift ;;
    --no-build) BUILD=0; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "try-release: unknown option $1" >&2; exit 2 ;;
  esac
done
command -v docker >/dev/null || { echo "try-release: needs Docker" >&2; exit 1; }

ROOT="$REPO/build/out/try"
DIST="$ROOT/dist"
if [[ "$BUILD" == 1 ]]; then
  echo "try-release: building the release from this checkout"
  FORCE_BUILD_ROOT="$ROOT" FORCE_BUILD_CACHE="$REPO/build/out/cache" "$REPO/scripts/build-release.sh"
fi
[[ -f "$DIST/install.sh" ]] || { echo "try-release: no build in $DIST (run without --no-build)" >&2; exit 1; }
VERSION="$(sed -n 's/^VERSION="\(.*\)"/\1/p' "$DIST/install.sh" | head -1)"

# Runs as root in the container: installs curl if missing, then becomes a normal user and installs Force.
# shellcheck disable=SC2016
INSIDE='
set -eu
if ! command -v curl >/dev/null 2>&1; then
  echo "try-release: installing curl in the container"
  apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null
fi
id tester >/dev/null 2>&1 || useradd -m -s /bin/bash tester
cat > /home/tester/checks.sh <<"EOF"
set -u
export PATH="$HOME/.local/bin:$PATH"
ok=0
check() { name="$1"; shift; if "$@"; then echo "  ok    $name"; else echo "  FAIL  $name"; ok=1; fi; }
echo "try-release: installing Force $1 as a new user (the real install.sh)"
FORCE_RELEASES_URL=file:///dist/releases sh /dist/install.sh --no-fonts || { echo "  FAIL  install.sh"; exit 1; }
echo "try-release: checks"
check "force --version is $1" sh -c "force --version | grep -qx \"force $1\""
# what a new user does first: open the cockpit (without systemd that starts the tmux server and the daemon); a
# pseudo-terminal for a few seconds, then the client goes and Force keeps running
check "the cockpit opens"     sh -c "timeout 8 script -qec force /dev/null >/tmp/cockpit.txt 2>&1; force list >/dev/null"
check "a shell session starts" sh -c "force new try --shell --no-switch >/dev/null"
check "it is listed"          sh -c "sleep 2; force list >/tmp/list.txt 2>&1; grep -q try /tmp/list.txt || { cat /tmp/list.txt; false; }"
check "it runs a command"     sh -c "force keys try \"echo hello-from-force\" Enter >/dev/null && sleep 1 && force read try | grep -q hello-from-force"
check "force doctor"          sh -c "force doctor >/tmp/doctor.txt 2>&1 || { cat /tmp/doctor.txt; false; }"
check "force uninstall"       sh -c "force uninstall --yes >/dev/null 2>&1 && ! test -e ~/.local/bin/force"
exit $ok
EOF
chown tester /home/tester/checks.sh
su - tester -c "bash ~/checks.sh $VERSION"
'
SHELL_CMD=""
[[ "$SHELL_AFTER" == 1 ]] && SHELL_CMD='; echo "try-release: shell as the test user (Force is installed again for you)"; su - tester -c "FORCE_RELEASES_URL=file:///dist/releases sh /dist/install.sh --no-fonts >/dev/null && exec bash -l"'

echo "try-release: $IMAGE, Force $VERSION"
TTY=(-i); [[ -t 0 ]] && TTY=(-it)
set +e
docker run --rm "${TTY[@]}" -e VERSION="$VERSION" -v "$DIST:/dist:ro" "$IMAGE" sh -c "$INSIDE $SHELL_CMD"
rc=$?
set -e
if [[ $rc == 0 ]]; then echo "try-release: all checks passed ($IMAGE)"; else echo "try-release: checks failed ($IMAGE)" >&2; fi
exit $rc
