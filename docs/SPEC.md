# Force — the code cockpit

Force keeps a set of named agent sessions running on a server, independent of any SSH login, and gives
one terminal ("the cockpit") that shows all of them with a sidebar and a header, and switches between
them without losing any agent TUI feature.

This file is the source of truth for behaviour. Where code and spec disagree, the spec wins unless it is
changed first.

## 1. Architecture

```
laptop ── ssh -t devbox force ──► tmux client ──► tmux server  (socket -L force, systemd user unit force-tmux.service)
                                                   ├─ tmux session "portal"   window 0: [sidebar pane | agent pane]
                                                   ├─ tmux session "license"  window 0: [sidebar pane | agent pane]
                                                   └─ ...
force daemon (systemd user unit force.service) ── polls tmux + ~/.claude/sessions/*.json every second,
            writes snapshot.json + header files, restores/restarts/upgrades sessions
```

- One tmux session per managed agent session. tmux session name == force name == agent session
  name (`-n`) == Remote Control name (`--remote-control`).
- Each tmux session has exactly one window with two panes: the **sidebar** pane on the left
  (`force sidebar`, fixed width) and the **agent** pane on the right (`force run <name>`, which execs
  `claude`). Panes are identified by the pane user option `@force_role` = `sidebar` | `agent`.
- Switching sessions = `tmux switch-client -t <name>`. All windows have the same layout, so the
  agent pane is not resized by switching.
- The header is a pane at the top of each session window, rendered by the Rust `force-header` binary
  (§16, §22); the tmux status line is off.
- Lingering is enabled for the user (`loginctl enable-linger`), so both units start at boot.

## 2. Paths and configuration

All paths are overridable by environment variables (needed for tests). Defaults (the internals use the
name `force`: Python package, `force-*` binaries, `@force_*` tmux options):

| Name | Env var | Default |
|---|---|---|
| data dir (releases, registry, archive) | `FORCE_HOME` | `~/.local/share/force` |
| state dir (snapshot, seen, headers, lock, log) | `FORCE_STATE` | `~/.local/state/force` |
| Claude config dir | `FORCE_CLAUDE_DIR` | `~/.claude` |
| claude launcher | `FORCE_CLAUDE_BIN` | `~/.local/bin/claude`, else `claude` on PATH |
| host tmux socket (`tmux -L`) | `FORCE_TMUX_SOCKET` | `force` |
| ui tmux socket (split layout) | `FORCE_UI_SOCKET` | `force-ui` |
| tmux config file | `FORCE_TMUX_CONF` | `<release>/share/tmux.conf` |
| settings file | `FORCE_CONFIG` | `~/.config/force/settings.json` (hosts: `hosts.json` next to it) |

Settings (JSON, camelCase, only what the user changed; one table `force/config.py` SETTINGS drives loading,
validation, `force settings` and `share/settings.schema.json`). A bad value warns and falls back to its
default; an invalid file is never overwritten by `force settings set`.

| key | default | meaning |
|---|---|---|
| `animations` | `"full"` | full / subtle / off (§23) |
| `intro` | true | startup intro on attach (§31) |
| `icons` | `"auto"` | auto / on / off: the glyph tier (§28; field `glyphs`) |
| `sidebarWidth` | 29 | sidebar width in columns |
| `staleAfterHours` | 12 | idle longer than this → STALE |
| `unreadReplies` | true | a working session's card turns blue on a finding you have not seen (§5 `unread`) |
| `autoUpgradeSessions` | true | restart idle, outdated sessions on the new release (§5) |
| `autoUpgradeIdleMinutes` | 10 | idle time before that |
| `layout` | `"split"` | split / classic (§39) |
| `opencodeManaged` | false | Force's own OpenCode copy (§36) |
| `checkForUpdates` | true | hourly check for a newer Force (§46) |

Advanced, accepted but not listed by `force settings` (only `--all`): `crashRestartMax` 3,
`crashWindowMinutes` 10, `startGraceSeconds` 30, `richTerminals`, `introCellAspect`, `reloadOnChange`,
`sharing`.

### 2.1 Instances (development)

`FORCE_INSTANCE=<i>` (`^[a-z][a-z0-9]{0,15}$`; unset or empty = the live install; anything else: every command
exits 2) runs a second, separate Force beside the live one. Its defaults become `~/.local/share/force-<i>`,
`~/.local/state/force-<i>`, `~/.config/force-<i>/settings.json` and the sockets `force-<i>` / `force-<i>-ui`
(explicit variables still win); the Claude config dir is shared. `FORCE_BIN` names the launcher that runs it
(default `~/.local/bin/force`): `tmux.conf` / `tmux-ui.conf` set it in the server's global environment when the
starting process had none, the menus run `$FORCE_BIN`, the scripts `${FORCE_BIN:-…}`, the header the same.

An instance never touches what the live install owns: no systemd (it starts its tmux server, with `@force_app`
and `@force_state` set, and its daemon itself, §1), and `force update`, `uninstall`, `fonts`, `terminals` and
`force.setup` refuse (exit 2). `force doctor` checks the units only where systemd is used. `./dev.sh` builds the
checkout into the `dev` instance (`deploy.sh` with `FORCE_INSTANCE`) and writes its launcher
`~/.local/bin/force-dev`; `./dev.sh stop` stops its servers and daemon (the pid the daemon writes into
`daemon.lock`), `./dev.sh reset` also deletes its four paths.

## 3. Registry (`$FORCE_HOME/registry.json`)

```json
{
  "version": 1,
  "sessions": [
    {
      "name": "portal",
      "session_id": "94dfcb67-2805-4385-be2a-090a17d98fca",
      "cwd": "/home/alice",
      "created_at": "…",
      "order": 3,
      "archived": false,
      "archived_at": null,
      "pending_name": null
    }
  ]
}
```

- **name**: `^[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?$` (≤ 31 chars, no trailing `-`). Unique across all entries, archived included.
  `slugify(s)`: lowercase, every run of characters outside `[a-z0-9]` → one `-`, strip leading and
  trailing `-`, truncate to 31 chars (then strip trailing `-` again); empty result → `None`.
- **session_id**: Claude conversation UUID (lowercase, 36 chars). Updated when Claude changes it
  (`/clear` starts a new id in the same process).
- **order**: integer for display order; new sessions get `max(order)+1` (0 if none).
- **pending_name**: set by `force rename` while the session is live; the daemon applies it (§6.5).
- Writes are atomic (write `registry.json.tmp` in the same dir, `fsync`, `os.replace`) and done
  under an exclusive `fcntl.flock` on `$FORCE_HOME/registry.lock`. Every read-modify-write holds the
  lock for the whole cycle. Readers that only display may read without the lock.
- Missing file → empty registry. Corrupt JSON → raise `RegistryError` (never silently overwrite);
  the daemon logs it and keeps running without touching sessions.
- Unknown keys on entries are preserved on rewrite.

## 4. Claude integration facts (verified on 2.1.292)

- Every interactive Claude process writes `$FORCE_CLAUDE_DIR/sessions/<pid>.json` with at least
  `pid, sessionId, cwd, version, name, nameSource, status, statusUpdatedAt` and optionally
  `waitingFor`. `status` ∈ `busy` (working) | `waiting` (blocked on the user: permission,
  question, plan approval; `waitingFor` e.g. `"input needed"`) | `idle`. `statusUpdatedAt` is epoch
  ms. The file is removed on clean exit. A file may exist for a dead pid (crash) — always check
  the pid is alive and its `/proc/<pid>/exe` points into `$FORCE_CLAUDE_DIR/../.local/share/claude/versions/`
  (in practice: the exe basename is a version string like `2.1.292`).
- `claude --session-id <uuid> -n <name> --remote-control <name>` starts a new conversation with that
  id. `claude --resume <uuid> -n <name> --remote-control <name>` continues it in place (same id,
  same Remote Control URL); `-n` overrides the stored name.
- `/clear` keeps the process but writes a new `sessionId` into the same `<pid>.json`.
- SIGTERM → clean exit, transcript complete, `<pid>.json` removed.
- Pressing `←` on an empty prompt moves the session into Claude's own background daemon, out of
  our pane. Prevent it: every launch passes `--settings '{"disableAgentView":true}'`.
- Transcript of a session: `$FORCE_CLAUDE_DIR/projects/<project-dir>/<session_id>.jsonl`, plus an
  optional sibling directory `<project-dir>/<session_id>/` (subagents, tool results). The
  project-dir name is not needed: search `projects/*/<session_id>.jsonl`.
- Running version of a process: basename of `os.readlink(f"/proc/{pid}/exe")`. Latest installed
  version: basename of `os.path.realpath(FORCE_CLAUDE_BIN)`. Outdated ⇔ both known and different.

## 5. Session states (pure function, `force/state.py`)

Inputs per session: registry entry, pane info (tmux session exists? agent pane alive? pane pid,
pane start time, pane dead status), Claude status file (or none), `last_seen_ms`,
`turn_done_ms`, `now_ms`, config. Evaluate in this order; first match wins:

| state | glyph | colour | rule |
|---|---|---|---|
| ARCHIVED | – | dim | `archived` |
| DOWN | `✖` (`×` in the plain tier) | red | no tmux session, or agent pane dead, or no live status file and pane older than `start_grace_seconds` |
| STARTING | `…` | dim | pane alive, no live status file yet, pane younger than grace |
| NEEDS_YOU | `●` | bold red | status `waiting` |
| WORKING | spinner `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` (frame = `now_ms // 100 % 10`) | yellow | status `busy` |
| YOUR_TURN | `●` | green | status `idle` and `turn_done_ms > last_seen_ms` |
| STALE | `◌` | dim | status `idle` and `now_ms - statusUpdatedAt > stale_hours` |
| IDLE | `○` | grey | status `idle` |
| UNKNOWN | `?` | dim | any other status string |

