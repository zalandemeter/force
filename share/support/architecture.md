# Force: a map of the code

Read this before the source. `SPEC.md` (the design, ~1100 lines) has the details; the code cites it as
`SPEC §N`.

## The processes

```
your terminal ── tmux client
                   │
   split layout:   ├─ ui tmux server  (socket force-ui, share/tmux-ui.conf): one ui session per terminal
                   │     main pane: `force nested NAME` = a nested client of the host server
                   │     sidebar pane: force-sidebar (Rust)   header pane: force-header (Rust)
                   │
                   └─ host tmux server (socket force, share/tmux.conf; systemd unit force-tmux.service)
                         one tmux session per Force session; its pane runs `force run NAME`, which execs
                         Claude Code, OpenCode or the user's shell
force daemon (systemd unit force.service; without systemd started by `force`):
   every second reads tmux + the agents' status files, computes each session's state, writes the snapshot,
   restarts crashed sessions, sends notifications, watches remote hosts (`ssh HOST force watch`)
classic layout: no ui server; sidebar and header panes live next to the agent pane on the host server
```

## Where state lives

| Path | What |
|---|---|
| `~/.local/share/force/` | `registry.json` (the managed sessions), `archive/`, `releases/` + `app` (the live release), `support/` (Chuck) |
| `~/.local/state/force/` | `snapshot.json` (what the sidebar draws), `seen.json`, `header/`, `daemon.lock`, `daemon.log`, `usage.json`, `remotes/`, `ssh/` |
| `~/.config/force/` | `settings.json` (only what the user changed), `hosts.json` (remote hosts) |
| `~/.local/bin/force` | the launcher: runs `<app>/force/cli.py` with Force's Python |
| `~/.claude/` | Claude Code's own files (sessions/, projects/): read, never written by Force |

A development instance (`FORCE_INSTANCE=dev`, `./dev.sh`) has the same layout under `force-dev` names and
sockets `force-dev` / `force-dev-ui` (SPEC §2.1).

## Python package `force/` (standard library only)

| Module | What |
|---|---|
| `cli.py` | every `force` command (argparse), attach and the ui sessions (split layout), host start without systemd |
| `daemon.py` | the supervisor loop: states, snapshot, crash restarts, upgrades, notifications, reload, remote watchers |
| `state.py` | the state machine (working, needs you, your turn, idle, stale, down…) and the snapshot format |
| `lifecycle.py` | create, restart, rename, archive, delete, move a session (shared by CLI, daemon, dialogs) |
| `kinds.py` | one class per session kind: Claude Code, OpenCode, shell (launch argv, status reads, cards) |
| `claude.py` / `opencode.py` | facts about each agent: argv, status files / API, transcripts, versions |
| `registry.py` | `registry.json`, with a lock |
| `tmux.py` | a thin wrapper over the tmux CLI |
| `config.py` | paths (env overrides, instances) and the settings table |
| `remote.py` | remote hosts: hosts.json, ssh options, `force watch`, the watchers |
| `automation.py`, `report.py` | `force prompt`, `wait`, `read`, `keys`, `status`, `explain`, reported states |
| `doctor.py` | `force doctor [--fix]` |
| `updates.py`, `setup.py` | `force update` and the hourly check; the install/setup steps (launcher, units, PATH, fonts) |
| `menus.py` | generates `share/menu-*.tmux` and `share/copy-mode.tmux` |
| `support.py`, `mcp.py` | Chuck: his session and folder, issue search and links, fix mode; Force's MCP server |
| `smoke.py` | `force smoke`: a self-test against the real agents |
| `usage.py`, `activity.py`, `history.py`, `importer.py`, `share.py`, `opencode_update.py` | plan limits, activity text, history view, `force import`, sharing (parked), managed OpenCode |

## Rust

| Crate | Binaries | What |
|---|---|---|
| `crates/sidebar` | `force-sidebar`, `force-header`, `force-dialog` | the sidebar (reads the snapshot; ratatui), the header bar, the popup dialogs (new, rename, move, archive, delete, jump, archived) |
| `crates/intro` | `force-intro` | the startup intro film |

## `share/` (shipped with every release)

tmux configs (`tmux.conf`, `tmux-ui.conf`, generated menus), the scripts the keys and menus run
(`ui-key.sh`, `switched.sh`, `settle.sh`, `tier.sh`, `presize.sh`, `fit.sh`, `sidebar-toggle.sh`,
`doctor-popup.sh`, `clip-set.sh`), `statusline.py` (Claude's plan usage), `fonts.sh` (the icon font and the
Windows Terminal profile), `get-force.sh` (the installer template), `skill.md` (the guide for agents that drive
Force), `settings.schema.json`, `support/` (Chuck's context pack).

## Tests and tools

- `tests/` (pytest): tmux servers named `forcetest-*`, never the live sockets; `cargo test` per crate.
- `./dev.sh`: the checkout as a separate `force-dev`. `scripts/try-release.sh`: the real installer in a fresh
  container. `scripts/devbox.sh`: everything in the development image.
