#!/usr/bin/env bash
# End-to-end install check (SPEC §51), run by CI on a fresh macOS runner (works on Linux too): install a built
# release from a local web root exactly as a user would, start the cockpit, open a shell session and an agent
# session (tests/fake_claude.py standing in for Claude Code), capture the cockpit's screen, then uninstall.
#
#   scripts/e2e-install.sh DIST OUTDIR
#
# DIST is a web root with install.sh (filled for http://127.0.0.1:$PORT) and releases/. OUTDIR receives the
# screen captures (.ansi and .html) and logs. Runs as the CI user against the real home: never on a workstation.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$(cd "$1" && pwd)"; OUT="$2"; PORT="${PORT:-8765}"
mkdir -p "$OUT"
[[ -n "${CI:-}" ]] || { echo "e2e-install: CI only (it installs into the real home)" >&2; exit 2; }
step() { printf '\n== %s\n' "$*"; }
wait_for() {  # wait_for SECONDS DESCRIPTION COMMAND...
  local n="$1" what="$2"; shift 2
  for _ in $(seq "$((n * 2))"); do "$@" >/dev/null 2>&1 && return 0; sleep 0.5; done
  echo "e2e-install: timed out waiting for $what" >&2; return 1
}

step "serve $DIST"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$DIST" >"$OUT/http.log" 2>&1 &
HTTP=$!
trap 'kill $HTTP 2>/dev/null || true' EXIT
wait_for 10 "the web root" curl -fsS "http://127.0.0.1:$PORT/install.sh"

step "a stand-in Claude Code (tests/fake_claude.py) at ~/.local/bin/claude"
mkdir -p "$HOME/.local/share/claude/versions" "$HOME/.local/bin"
cp "$REPO/tests/fake_claude.py" "$HOME/.local/share/claude/versions/9.9.9"
chmod +x "$HOME/.local/share/claude/versions/9.9.9"
ln -sf "$HOME/.local/share/claude/versions/9.9.9" "$HOME/.local/bin/claude"

step "install: curl … | sh"
curl -fsSL "http://127.0.0.1:$PORT/install.sh" | sh 2>&1 | tee "$OUT/install.log"
F="$HOME/.local/bin/force"
"$F" --version

step "the services"
if [[ "$(uname -s)" == Darwin ]]; then
  ls -l "$HOME/Library/LaunchAgents/" | tee "$OUT/launchagents.txt"
  launchctl print "gui/$(id -u)/cc.useforce.force" > "$OUT/launchd-daemon.txt" 2>&1 || true
  launchctl print "gui/$(id -u)/cc.useforce.force-tmux" > "$OUT/launchd-tmux.txt" 2>&1 || true
fi
# the server answers (a fresh install has no session yet, so not has-session)
wait_for 30 "the host tmux server" "$HOME/.local/share/force/app/share/tmux-bin/tmux" -L force show -gv exit-empty
wait_for 30 "the daemon's snapshot" test -s "$HOME/.local/state/force/snapshot.json"

step "sessions: a shell and an agent"
"$F" new e2e-shell --shell --no-switch
"$F" new e2e-agent --kind claude --no-switch
wait_for 30 "both sessions in the list" sh -c "\"$F\" list | grep -q e2e-shell && \"$F\" list | grep -q e2e-agent"
"$F" list | tee "$OUT/list.txt"
"$F" doctor 2>&1 | tee "$OUT/doctor.txt" || true

step "the cockpit on screen (a 140x40 terminal: Force's own tmux on a separate socket)"
T="$HOME/.local/share/force/app/share/tmux-bin/tmux"
printf 'set -g remain-on-exit on\nset -g exit-empty off\n' > "$OUT/outer.conf"   # an exiting client stays visible
"$T" -L e2e-outer -f "$OUT/outer.conf" new-session -d -x 140 -y 40 -e TERM=xterm-256color -e COLORTERM=truecolor \
  "$F attach --no-intro e2e-agent"
sleep 8
"$T" -L e2e-outer capture-pane -e -p -t 0 > "$OUT/cockpit.ansi"
python3 "$REPO/website/tools/ansi2html.py" "$OUT/cockpit.ansi" "$OUT/cockpit.html"
"$T" -L e2e-outer send-keys -t 0 'echo hello from the agent pane' Enter
"$F" switch e2e-shell 2>/dev/null || true
sleep 3
"$T" -L e2e-outer capture-pane -e -p -t 0 > "$OUT/cockpit-shell.ansi"
python3 "$REPO/website/tools/ansi2html.py" "$OUT/cockpit-shell.ansi" "$OUT/cockpit-shell.html"
grep -q "e2e-shell" "$OUT/cockpit.ansi" || { echo "e2e-install: the sidebar does not show e2e-shell" >&2; cat "$OUT/cockpit.ansi"; exit 1; }
"$T" -L e2e-outer kill-server 2>/dev/null || true

step "uninstall"
"$F" uninstall --yes 2>&1 | tee "$OUT/uninstall.log"
[[ ! -e "$HOME/.local/bin/force" ]] || { echo "e2e-install: the launcher is still there" >&2; exit 1; }
if [[ "$(uname -s)" == Darwin ]]; then
  ! ls "$HOME"/Library/LaunchAgents/cc.useforce.* >/dev/null 2>&1 || { echo "e2e-install: launch agents left" >&2; exit 1; }
fi
echo "e2e-install: ok"
