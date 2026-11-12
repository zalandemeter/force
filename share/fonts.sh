#!/usr/bin/env bash
# Force fonts (SPEC §45): Force's icons and the old terminal kit cleanup.
#
#   force fonts               install the Nerd Font symbols (icons) into ~/.local/share/fonts/force,
#                           then remove the old terminal profiles and launchers
#   force fonts --cleanup-only    remove the old terminal profiles/launchers only (no network)
#   force fonts --uninstall       remove the symbols font AND the old terminal profiles/launchers
#   force fonts -h, --help      this help
set -euo pipefail

BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
FONTS="$HOME/.local/share/fonts/force"
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CONF_DIR="$XDG_CONFIG_HOME/force"
SYMBOL_FAMILY="Symbols Nerd Font Mono"

# stable ids of the profiles the old kit created (a re-run updates the same profile)
TILIX_UUID=679ebf0c-c4d4-48ca-b4ff-43ea50a1e654
GNOME_UUID=4f0c4ace-f0c3-4c4c-9c4c-f0ce4c000001

NF_URL=https://github.com/ryanoasis/nerd-fonts/releases/download/v3.5.1/NerdFontsSymbolsOnly.tar.xz
NF_SHA=01172f37db8543edb102e5cb5c64101c9f4686630804d49b419aa07b23a69996

say()  { printf '\033[1;38;2;217;119;87mforce\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mforce: %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() { cat <<'EOF'
force fonts: install Force's Nerd Font symbols (icons) and remove the old terminal kit.
  force fonts                 install the symbols font into ~/.local/share/fonts/force, then clean up
  force fonts --cleanup-only  only remove the old terminal profiles/launchers (no network)
  force fonts --uninstall     remove the symbols font and the old terminal profiles/launchers
  force fonts -h, --help    this help
EOF
}

CLEANUP_ONLY=0 UNINSTALL=0
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --cleanup-only) CLEANUP_ONLY=1 ;;
  --uninstall) UNINSTALL=1 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac


# dconf/gsettings need the session bus. Use the well-known runtime socket when DBUS_SESSION_BUS_ADDRESS is unset;
# without any bus, skip those steps (a silent no-op).
DBUS=0
if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
  DBUS=1
else
  RUNDIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if [ -S "$RUNDIR/bus" ]; then
    DBUS=1
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNDIR/bus"
  fi
fi

REMOVED=0

rm_any() {  # remove a file or symlink, silent no-op when absent
  local f="$1"
  if [ -e "$f" ] || [ -L "$f" ]; then rm -f "$f"; REMOVED=1; fi
}

# gsettings string-list removal (Tilix / GNOME Terminal profile lists); schema missing -> no-op
list_remove() {
  local schema="$1" id="$2" cur
  cur=$(gsettings get "$schema" list 2>/dev/null) || return 0
  case "$cur" in *"'$id'"*) ;; *) return 0 ;; esac
  cur=$(printf '%s' "$cur" | sed "s/, '$id'//; s/'$id', //; s/'$id'//")
  [ "$cur" = "[]" ] && cur="@as []"
  gsettings set "$schema" list "$cur"
  REMOVED=1
}

# remove the "Force" profile from ~/.config/terminator/config (configobj); missing python3-configobj -> no-op
terminator_profile() {
  local cfg="$XDG_CONFIG_HOME/terminator/config"
  [ -f "$cfg" ] || return 0
  if python3 - "$cfg" <<'PY' 2>/dev/null
import sys
try:
    from configobj import ConfigObj
except ImportError:
    sys.exit(1)
cfg = ConfigObj(sys.argv[1], encoding="utf8")
profiles = cfg.setdefault("profiles", {})
if profiles.pop("Force", None) is not None:
    cfg.write()
    sys.exit(0)
sys.exit(1)
PY
  then
    REMOVED=1
  fi
}

# The old client kit: installed before Force ran locally; its files are gone, but this removes any leftovers.
old_kit_cleanup() {
  if have systemctl; then
    systemctl --user disable --now force-clipd.service >/dev/null 2>&1 && REMOVED=1 || true
  fi
  local f
  for f in "$XDG_CONFIG_HOME/systemd/user/force-clipd.service" \
           "$BIN/force-clipd" "$BIN/force-connect" "$CONF_DIR/client.conf"; do
    rm_any "$f"
  done
  if rmdir "$CONF_DIR" 2>/dev/null; then REMOVED=1; fi
}

