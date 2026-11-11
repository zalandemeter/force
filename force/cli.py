"""Command line interface (SPEC §8)."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import shlex
import signal
import socket
import threading
import types
import uuid
from pathlib import Path

from . import automation, claude, config, opencode, registry, share, state
from .config import Config, Paths
from . import remote as remote_mod
from .lifecycle import Cockpit, MoveError
from .registry import RegistryError
from .state import State
from .tmux import Tmux, TmuxError

SHARE = Path(__file__).resolve().parent.parent / "share"
THEME_BG = "262626"   # the cockpit's dark background, painted by tmux's theme (see the Force theme block in share/tmux.conf)


def make_cockpit(env: dict[str, str] | None = None) -> Cockpit:
    """Build the Cockpit object from env (default os.environ). Does not start tmux and imports no heavy UI."""
    paths = config.get_paths(env)
    cfg = config.load_config(paths.config_file)
    config.ensure_dirs(paths)
    tmux = Tmux(paths.tmux_socket, paths.tmux_conf)
    # an instance's launcher (SPEC §2.1) names itself in FORCE_BIN; the live install is `force` on PATH
    cockpit_bin = os.environ.get("FORCE_BIN") or shutil.which("force") or str(Path.home() / ".local/bin/force")
    # OpenCode: Force's own copy when managed (SPEC §36), else the one on PATH
    opencode.set_managed(paths.home / "opencode" if cfg.opencode_managed else None)
    return Cockpit(paths, cfg, tmux, cockpit_bin)


def _inside_cockpit(cockpit: Cockpit) -> bool:
    """True if $TMUX points at a socket whose basename equals the force socket name."""
    tmux_env = os.environ.get("TMUX")
    if not tmux_env:
        return False
    sock = tmux_env.split(",", 1)[0]
    return bool(sock) and os.path.basename(sock) in (cockpit.paths.tmux_socket, cockpit.paths.ui_socket)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_fresh_snapshot(cockpit: Cockpit, now_ms: int | None = None) -> state.Snapshot | None:
    """Snapshot if it exists and is < 5 s old, else None (caller computes live)."""
    snap = state.read_snapshot(cockpit.paths.snapshot)
    if snap is None:
        return None
    now = now_ms if now_ms is not None else _now_ms()
    if now - snap.generated_ms < 5000:
        return snap
    return None


def _find_agent_pane(cockpit: Cockpit, name: str, panes: list | None = None):
    panes = panes if panes is not None else cockpit.tmux.panes()
    for p in panes:
        if p.session == name and p.role == "agent":
            return p
    return None


def _to_pane_info(pane):
    return state.PaneInfo(
        session_exists=pane is not None,
        pane_alive=pane is not None and not pane.dead,
        pane_pid=pane.pid if pane else None,
        pane_started_ms=pane.started_ms if pane else None,
        dead_status=pane.dead_status if pane else None,
        dead_signal=pane.dead_signal if pane else None,
    )


def _compute_live_snapshot(cockpit: Cockpit, now_ms: int | None = None) -> state.Snapshot:
    """registry + tmux panes + read_status + compute_state (SPEC §8 list fallback)."""
    now = now_ms if now_ms is not None else _now_ms()
    reg = registry.load(cockpit.paths.registry)
    panes = cockpit.tmux.panes()
    try:
        clients = cockpit.tmux.clients()
    except TmuxError:
        clients = []
    viewed = {c.session for c in clients}
    seen_map = state.load_seen(cockpit.paths.seen)
    latest = claude.latest_version(cockpit.paths.claude_bin)
    live = reg.live()
    idx = {e.name: i + 1 for i, e in enumerate(live)}

    sessions: list[state.SessionView] = []
    for entry in live:
        pane = _find_agent_pane(cockpit, entry.name, panes)
        pane_info = _to_pane_info(pane)
        status = None
        if pane is not None and pane.pid:
            status = claude.read_status(cockpit.paths.claude_dir, pane.pid)
        seen = seen_map.get(entry.name, state.Seen())
        st = state.compute_state(entry, pane_info, status, seen, now, cockpit.cfg)
        version = None
        if pane is not None and pane.pid:
            version = claude.process_version(pane.pid)
        sessions.append(state.SessionView(
            name=entry.name,
            index=idx[entry.name],
            state=st,
            since_ms=status.status_updated_ms if status else (pane.started_ms if pane else None),
            version=version,
            outdated=(version is not None and latest is not None and version != latest),
            archived=False,
            order=entry.order,
            cwd=entry.cwd,
            session_id=entry.session_id,
            pid=pane.pid if pane else None,
            waiting_for=status.waiting_for if status else None,
            viewed=entry.name in viewed,
        ))
    for entry in reg.archived():
        sessions.append(state.SessionView(
            name=entry.name,
            index=None,
            state=State.ARCHIVED,
            since_ms=None,
            version=None,
            outdated=False,
            archived=True,
            order=entry.order,
            cwd=entry.cwd,
            session_id=entry.session_id,
            pid=None,
            waiting_for=None,
            viewed=False,
        ))
    return state.Snapshot(now, latest, sessions)


# --------------------------------------------------------------------------- #
# shared helpers used by CLI, attach, dialogs
# --------------------------------------------------------------------------- #

def available_kinds(cockpit: Cockpit) -> list[str]:
    """Session kinds this server can run (SPEC §36): "claude" when Claude Code is installed, "opencode"
    when OpenCode is, and always "shell". The new-session dialog offers only these."""
    from . import kinds
    return [k.name for k in kinds.ALL.values() if k.available(cockpit.paths, cockpit.cfg)]


def create_session(cockpit: Cockpit, name: str | None, cwd: str | None = None,
                  no_switch: bool = False, shell: bool = False, kind: str | None = None,
                  extra: dict | None = None) -> int:
    """Shared `force new` logic used by the CLI, attach and dialog new. Returns exit code.
    kind: "claude" (default), "opencode" (SPEC §36) or "shell" (registry extra kind; shell=True is the
    older spelling of kind="shell"). No name (SPEC §49.6): an agent session starts as `new`, `new-2`, … and is
    named after its first prompt; a shell is named after its folder. The name is printed on stdout then."""
    kind = kind or ("shell" if shell else "claude")
    auto = not (name or "").strip()
    name = (name or "").strip()

    def _fail(msg: str) -> int:
        print(msg, file=sys.stderr)
        return 1

    if not auto and not registry.valid_name(name):
        return _fail(f"force: invalid name {name!r}")
    from . import kinds
    if kind not in kinds.ALL:
        return _fail(f"force: unknown session kind {kind!r}")
    if kind == "claude" and not (os.access(cockpit.paths.claude_bin, os.X_OK) or shutil.which("claude")):
        return _fail("force: Claude Code is not installed. Install it with: "
                    "curl -fsSL https://claude.ai/install.sh | bash")
    if kind != "shell" and kind not in available_kinds(cockpit):
        return _fail(f"force: {kind} is not installed on this server")
    if cwd is None:
        cwd = os.path.expanduser("~")
    cwd = os.path.abspath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        return _fail(f"force: cwd {cwd!r} is not a directory")
    with registry.locked(cockpit.paths.home) as reg:
        if auto:
            base = (registry.slugify(os.path.basename(cwd.rstrip("/"))) or "shell") if kind == "shell" \
                else kinds.AUTO_NAME_BASE
            name = kinds.free_name(base[:28].rstrip("-"), reg.name_taken, upto=99)
            if name is None:
                return _fail("force: no free automatic name: give the session a name")
        if reg.name_taken(name):
            return _fail(f"force: name already taken: {name!r}")
        entry = reg.add(name, str(uuid.uuid4()), cwd)
        if kind != "claude":
            entry.extra["kind"] = kind
        if auto and kind != "shell":
            entry.extra["auto_name"] = True
        if extra:
            entry.extra.update(extra)       # e.g. role = support: Chuck's session (SPEC §50)
    if auto:
        print(name)
    # Create only after the lock released and the entry is on disk: the pane's `force run` reads it.
    cockpit.create(entry)
    if not no_switch and _inside_cockpit(cockpit):
        cockpit.tmux.switch_client(name)
    return 0


def _current_state_for(cockpit: Cockpit, name: str, now_ms: int | None = None) -> State | None:
    """Fresh snapshot state for `name`; falls back to live read_status (SPEC §8 archive)."""
    snap = _read_fresh_snapshot(cockpit, now_ms)
    if snap is not None:
        for s in snap.sessions:
            if s.name == name:
                return s.state
    pane = cockpit.agent_pane(name)
    if pane is not None and pane.pid:
        st = claude.read_status(cockpit.paths.claude_dir, pane.pid)
        if st is not None:
            if st.status == "busy":
                return State.WORKING
            if st.status == "waiting":
                return State.NEEDS_YOU
    return None


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def attach_command(cockpit: Cockpit, name: str | None, no_intro: bool = False, glyphs: str = "auto",
                   cell_aspect: float | None = None, remote_name: str | None = None) -> int:
    if _inside_cockpit(cockpit):
        print("force: already inside Force", file=sys.stderr)
        return 1
    glyphs = _client_glyphs(glyphs)
    # One question to the terminal (SPEC §45): its background, so the edge it paints itself (the pixels left
    # over below the last row / right of the last column) can match the theme while Force runs; and, for
    # terminals that report no pixel size (VTE/Tilix) when the intro will play, its cell shape (the client's
    # `--cell-aspect` first, the config's intro_cell_aspect last).
    want_aspect = cell_aspect is None and not (no_intro or not cockpit.cfg.intro or _animations(cockpit) == "off") \
        and not _tty_reports_pixels()
    measured, term_bg = _query_terminal(want_aspect)
    if term_bg == f"#{THEME_BG}":
        term_bg = None                           # already the theme's: nothing to paint or put back
    if want_aspect:
        cell_aspect = measured
    # which host to show (SPEC §40): --remote, else local sessions, else the last host used, else a remote
    if remote_name is None and not registry.load(cockpit.paths.registry).live():
        remote_name = _default_remote(cockpit)
    if remote_name:
        r = remote_mod.get(cockpit.paths, remote_name)
        if r is None:
            print(f"force: no host {remote_name!r} (force host list)", file=sys.stderr)
            return 1
        target = name or _remote_target(cockpit, r)
        if target is None:
            print(f"force: cannot ask {r.name} for its sessions (ssh {r.ssh} must work without a password)",
                  file=sys.stderr)
            return 1
        if not target:
            target = _remote_shell(cockpit, r)
            if target is None:
                print(f"force: {r.name} has no sessions and cannot start a shell session", file=sys.stderr)
                return 1
        _remember_host(cockpit, r.name)
        return _attach_ui(cockpit, target, glyphs, no_intro, cell_aspect, r, term_bg)

    if not _start_host(cockpit):
        print("force: tmux server did not start", file=sys.stderr)
        return 2

    reg = registry.load(cockpit.paths.registry)
    if not reg.live():
        target = "shell"
        if create_session(cockpit, target, str(Path.home()), no_switch=True, kind="shell") != 0:
            return 1
    else:
        target = _target_session(cockpit, name)

    _remember_host(cockpit, "")
    if cockpit.split:
        return _attach_ui(cockpit, target, glyphs, no_intro, cell_aspect, term_bg=term_bg)
    _register_client_glyphs(cockpit, glyphs)
    _paint_terminal_bg(term_bg)
    play_intro(cockpit, no_intro, cell_aspect)
    _spawn_welcome(cockpit)
    _exec_tmux(["tmux", "-L", cockpit.paths.tmux_socket, "attach", "-t", "=" + target], term_bg)
    return 0


def _systemd_usable(any_units: bool = False) -> bool:
    """systemd user services usable: `systemctl` on PATH, its user instance answers, and (for the host
    start) the force-tmux.service unit is installed. `any_units` (uninstall): any force*.service/timer
    in ~/.config/systemd/user counts instead of the specific force-tmux.service. Never for a development
    instance (SPEC §2.1): the units belong to the live install."""
    if config.instance() is not None:
        return False
    if shutil.which("systemctl") is None:
        return False
    try:
        if subprocess.call(["systemctl", "--user", "show-environment"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            return False
    except OSError:
        return False
    user_dir = Path.home() / ".config" / "systemd" / "user"
    if any_units:
        return any(user_dir.glob("force*.service")) or any(user_dir.glob("force*.timer"))
    return (user_dir / "force-tmux.service").exists()


def _launcher() -> str:
    """How a `force daemon` was (or would be) started: the launcher (FORCE_BIN for an instance, SPEC §2.1, else
    ~/.local/bin/force) when it is executable, else this process's argv[0]."""
    try:
        p = Path(config.launcher_path())
        if os.access(p, os.X_OK):
            return str(p)
    except OSError:
        pass
    return sys.argv[0]


def _daemon_lock_held(cockpit: Cockpit) -> bool:
    """True when another process holds <state>/daemon.lock (a running daemon). Never blocks."""
    try:
        with open(cockpit.paths.daemon_lock, "a") as f:     # "a": never truncate the daemon's pid
            try:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            return False
    except OSError:
        return False


