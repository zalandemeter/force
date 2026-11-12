"""Thin wrapper over the tmux CLI on the force socket (SPEC §7)."""
from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import procinfo


def _no_sessions(err: str) -> bool:
    """tmux's answer to `list-panes -a` on a running server that has no sessions (tmux 3.4)."""
    return "no current target" in (err or "")


class TmuxError(Exception):
    pass


# The pre-rebrand role value of live panes. New panes are tagged "agent"; panes tagged with this
# legacy value are read as "agent" and the daemon re-tags them (force/daemon.py).
LEGACY_AGENT_ROLE = "claude"


@dataclass(frozen=True)
class Pane:
    pane_id: str            # "%12"
    session: str
    role: str | None        # @force_role
    pid: int | None
    dead: bool
    dead_status: int | None
    dead_signal: int | None
    started_ms: int | None  # from #{pane_start_time}? not available in 3.4 -> derive from the process start time (procinfo); None if unknown
    title: str | None = None  # #{pane_title}
    command: str | None = None  # #{pane_current_command}: the pane's foreground program (SPEC §49.3)


@dataclass(frozen=True)
class Client:
    name: str               # #{client_name}
    session: str            # #{client_session}
    width: int
    height: int
    activity: int           # #{client_activity} epoch seconds


_PANE_FMT = "\t".join([
    "#{session_name}", "#{pane_id}", "#{@force_role}", "#{pane_pid}",
    "#{pane_dead}", "#{pane_dead_status}", "#{pane_dead_signal}", "#{pane_current_command}", "#{pane_title}",
])
_CLIENT_FMT = "\t".join([
    "#{client_name}", "#{client_session}", "#{client_width}",
    "#{client_height}", "#{client_activity}",
])