cleanup() {  # remove the old terminal kit and profiles; silent no-op for anything absent
  local f
  for f in "$APPS"/force-{kitty,tilix,gnome-terminal,terminator,konsole}.desktop \
           "$XDG_CONFIG_HOME/kitty/force.conf" \
           "$HOME/.local/share/konsole/Force.profile" \
           "$HOME/.local/share/konsole/Force.colorscheme" \
           "$HOME/.local/share/konsole/Force.keytab"; do
    rm_any "$f"
  done
  if [ "$DBUS" = 1 ] && have dconf; then
    local dir
    for dir in "/com/gexperts/Tilix/profiles/$TILIX_UUID/" "/org/gnome/terminal/legacy/profiles:/:$GNOME_UUID/"; do
      # dconf reset exits 0 for an absent path too: only count it when the profile had keys
      if [ -n "$(dconf dump "$dir" 2>/dev/null)" ]; then
        dconf reset -f "$dir" >/dev/null 2>&1 && REMOVED=1 || true
      fi
    done
  fi
  if [ "$DBUS" = 1 ] && have gsettings; then
    list_remove com.gexperts.Tilix.ProfilesList "$TILIX_UUID"
    list_remove org.gnome.Terminal.ProfilesList "$GNOME_UUID"
  fi
  terminator_profile
  old_kit_cleanup
  if [ "$REMOVED" = 1 ] && have update-desktop-database; then
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
  fi
  if [ "$REMOVED" = 1 ]; then
    say "removed the old terminal profiles and launchers"
  fi
}

# --- fonts ----------------------------------------------------------------------------------------------
fetch() {  # fetch <url> <sha256> <dest>
  local tmp; tmp=$(mktemp)
  curl -fsSL --retry 2 -o "$tmp" "$1" || { rm -f "$tmp"; die "download failed: $1"; }
  local got
  if command -v sha256sum >/dev/null 2>&1; then got=$(sha256sum "$tmp" | cut -d' ' -f1)
  else got=$(shasum -a 256 "$tmp" | cut -d' ' -f1); fi        # macOS (SPEC §51)
  [ "$got" = "$2" ] || { rm -f "$tmp"; die "checksum mismatch: $1"; }
  mv "$tmp" "$3"
}
# (no grep -q: it quits early, fc-list gets SIGPIPE, and pipefail turns the match into a failure)
has_family() { fc-list : family 2>/dev/null | tr ',' '\n' | grep -xF "$1" >/dev/null; }
install_font() {
  have fc-list || die "fontconfig missing: install fontconfig (fc-list) to add the icon font"
  mkdir -p "$FONTS"
  if has_family "$SYMBOL_FAMILY"; then
    say "fonts: Nerd Font symbols already installed"
    return
  fi
  say "fonts: installing the Nerd Font symbols (icons)"
  local tmp; tmp=$(mktemp -d)
  fetch "$NF_URL" "$NF_SHA" "$tmp/nf.tar.xz"
  tar -xJf "$tmp/nf.tar.xz" -C "$tmp" SymbolsNerdFontMono-Regular.ttf SymbolsNerdFont-Regular.ttf LICENSE
  mv "$tmp/SymbolsNerdFontMono-Regular.ttf" "$tmp/SymbolsNerdFont-Regular.ttf" "$FONTS/"
  mv "$tmp/LICENSE" "$FONTS/LICENSE-NerdFontsSymbols"
  rm -rf "$tmp"
  fc-cache -f "$FONTS" >/dev/null 2>&1 || true
  say "fonts: installed the Nerd Font symbols (icons)"
}

