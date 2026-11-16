"""`force api` (SPEC §54): the frontend API. JSON lines on stdin/stdout for a frontend outside the terminal (the
first: a Minecraft mod). Force decides, the frontend draws: `state` is the view model of every host, `notice`
the §49.4 events, `run` the cockpit's own CLI commands, `terminal.*` a live view of a session's agent pane.

Threads: the main one reads requests; `state` and `notices` come from one poller; each terminal view has a
reader (its tmux control-mode client) and a framer. Every write to stdout goes through `Api.send` (one lock).
"""
from __future__ import annotations

import base64
import fcntl
import itertools
import json
import os
import re
import signal
import socket
import struct
import termios
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, notices as notices_mod, remote as remote_mod, updates
from .state import STATE_WORDS, Snapshot, State, read_snapshot

PROTOCOL = 1
FEATURES = ["state", "notices", "run", "terminal.cells", "terminal.vt.app"]
# the commands a cockpit runs (SPEC §54 `run`); never uninstall, update, host, install-to
RUN_ALLOWED = frozenset("prompt keys read wait new rename move archive unarchive delete restart upgrade "
                        "remote-control explain list settings".split())
RUN_TIMEOUT = 600
STATE_EVERY = 0.25          # the poller's period; a `state` event only when the document changed
STALE_MS = 10_000           # a local snapshot older than this: the daemon is not running (SPEC §43.1)
FRAME_EVERY = 1 / 30        # at most 30 frames/s per view
REFRESH_EVERY = 2.0         # a frame at least this often while shown, even without output (cursor, history)
# a tmux key name (Enter, BTab, F5, C-Up, M-x, C-[, C-\\, S-Up), or one printable character
_KEY = re.compile(r"^(?:[CMS]-){0,3}(?:[A-Za-z][A-Za-z0-9]{0,15}|[!-~])$")


def short_host() -> str:
    return socket.gethostname().split(".")[0]


# --- the view model (SPEC §54 `state`) ------------------------------------------------------------------------
def _session(host: str, s) -> dict:
    return {"host": host, "name": s.name, "address": f"{host}:{s.name}", "index": s.index, "order": s.order,
            "cwd": s.cwd, "kind": s.kind, "state": STATE_WORDS.get(s.state, "unknown"), "since_ms": s.since_ms,
            "activity": s.activity, "title": s.title, "note": s.note, "unread": s.unread, "watching": s.watching,
            "viewed": s.viewed, "model": s.model, "effort": s.effort, "archived": s.archived,
            "support": s.support, "remote_control": s.remote_control, "rc_connected": s.rc_connected}


def _remote_snapshot(paths, name: str) -> Snapshot | None:
    return read_snapshot(remote_mod.remote_dir(paths, name) / "snapshot.json")


