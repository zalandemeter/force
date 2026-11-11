#!/usr/bin/env bash
# The dev instance (SPEC §2.1): this checkout as a second, separate Force beside the live one. It has its own
# data, settings and tmux servers (force-dev, force-dev-ui), no systemd units, and its own command, force-dev.
# The live install (~/.local/share/force, ~/.local/bin/force, the force and force-ui servers, the units) is never
# touched.
#
#   ./dev.sh [--no-test]   test, build and deploy the checkout into the instance; then run: force-dev
#   ./dev.sh stop          stop its tmux servers and its daemon
#   ./dev.sh reset         stop it and delete it (its data, settings, state and command)
#   ./dev.sh help
#
# FORCE_INSTANCE picks another instance name (default dev): FORCE_INSTANCE=b ./dev.sh, then force-b.
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
I="${FORCE_INSTANCE:-dev}"
[[ "$I" =~ ^[a-z][a-z0-9]{0,15}$ ]] || { echo "dev: FORCE_INSTANCE must be a short lowercase name (a-z, 0-9)" >&2; exit 2; }
N="force-$I"
HOME_DIR="$HOME/.local/share/$N"
STATE_DIR="$HOME/.local/state/$N"
CONFIG_DIR="$HOME/.config/$N"
LAUNCHER="$HOME/.local/bin/$N"

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

# A python 3.11+ for the launcher, found as install.sh does (the launcher runs Force with -I: no venv needed).
find_python() {
  local c p
  for c in python3 python3.13 python3.12 python3.11; do
    p="$(command -v "$c" 2>/dev/null || true)"
    if [[ -n "$p" ]] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      printf '%s\n' "$p"; return 0
    fi
  done
  echo "dev: needs Python 3.11+" >&2
  return 1
}

write_launcher() {
  local py tmp
  py="$(find_python)"
  mkdir -p "$(dirname "$LAUNCHER")"
  tmp="$LAUNCHER.$$.tmp"
  cat > "$tmp" <<EOF
#!$py -I
# The $I instance of Force (written by ./dev.sh in $REPO); it runs $HOME_DIR/app.
import os, sys
os.environ["FORCE_INSTANCE"] = "$I"
os.environ["FORCE_BIN"] = "$LAUNCHER"
sys.path.insert(0, "$HOME_DIR/app")
from force.cli import main
sys.exit(main())
EOF
  chmod 755 "$tmp"
  mv -f "$tmp" "$LAUNCHER"
}

stop() {
  tmux -L "$N-ui" kill-server 2>/dev/null || true
  tmux -L "$N" kill-server 2>/dev/null || true
  # the daemon: the pid it writes into its lock file, killed only when that process is this instance's daemon
  local pid
  pid="$(head -c 16 "$STATE_DIR/daemon.lock" 2>/dev/null | tr -dc '0-9' || true)"
  if [[ -n "$pid" && -r "/proc/$pid/cmdline" ]] \
      && tr '\0' '\n' < "/proc/$pid/cmdline" | grep -qxF "$LAUNCHER" \
      && tr '\0' '\n' < "/proc/$pid/cmdline" | grep -qx daemon; then
    kill "$pid" 2>/dev/null || true
    echo "dev: stopped the $I daemon ($pid)"
  fi
  echo "dev: $I stopped"
}

reset() {
  stop
  local p
  for p in "$HOME_DIR" "$STATE_DIR" "$CONFIG_DIR" "$LAUNCHER"; do
    # every path is exactly .../force-<instance>: never the live install's
    [[ "$(basename "$p")" == "$N" && "$N" != "force" ]] || { echo "dev: refusing to remove $p" >&2; exit 1; }
    if [[ -e "$p" || -L "$p" ]]; then
      echo "dev: removing $p"
      rm -rf -- "$p"
    fi
  done
  echo "dev: $I reset"
}

case "${1:-}" in
  help|-h|--help) usage ;;
  stop) stop ;;
  reset) reset ;;
  ""|--no-test)
    FORCE_INSTANCE="$I" "$REPO/deploy.sh" "$@"
    write_launcher
    echo "dev: run $N (the live Force is untouched)"
    ;;
  *) echo "dev: unknown command $1 (./dev.sh help)" >&2; exit 2 ;;
esac
