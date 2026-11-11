"""The force supervisor loop (SPEC §6)."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import activity, kinds, report as report_mod, claude as claude_mod, opencode as opencode_mod, remote as remote_mod, share as share_mod, updates, usage as usage_mod
from . import config
from .config import Config, Paths, ensure_dirs
from .lifecycle import Cockpit
from .tmux import LEGACY_AGENT_ROLE, TmuxError
from .registry import (RegistryError, is_support, is_claude, is_opencode, is_shell, kind as kind_of, locked, slugify, valid_name,
                     slugify, valid_name)
from .state import (PaneInfo, Seen, SessionView, Snapshot, State, compute_state, compute_state_why, clean_title,
                   glyph, load_seen, save_seen, unread, write_snapshot)

_UPGRADE_INTERVAL = 30.0
_CRASH_UNKNOWN_GRACE = 5.0
_RELOAD_DEBOUNCE = 2.0          # SPEC §6.11: wait this long of clock time with a stable fp
_UI_ORPHAN_SECS = 60           # a ui session without a client this long has lost its terminal
_SIDEBAR_RESPAN_INTERVAL = 10.0  # SPEC §6.11: min clock time between sidebar respawns per session
_ACTIVITY_MIN_INTERVAL = 3.0       # SPEC §15: min clock time between transcript re-describes per session
_MODE_INTERVAL = 2.0               # min clock time between permission-mode footer captures per session
_SHARE_THROTTLE = 10.0             # min clock time between proxy (re)starts per (session, user) (SPEC §19)
_OC_HOLD = kinds._OC_HOLD          # an OpenCode TUI's API that misses an answer keeps its last state this long
# Footer phrases at the bottom of the agent pane (Claude Code 2.1.292), mapped to the sidebar badge modes.
# Search case-sensitively; the LAST occurrence in the captured tail wins.
_MODE_PHRASES = (
    ("auto mode on", "auto"),
    ("manual mode on", "manual"),
    ("accept edits on", "edits"),
    ("plan mode on", "plan"),
    ("bypass permissions on", "bypass"),
)
# modules the live daemon imports; reload only applies when all of these still import cleanly.
# every module the new release contains (not a fixed list: removing a module once made every later
# release fail this check, so the daemon kept running old code)
_RELOAD_IMPORT = ("import importlib, pkgutil, force\n"
                  "for m in pkgutil.iter_modules(force.__path__):\n"
                  "    importlib.import_module('force.' + m.name)")


def _log(step: str, err) -> None:
    print(f"force: {step}: {err}", file=sys.stderr)


def _pkg_dir() -> Path:
    """The force package directory. Deliberately WITHOUT .resolve()/realpath: deploy.sh re-points the
    installed force dir via an atomic symlink swap, and an unresolved os.scandir follows the current link
    target, so a swap surfaces as a fingerprint change (SPEC §6.11). daemon.py lives inside the cockpit
    package, so its parent *is* the package dir as imported (symlink preserved)."""
    return Path(__file__).parent


def _stop(pid: int) -> None:
    """SIGTERM a proxy - but only if that pid still IS a Force share proxy: a recorded pid may have
    been reused by an unrelated process of the user. A pid that is gone is fine."""
    if not _pid_is_live_proxy(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _pid_is_live_proxy(pid: int) -> bool:
    """A Force share proxy is live when its pid is alive AND its cmdline mentions `share-proxy` (so an
    unrelated process that re-used the pid is never mistaken for ours)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    return "share-proxy" in cmdline


def _share_key(stem: str) -> tuple[str, str] | None:
    """`<name>.<user>` -> (name, user). Registry names never contain a '.', so the first dot splits it."""
    name, sep, user = stem.partition(".")
    if not sep or not name or not user:
        return None
    return name, user


def _write_share_json(shares_dir: Path, name: str, user: str, pid: int, mode: str) -> None:
    """Atomically write $FORCE_STATE/shares/<name>.<user>.json."""
    dest = shares_dir / f"{name}.{user}.json"
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": pid, "mode": mode}))
    os.replace(tmp, dest)


def _delete_share_json(shares_dir: Path, name: str, user: str) -> None:
    try:
        (shares_dir / f"{name}.{user}.json").unlink()
    except FileNotFoundError:
        pass


def _install_join(tmux_conf: Path) -> None:
    """Install the guest client `force-join` into the share dir (0755) if missing or different. The
    source is <tmux_conf parent>/force-join WITHOUT symlink resolution, so a deploy symlink swap is followed
    live (mirrors the daemon's other tmux.conf handling)."""
    src = Path(tmux_conf).parent / "force-join"
    try:
        if not src.is_file():
            return
        dest = share_mod.share_dir() / "force-join"
        try:
            if dest.read_bytes() == src.read_bytes() and (dest.stat().st_mode & 0o777) == 0o755:
                return
        except OSError:
            pass
        dest.write_bytes(src.read_bytes())
        dest.chmod(0o755)
    except OSError as e:
        _log("shares", e)


def shown_activity(status, act: str | None) -> str | None:
    """The activity phrase for a session. Claude reports `shell` when its turn is over but a background
    command it started is still running (it resumes when that ends): the transcript's last record is then
    usually the reply text, so the phrase would wrongly say "writing a reply"."""
    if status is not None and status.status == "shell":
        return "waiting for a command"
    return act


TRUST_ACTIVITY = "trust this folder?"
TRUST_DECLINED_ACTIVITY = "folder not trusted"
TRUST_MARKERS = ("yes, i trust this folder", "is this a project you created or one you trust")


def pending_restart_activity(state: State) -> str | None:
    """`restart --all` left this working session to restart once its turn ends: say so in the sidebar,
    or it looks as if the restart never happened (SPEC §7.3). A session that needs you keeps its prompt
    label (the sidebar shows only that); it restarts once answered and idle."""
    return "restarts after this turn" if state == State.WORKING else None


RESTART_NOTE_S = 10.0


def restart_note(state: State, ago: float | None, same_process: bool, up_for: float | None) -> str | None:
    """Feedback for a restart from the menu (or an upgrade), in place of the card's state label (SPEC §7.3):
    "restarting" from force's stop until the agent is back up, then "restarted" for its first seconds.
    `ago`: seconds since force stopped it; `same_process`: the pane still runs the process it stopped;
    `up_for`: seconds the pane's current process has been running (whole-second accuracy)."""
    if ago is None or not 0 <= ago < 60:
        return None
    if same_process or state == State.STARTING:
        return "restarting"            # the old process is still dying, or the new one is starting
    if state in (State.IDLE, State.YOUR_TURN, State.STALE) and (up_for is None or up_for < RESTART_NOTE_S):
        return "restarted"
    return None


_STATE_WORDS = {State.NEEDS_YOU: "needs-you", State.WORKING: "working", State.YOUR_TURN: "your-turn",
                State.IDLE: "idle", State.STALE: "stale", State.DOWN: "down", State.STARTING: "starting",
                State.SHELL: "shell", State.UNKNOWN: "unknown", State.ARCHIVED: "archived"}


def notify_env(view, event: str) -> dict[str, str]:
    """The environment of the notify command for one event (SPEC §49.4)."""
    if event == "needs-you":
        what = view.activity or ("needs permission" if view.kind != "shell" else "needs you")
    elif event == "finished":
        what = "finished"
    else:
        what = "stopped"
    return {
        "FORCE_EVENT": event,
        "FORCE_SESSION": view.name,
        "FORCE_KIND": view.kind,
        "FORCE_HOST": socket.gethostname().split(".")[0],
        "FORCE_STATE": _STATE_WORDS.get(view.state, "unknown"),
        "FORCE_MESSAGE": f"{view.name}: {what}",
        "FORCE_VIEWED": "1" if view.viewed else "0",
    }


def title_sync_ready(screen: str, connected: bool = False) -> bool:
    """Remote Control is connected (`connected`: Claude's status file names its Remote Control session; or the
    screen still shows "remote-control is active", which scrolls away in a long session) and the input line is
    empty (nothing the user is typing gets mixed into the /rename): the last `❯` line holds nothing, or only the
    dim example prompt (an open menu's `❯ 1. …` or a draft is not empty)."""
    if not connected and "remote-control is active" not in screen.lower():
        return False
    prompts = [ln.strip() for ln in screen.splitlines() if ln.strip().startswith("❯")]
    if not prompts:
        return False
    rest = prompts[-1][1:].strip()
    return rest == "" or rest.startswith('Try "')