# --- WSL: the terminal is a Windows program that draws with Windows' fonts ---------------------------------
# A Linux font never reaches it. Instead: the symbols font goes into Windows for this user (no admin), and a
# Windows Terminal fragment adds a "Force (<distro>)" profile whose font is Cascadia Mono with the symbols as
# fallback and which starts Force with icons. The user's own profiles and settings are not touched.
is_wsl() {
  [ -n "${WSL_DISTRO_NAME:-}${WSL_INTEROP:-}" ] || [ -e /proc/sys/fs/binfmt_misc/WSLInterop ] \
    || grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null
}
wsl_hint() {
  say "Windows Terminal: for Force's icons, install \"$SYMBOL_FAMILY\" in Windows (from"
  say "https://github.com/ryanoasis/nerd-fonts/releases), add it to your profile's font, then: force settings set icons on"
}
win_exe() {  # win_exe NAME: a Windows program through WSL interop (PATH, else the usual System32)
  command -v "$1" 2>/dev/null || { [ -x "/mnt/c/Windows/System32/$1" ] && echo "/mnt/c/Windows/System32/$1"; }
}
win_localappdata() {  # %LOCALAPPDATA% as a Windows path (run from a Windows folder: cmd.exe refuses a UNC cwd)
  local cmd; cmd=$(win_exe cmd.exe) || return 1
  (cd /mnt/c 2>/dev/null || cd /; "$cmd" /c "echo %LOCALAPPDATA%" 2>/dev/null) | tr -d '\r' | tail -n 1
}
json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
WIN_FONT=SymbolsNerdFontMono-Regular.ttf
WIN_FONT_VALUE="$SYMBOL_FAMILY (TrueType)"
WIN_FONTS_KEY='HKCU\Software\Microsoft\Windows NT\CurrentVersion\Fonts'
windows_terminal() {
  local lad_win lad reg distro fonts frag force_bin tmp
  distro="${WSL_DISTRO_NAME:-}"
  [ -n "$distro" ] && have wslpath || return 1
  lad_win=$(win_localappdata) || return 1
  case "$lad_win" in ""|*%*) return 1 ;; esac
  lad=$(wslpath -u "$lad_win") || return 1
  reg=$(win_exe reg.exe) || return 1
  # 1. the symbols font, installed for this Windows user
  fonts="$lad/Microsoft/Windows/Fonts"
  mkdir -p "$fonts" || return 1
  if [ ! -f "$fonts/$WIN_FONT" ]; then
    say "fonts: installing the Nerd Font symbols in Windows (for this user)"
    tmp=$(mktemp -d)
    fetch "$NF_URL" "$NF_SHA" "$tmp/nf.tar.xz"
    tar -xJf "$tmp/nf.tar.xz" -C "$tmp" "$WIN_FONT" || { rm -rf "$tmp"; return 1; }
    cp "$tmp/$WIN_FONT" "$fonts/$WIN_FONT" || { rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
  fi
  (cd /mnt/c 2>/dev/null || cd /; "$reg" add "$WIN_FONTS_KEY" /v "$WIN_FONT_VALUE" /t REG_SZ \
      /d "$lad_win\\Microsoft\\Windows\\Fonts\\$WIN_FONT" /f) >/dev/null 2>&1 || return 1
  # 2. the "Force" profile, as a Windows Terminal fragment
  frag="$lad/Microsoft/Windows Terminal/Fragments/Force"
  mkdir -p "$frag" || return 1
  force_bin="$HOME/.local/bin/force"
  cat > "$frag/force-$distro.json" <<JSON || return 1
{
  "profiles": [
    {
      "name": "Force ($(json_str "$distro"))",
      "commandline": "wsl.exe -d $(json_str "$distro") --cd ~ -- bash -lc 'exec $(json_str "$force_bin") --glyphs rich'",
      "icon": "⚡",
      "font": { "face": "Cascadia Mono, $SYMBOL_FAMILY" }
    }
  ]
}
JSON
  say "Windows Terminal: restart it once, then open \"Force ($distro)\" from its new-tab menu for Force with icons"
}
windows_uninstall() {  # the fragment and, when no other distro's Force profile is left, the font too
  local lad_win lad reg frag
  lad_win=$(win_localappdata) || return 0
  case "$lad_win" in ""|*%*) return 0 ;; esac
  lad=$(wslpath -u "$lad_win" 2>/dev/null) || return 0
  frag="$lad/Microsoft/Windows Terminal/Fragments/Force"
  rm -f "$frag/force-${WSL_DISTRO_NAME:-}.json"
  rmdir "$frag" 2>/dev/null || return 0                 # another distro still has its profile: keep the font
  rm -f "$lad/Microsoft/Windows/Fonts/$WIN_FONT" 2>/dev/null || true
  reg=$(win_exe reg.exe) || return 0
  (cd /mnt/c 2>/dev/null || cd /; "$reg" delete "$WIN_FONTS_KEY" /v "$WIN_FONT_VALUE" /f) >/dev/null 2>&1 || true
}

# --- main -----------------------------------------------------------------------------------------------
if is_wsl && [ "$CLEANUP_ONLY" = 0 ]; then
  if [ "$UNINSTALL" = 1 ]; then
    windows_uninstall
  elif ! windows_terminal; then
    wsl_hint
    exit 3                                     # setup reports it; the plain symbols still work everywhere
  fi
fi
if [ "$CLEANUP_ONLY" = 1 ]; then
  cleanup
  exit 0
fi
if [ "$UNINSTALL" = 1 ]; then
  cleanup
  if [ -e "$FONTS" ] || [ -L "$FONTS" ]; then rm -rf "$FONTS"; REMOVED=1; fi
  have fc-cache && fc-cache -f "$(dirname "$FONTS")" >/dev/null 2>&1 || true
  exit 0
fi
is_wsl || install_font                       # on WSL a Linux font would never reach the terminal
cleanup
