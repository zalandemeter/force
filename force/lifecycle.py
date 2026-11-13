"""Session lifecycle operations shared by CLI, daemon, dialogs and importer (SPEC §7)."""
from __future__ import annotations

import copy
import fcntl
import os
import shlex
import json
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone

from . import opencode as opencode_mod
from .claude import find_transcript, global_memory_dir, memory_dir_for, project_slug
from .config import Config, Paths
from .registry import Entry, RegistryError, is_claude, is_opencode, is_shell, locked, valid_name
from .state import load_seen, save_seen
from .tmux import Tmux, TmuxError

DEFAULT_SIZE = (200, 50)
_POLL = 0.2


def queue_title_sync(entry, name: str) -> None:
    """Ask the daemon to type `/rename NAME` into this Claude session once it is up, idle, connected to Remote
    Control and has an empty input line (SPEC §8): Claude Code reuses a resumed conversation's Remote Control
    session with its first title, `-n` / `--remote-control NAME` do not change it, and a `/rename` reaches the
    phone only after the connection is up (tried 2026-11-04)."""
    entry.extra["title_sync"] = name
    entry.extra["title_sync_at"] = time.time()


def _entry_copy(entry: Entry) -> Entry:
    return Entry(
        name=entry.name, session_id=entry.session_id, cwd=entry.cwd,
        created_at=entry.created_at, order=entry.order, archived=entry.archived,
        archived_at=entry.archived_at, pending_name=entry.pending_name,
        extra=copy.deepcopy(entry.extra),
    )


def _rename_seen_key(path, old: str, new: str) -> None:
    seen = load_seen(path)
    if old in seen:
        seen[new] = seen.pop(old)
        save_seen(path, seen)


class MoveError(Exception):
    """A session could not be moved to another folder (SPEC §7.7)."""