def view_model(paths, now_ms: int | None = None) -> tuple[dict, dict[str, list]]:
    """(the `state` document, {host: [SessionView]} for notices). This machine's snapshot plus each enabled
    remote's copy (§40); a remote's sessions only while it is connected."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    here = short_host()
    snap = read_snapshot(paths.snapshot)
    fresh = snap is not None and now_ms - snap.generated_ms < STALE_MS
    hosts = [{"name": here, "local": True, "status": "connected" if fresh else "daemon_not_running",
              "version": updates.current_version()}]
    views: dict[str, list] = {here: list(snap.sessions) if fresh else []}
    for r in remote_mod.load(paths):
        if not r.enabled:
            continue
        st = remote_mod.read_status(paths, r.name)
        status = st.get("state") or "unknown"
        hosts.append({"name": r.name, "local": False, "status": status,
                      "version": updates.base_version(st.get("version"))})
        rs = _remote_snapshot(paths, r.name) if status == "connected" else None
        views[r.name] = list(rs.sessions) if rs is not None else []
    sessions = [_session(h, s) for h, vs in views.items() for s in vs]
    live = [s for s in sessions if not s["archived"] and not s["support"]]
    doc = {"generated_ms": now_ms, "hosts": hosts, "sessions": sessions,
           "attention": {"needs_you": sum(s["state"] == "needs-you" for s in live),
                         "unread": sum(bool(s["unread"]) for s in live)},
           "limits": (snap.limits or []) if fresh else [],
           "update": snap.update if fresh else None}
    return doc, views


# --- SGR (`capture-pane -e`) to runs ----------------------------------------------------------------------------
def _xterm256(n: int) -> str:
    base = ["#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
            "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"]
    if n < 16:
        return base[n]
    if n < 232:
        n -= 16
        lv = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (lv[n // 36], lv[(n // 6) % 6], lv[n % 6])
    g = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (g, g, g)


_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_OTHER_ESC = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-ln-z]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z]|[=>])")


def _apply_sgr(st: dict, params: str) -> None:
    ps = [int(p) if p.isdigit() else 0 for p in re.split(r"[;:]", params)] if params else [0]
    i = 0
    while i < len(ps):
        p = ps[i]
        if p == 0:
            st.clear()
        elif p == 1:
            st["bold"] = True
        elif p == 2:
            st["dim"] = True
        elif p == 3:
            st["italic"] = True
        elif p == 4:
            st["underline"] = True
        elif p == 7:
            st["reverse"] = True
        elif p == 22:
            st.pop("bold", None)
            st.pop("dim", None)
        elif p == 23:
            st.pop("italic", None)
        elif p == 24:
            st.pop("underline", None)
        elif p == 27:
            st.pop("reverse", None)
        elif 30 <= p <= 37 or 90 <= p <= 97:
            st["fg"] = _xterm256(p - 30 if p < 90 else p - 90 + 8)
        elif 40 <= p <= 47 or 100 <= p <= 107:
            st["bg"] = _xterm256(p - 40 if p < 100 else p - 100 + 8)
        elif p == 39:
            st.pop("fg", None)
        elif p == 49:
            st.pop("bg", None)
        elif p in (38, 48) and i + 1 < len(ps):
            key = "fg" if p == 38 else "bg"
            if ps[i + 1] == 5 and i + 2 < len(ps):
                st[key] = _xterm256(ps[i + 2] % 256)
                i += 2
            elif ps[i + 1] == 2 and i + 4 < len(ps):
                st[key] = "#%02x%02x%02x" % tuple(min(255, v) for v in ps[i + 2:i + 5])
                i += 4
        i += 1


def parse_line(line: str) -> list[dict]:
    """One `capture-pane -e -p` row as runs: [{"text", "fg", "bg", "bold", …}], styles only when set."""
    runs: list[dict] = []
    st: dict = {}
    pos = 0
    line = _OTHER_ESC.sub("", line)
    for m in _SGR.finditer(line):
        if m.start() > pos:
            _add(runs, line[pos:m.start()], st)
        _apply_sgr(st, m.group(1))
        pos = m.end()
    if pos < len(line):
        _add(runs, line[pos:], st)
    return runs


def _add(runs: list[dict], text: str, st: dict) -> None:
    text = "".join(ch if ch >= " " or ch == "\t" else "" for ch in text)
    if not text:
        return
    run = {"text": text, "fg": st.get("fg"), "bg": st.get("bg")}
    for k in ("bold", "dim", "italic", "underline", "reverse"):
        if st.get(k):
            run[k] = True
    if runs and {k: v for k, v in runs[-1].items() if k != "text"} == {k: v for k, v in run.items() if k != "text"}:
        runs[-1]["text"] += text
    else:
        runs.append(run)


# --- tmux command quoting (control mode reads commands as text lines) ----------------------------------------
def tmux_quote(s: str) -> str:
    """`s` as one tmux command argument: double quotes with \\, ", $ and control characters escaped (tmux 3.x
    parses \\n, \\r, \\t, \\e and \\uXXXX inside double quotes), so no text can end the command line."""
    out = ['"']
    for ch in s:
        if ch in '\\"$':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\x1b":
            out.append("\\e")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --- one live view of a session's agent pane (cells mode) ---------------------------------------------------
class CellsTerminal:
    """A tmux control-mode client (`tmux -C attach -t =NAME`) on the session's host: its `%output` lines say when
    the pane changed, and the commands it reads on stdin (capture-pane, send-keys) run over that one connection,
    also through ssh for a remote. It never sizes a window (SPEC §54); while it is attached its session counts as
    viewed, so `hidden` detaches it."""

    def __init__(self, api: "Api", term: int, host: str, name: str):
        self.api, self.term = api, term
        self.host, self.name = host, name
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.pending: list = []             # callbacks for the command blocks still to come, in order
        self.block: list[str] | None = None
        self.ready = threading.Event()
        self.pane: str | None = None
        self.rows: list[str] = []           # the last frame's rows, as JSON (for the diff)
        self.full = True                    # the next frame sends every row
        self.dirty = True
        self.in_flight = False
        self.last_frame = 0.0
        self.offset = 0                     # lines scrolled back into the history (0 = live)
        self.hidden = False
        self.closed = False
        self.seq = itertools.count(1)
        self.size = (0, 0)
        self.want: tuple[int, int] | None = None     # the view's own size (terminal.resize), re-applied on attach

    # the tmux argv on the session's host
    def _argv(self) -> list[str]:
        cockpit = self.api.cockpit
        if self.host == short_host():
            return [cockpit.tmux.binary, "-L", cockpit.tmux.socket, "-C", "attach", "-t", "=" + self.name]
        r = self.api.remote(self.host)
        return remote_mod.ssh_argv(cockpit.paths, r, remote_mod.remote_tmux(cockpit.paths, r), "-L",
                                   remote_mod.REMOTE_TMUX_SOCKET, "-C", "attach", "-t", "=" + self.name)

    def start(self, timeout: float = 15.0) -> None:
        self.ready.clear()
        self.pane = None
        self.proc = subprocess.Popen(self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1, errors="replace",
                                     start_new_session=True)
        proc = self.proc
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        if not self.ready.wait(timeout):
            self._stop_proc()
            raise ApiError("no_session", f"{self.host}:{self.name}: no such session (or no answer)")
        got = threading.Event()
        found: dict = {}

        def panes(lines, ok):
            for ln in lines:
                role, _, pid = ln.partition(" ")
                if role in ("agent", "claude") and pid:
                    found["pane"] = pid
                found.setdefault("first", pid)
            got.set()
        self.command("list-panes -s -t " + tmux_quote("=" + self.name) + " -F " + tmux_quote("#{@force_role} #{pane_id}"),
                     panes)
        if not got.wait(timeout):
            self._stop_proc()
            raise ApiError("no_session", f"{self.host}:{self.name}: no answer")
        self.pane = found.get("pane") or found.get("first")
        if not self.pane:
            self._stop_proc()
            raise ApiError("no_session", f"{self.host}:{self.name}: no pane")
        if self.want:
            self._apply_size()
        self.full = self.dirty = True

    def _apply_size(self) -> None:
        """The control client takes the view's size: the window follows it as it follows any client (Force's
        `window-size smallest`), until the view is hidden or closed (SPEC §54)."""
        cols, rows = self.want
        self.command(f"refresh-client -C {cols}x{rows}")

    def resize(self, cols: int, rows: int) -> None:
        self.want = (cols, rows)
        if self.proc is not None and self.ready.is_set():
            self._apply_size()
        self.full = self.dirty = True

    def _stop_proc(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.write("detach-client\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        with self.lock:
            self.pending.clear()
            self.in_flight = False

    def command(self, line: str, *callbacks) -> None:
        """Send one command line; each of `callbacks` (`cb(lines, ok)`, or None) gets one of its reply blocks, in
        order (tmux answers a line of `a ; b` with one block per command)."""
        with self.lock:
            proc = self.proc
            if proc is None:
                return
            self.pending.extend(callbacks or (None,))
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def _read(self, proc) -> None:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if self.block is not None:
                if line.startswith("%end ") or line.startswith("%error "):
                    lines, self.block = self.block, None
                    if not self.ready.is_set():
                        continue                        # the attach's own block: we send nothing before it
                    with self.lock:
                        cb = self.pending.pop(0) if self.pending else None
                    if cb is not None:
                        try:
                            cb(lines, line.startswith("%end "))
                        except Exception as e:          # a bad frame must not end the reader
                            print(f"force api: {e}", file=sys.stderr)
                    continue
                self.block.append(line)
                continue
            if line.startswith("%begin "):
                self.block = []
            elif line.startswith("%session-changed"):
                self.ready.set()
                self.dirty = self.full = True
            elif line.startswith("%output "):
                if self.pane and line.split(" ", 2)[1] == self.pane:
                    self.dirty = True
            elif line.startswith("%exit"):
                break
        if proc is self.proc and not self.closed and not self.hidden:
            self.api.send({"event": "terminal.closed", "term": self.term, "reason": "session ended"})
            self.closed = True

    # --- frames
    def maybe_frame(self, now: float) -> None:
        if self.closed or self.hidden or self.proc is None or not self.ready.is_set() or not self.pane:
            return
        if self.in_flight or now - self.last_frame < FRAME_EVERY:
            return
        if not self.dirty and now - self.last_frame < REFRESH_EVERY:
            return
        self.dirty = False
        self.in_flight = True
        self.last_frame = now
        meta: dict = {}

        def got_meta(lines, ok):
            if ok and lines:
                parts = lines[0].split()
                if len(parts) >= 6:
                    meta.update(x=int(parts[0]), y=int(parts[1]), visible=parts[2] == "1",
                                cols=int(parts[3]), rows=int(parts[4]), history=int(parts[5]))

        def got_rows(lines, ok):
            self.in_flight = False
            if ok and meta:
                self._frame(meta, lines)
        fmt = tmux_quote("#{cursor_x} #{cursor_y} #{cursor_flag} #{pane_width} #{pane_height} #{history_size}")
        # scrolled back: from `offset` lines into the history down to the screen's end; the first `rows` lines
        # are the view (the height comes with the first block)
        cap = f"capture-pane -e -p -t {self.pane}" + (f" -S {-self.offset}" if self.offset else "")
        self.command(f"display-message -p -t {self.pane} {fmt} ; {cap}", got_meta, got_rows)

    def _frame(self, meta: dict, lines: list[str]) -> None:
        cols, rows = meta["cols"], meta["rows"]
        lines = (lines + [""] * rows)[:rows]
        self.offset = min(self.offset, meta.get("history", 0))
        encoded = [json.dumps(parse_line(ln), separators=(",", ":")) for ln in lines]
        if self.full or (cols, rows) != self.size or len(self.rows) != rows:
            changed = list(range(rows))
        else:
            changed = [y for y in range(rows) if encoded[y] != self.rows[y]]
        cursor = {"x": meta["x"], "y": meta["y"], "visible": meta["visible"] and self.offset == 0}
        first = self.full
        self.full = False
        self.rows, self.size = encoded, (cols, rows)
        if not changed and not first and cursor == getattr(self, "_cursor", None):
            return
        self._cursor = cursor
        self.api.send({"event": "terminal.screen", "term": self.term, "seq": next(self.seq), "cols": cols,
                       "rows": rows, "cursor": cursor, "offset": self.offset,
                       "lines": [{"y": y, "runs": json.loads(encoded[y])} for y in changed]})

    # --- requests
    def input(self, keys: list) -> None:
        if not self.pane or self.proc is None:
            raise ApiError("bad_request", "the view is not shown")
        target = self.pane
        cmds = []
        plain: list[str] = []

        def flush():
            if plain:
                cmds.append(f"send-keys -t {target} " + " ".join(plain))
                plain.clear()
        for k in keys:
            if isinstance(k, str):
                if not _KEY.match(k):
                    raise ApiError("bad_request", f"not a key name: {k!r}")
                plain.append(tmux_quote(k))
            elif isinstance(k, dict) and isinstance(k.get("text"), str):
                flush()
                text = k["text"]
                if "\n" in text or "\r" in text:
                    # a paste: bracketed when the app asked for it, newlines kept (as `force prompt`, §43.1)
                    cmds.append("set-buffer -b force-api " + tmux_quote(text))
                    cmds.append(f"paste-buffer -p -r -d -b force-api -t {target}")
                elif text:
                    cmds.append(f"send-keys -t {target} -l " + tmux_quote(text))
            else:
                raise ApiError("bad_request", "keys: tmux key names or {\"text\": …}")
        flush()
        if self.offset:                         # typing goes back to the live screen
            self.offset = 0
            self.full = True
        for c in cmds:
            self.command(c)
        self.dirty = True

    def scroll(self, lines: int) -> None:
        self.offset = 0 if lines == 0 else max(0, self.offset - lines)
        self.full = self.dirty = True

    def set_hidden(self, hidden: bool) -> None:
        if hidden == self.hidden:
            return
        self.hidden = hidden
        if hidden:
            self._stop_proc()
        else:
            self.start()

    def switch(self, host: str, name: str) -> None:
        self._stop_proc()
        self.host, self.name, self.offset = host, name, 0
        self.rows = []
        self.start()

    def close(self) -> None:
        self.closed = True
        self._stop_proc()


# --- the whole Force app in a pty (vt mode, SPEC §54) -----------------------------------------------------------
_CTTY_EXEC = ("import os, sys, fcntl, termios; os.setsid(); fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
              "os.execvp(sys.argv[1], sys.argv[1:])")
class AppTerminal:
    """Force's own ui (`force --no-intro --glyphs rich`, the same program as in any terminal) in a pseudo-terminal of
    this process: its output goes to the frontend as `terminal.data` (base64), the frontend's emulator draws it and
    sends keys back as bytes. Every key, menu, dialog and host works as in a terminal, because it is that program."""

    def __init__(self, api: "Api", term: int, cols: int, rows: int, argv: list[str]):
        self.api, self.term = api, term
        self.cols, self.rows = cols, rows
        self.closed = False
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        master, slave = os.openpty()
        self.tty = os.ttyname(slave)                    # the app's tmux client is this tty on the ui server
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env.update(TERM="xterm-256color", COLORTERM="truecolor")
        # a fresh helper (single-threaded: no fork of this multi-threaded process by hand) makes a new session and the
        # pty its controlling terminal, then becomes the app: tmux's client needs that terminal (and SIGWINCH)
        try:
            self.proc = subprocess.Popen([sys.executable, "-I", "-c", _CTTY_EXEC, *argv], env=env, stdin=slave,
                                         stdout=slave, stderr=slave, close_fds=True)
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)                             # the app holds it now: its end is our read's end
        self.pid, self.fd = self.proc.pid, master
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:                             # EIO: the app ended and the pty is gone
                break
            if not data:
                break
            self.api.send({"event": "terminal.data", "term": self.term,
                           "data": base64.b64encode(data).decode("ascii")})
        if not self.closed:
            self.closed = True
            self.api.send({"event": "terminal.closed", "term": self.term, "reason": "Force ended"})
        self._reap()

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ApiError("bad_request", "the app has ended")
        try:
            os.write(self.fd, data)
        except OSError as e:
            raise ApiError("bad_request", str(e))

    def resize(self, cols: int, rows: int) -> None:
        """The pty's size: the kernel tells the app (SIGWINCH), and tmux redraws for it."""
        self.cols, self.rows = cols, rows
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def set_hidden(self, hidden: bool) -> None:
        """Hidden, the app keeps running (shown again at once, as it was) but its ui window says so (@force_hidden),
        and the daemon does not count the session it shows as viewed (SPEC §54)."""
        ui = self.api.cockpit.ui_tmux
        try:
            tty = self.tty
            rc, out, _err = ui._run_rc(["list-clients", "-F", "#{client_tty}\t#{window_id}"])
            for line in out.splitlines() if rc == 0 else []:
                ctty, _, window = line.partition("\t")
                if ctty == tty and window:
                    if hidden:
                        ui._run_rc(["set-option", "-w", "-t", window, "@force_hidden", "1"])
                    else:
                        ui._run_rc(["set-option", "-wu", "-t", window, "@force_hidden"])
        except Exception as e:                          # pragma: no cover (the ui server went away)
            print(f"force api: hidden: {e}", file=sys.stderr)

    def close(self) -> None:
        if self.closed and self.fd is None:
            return
        self.closed = True
        try:
            os.killpg(self.pid, signal.SIGHUP)          # the app and its tmux client
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self._reap()

    def _reap(self) -> None:
        self.proc.poll()


