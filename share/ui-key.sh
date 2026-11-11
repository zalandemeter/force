#!/bin/sh
# Key actions of a ui window (split mode, SPEC §39/§40): act on the host the window shows. This machine:
# its tmux directly (fast); a remote: `force …` with FORCE_HOST, which runs it there over ssh.
# Usage: ui-key.sh <ui socket_path> <window_id> <action> [arg]
#   idx N | next | prev | last | restart | restart-all | upgrade | up | down | newshell NAME
S="$1"; W="$2"; A="$3"; ARG="$4"
t() { tmux -S "$S" "$@"; }
host=$(t display -p -t "$W" '#{@force_host}')
htmux=$(t display -p -t "$W" '#{@force_host_tmux}')
nested=$(t display -p -t "$W" '#{@force_nested}')
sess=$(t display -p -t "$W" '#{@force_session}')
[ -n "$htmux" ] || htmux="tmux -L force"
f() { FORCE_HOST="$host" "${FORCE_BIN:-$HOME/.local/bin/force}" "$@"; }
# The session the nested client shows now, asked from its host (@force_session can lag a switch by a
# sidebar poll); published to the window, and the sidebar and header poked to redraw. A remote's shell
# reads the format, so it is quoted for it.
shown() {
  if [ -n "$host" ]; then s=$($htmux display -p -c "$nested" "'#{client_session}'" 2>/dev/null)
  else s=$($htmux display -p -c "$nested" '#{client_session}' 2>/dev/null); fi
  if [ -n "$s" ] && [ "$s" != "$sess" ]; then
    t set-option -w -t "$W" @force_session "$s"
    t list-panes -t "$W" -F '#{?#{m:force-*,#{pane_current_command}},#{pane_pid},}' | while read -r pid; do
      [ -n "$pid" ] && kill -USR1 "$pid" 2>/dev/null; done
  fi
  printf '%s' "$s"
}
# a session action: on the session shown now; nothing when its host does not answer
on_shown() {
  sess=$(shown)
  if [ -z "$sess" ]; then t display-message -d 3000 " ${host:-this machine} does not answer "; exit 0; fi
}
case "$A" in
  idx)
    if [ -n "$host" ]; then
      f switch --index "$ARG" --client "$nested" >/dev/null 2>&1
    else
      target=$(t show -gqv "@force_idx_$ARG")
      [ -n "$target" ] && $htmux switch-client -c "$nested" -t "=$target" 2>/dev/null
    fi ;;
  next|prev) f switch "--$A" --client "$nested" >/dev/null 2>&1 ;;
  last) $htmux switch-client -c "$nested" -l 2>/dev/null ;;
  restart) on_shown; f restart "$sess" >/dev/null 2>&1 ;;
  restart-all) f restart --all >/dev/null 2>&1 ;;
  upgrade) on_shown; f upgrade "$sess" >/dev/null 2>&1 ;;
  up|down) on_shown; f move "$sess" "$A" >/dev/null 2>&1 ;;
  newshell) f new "$ARG" --shell --no-switch >/dev/null 2>&1 && $htmux switch-client -c "$nested" -t "=$ARG" 2>/dev/null ;;
esac
# after a switch from here the window says so at once (a remote's switch hook cannot reach this ui server)
case "$A" in idx|next|prev|last|newshell) [ -n "$host" ] && shown >/dev/null ;; esac
exit 0