class Cockpit:
    """Bundles paths, config and tmux so every caller does lifecycle work the same way."""

    def __init__(self, paths: Paths, cfg: Config, tmux: Tmux, cockpit_bin: str):
        self.paths = paths
        self.cfg = cfg
        self.tmux = tmux
        self.cockpit_bin = cockpit_bin
        self._ui_tmux: Tmux | None = None

    @property
    def split(self) -> bool:
        """Split mode (SPEC §39): host sessions hold only their main pane; sidebar and header live in
        the ui tmux server, one ui session per terminal."""
        return self.cfg.ui == "split"

    @property
    def ui_tmux(self) -> Tmux:
        """The ui tmux server (split mode)."""
        if self._ui_tmux is None:
            self._ui_tmux = Tmux(self.paths.ui_socket, self.paths.ui_conf)
        return self._ui_tmux

    # --- helpers -------------------------------------------------------------
    def pane_command(self, name: str) -> str:
        """"exec <cockpit_bin> run <name>" (SPEC §7.1). `force run` scrubs inherited Claude session variables
        and re-executes itself through `bash -lic` before starting claude (cli.run_command). tmux runs a pane's
        command through the user's shell (`default-shell -c`): `exec` makes the pane's process Force itself, and so
        the agent, with any shell (dash would otherwise stay in between and own the pane's pid)."""
        return "exec " + shlex.join([self.cockpit_bin, "run", name])

    def sidebar_command(self) -> str:
        """"exec <cockpit_bin> sidebar" quoted (exec: see pane_command)."""
        return "exec " + shlex.join([self.cockpit_bin, "sidebar"])

    def header_command(self) -> str:
        """"exec <cockpit_bin> header" quoted (the 2-row header pane above the agent; exec: see pane_command)."""
        return "exec " + shlex.join([self.cockpit_bin, "header"])

    def role_command(self, role: str) -> str:
        return self.header_command() if role == "header" else self.sidebar_command()

    def sidebar_hidden(self) -> bool:
        """The `≡ sidebar` button parks sidebars in a background window (share/sidebar-toggle.sh)."""
        return self.tmux.get_option("@force_sidebar_hidden") == "1"

    def agent_pane(self, name: str):
        """The tmux.Pane with role 'agent' in session `name`, else None."""
        for p in self.tmux.panes():
            if p.session == name and p.role == "agent":
                return p
        return None

    def client_size(self) -> tuple[int, int]:
        """Size of the client with the highest activity, else DEFAULT_SIZE."""
        clients = self.tmux.clients()
        if not clients:
            return DEFAULT_SIZE
        best = max(clients, key=lambda c: c.activity)
        w = best.width or DEFAULT_SIZE[0]
        h = best.height or DEFAULT_SIZE[1]
        return (w, h)

    def sidebar_enabled(self) -> bool:
        """Global tmux option @force_sidebar != 'off' (default on)."""
        return self.tmux.get_option("@force_sidebar") != "off"

    def _entry(self, name: str) -> Entry | None:
        with locked(self.paths.home) as reg:
            e = reg.get(name)
            return _entry_copy(e) if e else None

    # --- operations ----------------------------------------------------------
    def create(self, entry: Entry) -> None:
        """SPEC §7.1. No-op if the tmux session already exists."""
        if self.tmux.has_session(entry.name):
            return
        width, height = self.client_size()
        cmd = self.pane_command(entry.name)
        try:
            pane_id = self.tmux.new_session(
                entry.name, width, height, entry.cwd, cmd,
                env={"FORCE_SESSION": entry.name},
            )
        except TmuxError as e:
            # `force new` and the daemon's tick can both create a new entry's session: the one that comes second gets
            # tmux's "duplicate session". The first one runs these same steps, so the session is there as it should be.
            if "duplicate session" in str(e) and self.tmux.has_session(entry.name):
                return
            raise
        self.tmux.run("set-option", "-p", "-t", pane_id, "@force_role", "agent")
        self.tmux.run("set-option", "-t", entry.name, "remain-on-exit", "on")
        if not self.split:           # split mode: the ui server draws them (SPEC §39)
            self.ensure_header(entry.name)
            if self.sidebar_enabled():
                self.ensure_sidebar(entry.name)
        self.tmux.select_pane(pane_id)

    def stop_agent(self, name: str, timeout: float = 15.0) -> None:
        """SIGTERM the agent pane pid, wait for pane_dead up to timeout, then SIGKILL + 5 s.
        Calls self.on_cockpit_kill(name) before signalling (daemon uses it to mark cockpit-initiated deaths).
        No-op if no live agent pane."""
        pane = self.agent_pane(name)
        if pane is None or not pane.pid or pane.dead:
            return
        self.on_cockpit_kill(name)
        self._mark_cockpit_kill(name, pane.pid)
        pid = pane.pid
        try:
            os.kill(pid, self._stop_signal(name))
        except ProcessLookupError:
            return
        if self._wait_dead(name, timeout):
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        self._wait_dead(name, 5.0)

    def _stop_signal(self, name: str) -> int:
        """SIGTERM for an agent; SIGHUP for a shell session (an interactive bash ignores SIGTERM, so it
        used to sit out the whole timeout before SIGKILL)."""
        entry = self._entry(name)
        return signal.SIGHUP if entry is not None and is_shell(entry) else signal.SIGTERM

    def _mark_cockpit_kill(self, name: str, pid: int | None = None) -> None:
        """Tell the daemon (another process) that this death is force's doing, not a crash. The file holds
        "<time> <pid stopped>"; the pid tells the sidebar's "restarting" from "restarted"."""
        try:
            d = self.paths.state / "kills"
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(f"{time.time()} {pid or 0}")
        except OSError:
            pass

    def _kill_mark(self, name: str) -> tuple[float, int] | None:
        try:
            parts = (self.paths.state / "kills" / name).read_text().split()
            return float(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except (OSError, ValueError, IndexError):
            return None

    def restarted(self, name: str) -> tuple[float, int] | None:
        """(seconds since force last stopped this session's agent, pid it stopped), else None."""
        m = self._kill_mark(name)
        return (time.time() - m[0], m[1]) if m else None

    def recently_killed_by_cockpit(self, name: str, within: float = 60.0) -> bool:
        m = self._kill_mark(name)
        return m is not None and time.time() - m[0] < within

    def _wait_dead(self, name: str, duration: float) -> bool:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.tmux.invalidate()
            p = self.agent_pane(name)
            if p is None or p.dead:
                return True
            time.sleep(_POLL)
        return False

    def restart(self, name: str) -> None:
        """SPEC §7.3: stop_agent then respawn the agent pane; if the tmux session is missing, create()."""
        entry = self._entry(name)
        if entry is None:
            return
        if not self.tmux.has_session(name):
            self.create(entry)
            return
        pane = self.agent_pane(name)
        if pane is None:
            self.tmux.kill_session(name)
            self.create(entry)
            return
        self.stop_agent(name)
        self.tmux.respawn_pane(pane.pane_id, self.pane_command(name))

    def restart_many(self, names: list[str], timeout: float = 15.0) -> list[str]:
        """Restart several sessions together: signal every live pane first, then respawn each one as
        soon as it is dead, SIGKILLing whatever is still alive after `timeout`. The total is about the
        slowest single stop, not their sum (one by one took ~1.3 s per agent and the whole timeout for
        a shell). Returns the names that could not be restarted."""
        failed: list[str] = []
        waiting: dict[str, int] = {}        # name -> pid signalled
        for name in names:
            try:
                entry = self._entry(name)
                if entry is None:
                    continue
                if not self.tmux.has_session(name):
                    self.create(entry)
                    continue
                pane = self.agent_pane(name)
                if pane is None:
                    self.tmux.kill_session(name)
                    self.create(entry)
                    continue
                if pane.pid and not pane.dead:
                    self.on_cockpit_kill(name)
                    self._mark_cockpit_kill(name, pane.pid)
                    try:
                        os.kill(pane.pid, self._stop_signal(name))
                    except ProcessLookupError:
                        pass
                    waiting[name] = pane.pid
                else:
                    self.tmux.respawn_pane(pane.pane_id, self.pane_command(name))
            except Exception:
                failed.append(name)

        def respawn_dead() -> None:
            self.tmux.invalidate()
            for name in list(waiting):
                pane = self.agent_pane(name)
                if pane is None or pane.dead:
                    del waiting[name]
                    try:
                        if pane is None:
                            entry = self._entry(name)
                            if entry is not None:
                                self.tmux.kill_session(name)
                                self.create(entry)
                        else:
                            self.tmux.respawn_pane(pane.pane_id, self.pane_command(name))
                    except Exception:
                        failed.append(name)

        for limit, kill in ((timeout, True), (5.0, False)):
            deadline = time.monotonic() + limit
            while waiting and time.monotonic() < deadline:
                respawn_dead()
                if waiting:
                    time.sleep(_POLL)
            if not waiting or not kill:
                break
            for pid in waiting.values():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        failed.extend(waiting)
        return failed

    def archive(self, name: str, force: bool = False) -> None:
        """SPEC §7.4 (state check done by caller when force is False). Moves viewing clients to another
        live session first (if any)."""
        clients = self.tmux.clients()
        viewing = [c for c in clients if c.session == name]
        target = None
        with locked(self.paths.home) as reg:
            entry = reg.get(name)
            live = [e for e in reg.live() if e.name != name]
            if live and entry is not None:
                target = min(live, key=lambda e: e.order).name
            if entry is not None:
                # the sidebar says "archiving" while it stops (not the restart feedback the stop would show)
                entry.extra["archiving"] = True
        try:
            if viewing and target:
                for c in viewing:
                    self.tmux.switch_client(target, c.name)
            self.stop_agent(name)
            try:
                self.tmux.kill_session(name)
            except Exception:
                pass
            if entry:
                transcript = find_transcript(self.paths.claude_dir, entry.session_id)
                if transcript:
                    dest = self.paths.archive / entry.session_id / transcript.parent.name
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(transcript, dest / transcript.name)
                    sibling = transcript.parent / entry.session_id
                    if sibling.is_dir():
                        shutil.copytree(sibling, dest / entry.session_id, dirs_exist_ok=True)
        except BaseException:
            with locked(self.paths.home) as reg:
                e = reg.get(name)
                if e is not None:
                    e.extra.pop("archiving", None)
            raise
        with locked(self.paths.home) as reg:
            e = reg.get(name)
            if e is not None:
                e.archived = True
                e.archived_at = datetime.now(timezone.utc).isoformat()
                e.extra.pop("archiving", None)

    def move(self, name: str, folder: str) -> None:
        """SPEC §7.7: continue the session in `folder`: same conversation, its auto memory kept (a Claude
        session is pinned to the memory directory it used so far), the new folder's CLAUDE.md / AGENTS.md
        from now on. Claude resumes by id from any folder; an OpenCode session is re-homed into the new
        folder's project first (export in the old folder, import in the new one). Restarts the session.
        The caller checks it is not busy. Raises RegistryError / MoveError; on failure the session stays
        (or is restarted) in its old folder."""
        target = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(target):
            raise MoveError(f"no such folder {folder!r}")
        with locked(self.paths.home) as reg:
            e = reg.get(name)
            if e is None:
                raise RegistryError(f"unknown session {name!r}")
            if e.archived:
                raise RegistryError(f"session {name!r} is archived")
            old = e.cwd
            if os.path.abspath(old) == target:
                return
            if is_claude(e) and "memory_dir" not in e.extra and global_memory_dir(self.paths.claude_dir) is None:
                md = memory_dir_for(self.paths.claude_dir, e.session_id, old)
                if md is not None:
                    e.extra["memory_dir"] = str(md)
            oc_session = e.extra.get("oc_session") if is_opencode(e) else None
            claude_sid = e.session_id if is_claude(e) else None
            e.extra["moving"] = True       # the sidebar says "moving", not the restart feedback
        stopped = False
        try:
            if oc_session:
                # OpenCode must not write the session while it moves to the other project
                self.stop_agent(name)
                stopped = True
                self._oc_rehome(oc_session, old, target)
            elif claude_sid:
                t = find_transcript(self.paths.claude_dir, claude_sid)
                if t is not None:
                    # the transcript goes along to the new folder's project (owner: Claude's own /resume
                    # picker there lists it; a never-used one is even found only from its own project).
                    # Stopped first: Claude appends to it while it runs.
                    self.stop_agent(name)
                    stopped = True
                    self._carry_transcript(t, claude_sid, target)
            with locked(self.paths.home) as reg:
                e = reg.get(name)
                if e is not None:
                    e.cwd = target
            self.restart(name)
        except BaseException:
            with locked(self.paths.home) as reg:
                e = reg.get(name)
                if e is not None:
                    e.cwd = old
                    e.extra.pop("moving", None)
            if stopped:
                try:
                    self.restart(name)
                except Exception:
                    pass
            raise
        with locked(self.paths.home) as reg:
            e = reg.get(name)
            if e is not None:
                e.extra.pop("moving", None)
                e.extra["moved_at"] = time.time()

    def _carry_transcript(self, transcript, session_id: str, new: str) -> None:
        """Move a session's transcript (and its sibling dir: subagents, tool results) into `new`'s project
        directory. The sibling dir goes first, so a failure leaves the transcript where it was."""
        slug = project_slug(new)
        if slug is None:
            raise MoveError(f"cannot tell Claude's project directory for {new!r}")
        dest = self.paths.claude_dir / "projects" / slug
        if dest == transcript.parent:
            return
        dest.mkdir(parents=True, exist_ok=True)
        sibling = transcript.parent / session_id
        if sibling.is_dir():
            if (dest / session_id).exists():
                shutil.copytree(sibling, dest / session_id, dirs_exist_ok=True)
                shutil.rmtree(sibling)
            else:
                shutil.move(str(sibling), str(dest / session_id))
        os.replace(transcript, dest / transcript.name)

    def _oc_rehome(self, session: str, old: str, new: str) -> None:
        """Move an OpenCode session into `new`'s project: OpenCode finds a session only in the project of
        the folder it runs in. `opencode export` in the old folder, `opencode import` in the new one (same
        session id, project and directory updated)."""
        binary = opencode_mod.installed()
        if binary is None:
            raise MoveError("OpenCode is not installed")
        env = dict(os.environ, OPENCODE_DISABLE_AUTOUPDATE="1")
        if not os.path.isdir(old):
            raise MoveError(f"the old folder {old!r} is gone; OpenCode exports a session from its own folder")
        try:
            out = subprocess.run([binary, "export", session], cwd=old, env=env, capture_output=True,
                                 text=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as ex:
            raise MoveError(f"opencode export failed: {ex}")
        try:
            data = json.loads(out.stdout)
            ok = out.returncode == 0 and data.get("info", {}).get("id") == session
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            raise MoveError(f"opencode export failed: {(out.stderr or out.stdout).strip()[-300:]}")
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=self.paths.state, delete=False) as f:
            f.write(out.stdout)
            tmp = f.name
        try:
            r = subprocess.run([binary, "import", tmp], cwd=new, env=env, capture_output=True, text=True,
                               timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as ex:
            raise MoveError(f"opencode import failed: {ex}")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if r.returncode != 0:
            raise MoveError(f"opencode import failed: {(r.stderr or r.stdout).strip()[-300:]}")

    def delete(self, name: str) -> None:
        """Remove a session for good (live or archived): drop it from the registry FIRST (so the daemon
        never restores it in between), move viewing clients to another live session, stop it, close its
        tmux session and delete cockpit's archived copy. Claude Code's own transcript in ~/.claude is left
        alone (Claude deletes it after 30 days)."""
        with locked(self.paths.home) as reg:
            entry = reg.get(name)
            if entry is None:
                raise RegistryError(f"unknown session {name!r}")
            live = [e for e in reg.live() if e.name != name]
            target = min(live, key=lambda e: e.order).name if live else None
            reg.remove(name)
        if not entry.archived:
            if target:
                for c in self.tmux.clients():
                    if c.session == name:
                        self.tmux.switch_client(target, c.name)
            self.stop_agent(name)
            try:
                self.tmux.kill_session(name)
            except Exception:
                pass
        try:
            uuid.UUID(entry.session_id)              # never rmtree a path built from a bad id
        except (ValueError, AttributeError, TypeError):
            return
        copy_dir = self.paths.archive / entry.session_id
        if copy_dir.is_dir() and not copy_dir.is_symlink():
            shutil.rmtree(copy_dir, ignore_errors=True)

    def unarchive(self, name: str) -> Entry:
        """SPEC §7.5, then create() so it starts immediately."""
        with locked(self.paths.home) as reg:
            entry = reg.get(name)
            if entry is None or not entry.archived:
                raise RegistryError(f"unknown or not archived session: {name!r}")
            src = self.paths.archive / entry.session_id
            if src.is_dir():
                for proj_src in src.iterdir():
                    if not proj_src.is_dir():
                        continue
                    proj_dst = self.paths.claude_dir / "projects" / proj_src.name
                    proj_dst.mkdir(parents=True, exist_ok=True)
                    for item in proj_src.rglob("*"):
                        rel = item.relative_to(proj_src)
                        dst = proj_dst / rel
                        if item.is_dir():
                            dst.mkdir(parents=True, exist_ok=True)
                        elif not dst.exists():
                            shutil.copy2(item, dst)
            entry.archived = False
            entry.archived_at = None
            entry.order = max((e.order for e in reg.entries), default=-1) + 1
            result = _entry_copy(entry)
        self.create(result)
        return result

    def rename(self, old: str, new: str) -> str:
        """SPEC §8 rename: a live agent pane -> set pending_name; a live shell is renamed immediately
        (no pending_name, no restart). Else (down) immediate rename of registry entry, tmux session (if any)
        and seen.json key. Renaming to the name already pending is a no-op; renaming back to the current name
        cancels a pending rename. Returns a one-line message for the user."""
        if not valid_name(new):
            raise RegistryError(f"invalid name {new!r}")
        pane = self.agent_pane(old)
        live = pane is not None and not pane.dead
        with locked(self.paths.home) as reg:
            entry = reg.get(old)
            if entry is None:
                raise RegistryError(f"unknown session: {old!r}")
            if entry.archived:
                raise RegistryError(f"cannot rename archived session: {old!r}")
            if entry.pending_name == new:
                return f"{old} becomes {new} as soon as it is idle (already queued)"
            if new == old:
                if entry.pending_name:
                    entry.pending_name = None
                    return f"{old}: rename cancelled"
                return f"{old} already has that name"
            if any(e is not entry and (e.name == new or e.pending_name == new) for e in reg.entries):
                raise RegistryError(f"name already taken: {new!r}")
            entry.extra.pop("auto_name", None)      # named by hand: no automatic name any more (§49.6)
            if live and is_claude(entry):     # Claude carries the name (-n): rename at the next restart
                entry.pending_name = new
                return f"{old} becomes {new} as soon as it is idle (its agent restarts with the new name)"
            reg.rename(old, new)
        if self.tmux.has_session(old):
            self.tmux.rename_session(old, new)
        _rename_seen_key(self.paths.seen, old, new)
        return f"renamed {old} to {new}"

    def apply_rename(self, name: str) -> str:
        """SPEC §6.6: perform a pending rename now (tmux rename, registry, seen, restart). Returns new name."""
        with locked(self.paths.home) as reg:
            entry = reg.get(name)
            if entry is None or not entry.pending_name:
                return name
            new = entry.pending_name
            reg.rename(name, new)
            if is_claude(entry):
                queue_title_sync(entry, new)     # Remote Control keeps its old title otherwise (§8)
        if self.tmux.has_session(name):
            self.tmux.rename_session(name, new)
        _rename_seen_key(self.paths.seen, name, new)
        old_header = self.paths.header_dir / f"{name}.txt"
        if old_header.exists():
            old_header.unlink()
        self.restart(new)
        return new

    def sidebar_toggle(self) -> bool:
        """SPEC §7.6; returns the new enabled state."""
        enabled = self.sidebar_enabled()
        if enabled:
            self.tmux.set_option("@force_sidebar", "off")
            for p in self.tmux.panes():
                if p.role == "sidebar":
                    self.tmux.kill_pane(p.pane_id)
            return False
        self.tmux.set_option("@force_sidebar", "on")
        with locked(self.paths.home) as reg:
            names = [e.name for e in reg.live()]
        for n in names:
            self.ensure_sidebar(n)
        return True

    def _layout_lock(self):
        self.paths.state.mkdir(parents=True, exist_ok=True)
        lf = open(self.paths.state / "sidebar.lock", "w")
        fcntl.flock(lf, fcntl.LOCK_EX)
        return lf

    def ensure_sidebar(self, name: str) -> bool:
        """Add the sidebar pane to session `name` if enabled and missing.

        Layout: [sidebar, full window height] | [header / agent]. While the sidebar is hidden it is
        created in the session's background window `force-stash` instead. The CLI and the daemon both
        call this; check-and-create runs under a file lock with fresh tmux data and the new pane is
        tagged in the same tmux command, so two callers can never both add one."""
        if not self.sidebar_enabled():
            return False
        # fast path on the (possibly cached) pane list; the locked re-check below decides creation
        if any(p.session == name and p.role == "sidebar" for p in self.tmux.panes()):
            return False
        with self._layout_lock():
            self.tmux.invalidate()
            panes = self.tmux.panes(strict=True)        # never create on a failed listing
            if not any(p.session == name and p.role == "agent" for p in panes):
                return False
            if any(p.session == name and p.role == "sidebar" for p in panes):
                return False
            agent = self.agent_pane(name)
            if agent is None:
                return False
            entry = self._entry(name)
            cwd = entry.cwd if entry else str(self.paths.home)
            if self.sidebar_hidden():
                # -d leaves the current pane unchanged, so tag the new pane by id (we hold the lock).
                pane = self.tmux.run("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={name}:",
                                     "-n", "force-stash", "-c", cwd, self.sidebar_command())
                self.tmux.run("set-option", "-p", "-t", pane.strip(), "@force_role", "sidebar")
            else:
                # -f: span the full window height, beside both the header and the agent pane.
                self.tmux.run(
                    "split-window", "-h", "-b", "-f", "-l", str(self.cfg.sidebar_width),
                    "-t", agent.pane_id, "-c", cwd, self.sidebar_command(),
                    ";", "set-option", "-p", "@force_role", "sidebar",
                )
                self.tmux.select_pane(agent.pane_id)
            return True

    def ensure_header(self, name: str) -> bool:
        """Add the 2-row header pane above the agent pane of session `name` if missing."""
        # fast path on the (possibly cached) pane list; the locked re-check below decides creation
        if any(p.session == name and p.role == "header" for p in self.tmux.panes()):
            return False
        with self._layout_lock():
            self.tmux.invalidate()
            panes = self.tmux.panes(strict=True)        # never create on a failed listing
            if not any(p.session == name and p.role == "agent" for p in panes):
                return False
            if any(p.session == name and p.role == "header" for p in panes):
                return False
            agent = self.agent_pane(name)
            if agent is None:
                return False
            entry = self._entry(name)
            cwd = entry.cwd if entry else str(self.paths.home)
            self.tmux.run(
                "split-window", "-v", "-b", "-l", "2", "-t", agent.pane_id, "-c", cwd,
                self.header_command(), ";", "set-option", "-p", "@force_role", "header",
            )
            self.tmux.select_pane(agent.pane_id)
            return True

    def dedupe_sidebars(self) -> int:
        """Kill all but the first sidebar and header pane of every session; returns how many."""
        seen: set[tuple[str, str]] = set()
        removed = 0
        panes = sorted(self.tmux.panes(), key=lambda p: int(p.pane_id.lstrip("%") or 0))
        for p in panes:                      # oldest first: the original pane survives
            if p.role not in ("sidebar", "header"):
                continue
            key = (p.session, p.role)
            if key in seen:
                self.tmux.kill_pane(p.pane_id)
                removed += 1
            else:
                seen.add(key)
        return removed

    on_cockpit_kill = staticmethod(lambda name: None)
