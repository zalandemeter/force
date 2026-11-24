#!/bin/sh
# Key actions of a ui window (split mode, SPEC §39/§40): act on the host the window shows. This machine:
# its tmux directly (fast); a remote: `force …` with FORCE_HOST, which runs it there over the link.
# Usage: ui-key.sh <ui socket_path> <window_id> <action> [arg]
#   idx N | next | prev | last | restart | restart-all | upgrade | rc | unread | up | down | newshell NAME
# Every call to a remote is a round trip (SPEC §40): a switch and the question which session the client shows
# then go in one call (`force switch … --print`, or one tmux command list).
S="$1"; W="$2"; A="$3"; ARG="$4"
t() { tmux -S "$S" "$@"; }
NL='
'
# the window's options, one local tmux call
window() {
  vals=$(t display -p -t "$W" "#{@force_host}$NL#{@force_host_tmux}$NL#{@force_nested}$NL#{@force_session}$NL#{@force_overlay}")
  { IFS= read -r host; IFS= read -r htmux; IFS= read -r nested; IFS= read -r sess; IFS= read -r overlay; } <<EOF
$vals
EOF
  [ -n "$htmux" ] || htmux="tmux -L force"
}
window
# `force …` on the host shown. A remote reached by force-link (@force_host_tmux = `force-link exec … NAME -- TMUX -L
# force`): straight through the link, as `force` here would forward it (_forward: `force-link exec … NAME --
# ~/.local/bin/force …`), without starting Python here first; through plain ssh (a dev checkout without force-link)
# or this machine: `force` with FORCE_HOST.
f() {
  if [ -n "$host" ]; then
    case "$htmux" in
      *" -- "*) ${htmux%% -- *} -- '~/.local/bin/force' "$@"; return ;;
    esac
  fi
  FORCE_HOST="$host" "${FORCE_BIN:-$HOME/.local/bin/force}" "$@"
}
# Chuck shown over a remote (SPEC §50, @force_overlay): going to another session first brings the window's own
# host back in the main pane; the shown session's own actions (restart, upgrade) stay with Chuck here
case "$A" in
  idx|next|prev|last|newshell|restart-all|up|down)
    if [ -n "$overlay" ]; then
      "${FORCE_BIN:-$HOME/.local/bin/force}" ui-main --window "$W" --wait >/dev/null 2>&1
      window
    fi ;;
esac
# A remote still connecting (SPEC §40): its nested client is named once the remote has reported; wait for it
# (at most 10 s) instead of acting on no client
if [ -z "$nested" ] && [ -n "$host" ]; then
  i=0
  while [ -z "$nested" ] && [ "$i" -lt 100 ]; do
    sleep 0.1; i=$((i + 1))
    nested=$(t display -p -t "$W" '#{@force_nested}')
  done
  [ -n "$nested" ] || exit 0
  sess=$(t display -p -t "$W" '#{@force_session}')
fi
# A word for the host's tmux: through plain ssh the remote shell reads it, so it is quoted there; over force-link
# (which quotes every word itself, SPEC §56.1) or a local tmux it is passed as it is.
q() {
  case "$htmux" in
    ssh\ *) printf "'%s'" "$1" ;;
    *)      printf '%s' "$1" ;;
  esac
}
# The session the nested client shows now (@force_session can lag a switch by a sidebar poll): published to the
# window, and the sidebar and header poked to redraw.
publish() {
  s="$1"
  if [ -n "$s" ] && [ "$s" != "$sess" ]; then
    t set-option -w -t "$W" @force_session "$s"
    t list-panes -t "$W" -F '#{?#{m:force-*,#{pane_current_command}},#{pane_pid},}' | while read -r pid; do
      [ -n "$pid" ] && kill -USR1 "$pid" 2>/dev/null; done
  fi
  printf '%s' "$s"
}
# asked from its host
shown() {
  publish "$($htmux display -p -c "$nested" "$(q '#{client_session}')" 2>/dev/null)"
}
# a session action: on the session shown now; nothing when its host does not answer
on_shown() {
  sess=$(shown)
  if [ -z "$sess" ]; then t display-message -d 3000 " ${host:-this machine} does not answer "; exit 0; fi
}
# a remote's tmux: `switch-client ARGS` and what the client shows then, in one call; when the switch fails (nothing
# printed), asked on its own as before
tmux_switch() {
  s=$($htmux switch-client -c "$nested" "$@" "$(q ';')" display -p -c "$nested" "$(q '#{client_session}')" 2>/dev/null)
  if [ -n "$s" ]; then publish "$s" >/dev/null; else shown >/dev/null; fi
}
# a remote's `force switch ARGS`: the session it switched to comes back (--print); a remote too old for --print
# switches without it, then is asked
force_switch() {
  out=$(f switch "$@" --client "$nested" --print 2>&1)
  case "$out" in
    *"unrecognized arguments: --print"*) f switch "$@" --client "$nested" >/dev/null 2>&1; shown >/dev/null ;;
    *) s=$(printf '%s\n' "$out" | tail -n 1)
       if [ -n "$s" ] && [ "$s" = "$(printf '%s' "$out")" ]; then publish "$s" >/dev/null; else shown >/dev/null; fi ;;
  esac
}
case "$A" in
  idx)
    if [ -n "$host" ]; then
      force_switch --index "$ARG"
    else
      target=$(t show -gqv "@force_idx_$ARG")
      [ -n "$target" ] && $htmux switch-client -c "$nested" -t "=$target" 2>/dev/null
    fi ;;
  next|prev)
    if [ -n "$host" ]; then force_switch "--$A"
    else f switch "--$A" --client "$nested" >/dev/null 2>&1; fi ;;
  last)
    if [ -n "$host" ]; then tmux_switch -l
    else $htmux switch-client -c "$nested" -l 2>/dev/null; fi ;;
  restart) on_shown; f restart "$sess" >/dev/null 2>&1 ;;
  restart-all) f restart --all >/dev/null 2>&1 ;;
  upgrade) on_shown; f upgrade "$sess" >/dev/null 2>&1 ;;
  rc) on_shown; m=$(f remote-control "$sess" toggle 2>&1); t display-message -d 4000 " $m " ;;    # SPEC §53
  unread) on_shown; m=$(f mark-unread "$sess" 2>&1); t display-message -d 4000 " $m " ;;    # SPEC §5 marked unread
  up|down) on_shown; f move "$sess" "$A" >/dev/null 2>&1 ;;
  newshell)
    if f new "$ARG" --shell --no-switch >/dev/null 2>&1; then
      if [ -n "$host" ]; then tmux_switch -t "=$ARG"
      else $htmux switch-client -c "$nested" -t "=$ARG" 2>/dev/null; fi
    elif [ -n "$host" ]; then
      shown >/dev/null
    fi ;;
esac
exit 0