def _start_daemon(cockpit: Cockpit) -> None:
    """Start `force daemon` detached, its output appended to <state>/daemon.log."""
    try:
        cockpit.paths.state.mkdir(parents=True, exist_ok=True)
        log = open(cockpit.paths.state / "daemon.log", "a")
    except OSError:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen([_launcher(), "daemon"], start_new_session=True,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    except OSError:
        try:
            log.close()
        except OSError:
            pass


def _app_dir(cockpit: Cockpit) -> str:
    """The release this cockpit runs (<app>, the parent of share/tmux.conf): tmux's @force_app."""
    return os.path.abspath(Path(cockpit.paths.tmux_conf).parent.parent)


def _start_host(cockpit: Cockpit) -> bool:
    """This machine's host part (force-tmux + daemon) running; False when its tmux server does not come up.
    With systemd usable it starts the units as today; otherwise it starts the tmux server and the daemon itself
    (SPEC §1)."""
    if _systemd_usable():
        try:
            active = subprocess.call(["systemctl", "--user", "is-active", "--quiet", "force.service"])
        except OSError:
            active = 0
        if active != 0:
            try:
                subprocess.call(["systemctl", "--user", "start", "force-tmux.service", "force.service"])
            except OSError:
                pass
    else:
        if not cockpit.tmux.server_running():
            # @force_app / @force_state at once (tmux.conf's defaults are the live install's paths; the
            # daemon sets them too, but menus can be opened before its first tick)
            subprocess.call(["tmux", "-L", cockpit.paths.tmux_socket, "-f", str(cockpit.paths.tmux_conf),
                             "start-server", ";", "set", "-g", "@force_app", _app_dir(cockpit), ";",
                             "set", "-g", "@force_state", str(cockpit.paths.state)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _daemon_lock_held(cockpit):
            _start_daemon(cockpit)
    deadline = time.time() + 10
    while not cockpit.tmux.server_running():
        if time.time() > deadline:
            return False
        time.sleep(0.1)
    return True


def _attach_ui(cockpit: Cockpit, target: str, glyphs: str, no_intro: bool, cell_aspect: float | None,
               remote=None, term_bg: str | None = None) -> int:
    """Split mode (SPEC §39): a ui session for this terminal on the ui server: the main pane shows `target`
    through a nested client of the host server, the sidebar and header are drawn here."""
    try:
        cols, rows = os.get_terminal_size(0)
    except OSError:
        cols, rows = 160, 45
    try:
        name = build_ui_session(cockpit, target, f"ui-{os.getpid()}", cols, rows, remote, glyphs)
    except TmuxError as e:
        print(f"force: cannot start the ui: {e}", file=sys.stderr)
        return 2
    _register_client_glyphs(cockpit, glyphs, cockpit.ui_tmux)
    _paint_terminal_bg(term_bg)
    play_intro(cockpit, no_intro, cell_aspect)
    _spawn_welcome(cockpit)
    _exec_tmux(["tmux", "-L", cockpit.paths.ui_socket, "attach", "-t", "=" + name], term_bg)
    return 0


def build_ui_session(cockpit: Cockpit, target: str, name: str, cols: int, rows: int, remote=None,
                     glyphs: str | None = None) -> str:
    """Create the ui session `name` (detached): main pane (role agent) running `force nested TARGET`, the
    sidebar (left, full height, unless switched off) and the header (2 rows above the main pane). The
    window options say which host client and session it shows (@force_nested, @force_session) and how to
    reach that host (@force_host: a remote's name or empty, @force_host_tmux). A remote's nested client
    is named by the remote side once it runs (force nested --report). `glyphs` (rich/plain): the session's
    glyph tier, set before the sidebar and header start, so they draw it at once instead of the plain look
    until their next tier read (a host switch knows its terminal's tier already)."""
    ui = cockpit.ui_tmux
    home = str(Path.home())
    host = remote.name if remote else ""
    nested_cmd = [cockpit.cockpit_bin, "nested", target] + (["--remote", host] if remote else [])
    main = ui.run("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, "-x", str(cols), "-y", str(rows),
                  "-c", home, "-e", f"FORCE_HOST={host}", shlex.join(nested_cmd))
    ui.run("set-environment", "-t", "=" + name, "FORCE_HOST", host)
    if glyphs in ("rich", "plain"):
        ui.run("set-option", "-t", "=" + name + ":", "@force_glyphs", glyphs)
    ui.run("set-option", "-p", "-t", main, "@force_role", "agent")
    # the nested client's name on the host: here the main pane's tty, known before the sidebar starts
    nested = "" if remote else ui.run("display", "-p", "-t", main, "#{pane_tty}")
    ui.run("set-option", "-w", "-t", main, "@force_nested", nested)
    ui.run("set-option", "-w", "-t", main, "@force_session", target)
    ui.run("set-option", "-w", "-t", main, "@force_host", host)
    host_tmux = remote_mod.host_tmux_prefix(cockpit.paths, remote) if remote else f"tmux -L {cockpit.paths.tmux_socket}"
    ui.run("set-option", "-w", "-t", main, "@force_host_tmux", host_tmux)
    # the app path as given (normally the ~/.local/share/force/app symlink), not resolved: a resolved release
    # path kept the ui server's bindings on that release's scripts after every upgrade
    # the launcher the menus run ($FORCE_BIN, SPEC §2.1): also on a ui server started by an older release
    ui.run("set-environment", "-g", "FORCE_BIN", config.launcher_path())
    for k, v in (("@force_app", _app_dir(cockpit)),
                 ("@force_state", str(cockpit.paths.state)), ("@force_rich_terms", cockpit.cfg.rich_terminals)):
        ui.set_option(k, v)
    if ui.get_option("@force_sidebar") != "off":
        ui.run("split-window", "-h", "-b", "-f", "-l", str(cockpit.cfg.sidebar_width), "-t", main,
               "-c", home, "-e", f"FORCE_HOST={host}", cockpit.sidebar_command(),
               ";", "set-option", "-p", "@force_role", "sidebar")
    ui.run("split-window", "-v", "-b", "-l", "2", "-t", main, "-c", home, "-e", f"FORCE_HOST={host}",
           cockpit.header_command(), ";", "set-option", "-p", "@force_role", "header")
    ui.select_pane(main)
    return name


def nested_command(cockpit: Cockpit, target: str, remote_name: str | None = None, report: str | None = None) -> int:
    """The ui main pane (split mode, SPEC §39): a nested client of the host server showing `target`. When it
    ends (its session and every other one gone, the host server restarted) it attaches to a live session
    again; it only exits with its pane.
    --remote NAME (SPEC §40): runs `force nested TARGET --report ID` on that host over ssh, and names the
    remote nested client in this ui window (@force_nested) once the remote side reported its tty; a dropped
    connection is retried. --report ID (the remote side): write this tty to <state>/nested/ID first."""
    if remote_name:
        return _nested_remote(cockpit, target, remote_name)
    if report:
        d = cockpit.paths.state / "nested"
        d.mkdir(parents=True, exist_ok=True)
        try:
            (d / report).write_text(os.ttyname(0))
        except OSError:
            pass
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    waiting = False
    while True:
        if cockpit.tmux.has_session(target):
            waiting = False
            subprocess.call(["tmux", "-L", cockpit.paths.tmux_socket, "attach", "-t", "=" + target], env=env)
        live = [e.name for e in registry.load(cockpit.paths.registry).live() if cockpit.tmux.has_session(e.name)]
        if live:
            target = live[0]
            time.sleep(0.2)
            continue
        if not waiting:
            print("\x1b[2J\x1b[Hno session to show: Ctrl+Space n starts one", flush=True)
            waiting = True
        time.sleep(1)


def _nested_remote(cockpit: Cockpit, target: str, remote_name: str) -> int:
    r = remote_mod.get(cockpit.paths, remote_name)
    if r is None:
        print(f"force: no host {remote_name!r}", flush=True)
        time.sleep(5)
        return 1
    pane = os.environ.get("TMUX_PANE", "")
    rid = f"{socket.gethostname()}-{os.getpid()}"
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX_PANE",)}
    while True:
        child = subprocess.Popen(remote_mod.ssh_argv(cockpit.paths, r, remote_mod.REMOTE_FORCE, "nested", target,
                                                     "--report", rid, tty=True), env=env)
        _name_remote_client(cockpit, r, rid, pane, child)
        child.wait()
        print(f"\x1b[2J\x1b[Hconnection to {r.name} ended; reconnecting…", flush=True)
        time.sleep(2)
        try:                        # back to the session this window showed last, not the first one
            target = cockpit.ui_tmux.run("show", "-wqv", "-t", pane, "@force_session") or target
        except TmuxError:
            pass


def _name_remote_client(cockpit: Cockpit, r, rid: str, pane: str, child) -> None:
    """Wait (in the background) until the remote side reported its tty, then name the nested client in this
    ui window and tell its sidebar and header."""
    def run():
        for _ in range(60):
            if child.poll() is not None:
                return
            f = f"$HOME/.local/state/force/nested/{rid}"
            try:
                p = subprocess.run(remote_mod.ssh_argv(cockpit.paths, r, "sh", "-c", f'cat "{f}" && rm -f "{f}"'),
                                   capture_output=True, text=True, timeout=20)
            except subprocess.TimeoutExpired:
                continue
            tty = p.stdout.strip()
            if p.returncode == 0 and tty:
                ui = cockpit.ui_tmux
                try:
                    ui.run("set-option", "-w", "-t", pane, "@force_nested", tty)
                    for line in ui.run("list-panes", "-t", pane, "-F",
                                       "#{?#{m:force-*,#{pane_current_command}},#{@force_role},starting} #{pane_pid}").splitlines():
                        role, _, pid = line.partition(" ")
                        if role in ("sidebar", "header") and pid.isdigit():
                            os.kill(int(pid), signal.SIGUSR1)
                except (TmuxError, OSError):
                    pass
                return
            time.sleep(0.5)
    threading.Thread(target=run, daemon=True).start()


def _default_remote(cockpit: Cockpit) -> str | None:
    """The host to show when there are no local sessions: the last one used, else the first enabled remote."""
    rs = [r for r in remote_mod.load(cockpit.paths) if r.enabled]
    if not rs:
        return None
    try:
        last = (cockpit.paths.state / "ui_host").read_text().strip()
    except OSError:
        last = ""
    return last if any(r.name == last for r in rs) else rs[0].name


def _remember_host(cockpit: Cockpit, name: str) -> None:
    try:
        cockpit.paths.state.mkdir(parents=True, exist_ok=True)
        (cockpit.paths.state / "ui_host").write_text(name)
    except OSError:
        pass


def _remote_target(cockpit: Cockpit, r) -> str | None:
    """A session of the remote to show first: from its watched snapshot, else asked over ssh. Prefers one no
    client is viewing, then the lowest index. "" when the remote has no sessions, None when it cannot be asked."""
    snap = None
    try:
        snap = json.loads((remote_mod.remote_dir(cockpit.paths, r.name) / "snapshot.json").read_text())
    except (OSError, ValueError):
        try:
            p = subprocess.run(remote_mod.ssh_argv(cockpit.paths, r, remote_mod.REMOTE_FORCE, "list", "--json"),
                               capture_output=True, text=True, timeout=30)
            rows = json.loads(p.stdout or "[]")
            snap = {"sessions": rows if isinstance(rows, list) else rows.get("sessions", [])}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
    live = [s for s in snap.get("sessions") or [] if not s.get("archived") and s.get("name")]
    if not live:
        return ""
    live.sort(key=lambda s: (bool(s.get("viewed")), s.get("index") or 999))
    return live[0]["name"]


def _remote_shell(cockpit: Cockpit, r) -> str | None:
    """A remote with no sessions gets a shell session, as this machine does: its name, None when that fails."""
    try:
        p = subprocess.run(remote_mod.ssh_argv(cockpit.paths, r, remote_mod.REMOTE_FORCE, "new", "shell",
                                               "--kind", "shell", "--no-switch"),
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return "shell" if p.returncode == 0 else None


def watch_command(cockpit: Cockpit) -> int:
    """`force watch` (SPEC §40): this host's snapshot and registry as JSON lines, for a remote ui."""
    app = cockpit.paths.home / "app"
    release = os.path.basename(os.path.realpath(app)) if app.exists() else None
    return remote_mod.watch(cockpit.paths, release, cockpit.cfg.ui)


def install_to_command(cockpit: Cockpit, dest: str, yes: bool = True) -> int:
    """`force install-to SSH`: copy the live release to that machine over ssh and install it there
    (install.sh --release-dir: no build, no Rust or git needed there)."""
    app = cockpit.paths.home / "app"
    if not app.exists():
        print("force: no live release here", file=sys.stderr)
        return 1
    rel = Path(os.path.realpath(app))
    if not (rel / "force" / "setup.py").exists():
        print(f"force: release {rel.name} has no setup (deploy again)", file=sys.stderr)
        return 1
    r = remote_mod.Remote("target", dest)
    print(f"force: copying release {rel.name} to {dest}…", flush=True)
    tar = subprocess.Popen(["tar", "-C", str(rel.parent), "-cz", rel.name], stdout=subprocess.PIPE)
    put = subprocess.run(remote_mod.ssh_argv(cockpit.paths, r, "sh", "-c",
                                             "mkdir -p ~/.local/share/force/releases && "
                                             "tar -C ~/.local/share/force/releases -xz"),
                         stdin=tar.stdout)
    # our copy of the pipe closed, so tar ends (SIGPIPE) when ssh did not read it all (host unreachable)
    tar.stdout.close()
    if put.returncode != 0:
        tar.kill()
    tar.wait()
    if put.returncode != 0 or tar.returncode != 0:
        print(f"force: copying to {dest} failed", file=sys.stderr)
        return 1
    print(f"force: installing on {dest}…", flush=True)
    return subprocess.call(remote_mod.ssh_argv(cockpit.paths, r, "sh", "-c", _REMOTE_SETUP, "force-setup", rel.name,
                                               tty=True))


# Runs on the other machine (sh -c, $1 = the release's name): its Python 3.11+ (the system's, else one the one-line
# installer fetched), then that release's setup, as the one-line installer does it.
_REMOTE_SETUP = r"""
rel="$HOME/.local/share/force/releases/$1"
py=""
for c in python3 python3.13 python3.12 python3.11 "$HOME"/.local/share/force/python/*/bin/python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && { py="$p"; break; }
done
if [ -z "$py" ]; then
  echo "force: no Python 3.11+ there: install Force there with the one-line installer instead" >&2
  exit 1
fi
exec "$py" -I -c 'import runpy, sys; sys.path.insert(0, sys.argv[1]); sys.argv = ["force setup"] + sys.argv[2:]; runpy.run_module("force.setup", run_name="__main__")' "$rel" --release "$rel" --python "$py" --no-fonts
"""


def remote_command(cockpit: Cockpit, action: str, name: str | None, dest: str | None, no_install: bool) -> int:
    """`force host add NAME [SSH] | list | remove NAME | upgrade NAME` (SPEC §40; `force remote …` is an alias)."""
    rs = remote_mod.load(cockpit.paths)
    if action == "list":
        if not rs:
            print("no hosts (force host add NAME [SSH])")
        wn = max((len(r.name) for r in rs), default=0)
        ws = max((len(r.ssh) for r in rs), default=0)
        for r in rs:
            st = remote_mod.read_status(cockpit.paths, r.name)
            print(f"{r.name:{wn}}  {r.ssh:{ws}}  {'on ' if r.enabled else 'off'} {st.get('state', '?'):10} "
                  f"{st.get('version') or ''} {st.get('detail') or ''}".rstrip())
        return 0
    if not name or not remote_mod.NAME_RE.match(name):
        print("force: a host needs a name (letters, digits, . _ -)", file=sys.stderr)
        return 1
    existing = next((r for r in rs if r.name == name), None)
    if action == "remove":
        if existing is None:
            print(f"force: no host {name!r}", file=sys.stderr)
            return 1
        remote_mod.save(cockpit.paths, [r for r in rs if r.name != name])
        shutil.rmtree(remote_mod.remote_dir(cockpit.paths, name), ignore_errors=True)
        print(f"removed {name}")
        return 0
    if action == "upgrade":
        if existing is None:
            print(f"force: no host {name!r}", file=sys.stderr)
            return 1
        return install_to_command(cockpit, existing.ssh)
    # add
    dest = dest or name                          # SSH defaults to NAME when omitted
    if existing is not None:
        print(f"force: host {name!r} exists (force host remove {name} first)", file=sys.stderr)
        return 1
    r = remote_mod.Remote(name, dest)
    if subprocess.run(remote_mod.ssh_argv(cockpit.paths, r, "true"), capture_output=True, timeout=30).returncode != 0:
        print(f"force: cannot log in to {dest} with a key (ssh {dest} must work without a password)", file=sys.stderr)
        return 1
    probe = subprocess.run(remote_mod.ssh_argv(cockpit.paths, r, remote_mod.REMOTE_FORCE, "ui"),
                           capture_output=True, text=True, timeout=30)
    if probe.returncode != 0:
        if no_install:
            print(f"force: Force is not installed on {dest} (drop --no-install to install it)", file=sys.stderr)
            return 1
        if install_to_command(cockpit, dest) != 0:
            return 1
        probe = subprocess.run(remote_mod.ssh_argv(cockpit.paths, r, remote_mod.REMOTE_FORCE, "ui"),
                               capture_output=True, text=True, timeout=30)
    if probe.stdout.strip() != "split":
        # a remote is shown through a nested client: its sessions must be bare (split mode)
        subprocess.run(remote_mod.ssh_argv(cockpit.paths, r, remote_mod.REMOTE_FORCE, "ui", "split"),
                       capture_output=True, timeout=30)
        print(f"force: {name} switched to split mode (terminals open there: run force again)")
    remote_mod.save(cockpit.paths, rs + [r])
    if not cockpit.split:
        # this machine draws the remote's sessions in its ui server: its own sessions must be bare too
        _write_ui_mode(cockpit, "split")
        print("force: this machine switched to split mode too (terminals open here: run force again)")
    print(f"added {name}: force --remote {name}")
    return 0


def _attention_target(snapshot: Path) -> str | None:
    """The session a host's needs-you marker leads to: the first that needs you, else the first finished."""
    try:
        sessions = [s for s in json.loads(snapshot.read_text()).get("sessions", [])
                    if not s.get("archived") and s.get("name")]
    except (OSError, ValueError, AttributeError):
        return None
    for state in ("NEEDS_YOU", "YOUR_TURN"):
        hits = sorted((s for s in sessions if s.get("state") == state), key=lambda s: s.get("index") or 999)
        if hits:
            return hits[0]["name"]
    return None


def ui_host_command(cockpit: Cockpit, name: str, window: str, attention: bool = False,
                    target_name: str | None = None) -> int:
    """`force ui-host NAME --window W [--attention]` (host picker, header markers): show another host in this
    ui window ("local" = this machine; with no sessions here it starts a shell session); --attention: on
    the session there that needs you. Problems are shown as a message in the window (it runs from a key
    binding or a click: printed output would cover the main pane)."""
    ui = cockpit.ui_tmux

    def note(msg: str) -> int:
        # on the terminal(s) showing this window: run from a key or a click, there is no current client
        try:
            here = ui.run("display", "-p", "-t", window, "#{session_name}")
            clients = [c.name for c in ui.clients() if c.session == here]
        except TmuxError:
            clients = []
        for c in clients or [None]:
            ui.run("display-message", *(["-c", c] if c else []), "-d", "4000", f" {msg} ")
        return 0

    remote = None
    try:
        if name and name != "local":
            remote = remote_mod.get(cockpit.paths, name)
            if remote is None:
                return note(f"no host {name}")
            st = remote_mod.read_status(cockpit.paths, name)
            if st.get("state") != "connected":
                return note(f"{name}: {st.get('state', 'unknown')}" + (f" ({st['detail']})" if st.get("detail") else ""))
            target = (attention and _attention_target(remote_mod.remote_dir(cockpit.paths, name) / "snapshot.json")) \
                or _remote_target(cockpit, remote)
            if target is None:
                return note(f"{name}: cannot ask it for its sessions")
            if not target:
                target = _remote_shell(cockpit, remote)
                if target is None:
                    return note(f"{name}: cannot start a shell session there")
        elif not cockpit.split:
            return note("this machine uses the classic layout: force ui split")
        elif target_name:           # a given session here (Chuck's, SPEC §50)
            target = target_name
        elif registry.load(cockpit.paths.registry).live():
            target = (attention and _attention_target(cockpit.paths.snapshot)) or _target_session(cockpit, None)
        else:
            if not _start_host(cockpit):
                return note("this machine: tmux server did not start")
            target = "shell"
            if create_session(cockpit, target, str(Path.home()), no_switch=True, kind="shell") != 0:
                return note("this machine: cannot start a shell session")
        sess = ui.run("display", "-p", "-t", window, "#{session_name}")
        cols = int(ui.run("display", "-p", "-t", window, "#{window_width}"))
        rows = int(ui.run("display", "-p", "-t", window, "#{window_height}"))
        tier = ui.run("show", "-qv", "-t", "=" + sess + ":", "@force_glyphs")
        tmp = sess + "-h"
        build_ui_session(cockpit, target, tmp, cols, rows, remote, tier)
    except (TmuxError, RegistryError, ValueError, OSError) as e:
        return note(f"cannot switch host: {e}")
    _remember_host(cockpit, remote.name if remote else "")
    # the terminal(s) of the old ui session move over, then it goes
    for c in ui.clients():
        if c.session == sess:
            ui.run("switch-client", "-c", c.name, "-t", "=" + tmp)
    try:
        ui.run("kill-session", "-t", "=" + sess)
    except TmuxError:
        pass                        # already gone: destroy-unattached ended it when its terminal moved
    try:
        ui.run("rename-session", "-t", "=" + tmp, sess)
        ui.run("set-option", "-t", "=" + sess + ":", "destroy-unattached", "on")
    except TmuxError as e:
        return note(f"cannot switch host: {e}")
    return 0


def _attention_note(snapshot: Path) -> str:
    """` · 2 need you, 1 finished` for a host's snapshot (empty when nothing waits)."""
    try:
        sessions = [s for s in json.loads(snapshot.read_text()).get("sessions", []) if not s.get("archived")]
    except (OSError, ValueError, AttributeError):
        return ""
    needs = sum(1 for s in sessions if s.get("state") == "NEEDS_YOU")
    turn = sum(1 for s in sessions if s.get("state") == "YOUR_TURN")
    parts = ([f"{needs} need you"] if needs else []) + ([f"{turn} finished"] if turn else [])
    return f" · {', '.join(parts)}" if parts else ""


def _has_sessions(snapshot: Path) -> bool:
    """Whether a host's snapshot lists a live session (True when it cannot be read: nothing to claim)."""
    try:
        return any(not s.get("archived") and s.get("name") for s in json.loads(snapshot.read_text()).get("sessions", []))
    except (OSError, ValueError, AttributeError):
        return True


def hosts_menu_command(cockpit: Cockpit, client: str, window: str) -> int:
    """The host picker (prefix H): a menu of this machine and the enabled remotes."""
    items = []
    keys = iter("123456789")
    current = cockpit.ui_tmux.run("display", "-p", "-t", window, "#{@force_host}")
    hosts = [("this machine", "local")] + [(r.name, r.name) for r in remote_mod.load(cockpit.paths) if r.enabled]
    for label, host in hosts:
        mark = "● " if (host == "local" and not current) or host == current else "  "
        if host == "local":
            st = _attention_note(cockpit.paths.snapshot) if registry.load(cockpit.paths.registry).live() else " (new shell)"
        else:
            state = remote_mod.read_status(cockpit.paths, host).get("state", "?")
            snap = remote_mod.remote_dir(cockpit.paths, host) / "snapshot.json"
            st = f" ({state})" if state != "connected" else \
                _attention_note(snap) if _has_sessions(snap) else " (new shell)"
        items += [f"{mark}{label}{st}", next(keys, ""),
                  f"run-shell -b {shlex.quote(shlex.join([cockpit.cockpit_bin, 'ui-host', host, '--window', window]) + ' >/dev/null 2>&1')}"]
    if len(hosts) == 1:
        items += ["", "-add a host: force host add NAME [SSH]", "", ""]
    args = ["display-menu", "-O", "-c", client, "-x", "C", "-y", "C", "-T", "#[align=centre] hosts ", *items]
    out = os.environ.get("FORCE_MENU_OUT")
    if out:
        # opened by a click (share/menu.sh): the binding runs this file with source-file, so the menu has the click
        tmp = out + ".tmp"
        Path(tmp).write_text(" ".join([args[0]] + [_tmux_quote(a) for a in args[1:]]) + "\n")
        os.replace(tmp, out)
        return 0
    cockpit.ui_tmux.run(*args)
    return 0


def _tmux_quote(word: str) -> str:
    """A word for a tmux command file: double quotes, with \\ " $ escaped."""
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def ui_command(cockpit: Cockpit, mode: str | None) -> int:
    """`force ui [classic|split]`: show or set the layout mode (config `ui`, SPEC §39). The daemon converts
    the host sessions' layout; terminals attached in the other mode have to run `force` again."""
    if mode is None:
        print(cockpit.cfg.ui)
        return 0
    if mode == "classic" and remote_mod.load(cockpit.paths):
        print("force: hosts are shown in split mode only (force host remove them first)", file=sys.stderr)
        return 1
    _write_ui_mode(cockpit, mode)
    if mode == "classic" and cockpit.ui_tmux.server_running():
        cockpit.ui_tmux.kill_server()      # its nested views would show the host's own sidebars again
    print(f"layout: {mode}. Quit (Ctrl+Space q) and run force again in every open terminal.")
    return 0


def _write_ui_mode(cockpit: Cockpit, mode: str) -> None:
    config.save_setting(cockpit.paths.config_file, "layout", mode)


def settings_command(cockpit: Cockpit, action: str | None, key: str | None, value: str | None,
                   all_: bool, as_json: bool) -> int:
    """`force settings [path|set KEY VALUE|unset KEY|edit] [--all] [--json]`."""
    path = cockpit.paths.config_file
    if action == "path":
        print(path)
        return 0
    if action == "set":
        try:
            typed = config.parse_value(key, value)
            config.save_setting(path, key, typed)
        except ValueError as e:
            print(f"force: {e}", file=sys.stderr)
            return 1
        print(f"{key} = {typed}")
        print("(applies within a few seconds: the daemon reloads)")
        return 0
    if action == "unset":
        try:
            config.unset_setting(path, key)
        except ValueError as e:
            print(f"force: {e}", file=sys.stderr)
            return 1
        print(f"unset {key}")
        print("(applies within a few seconds: the daemon reloads)")
        return 0
    if action == "edit":
        return _edit_settings(path)
    # `force settings` alone: print the effective settings (warnings for a broken file come from load_config)
    cfg = config.load_config(path)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raw = ""
    try:
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    if as_json:
        out = {s.key: config._to_json(s, getattr(cfg, s.field)) for s in config.SETTINGS}
        print(json.dumps(out, indent=2))
        return 0
    rows = []
    for spec in config.SETTINGS:
        if not all_ and not spec.public:
            continue
        effective = config._to_json(spec, getattr(cfg, spec.field))
        shown = "true" if effective is True else "false" if effective is False else str(effective)
        rows.append((spec.key, shown, "" if spec.key in data else "(default)"))
    wk = max(len(r[0]) for r in rows)
    wv = max(len(r[1]) for r in rows)
    for key, shown, mark in rows:
        print(f"{key:{wk}}  {shown:{wv}}  {mark}".rstrip())
    print(f"\n{cockpit.paths.config_file}  ·  force settings set KEY VALUE")
    return 0


def _edit_settings(path: Path) -> int:
    """Create the settings file if missing, open it in $VISUAL/$EDITOR/nano/vi, then validate it."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = next((c for c in ("nano", "vi") if shutil.which(c)), "vi")
    subprocess.call(shlex.split(editor) + [str(path)])
    config.load_config(path)            # prints its warnings for a broken file
    print("settings ok")
    print("(applies within a few seconds: the daemon reloads)")
    return 0


def _client_glyphs(glyphs: str) -> str:
    """Resolve the client's glyph tier for `auto`: rich when this is an interactive (non-ssh) login whose font set
    (fc-list) has the Nerd Font symbols, so the icons work without any per-terminal profile. Explicit `rich`/`plain`
    stay as asked; over ssh, or when the font cannot be detected (or is not installed), it stays `auto` (tier.sh
    then guesses by terminal type)."""
    if glyphs in ("rich", "plain"):
        return glyphs
    if glyphs != "auto":
        return "auto"
    if any(os.environ.get(k) for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return "auto"
    if config.is_wsl():
        return "auto"           # the terminal is a Windows program: Linux's fonts are not what it draws with
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "auto"
    for fam in (p.strip() for p in re.split(r"[\n,]", out)):
        if fam.startswith("Symbols Nerd Font"):
            return "rich"
    return "auto"


def _register_client_glyphs(cockpit: Cockpit, glyphs: str, tmux=None) -> None:
    """This terminal's glyph tier: there is no client kit any more — the tier is stated by `force` itself when the
    symbols font is installed (or by `--glyphs`) instead of guessed from its terminal type (SPEC §28). Kept in
    the global option @force_client_glyphs as "<tty>=<tier>" words, read by tier.sh and presize.sh; the tmux
    client that attaches next runs on this same tty. `auto` drops this tty's entry; entries of ttys with no
    client any more are dropped too."""
    try:
        tty = os.ttyname(0)            # the tmux client exec'd next inherits this tty
    except OSError:
        return
    tmux = tmux or cockpit.tmux           # the server the terminal attaches to (the ui server in split mode)
    try:
        live = {c.name for c in tmux.clients()}
        words = (tmux.get_option("@force_client_glyphs") or "").split()
        keep = [w for w in words if "=" in w and w.split("=", 1)[0] in live and w.split("=", 1)[0] != tty]
        if glyphs in ("rich", "plain"):
            keep.append(f"{tty}={glyphs}")
        tmux.set_option("@force_client_glyphs", " ".join(keep))
    except TmuxError:
        pass


def _target_session(cockpit: Cockpit, name: str | None) -> str:
    if name:
        return name
    seen = state.load_seen(cockpit.paths.seen)
    live = registry.load(cockpit.paths.registry).live()
    if not live:
        raise RegistryError("no live sessions")

    def last(e):
        return seen.get(e.name, state.Seen()).last_seen_ms

    # Another terminal may already be in cockpit: open where nobody is looking, so concurrent
    # clients show different sessions (falls back to all sessions when every one is on screen).
    try:
        viewed = {c.session for c in cockpit.tmux.clients()}
    except Exception:
        viewed = set()
    pool = [e for e in live if e.name not in viewed] or live
    best = max(pool, key=last)
    if last(best) > 0:
        return best.name
    return min(pool, key=lambda e: e.order).name


def new_command(cockpit: Cockpit, name: str | None, cwd: str | None, no_switch: bool,
              shell: bool = False, kind: str | None = None) -> int:
    return create_session(cockpit, name, cwd, no_switch, shell=shell, kind=kind)


def opencode_update_command(cockpit: Cockpit, version: str | None, check: bool) -> int:
    """`force opencode-update [--version V] [--check]`: install the latest OpenCode release (or V) into
    Force's own copy (config opencode_managed, SPEC §36). The daemon runs it every few hours."""
    from . import opencode_update as ou
    if not cockpit.cfg.opencode_managed:
        print("force: opencodeManaged is off: OpenCode comes from PATH"
              + (f" ({opencode.installed()})" if opencode.installed() else "")
              + "; force settings set opencodeManaged true to keep Force's own copy", file=sys.stderr)
        return 1
    root = cockpit.paths.home / "opencode"
    try:
        if check:
            newer = ou.newer_available(root)
            print(f"current {ou.current_version(root) or 'none'}; "
                  + (f"{newer} available" if newer else "up to date"))
            return 0
        ver, changed = ou.install(root, version)
    except ou.UpdateError as e:
        print(f"force: opencode-update: {e}", file=sys.stderr)
        return 1
    print(f"opencode {ver} " + ("installed (current)" if changed else "is current"))
    return 0


def notify_test_command(cockpit: Cockpit, event: str, session: str) -> int:
    """`force notify-test [EVENT]`: run the notifyCommand setting once, as the daemon would (SPEC §49.4), and
    show what it printed and how it ended."""
    from .daemon import notify_env
    from .state import State
    cmd = (cockpit.cfg.notify_command or "").strip()
    if not cmd:
        print("force: notifyCommand is not set. Example: force settings set notifyCommand "
              "'notify-send Force \"$FORCE_MESSAGE\"'", file=sys.stderr)
        return 1
    state = {"needs-you": State.NEEDS_YOU, "finished": State.YOUR_TURN, "down": State.DOWN}[event]
    view = types.SimpleNamespace(name=session, kind="claude", state=state, viewed=False,
                                 activity="needs permission" if event == "needs-you" else None)
    env = notify_env(view, event)
    print(f"running: {cmd}\n  " + "\n  ".join(f"{k}={v}" for k, v in env.items()))
    try:
        p = subprocess.run(["sh", "-c", cmd], env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("force: the command did not finish within 30 s (the daemon kills it then)", file=sys.stderr)
        return 1
    for label, out in (("stdout", p.stdout), ("stderr", p.stderr)):
        if out.strip():
            print(f"{label}:\n" + out.rstrip())
    print(f"exit status {p.returncode}")
    return 0 if p.returncode == 0 else 1


def skill_command(cockpit: Cockpit | None) -> int:
    """`force skill` / `force --skill`: the guide for agents that drive Force (SPEC §49.5)."""
    paths = cockpit.paths if cockpit is not None else config.get_paths()
    for f in (Path(paths.tmux_conf).parent / "skill.md", Path(__file__).resolve().parent.parent / "share" / "skill.md"):
        try:
            sys.stdout.write(f.read_text())
            return 0
        except OSError:
            continue
    print("force: the agent guide (share/skill.md) is missing from this install", file=sys.stderr)
    return 1


def kinds_command(cockpit: Cockpit, as_json: bool) -> int:
    """`force kinds`: the session kinds this server can run (the new-session dialog asks)."""
    kinds = available_kinds(cockpit)
    print(json.dumps(kinds) if as_json else "\n".join(kinds))
    return 0


def dirs_command(dir_arg: str, prefix: str) -> int:
    """`force dirs DIR PREFIX` (internal): folder names in DIR whose names start with PREFIX, for the dialogs'
    Tab completion. An empty DIR (or a relative one) is the home directory. Never fails."""
    try:
        if not dir_arg:
            base = Path.home()
        else:
            exp = os.path.expanduser(dir_arg)
            base = Path(exp) if os.path.isabs(exp) else Path.home() / exp
        names = sorted(e.name for e in base.iterdir()
                      if e.is_dir() and e.name.startswith(prefix))
        print("\n".join(names[:500]))
    except OSError:
        pass
    return 0


def rename_command(cockpit: Cockpit, old: str, new: str) -> int:
    if not registry.valid_name(new):
        print(f"force: invalid name {new!r}", file=sys.stderr)
        return 1
    print(cockpit.rename(old, new))
    return 0


def sync_titles_command(cockpit: Cockpit, names: list[str]) -> int:
    """`force sync-titles [NAME...]`: make the Remote Control title (the phone) match each Claude session's Force
    name again: the daemon types `/rename NAME` into each once it is idle, connected and its input is empty."""
    from .lifecycle import queue_title_sync
    queued = []
    with registry.locked(cockpit.paths.home) as reg:
        for e in reg.entries:
            if e.archived or not registry.is_claude(e) or (names and e.name not in names):
                continue
            queue_title_sync(e, e.name)
            queued.append(e.name)
    missing = [n for n in names if n not in queued]
    for n in missing:
        print(f"force: no live Claude session {n!r}", file=sys.stderr)
    if queued:
        print(f"queued {len(queued)}: each gets its name on the phone as soon as it is idle "
              f"({', '.join(queued)})")
    return 1 if missing else 0


def archive_command(cockpit: Cockpit, name: str, force: bool) -> int:
    if not force:
        cur = _current_state_for(cockpit, name)
        if cur in (State.WORKING, State.NEEDS_YOU):
            print(f"force: {name} is {cur.value}; use --force to archive anyway", file=sys.stderr)
            return 1
    cockpit.archive(name, force=force)
    return 0


def delete_command(cockpit: Cockpit, name: str, force: bool, yes: bool) -> int:
    """`force delete NAME [--force] [--yes]`: remove a session for good (asks first on a terminal)."""
    entry = registry.load(cockpit.paths.registry).get(name)
    if entry is None:
        print(f"force: unknown session {name!r}", file=sys.stderr)
        return 1
    if not force and not entry.archived:
        cur = _current_state_for(cockpit, name)
        if cur in (State.WORKING, State.NEEDS_YOU):
            print(f"force: {name} is {cur.value}; use --force to delete anyway", file=sys.stderr)
            return 1
    if not yes:
        if not sys.stdin.isatty():
            print("force: delete needs --yes when not run from a terminal", file=sys.stderr)
            return 1
        answer = input(f"delete {name} for good? It cannot be restored. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("not deleted")
            return 1
    cockpit.delete(name)
    print(f"deleted {name}")
    return 0


def unarchive_command(cockpit: Cockpit, name: str) -> int:
    cockpit.unarchive(name)
    return 0


def restart_command(cockpit: Cockpit, name: str | None, all_: bool = False) -> int:
    """One session: restart now. --all: restart every idle session now; sessions that are working or
    waiting on you get `pending_restart` and the daemon restarts them as soon as they are idle (so no
    turn is cut off). Restarting resumes the same conversation and picks up new settings."""
    if not all_:
        if not name:
            print("force: restart needs NAME or --all", file=sys.stderr)
            return 1
        cockpit.restart(name)
        return 0
    now = []
    for e in registry.load(cockpit.paths.registry).live():
        n = e.name
        if registry.is_shell(e):        # a shell has no settings to pick up; restarting it kills its work
            print(f"{n}: shell, not restarted")
            continue
        cur = _current_state_for(cockpit, n)
        if cur in (State.WORKING, State.NEEDS_YOU):
            with registry.locked(cockpit.paths.home) as reg:
                r = reg.get(n)
                if r is not None:
                    r.extra["pending_restart"] = True
            print(f"{n}: busy, restarts when idle")
        else:
            now.append(n)
    failed = cockpit.restart_many(now)      # all at once: one slow session must not hold up the rest
    for n in now:
        if n in failed:
            print(f"{n}: restart failed", file=sys.stderr)
        else:
            print(f"{n}: restarted")
    return 0


def _upgrade_reason(cockpit: Cockpit, name: str) -> str | None:
    """None if the session should be upgraded, else a reason string."""
    entry = registry.load(cockpit.paths.registry).get(name)
    if entry is not None and registry.is_shell(entry):
        return "a shell session"
    pane = cockpit.agent_pane(name)
    if entry is not None and registry.is_opencode(entry):
        # OpenCode: restart onto the installed binary when it is newer than the running TUI (SPEC §36)
        if pane is None or pane.pid is None:
            return "not running"
        st, meta = opencode.read_status(entry.extra.get("oc_port"), pane.pid, entry.extra.get("oc_session"))
        latest = opencode.installed_version()
        if st is None or meta is None or meta.version is None or latest is None:
            return "version unknown"
        if meta.version == latest:
            return "not outdated"
        if st.status == "busy":
            return "busy"
        if st.status == "waiting":
            return "needs you"
        return None
    if pane is None or pane.pid is None:
        return "not running"
    ver = claude.process_version(pane.pid)
    latest = claude.latest_version(cockpit.paths.claude_bin)
    if ver is None or latest is None:
        return "version unknown"
    if ver == latest:
        return "not outdated"
    st = claude.read_status(cockpit.paths.claude_dir, pane.pid)
    if st is not None:
        if st.status == "busy":
            return "busy"
        if st.status == "waiting":
            return "needs you"
    return None


def upgrade_command(cockpit: Cockpit, name: str | None, all_: bool) -> int:
    if all_:
        names = [e.name for e in registry.load(cockpit.paths.registry).live()]
    elif name:
        names = [name]
    else:
        print("force: upgrade needs NAME or --all", file=sys.stderr)
        return 1
    for n in names:
        reason = _upgrade_reason(cockpit, n)
        if reason is None:
            cockpit.restart(n)
            print(f"upgraded {n}")
        else:
            print(f"skipped {n}: {reason}")
    return 0


def list_command(cockpit: Cockpit, all_: bool, json_: bool) -> int:
    snap = _read_fresh_snapshot(cockpit)
    if snap is None:
        snap = _compute_live_snapshot(cockpit)
    now = _now_ms()
    if json_:
        print(json.dumps(snap.to_json(), indent=2))
        return 0

    # Chuck (SPEC §50) is not a list entry (--json still has him, marked "support")
    rows = [s for s in snap.sessions if (all_ or not s.archived) and not s.support]
    rows.sort(key=lambda s: (s.archived, s.index if s.index is not None else 0))

    width = max([len(s.name) for s in rows] + [4])
    print(f"{'#':>2} {'':1} {'NAME':<{width}} {'STATE':<9} {'AGE':>6} {'UP':>2}")
    for s in rows:
        g = state.glyph(s.state, now)
        idx = "" if s.index is None else str(s.index)
        age = state.format_age(s.since_ms, now)
        up = "↑" if s.outdated else ""
        print(f"{idx:>2} {g:1} {s.name:<{width}} {s.state.value:<9} {age:>6} {up:>2}")
    return 0


def _current_session(cockpit: Cockpit, client: str | None) -> str | None:
    """Current session of the calling client (for next/prev resolution)."""
    if client:
        for c in cockpit.tmux.clients():
            if c.name == client:
                return c.session
    tpane = os.environ.get("TMUX_PANE")
    if tpane:
        try:
            return cockpit.tmux.run("display", "-p", "-t", tpane, "#{session_name}")
        except TmuxError:
            pass
    try:
        for c in cockpit.tmux.clients():
            return c.session
    except TmuxError:
        pass
    return None


def switch_command(cockpit: Cockpit, name: str | None, index: int | None,
                  next_: bool, prev_: bool, client: str | None) -> int:
    # the session list (Alt+N, next, previous): without Chuck (SPEC §50), who opens from his own button
    live = [e.name for e in registry.load(cockpit.paths.registry).live() if not registry.is_support(e)]
    if name:
        target = name
    elif index is not None:
        if not (1 <= index <= len(live)):
            print(f"force: no session at index {index}", file=sys.stderr)
            return 1
        target = live[index - 1]
    elif next_ or prev_:
        if not live:
            print("force: no live sessions", file=sys.stderr)
            return 1
        cur = _current_session(cockpit, client)
        if cur in live:
            i = live.index(cur)
        else:                   # from outside the list (Chuck): next is the first, previous the last
            i = -1 if next_ else 0
        step = 1 if next_ else -1
        target = live[(i + step) % len(live)]
    else:
        print("force: switch needs NAME, --index, --next or --prev", file=sys.stderr)
        return 1
    cockpit.tmux.switch_client(target, client)
    return 0


def forget_command(cockpit: Cockpit, name: str) -> int:
    """Delete an archived session (older name for `delete` on the archived list)."""
    entry = registry.load(cockpit.paths.registry).get(name)
    if entry is None:
        print(f"force: unknown session {name!r}", file=sys.stderr)
        return 1
    if not entry.archived:
        print(f"force: only archived sessions can be forgotten: {name!r}", file=sys.stderr)
        return 1
    cockpit.delete(name)
    return 0


def doctor_command(cockpit: Cockpit, fix: bool) -> int:
    from .doctor import run_doctor
    # the systemd units are only checked where Force uses them (not without systemd, not for an instance)
    return run_doctor(cockpit, fix=fix, systemd=_systemd_usable())


def import_command(cockpit: Cockpit, dry_run: bool, pid: int | None, name: str | None,
                  session: str | None = None, all_: bool = False, screen: bool = False) -> int:
    from .importer import import_session, run_import, run_list
    if screen:
        failures = run_import(cockpit, dry_run=dry_run, pid=pid, name=name)
        return 1 if failures else 0
    if session is None and pid is None:
        return run_list(cockpit, all_=all_)
    sel = str(pid) if pid is not None else session
    return import_session(cockpit, sel, explicit_name=name, dry_run=dry_run)


def move_folder_command(cockpit: Cockpit, name: str, folder: str) -> int:
    """`force move NAME DIR` (SPEC §7.7): the session continues in DIR (its conversation and memory kept,
    DIR's CLAUDE.md / AGENTS.md read from now on). A busy session moves once its turn ends."""
    target = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(target):
        print(f"force: no such folder {folder!r}", file=sys.stderr)
        return 1
    entry = registry.load(cockpit.paths.registry).get(name)
    if entry is None or entry.archived:
        print(f"force: {'archived' if entry else 'unknown'} session {name!r}", file=sys.stderr)
        return 1
    if os.path.abspath(entry.cwd) == target:
        print(f"{name}: already in {target}")
        return 0
    cur = _current_state_for(cockpit, name)
    if cur in (State.WORKING, State.NEEDS_YOU):
        with registry.locked(cockpit.paths.home) as reg:
            r = reg.get(name)
            if r is not None:
                r.extra["pending_move"] = target
        print(f"{name}: busy, moves to {target} when idle")
        return 0
    try:
        cockpit.move(name, target)
    except (RegistryError, MoveError) as e:
        print(f"force: {e}", file=sys.stderr)
        return 1
    print(f"{name}: moved to {target}")
    return 0


def move_command(cockpit: Cockpit, name: str, direction: str) -> int:
    """Swap NAME's registry `order` with its neighbour among live sessions in `direction` (up|down).
    At the top/bottom: no change, exit 0. Unknown/archived NAME: error exit 1."""
    with registry.locked(cockpit.paths.home) as reg:
        entry = reg.get(name)
        if entry is None:
            print(f"force: unknown session {name!r}", file=sys.stderr)
            return 1
        if entry.archived:
            print(f"force: session {name!r} is archived", file=sys.stderr)
            return 1
        live = reg.live()
        try:
            i = next(k for k, e in enumerate(live) if e.name == name)
        except StopIteration:
            return 0
        if direction == "up":
            j = i - 1
        else:
            j = i + 1
        if not (0 <= j < len(live)):
            return 0                      # at the top/bottom: nothing to swap
        live[i].order, live[j].order = live[j].order, live[i].order
    return 0


def sidebar_toggle_command(cockpit: Cockpit) -> int:
    cockpit.sidebar_toggle()
    return 0


def share_command(cockpit: Cockpit, name: str, user: str, write_: bool) -> int:
    """`force share NAME --user USER [--write]`: share a LIVE registry session (shells allowed) with an
    existing account other than yourself. Stores mode "w" if --write else "r"; re-sharing changes the mode."""
    mode = "w" if write_ else "r"
    if not cockpit.cfg.sharing:
        print("force: sharing is disabled (force settings set sharing true enables it)", file=sys.stderr)
        return 1
    with registry.locked(cockpit.paths.home) as reg:
        entry = reg.get(name)
        if entry is None:
            print(f"force: unknown session {name!r}", file=sys.stderr)
            return 1
        if entry.archived:
            print(f"force: session {name!r} is archived", file=sys.stderr)
            return 1
        try:
            share.guest_uid(user)
        except KeyError:
            print(f"force: no such user: {user!r}", file=sys.stderr)
            return 1
        if user == getpass.getuser():
            print("force: you already have access", file=sys.stderr)
            return 1
        entry.extra.setdefault("shares", {})[user] = mode
    owner = getpass.getuser()
    label = "read/write" if mode == "w" else "read-only"
    print(f"shared {name} with {user} ({label})")
    print(f"python3 /tmp/force-share-{owner}/force-join {owner}/{name}")
    if mode == "w":
        print(f"{user} can type into this session. The agent acts with YOUR account: "
              "your files, keys, git and your other agents.")
    return 0


def unshare_command(cockpit: Cockpit, name: str, user: str | None) -> int:
    """`force unshare NAME [--user USER]`: remove USER's share of NAME, or all shares of NAME when no user."""
    with registry.locked(cockpit.paths.home) as reg:
        entry = reg.get(name)
        if entry is None:
            print(f"force: unknown session {name!r}", file=sys.stderr)
            return 1
        shares = entry.extra.get("shares") or {}
        if user is not None:
            if user not in shares:
                print(f"force: {name} is not shared with {user!r}", file=sys.stderr)
                return 1
            del shares[user]
            if not shares:
                entry.extra.pop("shares", None)
        else:
            if not shares:
                print(f"force: {name} is not shared", file=sys.stderr)
                return 1
            entry.extra.pop("shares", None)
    return 0


def shares_command(cockpit: Cockpit) -> int:
    """`force shares`: table of every share (session, user, mode, proxy running yes/no from the pidfile)."""
    from .daemon import _pid_is_live_proxy, _share_key
    shares_dir = cockpit.paths.state / "shares"
    proxy: set[tuple[str, str]] = set()
    try:
        shares_dir.mkdir(parents=True, exist_ok=True)
        for f in shares_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            pid = data.get("pid")
            key = _share_key(f.stem)
            if isinstance(pid, int) and key is not None and _pid_is_live_proxy(pid):
                proxy.add(key)
    except OSError:
        pass
    rows = []
    with registry.locked(cockpit.paths.home) as reg:
        for entry in reg.entries:
            shares = entry.extra.get("shares") or {}
            for user, mode in shares.items():
                label = "read/write" if mode == "w" else "read-only"
                rows.append((entry.name, user, label, "yes" if (entry.name, user) in proxy else "no"))
    rows.sort(key=lambda r: (r[0], r[1]))
    ws = max([len(r[0]) for r in rows] + [len("session")])
    wu = max([len(r[1]) for r in rows] + [len("user")])
    print(f"{'session':<{ws}}  {'user':<{wu}}  {'mode':<10}  proxy")
    for name, user, label, running in rows:
        print(f"{name:<{ws}}  {user:<{wu}}  {label:<10}  {running}")
    return 0


def share_proxy_command(cockpit: Cockpit, name: str, user: str, mode: str) -> int:
    """`force share-proxy NAME USER MODE` (internal): serve guests for one (session, user) share until killed."""
    return share.run_proxy(cockpit.tmux, name, user, mode, cockpit.paths.home, cockpit.paths.claude_dir)


def fonts_command(args: list[str]) -> int:
    """`force fonts [--cleanup-only | --uninstall]` (SPEC §45): run share/fonts.sh (installs the Nerd Font
    symbols and removes the old terminal kit), passing the arguments straight through."""
    os.execvp("bash", ["bash", str(SHARE / "fonts.sh"), *args])
    return 0  # unreachable: execvp replaces the process


def terminals_alias_command(args: list[str]) -> int:
    """Deprecated `force terminals`: profiles and launchers are gone (Force themes itself now); run the fonts
    cleanup instead. `--uninstall` / `--no-fonts` map onto fonts.sh's cleanup modes."""
    print("force: terminal profiles are gone, Force themes itself now; running `force fonts`", file=sys.stderr)
    if "--uninstall" in args:
        fonts_args = ["--uninstall"]
    elif "--no-fonts" in args:
        fonts_args = ["--cleanup-only"]
    else:
        fonts_args = []
    os.execvp("bash", ["bash", str(SHARE / "fonts.sh"), *fonts_args])
    return 0  # unreachable: execvp replaces the process


def daemon_command(cockpit: Cockpit) -> int:
    from .daemon import main as daemon_main
    return daemon_main(cockpit)


# --------------------------------------------------------------------------- #
# force uninstall
# --------------------------------------------------------------------------- #

def _strip_force_blocks(content: str) -> str:
    """Remove every marked ~/.local/bin PATH block — the new `# force: … # force end` block and the old
    single-line `_OLD_BASHRC_MARK` one — leaving every other line intact (reuses force.setup's markers)."""
    from .setup import _OLD_BASHRC_MARK
    lines = content.splitlines(keepends=True)
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\n")
        if line == _OLD_BASHRC_MARK:
            i += 2                    # the marker and the case line right after it
            continue
        if line.startswith("# force:"):
            i += 1                   # the new block's header
            while i < n and lines[i].strip() != "# force end":
                i += 1
            if i < n:
                i += 1               # the "# force end" line
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def _remove_path_blocks() -> None:
    """Remove Force's ~/.local/bin PATH blocks (only the marked ones) from .bashrc, .zshenv and the fish
    conf.d file, leaving every other line and file untouched."""
    bashrc = Path.home() / ".bashrc"
    if bashrc.exists():
        old = bashrc.read_text()
        new = _strip_force_blocks(old)
        if new != old:
            bashrc.write_text(new)
    zshenv = Path.home() / ".zshenv"
    if zshenv.exists():
        old = zshenv.read_text()
        new = _strip_force_blocks(old)
        if new != old:
            zshenv.write_text(new)
    fish = Path.home() / ".config" / "fish" / "conf.d" / "force.fish"
    try:
        if fish.exists():
            fish.unlink()
    except OSError:
        pass


def _stop_daemon(cockpit: Cockpit) -> None:
    """Stop the no-systemd daemon (under systemd the unit was disabled already): the pid in daemon.lock if
    there is one, else every `force daemon` of this user whose cmdline is exactly launcher + "daemon".
    Never kills by pattern."""
    pids: list[int] = []
    try:
        pid = cockpit.paths.daemon_lock.read_text().strip()
        if pid.isdigit():
            pids.append(int(pid))
    except OSError:
        pass
    if not pids:
        launcher = _launcher()
        try:
            for entry in os.scandir("/proc"):
                if not entry.name.isdigit():
                    continue
                try:
                    with open(entry.path + "/cmdline", "rb") as f:
                        cmd = [c.decode("utf-8", "replace") for c in f.read().split(b"\0") if c]
                    if os.stat(entry.path).st_uid != os.getuid():
                        continue
                except OSError:
                    continue
                if cmd == [launcher, "daemon"]:
                    pids.append(int(entry.name))
        except OSError:
            pass
    for pid in set(pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _uninstall_data(cockpit: Cockpit, purge: bool) -> None:
    """Remove the app's release pieces (and, with --purge, the whole data/state/config dirs)."""
    if purge:
        shutil.rmtree(cockpit.paths.home, ignore_errors=True)
        shutil.rmtree(cockpit.paths.state, ignore_errors=True)
        shutil.rmtree(cockpit.paths.config_file.parent, ignore_errors=True)
        return
    for name in ("app", "releases", "python", "opencode"):
        p = cockpit.paths.home / name
        if p.is_symlink() or p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def uninstall_command(cockpit: Cockpit, purge: bool, yes: bool) -> int:
    """`force uninstall [--purge] [--yes]`: stop Force, remove the launcher and PATH blocks and the
    app's files; without --purge keep the registry, snapshots, archive, config and state."""
    data = cockpit.paths.home
    print("This removes: the force launcher (~/.local/bin/force), the ~/.local/bin PATH blocks, and "
          f"the app in {data}/app (releases, python, opencode).")
    if purge:
        print("This also removes (--purge): your registry, snapshots, archive, config and state.")
    else:
        print("This keeps: your registry and snapshots, archive, ~/.config/force and ~/.local/state/force.")
    if not yes:
        if not sys.stdin.isatty():
            print("force: run force uninstall --yes to confirm", file=sys.stderr)
            return 1
        answer = input("Remove Force? Running sessions stop (their conversations stay in Claude Code). [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("not removed")
            return 1

    if purge:
        import force
        fonts = Path(force.__file__).resolve().parent.parent / "share" / "fonts.sh"
        if fonts.exists():
            subprocess.call(["bash", str(fonts), "--uninstall"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if _systemd_usable(any_units=True):
        subprocess.call(["systemctl", "--user", "disable", "--now",
                       "force.service", "force-smoke.timer", "force-smoke.service", "force-tmux.service"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for unit in list(Path.home().glob(".config/systemd/user/force*.service")) \
                + list(Path.home().glob(".config/systemd/user/force*.timer")):
            try:
                unit.unlink()
            except OSError:
                pass
        subprocess.call(["systemctl", "--user", "daemon-reload"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.call(["tmux", "-L", cockpit.paths.tmux_socket, "kill-server"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _stop_daemon(cockpit)
    subprocess.call(["tmux", "-L", cockpit.paths.ui_socket, "kill-server"],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # the launcher: only when it is Force's (contains force.cli)
    launcher = Path.home() / ".local/bin" / "force"
    try:
        if launcher.exists() and "force.cli" in launcher.read_text():
            launcher.unlink()
    except OSError:
        pass
    _remove_path_blocks()
    _uninstall_data(cockpit, purge)

    if not purge:
        print(f"kept your data: {data}/registry.json and registry-snapshots/, {cockpit.paths.state}, "
              f"{cockpit.paths.config_file.parent}, ~/.config/force")
    from . import updates
    base = updates.download_base()
    if base:
        print(f"force: removed. Reinstall: curl -fsSL {base}/install.sh | sh")
    return 0


# Variables a running Claude session exports to its children. A pane must never inherit them
# (e.g. when the tmux server or `force new` was started from inside a Claude session): they mark
# the new claude as a child session (transcript saving off) and point it at another session's
# messaging socket and Remote Control bridge.
_SESSION_ENV_PREFIXES = ("CLAUDE_CODE_",)
_SESSION_ENV_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")


def _scrubbed_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items()
            if k not in _SESSION_ENV_NAMES and not k.startswith(_SESSION_ENV_PREFIXES)}


def run_command(cockpit: Cockpit, name: str) -> int:
    """force run NAME (SPEC §7.2). Must not import tmux-heavy modules.

    Stage 1 (pane command): drop inherited Claude session variables, then re-exec through
    `bash -lic` so the pane gets the same environment as an SSH login shell. Stage 2 (inside
    that shell, FORCE_RUN_STAGE=2): exec claude. Variables set by the user's shell rc survive.
    For a SHELL session, stage 1 instead chdir(cwd) and execs `bash -l` directly with the
    scrubbed env + FORCE_SESSION (no claude, no stage 2)."""
    if os.environ.get("FORCE_RUN_STAGE") != "2":
        env = _scrubbed_env(dict(os.environ))
        entry = registry.load(cockpit.paths.registry).get(name)
        from . import kinds
        if entry is not None and kinds.of(entry).direct:
            if entry.archived:
                print(f"force: session {name!r} is archived", file=sys.stderr)
                time.sleep(5)
                return 2
            if not os.path.isdir(entry.cwd):
                print(f"force: cwd {entry.cwd!r} does not exist", file=sys.stderr)
                time.sleep(5)
                return 2
            os.chdir(entry.cwd)
            env["FORCE_SESSION"] = name
            argv, extra = kinds.of(entry).exec_argv(cockpit, entry)
            env.update(extra)
            os.execvpe(argv[0], argv, env)
        env["FORCE_RUN_STAGE"] = "2"
        inner = "exec " + shlex.join([sys.argv[0], "run", name])
        os.execvpe("bash", ["bash", "-lic", inner], env)
    os.environ.pop("FORCE_RUN_STAGE", None)
    entry = registry.load(cockpit.paths.registry).get(name)
    if entry is None:
        print(f"force: no such session {name!r}", file=sys.stderr)
        time.sleep(5)
        return 2
    if entry.archived:
        print(f"force: session {name!r} is archived", file=sys.stderr)
        time.sleep(5)
        return 2
    if not os.path.isdir(entry.cwd):
        print(f"force: cwd {entry.cwd!r} does not exist", file=sys.stderr)
        time.sleep(5)
        return 2
    os.chdir(entry.cwd)
    os.environ["FORCE_SESSION"] = name
    from . import kinds
    try:
        argv, env = kinds.of(entry).exec_argv(cockpit, entry)
    except RuntimeError as e:
        print(f"force: {e}", file=sys.stderr)
        time.sleep(5)
        return 2
    os.environ.update(env)
    os.execvp(argv[0], argv)
    return 0


def _animations(cockpit: Cockpit) -> str:
    a = cockpit.cfg.animations
    return a if a in ("full", "subtle", "off") else "full"


def _glyphs(cockpit: Cockpit) -> str:
    g = cockpit.cfg.glyphs
    return g if g in ("auto", "rich", "plain") else "auto"


def intro_argv(cockpit: Cockpit, cell_aspect: float | None = None) -> list[str] | None:
    """argv of the startup intro in the live release (SPEC §31), or None when it is not built."""
    binary = Path(cockpit.paths.tmux_conf).parent / "bin" / "force-intro"
    if not os.access(binary, os.X_OK):
        return None
    argv = [str(binary), "--animations", _animations(cockpit), "--color", _intro_color(os.environ),
            "--fade-to", THEME_BG, "--handoff"]
    # the client's own value (`force --cell-aspect`, from the client kit) before the server config's
    aspect = cell_aspect if cell_aspect is not None else cockpit.cfg.intro_cell_aspect
    if 1 <= aspect <= 4 and not _tty_reports_pixels():
        argv += ["--cell-aspect", f"{aspect:g}"]
    return argv


def _tty_reports_pixels() -> bool:
    """True when the terminal on stdout reports its pixel size (TIOCGWINSZ; also over ssh). kitty does,
    VTE (Tilix, GNOME Terminal) does not, so the intro would have to guess its cell shape."""
    import fcntl
    import struct
    import termios
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)    # the terminal itself, even if stdout is a pipe
    except OSError:
        return False
    try:
        _r, _c, xpix, ypix = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except (OSError, ValueError):
        return False
    finally:
        os.close(fd)
    return xpix > 0 and ypix > 0


def _has_da1(buf: bytes) -> bool:
    """True when `buf` holds a full DA1 reply (CSI ? ... c), the terminal's cursor-position/device response."""
    i = buf.find(b"\x1b[?")
    return i >= 0 and buf.find(b"c", i) >= 0


def _parse_cell_aspect(reply: bytes, rows: int, cols: int) -> float | None:
    """A terminal's CSI 14t reply (`\x1b[4;<height>;<width>t`) -> the cell aspect (height/width),
    rounded to 2 decimals. None for a missing/garbage/broken reply, zero pixel sizes, or an aspect outside 1..4."""
    m = re.search(rb"\x1b\[4;(\d+);(\d+)t", reply)
    if not m:
        return None
    height, width = int(m.group(1)), int(m.group(2))
    if height <= 0 or width <= 0 or rows <= 0 or cols <= 0:
        return None
    aspect = round((height / rows) / (width / cols), 2)
    return aspect if 1 <= aspect <= 4 else None


_BG_REPLY = re.compile(rb"\x1b\]11;(rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4})(?:\x1b\\|\x07)")


def _parse_bg(reply: bytes) -> str | None:
    """A terminal's OSC 11 reply (`ESC ] 11 ; rgb:R/G/B`, 1-4 hex digits per channel, ended by ST or BEL) ->
    `#rrggbb`, the form every terminal takes back (Konsole ignores the `rgb:` form when setting). None when
    there is no such reply."""
    m = _BG_REPLY.search(reply)
    if not m:
        return None
    chans = m.group(1).decode()[4:].split("/")
    return "#" + "".join(f"{int(c, 16) * 255 // (16 ** len(c) - 1):02x}" for c in chans)


def _query_terminal(want_aspect: bool) -> tuple[float | None, str | None]:
    """Ask the terminal on /dev/tty, in one round trip ended by DA1 (which every terminal answers; 0.5 s at
    most), for its background (OSC 11) and, with `want_aspect`, its text-area pixel size (CSI 14 t).
    Returns (cell aspect or None, background spec or None). Nothing is asked unless stdin and stdout are the
    terminal (not in pipes, scripts or tests). termios is always restored."""
    import fcntl
    import select
    import struct
    import termios
    import tty
    if not (os.isatty(0) and os.isatty(1)):
        return None, None
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)    # the terminal itself
    except OSError:
        return None, None
    try:
        try:
            rows, cols, _x, _y = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
            saved = termios.tcgetattr(fd)
        except (OSError, ValueError, termios.error):
            return None, None
        tty.setraw(fd)                         # raw so the replies come straight back to us
        try:
            # background, text area size (px), then DA1 as the end marker
            os.write(fd, b"\x1b]11;?\x1b\\" + (b"\x1b[14t" if want_aspect else b"") + b"\x1b[c")
            buf = b""
            end = time.monotonic() + 0.5
            while time.monotonic() < end:
                r, _, _ = select.select([fd], [], [], max(0.0, end - time.monotonic()))
                if not r:
                    break
                chunk = os.read(fd, 256)
                if not chunk:
                    break
                buf += chunk
                if _has_da1(buf):            # DA1 caps the reply
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        aspect = _parse_cell_aspect(buf, rows, cols) if want_aspect and rows and cols else None
        return aspect, _parse_bg(buf)
    except OSError:
        return None, None
    finally:
        os.close(fd)


def _paint_terminal_bg(term_bg: str | None) -> None:
    """Set the terminal's own background to the theme's (OSC 11) while Force runs, so the edge pixels the
    terminal paints itself match (GNOME Terminal, Konsole, kitty, Terminator; Tilix ignores it). Only for a
    terminal that told us its background (term_bg), so _exec_tmux can put exactly that one back."""
    if not term_bg:
        return
    try:
        os.write(1, f"\x1b]11;#{THEME_BG}\x1b\\".encode())
    except OSError:
        pass


# sh runs the tmux client and cleans up after it: when the client did not end cleanly (the server went away while it
# was attached, e.g. the quit button's detach racing the ui server's exit) tmux leaves the terminal on its alternate
# screen with mouse reporting on, so leave that screen and reset the modes; then give the terminal its own
# background back when Force painted it (_paint_terminal_bg). The client's exit status is kept.
_CLIENT_SH = ('tmux "$@"; s=$?; '
              '[ "$s" -ne 0 ] && printf "\033[?1049l\033[?25h\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?2004l\033[0m"; '
              '[ -n "$FORCE_TERM_BG" ] && printf "\033]11;%s\007" "$FORCE_TERM_BG"; exit $s')   # BEL ends the OSC


def _exec_tmux(argv: list[str], term_bg: str | None) -> None:
    """Become the tmux client through a small sh that waits for it, so the terminal is put right however the client
    ends (detach, quit, the server going away): see _CLIENT_SH."""
    os.environ["FORCE_TERM_BG"] = term_bg or ""
    os.execvp("sh", ["sh", "-c", _CLIENT_SH, "force", *argv[1:]])


def _intro_color(env: dict[str, str]) -> str:
    """truecolor on a terminal that supports it (xterm/alacritty/kitty) when the locale is UTF-8."""
    term = env.get("TERM", "")
    locale = next((env[k] for k in ("LC_ALL", "LC_CTYPE", "LANG") if env.get(k)), "")
    if ((term.startswith("xterm") or term.startswith("alacritty") or "kitty" in term)
            and ("utf-8" in locale.lower() or "utf8" in locale.lower())):
        return "truecolor"
    return "auto"


def play_intro(cockpit: Cockpit, no_intro: bool, cell_aspect: float | None = None) -> None:
    """Play the startup intro on attach; any reason not to (opt-out, off, missing binary) returns."""
    if no_intro or not cockpit.cfg.intro or _animations(cockpit) == "off":
        return
    argv = intro_argv(cockpit, cell_aspect)
    if argv is None:
        return
    try:
        rc = subprocess.call(argv)
    except OSError:
        return
    if rc == 130:
        raise KeyboardInterrupt


_WELCOME_TEXT = " Welcome to Force · + new (sidebar) starts an agent session · Ctrl+Space opens the menu "


def _spawn_welcome(cockpit: Cockpit) -> None:
    """On this machine's very first attach (marker <state>/welcomed), show a one-off tmux message to the
    client, spawned detached right before it starts. The display-message runs 2 s in (after the client drew
    its first frame) and lasts 9 s. No tty -> nothing."""
    if (cockpit.paths.state / "welcomed").exists():
        return
    try:
        tty = os.ttyname(0)
    except OSError:
        return
    sock = cockpit.paths.ui_socket if cockpit.split else cockpit.paths.tmux_socket
    sh = (f'sleep 2; tmux -L {shlex.quote(sock)} display-message '
          f'-c {shlex.quote(tty)} -d 9000 {shlex.quote(_WELCOME_TEXT)}')
    try:
        cockpit.paths.state.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["sh", "-c", sh], start_new_session=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (cockpit.paths.state / "welcomed").write_text("")
    except OSError:
        pass


def _host_files(cockpit: Cockpit) -> tuple[Path, Path]:
    """(snapshot, registry) of the host the ui shows: this machine's, or a remote's watched copies
    (FORCE_HOST, SPEC §40)."""
    host = os.environ.get("FORCE_HOST", "")
    if host:
        d = remote_mod.remote_dir(cockpit.paths, host)
        return d / "snapshot.json", d / "registry.json"
    return cockpit.paths.snapshot, cockpit.paths.registry


def rust_sidebar_argv(cockpit: Cockpit) -> list[str] | None:
    """argv of the Rust sidebar in the live release (SPEC §22), or None when it is not built."""
    binary = Path(cockpit.paths.tmux_conf).parent / "bin" / "force-sidebar"
    if not os.access(binary, os.X_OK):
        return None
    return [str(binary), "--snapshot", str(_host_files(cockpit)[0]), "--force-bin", cockpit.cockpit_bin,
            "--animations", _animations(cockpit), "--glyphs", _glyphs(cockpit)]


def _exec_rust(argv: list[str] | None, what: str) -> int:
    """Exec a Rust UI program from the live release (SPEC §22), or say it is not built."""
    if argv is None:
        print(f"force: the {what} is not built into the live release (./deploy.sh)", file=sys.stderr)
        time.sleep(5)          # keep the message readable in the pane before it closes
        return 1
    os.execv(argv[0], argv)
    return 0


def sidebar_command(cockpit: Cockpit) -> int:
    return _exec_rust(rust_sidebar_argv(cockpit), "sidebar")


def rust_header_argv(cockpit: Cockpit) -> list[str] | None:
    """argv of the Rust header in the live release (SPEC §22), or None when it is not built."""
    share_dir = Path(cockpit.paths.tmux_conf).parent
    binary = share_dir / "bin" / "force-header"
    if not os.access(binary, os.X_OK):
        return None
    return [str(binary), "--snapshot", str(_host_files(cockpit)[0]), "--share-dir", str(share_dir),
            "--animations", _animations(cockpit), "--glyphs", _glyphs(cockpit)]


def header_command(cockpit: Cockpit) -> int:
    return _exec_rust(rust_header_argv(cockpit), "header")


def rust_dialog_argv(cockpit: Cockpit, kind: str, name: str | None) -> list[str] | None:
    """argv of the Rust popup (SPEC §22), or None when it is not built."""
    binary = Path(cockpit.paths.tmux_conf).parent / "bin" / "force-dialog"
    if not os.access(binary, os.X_OK):
        return None
    return ([str(binary), kind] + ([name] if name else [])
            + ["--registry", str(_host_files(cockpit)[1]), "--snapshot", str(_host_files(cockpit)[0]),
               "--force-bin", cockpit.cockpit_bin, "--tmux-socket", cockpit.paths.tmux_socket,
               "--glyphs", _glyphs(cockpit)])


def _shown_session(cockpit: Cockpit, host: str, client: str) -> str | None:
    """The session the nested client `client` shows right now, asked from its host (local tmux, or the remote
    over ssh). The ui window's @force_session can lag a switch by a poll; a dialog that renames, archives,
    deletes or moves "this session" must not act on the one shown before. None: the host did not answer."""
    if not client:
        return None
    try:
        if not host:
            return cockpit.tmux.run("display", "-p", "-c", client, "#{client_session}") or None
        r = remote_mod.get(cockpit.paths, host)
        if r is None:
            return None
        p = subprocess.run(remote_mod.ssh_argv(cockpit.paths, r, "tmux", "-L", cockpit.paths.tmux_socket, "display", "-p", "-c", client,
                                               "#{client_session}"), capture_output=True, text=True, timeout=15)
        return (p.stdout.strip() or None) if p.returncode == 0 else None
    except (TmuxError, OSError, subprocess.TimeoutExpired):
        return None


def dialog_command(cockpit: Cockpit, kind: str, name: str | None) -> int:
    host = os.environ.get("FORCE_HOST", "")
    if host:
        # the dialog's own tmux calls (switch the nested client) go to the remote; its `force …` calls are
        # forwarded there by FORCE_HOST (_forward)
        r = remote_mod.get(cockpit.paths, host)
        if r is not None:
            os.environ["FORCE_HOST_TMUX"] = remote_mod.host_tmux_prefix(cockpit.paths, r)
    client = os.environ.get("FORCE_CLIENT", "")
    if name and client and kind in ("rename", "archive", "delete", "move"):
        shown = _shown_session(cockpit, host, client)
        if shown is None and host:
            print(f"force: {host} does not answer; try again", flush=True)
            time.sleep(2)
            return 1
        if shown and shown != name:
            name = shown
            os.environ["FORCE_SESSION"] = shown
    return _exec_rust(rust_dialog_argv(cockpit, kind, name), "dialog")


# --------------------------------------------------------------------------- #
# argparse + dispatch
# --------------------------------------------------------------------------- #

def _cell_aspect(text: str) -> float:
    v = float(text)
    if not 1 <= v <= 4:
        raise argparse.ArgumentTypeError("must be between 1 and 4")
    return v


def _attach_client_args(p: argparse.ArgumentParser, default=None) -> None:
    """What the client knows about its terminal; not needed normally (Force measures it), only for overrides."""
    p.add_argument("--glyphs", choices=["auto", "rich", "plain"], default="auto" if default is None else default,
                  help="this terminal's glyph tier (override; rich = it has the Nerd Font symbols)")
    p.add_argument("--cell-aspect", type=_cell_aspect, default=default,
                  help="this terminal's cell height/width for the intro (override; normally measured)")
    p.add_argument("--remote", default=default, metavar="NAME",
                   help="show this remote host (force remote add); default: local sessions, else the last host used")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]) or "force",
                                    description="Force — the code cockpit: your AI coding agent sessions in one terminal")
    parser.add_argument("--no-intro", action="store_true", help="attach without the startup intro")
    from .updates import current_version   # the installed release (VERSION next to the app); none in a dev build
    parser.add_argument("--version", action="version", version=f"force {current_version() or '(development build)'}",
                        help="print the installed version and exit")
    _attach_client_args(parser)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("attach", help="attach to the cockpit")
    p.add_argument("name", nargs="?", help="session name to attach to")
    p.add_argument("--no-intro", action="store_true", default=argparse.SUPPRESS,
                  help="attach without the startup intro")
    _attach_client_args(p, default=argparse.SUPPRESS)

    p = sub.add_parser("new", help="create a new session")
    p.add_argument("name", nargs="?", help="session name (omit: named after your first prompt)")
    p.add_argument("--cwd", default=None, help="working directory (default $HOME)")
    p.add_argument("--no-switch", action="store_true", help="do not switch to it")
    p.add_argument("--shell", action="store_true", help="create a managed shell session instead of an agent")
    p.add_argument("--kind", choices=["claude", "opencode", "shell"], default=None,
                   help="session kind: claude (default), opencode or shell")

    p = sub.add_parser("kinds", help="session kinds this server can run (claude, opencode, shell)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("dirs", help="folder names for Tab-completion (internal, dialogs)")
    p.add_argument("dir")
    p.add_argument("prefix")

    p = sub.add_parser("opencode-update", help="update Force's own OpenCode (with opencode_managed = true)")
    p.add_argument("--version", default=None, help="install this release instead of the latest")
    p.add_argument("--check", action="store_true", help="only say whether a newer release exists")

    p = sub.add_parser("update", help="update Force to the latest release (—check just says whether one exists)")
    p.add_argument("--check", action="store_true", help="only say whether a newer release exists")

    p = sub.add_parser("uninstall", help="remove Force from this machine (—purge also removes your data)")
    p.add_argument("--purge", action="store_true", help="also remove your registry, state and config")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask")

    p = sub.add_parser("sync-titles", help="make the phone (Remote Control) names match the Force names again")
    p.add_argument("names", nargs="*")

    p = sub.add_parser("rename", help="rename a session")
    p.add_argument("old")
    p.add_argument("new")

    p = sub.add_parser("delete", help="delete a session for good (live or archived)")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="even while it is working")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask")

    p = sub.add_parser("archive", help="archive a session")
    p.add_argument("name")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("unarchive", help="unarchive a session")
    p.add_argument("name")

    p = sub.add_parser("restart", help="restart a session (same conversation), or --all of them")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", dest="all_", action="store_true",
                   help="all sessions; busy ones restart as soon as they are idle")

    p = sub.add_parser("upgrade", help="upgrade outdated sessions")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("list", help="list sessions")
    p.add_argument("--all", dest="all_", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("switch", help="switch the calling client")
    p.add_argument("name", nargs="?")
    p.add_argument("--index", type=int, default=None)
    p.add_argument("--next", action="store_true")
    p.add_argument("--prev", action="store_true")
    p.add_argument("--client", default=None)

    p = sub.add_parser("forget", help="forget an archived session")
    p.add_argument("name")

    p = sub.add_parser("move", help="move a session up/down in the sidebar, or to another folder")
    p.add_argument("name")
    p.add_argument("direction", metavar="up|down|DIR",
                   help="up/down: the sidebar order; a folder: the session's working folder (SPEC §7.7)")

    p = sub.add_parser("ui", help="layout mode: classic (one tmux) or split (ui tmux per terminal, SPEC §39)")
    p.add_argument("mode", nargs="?", choices=["classic", "split"])

    p = sub.add_parser("nested", help="the ui main pane: a nested client of the host (internal)")
    p.add_argument("name")
    p.add_argument("--remote", default=None)
    p.add_argument("--report", default=None)

    p = sub.add_parser("watch", help="this host's sessions as JSON lines, for a remote ui (SPEC §40)")

    p = sub.add_parser("remote", help="remote hosts (alias of `force host`): add NAME [SSH] | list | remove NAME | upgrade NAME")
    p.add_argument("action", choices=["add", "list", "remove", "upgrade"])
    p.add_argument("name", nargs="?")
    p.add_argument("ssh", nargs="?")
    p.add_argument("--no-install", action="store_true", help="add only if Force is already installed there")

    p = sub.add_parser("host", help="remote hosts: add NAME [SSH] | list | remove NAME | upgrade NAME")
    p.add_argument("action", choices=["add", "list", "remove", "upgrade"])
    p.add_argument("name", nargs="?")
    p.add_argument("ssh", nargs="?")
    p.add_argument("--no-install", action="store_true", help="add only if Force is already installed there")

    p = sub.add_parser("settings",
                      help="user settings: show | set KEY VALUE | unset KEY | path | edit")
    p.add_argument("action", nargs="?", default=None,
                  help="set KEY VALUE | unset KEY | path | edit (omit: show the settings)")
    p.add_argument("key", nargs="?", help="the setting (camelCase, e.g. opencodeManaged)")
    p.add_argument("value", nargs="?", help="its new value")
    p.add_argument("--all", action="store_true", help="also the advanced settings")
    p.add_argument("--json", action="store_true", help="print the effective settings as JSON")

    p = sub.add_parser("install-to", help="copy this release to another machine over ssh and install it there")
    p.add_argument("ssh")

    p = sub.add_parser("ui-host", help="show another host in a ui window (internal, host picker)")
    p.add_argument("name")
    p.add_argument("--window", required=True)
    p.add_argument("--attention", action="store_true", help="open the session that needs you there (header marker)")

    p = sub.add_parser("hosts-menu", help="the host picker menu (internal)")
    p.add_argument("--client", required=True)
    p.add_argument("--window", required=True)

    p = sub.add_parser("smoke", help="self-test against the real Claude Code and OpenCode (SPEC §37)")
    p.add_argument("--if-due", action="store_true", help="only when versions changed or the last run is old")
    p.add_argument("--last", action="store_true", help="show the last result")

    p = sub.add_parser("doctor", help="health check of the cockpit (--fix repairs what it can)")
    p.add_argument("--fix", action="store_true")

    fonts_p = sub.add_parser(
        "fonts",
        help="install Force's Nerd Font symbols (icons) and remove the old terminal kit (--cleanup-only / --uninstall)")
    fonts_p.add_argument("args", nargs=argparse.REMAINDER)

    terminals_p = sub.add_parser(
        "terminals",
        help="deprecated: was Force's terminal profiles + launchers; now Force themes itself and just runs `force fonts`")
    terminals_p.add_argument("args", nargs=argparse.REMAINDER)

    p = sub.add_parser("prompt", help="send TEXT into the agent's input (SPEC §43.1)")
    p.add_argument("name")
    p.add_argument("text", nargs="+")
    p.add_argument("--wait", action="store_true", help="wait for the turn to end before returning")
    p.add_argument("--until", action="append", default=[],
                  help="states to wait for with --wait (repeatable; default your-turn idle needs-you)")
    p.add_argument("--timeout", type=float, default=None, help="give up after SEC seconds")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("wait", help="wait until a session is in one of --until's states (SPEC §43.1)")
    p.add_argument("name")
    p.add_argument("--until", action="append", default=[],
                  help="states to wait for (repeatable; default your-turn idle needs-you)")
    p.add_argument("--timeout", type=float, default=None, help="give up after SEC seconds")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("read", help="print the agent pane's last N rows (SPEC §43.1)")
    p.add_argument("name")
    p.add_argument("--lines", type=int, default=80, help="rows to show (default 80)")
    p.add_argument("--ansi", action="store_true", help="keep ANSI colour escapes")

    p = sub.add_parser("keys", help="send-keys to the agent pane (SPEC §43.1)")
    p.add_argument("name")
    p.add_argument("keys", nargs="+")
    p.add_argument("--literal", action="store_true", help="send the words as literal text")

    p = sub.add_parser("report", help="report what a shell session is doing (SPEC §43.2)")
    p.add_argument("name", nargs="?", help="default: the Force session of the calling pane")
    p.add_argument("--state", default=None, help="working | needs-you | your-turn | idle")
    p.add_argument("--summary", default=None, help="one-line activity shown on the card")
    p.add_argument("--pid", type=int, default=None, help="reporting process (default the caller's parent)")
    p.add_argument("--clear", action="store_true", help="remove the report (same as --state idle)")

    p = sub.add_parser("status", help="report the calling shell session's state: force status working|needs-you|"
                                      "your-turn|idle [--summary TEXT]")
    p.add_argument("state", help="working | needs-you | your-turn | idle")
    p.add_argument("name", nargs="?", help="default: the Force session of the calling pane")
    p.add_argument("--summary", default=None, help="one-line activity shown on the card")
    p.add_argument("--pid", type=int, default=None, help="reporting process (default the caller's parent)")

    p = sub.add_parser("notify-test", help="run the notifyCommand setting once with a sample event")
    p.add_argument("event", nargs="?", default="needs-you", choices=["needs-you", "finished", "down"])
    p.add_argument("--session", default="example", help="the session name the sample event names")

    sub.add_parser("skill", help="print the guide for agents that drive Force (also: force --skill)")
    from . import support as support_mod
    support_mod.add_parser(sub)
    sub.add_parser("mcp", help="Force's local MCP server on stdin/stdout, for Chuck's session (SPEC §50)")

    p = sub.add_parser("explain", help="why a card shows its state (SPEC §43.3)")
    p.add_argument("name")
    p.add_argument("--lines", type=int, default=8, help="screen rows to show (default 8)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("import", help="migrate existing Claude sessions")
    p.add_argument("session", nargs="?", default=None,
                   help="session id/prefix, running Claude pid or name")
    p.add_argument("--all", dest="all_", action="store_true",
                   help="list every stopped session, not just the newest 15")
    p.add_argument("--screen", action="store_true",
                   help="bulk import: every idle Claude in a GNU screen session")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--pid", type=int, default=None)
    p.add_argument("--name", default=None)

    p = sub.add_parser("share", help="share a session with another user of this host")
    p.add_argument("name")
    p.add_argument("--user", required=True)
    p.add_argument("--write", action="store_true", help="let the guest type into the session")

    p = sub.add_parser("unshare", help="stop sharing a session with a user (or everyone)")
    p.add_argument("name")
    p.add_argument("--user", default=None)

    sub.add_parser("shares", help="list every shared session")

    p = sub.add_parser("share-proxy", help="run a share proxy (internal)")
    p.add_argument("name")
    p.add_argument("user")
    p.add_argument("mode", choices=["r", "w"])

    sub.add_parser("sidebar-toggle", help="toggle sidebars on/off")
    sub.add_parser("daemon", help="run the daemon")
    prun = sub.add_parser("run", help="run a session's agent process (internal)")
    prun.add_argument("name")
    sub.add_parser("sidebar", help="run the sidebar UI (internal)")
    sub.add_parser("header", help="run the header pane UI (internal)")

    p = sub.add_parser("dialog", help="run a popup dialog (internal)")
    dsub = p.add_subparsers(dest="dialog_kind", required=True)
    dsub.add_parser("new", help="create a new session")
    pr = dsub.add_parser("rename", help="rename a session")
    pr.add_argument("name")
    pm = dsub.add_parser("move", help="move a session to another folder")
    pm.add_argument("name")
    pa = dsub.add_parser("archive", help="archive a session")
    pa.add_argument("name")
    pd = dsub.add_parser("delete", help="delete a session for good")
    pd.add_argument("name")
    dsub.add_parser("archived", help="list archived sessions")
    dsub.add_parser("jump", help="jump to a session")

    return parser


def _dispatch(args) -> int:
    cockpit = make_cockpit()
    cmd = args.command
    if cmd is None or cmd == "attach":
        return attach_command(cockpit, getattr(args, "name", None), getattr(args, "no_intro", False),
                              getattr(args, "glyphs", "auto"), getattr(args, "cell_aspect", None),
                              getattr(args, "remote", None))
    if cmd == "new":
        return new_command(cockpit, args.name, args.cwd, args.no_switch, args.shell, args.kind)
    if cmd == "kinds":
        return kinds_command(cockpit, args.json)
    if cmd == "dirs":
        return dirs_command(args.dir, args.prefix)
    if cmd == "opencode-update":
        return opencode_update_command(cockpit, args.version, args.check)
    if cmd == "update":
        from . import updates
        return updates.update_command(args.check)
    if cmd == "uninstall":
        return uninstall_command(cockpit, args.purge, args.yes)
    if cmd == "rename":
        return rename_command(cockpit, args.old, args.new)
    if cmd == "sync-titles":
        return sync_titles_command(cockpit, args.names)
    if cmd == "archive":
        return archive_command(cockpit, args.name, args.force)
    if cmd == "unarchive":
        return unarchive_command(cockpit, args.name)
    if cmd == "restart":
        return restart_command(cockpit, args.name, args.all_)
    if cmd == "upgrade":
        return upgrade_command(cockpit, args.name, args.all)
    if cmd == "list":
        return list_command(cockpit, args.all_, args.json)
    if cmd == "switch":
        return switch_command(cockpit, args.name, args.index, args.next, args.prev, args.client)
    if cmd == "delete":
        return delete_command(cockpit, args.name, args.force, args.yes)
    if cmd == "forget":
        return forget_command(cockpit, args.name)
    if cmd == "move":
        if args.direction in ("up", "down"):
            return move_command(cockpit, args.name, args.direction)
        return move_folder_command(cockpit, args.name, args.direction)
    if cmd == "ui":
        return ui_command(cockpit, args.mode)
    if cmd == "nested":
        return nested_command(cockpit, args.name, args.remote, args.report)
    if cmd == "watch":
        return watch_command(cockpit)
    if cmd == "remote":
        return remote_command(cockpit, args.action, args.name, args.ssh, args.no_install)
    if cmd == "host":
        return remote_command(cockpit, args.action, args.name, args.ssh, args.no_install)
    if cmd == "settings":
        return settings_command(cockpit, args.action, args.key, args.value, args.all, args.json)
    if cmd == "install-to":
        return install_to_command(cockpit, args.ssh)
    if cmd == "ui-host":
        return ui_host_command(cockpit, args.name, args.window, args.attention)
    if cmd == "hosts-menu":
        return hosts_menu_command(cockpit, args.client, args.window)
    if cmd == "smoke":
        from . import smoke
        return smoke.command(cockpit.paths, cockpit.cfg, args.if_due, args.last)
    if cmd == "doctor":
        return doctor_command(cockpit, args.fix)
    if cmd == "fonts":
        return fonts_command(args.args)
    if cmd == "terminals":
        return terminals_alias_command(args.args)
    if cmd == "prompt":
        return automation.prompt_command(cockpit, args)
    if cmd == "wait":
        return automation.wait_command(cockpit, args)
    if cmd == "read":
        return automation.read_command(cockpit, args)
    if cmd == "keys":
        return automation.keys_command(cockpit, args)
    if cmd == "report":
        return automation.report_command(cockpit, args)
    if cmd == "status":
        args.clear = False
        return automation.report_command(cockpit, args)
    if cmd == "notify-test":
        return notify_test_command(cockpit, args.event, args.session)
    if cmd == "skill":
        return skill_command(cockpit)
    if cmd == "support":
        from . import support as support_mod
        return support_mod.command(cockpit, args)
    if cmd == "mcp":
        from . import mcp as mcp_mod
        return mcp_mod.serve()
    if cmd == "explain":
        return automation.explain_command(cockpit, args)
    if cmd == "import":
        return import_command(cockpit, args.dry_run, args.pid, args.name, args.session,
                             args.all_, args.screen)
    if cmd == "share":
        return share_command(cockpit, args.name, args.user, args.write)
    if cmd == "unshare":
        return unshare_command(cockpit, args.name, args.user)
    if cmd == "shares":
        return shares_command(cockpit)
    if cmd == "share-proxy":
        return share_proxy_command(cockpit, args.name, args.user, args.mode)
    if cmd == "sidebar-toggle":
        return sidebar_toggle_command(cockpit)
    if cmd == "daemon":
        return daemon_command(cockpit)
    if cmd == "run":
        return run_command(cockpit, args.name)
    if cmd == "header":
        return header_command(cockpit)
    if cmd == "sidebar":
        return sidebar_command(cockpit)
    if cmd == "dialog":
        return dialog_command(cockpit, args.dialog_kind, getattr(args, "name", None))
    build_parser().print_help(sys.stderr)
    return 2


# commands that act on a host's sessions: run on the remote the ui shows (FORCE_HOST, SPEC §40)
_FORWARDED = {"new", "rename", "sync-titles", "delete", "archive", "unarchive", "restart", "upgrade", "list", "switch",
              "forget", "move", "kinds", "dirs", "prompt", "wait", "read", "keys", "report", "status", "explain",
              "import"}


def _forward(argv: list[str]) -> int | None:
    """In a ui that shows a remote (FORCE_HOST in its environment), a session command runs there."""
    host = os.environ.get("FORCE_HOST", "")
    words = [a for a in argv if not a.startswith("-")]
    if not host or not words or words[0] not in _FORWARDED:
        return None
    paths = config.get_paths()
    r = remote_mod.get(paths, host)
    if r is None:
        print(f"force: no host {host!r}", file=sys.stderr)
        return 1
    return subprocess.call(remote_mod.ssh_argv(paths, r, remote_mod.REMOTE_FORCE, *argv))


def _prepend_tmux_bin() -> None:
    """Put Force's own static tmux first on PATH when it ships (release tarball), so every `tmux` Force runs
    (Python subprocess/exec, the tmux server it starts, run-shell scripts, the Rust panes, the sh wrapper in
    `_exec_tmux`) is that one. On dev deploys (no shipped tmux) the system tmux is used as today."""
    d = config.tmux_bin_dir(config.get_paths())
    if d is None:
        return
    dstr = str(d)
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if parts and parts[0] == dstr:
        return
    os.environ["PATH"] = os.pathsep.join([dstr] + parts)


def _instance_refusal(words: list[str]) -> int | None:
    """A development instance (SPEC §2.1) never touches what the live install owns: its launcher, units, PATH
    blocks, fonts and releases. Exit 2 for the commands that would; None otherwise."""
    inst = config.instance()
    if inst is None or not words:
        return None
    how = {"update": "update it from the checkout with ./dev.sh",
           "uninstall": "remove it with ./dev.sh reset",
           "fonts": "fonts belong to the live install", "terminals": "fonts belong to the live install"}
    if words[0] in how:
        print(f"force: this is the {inst} instance (FORCE_INSTANCE={inst}); {how[words[0]]}", file=sys.stderr)
        return 2
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        config.instance()
    except ValueError as e:
        print(f"force: {e}", file=sys.stderr)
        return 2
    _prepend_tmux_bin()
    words = list(sys.argv[1:] if argv is None else argv)
    if words == ["--skill"]:
        return skill_command(None)
    refused = _instance_refusal(words)
    if refused is not None:
        return refused
    fwd = _forward(words)
    if fwd is not None:
        return fwd
    if words[:1] == ["fonts"]:
        # everything after it goes to share/fonts.sh as is: argparse's REMAINDER drops a leading
        # option (`force fonts --uninstall` failed)
        return fonts_command(words[1:])
    if words[:1] == ["terminals"]:
        # the deprecated alias, with its leading options, goes straight through to fonts.sh's cleanup modes
        return terminals_alias_command(words[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except RegistryError as e:
        print(f"force: {e}", file=sys.stderr)
        return 1
    except automation.CmdError as e:
        if getattr(args, "json", False):
            print(json.dumps({"error": e.code, "message": e.message}), file=sys.stderr)
        else:
            print(f"force: {e.message}", file=sys.stderr)
        return 1
    except automation.UsageError as e:
        print(f"force: {e.message}", file=sys.stderr)
        return 2
    except TmuxError as e:
        print(f"force: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