class Daemon:
    """One instance per process. tick() does one full pass of SPEC §6 steps 1-10 and never raises."""

    def __init__(self, cockpit: Cockpit, clock=None, watch_dir=None, validator=None, reexec=None):
        self.cockpit = cockpit
        self.clock = clock or time.time
        self.cfg: Config = cockpit.cfg
        self.paths: Paths = cockpit.paths
        # live reload (SPEC §6.11). The watch dir is the force package dir (a symlink that deploy.sh
        # atomically re-points); it is deliberately NOT resolved so a symlink swap is seen as a change.
        self._watch_dir = watch_dir if watch_dir is not None else _pkg_dir()
        self._validator = validator if validator is not None else self._default_validate
        self._reexec = reexec if reexec is not None else self._default_reexec
        # reload state
        self._stable_fp = None      # fingerprint we last recorded / acted on
        self._pending_fp = None     # fingerprint currently under debounce
        self._changed_since = None   # clock() when this fingerprint was first seen
        self._acted = set()         # distinct fingerprints already handled (success or failure)
        # sidebar robustness
        self._sidebar_respawn: dict[str, float] = {}
        # turn_done detection
        self._prev_status: dict[str, str | None] = {}
        self._seen_pid: dict[str, int] = {}
        # cockpit-initiated kill marks
        self._cockpit_killed: dict[str, bool] = {}
        self.cockpit.on_cockpit_kill = self._mark
        self._start_update_thread()
        # crash restarts per name (list of clock timestamps)
        self._crash_restarts: dict[str, list[float]] = {}
        # first clock() time a dead pane's exit reason was unknown (status+signal both None)
        self._crash_unknown: dict[str, float] = {}
        # previously notified state per name
        # last written switch-target options
        self._last_switch = None
        self._watchers = remote_mod.Watchers(self.paths)     # one `ssh HOST force watch` per remote (§40)
        self._last_upgrade: float | None = None
        # permission-mode detection (permission badge, SPEC §16)
        self._mode_last: dict[str, float] = {}   # clock() time of the last footer capture per session
        self._mode_pid: dict[str, int] = {}      # pane pid the last capture per session was taken from
        self._mode: dict[str, str] = {}           # last known permission mode per session name
        # share proxy (SPEC §19): last clock() time a (name, user) proxy was (re)started, to throttle
        self._share_start: dict[tuple[str, str], float] = {}
        # session activity cache: session_id -> ((st_mtime_ns, st_size) | None, clock-time, value).
        # Recompute only when the transcript changed AND at least 3 s of clock time has passed (SPEC §15).
        self._activity: dict[str, tuple] = {}
        self._reply: dict[str, int] = {}          # name -> newest reply_ms seen (SPEC §5, unread)
        self._oc_reply: dict[str, tuple] = {}     # OpenCode sessions: name -> (pid, opencode.reply_key) last read
        self._oc_meta: dict[str, object] = {}     # OpenCode sessions: name -> opencode.Meta (SPEC §36)
        self._oc_last: dict[str, tuple] = {}      # name -> (status, meta, clock) of the last answer from its API
        self._oc_src: dict[str, str] = {}         # OpenCode sessions: name -> source clause for `why` (SPEC §43.3)
        self._oc_agent: dict[str, str] = {}       # OpenCode sessions: name -> agent seen on screen
        self._shell_prog: dict[str, tuple] = {}   # shells: name -> (pid, program, since clock, change ms) (§49.3)
        self._readings: dict[str, kinds.Reading] = {}   # name -> this tick's kinds.Reading (§49.1)
        self._claude_name: dict[str, tuple] = {}  # name -> (pid, the name in Claude's status file) last tick
        self._auto_name_last: dict[str, float] = {}   # name -> clock of the last first-prompt look (§49.6)
        # notifications (SPEC §49.4)
        self._views: list = []                    # the last snapshot's sessions
        self._note_state: dict[str, State] = {}   # name -> state at the last notify step
        self._note_down: dict[str, tuple] = {}    # name -> (clock since down, state before) while down
        self._note_last: dict[tuple, float] = {}  # (name, event) -> clock of the last notification
        self._note_procs: list = []               # (Popen, clock started)
        # current tick state
        self._registry = []
        self._skipping = False
        # per-tick per-session info cache: name -> (pane, status, pane_info, version).
        # Built lazily once per tick and cleared whenever a step mutates tmux.
        self._pinfo = None
        self._pinfo_dirty = False

    def _mark(self, name: str) -> None:
        self._cockpit_killed[name] = True

    def _start_update_thread(self) -> None:
        """A background check for a newer Force (SPEC §3): first 2 min after start, then every 24 h, only
        when the checkForUpdates setting is on and a base + running VERSION exist. It never blocks a tick and
        failures are silent; the snapshot picks the result up via <state>/update.json."""
        if not self.cfg.check_updates:
            return
        if updates.download_base() is None or updates.current_version() is None:
            return

        def run() -> None:
            time.sleep(updates._FIRST_DELAY)       # first check 2 min after start, then every 24 h
            while True:
                try:
                    updates.check_daemon(self.paths.state)
                except Exception:
                    pass
                time.sleep(updates._INTERVAL)

        threading.Thread(target=run, daemon=True).start()

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _config_for(self, session_id: str) -> tuple[str | None, str | None, str | None, int | None]:
        """(activity, model, effort, reply_ms) for a live session, cached per transcript path keyed by
        (st_mtime_ns, st_size) and throttled to at most one recompute per 3 s of clock time per session. All
        None when no session_id, no transcript, or the transcript is unreadable."""
        if not session_id:
            return None, None, None, None
        path = claude_mod.find_transcript(self.paths.claude_dir, session_id)
        if path is None:
            return None, None, None, None
        try:
            st = os.stat(path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        cached = self._activity.get(session_id)
        now = self.clock()
        if cached is not None:
            ckey, _when, value = cached
            if ckey == key:
                return value          # transcript unchanged: reuse
            if now - _when < _ACTIVITY_MIN_INTERVAL:
                return value         # changed, but within the 3 s throttle window
        value = activity.scan(path)
        self._activity[session_id] = (key, now, value)
        return value

    def _oc_reply_for(self, name: str, pane, meta) -> int | None:
        """SPEC §5 unread: now when the text of an OpenCode TUI's last assistant message changed since the last
        read of the same TUI; never on its first sight, so a (re)start or a daemon restart marks nothing."""
        key = getattr(meta, "reply", None)
        pid = pane.pid if (pane is not None and not pane.dead and pane.pid) else None
        if key is None or pid is None:
            return None
        prev = self._oc_reply.get(name)
        self._oc_reply[name] = (pid, key)
        changed = prev is not None and prev[0] == pid and prev[1] != key
        return self._now_ms() if changed else None

    def _oc_agent_for(self, name: str, pane) -> str | None:
        """The agent an OpenCode TUI is on, from its screen (SPEC §36), read at most once per _MODE_INTERVAL
        per session like the agent's mode; the last one found while it is off screen (a dialog open)."""
        now = self.clock()
        pid = pane.pid if (pane is not None and not pane.dead and pane.pid) else None
        if pid is None:
            return self._oc_agent.get(name)
        if self._mode_pid.get(name) != pid:
            self._mode_pid[name] = pid
            self._oc_agent.pop(name, None)
        if now - self._mode_last.get(name, float("-inf")) < _MODE_INTERVAL:
            return self._oc_agent.get(name)
        self._mode_last[name] = now
        found = opencode_mod.screen_agent(self.cockpit.tmux.capture_tail(pane.pane_id, 200))
        if found:
            self._oc_agent[name] = found
        return self._oc_agent.get(name)

    def _mode_for(self, name: str, pane) -> str | None:
        """Best-effort permission-mode badge for a live Claude session. Returns the last known mode (None if
        never seen). Captures the agent pane's visible screen at most once per _MODE_INTERVAL of clock time
        per session; a pane restart (pid change) forgets the old mode until a phrase is found again. No live
        agent pane -> the last known mode (shells/archived never set one)."""
        now = self.clock()
        pid = pane.pid if (pane is not None and not pane.dead and pane.pid) else None
        if pid is None:
            return self._mode.get(name)
        if self._mode_pid.get(name) != pid:
            self._mode_pid[name] = pid          # pane restarted: forget the old mode until found again
            self._mode.pop(name, None)
        if now - self._mode_last.get(name, float("-inf")) < _MODE_INTERVAL:
            return self._mode.get(name)
        self._mode_last[name] = now
        found = _parse_mode(self.cockpit.tmux.capture_tail(pane.pane_id, 6))
        if found is not None:
            self._mode[name] = found
        return self._mode.get(name)

    def _load(self):
        with locked(self.paths.home) as reg:
            return list(reg.entries)

    def _live(self):
        # Chuck, the support session (SPEC §50), always last
        return sorted((e for e in self._registry if not e.archived), key=lambda e: (is_support(e), e.order))

    def _info(self, name: str, entry=None):
        pane = self.cockpit.agent_pane(name)
        reading = kinds.Reading()
        if entry is not None:
            reading = kinds.of(entry).read(self, entry, pane)
        self._readings[name] = reading
        status = reading.status
        pane_info = PaneInfo(
            session_exists=self.cockpit.tmux.has_session(name),
            pane_alive=(pane is not None and not pane.dead),
            pane_pid=(pane.pid if pane else None),
            pane_started_ms=(pane.started_ms if pane else None),
            dead_status=(pane.dead_status if pane else None),
            dead_signal=(pane.dead_signal if pane else None),
        )
        return pane, status, pane_info

    def _per_tick(self) -> dict:
        """name -> (pane, status, pane_info, version), built lazily once per tick from the live
        sessions and reused by the upgrade/pending_rename/seen/outputs (and follow) steps, so
        agent_pane/read_status/process_version run at most once per session per tick. Cleared
        whenever a step mutates tmux (via `_pinfo_dirty`)."""
        if self._pinfo_dirty or self._pinfo is None:
            info = {}
            for e in self._live():
                pane, status, pane_info = self._info(e.name, e)
                info[e.name] = (pane, status, pane_info, self._readings[e.name].version)
            self._pinfo = info
            self._pinfo_dirty = False
        return self._pinfo

    def _publish_force_bin(self) -> None:
        launcher = config.launcher_path()
        servers = [self.cockpit.tmux]
        try:
            if self.cockpit.ui_tmux.server_running():
                servers.append(self.cockpit.ui_tmux)
        except Exception:
            pass
        for t in servers:
            try:
                t.run("set-environment", "-g", "FORCE_BIN", launcher)
            except Exception as e:
                _log("force-bin", e)

    def tick(self) -> None:
        with self.cockpit.tmux.cached():
            # --- step 1: tmux server -------------------------------------
            try:
                if not self.cockpit.tmux.server_running():
                    # Under systemd the server must belong to force-tmux.service, never to this
                    # unit's cgroup, or restarting force.service would kill every agent session.
                    if os.environ.get("INVOCATION_ID"):
                        subprocess.run(["systemctl", "--user", "start", "force-tmux.service"],
                                       capture_output=True, timeout=15)
                    if not self.cockpit.tmux.server_running():
                        self.cockpit.tmux.start_server()
                    self._state_published = False
                # tmux.conf reads header files from @force_state; publish ours once per server.
                if not getattr(self, "_state_published", False):
                    self.cockpit.tmux.set_option("@force_state", str(self.paths.state))
                    # the launcher the menus run ($FORCE_BIN, SPEC §2.1), on both servers: tmux.conf sets it for a
                    # new server, but a server started by an older release keeps running through an update
                    self._publish_force_bin()
                    # the release the menus run scripts from: tmux.conf's default is the live install's
                    # (a development instance, SPEC §2.1, runs its own)
                    self.cockpit.tmux.set_option(
                        "@force_app", os.path.abspath(Path(self.paths.tmux_conf).parent.parent))
                    # tier.sh / presize.sh: which terminals get the rich glyph tier (SPEC §28)
                    self.cockpit.tmux.set_option("@force_rich_terms", self.cfg.rich_terminals)
                    # switched.sh signals the ui panes that show a session (split mode, SPEC §39)
                    self.cockpit.tmux.set_option("@force_ui_socket", self.paths.ui_socket)
                    self._retag_legacy()
                    self._respawn_outdated_ui()
                    self._state_published = True
            except Exception as e:
                _log("server", e)

            self._skipping = False
            self._registry = []
            # per-tick info cache is rebuilt each tick (after restore/follow/crash may change panes).
            self._pinfo = None
            self._pinfo_dirty = False
            try:
                self._registry = self._load()
            except RegistryError as e:
                _log("registry", e)
                self._skipping = True

            self._seen = load_seen(self.paths.seen)

            steps = [
                ("restore", self._step_restore, True),
                ("follow", self._step_follow, True),
                ("auto_name", self._step_auto_name, True),
                ("crash", self._step_crash, True),
                ("upgrade", self._step_upgrade, True),
                ("pending_rename", self._step_pending_rename, True),
                ("title_sync", self._step_title_sync, True),
                ("pending_restart", self._step_pending_restart, True),
                ("pending_move", self._step_pending_move, True),
                ("seen", self._step_seen, False),
                ("outputs", self._step_outputs, False),
                ("notify", self._step_notify, False),
                ("shares", self._step_shares, False),
                ("switch", self._step_switch, False),
                ("sidebar", self._step_sidebar, True),
                ("remotes", self._step_remotes, False),
                ("tiers", self._step_tiers, False),
                ("opencode_update", self._step_opencode_update, False),
                ("reload", self._step_reload, False),
            ]
            for name, fn, sc in steps:
                if sc and self._skipping:
                    continue
                try:
                    fn()
                except RegistryError as e:
                    self._skipping = True
                    _log(name, e)
                except Exception as e:
                    _log(name, e)

    # --- step 2 ------------------------------------------------------------
    def _step_restore(self) -> None:
        for entry in self._registry:
            if entry.archived:
                continue
            if not self.cockpit.tmux.has_session(entry.name):
                self.cockpit.create(entry)
                print(f"force: started {entry.name}", file=sys.stderr)

    def _follow_opencode(self, entry, status) -> None:
        """Keep the OpenCode session id the TUI works on in the registry (a new TUI has none until its first
        message; the user may also switch sessions in it), so a restart resumes that one (SPEC §36)."""
        if status is None or not status.session_id or status.session_id == entry.extra.get("oc_session"):
            return
        with locked(self.paths.home) as reg:
            e = reg.get(entry.name)
            if e is not None:
                e.extra["oc_session"] = status.session_id
        print(f"force: {entry.name} follows OpenCode session {status.session_id}", file=sys.stderr)

    def _follow_claude(self, entry, status) -> None:
        """A new conversation id (`/clear`) into the registry; a name the user gave in Claude (`/rename`) is
        adopted: registry, tmux session and seen key, no restart (SPEC §6.3)."""
        if status is None:
            return
        if status.session_id != entry.session_id:
            with locked(self.paths.home) as reg:
                e = reg.get(entry.name)
                if e is not None:
                    e.session_id = status.session_id
        # adopt only a CHANGE of Claude's name (a `/rename` typed there): the name it was started with (`-n`) is
        # older than a rename Force made without a restart (automatic names, §49.6)
        prev = self._claude_name.get(entry.name)
        self._claude_name[entry.name] = (status.pid, status.name)
        if prev is None or prev[0] != status.pid or prev[1] == status.name:
            return
        if entry.pending_name is None and status.name is not None \
                and status.name_source == "user" and status.name != entry.name:
            new = slugify(status.name)
            if not new or not valid_name(new):
                return
            self._rename_now(entry.name, new, extra_pop=("auto_name",))

    def _rename_now(self, old: str, new: str, *, extra_pop=(), title_sync: bool = False) -> bool:
        """Rename a session at once, without a restart: registry (dropping `extra_pop` keys; queueing the phone
        title sync when asked), tmux session, seen key. False when the entry is gone or the name is taken."""
        from .lifecycle import queue_title_sync
        with locked(self.paths.home) as reg:
            e = reg.get(old)
            if e is None or reg.name_taken(new):
                return False
            reg.rename(old, new)
            for k in extra_pop:
                e.extra.pop(k, None)
            if title_sync:
                queue_title_sync(e, new)
            self._pinfo_dirty = True
        if self.cockpit.tmux.has_session(old):
            self.cockpit.tmux.rename_session(old, new)
        _rename_seen_key(self.paths.seen, old, new)
        for cache in (self._readings, self._shell_prog, self._oc_meta, self._oc_last, self._oc_agent,
                      self._claude_name, self._auto_name_last, self._note_state, self._note_down,
                      self._reply, self._oc_reply):
            if old in cache:
                cache[new] = cache.pop(old)
        # the rest of this tick works on the new name: a stale registry would show the old one down for a tick,
        # and the seen step would write back the seen data it loaded before the rename
        self._registry = self._load()
        self._seen = load_seen(self.paths.seen)
        return True

    # --- step 3 ------------------------------------------------------------
    def _step_follow(self) -> None:
        for entry in self._live():
            pane, status, _pi, _ver = self._per_tick()[entry.name]
            if pane is None or pane.dead or not pane.pid:
                continue
            kinds.of(entry).follow(self, entry, self._readings.get(entry.name) or kinds.Reading(status=status))

    # --- step 3b: automatic names (SPEC §49.6) ---------------------------------
    _AUTO_NAME_EVERY = 3.0

    def _step_auto_name(self) -> None:
        now = self.clock()
        for entry in self._live():
            if not entry.extra.get("auto_name") or entry.pending_name:
                continue
            if now - self._auto_name_last.get(entry.name, float("-inf")) < self._AUTO_NAME_EVERY:
                continue
            self._auto_name_last[entry.name] = now
            kind = kinds.of(entry)
            text = kind.first_prompt(self.paths, entry, self._readings.get(entry.name) or kinds.Reading())
            if not text:
                continue
            base = kinds.name_from_prompt(text)
            new = None
            if base is not None:
                with locked(self.paths.home) as reg:
                    new = kinds.free_name(base, lambda n: reg.name_taken(n) and n != entry.name)
            if new is None or new == entry.name:
                with locked(self.paths.home) as reg:
                    e = reg.get(entry.name)
                    if e is not None:
                        e.extra.pop("auto_name", None)
                print(f"force: {entry.name}: keeps its name (nothing to name it after)", file=sys.stderr)
                continue
            if self._rename_now(entry.name, new, extra_pop=("auto_name",), title_sync=kind.name_in_agent):
                print(f"force: named {entry.name} -> {new} after its first prompt", file=sys.stderr)

    # --- step 4 ------------------------------------------------------------
    def _step_crash(self) -> None:
        now = self.clock()
        window = self.cfg.crash_window_minutes * 60.0
        for entry in self._live():
            pane = self.cockpit.agent_pane(entry.name)
            if pane is None:
                continue
            if not pane.dead:
                self._cockpit_killed.pop(entry.name, None)
                self._crash_unknown.pop(entry.name, None)
                continue
            if self._cockpit_killed.pop(entry.name, False) or self.cockpit.recently_killed_by_cockpit(entry.name):
                # stopped by cockpit: this daemon, or a CLI restart/archive in another process
                self._crash_unknown.pop(entry.name, None)
                continue
            if pane.dead_status is None and pane.dead_signal is None:
                # exit reason not reported by tmux yet; wait. If it is still unknown after a
                # short grace period, treat it as a crash (it never exited 0 cleanly).
                first = self._crash_unknown.get(entry.name)
                if first is None:
                    self._crash_unknown[entry.name] = now
                    continue
                if now - first < _CRASH_UNKNOWN_GRACE:
                    continue
                self._crash_unknown.pop(entry.name, None)
            else:
                self._crash_unknown.pop(entry.name, None)
            if self._declined_trust(entry, pane):
                self._delete_if_new(entry)       # a restart would only ask again
                continue
            if pane.dead_status == 0 and not pane.dead_signal:
                continue  # exit status 0 -> leave DOWN
            restarts = self._crash_restarts.setdefault(entry.name, [])
            restarts[:] = [t for t in restarts if now - t < window]
            if len(restarts) < self.cfg.crash_restart_max:
                restarts.append(now)
                self.cockpit.restart(entry.name)
                self._pinfo_dirty = True
                print(f"force: restarted {entry.name} after crash (status={pane.dead_status} signal={pane.dead_signal})", file=sys.stderr)
            else:
                print(f"force: {entry.name} crash-restart limit reached; leaving DOWN", file=sys.stderr)

    # --- step 5 ------------------------------------------------------------
    def _step_upgrade(self) -> None:
        if not self.cfg.auto_upgrade:
            return
        now = self.clock()
        if self._last_upgrade is not None and now - self._last_upgrade < _UPGRADE_INTERVAL:
            return
        self._last_upgrade = now
        now_ms = int(now * 1000)
        seen = self._seen
        clients = self.cockpit.tmux.clients()
        latest: dict[str, str | None] = {}
        for entry in self._live():
            kind = kinds.of(entry)
            _pane, status, pane_info, pv = self._per_tick()[entry.name]
            if status is None or not pv:
                continue      # shells (no version) and agents without an answer
            st = compute_state(entry, pane_info, status, seen.get(entry.name, Seen()), now_ms, self.cfg)
            if st not in (State.IDLE, State.YOUR_TURN, State.STALE):
                continue
            if kind.name not in latest:
                latest[kind.name] = kind.latest_version(self.paths)
            lv = latest[kind.name]
            if not (pv and lv and pv != lv):
                continue
            if now_ms - status.status_updated_ms < int(self.cfg.upgrade_idle_minutes * 60_000):
                continue
            if any(c.session == entry.name for c in clients):
                continue
            self.cockpit.restart(entry.name)
            self._pinfo_dirty = True
            print(f"force: upgraded {entry.name} {pv} -> {lv}", file=sys.stderr)

    # --- step 6 ------------------------------------------------------------
    def _step_pending_rename(self) -> None:
        seen = self._seen
        now_ms = self._now_ms()
        for entry in self._live():
            if not entry.pending_name:
                continue
            _pane, status, pane_info, _ver = self._per_tick()[entry.name]
            st = compute_state(entry, pane_info, status, seen.get(entry.name, Seen()), now_ms, self.cfg)
            if st in (State.WORKING, State.NEEDS_YOU):
                continue
            new = self.cockpit.apply_rename(entry.name)
            self._pinfo_dirty = True
            print(f"force: renamed {entry.name} -> {new}", file=sys.stderr)

    def _step_title_sync(self) -> None:
        """A renamed (or imported) Claude session: type `/rename NAME` once it is idle, connected to Remote
        Control and its input line is empty, so the phone shows the new name too (SPEC §8). Given up after 30 min
        only when Remote Control never connected; a draft or an open menu is waited out."""
        seen = self._seen
        now_ms = self._now_ms()
        for entry in self._live():
            name = entry.extra.get("title_sync")
            if not name or not is_claude(entry):
                continue
            pane, status, pane_info, _ver = self._per_tick()[entry.name]
            if pane is None or entry.pending_name:
                continue
            connected = self._rc_connected(pane_info)
            if not connected and time.time() - float(entry.extra.get("title_sync_at") or 0) > 1800:
                # Remote Control never connected (switched off, logged out): nothing to rename there. A draft or an
                # open menu, on the other hand, is waited out however long it stays
                self._clear_title_sync(entry.name, "gave up (Remote Control not connected within 30 min)")
                continue
            st = compute_state(entry, pane_info, status, seen.get(entry.name, Seen()), now_ms, self.cfg)
            if st not in (State.IDLE, State.YOUR_TURN, State.STALE):
                continue
            try:
                screen = self.cockpit.tmux.capture_tail(pane.pane_id, 60)
            except Exception:
                continue
            if not title_sync_ready(screen, connected):
                continue
            self.cockpit.tmux.run("send-keys", "-t", pane.pane_id, "-l", f"/rename {name}")
            time.sleep(0.3)
            self.cockpit.tmux.run("send-keys", "-t", pane.pane_id, "Enter")
            self._clear_title_sync(entry.name, f"/rename {name} sent")

    def _rc_connected(self, pane_info) -> bool:
        """Claude's status file (sessions/<pid>.json) names its Remote Control session: it is connected."""
        pid = getattr(pane_info, "pane_pid", None)
        if not pid:
            return False
        try:
            data = json.loads((self.paths.claude_dir / "sessions" / f"{pid}.json").read_text())
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and bool(data.get("bridgeSessionId"))

    def _clear_title_sync(self, name: str, why: str) -> None:
        with locked(self.paths.home) as reg:
            e = reg.get(name)
            if e is not None:
                e.extra.pop("title_sync", None)
                e.extra.pop("title_sync_at", None)
        print(f"force: {name}: phone title: {why}", file=sys.stderr)

    def _smoke_status(self) -> dict | None:
        """{"ok", "at"} (plus "reason" when it failed) of the last `force smoke` run (SPEC §37), None when
        there was none."""
        from .smoke import read_last, reason
        last = read_last(self.paths.state / "smoke")
        if not isinstance(last, dict):
            return None
        if last.get("ok"):
            return {"ok": True, "at": last.get("at")}
        why = last.get("reason") or reason(last.get("checks"))
        return {"ok": False, "at": last.get("at"), "reason": why or "self-test failed"}

    def _asks_trust(self, pane_id: str) -> bool:
        """Claude's "trust this folder?" question is on screen (it writes no status file until answered, so
        the card would otherwise go from starting to down; a new session or a move to a new folder)."""
        try:
            screen = self.cockpit.tmux.capture_tail(pane_id, 20).lower()
        except Exception:
            return False
        return any(m in screen for m in TRUST_MARKERS)

    def _declined_trust(self, entry, pane) -> bool:
        """SPEC §6.4: a dead Claude pane that still shows the trust question was answered "No, exit" or Esc. A pane
        that wrote a status file got past the question: the phrase on its screen is conversation text."""
        return (is_claude(entry) and pane.dead and self._seen_pid.get(entry.name) != pane.pid
                and self._asks_trust(pane.pane_id))

    def _delete_if_new(self, entry) -> None:
        """SPEC §6.4, trust declined: delete a session that has no conversation yet (just created in that folder);
        any other (moved there, Chuck) stays DOWN."""
        if is_support(entry) or claude_mod.find_transcript(self.paths.claude_dir, entry.session_id) is not None:
            return
        try:
            self.cockpit.delete(entry.name)
            print(f"force: deleted {entry.name}: its folder was not trusted", file=sys.stderr)
        except Exception as ex:
            print(f"force: deleting {entry.name} (folder not trusted) failed: {ex}", file=sys.stderr)
        self._registry = self._load()
        self._pinfo_dirty = True

    def _step_pending_move(self) -> None:
        """`force move NAME DIR` on a busy session marks it; move it once it is no longer busy (§7.7)."""
        seen = self._seen
        now_ms = self._now_ms()
        for entry in self._live():
            target = entry.extra.get("pending_move")
            if not target:
                continue
            _pane, status, pane_info, _ver = self._per_tick()[entry.name]
            st = compute_state(entry, pane_info, status, seen.get(entry.name, Seen()), now_ms, self.cfg)
            if st in (State.WORKING, State.NEEDS_YOU, State.STARTING):
                continue
            with locked(self.paths.home) as reg:
                e = reg.get(entry.name)
                if e is not None:
                    e.extra.pop("pending_move", None)
            try:
                self.cockpit.move(entry.name, target)
                print(f"force: moved {entry.name} to {target} (pending move)", file=sys.stderr)
            except Exception as ex:
                print(f"force: pending move of {entry.name} failed: {ex}", file=sys.stderr)
            self._pinfo_dirty = True

    def _step_pending_restart(self) -> None:
        """`force restart --all` marks busy sessions; restart each once it is no longer busy."""
        seen = self._seen
        now_ms = self._now_ms()
        due = []
        for entry in self._live():
            if not entry.extra.get("pending_restart"):
                continue
            _pane, status, pane_info, _ver = self._per_tick()[entry.name]
            st = compute_state(entry, pane_info, status, seen.get(entry.name, Seen()), now_ms, self.cfg)
            if st in (State.WORKING, State.NEEDS_YOU, State.STARTING):
                continue
            with locked(self.paths.home) as reg:
                e = reg.get(entry.name)
                if e is not None:
                    e.extra.pop("pending_restart", None)
            due.append(entry.name)
        if not due:
            return
        failed = self.cockpit.restart_many(due)
        self._pinfo_dirty = True
        for name in due:
            if name not in failed:
                print(f"force: restarted {name} (pending restart)", file=sys.stderr)
            else:
                print(f"force: pending restart of {name} failed", file=sys.stderr)

    # --- Force-managed OpenCode (SPEC §36) ----------------------------------
    _OC_UPDATE_EVERY = 6 * 3600.0

    def _step_opencode_update(self) -> None:
        """With opencode_managed and auto_upgrade: run `force opencode-update` in the background at start and
        every 6 h (it downloads ~60 MB, so never inside the tick). Sessions then move to the new version via
        the upgrade step, as the other agent sessions do."""
        if not (self.cfg.opencode_managed and self.cfg.auto_upgrade):
            return
        now = time.monotonic()
        last = getattr(self, "_oc_update_at", None)
        if last is not None and now - last < self._OC_UPDATE_EVERY:
            return
        proc = getattr(self, "_oc_update_proc", None)
        if proc is not None and proc.poll() is None:
            return                                  # still running
        self._oc_update_at = now
        try:
            log = open(self.paths.state / "opencode-update.log", "a")
            self._oc_update_proc = subprocess.Popen([self.cockpit.cockpit_bin, "opencode-update"], stdin=subprocess.DEVNULL,
                                                    stdout=log, stderr=log, start_new_session=True)
            log.close()
        except OSError as e:
            _log("opencode_update", e)

    # --- glyph tiers (SPEC §28) ---------------------------------------------
    _TIER_EVERY = 5.0

    def _step_tiers(self) -> None:
        """Re-run tier.sh for every session a client shows, every few seconds. The hooks run it on attach
        and switches only, so a client attached before `rich_terminals` (or its terminal) changed kept the
        old tier until the next switch (plain glyphs in Tilix after it was opted in). tier.sh signals the
        panes only when the tier really changes."""
        now = time.monotonic()
        if now - getattr(self, "_tiers_at", 0.0) < self._TIER_EVERY:
            return
        self._tiers_at = now
        script = str(Path(__file__).resolve().parent.parent / "share" / "tier.sh")
        # split mode: the ui server's clients are the real terminals (a terminal's type, e.g. VTE, is only
        # known a moment after it attached, so the attach hook alone can leave it plain)
        for tmux in self._servers():
            try:
                sessions = sorted({c.session for c in tmux.clients() if c.session})
                if not sessions:
                    continue
                sock = subprocess.run(tmux._cmd(["display", "-p", "#{socket_path}"]),
                                      capture_output=True, text=True, timeout=5).stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                continue
            if not sock:
                continue
            for name in sessions:
                try:
                    subprocess.run(["sh", script, sock, name, "--signal"], capture_output=True, timeout=5)
                except (subprocess.TimeoutExpired, OSError) as e:
                    _log("tiers", e)

    # --- step 7 ------------------------------------------------------------
    def _step_seen(self) -> None:
        if self._skipping:
            return
        seen = self._seen
        now = self.clock()
        now_ms = int(now * 1000)
        clients = self.cockpit.tmux.clients()
        viewed = {c.session for c in clients}
        names = {e.name for e in self._registry}
        for entry in self._registry:
            name = entry.name
            if entry.archived:
                continue
            _pane, status, _pi, _ver = self._per_tick()[name]
            if self.cockpit.tmux.has_session(name) and name in viewed:
                seen.setdefault(name, Seen()).last_seen_ms = now_ms
            if status is not None:
                first = self._seen_pid.get(name) != status.pid
                self._seen_pid[name] = status.pid
                prev = self._prev_status.get(name)
                self._prev_status[name] = status.status
                if status.status == "idle" and prev in ("busy", "waiting") and not first:
                    seen.setdefault(name, Seen()).turn_done_ms = now_ms
            # (no toasts: the header pills already show needs-you / finished sessions; owner 2026-10-13)
        for n in list(seen):
            if n not in names:
                seen.pop(n)
        save_seen(self.paths.seen, seen)

    # --- step 8 ------------------------------------------------------------
    def _step_outputs(self) -> None:
        if self._skipping:
            return
        seen = self._seen
        now = self.clock()
        now_ms = int(now * 1000)
        clients = self.cockpit.tmux.clients()
        viewed = {c.session for c in clients}
        lv = claude_mod.latest_version(self.paths.claude_bin)
        live = self._live()
        # the list's numbers (Alt+N, jump): Chuck (SPEC §50) is not in the list and has none
        index = {e.name: i + 1 for i, e in enumerate(e for e in live if not is_support(e))}
        latest_by_kind: dict[str, str | None] = {}
        views = []
        live_names = {e.name for e in live}
        for rname in report_mod.names(self.paths):
            if rname not in live_names:
                report_mod.clear(self.paths, rname)       # orphaned report (SPEC §43.2)
        host = socket.gethostname().split(".")[0]
        for entry in self._registry:
            name = entry.name
            if entry.archived:
                views.append(SessionView(
                    name=name, index=None, state=State.ARCHIVED, since_ms=None,
                    version=None, outdated=False, archived=True, order=entry.order,
                    cwd=entry.cwd, session_id=entry.session_id, pid=None,
                    waiting_for=None, viewed=False))
                continue
            _pane, status, pi, pv = self._per_tick()[name]
            kind = kinds.of(entry)
            reading = self._readings.get(name) or kinds.Reading(status=status, version=pv)
            # outdated: the running version against the installed binary of the session's own kind
            if kind.name not in latest_by_kind:
                latest_by_kind[kind.name] = lv if kind.name == "claude" else kind.latest_version(self.paths)
            latest = latest_by_kind[kind.name]
            outdated = bool(pv and latest and pv != latest)
            since = status.status_updated_ms if status else pi.pane_started_ms
            card = kind.card(self, entry, _pane, reading)
            act, model, effort, mode = card.activity, card.model, card.effort, card.mode
            state, why0 = compute_state_why(entry, pi, status, seen.get(name, Seen()), now_ms, self.cfg)
            clauses = [reading.source] if reading.source else []
            clauses.append(why0)
            if state in (State.STARTING, State.DOWN) and is_claude(entry) and _pane is not None \
                    and pi.pane_alive and self._asks_trust(_pane.pane_id):
                state, act = State.NEEDS_YOU, TRUST_ACTIVITY
                clauses.append("screen shows the trust-folder question")
            elif state is State.DOWN and _pane is not None and self._declined_trust(entry, _pane):
                act = TRUST_DECLINED_ACTIVITY
                clauses.append("the trust-folder question was declined")
            title = clean_title(_pane.title if _pane else None, name, host) or card.title
            # reported state (SPEC §43.2)
            rep = report_mod.read(self.paths, name)
            if rep is None:
                if report_mod.path_for(self.paths, name).exists():
                    report_mod.clear(self.paths, name)     # unreadable file
            else:
                if rep.state == "your-turn":
                    valid = not (seen.get(name, Seen()).last_seen_ms > rep.at_ms)
                else:
                    valid = rep.pid == 0 or report_mod.pid_alive(rep.pid)
                if not valid:
                    report_mod.clear(self.paths, name)
                else:
                    applied = False
                    if is_shell(entry):
                        # a report wins over what the shell's foreground program says (SPEC §49.3)
                        if state in (State.SHELL, State.WORKING, State.YOUR_TURN):
                            state = {"working": State.WORKING, "needs-you": State.NEEDS_YOU,
                                     "your-turn": State.YOUR_TURN}[rep.state]
                            act, since = rep.summary, rep.at_ms
                            applied = True
                    else:
                        if act is None:
                            act = rep.summary
                        applied = True
                    if applied:
                        ago = max(0, now_ms - rep.at_ms) // 1000
                        clauses.append(f"reported by pid {rep.pid} {ago} s ago: {rep.state}" if rep.pid
                                       else f"reported {ago} s ago: {rep.state}")
            if entry.extra.get("pending_restart"):
                act = pending_restart_activity(state) or act
            if entry.extra.get("pending_move") and state == State.WORKING:
                act = "moves after this turn"
            if card.reply_ms is not None and card.reply_ms > self._reply.get(name, 0):
                self._reply[name] = card.reply_ms
            is_unread = unread(state, self._reply.get(name), seen.get(name, Seen()), name in viewed)
            if is_unread:
                clauses.append(f"unread: wrote you {max(0, now_ms - self._reply[name]) // 1000} s ago, "
                               "not viewed since")
            note = None
            mark = None if is_shell(entry) else self.cockpit.restarted(name)
            if mark is not None:
                # wall clock, like the kill marker
                up_for = time.time() - pi.pane_started_ms / 1000 if pi.pane_started_ms else None
                note = restart_note(state, mark[0], bool(mark[1]) and pi.pane_pid == mark[1], up_for)
            if entry.extra.get("archiving"):
                note, act = "archiving", None      # being archived: its stop is not a restart
            elif entry.extra.get("moving"):
                note, act = "moving", None
            elif note is not None and time.time() - float(entry.extra.get("moved_at") or 0) < 60:
                note = "moving" if note == "restarting" else "moved"    # the restart that a move does
            elif note is None and entry.pending_name:
                note = f"→ {entry.pending_name} when idle"     # a rename waits for the agent to be idle (§6.6)
            views.append(SessionView(
                name=name, index=index.get(name), state=state,
                since_ms=since, version=pv, outdated=outdated, archived=False,
                order=entry.order, cwd=entry.cwd, session_id=entry.session_id,
                pid=pi.pane_pid, waiting_for=(status.waiting_for if status else None),
                viewed=name in viewed,
                activity=act,
                mode=mode, model=model, effort=effort,
                shared=(entry.extra.get("shares") or None) if self.cfg.sharing else None,
                note=note, kind=kind_of(entry), why="; ".join(clauses), title=title,
                support=is_support(entry), unread=is_unread))
        self._views = views
        limits = self._limits(live)
        snap = Snapshot(generated_ms=now_ms, latest_version=lv, sessions=views,
                        usage=({"five_hour": limits[0].get("five_hour"), "seven_day": limits[0].get("seven_day")}
                               if limits else None),
                        limits=limits,
                        smoke=self._smoke_status(),
                        update=updates.snapshot_update(self.paths.state))
        write_snapshot(self.paths.snapshot, snap)
        # The header is a pane (`force header`) reading the snapshot; no status-line files any more.

    # --- step 8a: notifications (SPEC §49.4) -----------------------------------
    _NOTE_EVERY = 30.0        # at most one event per session and kind of event this often
    _NOTE_DOWN_AFTER = 10.0   # a session must stay down this long (a crash restart brings it back sooner)
    _NOTE_TIMEOUT = 30.0      # the command is killed after this long

    def _step_notify(self) -> None:
        now = self.clock()
        self._reap_notify(now)
        cmd = (self.cfg.notify_command or "").strip()
        up = (State.WORKING, State.NEEDS_YOU, State.YOUR_TURN, State.IDLE, State.STALE, State.SHELL)
        names = set()
        for v in self._views:
            if v.archived:
                continue
            names.add(v.name)
            prev = self._note_state.get(v.name)
            self._note_state[v.name] = v.state
            event = None
            if v.state is State.DOWN:
                if prev in up and v.note is None:
                    self._note_down[v.name] = (now, prev)
                down = self._note_down.get(v.name)
                if down is not None and now - down[0] >= self._NOTE_DOWN_AFTER:
                    self._note_down.pop(v.name, None)
                    if not self.cockpit.recently_killed_by_cockpit(v.name, within=120.0):
                        event = "down"
            else:
                self._note_down.pop(v.name, None)
                if prev is None or prev == v.state:
                    continue            # first sight after a start, or no change
                if v.state is State.NEEDS_YOU:
                    event = "needs-you"
                elif v.state is State.YOUR_TURN and prev in (State.WORKING, State.NEEDS_YOU):
                    event = "finished"
            if event is None or not cmd:
                continue
            if v.viewed and not self.cfg.notify_when_viewed:
                continue
            last = self._note_last.get((v.name, event))
            if last is not None and now - last < self._NOTE_EVERY:
                continue
            self._note_last[(v.name, event)] = now
            self._run_notify(cmd, notify_env(v, event))
        for n in list(self._note_state):
            if n not in names:
                self._note_state.pop(n, None)
                self._note_down.pop(n, None)

    def _run_notify(self, cmd: str, env: dict[str, str]) -> None:
        try:
            p = subprocess.Popen(["sh", "-c", cmd], env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        except OSError as e:
            _log("notify", e)
            return
        print(f"force: notify {env['FORCE_EVENT']} {env['FORCE_SESSION']}", file=sys.stderr)
        self._note_procs.append((p, self.clock()))

    def _reap_notify(self, now: float) -> None:
        keep = []
        for p, started in self._note_procs:
            if p.poll() is not None:
                if p.returncode:
                    print(f"force: notify command exited {p.returncode}", file=sys.stderr)
                continue
            if now - started > self._NOTE_TIMEOUT:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                print("force: notify command killed after 30 s", file=sys.stderr)
                continue
            keep.append((p, started))
        self._note_procs = keep

    def _limits(self, live) -> list[dict]:
        """The dock's plan limits (SPEC §49.2): one block per kind that has limits and a live session here."""
        out = []
        present = {kinds.of(e).name for e in live}
        for kind in kinds.ALL.values():
            if kind.name not in present:
                continue
            lim = kind.limits(self.paths)
            if lim:
                out.append({"kind": kind.name, "label": getattr(kind, "limits_label", kind.label), **lim})
        return out

    # --- step 8b: session sharing (SPEC §19) --------------------------------
    def _step_shares(self) -> None:
        """Reconcile the `force share-proxy` processes with the registry's `extra.shares`. Every tick:
        desired = {(name, user, mode)} for every LIVE entry's shares; each is given a live proxy (pidfile
        in $FORCE_STATE/shares/<name>.<user>.json) with the right mode, proxies that are no longer desired
        are stopped, and the guest client is installed when at least one share is desired."""
        if self._skipping:
            return
        now = self.clock()
        desired: dict[tuple[str, str], str] = {}
        for entry in self._live() if self.cfg.sharing else ():   # disabled: stop every proxy
            shares = entry.extra.get("shares")
            if not isinstance(shares, dict):
                continue
            for user, mode in shares.items():
                if mode in ("r", "w"):
                    desired[(entry.name, user)] = mode
        shares_dir = self.paths.state / "shares"
        tracked: dict[tuple[str, str], tuple[int, str]] = {}
        try:
            shares_dir.mkdir(parents=True, exist_ok=True)
            for f in shares_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                pid, mode = data.get("pid"), data.get("mode")
                if not isinstance(pid, int) or mode not in ("r", "w"):
                    continue
                key = _share_key(f.stem)
                if key is not None:
                    tracked[key] = (pid, mode)
        except OSError as e:
            _log("shares", e)
            return

        # stop proxies that are no longer desired (unshared, archived/renamed/forgotten)
        for (name, user), (pid, _mode) in list(tracked.items()):
            if (name, user) in desired:
                continue
            _stop(pid)
            _delete_share_json(shares_dir, name, user)
            print(f"force: stopped sharing {name} with {user}", file=sys.stderr)

        # start or restart each desired share
        for (name, user), mode in desired.items():
            live_pid, live_mode = tracked.get((name, user), (None, None))
            if live_pid is not None and _pid_is_live_proxy(live_pid) and live_mode == mode:
                continue                       # already running with the right mode
            last = self._share_start.get((name, user))
            if last is not None and now - last < _SHARE_THROTTLE:
                continue                       # throttled: at most one start per 10 s of clock time
            self._share_start[(name, user)] = now
            if live_pid is not None and _pid_is_live_proxy(live_pid):
                _stop(live_pid)
            try:
                proc = subprocess.Popen(
                    [self.cockpit.cockpit_bin, "share-proxy", name, user, mode],
                    start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                _log("shares", e)
                continue
            _write_share_json(shares_dir, name, user, proc.pid, mode)
            print(f"force: sharing {name} with {user} ({mode})", file=sys.stderr)

        # install the guest client when at least one share is desired
        if desired:
            try:
                share_mod.ensure_share_dir()
            except PermissionError as e:
                _log("shares", e)
                return
            _install_join(self.paths.tmux_conf)

    # --- step 9 ------------------------------------------------------------
    def _step_switch(self) -> None:
        if self._skipping:
            return
        live = [e for e in self._live() if not is_support(e)]     # Alt+N, next, previous: not Chuck (SPEC §50)
        idx = {}
        for i in range(1, 10):
            idx[f"@force_idx_{i}"] = live[i - 1].name if (i - 1) < len(live) else None
        per = {}
        n = len(live)
        for i, e in enumerate(live):
            per[e.name] = (
                live[(i + 1) % n].name if n > 1 else None,
                live[(i - 1) % n].name if n > 1 else None,
            )
        key = (tuple(sorted(idx.items())), tuple(per.items()))
        if key == self._last_switch:
            return
        self._last_switch = key
        for t in self._servers():
            for k, v in idx.items():
                if v:
                    t.set_option(k, v)
                else:
                    t.unset_option(k)
        for sname, (nxt, prv) in per.items():
            if not self.cockpit.tmux.has_session(sname):
                continue
            if nxt:
                self.cockpit.tmux.set_option("@force_next", nxt, target=sname)
            else:
                self.cockpit.tmux.unset_option("@force_next", target=sname)
            if prv:
                self.cockpit.tmux.set_option("@force_prev", prv, target=sname)
            else:
                self.cockpit.tmux.unset_option("@force_prev", target=sname)

    # --- step 10 -----------------------------------------------------------
    def _step_sidebar(self) -> None:
        if self._skipping:
            return
        if self.cockpit.split:
            self._split_layout()
            return
        # Robustness (always on, independent of dev_reload): a dead sidebar or header pane is
        # respawned, at most once per 10 s of clock time per session and role. Agent panes are
        # never touched here (crash handling is the only path that may respawn them).
        now = self.clock()
        enabled = self.cockpit.sidebar_enabled()
        for p in self.cockpit.tmux.panes():
            if p.role not in ("sidebar", "header") or not p.dead:
                continue
            if p.role == "sidebar" and not enabled:
                continue
            key = f"{p.session}/{p.role}"
            last = self._sidebar_respawn.get(key)
            if last is not None and now - last < _SIDEBAR_RESPAN_INTERVAL:
                continue
            self._sidebar_respawn[key] = now
            self.cockpit.tmux.respawn_pane(p.pane_id, self.cockpit.role_command(p.role))
            print(f"force: respawned {p.role} {p.session}", file=sys.stderr)
        for e in self._live():
            if self.cockpit.ensure_header(e.name):
                print(f"force: added header pane to {e.name}", file=sys.stderr)
            if enabled and self.cockpit.ensure_sidebar(e.name):
                print(f"force: added sidebar pane to {e.name}", file=sys.stderr)
        removed = self.cockpit.dedupe_sidebars()
        if removed:
            print(f"force: removed {removed} duplicate sidebar/header pane(s)", file=sys.stderr)

    def _servers(self) -> list:
        """The host tmux, plus the ui tmux when it runs (split mode)."""
        out = [self.cockpit.tmux]
        try:
            if self.cockpit.ui_tmux.server_running():
                out.append(self.cockpit.ui_tmux)
        except Exception:
            pass
        return out

    def _retag_legacy(self) -> None:
        """Start/reload fix-ups of live tmux servers: re-tag panes that still carry the pre-rebrand role
        "claude" as "agent" (one list-panes + one set-option per pane; errors ignored), and the ui server's
        @force_app."""
        for t in self._servers():
            rc, out, _err = t._run_rc(["list-panes", "-a", "-F", "#{pane_id} #{@force_role}"])
            if rc != 0:
                continue
            for line in (out.splitlines() if out else []):
                pid, _, role = line.partition(" ")
                if role == LEGACY_AGENT_ROLE:
                    t._run_rc(["set-option", "-p", "-t", pid, "@force_role", "agent"])
        # a ui server started by an older release may hold @force_app as a resolved release path (its bindings
        # then kept running that release's scripts): point it at the app path the host server uses
        ui = self.cockpit.ui_tmux
        app = os.path.abspath(Path(self.paths.tmux_conf).parent.parent)
        if ui.server_running() and ui.get_option("@force_app") not in (None, "", app):
            ui._run_rc(["set-option", "-g", "@force_app", app])

    def _respawn_outdated_ui(self) -> None:
        """Start fix-up: respawn sidebar/header panes whose program belongs to another release. An upgrade that
        restarts the daemon (install.sh: `force install-to`, `force remote upgrade`) is never seen by the live
        reload (the new daemon starts from the new fingerprint), so open windows kept the old sidebar and header.
        Only the Rust programs are judged (/proc/<pid>/exe); a pane still starting (the launcher) is left alone."""
        current = os.path.realpath(Path(self.paths.tmux_conf).parent.parent)
        for t in self._servers():
            for p in t.panes():
                if p.role not in ("sidebar", "header") or p.dead or not p.pid:
                    continue
                try:
                    exe = os.readlink(f"/proc/{p.pid}/exe").removesuffix(" (deleted)")
                except OSError:
                    continue
                if os.path.basename(exe) not in ("force-sidebar", "force-header"):
                    continue
                if os.path.commonpath([exe, current]) == current:
                    continue
                try:
                    t.run("respawn-pane", "-k", "-t", p.pane_id, self.cockpit.role_command(p.role))
                    print(f"force: respawned {p.role} {p.session} (older release)", file=sys.stderr)
                except Exception as e:
                    _log("respawn", e)

    def _split_layout(self) -> None:
        """Split mode (SPEC §39): host sessions hold only their main pane (sidebar/header panes and stash
        windows from classic mode are removed); dead sidebar/header panes in the ui server are respawned."""
        host = self.cockpit.tmux
        removed = 0
        for p in host.panes():
            if p.role in ("sidebar", "header"):
                try:
                    host.kill_pane(p.pane_id)
                    removed += 1
                except TmuxError:
                    pass
        rc, out, _ = host._run_rc(["list-windows", "-a", "-F", "#{window_id} #{window_name}"])
        for line in out.splitlines() if rc == 0 else []:
            wid, _, wname = line.partition(" ")
            if wname == "force-stash":
                host._run_rc(["kill-window", "-t", wid])
                removed += 1
        if removed:
            print(f"force: split mode: removed {removed} sidebar/header pane(s) from host sessions", file=sys.stderr)
        ui = self.cockpit.ui_tmux
        if not ui.server_running():
            return
        now = self.clock()
        for p in ui.panes():
            if p.role not in ("sidebar", "header") or not p.dead:
                continue
            key = f"ui:{p.session}/{p.role}"
            last = self._sidebar_respawn.get(key)
            if last is not None and now - last < _SIDEBAR_RESPAN_INTERVAL:
                continue
            self._sidebar_respawn[key] = now
            ui.respawn_pane(p.pane_id, self.cockpit.role_command(p.role))
            print(f"force: respawned ui {p.role} {p.session}", file=sys.stderr)
        # A ui session belongs to one terminal and goes with it (destroy-unattached, set when its client
        # attaches). One whose terminal closed before that (during the intro) would stay forever: ended
        # once it has been without a client for a minute.
        rc, out, _ = ui._run_rc(["list-sessions", "-F", "#{session_name} #{session_attached} #{session_activity}"])
        for line in out.splitlines() if rc == 0 else []:
            name, att, last = (line.rsplit(" ", 2) + ["", ""])[:3]
            if att == "0" and last.isdigit() and time.time() - int(last) > _UI_ORPHAN_SECS:
                ui._run_rc(["kill-session", "-t", "=" + name])
                print(f"force: ended ui session {name} (no terminal)", file=sys.stderr)

    # --- step 11: live reload (SPEC §6.11) --------------------------------
    def _fingerprint(self):
        """Snapshot of the watched files: every *.py directly in the watch dir plus tmux.conf.
        Stats go through the unresolved (possibly symlinked) paths so a deployment symlink swap is seen.
        Returns a sorted tuple so it can be hashed and compared."""
        # The release the symlink points at: deploy.sh copies files with `cp -a` (mtimes preserved),
        # so a swap or rollback is only guaranteed to show up through the target path itself.
        items = [("py", "<target>", os.path.realpath(self._watch_dir), 0)]
        try:
            for entry in os.scandir(self._watch_dir):
                if entry.name.endswith(".py"):
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    items.append(("py", entry.name, st.st_mtime_ns, st.st_size))
        except OSError:
            items.append(("py", "<missing dir>", 0, 0))
        for conf in (self.paths.tmux_conf, self.paths.ui_conf):
            try:
                st = os.stat(conf)
                items.append(("conf", os.fspath(conf), st.st_mtime_ns, st.st_size))
            except OSError:
                pass
        # settings.json (SPEC §6.11): read only at start, so a change re-execs the daemon and respawns the UI
        # panes (their flags come from it); a missing file counts too, so creating it is seen
        try:
            st = os.stat(self.paths.config_file)
            items.append(("cfg", os.fspath(self.paths.config_file), st.st_mtime_ns, st.st_size))
        except OSError:
            items.append(("cfg", "<none>", 0, 0))
        return tuple(sorted(items))

    def _step_reload(self) -> None:
        if not self.cfg.dev_reload:
            return
        now = self.clock()
        fp = self._fingerprint()
        if self._stable_fp is None:
            self._stable_fp = fp
            self._pending_fp = None
            return
        if fp == self._stable_fp:
            self._pending_fp = None
            return
        if self._pending_fp != fp:
            self._pending_fp = fp
            self._changed_since = now
            return
        if now - self._changed_since < _RELOAD_DEBOUNCE:
            return
        if fp in self._acted:
            # a broken state we already validated and skipped: do not re-validate until it changes again
            self._stable_fp = fp
            self._pending_fp = None
            return
        self._apply_reload(fp)

    def _message_all(self, text: str, duration_ms: int) -> None:
        for c in self.cockpit.tmux.clients():
            try:
                self.cockpit.tmux.display_message(c.name, text, duration_ms)
            except Exception as e:
                _log("reload", e)

    def _apply_reload(self, fp) -> None:
        diff = set(self._stable_fp) ^ set(fp)
        changed_py = any(t[0] == "py" for t in diff)
        changed_conf = any(t[0] == "conf" for t in diff)
        changed_cfg = any(t[0] == "cfg" for t in diff)    # load_config tolerates anything: no validation
        err = self._validator(changed_py, changed_conf)
        self._stable_fp = fp
        self._pending_fp = None
        self._acted.add(fp)
        if err:
            _log("reload", f"skipped: {err}")
            self._message_all(f"force: reload skipped — {err}"[:120], 8000)
            return
        if changed_conf:
            # tmux runs the whole file even when one command in it fails (and then exits 1): log that and go on,
            # so the ui config and the re-exec below still happen
            servers = [(self.cockpit.tmux, self.paths.tmux_conf)]
            if self.cockpit.ui_tmux.server_running() and self.paths.ui_conf.exists():
                servers.append((self.cockpit.ui_tmux, self.paths.ui_conf))
            for t, conf in servers:
                rc, _out, err = t._run_rc(["source-file", str(conf)])
                if rc != 0:
                    _log("reload", f"{conf.name}: {err.strip()[:200]}")
        self._retag_legacy()
        if changed_py or changed_cfg:
            # Order matters: respawn the sidebars and announce first, re-exec LAST so the new process
            # takes over with everything already in place. NEVER respawn a role other than 'sidebar'.
            for t in self._servers():
                for p in t.panes():
                    if p.role in ("sidebar", "header"):   # never any other role (agent!)
                        t.run("respawn-pane", "-k", "-t", p.pane_id, self.cockpit.role_command(p.role))
            what = "code" if changed_py else "config"
            print(f"force: {what} changed, sidebars respawned, re-executing daemon", file=sys.stderr)
            self._reexec()
        else:
            print("force: tmux.conf reloaded", file=sys.stderr)

    def _default_validate(self, changed_py: bool, changed_conf: bool):
        """Default reload validator: imports every package module the daemon lives in, and parses tmux.conf."""
        if changed_py:
            cmd = [sys.executable, "-c", _RELOAD_IMPORT]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                  cwd=str(self._watch_dir.parent))
            except (subprocess.TimeoutExpired, OSError) as e:
                return str(e)[:120]
            if p.returncode != 0:
                errs = [l for l in (p.stderr or "").splitlines() if l.strip()]
                return (errs[-1] if errs else f"import failed (rc={p.returncode})")[:120]
        if changed_conf:
            # `source-file -n` parses the config without executing anything (SPEC §6.11).
            rc, _out, err = self.cockpit.tmux._run_rc(["source-file", "-n", str(self.paths.tmux_conf)])
            if rc != 0:
                return (err or "source-file parse failed").strip()[:120]
            if self.paths.ui_conf.exists():
                rc, _out, err = self.cockpit.tmux._run_rc(["source-file", "-n", str(self.paths.ui_conf)])
                if rc != 0:
                    return ("ui: " + (err or "source-file parse failed")).strip()[:120]
        return None

    def _step_remotes(self) -> None:
        """Keep a `force watch` connection to every enabled remote; their snapshots land in
        <state>/remotes/<name>/ for the ui (SPEC §40)."""
        self._watchers.tick()

    def _default_reexec(self) -> None:
        """Replace this process with a fresh `force daemon`. The daemon lock fd is O_CLOEXEC (Python
        opens files that way by default), so it does not survive exec and the new process re-takes it."""
        self._watchers.stop()         # the new process starts its own
        sys.stdout.flush()
        sys.stderr.flush()
        argv0 = sys.argv[0]
        if argv0 and os.path.isfile(argv0) and os.access(argv0, os.X_OK):
            os.execv(argv0, sys.argv)
        else:
            os.execv(sys.executable, [sys.executable, "-m", "force.cli", "daemon"])


def _parse_mode(text: str) -> str | None:
    """Scan `text` (the captured agent pane tail) case-sensitively for a permission-mode footer phrase; the
    LAST occurrence wins. None if none is found."""
    found = None
    for line in text.split("\n"):
        for phrase, mode in _MODE_PHRASES:
            if phrase in line:
                found = mode
    return found


def _rename_seen_key(path, old: str, new: str) -> None:
    seen = load_seen(path)
    if old in seen:
        seen[new] = seen.pop(old)
        save_seen(path, seen)


def main(cockpit: Cockpit, interval: float = 1.0) -> int:
    """Take the daemon lock (SPEC §6), loop tick() every `interval` seconds until SIGTERM/SIGINT,
    return 0. Lock taken -> print 'force daemon already running' to stderr, return 1."""
    ensure_dirs(cockpit.paths)
    daemon = Daemon(cockpit)
    lock_path = cockpit.paths.state / "daemon.lock"
    # `lf` is opened by Python, which defaults to O_CLOEXEC, so the lock fd never survives the
    # reload execv (SPEC §6.11) and the fresh process takes the lock itself.
    with open(lock_path, "a") as lf:      # "a": a second daemon must not wipe the running one's pid
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("force daemon already running", file=sys.stderr)
            return 1
        # the pid, for `force uninstall` and ./dev.sh stop (lock holders open the file without truncating it)
        lf.truncate(0)
        lf.write(f"{os.getpid()}\n")
        lf.flush()
        stop = threading.Event()

        def handler(_signum, _frame):
            stop.set()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
        while not stop.is_set():
            daemon.tick()
            deadline = time.monotonic() + interval
            while not stop.is_set() and time.monotonic() < deadline:
                stop.wait(0.1)
        daemon._watchers.stop()
    return 0