- `outdated` is a separate boolean flag (shown as cyan `↑`), not a state.
- `unread` is a separate boolean flag, not a state: the session is WORKING, not viewed by any client, and its
  agent wrote you a **finding** since you last looked at it (`reply_ms > last_seen_ms`). Claude Code narrates
  between tool calls ("Let me check the config…"; median 118 characters, 95 % under 200): narration never
  counts, or every working session off screen would turn blue within seconds.
  - **Finding** (`activity.is_finding`): a text block of at least 300 characters (`FINDING_CHARS`), or of at
    least 100 (`FINDING_STRUCTURED_CHARS`) with two list items, a heading, a table (two `|…|` rows) or a code
    block.
  - **Mid-turn**: a tool call follows it before the next prompt. A finding that ends the turn is its answer,
    which YOUR_TURN already shows.
  - **Not a plan** (`activity.is_plan`): mid-turn text whose first sentence announces the next steps ("I'll make it…",
    "Let me…", "Going with A.", "Building that split view now.", also after "Got it." / "Good,") is narration however
    long, unless it asks you something (`?`). Measured on 355 real mid-turn findings: it removed 24, all plans. The
    reply that ends a turn into watching is judged by length and structure alone (it is the agent's answer).
  - `reply_ms`, Claude Code: the `timestamp` of the newest such text block in the transcript's main thread
    (sidechains and API error records do not count; tool results are not prompts). OpenCode: the tick on which
    the count of such text parts in the last assistant message (a text part followed by a tool part; read while
    busy, §36) changed, never its first sight after a (re)start. Shells have none.
  - The daemon keeps the newest `reply_ms` per `session_id` in memory and drops it when the session leaves the
    registry, so a session created again under a deleted one's name starts without it. With `unreadReplies` off
    the flag is never set; opening the session clears it on the next tick.
  - The sidebar draws the whole card blue: icon and edge `#5c9cf5`, the line-2 activity (or the "working" label)
    `#5b8fd6` (about 4.6:1 on the card, like the working text). The spinner keeps turning; name and age stay
    grey.
- `watching` is a separate boolean flag, not a state: a Claude Code session whose turn is over while background
  work of its own still runs, work that ends by itself and after which the agent resumes on its own: a watcher
  loop, a CI watch, a long build or test run, a script, a background subagent, a Monitor. It is WORKING for
  everything that acts on states (`force wait`, `--until`, notifications, the §43 refusals, `force list` counts);
  only the cockpit shows it apart, because nothing happens there for minutes or hours.
  - **Background tasks** come from the transcript tail (`activity.scan`): a `tool_use` with
    `input.run_in_background` true (Bash, Agent) or named `Monitor`, with its `description` (Bash/Agent/Monitor
    all carry one) and, for Bash, its `command`. It has ended when a later user record's text holds
    `<tool-use-id>ID</tool-use-id>` with a `<status>` of completed, failed, killed, stopped or expired (a Monitor's
    event notices carry other words and do not end it). A task launched before this Claude process started (the
    pane's start) or more than 24 h ago does not count (it died with an earlier process, or its notice was lost).
    A Bash task counts as running only while the status is `shell` (Claude's own word that a shell runs).
  - **Endless commands** (`activity.is_endless(command)`, a pattern list) never make a session watching: servers
    and followers that run until stopped (`npm|pnpm|yarn|bun run dev|start|serve`, `vite` but not `vite build`,
    `next dev`, `uvicorn`, `gunicorn`, `flask run`, `manage.py runserver`, `python -m http.server`,
    `jupyter`, `docker compose up` / `docker-compose up` without `-d`/`--detach`, `tail -f|-F`, `journalctl -f`,
    `kubectl logs -f`, `cargo watch`, `--watch` of a builder (`tsc`, `webpack`, `jest`, `vitest`, `nodemon`) but
    not of `gh`). Everything else ends by itself and counts.
  - **Effective status**: status `shell` or `idle` with at least one counting task -> treated as `shell`: WORKING
    with `watching`; status `shell` without one (only endless commands, or a Bash task whose notice came) ->
    treated as `idle` (your turn or idle, by the usual rules, with the turn-finished transition as for idle).
    Status `shell` with no background record in the transcript tail at all (launched long ago) stays watching,
    described as "a background command".
  - Sidebar card: it asks nothing of you, so it looks idle: a still icon instead of the spinner (RICH: the
    hourglass U+F252, PLAIN `◷`) in idle grey `#8a8a8a`, the idle edge, line 2 `waiting · <description of the
    newest counting task>` (`+N` when there are more) in the same grey. Only the line-2 glow of working cards
    still sweeps over it, in a neutral `#d0d0d0` instead of the warm working highlight. The age counts from the
    status change. The rail draws the same icon. An unread finding (`unread`, blue) wins over the colours.
  - The reply that ends a turn into watching counts as a finding for `unread` (it is not a YOUR_TURN, so nothing
    else would show it): `reply_ms` also takes the newest finding of the last turn (`end_reply_ms`).
  - Header: `⠋ N working` counts only the sessions that are WORKING and not watching.
  - Snapshot `"watching": true|false`; `force list --json` carries it. OpenCode and shell sessions are never
    watching.
- `since_ms` (for the age column) = `statusUpdatedAt` when a status file exists, else pane start.
- **turn_done_ms**: the daemon records `now` when it observes a session go from `busy`/`waiting`
  to `idle` (not on first sight, not after a (re)start). Persisted in `$FORCE_STATE/seen.json`.
- **last_seen_ms**: every tick, each tmux session that has at least one attached client
  (`list-clients -F '#{client_session}'`) gets `last_seen_ms = now`. Persisted in `seen.json`.
  Consequence: the session you are looking at never shows YOUR_TURN.
- `seen.json`: `{"<name>": {"last_seen_ms": int, "turn_done_ms": int}}`, atomic writes, entries
  renamed along with sessions, dropped for names no longer in the registry.

## 6. Daemon (`force daemon`)

Single instance: exclusive non-blocking `flock` on `$FORCE_STATE/daemon.lock`; if taken, print
"force daemon already running" and exit 1. Logs to stderr (journald under systemd). Tick every 1 s.
Each step catches and logs its own exceptions; one bad session never stops the loop.

1. **tmux server**: if no server is running: under systemd (`INVOCATION_ID` set) run
   `systemctl --user start force-tmux.service` (the server must never live in force.service's cgroup);
   if still none (or not under systemd), `tmux -L $SOCK -f $CONF start-server`. Once per server,
   set the global option `@force_state` to `$FORCE_STATE` (tmux.conf reads header files from there).
2. **Restore**: for every non-archived registry entry without a tmux session → create it (§7) and
   log `started <name>`.
3. **Follow the agent**: for each live agent pane, read `sessions/<pane_pid>.json` (pane pid == claude
   pid because `force run` execs claude).
   - `sessionId` differs from registry → update registry `session_id`.
   - Name adoption: if `pending_name` is null and the agent's `name` ≠ registry name and
     `nameSource == "user"`: `new = slugify(name)`; if `new` is valid and unused → rename (registry,
     tmux session, seen.json); otherwise ignore. Adoption takes only a change of the name in Claude's
     status file (§49.6).
4. **Crash restart**: tmux can report a pane dead before its exit status is known (both empty):
   then wait, re-check on later ticks, and treat it as a crash only if still unknown after 5 s.
   agent pane dead with non-zero exit status or killed by a signal, and the
   death was not caused by force → respawn it (§7, resume), at most `crash_restart_max` times per
   `crash_window_minutes` per session; after that leave it DOWN and log it. Exit status 0 (user
   typed `/exit`) → leave it DOWN. Deaths caused by force (upgrade, rename, restart, archive) are
   marked in memory before force sends the signal.
   **Trust declined**: a dead Claude pane whose screen still shows the "trust this folder?" question
   (`TRUST_MARKERS`) was answered "No, exit" (exit 1) or Esc (exit 0): never restarted, it would only ask
   again. A session that has no conversation yet (no transcript: just created there) is deleted (§20) and
   logged; any other (moved to a folder Claude does not trust yet, §7.7; Chuck, §50) stays DOWN with the
   activity "folder not trusted" until it is restarted, moved back or deleted.
5. **Auto-upgrade** (every 30 s, if `auto_upgrade`): session is outdated, state IDLE / YOUR_TURN /
   STALE, `now - statusUpdatedAt ≥ upgrade_idle_minutes`, and no client attached to its tmux
   session → restart it (§7.3). Never touch WORKING, NEEDS_YOU, STARTING.
6. **Pending rename**: `pending_name` set → as soon as the session is not WORKING / NEEDS_YOU:
   rename tmux session + seen.json key, set `name = pending_name`, `pending_name = null`, restart
   (§7.3) so `-n` and `--remote-control` carry the new name. Down sessions: rename only.
7. **Seen tracking** (§5) and **notifications**: when a session newly enters NEEDS_YOU or YOUR_TURN
   and is not the session a client is viewing, run
   `tmux display-message -c <client> -d 4000 "<glyph> <name>: needs you|finished"` for each client.
8. **Outputs** (§9): `snapshot.json` and header files.
9. **Switch targets** for the tmux bindings (only when changed since the last tick): global server
   options `@force_idx_1` … `@force_idx_9` = name of the live session at that index (unset with
   `set -gu` when there is none), and per tmux session options `@force_next` / `@force_prev` = the
   next / previous live session in index order, wrapping around.
10. **Sidebar presence**: if `@force_sidebar` is on, every live session has a sidebar pane
    (`ensure_sidebar`); a dead sidebar pane is respawned at most once per 10 s per session. Only
    panes with `@force_role sidebar` are ever touched by this step.
11. **Live reload** (config `dev_reload`, default true): every tick the daemon fingerprints the
    `*.py` files of its package directory (through the unresolved `app` symlink, plus the symlink
    target itself) and `tmux.conf`. After a change has been stable for 2 s it validates: the new code
    must import in a fresh interpreter, and `tmux source-file -n` must parse the config. Failure →
    toast "force: reload skipped — <error>", nothing applied, the broken state is not re-validated.
    Success → `source-file` the config if it changed; if code changed respawn every sidebar pane
    (`respawn-pane -k`, sidebars only), then `os.execv` the daemon (the lock fd is close-on-exec).
    Agent panes are never touched.

## 7. Session lifecycle (`force/tmux.py` + `force/lifecycle.py`)

### 7.1 Create
```
tmux -L S new-session -d -s NAME -x W -y H -c CWD -e FORCE_SESSION=NAME "force run NAME"
tmux -L S set-option -p -t NAME:0.0 @force_role agent
tmux -L S set-option -t NAME remain-on-exit on          (window option; dead pane stays visible)
if sidebar enabled: tmux -L S split-window -h -b -l SIDEBAR_W -t NAME:0.0 -c CWD "force sidebar" ; tag @force_role sidebar
tmux -L S select-pane -t <agent pane>
```
- `W x H`: size of the most recently active client if any, else 200x50.
- `force run` re-executes itself through `bash -lic` (§7.2) so the pane environment is the same as an
  SSH login shell (PATH, tokens), because the tmux server is started by systemd with a minimal
  environment. `force` here is the absolute
  path of the installed launcher (`shutil.which("force")` at daemon start, fallback
  `~/.local/bin/force`; §33). NAME and paths are quoted with `shlex.quote`.
- The global option `@force_sidebar` (`on`/`off`, default `on`) decides whether sidebars exist.

### 7.2 `force run NAME` (pane command)
Two stages. **Stage 1** (`FORCE_RUN_STAGE` unset): remove every inherited variable named
`CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_EFFORT` or starting with `CLAUDE_CODE_` (a running Claude
exports them to its children; inherited, they mark the new claude as a child session with transcript
saving off and point it at another session's messaging socket and Remote Control bridge), set
`FORCE_RUN_STAGE=2` and exec `bash -lic 'exec force run NAME'`. Variables the user's shell rc sets are
kept, because the rc runs after the scrub. **Stage 2**: load registry entry NAME (missing or archived → print error, sleep 5 s, exit 2). `chdir(cwd)`
(missing dir → error, exit 2). Build argv and `os.execvp`:
- transcript exists for `session_id` → `[CLAUDE, "--resume", id, "-n", NAME, "--remote-control", NAME, "--settings", '{"disableAgentView":true}']`
- otherwise → same with `"--session-id", id` instead of `"--resume", id`.
- an entry with `remote_control: false` (§53) gets no `"--remote-control", NAME`.

`CLAUDE` = `FORCE_CLAUDE_BIN`. Export `FORCE_SESSION=NAME`.

### 7.3 Restart (used by upgrade, rename, manual restart)
Mark "force-initiated" for the pane; send SIGTERM to the agent pid; wait up to 15 s for the pane to
die (poll `#{pane_dead}`); if still alive send SIGKILL and wait 5 s; then
`tmux respawn-pane -t <agent pane> "force run NAME"` (the pane is dead, so no `-k`). A shell session
gets SIGHUP instead of SIGTERM (interactive bash ignores SIGTERM and used to sit out the 15 s).
`restart --all`: agent sessions only (shells are left alone); idle ones are restarted
together (`Cockpit.restart_many`: signal all, respawn each as it dies, one shared timeout), not one by
one; WORKING / NEEDS_YOU ones get `pending_restart` and the daemon restarts them once idle, the
sidebar saying "restarts after this turn" meanwhile. A restarted card shows "restarting" in place of its
state label until the agent is back, then "restarted" for its first 10 s (snapshot `note`; from the kill
marker, which records the time and the pid force stopped).

### 7.4 Archive (`force archive NAME`)
Restart-style stop (SIGTERM → wait → SIGKILL) without respawn, `tmux kill-session -t NAME`, copy
the transcript `.jsonl` and sibling dir (if present) to
`$FORCE_HOME/archive/<session_id>/<project-dir>/` (copy, don't move; `shutil.copy2` / `copytree`
with `dirs_exist_ok`), set `archived=true`, `archived_at=now`.
While it runs the entry carries `extra.archiving = true` (set before the stop, cleared with `archived=true`
under the same lock, or on failure): the card's label reads "archiving", not the "restarting" feedback the
stop's kill mark would show.

### 7.5 Unarchive (`force unarchive NAME`)
For each file under `$FORCE_HOME/archive/<session_id>/`, copy it back to
`$FORCE_CLAUDE_DIR/projects/...` only if the target is missing. Set `archived=false`,
`archived_at=null`, `order = max(order)+1`. The daemon (or the CLI directly, if the daemon is not
running) creates the tmux session.

### 7.6 Sidebar toggle
`force sidebar-toggle`: flip `@force_sidebar`; `off` → kill every pane with `@force_role sidebar`;
`on` → add one to every session that lacks it (7.1 split command).

### 7.7 Move to another folder (`force move NAME DIR`)
The session continues in DIR: same conversation, same memory, DIR's CLAUDE.md / AGENTS.md (and its
project settings) from the restart on. `force move NAME up|down` still reorders; any other argument is
a folder (the Move popup: `f` in the Force menu; it always passes an absolute path).

| Kind | What a move does |
|---|---|
| Claude | the transcript and its sibling dir move to the new folder's project directory (the session is stopped first), so Claude's own `/resume` picker there lists it; `--resume <id>` then runs in its own project. Auto memory is per folder, so the first move pins `extra.memory_dir` to the memory the session used so far (next to its transcript) and `run` passes it as `autoMemoryDirectory` in `--settings`; later moves keep it. Skipped when the user's settings already set `autoMemoryDirectory`. The memory is not moved: the old folder's other sessions share it. (Without the move, a never-used session's metadata-only transcript fails `--resume` from another folder with "No conversation found", and `--session-id` says "already in use".) |
| OpenCode | it finds a session only in the project of the folder it runs in: stop it, `opencode export <id>` in the old folder, `opencode import <file>` in the new one (same id; project and directory updated), restart. A failure restarts it in the old folder. |
| Shell | a fresh shell in DIR (the popup warns: what ran in the old shell ends). |

A busy session (working / needs you) gets `extra.pending_move`; the daemon moves it once idle and its card
says "moves after this turn". While moving, `extra.moving` makes the card say "moving", then "moved" for
the restart's first seconds (`extra.moved_at`). A folder Claude has not been trusted in asks its trust
question in the pane, as for a new session there; declining it leaves the moved session down, never deleted
(§6.4).

## 8. CLI (`force/cli.py`, argparse, entry point `force`)

Exit 0 on success, 1 on user error with a one-line message on stderr, 2 on environment error.

| command | behaviour |
|---|---|
| `force` / `force attach [NAME]` | ensure services: if `systemctl --user is-active force.service` is not active → `systemctl --user start force-tmux.service force.service`; wait ≤10 s for the tmux server. If `$TMUX` points at the force socket → error "already inside force". If no non-archived sessions → run `force new` interactively first. Then `os.execvp tmux -L S attach -t TARGET` where TARGET = NAME, else the session with the highest `last_seen_ms`, else lowest `order`. |
| `force new [NAME] [--cwd DIR] [--no-switch] [--no-remote-control]` | `--no-remote-control`: §53. NAME omitted → prompt on the tty (`input()`); validate (§3), cwd must exist (default: `$HOME`), add registry entry with a fresh uuid4, create the tmux session (§7.1) immediately; if run inside force and not `--no-switch` → `switch-client -t NAME`. |
| `force rename OLD NEW` | validate; down/archived → rename registry entry + tmux session + seen now; live → set `pending_name` (daemon applies, §6.6). |
| `force archive NAME` | §7.4. Refuses if state is WORKING or NEEDS_YOU unless `--force`. Switches attached clients that view NAME to another live session first. |
| `force unarchive NAME` | §7.5. |
| `force restart NAME` | §7.3 (works on DOWN sessions: just respawn/create). |
| `force upgrade [NAME \| --all]` | restart outdated sessions now if not WORKING / NEEDS_YOU; print what was done/skipped. |
| `force list [--all] [--json]` | table from the snapshot (falls back to registry + live computation if the daemon is not running): index, glyph, name, state, age, version, `↑`. `--all` includes archived. |
| `force switch NAME \| --index N \| --next \| --prev` | `switch-client` of the calling client (`-c $TMUX client` if run from a binding via `run-shell`, which passes `#{client_name}` as `--client`). Index = 1-based position in the sidebar order of non-archived sessions. next/prev wrap around. |
| `force forget NAME` | only for archived entries: remove from the registry (archive copy is kept). |
| `force import [--dry-run] [--pid PID --name NAME]` | migration (§11). |
| `force sidebar-toggle` | §7.6 |
| `force daemon` | §6 |
| `force run NAME` | §7.2 (internal) |
| `force sidebar` | §10 (internal) |
| `force dialog new\|rename NAME\|archive NAME\|archived` | small interactive dialogs used inside `tmux display-popup` (§10). |

## 9. Outputs

`$FORCE_STATE/snapshot.json` (atomic write every tick):
```json
{"generated_ms": 0, "latest_version": "2.1.292",
 "counts": {"needs_you": 1, "working": 2, "your_turn": 1, "idle": 3, "stale": 1, "down": 0},
 "sessions": [{"name": "portal", "index": 1, "state": "NEEDS_YOU", "since_ms": 0, "version": "2.1.289",
               "outdated": true, "archived": false, "order": 3, "cwd": "/home/alice",
               "session_id": "…", "pid": 188564, "waiting_for": "input needed", "viewed": false,
               "unread": false}]}
```
Archived sessions are included with `state: "ARCHIVED"` and `index: null`.

Header: the 2-row `force-header` pane above the agent (§16, §22). The daemon also writes
`$FORCE_STATE/header/<name>.txt` and `<name>.sub.txt` per live session (tabs with their state labels, click
ranges `#[range=user|<name>]`); tmux's status line, which could show them, is off.

## 10. Cockpit UI

**tmux.conf** (`share/tmux.conf`), required settings:
- no prefix key (§44): `unbind -a` first, then only force bindings; `C-Space` and `C-\` open the Force menu.
- `default-terminal tmux-256color`; `terminal-features` for `xterm*` adds `RGB,extkeys,clipboard,hyperlinks,usstyle,focus`;
  `extended-keys on`; `allow-passthrough on`; `set-clipboard on`; `focus-events on`; `escape-time 0`;
  `mouse on`; `history-limit 100000`; `status off` (the header is a pane, §16); `remain-on-exit on`; `detach-on-destroy off` (killing the viewed
  session moves the client to another one); `set-environment -g COLORTERM truecolor`.
- Bindings, no prefix: `M-1`…`M-9`, `M-Up`/`M-Down` and `M-PPage`/`M-NPage` switch via `run-shell` using the
  options of §6.9 (pure tmux, no Python start-up per key press).
- Every action is an item of the Force menu, opened with `C-Space` or `C-\`; each item has a single
  letter (§44).
- Popups: `display-popup -E -w 64 -h 14 "force dialog …"`.
- tmux resizes panes proportionally; hooks `window-resized`, `client-session-changed` and
  `client-attached` reset the leftmost pane to 29 columns when `@force_sidebar` is on.
- Bindings address the server with `tmux -S "#{socket_path}"`, never a fixed socket name, and the
  header path comes from `@force_state` (default `$HOME/.local/state/force` only if unset, so
  re-sourcing the config keeps the daemon's value).

**Sidebar** (`force sidebar`, the Rust binary per §22): reads `snapshot.json` (poll mtime every 0.5 s), shows
non-archived sessions in `order`, each a 3-row click target (line 1 `N glyph name … age ↑`, line 2
the state label and `↑ <version>` when outdated, one padding row; hover highlight), the current tmux
session highlighted, a collapsed `archived (k)` row and a `+ new` row. Mouse click / Enter on a session → switch the client viewing this sidebar (the
client attached to the sidebar's own tmux session) to it, then select the agent pane of the
sidebar's own session (so coming back to it later lands in the agent, not in the sidebar). Click on `archived` → popup archived
dialog. Click on `+ new` → popup new dialog. The sidebar takes no keys: any key that
reaches it hands focus back to the agent pane. The sidebar never exits on errors; it shows
`daemon not running` when the snapshot is older than 5 s.

**Dialogs** (`force dialog`, the Rust binary per §22, run inside a popup): `new` (name with live validation, cwd
default `$HOME`), `rename NAME`, `archive NAME` (confirm), `archived` (list archived sessions;
Enter → unarchive and switch to it; `f` → forget with confirm). Each calls the same functions as
the CLI and exits.

## 11. Taking over existing Claude Code sessions (`force import`)

| Command | |
|---|---|
| `force import` | lists, imports nothing. **running**: every live Claude process of this user with a `<claude_dir>/sessions/<pid>.json` (any terminal, not only screen) that is not a Force agent pane and whose session id is not in the registry; **stopped**: transcripts `<claude_dir>/projects/*/<uuid>.jsonl` not in the registry (active or archived) and not running, newest 15 (`--all`: every one). Left out: Force's self-test folders, transcripts without a user message, non-interactive runs (`claude -p`, SDK: entrypoint `sdk-…`, e.g. delegated jobs). One line each: id8, status or age, title, folder, the command to import it |
| `force import SESSION [--name NAME] [--dry-run]` | SESSION = session id, a unique prefix of 6+ hex chars, a running Claude's pid or name. Running + idle: SIGTERM, wait ≤ 20 s (then SIGKILL), registry entry, `cockpit.create` (resumes the conversation), wait ≤ 30 s until Claude runs there with the same sessionId, quit its screen if it had one. Running + busy: refused ("import it when it is idle"; also with `--dry-run`). Stopped: the transcript's first `cwd` must still exist; registry entry + create + the same wait. Refused: already in Force, the session running `force import` (or an ancestor) |
| names | `--name`, else the first free slug of: the running status `name`, the transcript's last `customTitle`, its last `summary`, the first five words of the first typed prompt (what Claude Code's resume picker shows), the folder's basename; `-2`..`-9` suffixes |
| `force import --screen [--dry-run]` | bulk import of every idle Claude inside GNU screen |

"import" is forwarded to the host shown (§40).

## 12. Systemd user units (`systemd/`)

`force-tmux.service`: `ExecStart=/usr/bin/tmux -L force -f %h/.local/share/force/app/share/tmux.conf -D`,
`Restart=always` (an accidental `kill-server` brings it back; the daemon then restores the
sessions), `WantedBy=default.target`.
`force.service`: `Wants=force-tmux.service`, `After=force-tmux.service`,
`ExecStart=%h/.local/bin/force daemon`, `Restart=always`, `RestartSec=3`,
`Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin`, `KillMode=process`,
`WantedBy=default.target`.
Restarting `force.service` must never kill agent sessions (they belong to the tmux unit's cgroup).

## 13. Tests

pytest, no network, no real Claude, never the real `~/.claude` or the `force` tmux socket:
- unit tests for registry, slugify, state rules (every row of §5), argv building, header rendering,
  config parsing, importer candidate discovery (with fake `/proc`-like inputs injected).
- integration tests with a temporary `FORCE_HOME`, `FORCE_STATE`, `FORCE_CLAUDE_DIR`, a unique
  `FORCE_TMUX_SOCKET` per test, and `tests/fake_claude.py` as `FORCE_CLAUDE_BIN` (writes
  `sessions/<pid>.json`, reacts to a control file to change status / sessionId / exit code, removes
  its file on SIGTERM). The tmux server of each test is killed in teardown.

## 14. Live release and deployment

The live cockpit never imports the development tree. `~/.local/share/force/app` is a symlink to an
immutable copy under `~/.local/share/force/releases/<timestamp>-<commit>`; the venv finds it through
`site-packages/force-live.pth`, the launcher `~/.local/bin/force` imports from there, and
force-tmux.service reads `app/share/tmux.conf`. `./deploy.sh` runs the test suite on the working tree,
copies it into a new release, checks that the release imports and its tmux.conf parses, then swaps
the symlink atomically; the daemon's live reload (§6.11) does the rest. `./deploy.sh --rollback`
re-points to the previous release. Tests always import the working tree (`pythonpath = ["."]`,
guarded by tests/test_dev_tree.py).
## 15. Attention header, theme, quit, several terminals

- **Header** (content of the two header lines; the files stay): left = the
  session you are in (bold name · folder with `~` · `v<version>`, cyan `↑` when outdated). Right =
  *attention chips* only: other live sessions in NEEDS_YOU (wine `bg=#5a2a2a`) then YOUR_TURN (moss
  `bg=#2c4232`), each 2 rows (`● name` / `needs you|finished`) with the session's click range on both
  rows; then `⠋ N working` (`#d7af5f`); then an always-present 2-row `× quit` button (range
  `__quit__`). Width fitting: chip names 14 → 10, then `+N more`, then shorten the folder, then drop
  the version. The sidebar is the full list; the header never repeats it.
- **Theme**: one palette for header, sidebar, dialogs, toasts and popups: greys of
  the tmux status line (`#262626`, `#303030`, `#3a3a3a`), coral `#d97757` as the only accent,
  no blue but the card of a working session with an unread finding (§5: `#5c9cf5`, text `#5b8fd6`).
- **Sidebar**: a coral `▌` marker column on the current session's rows (text, not a CSS border);
  no keyboard cursor; no scrollbar; `     archived (n)` and `   + new session` aligned with the names.
- **Dialogs**: padded frame, sentence-case title, labelled fields, coloured status row (× / ✓), a
  docked key-hint footer.
- **Quit**: menu `q` or a click on `× quit` detach this terminal only; every session
  keeps running.
- **Several terminals**: each attached terminal has its own current session. `force attach` opens on
  the most recently seen session that no other terminal shows; sidebar clicks and popups act on the
  most recently active terminal of that session; the header pills show the state toasts.
- **Status `shell`** (the agent's turn is over, a background command of its own still runs): WORKING, shown as
  waiting, when that command ends by itself; otherwise the turn is over (§5 `watching`).

## 16. Header pane, instant sidebar toggle, restarts

- **Layout** per session window: `[sidebar, full height, 29 cols] | [header pane, 2 rows / agent]`.
  The tmux status line is off. The header pane runs `force header` (force/headerbar.py; layout from
  `header.layout()`): left `≡ sidebar`; right attention chips, `+N more`, `⠋ N working`, `× quit`.
  Clicks act on the most recently active terminal of the pane's session. Panes have
  `@force_role` sidebar | header | agent; the daemon keeps one of each (ensure_*, dedupe keeps the
  oldest, respawns dead sidebar/header panes, logs every pane it adds).
- **Sidebar hide/show** (`≡ sidebar`, prefix `b`): share/sidebar-toggle.sh moves each sidebar into
  its session's background window `force-stash` (break-pane) and back (join-pane -f). Nothing
  restarts; ~10 ms per window, the viewed window first. State: global `@force_sidebar_hidden`; new
  sessions created while hidden start with the sidebar parked. share/fit.sh (resize hooks) keeps the
  sidebar 29 wide and the header 2 high.
- **Mouse**: clicks in sidebar or header never focus the pane or the list (the list's child-click focus
  is overridden), so no keyboard cursor appears after a click.
- **Restarts**: menu `x` this session (`force restart NAME`), `e` all (`force restart --all`); `--all` restarts idle
  sessions now and marks busy ones `pending_restart` (registry extra) — the daemon restarts them once
  they are not WORKING / NEEDS_YOU / STARTING. Use it after changing `~/.claude/settings.json`.
  `stop_agent` writes `$FORCE_STATE/kills/<name>` so a daemon in another process does not treat a
  CLI restart as a crash (exit 143).

## 17. Switcher, move, activity, doctor

- **Quick switcher** `force dialog jump` (prefix `j` or `/`): filter by subsequence (prefix > substring >
  subsequence), needs-you first, Enter jumps. prefix `Tab` = `switch-client -l` (previous session).
- **Move** `force move NAME up|down` (prefix ↑/↓): swaps registry order with the live neighbour. The
  sidebar always shows registry order.
- **Activity** (`force/activity.py`, snapshot field `activity`): fixed short phrases from the newest
  assistant record of the transcript tail (≤256 KiB): "running a command", "editing <file>",
  "searching files", "delegating", "asks you a question", "plan to review", "using <mcp server>",
  "writing a reply", "thinking". Sidebar line 2 shows it only for WORKING and NEEDS_YOU (NEEDS_YOU
  without a question/plan → "needs your permission"); other states show the state label.
- **Doctor** `force doctor [--fix]` (`force/doctor.py`; controls menu "Health check"): services, tmux,
  registry, snapshot freshness, per session one agent/header/sidebar, sidebar placement vs
  `@force_sidebar_hidden`, stray windows, tmux options; repairs never touch agent panes except
  `restart` when the agent pane is missing; orphan tmux sessions are only reported.

## 18. Shells, mode, model, effort

- **Shell sessions** (`force new NAME --shell`): registry `extra.kind = "shell"`; the main pane (role
  `agent`) runs `bash -l` in cwd with the scrubbed env; state `SHELL` (`$`, grey); rename is
  immediate; never followed, upgraded, chipped or given activity. Exit 0 leaves it DOWN (restart via `x`).
- **Permission mode**: read from the agent pane's visible footer ("auto mode on", "manual mode on",
  "accept edits on", "plan mode on", "bypass permissions on") at most every 2 s per session; last known
  mode kept while a dialog hides it; reset when the pane pid changes. Transcripts do not record mode
  changes reliably (none before the first message).
- **Model / effort**: newest non-sidechain assistant record of the transcript tail (`message.model`
  shortened, e.g. "opus 5.5"; top-level `effort`). Updates with the next reply.
- **Sidebar rows** are 4 rows: name+age · activity/state · `model · effort` + mode badge
  (A/E/P/M/B) · spacer.

## 19. Sharing with other users of the host

Config `sharing = false` (default): the daemon runs no proxies (stops any running), the snapshot carries
no `shared`, `force share` refuses; `unshare`/`shares` still work. When enabled, sharing works as
follows:

- Registry `extra.shares = {user: "r"|"w"}`; `force share/unshare/shares`.
- Guests never connect to force's tmux server (any tmux client can run commands as the user). Per
  (session, guest) the daemon runs `force share-proxy NAME USER MODE` (`force/share.py`): a Unix socket in
  `/tmp/force-share-<user>/` (dir 0711, ours, not a symlink, not group/world writable), socket 0666,
  every connection checked with SO_PEERCRED against the guest's uid.
- Pane mirror, not a tmux view (a tmux view of the window would show the sidebar = other sessions):
  `capture-pane -e` of the session's role=agent pane only, ≤8 frames/s, sent only on change; write
  shares deliver guest bytes with `send-keys -H` to that pane only; read-only shares drop input.
  Keyboard only (no mouse) for guests.
- Guest client `share/force-join` (stdlib only) is installed by the daemon to the share dir (0755).
- Pidfiles `$FORCE_STATE/shares/<name>.<user>.json`; the daemon reconciles every tick (start missing /
  changed, stop removed or non-live). Sidebar ⇄ marker, header "shared with …" (red when write).
- First frame `M` {user, session, mode}: the client shows a status line with the exit keys
  (Ctrl+Q always, Ctrl+]; read-only also Ctrl+C / q).
- History: Claude draws on the alternate screen, so the mirror has no scrollback. The
  guest client asks with `Q`; the proxy answers `T` {"entries": [[kind, text]]} built by
  `force/history.py` from THIS session's transcript (registry session_id; text, tool one-liners, first
  3 result lines; thinking and other sessions never) or, for shells, the pane's tmux scrollback.
  Sanitised (no escapes), ≤ 4 MB, cached per transcript size/mtime. The client pages it locally
  (PageUp in both modes, read-only also ↑ / k / Home / wheel; q or Esc back to live).

## 20. Delete

`force delete NAME [--force] [--yes]`, the menu `d`, and `d` in the archived list
(`f` and `force forget` still work). Live or archived. Order: remove the registry entry first (so the
daemon never restores it), move viewing clients to another live session, stop the agent, kill the tmux
session, delete force's archive copy `archive/<session_id>` (uuid-checked, never a symlink). Claude
Code's own transcript in `~/.claude/projects` is left alone (Claude removes it after 30 days). Busy
sessions need force; the CLI asks y/N on a terminal and needs `--yes` otherwise. `f` (`force forget`)
removes only the registry entry of an archived session and keeps the archive. The daemon deletes a new
Claude session the same way when its folder's trust question is declined (§6.4).

## 21. Plan usage bars

Source: Claude Code's documented status-line input (`rate_limits.five_hour|seven_day.used_percentage`,
`resets_at` epoch s; Pro/Max only, present after the first reply). force launches Claude with
`--settings {"disableAgentView":true,"statusLine":{"type":"command","command":"python3 -I -S
<app>/share/statusline.py <state>/usage.json"}}` (the user's settings.json is not touched; this
overrides a personal statusLine inside force). The script prints nothing (no visible status line) and
writes only those two windows, atomically, 0600, only when changed. The daemon puts
`force.usage.read_usage()` into `snapshot.usage`; the sidebar shows a bottom panel (rule, "session" and
"week" bars, reset times; amber ≥ 70 %, red ≥ 90 %; "new window" after resets_at; hidden with no data).
Sessions pick this up when (re)started. No OAuth token or undocumented endpoint is used.

## 22. Rust sidebar

`crates/sidebar/` (ratatui 0.30 + tachyonfx 0.25) renders the sidebar, header and popups, with short
effects (fade-in, state-change pulse, marker sweep, bar tween) only while viewed; idle = no output. A few
MB per pane, instant start. `deploy.sh` builds them into the release (`share/bin/force-sidebar`,
`force-header`, `force-dialog`; a build failure blocks the deploy) and `force sidebar|header|dialog` exec
them. The Rust programs only read `snapshot.json` and call `tmux -S $TMUX ...`; they never write force
state.

## 23. Animations

Config `animations = "full" | "subtle" | "off"` (passed as `--animations` to both Rust panes).
full: arrival fade of the current card on a switch
(3 frames, colour only), finish flash (card + ✓, header pill reveal), cards grow/shrink on
add/remove, needs-you pulse, bar tween, and a working shimmer on activity text (≤ 8 fps, the only
continuous one). subtle: all but the shimmer. off: final frames only. Animations run only while the pane
is on screen; idle = no output. Switch signal: the `client-session-changed` hook runs
`share/switched.sh`, which refits the panes and sends SIGUSR1 to the new session's sidebar (arrival
fade). Cards appear at once on start.

## 24. Multi-host, local UI

The UI (sidebar, header, animations) can render on a different machine than the one running the sessions;
several hosts are supported. See §39 (split
mode: host and ui tmux servers), §40 (remote hosts) and §41 (a local Force on the machine you sit at).

## 25. Rust popups and narrow rail

- `force dialog KIND [NAME]` execs `share/bin/force-dialog` (new, rename, archive, delete, archived, jump;
  same texts/keys/validation as the dialogs; mutations via the force CLI). Opens instantly.
- `share/fit.sh` sizes the sidebar 29 columns, or 7 when the window is narrower than 110 columns; the
  Rust sidebar draws a rail (brand, one row per session: edge, index, glyph; `a<N>` archived, `+`
  new; `5h NN%` / `wk NN%`) whenever its pane is narrower than 16 columns, switching live on resize.

## 26. Pre-sized windows
- A window is sized `smallest` (§28): shown in two terminals it keeps the smaller size, so it is not
  resized on every keystroke in either one (Claude Code fully redraws on each resize).
- `share/presize.sh` (hooks `client-attached`, `client-resized`; debounced 0.3 s) resizes every window that
  no client is showing to the calling client's window size, unsets the per-window `window-size` that
  `resize-window` pins, and refits its panes. Windows shown by other clients are left alone.
- Header pills: the trailing cap is `▌` (a `▐` cap left a BG strip inside the pill), and the drawn
  width uses display width, matching the fit estimate (the `●` lead counted 3 bytes).

## 27. Usage panel always visible
- The usage panel stays; a list that does not fit scrolls with the own card kept in view (the cursor rests
  on it while the keyboard cursor is inactive). A mouse-wheel scroll is kept until the next rest (focus
  loss, a switch). Only a window that would leave the list fewer than 8 rows drops the panel.
- Overflow: when the cards do not fit, `archived (n)` and `new session` are pinned to the
  bottom of the list area and only the cards scroll above them. A grey `↑ N more` row tops the cards when
  some are hidden above, and `↓ N more` takes the blank row above `archived` when some are hidden below
  (N counts cut cards too). The row is bold in the needs-you colour when a hidden card needs you. A click on
  it pages the cards; the keyboard cursor on the pinned rows does not scroll. Lists under 7 rows keep the
  old behaviour (everything scrolls). A list that fits is unchanged.
- Density: cards are 2 rows (name, activity). The model line (✻ model · effort, mode badge) shows only
  on the selected (own) card; shell cards are 2 rows. The limits are 2 rows between blank rows (`  5h ━━━╸━━━  28%  14:20`,
  `  wk … Thu`: time today, weekday later, `new` once reset; no PLAN USAGE header). `+ new` and
  `archived (n)` share one row after a blank row (new at the names' column, archived right-aligned; a
  click picks by column); a pane too narrow for both stacks them. The keyboard cursor colours an action
  coral.
- tmux.conf sets its terminal-features at fixed indices 10-12, so a reload does not append copies.
- Usage merge: every session's status line writes the same usage.json, and an idle session
  reports the figure of its last request. share/statusline.py
  merges per window: same resets_at -> the higher percentage, a later resets_at (new window) replaces, an
  earlier one is ignored, a missing window keeps the saved one.

## 28. Glyph tiers: rich terminals, automatic plain fallback
- Config `glyphs = "auto" | "rich" | "plain"` (default auto), passed as `--glyphs` to the Rust sidebar,
  header and popups. PLAIN = the classic glyphs (any truecolor terminal). RICH = Nerd Font icons for
  states/buttons/actions/dialog titles and slim usage bars of heavy box-drawing strokes (`━` filled in the
  threshold colour, `╸` for a half cell, `━` dim for the track, like Python Rich), which every modern
  terminal draws itself, seamless and without a font. PLAIN uses the same line bar (box drawing needs no Nerd
  Font). Every RICH glyph is one column wide, so layouts are identical.
- PLAIN draws only characters that Windows Terminal's default font (Cascadia Mono) has and that are not emoji
  (× ! ↔ ● ○ ✓ …): elsewhere Windows substitutes a colour emoji twice as wide, which breaks the layout.
  RICH keeps its own glyphs.
- auto: `share/tier.sh` (run by switched.sh on attach and every switch, for the shown and the left
  session, and by the `client-detached` hook) classifies every client showing the session from
  `#{client_termname} #{client_termtype}`: `rich` only if all match the config's `rich_terminals` (an
  extended regex, case-insensitive; default `kitty|ghostty|wezterm`; the daemon publishes it as the
  global option `@force_rich_terms`, read by tier.sh and presize.sh), else `plain`
  (tmux draws one picture for all clients of a session), stored in the session option `@force_glyphs`; the panes re-read it on SIGUSR1 (sent to the sidebar and
  header), at start and every 5 s. Nothing to configure per terminal.
- kitty draws the caps and progress glyphs itself but needs a Nerd Font for icons: the recommended setup
  installs "Symbols Nerd Font Mono" and maps the private-use ranges to it. Ghostty and WezTerm
  bundle the symbols.
- The server needs the client's terminfo (`~/.terminfo/x/xterm-kitty`, copied from kitty.app).
- A client can state its tier: `force --glyphs rich|plain` registers
  `<tty>=<tier>` in the global option `@force_client_glyphs` before attaching (the tmux client runs on the
  same tty; `auto` drops the entry, entries of gone ttys are pruned); tier.sh and presize.sh use a stated
  tier before the terminal-type regex. So a terminal with the symbols font gets RICH with no server
  config. `force --cell-aspect X` likewise gives the intro the client's cell shape (used only when the tty
  reports no pixel size; before the server's `intro_cell_aspect`).
- The daemon also re-runs tier.sh every 5 s for every session a client shows: the hooks
  only fire on attach and switches, so a client attached before `rich_terminals` changed kept the old tier.
- Sidebar and header repaint every cell after a resize and on SIGUSR1, not only the changed
  ones: tmux can drop part of a pane when it is resized and restored to the same size, which ratatui's
  diff never notices (header buttons with one row of background). The full repaint resets
  ratatui's previous frame (`swap_buffers`); never `Terminal::clear()`: it queries the cursor position,
  the input thread swallows the reply and the timeout exits the process (every sidebar and header would die
  on each SIGUSR1). Startup clears with a plain clear-screen for the same reason.
- VTE (Tilix / GNOME Terminal, `VTE(7600)`): with the Symbols Nerd Font installed, RICH renders like
  kitty (icons at text size through fontconfig fallback, usage bars, chips), so `rich_terminals` can add
  `|vte`. Not the default: a VTE without the symbols font shows boxes. VTE ignores OSC 52 (tmux copies
  never reach the local clipboard; copy-mode selection uses the terminal's own selection, Ctrl+Shift+C) and
  reports no pixel size (the intro gets `intro_cell_aspect`, §31). "Nerd Font Mono" builds as the main
  font squeeze icons into one cell: use the normal font plus the symbols font.
- Icon slot: kitty/Ghostty draw a Nerd Font icon across two cells (text size) when a space follows it, so
  RICH gives every icon that is followed by text a 2-cell slot plus a gap (`glyphs::gap`): card names, line
  2/3 indent, pills and buttons move one column right; PLAIN is unchanged. Working = a filling pie
  (md circle-slice 1–8, 125 ms steps) in RICH, the braille spinner in PLAIN. Usage bars are one solid
  threshold colour in both tiers (a gradient over a few cells reads as stripes). Dialogs: a blank row under
  the title, raised kind chips, text keeps the field / highlight background, busy warning in amber.
- Header pills: no caps any more; a pill is a state-coloured edge `▍` on both rows + body +
  one padding column, like the sidebar cards, in both tiers (rounded caps stacked over two rows looked
  scalloped in kitty). One BG column separates the sidebar button from the first pill.
- Unviewed sessions follow the most recently active client (presize.sh, on attach / resize / every switch):
  their windows take its size and their `@force_glyphs` its terminal's tier (hidden panes are signalled to
  redraw), so switching in that client shows a ready picture. Sessions shown by clients keep the all-viewers
  rule of tier.sh.
- `window-size smallest`: a session shown in two terminals keeps the smaller
  size instead of resizing on every keystroke in either one (Claude Code fully redraws on each resize);
  the larger terminal shows a margin. One picture per session is a tmux/PTY limit: a different layout per
  device is what Remote Control on claude.ai is for. Hook scripts exit 0 (tmux shows a failing hook's
  status over the window: tier.sh's "returned 1").

## 29. Brand logo
- The full sidebar's brand block is 4 rows: 3 rows of `▀` cells carrying two colours each (fg = top
  pixel, bg = bottom pixel: a 28×6 pixel canvas) and the `▔` underline. The mark: three overlapping cards
  stacked back to front with lit top edges, bright left edges (like the session cards) and a soft
  shadow; the wordmark `force` 5 px tall with a peach-to-coral vertical gradient and a 1 px drop shadow.
  Identical in every truecolor terminal (both glyph tiers). On arrival (SIGUSR1 / first view) a diagonal
  light sweep crosses the logo once in 700 ms (not with `animations = off`; frames only while viewed).
  (The current sidebar brand, a rule with `F O R C E`, is described in §48.)
- The host (dynamic: the machine force runs on, read at start) is a remote chip on
  the sidebar's bottom row, ` @ host ` (RICH: codicon `remote` in a 2-cell slot), coral icon on #303030,
  truncated with `…`; it always keeps that row (it opens the host picker, so it must stay clickable): an error,
  daemon-down or self-test note goes on the row above it. A remote that is down needs no note: the
  chip itself reads `host · offline`.
- The rail keeps its 2-row brand.
## 30. Internal names
The internals use the name `force` throughout:
- The Python package `force`.
- The binaries `force-sidebar`, `force-header` and `force-dialog` (the Rust panes, §22) and
  `force-intro` (§31).
- The tmux sockets `force` (host) and `force-ui` (ui).
- The tmux options `@force_*` (for example `@force_state`, `@force_role`, `@force_sidebar`,
  `@force_sidebar_hidden`, `@force_glyphs`, `@force_idx_N`).
- The `force-stash` background window that parked sidebars live in (§16).
- The data dirs `~/.local/share/force`, `~/.local/state/force` and `~/.config/force` (§2).
- The systemd user units `force.service`, `force-tmux.service` and `force-smoke.service` / `force-smoke.timer` (§37).

The command and the user-facing names are described in §33.

## 31. Startup intro
- A ~12 s intro plays on every attach, right before tmux takes the terminal: a film of a man settling into a
  lounge chair with a laptop whose screen shows the Force splash ("Underline, black": italic FORCE over an
  underline, the name "The code cockpit" centred under it); the camera goes into the screen and the film
  crossfades into the identical splash drawn crisp on the terminal, which then comes alive (speed streaks rush
  into the word and fade, a light sweep, a light along the underline, a shimmer over the name).
  Any key skips it; the skip hint reads `skip intro · any key`.
- `intro_cell_aspect` (default 0 = unset): passed as `--cell-aspect` only when the terminal reports no
  pixel size (TIOCGWINSZ; VTE/Tilix: 2.2 for Ubuntu Sans Mono 13), so the film is not squashed by the
  intro's 2.0 guess; a terminal that reports its size (kitty) is always measured.
- Off via `intro = false`, `animations = "off"`, `--no-intro`, or when the binary is missing / not a TTY /
  terminal < 80x24. Ctrl+C aborts the attach.
- The intro is a separate Rust crate (`force-intro`) in `crates/intro/` of this repo. Its argv and exit codes are a
  stable contract: `--animations`, `--color`, `--fade-to`, `--handoff`, `--cell-aspect`, exit 0 /
  129 hang-up / 130 Ctrl+C. Force builds it in `deploy.sh` / `scripts/build-release.sh` and calls it from
  `attach` (`force/cli.py::play_intro`, `intro_argv`) right before the final tmux exec. `crates/intro/` also holds
  the sources that make the film (`blender/`, `motion/`, `tools/`); its README is the working guide (renders
  and assets live outside git).

## 32. Background commands
- Claude reports status `shell` when its turn is over but a background command it started still runs (it
  resumes when that ends); while it runs a foreground command it reports `busy`. What the session then is depends
  on that command (§5 `watching`): one that ends by itself makes it WORKING with `watching` and the activity
  `waiting · <its description>`; a server or follower that runs until stopped does not, and the session is your
  turn or idle as after any turn.

## 33. Name: Force
- The command is `force`; `~/.local/bin/force` is the only launcher, and the units, tmux.conf bindings,
  menus and panes call it by that absolute path.
- User-facing texts say Force: the window title `Force · <session>`, the controls menu "Force controls",
  help "Force keys", and messages `force: …`. CLI help and the README say "Force — the code cockpit".
- The tagline is **the code cockpit**.
- The sidebar brand is described in §48.

## 34. Clipboard bridge: copies reach the laptop in any terminal
Copies now reach the machine you sit at through the ui tmux itself: it pipes each copy from the
`pane-set-clipboard` hook to `share/clip-set.sh`, which writes the local clipboard. See §41.

## 35. Client kit
No client kit or launchers exist any more: Force themes itself in tmux and needs no per-terminal setup. The
only optional extra is `force fonts`, which installs the icon font. See §41 and §45.

## 36. OpenCode sessions
- A session kind next to Claude and Shell. Registry: `kind: "opencode"`, `oc_session` (the OpenCode
  session id, once known), `oc_port`; the `session_id` field stays a uuid placeholder.
- Offered only where installed: `force kinds [--json]` lists claude (Claude Code installed), opencode
  (`opencode` on PATH) and shell; the New dialog shows those chips (Claude / OpenCode / Shell, ←/→ or
  Ctrl+T), `force new NAME --kind opencode` refuses when OpenCode is missing.
- Pane: `force run` stage 2 picks a free 127.0.0.1 port (never 8000/8080/8787), stores it as `oc_port` and
  execs `opencode --port P --hostname 127.0.0.1 [--session <oc_session>] <cwd>`; a restart resumes the
  session on a new port.
- State from the TUI's own HTTP API (force/opencode.py, ~3 GETs per tick, 0.4 s timeout):
  `/session/status` busy|retry → WORKING; a pending permission or question for the session → NEEDS_YOU
  ("asks you a question" for questions), read from the TUI's `/permission` and `/question` lists and the newer
  `/api/permission/request` / `/api/question/request`; else idle (YOUR_TURN after a turn,
  STALE by `/session/<id>` time.updated). No answer → STARTING within the grace, then DOWN. The API is
  per TUI process, so a busy session there is the pane's own (the daemon follows it into `oc_session`,
  also when the user switches sessions in the TUI).
- Cards use the same slots as Claude's: line 3 = model label · effort, where the model
  label is the id without provider, dashes as spaces ("deepseek v4 flash"; before the first message the
  configured default from `/config`) and effort is the model variant unless "default"; the mode badge slot
  shows the agent (`oc-build` B, `oc-plan` P), read from the TUI's screen (the prompt box footer
  `┃  Build ·…`, every 2 s like Claude's mode line) and else from the last message;
  a kind mark in the state-icon column of line 3 tells the kinds apart (✻ coral for Claude Code, ▣ grey for OpenCode).
- Activity while busy, Claude's phrases from the running part of `/session/<id>/message?limit=1`: bash →
  running a command, read → reading <file>, edit/write/patch → editing <file>, grep/glob/list →
  searching files, task → delegating, webfetch → browsing the web, todo → planning, reasoning →
  thinking, text → writing a reply.
- Versions: the running TUI's from `/global/health`, the installed binary's from `opencode --version`
  (cached 60 s); differing → the card's outdated marker, and the upgrade step (and `force upgrade`)
  restarts an idle (since `time.updated`, or the pane start for a session with no messages yet),
  unviewed session onto it like a Claude upgrade.
- Which OpenCode: by default the `opencode` on PATH (it may be installed system-wide, e.g. root-owned
  in `/opt/opencode` and kept current outside Force); Force never updates that one. With
  `opencode_managed = true` Force keeps its own copy (force/opencode_update.py) in
  `~/.local/share/force/opencode/<version>/opencode` with `current` → the version new sessions start:
  `force opencode-update [--version V] [--check]` fetches a GitHub release
  (`anomalyco/opencode`, `opencode-linux-<arch>.tar.gz`), checks its sha256 against the digest GitHub
  lists, extracts only the `opencode` file, requires `--version` to match, switches `current`
  atomically and keeps the two newest versions (and any still running). The daemon runs it in the
  background at start and every 6 h (with auto_upgrade), so sessions follow new releases like Claude Code.
- OpenCode's own update dialog ("Update Available … update now?") sits over the prompt and takes every
  keystroke until dismissed (the "first keys are lost" quirk). Sessions Force starts set
  `OPENCODE_DISABLE_AUTOUPDATE=1`, since Force handles updates.
- Same as Claude: restart, restart --all, pending restart, crash restart, archive, delete, the restart
  note, turn-finished and needs-you states. Not for OpenCode: Remote Control naming (`-n`), Claude's plan
  usage bars, `force import` from screen; renames apply at once.

## 37. Self-test against the real tools: `force smoke`
Force depends on how Claude Code and OpenCode behave (status files, the TUI API, prompts, `--resume`,
project and memory directories); the unit tests use fakes, so an upstream update can break the cockpit
while they pass. `force smoke` (force/smoke.py) drives the real tools through Force's own lifecycle and
daemon on a separate tmux server (`forcetest-smoke-<pid>`) with its own registry, state and config.

| Check | Claude Code | OpenCode |
|---|---|---|
| start | card idle (answers "trust this folder?" for its work folders, card must say needs you meanwhile) | card idle |
| turn | PONG prompt: working, then your turn; the reply is in the transcript | same; the daemon learns the session id; reply via `opencode export` |
| needs you | a `touch` the work folder's settings make ask for (`permissions.ask`): needs you, Esc denies | same (`opencode.json` `permission.bash = ask`) |
| restart | resumes the same session id | — |
| move | to the second work folder: memory pinned (`autoMemoryDirectory`), transcript carried, the reply recalls PONG and gives the new folder's codename (BRAVO) | re-homed: recalls PONG and says BRAVO |
| move while busy | `force move` during a long reply: deferred (`pending_move`, card "moves after this turn"), the daemon moves it back after the turn, same conversation | — |

- Work folders `<state>/smoke/work/{a,b}`: git repos (separate OpenCode projects) with CLAUDE.md /
  AGENTS.md (codename ALPHA / BRAVO) and the permission settings; kept between runs. Each run's registry,
  state and tmux server are removed, and so are its Claude transcripts and every OpenCode session in the
  work folders.
- Sessions use the `haiku` model (`ANTHROPIC_MODEL`, override `FORCE_SMOKE_MODEL`) and never register for
  Remote Control (`FORCE_NO_REMOTE_CONTROL=1` drops `--remote-control`). The smoke daemon runs no sidebars.
  `FORCE_SMOKE_FORCE` points the panes at another `force` (development).
- Result: `<state>/smoke/last.json` (+ `history.jsonl`, logs in `log/`, 30 kept); `force smoke --last` prints it.
  The daemon puts `{"ok", "at"}` (plus `reason` when it failed) into the snapshot (`smoke`); a failed run shows
  "✖ <reason>" (`×` in the plain tier) just above the sidebar's host chip (e.g. "✖ Claude Code logged out") and a `self-test` problem in
  `force doctor`, until a run passes.
- The claude flow first checks `claude auth status`: logged out, it records a "claude: logged in" failure and stops
  at once (no session is created).
- `force-smoke.timer` (hourly) runs `force smoke --if-due`: when the Claude Code, OpenCode or Force
  version changed, a failed run is 6 h old, the last run is a day old, or a run that failed because Claude
  is logged out is back in (it re-runs within the hour once logged in again). One run at a time (flock). ~1 min.
- Claude Code's binary: `FORCE_CLAUDE_BIN`, else `~/.local/bin/claude` (its own installer), else `claude`
  on PATH (a system package, e.g. `/usr/bin/claude`).
- A Claude session at the "trust this folder?" question has no status file and would read as starting, then down;
  the daemon reads the screen of such a pane, and the question makes it needs you ("trust this folder?").

## 38. Second host
A second machine is added with `force host add`, which installs Force there, switches it to split mode and
makes it a remote; see §40.

## 39. Split mode: host and ui tmux
Every install is host + ui; this section covers the one-machine part. Config `ui = "classic" | "split"`
(`force ui [classic|split]`); split is the default (§2).

| Part | Split mode |
|---|---|
| host `force` | sessions only: one window, one pane (`@force_role agent`). `create()` adds no sidebar/header; the daemon's layout step removes leftover sidebar/header panes and `force-stash` windows. Same tmux.conf as classic (its keys never fire: the ui catches them) plus `terminal-features[13] tmux*` for the nested clients; its `window-style` forces no text colour (§45, text colour) |
| ui `force-ui` (`FORCE_UI_SOCKET`, `share/tmux-ui.conf`) | `force` builds one ui session per terminal (`build_ui_session`: `ui-<pid>`, detached, then `exec tmux attach`): main pane (`@force_role agent`) running `force nested NAME`, sidebar (left, `sidebar_width`), header (2 rows). `destroy-unattached` is set per session in the client-attached hook (set globally it destroys the session while it is created detached); `exit-empty on`; no systemd unit |
| ui window options | `@force_nested` = the main pane's tty = the nested client's name on the host; `@force_session` = the host session shown (set at build, by the host's switch hook, by the sidebar); globals `@force_host_tmux` (`tmux -L force`; for a remote, ssh), `@force_app`, `@force_state`, `@force_rich_terms`, `@force_idx_N` (daemon) |
| `force nested NAME` | `tmux -L force attach -t =NAME` with `TMUX` unset; when the client ends it attaches to a live session again (or waits, "no session to show"); exits with its pane |
| bindings (`tmux-ui.conf`) | `#{@force_session}` instead of `#{session_name}`; switches run `#{@force_host_tmux} switch-client -c "#{@force_nested}" …`; popups get `-e FORCE_CLIENT/FORCE_SESSION/FORCE_GLYPHS` so dialogs switch the nested client |
| host switch hook | `switched.sh` also sets `@force_session` on the ui window whose `@force_nested` is the switching client and signals its sidebar/header (`@force_ui_socket`, published by the daemon) |
| USR1 | `tier.sh`, `switched.sh`, `presize.sh` signal a sidebar/header only once `pane_current_command` is the Rust program (`force-*`); signalled while still the `force` launcher, the process dies (USR1's default action), which shows at every split-mode attach |
| daemon | `_servers()` = host + ui (when running): `@force_idx_N` on both, reload respawns sidebar/header panes on both and sources/validates `tmux-ui.conf`; dead ui sidebar/header panes respawned (10 s rate); clipboard: the ui tmux sets it (§41) |
| doctor | host sessions: one pane each (`split-extra-pane`), no extra windows; ui sessions complete (`ui-incomplete`) |
| `_inside_cockpit` | true in either server |
| tests | fixtures get `FORCE_UI_SOCKET = <test socket>-ui` (never the live ui server) |


## 40. Remote hosts
One ui shows one host at a time; the local machine renders, only the session terminals cross the
network. A remote is another machine with Force installed; both ends run split mode (`force host add`
switches the remote and this machine; `force ui classic` is refused while remotes exist).

| Part | |
|---|---|
| `~/.config/force/hosts.json` | `{"hosts": [{"name", "ssh", "enabled"}]}`; `force host add NAME [SSH] [--no-install] \| list \| remove NAME \| upgrade NAME` (`force remote` is an alias). `add` checks key login (BatchMode), installs Force there when missing (`install-to`), switches it to split |
| `force install-to SSH` | tars the live release to `~/.local/share/force/releases` there and runs its `install.sh --release-dir` (no build, no git or Rust there). Remotes follow this machine's version; a newer remote is reported, never downgraded |
| ssh (`force/remote.py`) | one ControlMaster per host (`<state>/ssh/%C`, persist 10 m, keepalive 15 s × 3, BatchMode, ConnectTimeout 10). `ssh_argv` quotes every word for the remote shell except a leading `~/` (a bare `#{…}` is a comment there) |
| `force watch` (host side) | a hello line `{proto, version, host, ui}`, then `{snapshot, registry}` on every snapshot change and every 10 s. `PROTO = 1` |
| watchers (daemon step `remotes`) | one `ssh HOST force watch` per enabled remote → `<state>/remotes/NAME/{snapshot,registry,status}.json`; status `connected \| offline \| version` (other PROTO: "force remote upgrade NAME"); reconnect pause 2 s doubling to 60 s; stopped on reexec and exit |
| ui window | `@force_host` = remote name (empty = this machine), `@force_host_tmux` = `ssh … HOST TMUX -L force`, TMUX = the remote's own (`~/.local/share/force/app/share/tmux-bin/tmux` when its release ships one, else `command -v tmux`; asked once over ssh, cached in `<state>/remotes/NAME/tmux`; an absolute path, as tmux escapes `$` in option values), the remote's live socket `force` also from a development instance; session env `FORCE_HOST`. Main pane `force nested NAME --remote R` = `ssh -t HOST force nested NAME --report ID`; the remote writes its tty to `~/.local/state/force/nested/ID`, the local side reads and removes it and sets `@force_nested`. A dropped connection reconnects to the window's current `@force_session` |
| reading a remote | sidebar, header, dialogs read the local copies (`_host_files`); the Rust `HostTmux` shell-quotes its args when the prefix starts with `ssh`; the host chip shows `FORCE_HOST` |
| acting on a remote | `force …` session commands with `FORCE_HOST` set run there (`_forward`); `ui-key.sh`, the Force menu (`share/menu-split.tmux`), popups pass `FORCE_HOST`. A remote's switch hook cannot reach this ui server: `@force_session` follows by the sidebar's poll, `ui-key.sh` publishes it after its own switches |
| "this session" | rename/archive/delete/move dialogs and `ui-key.sh` restart/upgrade/up/down ask the host which session the nested client shows now (`_shown_session`, `shown()`), never the possibly lagging `@force_session`; a remote that does not answer: nothing happens |
| which host | `force [--remote R]`: local sessions, else the last host used (`<state>/ui_host`), else the first enabled remote. Host picker (`hosts-menu`/`ui-host`): menu `h`, a click on the sidebar's host chip, "Switch host…" in the controls menu; without remotes it says how to add one: it rebuilds the window for another host; a host with no sessions (this machine, or a connected remote: `force new shell --kind shell` over ssh; the menu says "(new shell)") starts a shell session `shell`, as does `force --remote R` on an empty remote; an offline remote is a note. `ui-host` never prints (it runs from a binding): its notes go to the terminal(s) showing that window |
| other hosts | header: a marker pill per other host with sessions that need you or finished (`hosts.rs`: local snapshot + every `connected` remote copy, fresh only), before the session pills; click = `ui-host NAME --attention` (opens the first NEEDS_YOU, else YOUR_TURN session there). Host picker lines carry the same counts |
| remote down | sidebar host chip `NAME · offline` / `· other version` on red; header and empty list say `NAME offline · reconnecting` / `other Force version · force remote upgrade NAME`. Copies are stamped with the local receive time (`remote_generated_ms` keeps the remote's), heartbeat 2 s, so "stale" means a silent link |
| upgrades | `force watch` exits when its host's live release changes; the reader reconnects and gets the new hello. `remote remove` deletes the copies; a disabled remote's status is `disabled` |
| ui sessions | the daemon ends ui sessions without a client for 60 s (terminal closed during the intro, before destroy-unattached was set) |
| tests | `tests/test_remote.py` (ssh replaced by a local script), `crates/sidebar/src/hosts.rs` |

## 41. Local Force on the laptop
The machine you sit at runs Force itself.

| Part | |
|---|---|
| `force fonts [--cleanup-only \| --uninstall]` | `share/fonts.sh` (§45): installs the Nerd Font symbols and removes the old per-terminal profiles, launchers and clipboard kit |
| on WSL | the terminal is a Windows program drawing with Windows' fonts, so no Linux font: `fonts.sh` (run by the installer too) copies `SymbolsNerdFontMono-Regular.ttf` into `%LOCALAPPDATA%\Microsoft\Windows\Fonts`, registers it under `HKCU\…\Fonts` (per user, no admin) and writes a Windows Terminal fragment (`Fragments\Force\force-<distro>.json`) adding a "Force (<distro>)" profile: font `Cascadia Mono, Symbols Nerd Font Mono`, command `wsl.exe -d <distro> --cd ~ -- bash -lc 'exec ~/.local/bin/force --glyphs rich'`. The user's own profiles are untouched; there Force shows the plain tier (§28). `--uninstall` removes the fragment, and the font with the last distro's profile. Without interop: a hint, exit 3 |
| clipboard | the ui tmux (on the machine you sit at) pipes each copy to `share/clip-set.sh` through the `pane-set-clipboard` hook, which tmux runs for an application's OSC 52 (Claude Code's selection, passed up by the host tmux) and, with set-clipboard on, for copy-mode and mouse copies too. clip-set.sh: ≤ 4 MiB, needs `DISPLAY`/`WAYLAND_DISPLAY` (none, e.g. a ui over ssh: OSC 52 only), then `wl-copy`, `xclip`, `xsel`, else `share/clip-hold.py` (GTK 3 over X11/XWayland: owns CLIPBOARD and PRIMARY until another program takes them, 24 h at most, one holder at a time: `$XDG_RUNTIME_DIR/force-clip-hold.pid`, stop-previous + record under a flock). The daemon's clipboard step and `<state>/clip` are gone |
| self-test | `force smoke` adds `split: keys reach a session through the ui` (a shell session, a ui session on a test ui socket, keys typed in the ui reach the host pane) and `split: force watch` (hello with this PROTO, then a snapshot with the session); both run without Claude or OpenCode |
| doctor | per enabled remote: `remote-offline` (with the watcher's detail), `remote-version` (`force remote upgrade NAME`); an unreadable hosts.json is `remotes` |
| install.sh | runs `fonts.sh --cleanup-only` and ends with a hint: optional `force fonts` |
| remote reach | the sidebar/header's host tmux commands (`@force_host_tmux`, ssh for a remote) are killed after 8 s (`HOST_TMUX_TIMEOUT`): a hung link cannot freeze them. The New/Move dialogs' folder Tab-completion asks the host shown: `force dirs DIR PREFIX` (forwarded like the session commands; `~/` words keep their tilde for the remote shell, the rest is quoted) |
| menus by click | tmux opens a menu with mouse support only from a command that carries the click; from run-shell (the header/sidebar apps) it has none: the menu closes and its items ignore clicks. So the header (`controls` button) and the sidebar (host chip) publish the button's cells as pane options `@force_menu_kind` (`controls`/`hosts`), `_x0 _x1 _y0 _y1` (pane-relative, end exclusive), and `MouseDown1Pane` opens that menu itself: the controls button sources the static Force menu (§44); for the host chip `share/menu.sh` has `force hosts-menu` write the display-menu command to `<socket_path>.menu-<client_pid>` (`FORCE_MENU_OUT`, words in tmux double quotes), then `source-file -qF` runs it with the click. With `-O` it stays open on release, an item takes a click, a click outside (the button again too) closes it. Keyboard-opened menus (Ctrl+Space, menu `h`) stay keyboard menus |
| narrow header | the right block drops, in order, the working count, quit, controls until it fits after the sidebar button; a status text (offline, other version, daemon not running) starts after the button and is cut with `…` |
| Shift/Ctrl+Enter | terminals send both as Enter unless configured, and Force configures no terminal (§45): a new line is Alt+Enter or Ctrl+J everywhere (Claude Code takes LF and ESC CR as one). Both tmux configs keep `bind -n S-Enter`/`C-Enter send-keys C-j` for terminals that send CSI u by themselves (tmux 3.4 passes extended keys only to apps that ask; Claude Code does not). In classic layout, menu `h` says the host picker needs the split layout |

## 42. Public releases

Force is distributed as public GitHub releases with a one-line installer and a GitHub Pages landing page.

| Part | Plan |
|---|---|
| Build | GitHub Actions (`.github/workflows/release.yml`): a pushed tag `vX.Y.Z` is the release and its version (no file in the repository holds it; `pyproject.toml` says 0.0.0, local builds default to the newest tag, `scripts/version.sh`). The `check` job refuses a tag that is not on `main` or not newer than every release, and waits for the commit's `ci.yml` run on `main` to pass instead of testing again; at the same time `scripts/build-release.sh` (builder container) and `scripts/build-darwin.sh` (a GitHub Mac) build the tarballs; then `publish` fills `install.sh` with every platform's SHA and creates the GitHub Release with the tarballs, their `.sha256` and `install.sh`, build provenance attested; then the site is redeployed with the new `install.sh`. A pull request that changes the release machinery runs the builds and assembles `install.sh` as 0.0.0 without publishing. Linux x86_64 and macOS arm64 now, Linux aarch64 later |
| Hosting | the release tarballs and their `.sha256` live on **GitHub Releases** of the public repo (`https://github.com/<owner>/<repo>/releases/download/vX.Y.Z/force-X.Y.Z-linux-ARCH.tar.gz`), never on the site; the site serves only `install.sh` (pinned version + SHA-256), which the updater also reads. `share/get-force.sh` fetches the tarball from GitHub Releases (`@RELEASES_URL@`), with `FORCE_DOWNLOAD_BASE` / `FORCE_RELEASES_URL` for test servers and mirrors; `force/updates.py` reads the released `<base>/install.sh` to find the newest version |
| install.sh | pins version + SHA-256 per architecture; downloads, verifies, installs, sets up systemd units and linger and PATH: `curl -fsSL https://useforce.cc/install.sh \| sh` |
| Website | `website/` in this repo, built by `website/tools/build.py` and deployed to GitHub Pages by `.github/workflows/pages.yml` on every push to `main` and after each release; https://useforce.cc (apex A/AAAA records to GitHub Pages, `www` CNAME to `<account>.github.io`, DNS on Cloudflare) |
| Self-update | Force switches to the latest release (atomic, sessions untouched); remotes follow this machine |
| Licence | MIT (`LICENSE`, Cargo.toml / pyproject `license`). `THIRD-PARTY-NOTICES.md` (`scripts/notices.sh`: cargo-about with `about.toml`, whose accepted list refuses a crate under any other licence; Linux x86_64 + aarch64 only) ships in every release next to LICENSE; `tests/test_notices.py` fails when a crate compiled in is missing from it. CI regenerates and checks it |
| Platforms | Linux x86_64 + aarch64; macOS on Apple Silicon (arm64) (§51) |
| Name | Force, **the code cockpit**; the codebase names Claude Code only where it means that program (its kind, binary, files, the self-test and importer) |
## 43. Automation, reports, explain, titles, registry history
Commands that scripts and agents use instead of raw `tmux send-keys`, plus state that
scripts report and a way to see why a card shows what it shows.

### 43.1 Driving a session: `force prompt | wait | read | keys`
State names on the command line: `needs-you working your-turn idle stale down starting shell unknown`
(also accepted: upper case, `_` for `-`). The state is the daemon's snapshot (`snapshot.json`, one tick ≈ 1 s);
a snapshot older than 10 s = error `daemon_not_running`.

| Command | Does |
|---|---|
| `force read NAME [--lines N] [--ansi]` | the agent pane's last N rows (default 80): `capture-pane -p -J -S -N` (`-e` with `--ansi`), trailing blank lines dropped |
| `force keys NAME KEY…` | `send-keys` to the agent pane. Aliases (any case): `esc escape`→Escape, `enter return`→Enter, `tab`, `space`, `up down left right`, `backspace`→BSpace, `shift+tab`→BTab, `ctrl+x`/`c-x`→`C-x`, `alt+x`/`m-x`→`M-x`; other words go to tmux as they are. `--literal`: the words joined by spaces, sent as text (`send-keys -l`) |
| `force prompt NAME TEXT… [--wait] [--until S]… [--timeout SEC] [--json]` | TEXT = the words joined by spaces (`-` = read stdin). Paste with `load-buffer` + `paste-buffer -p -r -d` (bracketed paste when the app asked for it, LF kept), 0.15 s later Enter |
| `force wait NAME [--until S]… [--timeout SEC] [--json]` | returns when the state is one of S (at once if it already is) |

- `prompt` refuses, sending nothing: `no_session`, `archived`, `down`, `not_agent` (a shell session),
  `not_ready` (starting), `blocked` (needs-you: answer the dialog with `force keys`).
- `prompt --wait`: first wait (≤ 5 s, or less if --timeout ends first) for working or needs-you, else
  `prompt_stalled`; then wait for `--until` (default your-turn, idle, needs-you). `--until` needs `--wait`.
- `wait` default `--until`: your-turn, idle, needs-you. No `--timeout` = wait for ever; timeout = `timeout`.
- Output: nothing on success except `wait` / `prompt --wait`, which print the final state name. `--json`: the
  session `{"name","state","activity","since_ms"}` on stdout; errors as `{"error": CODE, "message": …}` on
  stderr. Errors without --json: `force: MESSAGE` on stderr. Exit 0 ok, 1 any error, 2 usage.
- A timeout or `prompt_stalled` does not prove nothing was sent: `force read` before retrying.
- In a ui that shows a remote host these run there (`_FORWARDED`, §40), like the other session commands.

### 43.2 Reported state: `force report`
`force report [NAME] --state working|needs-you|your-turn|idle [--summary TEXT] [--pid PID]`, `--clear`.
NAME defaults to the Force session of the calling pane (`$TMUX_PANE` on Force's host server); outside one it is
an error. Stored by `force/report.py` in `$FORCE_STATE/reports/NAME.json`; `idle` or `--clear` deletes it.

- Lifetime: working / needs-you hold while the reporting process lives (`--pid`, default the caller's parent,
  i.e. the script that ran `force report`; `--pid 0` = until replaced). your-turn holds until the session is
  viewed after the report (`seen.last_seen_ms > at_ms`). The daemon deletes expired, unreadable and orphaned
  (no live registry entry) report files.
- Effect: a shell session takes the reported state (WORKING / NEEDS_YOU / YOUR_TURN, since = at_ms) and the
  summary as its activity. An agent session keeps its own state; the summary is its activity only when it has
  none of its own.
- Sidebar: a shell card in a reported state is drawn like an agent card in that state; its line 2 is the
  summary when there is one (also for needs-you and your-turn), else the state label.

### 43.3 Why a card shows its state: `force explain`
- `state.compute_state_why()` returns (state, reason) — one short clause per rule of the §5 table;
  `compute_state()` = its state. The daemon adds where the status came from (Claude status file and its age;
  OpenCode API answered / missed and the last state held for N s (§36); trust-folder screen; report) and
  stores `why` (clauses joined by `; `) in the snapshot per session.
- `force explain NAME [--lines N] [--json]`: from the snapshot: kind, state, since, viewed, activity, title,
  why, snapshot age; live: agent pane id, pid, current command, dead or not, the report file if any, and the
  last N (default 8) non-blank screen rows. Works with a stale snapshot (says so).

### 43.4 Pane titles on cards
- The daemon reads `#{pane_title}` of the agent pane. Cleaned: leading non-alphanumeric glyphs (spinners,
  `✳`) and spaces stripped, an `OC | ` prefix dropped, at most 60 characters. Dropped (None) when empty or equal
  (case-insensitive) to the session name, the host name (also with a domain, `name.local`: tmux's default title
  on macOS), or a bare program name (claude, opencode, bash, zsh, sh, fish). Snapshot field `title`. Claude Code titles itself with the session name, so in practice titles
  show for OpenCode and for programs in shell sessions.
- Sidebar line 2: the title replaces the plain label `idle` / `stale` and the `shell <path>` of a shell card.
  your-turn, needs-you, working, notes (restarting…) and outdated tails are unchanged.

### 43.5 Registry history
- `registry.save()` keeps copies in `registry-snapshots/` next to registry.json: the saved content, at most one
  copy per 15 min (newest copy's mtime), skipped when equal to the newest copy, the newest 48 kept.
- `registry.load()` on corrupt content: before raising, copies the bytes to `registry-backups/` (one copy per
  distinct content, newest 3 kept). Copies are never restored automatically: stop the daemon, copy one over
  registry.json, start it. Failures to copy are ignored (they never break a save or a load).

## 44. One menu key, click-only sidebar

Ctrl+Space opens the Force menu directly; every feature stays; terminal settings are not touched; the sidebar has no
keyboard mode. The keys that reach tmux in each terminal determine the bindings:

| finding | consequence |
|---|---|
| Tilix takes Alt+1…9, Alt+arrows, Ctrl+Tab, Ctrl+PgUp/PgDn (its own pane switching); Terminator takes Alt+arrows, Ctrl+Tab, F1; GNOME Terminal Ctrl+Tab, F10 | those keys never reach Force there |
| Ctrl+Space, Ctrl+\\, Alt+PgUp/PgDn reach tmux in kitty, Konsole, Tilix, Terminator and GNOME Terminal, with the shell idle, scrolled or in copy mode | the dependable keys |
| a hidden prefix gives no sign it waits; a letter typed quickly after a `run-shell` command went to the session | replaced |
| a `display-menu` run by the binding itself, or by `source-file` of a file, is open before tmux reads the next key; a menu built by a script (run-shell) is not | the menu is static config |

| part | behaviour |
|---|---|
| keys | `prefix None`, `prefix2 None`; `bind -n C-Space` and `C-\` → `source-file -F #{@force_app}/share/menu-<classic\|split>.tmux`; no prefix table bindings |
| menu files | generated by `force/menus.py` (`python -m force.menus`; `--check` in tests) from one table: key, label, action, core. Classic and split differ only in the commands (split: dialogs get `FORCE_HOST/CLIENT/SESSION/GLYPHS`, session actions go through `ui-key.sh`) |
| items | `j` jump · `Tab` previous · `h` host · `n` new · `t` new shell · `r` rename · `f` folder · `K`/`J` move up/down · `x` restart · `u` upgrade · `a` archive · `d` delete · `A` archived · `e` restart all · `b` sidebar · `H` health check · `U` update Force · `?` help · `q` quit. Move up/down uses `K`/`J` (an item's key beats the menu's own navigation, so arrows stay free) and host / health check use `h` / `H` |
| title | `Force · <session>` (truncated to 24): the session the items act on |
| short terminals | `if -F #{client_height}`: full menu with separators (24 rows), without (21), core items with separators (16), core (13), else a note |
| click | the header's controls button: `MouseDown1Pane` sources the same file (with the click: `-O`, items clickable); the header's fallback (no tmux catch) runs `send-keys -K -c <client> C-Space`. The host chip keeps share/menu.sh + `force hosts-menu` |
| switching | `M-PPage`/`M-NPage` previous/next (every terminal); `M-1…9`, `M-Up/Down` kept |
| sidebar | no keyboard mode: no cursor, no hint rows; any key hands focus to the agent pane |
| help | menu key `k`: share/help.ansi, written by `python3 -m force.cards` (a test keeps it current): the same card as Chuck's (title and rule, keys in coral, "any key Close"), 18 lines, popup 72×20 with the cards' style (`-b rounded`, border `fg=#4e4e4e`, surface `#212121` as the bars), no cursor |
| look | every popup and menu in the cards' style (force/cards.py): the bars' surface `#212121`, rounded `#4e4e4e` borders; the menus (Force menu, hosts) a coral Nerd Font icon per item in the rich tier and a styled title, selected `#363636` white bold; the dialogs a coral icon and white bold title over a `#3c3c3c` rule, the main action as a coral button and the other keys in grey on the bottom right (a blank row under them, so each dialog is one row taller); the health check (`force doctor --fix --card`) green ✓ and red ✗ rows |

### 44.1 Copying without select mode

Select mode is not needed (Force puts copies on the local clipboard, §41). In a shell session a selection
disappears automatically, and in a Claude Code session the selection stays and a right click copies it.

| part | behaviour |
|---|---|
| shell (no mouse taken) | drag = copy mode; `share/copy-mode.tmux` (generated by force/menus.py, sourced by both configs): `MouseDragEnd1Pane` stop-selection (the selection stays), `MouseDown3Pane` copy-pipe-and-cancel (clipboard), `MouseDown1Pane` cancel, double/triple click select word/line, Esc cancel, arrows and PgUp/PgDn move |
| typing in copy mode | every typing key (printable ASCII, Space, Enter, BSpace, Tab, C-c, C-d, C-u, C-l) has its own binding `cancel ; send-keys <key>`: back to the shell with the key. Not `Any`: tmux cannot name the caught key, and `Any` would swallow Ctrl+Space and the Alt keys |
| agent sessions | unchanged: the agent takes the mouse (its own selection, right click copies through OSC 52) |
| tested | tests/test_keys.py: drag stays, right click copies and returns, typing returns and reaches the shell, a click returns |

## 45. Any terminal, no setup

No Force terminal profiles or launchers: type `force` in any terminal and it looks like Force. The only
optional extra is `force fonts`.

| Part | |
|---|---|
| theme | both tmux configs paint it: `window-style`/`window-active-style` `fg=#d0d0d0,bg=#262626`, `cursor-colour #d97757`, `pane-colours[0..15]` = Force's palette. tmux turns default and palette colours into RGB, so the terminal's own theme never shows inside Force (only its few pixels of padding), and nothing has to be restored on exit. OSC 10/11 colours work in kitty, VTE and Konsole, but Tilix keeps painting its own background and Konsole ignores OSC 110/111, so the theme itself is tmux's; in split mode the text colour is the exception below |
| text colour | an explicit `fg` turns *default*-coloured text into RGB, and VTE (GNOME Terminal, Tilix) draws SGR 2 (dim) on an RGB colour at full strength: Claude Code's prompt suggestion, dim on the default colour, looked like typed text. So in split mode the text colour reaches a terminal as its own default where that default can be made Force's: the host server forces no `fg` (`window-style "bg=#262626"`, set by the daemon in split mode; classic keeps `tmux.conf`'s), `force` asks the terminal's text colour (OSC 10 `?`) with its background, sets it to `#d0d0d0` and asks again; only when the answer is `#d0d0d0` (each channel within 2: urxvt reports it as `#cfcfcf`) does the terminal's ui window get `window-style`/`window-active-style` `bg=#262626` (session option `@force_term_fg on`, carried by `force ui-host`). Any other terminal (no answer, a different one: mosh, a tmux or screen around Force, PuTTY) keeps the ui server's `fg=#d0d0d0`, exactly as before, so text never takes a light theme's dark colour on Force's background. The client's sh puts the reported text colour back on exit, as it does the background |
| terminal edge | the pixels left below the last row / right of the last column (and a scrollbar) are the terminal's own, out of reach of any program. `force` asks the terminal's background (OSC 11 `?`, in the same DA1-ended round trip as the cell shape; only when stdin and stdout are the terminal) and, if it answered and is not already #262626, sets it to #262626 and attaches through `sh -c 'tmux …; printf OSC 11 <original>'`, which puts exactly the reported colour back however the client ends (also Konsole, which has no OSC 111). Works in GNOME Terminal, Konsole, kitty, Terminator; Tilix answers but keeps drawing its profile colour there, and scrollbars stay — no terminal-profile changes are made, so that is accepted |
| icons | `force` (glyphs `auto`) states `rich` for its tty (§28 `@force_client_glyphs`) when it is not an ssh login and `fc-list` has a "Symbols Nerd Font…" family: fontconfig falls back to it from any font in kitty, VTE and Konsole. Over ssh, or without the font: the `rich_terminals` rule. `--glyphs` still overrides |
| intro cell shape | when the tty reports no pixel size (VTE), `force` asks `CSI 14 t` (text-area pixels, answered by VTE, Konsole, kitty) with a DA1 sentinel, 0.5 s at most, termios restored; aspect = (h/rows)/(w/cols), used if 1–4, else the config's `intro_cell_aspect`. Only when the intro plays. The intro fades to `#262626` (the tmux theme), not the terminal's default |
| resize settle | the ui server's `client-resized` hook also runs `share/settle.sh`: 0.4 s after a terminal's last resize (debounced per client, token in `$XDG_RUNTIME_DIR/force-settle/`) it sends SIGUSR1 to the window's sidebar and header (every cell repainted) and runs `refresh-client` for the window's nested client on its host (`@force_host_tmux`, at most 8 s). A shrink drops alternate-screen rows from the top; a repaint missed during a drag left the window shifted and half blank (seen in Tilix) |
| host switch | `force ui-host` builds the new ui session with the old one's `@force_glyphs` (and the first attach with the client's stated tier) set before its sidebar and header start: they would otherwise start plain, miss tier.sh's USR1 (sent only to panes already running the Rust program) and turn rich at their next 5 s read |
| dropped | the font (the terminal's own; no font is installed), the first window size (Force adapts: sidebar rail below 110 columns), Shift/Ctrl+Enter mappings (§41) |
| `force terminals` | deprecated alias: says profiles are gone and runs `force fonts` (`--uninstall` → `--uninstall`, `--no-fonts` → `--cleanup-only`) |

## 46. Production install: one line, no setup

`curl … | sh`, then type `force`, and it works; settings live in a simple JSON file in `~/.config/force`;
production is easy.

| Part | |
|---|---|
| release build | `scripts/build-release.sh [X.Y.Z]`: an Alpine/musl container (`build/Dockerfile`, pinned + checksummed tmux 3.4, libevent 2.1.12, ncurses 6.5 with the terminfo dirs of every common distro) builds static sidebar, header, dialog, intro and tmux; the tarball `force-VERSION-linux-ARCH.tar.gz` (+ `.sha256`) has the Python package, share/ (+ `bin/`, `tmux-bin/`, `terminfo/` with tmux/tmux-256color), systemd/, VERSION, licences. Output is a web root: `install.sh` + `releases/`. x86_64 here; aarch64 from CI later |
| `install.sh` (published) | `share/get-force.sh`, POSIX sh, filled by `scripts/fill-installer.py` (VERSION, base, releases URL, SHA-256 per arch): Linux + x86_64/aarch64 + not root, curl or wget, download, verify, unpack into `<data>/releases/VERSION`, a Python ≥ 3.11 (the system's, else python-build-standalone 3.12.15 / 20261013, pinned and checksummed, into `<data>/python/`), then `force.setup`. Options `--no-fonts`, `--no-services` |
| `force/setup.py` | checks the release, parses both tmux configs on a throwaway server, switches `<data>/app`, writes `~/.local/bin/force` (`#!PY -I`, the app path on sys.path), PATH blocks for bash (top of .bashrc, also for `ssh -t HOST force`), zsh (.zshenv), fish (conf.d), the units (enable --now force-tmux + force; restarts only force.service; never force-tmux), linger (a note when refused), the icon font on a desktop, hints (older install, no Claude Code), keeps 5 releases. A missing program is exit 127, never a crash. Dev releases (no VERSION) are named after their dir |
| without systemd | `force` starts `tmux -L force -f conf start-server` and a detached `force daemon` (log `<state>/daemon.log`) when the daemon lock is free |
| first run | no prompt: a `shell` session in $HOME; once per machine a welcome line (`<state>/welcomed`) |
| `force update [--check]` | fetches `<base>/install.sh`, compares its `VERSION=` with `<app>/VERSION` and runs it when newer; dev builds and builds without a base say so. The daemon checks 2 min after it starts, then hourly, and soon after a terminal opens the cockpit (a tmux client name, host or ui server, not attached on the last tick), at most once per 10 min (`checkForUpdates`). Each check after the first sends the last `ETag` / `Last-Modified` (kept in `<state>/update.json`): an unchanged `install.sh` is a `304` with no body. The site caches `install.sh` for 10 min, so a release reaches installs within about an hour, usually sooner; nothing pushes it (no server). the snapshot carries `update`, the sidebar footer shows `↑ Force X.Y.Z · force update` (below any error). A click on that note, or the menu's `U` "Update Force…", opens the update card (`force update --ui`, a 72×20 card popup): the running version, a live check, then `Force X.Y.Z is available` with Enter (Update) and Esc, or that it is the newest, or why it could not check; Enter runs the update in the card (install.sh's lines shown, then installed or failed). In a ui window showing a remote (`FORCE_HOST`) the card runs on that host (`ssh -t … force update --ui`); a remote whose Force has no card (exit 2) gets a plain `force update` there |
| `force uninstall [--purge] [--yes]` | stops and removes units, launcher, PATH blocks, releases, private Python, OpenCode copy; keeps registry, archive, settings unless `--purge` (then also the icon font) |
| other machines | `force install-to HOST` / `force host upgrade NAME` copy the release and run its setup there with that machine's Python 3.11+ (or the private one) |
| developer install | `./install.sh`: test venv `<data>/venv`, `./deploy.sh`, then setup |
| tested | fresh Ubuntu 22.04 / 24.04, Debian 12, Fedora 41 containers as a normal user: install, then `force` shows the cockpit with services started without systemd |

## 47. Phone names follow renames

A renamed session keeps its old name in Remote Control (the phone): Claude Code reuses a resumed
conversation's Remote Control session with its first title; `-n` / `--remote-control NAME` on resume and a
`/rename` passed as the startup prompt do not change it; a `/rename` typed once Remote Control is connected
does. An applied rename (§8) and an import (§11) set `extra.title_sync` (+ `title_sync_at`); the
daemon step `title_sync` types `/rename NAME` + Enter into the agent pane when the session is IDLE /
YOUR_TURN / STALE, Remote Control is connected (the status file names its session, or the screen says
"remote-control is active") and the last `❯` line is empty (or the dim example prompt), so a draft is never
touched; then it clears the flag (gives up after 30 min only when Remote Control never connected; a draft or
open menu is waited out). `force sync-titles [NAME…]` queues it for live Claude sessions (all by default), to
fix names that drifted.

## 48. One rule language in the sidebar

The sidebar uses the header's style throughout: `─` rules (#3c3c3c) with labels set into them, no filled
chips.

| part | now |
|---|---|
| brand | 3 rows: blank, ` <bolt>  F O R C E ` centred in a rule (letters #a0a0a0, not bold; bolt coral; PLAIN no bolt), blank. It is the top of the session list |
| actions | `─ + new ─── archived N ─`: one rule, left half opens new, right half archived; stacked when too narrow |
| dock | blank, a rule with the host centred in it (icon coral, name #c6c6c6; red with `· offline` / `· other version` when down; a click opens the host picker), blank, `5h`/`wk` slim bars, blank. No host: a plain rule. Footer notes go on their own row above it |
| centring | labelled rules run over columns `1..w` (the free column on the left mirrors the invisible pane border on the right); the icon's gap is 2 cells (the RICH icon slot) or 1, whichever leaves the same `─` on both sides (`model::icon_gap`) |
| limits | `   5h ━━━╸━━━━━━  34% 14:41  `: label in the cards' icon column, slim bar (grey < 70 %, amber, red ≥ 90 %), reset right-aligned, 3-column margins counting the border |
| header | while the window shows the sidebar, attention items are counts (`<bell> 2`, `✓ 1`, `◐ 1`, a click goes to the first); without it they name the sessions |
| colours | needs-you #f0645e (clear of the own card's coral edge); text that carries information is at least #8a8a8a (ages, activity, model/effort, limits); your-turn activity #79a580 |

## 49. Agent kinds, limits per provider, shell status, notifications, agent guide, automatic names

Agent kinds (Claude Code, OpenCode and a shell) are modelled uniformly; the sidebar plan limits are per provider
(they are Claude's and misled next to OpenCode sessions). Shell status is read without a script, and notifications,
an agent guide and automatic names are provided.

### 49.1 One module per agent kind (`force/kinds.py`)
Everything that differs between Claude Code, OpenCode and a shell sits behind one `Kind` class, so a new agent is
one more class and no new branch in the daemon. `kinds.of(entry)` / `kinds.get(name)`; `kinds.ALL`.

| member | does |
|---|---|
| `name`, `label`, `mark` | `"claude"`, `"Claude Code"`, `✻` (the card's kind mark, §36) |
| `available(paths, cfg)` | installed here (`force kinds`, the New dialog) |
| `exec_argv(cockpit, entry)` | `force run` stage 2: `(argv, env)` to exec; a shell runs in stage 1 (`direct = True`) |
| `read(d, entry, pane)` | `Reading(status, meta, version, source)`: the `AgentStatus` the §5 rules use, kind data for the card, the running version, the `why` clause (§43.3) |
| `latest_version(paths)` | the installed version an upgrade restarts onto (`None`: never outdated) |
| `card(d, entry, pane, reading)` | `(activity, model, effort, mode)` for the snapshot |
| `follow(d, entry, reading)` | keep the registry on what the agent does (Claude: new session id, a `/rename`; OpenCode: the session it works on) |
| `limits(paths)` | the provider's plan limits for the dock (§49.2), or None |
| `first_prompt(paths, entry)` | the first thing the user typed, for automatic names (§49.6) |
| `name_in_agent` | the agent carries the name (Claude: `-n`, Remote Control): a live rename restarts it (§8) |

`d` is the daemon (its per-kind caches stay on it). Kind-specific operations that are not on every tick keep their
place but ask the kind first: move (`rehome`), import (Claude only), smoke.

### 49.2 Limits per provider
- The dock's limits belong to a provider: snapshot `limits = [{"kind", "label", "five_hour", "seven_day"}]`, one
  block per kind whose `limits()` has data **and** that has a live session on this host. Claude: `usage.json` from
  the status line (§21), label `Claude`. OpenCode: none (pay-per-token providers have no plan windows). A host with
  only OpenCode sessions and shells shows no limits. `usage` stays in the snapshot (the first block's windows).
- Sidebar: each block is a label row, its windows, a blank row. The label row takes the blank row under
  the dock rule: the kind mark in the cards' icon column (✻ coral) and the label in the limits' grey
  (`   ✻ Claude`). The rail shows the first block only, unlabelled.

### 49.3 Shell status without a script
- The daemon reads the shell pane's `#{pane_current_command}`. A shell (`kinds.SHELLS`) or the pane's own first
  program → SHELL. Any other program that has run for 2 s → WORKING, activity `running <program>` (a
  prompt hook never gets there); interactive programs (editors, pagers, `ssh`, `top`/`htop`, `man`, `tmux`, REPLs,
  agents: `kinds.INTERACTIVE`) stay SHELL with the program as the card's title.
- A program that showed as working and returned → YOUR_TURN (`finished`) until the session is seen, like an
  agent's turn (the same busy → idle step on `seen.json`).
- `force report` (§43.2) wins over all of this. `force status STATE [--summary TEXT]` is a short alias of
  `force report --state` for scripts.

### 49.4 Notifications (`notifyCommand`)
- Setting `notifyCommand` (string, default empty = off): a shell command (`sh -c`) the daemon runs when a session
  newly enters NEEDS_YOU (`needs-you`), YOUR_TURN (`finished`) or DOWN after it was up (`down`). Any kind,
  shells included (§49.3).
- Environment: `FORCE_EVENT`, `FORCE_SESSION`, `FORCE_KIND`, `FORCE_HOST`, `FORCE_STATE` (state name, §43.1),
  `FORCE_MESSAGE` (one line: `<session>: needs permission`, `<session>: finished`, …), `FORCE_VIEWED` (1/0).
  Examples: `ntfy publish mytopic "$FORCE_MESSAGE"`, `notify-send Force "$FORCE_MESSAGE"`, a `curl` to a push
  server.
- Not while the session is viewed (setting `notifyWhenViewed`, advanced, default false). At most one event per
  session per 30 s; the first sight of a session after a daemon start never notifies. The command runs detached
  (own process group, stdin /dev/null, output to the daemon log, killed after 30 s); it never delays a tick.
- `force notify-test [EVENT]` runs the command once with a sample event and prints its exit status and output.

### 49.5 A guide for agents (`force skill`)
`force skill` (also `force --skill`) prints a plain-text guide (`share/skill.md`) an agent can read to drive Force:
list, new, prompt/wait/read/keys, report/status, explain, rename, archive, settings, notify-test, hosts. Kept
short, every command with its exact syntax; `tests/test_skill.py` checks each command it names parses.

### 49.6 Automatic names
- `force new` without a name (and the New dialog with an empty name: "named after your first message") creates the
  session as `new`, `new-2`, … with `auto_name: true` in the registry.
- The daemon (every 3 s per session) names it once the agent's first prompt is known (`kind.first_prompt`;
  OpenCode: the title OpenCode gives the session, else the first message once the first turn is over):
  `kinds.name_from_prompt(text)` = slug of the words (leading fillers like please/can you/hey dropped), cut at a word
  boundary to ≤ 20 characters; taken → `-2` … `-9`; nothing usable → keeps its name. `auto_name` is dropped
  after the attempt and by any manual rename.
- Renamed like an agent's own `/rename`: registry, tmux session and seen key at once, no restart; a Claude session
  gets the name into Claude Code and Remote Control by the title sync (`/rename NAME` typed when idle, §47).
- Name adoption (§6.3) takes only a CHANGE of the name in Claude's status file (seen on an earlier tick of the
  same process): the name Claude was started with is older than a rename Force made without a restart.

## 50. Chuck, the support assistant

One reused agent session, `chuck` (registry extra `role: "support"`), that answers questions about Force, sets
it up on request, drafts bug reports and feature requests after a duplicate search, and (fix mode) builds a fix
and a pull request from the user's fork. It runs on the user's own agent (`supportAgent`: auto = the first
installed of Claude Code, OpenCode) and plan; nothing reaches GitHub without the user's click.

| Piece | Rule |
|---|---|
| Entry | menu key `?` ("Ask Chuck…"; the key reference moved to `k`), the sidebar's `ask chuck` row (face U+F118 in coral; plain `?`; the rail too), `force support`. All show the card (who he is; one row per use; that he runs on the user's agent and plan and nothing goes to GitHub without a click; Enter as a coral button; the cursor hidden) in a 72×17 popup in the cards' style (force/cards.py: `-b rounded`, border `#4e4e4e`, surface `#212121` as the bars); Enter opens his session; `n` ("New conversation", `force support --new`) opens it on a new conversation: a new conversation id (OpenCode: its session dropped) and a restart, which cuts a running turn; the old conversation is not deleted (the agent's own `/resume` still lists it). Chuck always runs on this machine. In a ui window showing a remote host, only the main pane shows him (`overlay_open`): its host options point here (`@force_host` empty, `@force_host_tmux` local, `@force_nested` the new client, `@force_session` chuck), the remote's are kept in `@force_overlay_back`, `@force_overlay` = his name; the sidebar and header keep the remote's list. Going to another session brings the remote back first (`force ui-main --window W [--target N] [--wait] [--dialog K --client C]`): a card or header pill (`--target`), Alt+N, next, previous, last, new shell, restart all, move up/down (`ui-key.sh`), and the menu's jump, new and archived (reopened by ui-main with the remote's values). The shown session's own actions (restart, upgrade, rename…) act on Chuck. |
| Session | created once in `<home>/support`, brought back from the archive, never a second one (`chuck-2` when `chuck` is taken by another session). A normal session underneath, but not in the session list: no card in the sidebar or rail, not in the jump dialog, the header's pills and working count, `force list` (`--json` keeps him, `"support": true`), no index (Alt+N, next and previous skip him; from Chuck, next is the first and previous the last). His row shows him: lit (coral rule, white bold label) while the window shows his session, so no card is current then; otherwise the label takes his state's colour (needs you, working, your turn). |
| Context | `share/support/`: `CHUCK.md` (persona, rules, fix mode; written as `CLAUDE.md` and `AGENTS.md` with the version and app path filled in), `faq.md`, `architecture.md`; generated per release by `scripts/support-pack.py`: `docs.md`, `commands.md`, `contributing.md`. Claude Code: `claudeMdExcludes` for the user's CLAUDE.md files and rules, `autoMemoryEnabled: false`, `--mcp-config` with Force's server only (`--strict-mcp-config`), its read tools allowed, its change tools and `git push` as explicit ask rules, `--permission-mode auto`, `--add-dir` the installed release, `--model` = `supportModel` when set. OpenCode: `opencode.json` in the folder (the server; `force_*` ask, the read tools allow; `model` = `supportModel`). The MCP change tools carry `_meta["anthropic/requiresUserInteraction"]: true` (a prompt in every mode). A new `supportAgent` starts him on a new conversation on that agent (the same session, as `n` on the card); a new `supportModel` restarts him (same conversation). |
| `force mcp` | stdio MCP server (JSON-RPC, one message per line; protocol 2024-11-05 … 2025-11-25), tools run `$FORCE_BIN …` as subprocesses. Read: status, settings_get, hosts_list, doctor, version, logs_tail, issues_search, issue_link. Change (approved per call): settings_set/unset, host_add (ssh BatchMode check first)/remove/upgrade, session_new/rename/move, doctor_fix. Never: delete, archive, uninstall, update, keys or prompts into sessions. Changes logged to `<state>/support/actions.jsonl`. |
| GitHub | search: the public search API, no login (`repo:… is:issue`). Issues: `issues/new?template=…&title=…&<field id>=…` (fields shortened, logs first, to stay ≤ 8000 characters). Comments: the text copied, the issue link printed. Pull requests: `compare/main...USER:force:BRANCH?quick_pull=1&title&body` with a Conventional title and "Fixes #N". Copies go to tmux's buffer with `-w` (OSC 52) and `clip-set.sh`. `FORCE_SUPPORT_REPO=owner/name` (testing only) points search and links at another public repository. A vulnerability never goes to GitHub (SECURITY.md): `force support security --title T --file F` copies the report and prints a `mailto:hello@useforce.cc` link (subject "Security: T", the body when the link stays ≤ 1800 characters). |
| Fix mode | `force support fix N [--user NAME]`: the user's fork cloned once to `<support>/src` and reused for every fix (the name kept in its git config `force.githubUser`), remote `upstream`, branch `fix/N-slug` from `upstream/main`, refused while the branch checked out has uncommitted work; tests in the dev image, tries in the `./dev.sh` sandbox; push only after a yes; `force support pr --title …`. |

## 51. macOS
Force runs on macOS on Apple Silicon (arm64) like on Linux: the one-line install, local agent and shell sessions,
remotes, `force update` and uninstall. Intel Macs are not built. CI proves the install and the cockpit on GitHub's
macOS runners for every pull request; it was a beta until 1.2.0 had been used on a real Mac (Ghostty).

| Part | Linux | macOS |
|---|---|---|
| Process facts | `/proc` | `force/procinfo.py`, the one place that reads them: on Linux `/proc`; on macOS libproc through ctypes (`proc_listallpids`, `proc_pidpath`, `proc_pidinfo` PROC_PIDTBSDINFO for the parent, name and start time) and `sysctl` KERN_PROCARGS2 for the argument vector. Functions given an explicit root read that directory in the `/proc` format (tests) |
| Snapshot watch (sidebar) | inotify on the snapshot's folder | kqueue EVFILT_VNODE (NOTE_WRITE) on the folder: an entry added or renamed in it, then the same read |
| Host name (sidebar, header) | `gethostname()` | `gethostname()` |
| Shell scripts | POSIX sh plus `flock(1)`, `timeout(1)`, `sha256sum` | no GNU tools assumed: the toggle lock is a mkdir lock, `timeout` is used only when present, `shasum -a 256` stands in for `sha256sum` |
| Services | systemd user units, linger | launchd agents in `~/Library/LaunchAgents` (§51.1) |
| Binaries | static musl, built in the Alpine container | built on a macOS runner: the Rust programs for `aarch64-apple-darwin`; tmux 3.4 with libevent and ncurses linked in (only the system library is dynamic); ad-hoc signed by the linker |
| Python | the system's 3.11+, else python-build-standalone | macOS has 3.9 at most, so python-build-standalone `aarch64-apple-darwin` (same pin) |
| Download | `force-X.Y.Z-linux-ARCH.tar.gz` | `force-X.Y.Z-darwin-arm64.tar.gz`; files fetched by curl carry no quarantine flag, so no notarisation |
| Icon font | `~/.local/share/fonts`, fc-list | `~/Library/Fonts`; the rich tier as on Linux (kitty, Ghostty, WezTerm; a stated `--glyphs rich` for iTerm2 with the symbols font) |
| Clipboard | OSC 52; wl-copy / xclip / clip-hold | OSC 52; `pbcopy` |

CI runs the Python tests and both crates on `macos-26` for every pull request, and an end-to-end job installs the
built darwin tarball as a user would, starts the services, opens a shell and an agent session (a stand-in `claude`),
runs `force smoke`, updates from the previous release and uninstalls, keeping sidebar screenshots as artifacts. A
manual workflow opens an SSH session into a macOS runner for the maintainer's key only, for live debugging.

### 51.1 launchd services
`force/services.py` is the one place that talks to the service manager: systemd (`systemctl --user`, linger,
`journalctl`) on Linux, launchd on macOS, neither (Force starts its tmux server and daemon itself, §1) when the
manager does not answer. Three LaunchAgents in `~/Library/LaunchAgents`, written by setup with `plistlib`:

| Label | Runs | Keys |
|---|---|---|
| `cc.useforce.force-tmux` | `tmux -L force -f <app>/share/tmux.conf -D` | RunAtLoad, KeepAlive, ThrottleInterval 3 |
| `cc.useforce.force` | `~/.local/bin/force daemon`, `FORCE_SERVICE=launchd` | RunAtLoad, KeepAlive, AbandonProcessGroup (like KillMode=process), stdout/stderr to `<state>/daemon.log` |
| `cc.useforce.force-smoke` | `force smoke --if-due` hourly (self-test only) | StartInterval 3600, Nice 10, ProcessType Background |

They live in the `gui/<uid>` domain (`launchctl bootstrap` / `bootout` / `kickstart [-k]` / `print`), so they run
while the user is logged in; there is no linger. Setup never reloads a loaded, unchanged tmux agent (it holds
the sessions). A daemon started by launchd starts the tmux agent (`kickstart`), never a bare server, as under
systemd.

## 52. Messages between hosts
An agent on one host drives a session on another with the §43.1 commands, addressed `HOST:NAME` (or `--host
HOST`); session names never contain `:`. Force's own ssh connection (§40) carries it; nothing new to run.

| Part | |
|---|---|
| HOST | a name from this machine's `hosts.json`, else a remote whose last hello named that machine (its short host name, `status.json` `detail`), else a **peer**: a host whose daemon watches this one (below). This machine's own short name means local. Anything else: `no_route` |
| this machine connects to HOST | `prompt`, `wait`, `read`, `keys`, `explain` run there over ssh (`force CMD NAME [options] --from SENDER -- TEXT…` for prompt and keys, so a word starting with `-` stays text), with the local rules and options (`--wait`, `--until`, `--timeout`, `--json`, `-` = stdin, which is passed through ssh) and exit codes |
| HOST watches this machine (a peer) | only `prompt`, without `--wait`: queued in `<state>/outbox/HOST/`, streamed to HOST over its `force watch` connection and delivered there by its daemon (`force prompt NAME - --from SENDER`). The sender gets `queued` (exit 0); the outcome is in HOST's message log. A peer = `<state>/peers/HOST.json` written by a live `force watch` (its pid), which reads the watching host's name as a first stdin line `{"peer": NAME}`; older readers send none and are no peer. Lines `{"prompt": {…}}` are ignored by older readers; the reader delivers one only when its sender's host is that remote (its name here or the name its hello gave), so a host cannot speak for another one. Queued messages older than 10 min are dropped (logged) |
| sender | `--from SESSION@HOST`, filled in by Force: the calling pane's Force session (as `force report`, §43.2), else none (`@HOST`); HOST = this machine's short host name. Never typed by hand |
| receiving host | `prompt` and `keys` with `--from` are refused (`not_accepted`, nothing sent) unless the setting `acceptPromptsFrom` (a space- or comma-separated list of host names, `*` = any; default empty = off) lists the sender's host. The §43.1 refusals still apply (`blocked` for a session that needs you, …). `read`, `wait`, `explain` need no setting (they change nothing; ssh access already reaches them) |
| header | a delivered prompt starts with one line `[from SESSION on HOST · reply: force prompt HOST:SESSION "…"]` (`[from HOST]` without a session), then the text |
| log | every cross-host prompt and keys, sent, queued, delivered or refused, as one JSON line in `<state>/messages.log` on both hosts: time, direction, from, to, command, result, length (never the text) |
| trust | this is consent, not authentication: whoever can ssh to a host can run anything there. `acceptPromptsFrom` stops agents from driving another host's sessions unless its owner opted in |
| agents | `share/skill.md` documents `HOST:NAME`, the header and replying |

## 53. Remote Control per session
A Claude Code session starts with Remote Control (`--remote-control NAME`, §7.2) unless it was created without it.

| Part | |
|---|---|
| New dialog | a checkbox `[x] Remote Control` under Folder, shown only while the Claude Code chip is selected, checked by default. Focus order Kind → Name → Folder → Remote Control; Space toggles it when focused, Ctrl+R toggles it from any field, a click on its row toggles it; Enter on it creates |
| `force new --no-remote-control` | the dialog passes it only when unchecked (a checked box sends nothing, so an older remote Force still works). Refused for OpenCode and shell sessions (they have no Remote Control) |
| registry | `extra.remote_control: false` on the entry; absent means on. Kept for the session's life: restarts, upgrades, renames, moves and unarchive start it the same way |
| title sync (§47) | a session without Remote Control does not wait for a connection: `/rename NAME` is typed once it is idle with an empty input line, so Claude Code's own name follows renames and automatic names (§49.6) |
| MCP `session_new` | optional boolean `remote_control` (false = `--no-remote-control`) |
| `FORCE_NO_REMOTE_CONTROL` | still drops it for every session (`force smoke`, §37) |
