#!/usr/bin/env bash
# The version a build of this checkout gets when none is given (scripts/build-release.sh, build-darwin.sh): the
# newest release tag vX.Y.Z reachable from HEAD, without the v; 0.0.0 when there is none (a shallow clone, a fork
# without tags). A release itself takes its version from the tag pushed (.github/workflows/release.yml); nothing in
# the repository holds the version.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
v="$(git -C "$REPO" describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null || true)"
v="${v#v}"
[[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || v="0.0.0"
echo "$v"
