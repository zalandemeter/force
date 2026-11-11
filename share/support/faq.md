# Force: frequent questions and known limits

## Install and update

- **Supported systems.** Linux on x86-64. Windows through WSL (the installer sets up a Windows Terminal profile,
  "Force (DISTRO)", with the icon font; it needs Windows Terminal 1.21 or newer). macOS is not supported yet.
- **Root.** Never needed. Force installs as the user under `~/.local`.
- **Python.** Force needs Python 3.11+; when the system has none, the installer brings its own.
- **Update.** `force update`; a daily check shows a newer version in the sidebar footer (setting
  `checkForUpdates`). Sessions keep running through an update.
- **Uninstall.** `force uninstall` (conversations stay); `--purge` also removes Force's registry, state and
  settings.

## Sessions

- **Kinds.** Claude Code, OpenCode, and plain shells. Codex is not supported yet.
- **Closing the terminal** does not stop anything: sessions live in tmux on the machine. `force` brings the
  cockpit back. Quit with the menu's Quit (q) or the × in the top bar.
- **After a reboot** every session comes back with its conversation (the systemd units; without systemd, on the
  next `force`).
- **A crashed session** restarts by itself up to `crashRestartMax` times in `crashWindowMinutes`.
- **Names.** A session started without a name is named after its first message.
- **Why is it in that state?** `force explain NAME`.
- **States:** working, needs you (a permission prompt or a question), your turn (finished), idle, stale (idle
  longer than `staleAfterHours`), down, shell.
- **Taking over existing agent sessions:** `force import` lists them, `force import ID` takes one over.

## Remote hosts

- `force host add NAME [SSH]` adds a machine reachable with ssh **without a password** (a key). Force is
  installed there if missing. The header shows other hosts' sessions that need you; `h` in the menu switches.
- A host shown as "version" runs another Force version: `force host upgrade NAME`.
- Remote hosts need the split layout (`force ui split`).

## Notifications

- `force settings set notifyCommand 'COMMAND'`: run when a session needs you, finishes or goes down, with
  `$FORCE_MESSAGE`, `$FORCE_EVENT`, `$FORCE_SESSION` and more in its environment. Example with ntfy:
  `curl -d "$FORCE_MESSAGE" ntfy.sh/TOPIC`. Test it: `force notify-test`.
- A session a terminal is showing is not notified unless `notifyWhenViewed` is on.

## The look

- **Icons look wrong** (boxes or question marks): the terminal lacks the Nerd Font symbols. `force fonts`
  installs Symbols Nerd Font Mono; terminals that cannot use it get the plain tier (`force settings set icons
  off`).
- **Layouts:** split (default; sidebar and header drawn per terminal) or classic (one tmux): `force ui split|classic`.
- **Phone:** over ssh at phone width the sidebar becomes the whole screen.

## Known limits

- Linux x86-64 only for release builds (aarch64 later).
- Tilix and Terminator keep Alt+number for their own panes.
- Session sharing with other users is parked (setting `sharing`).

## Getting help

- `force doctor --fix` repairs the most common problems.
- Bugs and ideas: https://github.com/zalandemeter/force/issues (Chuck drafts them for you).
- Security problems: e-mail hello@useforce.cc, never a public issue.
