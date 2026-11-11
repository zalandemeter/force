#!/bin/sh
# The host picker opened by a click on the sidebar's host chip (the MouseDown1Pane binding in tmux.conf /
# tmux-ui.conf, SPEC §41): `force hosts-menu` writes its display-menu command to OUT instead of running it, and
# the binding runs OUT with source-file. That way the menu carries the click, so it stays open on release and its
# items can be clicked (a menu opened from run-shell has no mouse event: tmux then ignores clicks on it and
# closes it when the button is released). The controls button's menu is static config (share/menu-*.tmux).
# Usage: menu.sh hosts <socket_path> <client> <window_id> <out>
kind=$1; S=$2; C=$3; W=$4; OUT=$5
rm -f "$OUT"
case $kind in
  hosts) FORCE_MENU_OUT="$OUT" "${FORCE_BIN:-$HOME/.local/bin/force}" hosts-menu --client "$C" --window "$W" ;;
esac
exit 0