# --- the server ------------------------------------------------------------------------------------------------
class Api:
    def __init__(self, cockpit, inp=None, out=None, run=None, app_argv: list[str] | None = None):
        self.cockpit = cockpit
        # the app a vt view runs: this install's own Force ui (tests give another program)
        self.app_argv = app_argv or [config.launcher_path(), "--no-intro", "--glyphs", "rich"]
        self.inp = inp if inp is not None else sys.stdin
        self.out = out if out is not None else sys.stdout
        self.run = run or subprocess.run
        self.wlock = threading.Lock()
        self.hello = False
        self.subs: set[str] = set()
        self.terms: dict[int, CellsTerminal] = {}
        self.next_term = itertools.count(1)
        self.stop = threading.Event()
        self.poller: threading.Thread | None = None
        self.framer: threading.Thread | None = None

    def send(self, obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        with self.wlock:
            try:
                self.out.write(line + "\n")
                self.out.flush()
            except (OSError, ValueError):
                self.stop.set()

    def reply(self, rid, ok: dict) -> None:
        self.send({"id": rid, "ok": ok})

    def error(self, rid, code: str, message: str) -> None:
        self.send({"id": rid, "error": {"code": code, "message": message}})

    def remote(self, host: str):
        for r in remote_mod.load(self.cockpit.paths):
            if r.name == host and r.enabled:
                return r
        raise ApiError("no_route", f"no host {host!r} (force host list)")

    def address(self, addr) -> tuple[str, str]:
        if not isinstance(addr, str) or not addr:
            raise ApiError("bad_request", "session: HOST:NAME or NAME")
        host, _, name = addr.rpartition(":")
        here = short_host()
        host = host or here
        if host != here:
            self.remote(host)
        if not name:
            raise ApiError("bad_request", "session: HOST:NAME or NAME")
        return host, name

    # --- the loop
    def serve(self) -> int:
        try:
            for raw in self.inp:
                if self.stop.is_set():
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    self.error(None, "bad_request", "not JSON")
                    continue
                if not isinstance(msg, dict):
                    self.error(None, "bad_request", "not an object")
                    continue
                self.dispatch(msg)
        finally:
            self.stop.set()
            for t in list(self.terms.values()):
                t.close()
        return 0

    def dispatch(self, msg: dict) -> None:
        rid, op = msg.get("id"), msg.get("op")
        try:
            if op == "hello":
                return self.op_hello(rid, msg)
            if not self.hello:
                raise ApiError("bad_request", "hello first")
            handler = {"subscribe": self.op_subscribe, "run": self.op_run,
                       "terminal.open": self.op_term_open, "terminal.switch": self.op_term_switch,
                       "terminal.input": self.op_term_input, "terminal.scroll": self.op_term_scroll,
                       "terminal.resize": self.op_term_resize, "terminal.hidden": self.op_term_hidden,
                       "terminal.close": self.op_term_close}.get(op)
            if handler is None:
                raise ApiError("unknown_op", f"unknown op {op!r}")
            handler(rid, msg)
        except ApiError as e:
            self.error(rid, e.code, str(e))

    def _later(self, rid, fn) -> None:
        """Run `fn()` (-> the ok dict) on its own thread: a slow request never holds up the others."""
        def go():
            try:
                self.reply(rid, fn())
            except ApiError as e:
                self.error(rid, e.code, str(e))
            except Exception as e:      # pragma: no cover (a bug: still answer)
                self.error(rid, "internal", str(e))
        threading.Thread(target=go, daemon=True).start()

    # --- ops
    def op_hello(self, rid, msg) -> None:
        if msg.get("protocol") not in (PROTOCOL,):
            self.error(rid, "protocol", f"this Force speaks protocol {PROTOCOL}")
            self.stop.set()
            return
        self.hello = True
        self.reply(rid, {"protocol": PROTOCOL, "force": updates.current_version() or "dev", "host": short_host(),
                         "features": FEATURES})

    def op_subscribe(self, rid, msg) -> None:
        to = msg.get("to")
        if not isinstance(to, list) or not all(t in ("state", "notices") for t in to):
            raise ApiError("bad_request", 'to: ["state", "notices"]')
        self.subs.update(to)
        self.reply(rid, {"subscribed": sorted(self.subs)})
        if self.poller is None:
            self.poller = threading.Thread(target=self._poll, daemon=True)
            self.poller.start()

    def _poll(self) -> None:
        last = None
        trackers: dict[str, notices_mod.Transitions] = {}
        while not self.stop.is_set():
            try:
                doc, views = view_model(self.cockpit.paths)
            except Exception as e:      # pragma: no cover (a half-written file: try again next round)
                print(f"force api: state: {e}", file=sys.stderr)
                self.stop.wait(STATE_EVERY)
                continue
            if "state" in self.subs:
                body = json.dumps({k: v for k, v in doc.items() if k != "generated_ms"}, sort_keys=True)
                if body != last:
                    last = body
                    self.send({"event": "state", **doc})
            now = time.monotonic()
            for host, vs in views.items():
                tr = trackers.setdefault(host, notices_mod.Transitions())
                for v, event in tr.step(vs, now):
                    if "notices" in self.subs:
                        self.send({"event": "notice", "kind": event, "host": host, "session": v.name,
                                   "address": f"{host}:{v.name}", "state": STATE_WORDS.get(v.state, "unknown"),
                                   "message": notices_mod.message(v, event), "viewed": v.viewed,
                                   "at_ms": int(time.time() * 1000)})
            self.stop.wait(STATE_EVERY)

    def op_run(self, rid, msg) -> None:
        argv = msg.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ApiError("bad_request", "argv: a list of strings")
        if argv[0] not in RUN_ALLOWED:
            raise ApiError("not_allowed", f"{argv[0]!r} is not available here")

        def go():
            try:
                p = self.run([config.launcher_path(), *argv], capture_output=True, text=True, timeout=RUN_TIMEOUT,
                             stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                raise ApiError("timeout", f"no answer within {RUN_TIMEOUT} s")
            except OSError as e:
                raise ApiError("internal", str(e))
            return {"code": p.returncode, "stdout": p.stdout or "", "stderr": p.stderr or ""}
        self._later(rid, go)

    def _term(self, msg):
        t = self.terms.get(msg.get("term"))
        if t is None:
            raise ApiError("bad_request", "no such term")
        return t

    def _ensure_framer(self) -> None:
        if self.framer is None:
            self.framer = threading.Thread(target=self._frames, daemon=True)
            self.framer.start()

    def _frames(self) -> None:
        while not self.stop.is_set():
            now = time.monotonic()
            for t in list(self.terms.values()):
                if isinstance(t, CellsTerminal):
                    t.maybe_frame(now)
            self.stop.wait(FRAME_EVERY / 2)

    def op_term_open(self, rid, msg) -> None:
        mode = msg.get("mode", "cells")
        if mode == "vt" and msg.get("target") == "app":
            cols, rows = msg.get("cols", 120), msg.get("rows", 40)
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (cols, rows)) \
                    or not (20 <= cols <= 500 and 5 <= rows <= 200):
                raise ApiError("bad_request", "cols 20..500, rows 5..200")
            term = next(self.next_term)
            self.terms[term] = AppTerminal(self, term, cols, rows, self.app_argv)
            self.reply(rid, {"term": term, "mode": "vt", "target": "app", "cols": cols, "rows": rows})
            return
        if mode != "cells":
            raise ApiError("bad_request", f"mode {mode!r} is not served (features)")
        host, name = self.address(msg.get("session"))
        term = next(self.next_term)

        def go():
            t = CellsTerminal(self, term, host, name)
            t.start()
            self.terms[term] = t
            self._ensure_framer()
            return {"term": term, "session": f"{host}:{name}", "mode": "cells"}
        self._later(rid, go)

    def op_term_switch(self, rid, msg) -> None:
        t = self._term(msg)
        if isinstance(t, AppTerminal):
            raise ApiError("bad_request", "vt mode: the app switches itself")
        host, name = self.address(msg.get("session"))

        def go():
            t.switch(host, name)
            return {"term": t.term, "session": f"{host}:{name}"}
        self._later(rid, go)

    def op_term_input(self, rid, msg) -> None:
        t = self._term(msg)
        if isinstance(t, AppTerminal):
            try:
                data = base64.b64decode(msg.get("data") or "", validate=True)
            except (ValueError, TypeError):
                raise ApiError("bad_request", "data: base64 bytes")
            t.write(data)
            self.reply(rid, {})
            return
        keys = msg.get("keys")
        if not isinstance(keys, list):
            raise ApiError("bad_request", "keys: a list")
        t.input(keys)
        self.reply(rid, {})

    def op_term_scroll(self, rid, msg) -> None:
        t = self._term(msg)
        if isinstance(t, AppTerminal):
            raise ApiError("bad_request", "vt mode: the app scrolls itself")
        lines = msg.get("lines")
        if not isinstance(lines, int) or isinstance(lines, bool):
            raise ApiError("bad_request", "lines: a whole number")
        t.scroll(lines)
        self.reply(rid, {})

    def op_term_resize(self, rid, msg) -> None:
        t = self._term(msg)
        cols, rows = msg.get("cols"), msg.get("rows")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (cols, rows)) \
                or not (20 <= cols <= 500 and 5 <= rows <= 200):
            raise ApiError("bad_request", "cols 20..500, rows 5..200")
        t.resize(cols, rows)
        self.reply(rid, {"cols": cols, "rows": rows})

    def op_term_hidden(self, rid, msg) -> None:
        t = self._term(msg)
        if isinstance(t, AppTerminal):
            hidden = msg.get("hidden")
            if not isinstance(hidden, bool):
                raise ApiError("bad_request", "hidden: true or false")
            t.set_hidden(hidden)
            self.reply(rid, {})
            return
        hidden = msg.get("hidden")
        if not isinstance(hidden, bool):
            raise ApiError("bad_request", "hidden: true or false")
        self._later(rid, lambda: (t.set_hidden(hidden), {})[1])

    def op_term_close(self, rid, msg) -> None:
        t = self._term(msg)
        self.terms.pop(t.term, None)
        t.close()
        self.reply(rid, {})


def serve(cockpit) -> int:
    """`force api`: until stdin closes."""
    return Api(cockpit).serve()
