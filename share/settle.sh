#!/bin/sh
# client-resized hook of the ui server (split mode): once a terminal has stopped resizing, repaint its whole
# window from scratch. A shrink drops pane rows (tmux keeps the bottom of an alternate screen), and when a
# repaint during a drag is missed the panes stay shifted or half blank: so 0.4 s after the last resize the
# sidebar and header get SIGUSR1 (they repaint every cell) and the nested client of the host is refreshed
# (a full redraw of the session). Debounced per client: a drag fires many hooks, only the last one works.
# Usage: settle.sh <ui socket_path> <client name>
S="$1"
C="$2"
t() { tmux -S "$S" "$@"; }
dir="${XDG_RUNTIME_DIR:-/tmp}/force-settle"
mkdir -p "$dir" 2>/dev/null || exit 0
token="$dir/$(printf '%s' "$C" | tr '/' '_')"
echo "$$" > "$token" 2>/dev/null || exit 0
sleep 0.4
[ "$(cat "$token" 2>/dev/null)" = "$$" ] || exit 0
rm -f "$token"
W=$(t display -p -c "$C" '#{window_id}' 2>/dev/null) || exit 0
[ -n "$W" ] || exit 0
# sidebar and header: only panes already running the Rust program (USR1 would kill one still starting)
t list-panes -t "$W" -F '#{?#{m:force-*,#{pane_current_command}},#{@force_role},starting} #{pane_pid}' 2>/dev/null |
while read -r role pid; do
  case "$role" in sidebar|header) kill -USR1 "$pid" 2>/dev/null ;; esac
done
# the session: its nested client on the host redraws everything (a remote over ssh, at most 8 s)
nested=$(t display -p -t "$W" '#{@force_nested}' 2>/dev/null)
htmux=$(t display -p -t "$W" '#{@force_host_tmux}' 2>/dev/null)
[ -n "$nested" ] || exit 0
[ -n "$htmux" ] || htmux="tmux -L force"
if command -v timeout >/dev/null 2>&1; then
  timeout 8 $htmux refresh-client -t "$nested" >/dev/null 2>&1 </dev/null
else  # macOS has no timeout(1) (SPEC §51)
  $htmux refresh-client -t "$nested" >/dev/null 2>&1 </dev/null &
  rp=$!
  ( sleep 8; kill "$rp" 2>/dev/null ) >/dev/null 2>&1 &
  wp=$!
  wait "$rp" 2>/dev/null
  kill "$wp" 2>/dev/null
fi
exit 0