class Tmux:
    def __init__(self, socket: str, conf: Path | None = None, binary: str = "tmux"):
        self.socket = socket
        self.conf = conf
        self.binary = binary
        self._cache_on = False
        self._panes_cache = None
        self._clients_cache = None

    # --- read cache ----------------------------------------------------------
    def begin_cache(self) -> None:
        """Start caching reads (panes()/clients()). Any mutation refetches on the next read."""
        self._cache_on = True
        self._panes_cache = None
        self._clients_cache = None

    def end_cache(self) -> None:
        self._cache_on = False
        self._panes_cache = None
        self._clients_cache = None

    def invalidate(self) -> None:
        """Drop cached reads so the next panes()/clients() refetches from tmux."""
        self._panes_cache = None
        self._clients_cache = None

    @contextmanager
    def cached(self):
        self.begin_cache()
        try:
            yield self
        finally:
            self.end_cache()

    def _cmd(self, args: list[str]) -> list[str]:
        cmd = [self.binary, "-L", self.socket]
        if self.conf is not None:
            cmd += ["-f", str(self.conf)]
        return cmd + args

    def _run_rc(self, args: list[str]):
        """Run, returning (returncode, stdout, stderr). Never raises."""
        try:
            p = subprocess.run(self._cmd(args), capture_output=True, text=True, timeout=10)
            return p.returncode, p.stdout or "", p.stderr or ""
        except (subprocess.TimeoutExpired, OSError):
            return 1, "", "error connecting to tmux"

    def run(self, *args: str, check: bool = True) -> str:
        """Run `tmux -L socket [-f conf] args...`, return stdout (stripped). check -> TmuxError with stderr.
        run() is only ever used for mutations, so it invalidates the read cache first."""
        self.invalidate()
        try:
            p = subprocess.run(self._cmd(list(args)), capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise TmuxError(str(e))
        if check and p.returncode != 0:
            err = (p.stderr or "").strip()
            raise TmuxError(err or f"tmux failed: {' '.join(args)}")
        return (p.stdout or "").strip()

    # --- server --------------------------------------------------------------
    def server_running(self) -> bool:
        rc, _out, err = self._run_rc(["list-sessions"])
        if rc == 0:
            return True
        if "no sessions" in err:
            return True
        if "no server running" in err or "error connecting" in err:
            return False
        return False

    def start_server(self) -> None:
        self.run("start-server")

    def kill_server(self) -> None:
        try:
            self.run("kill-server", check=False)
        except TmuxError:
            pass
        except subprocess.TimeoutExpired:
            pass

    # --- queries -------------------------------------------------------------
    def panes(self, strict: bool = False) -> list[Pane]:
        """All panes. A failed tmux call yields [] unless strict=True, which raises TmuxError instead
        (callers that would *create* panes when one is missing must never act on a failed listing)."""
        if self._cache_on and self._panes_cache is not None and not strict:
            return self._panes_cache
        rc, out, err = self._run_rc(["list-panes", "-a", "-F", _PANE_FMT])
        if rc != 0 and _no_sessions(err):
            rc, out = 0, ""            # a server without sessions (a fresh install): no panes, not a failure
        if rc != 0 and strict:
            raise TmuxError(err.strip() or "list-panes failed")
        if rc != 0:
            result: list[Pane] = []
        else:
            result = []
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) < 8:
                    continue
                session, pane_id, role, pid_s, dead_s, dstat_s, dsig_s, command = parts[:8]
                title = "\t".join(parts[8:]) if len(parts) > 8 else None   # a title may contain tabs
                pid = _int(pid_s)
                if role == LEGACY_AGENT_ROLE:
                    role = "agent"        # a pre-rebrand pane tagged "claude" reads as an agent pane
                result.append(Pane(
                    pane_id=pane_id,
                    session=session,
                    role=(role or None),
                    pid=pid,
                    dead=(dead_s == "1"),
                    dead_status=_int(dstat_s),
                    dead_signal=_int(dsig_s),
                    started_ms=procinfo.started_ms(pid),
                    title=title,
                    command=command or None,
                ))
        if self._cache_on:
            self._panes_cache = result
        return result

    def capture_tail(self, pane_id: str, lines: int = 6) -> str:
        """Capture the visible screen of `pane_id` (capture-pane -p) and return its last `lines`
        non-empty lines joined by "\\n". Errors -> ""."""
        rc, out, _err = self._run_rc(["capture-pane", "-p", "-t", pane_id])
        if rc != 0:
            return ""
        tail = [l for l in out.splitlines() if l.strip()]
        return "\n".join(tail[-lines:])

    def clients(self) -> list[Client]:
        if self._cache_on and self._clients_cache is not None:
            return self._clients_cache
        rc, out, _err = self._run_rc(["list-clients", "-F", _CLIENT_FMT])
        if rc != 0:
            result: list[Client] = []
        else:
            result = []
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                name, session, w, h, act = parts[:5]
                result.append(Client(name, session, _int(w) or 0, _int(h) or 0, _int(act) or 0))
        if self._cache_on:
            self._clients_cache = result
        return result

    def sessions(self) -> list[str]:
        """Session names, derived from cached panes() (exact session names)."""
        return sorted({p.session for p in self.panes()})

    def has_session(self, name: str) -> bool:
        if self._cache_on:
            return any(p.session == name for p in self.panes())
        rc, _out, _err = self._run_rc(["has-session", "-t", f"={name}"])
        return rc == 0

    def get_option(self, key: str) -> str | None:
        """Global server/session option (show-options -gqv)."""
        rc, out, _err = self._run_rc(["show-options", "-gqv", key])
        if rc != 0:
            return None
        out = out.strip()
        return out or None

    # --- modification ---------------------------------------------------------
    def set_option(self, key: str, value: str, target: str | None = None) -> None:
        args = ["set-option"]
        if target is not None:
            args += ["-t", target]
        else:
            args += ["-g"]
        args += [key, str(value)]
        self.run(*args)

    def unset_option(self, key: str, target: str | None = None) -> None:
        args = ["set-option", "-u"]
        if target is not None:
            args += ["-t", target]
        else:
            args += ["-g"]
        args += [key]
        self.run(*args)

    def new_session(self, name: str, width: int, height: int, cwd: str, command: str,
                  env: dict[str, str] | None = None) -> str:
        args = ["new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name,
                "-x", str(width), "-y", str(height), "-c", cwd]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        args.append(command)
        return self.run(*args)

    def select_pane(self, pane: str) -> None:
        self.run("select-pane", "-t", pane)

    def respawn_pane(self, pane: str, command: str) -> None:
        self.run("respawn-pane", "-t", pane, command)

    def kill_pane(self, pane: str) -> None:
        self.run("kill-pane", "-t", pane)

    def kill_session(self, name: str) -> None:
        self.run("kill-session", "-t", f"={name}")

    def rename_session(self, old: str, new: str) -> None:
        self.run("rename-session", "-t", f"={old}", new)

    def switch_client(self, target: str, client: str | None = None) -> None:
        args = ["switch-client"]
        if client:
            args += ["-c", client]
        args += ["-t", f"={target}"]
        self.run(*args)

    def display_message(self, client: str, text: str, duration_ms: int = 4000) -> None:
        self.run("display-message", "-c", client, "-d", str(duration_ms), text)


def _int(s: str) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


