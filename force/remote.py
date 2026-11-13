"""Remote hosts (SPEC §40, multi-host phase 2/3): the hosts file, SSH options, the `force watch` stream a
host serves, and the watchers the local daemon keeps for every enabled remote.

A remote is another machine running Force (its host part: tmux `force` + daemon). The local ui shows one host
at a time (D1): the main pane runs `ssh -t HOST force nested NAME --report ID`, sidebar and header read the
remote's snapshot from a local copy, and every action runs as `ssh HOST force …`. All SSH traffic to a host
shares one connection (ControlMaster), so a command costs one round trip.

Files (local machine):
  ~/.config/force/hosts.json           {"hosts": [{"name":, "ssh":, "enabled":}]}
  <state>/remotes/<name>/snapshot.json the remote's snapshot (as of its last `force watch` line)
  <state>/remotes/<name>/registry.json the remote's registry
  <state>/remotes/<name>/status.json   {"state": "connected"|"offline"|"version", "detail", "at", "version"}
  <state>/ssh/                         ControlMaster sockets
"""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, registry

PROTO = 1                     # bumped when `force watch` or the remote command set changes incompatibly
REMOTE_FORCE = "~/.local/bin/force"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


@dataclass
class Remote:
    name: str
    ssh: str
    enabled: bool = True


def remotes_file(paths) -> Path:
    """The hosts file, next to the settings file (SPEC §2)."""
    return paths.config_file.parent / "hosts.json"


def load(paths) -> list[Remote]:
    """The configured hosts; a missing or broken file is no hosts (broken: a warning)."""
    try:
        data = json.loads(remotes_file(paths).read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        print(f"force: warning: {remotes_file(paths)}: {e}", file=sys.stderr)
        return []
    out = []
    if isinstance(data, dict):
        for r in data.get("hosts") or []:
            if isinstance(r, dict) and isinstance(r.get("name"), str) and isinstance(r.get("ssh"), str) \
                    and NAME_RE.match(r["name"]):
                out.append(Remote(r["name"], r["ssh"], bool(r.get("enabled", True))))
    return out


def save(paths, remotes: list[Remote]) -> None:
    """Write the hosts file; `enabled` is only stored when False."""
    hosts = []
    for r in remotes:
        host = {"name": r.name, "ssh": r.ssh}
        if not r.enabled:
            host["enabled"] = False
        hosts.append(host)
    path = remotes_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"hosts": hosts}, indent=2) + "\n")
    os.replace(tmp, path)


def get(paths, name: str) -> Remote | None:
    return next((r for r in load(paths) if r.name == name), None)


def remote_dir(paths, name: str) -> Path:
    return paths.state / "remotes" / name


def ssh_opts(paths) -> list[str]:
    """One shared connection per host (ControlMaster), kept 10 minutes after the last use; keepalives so a
    dead link is noticed; never a password prompt."""
    ctl = paths.state / "ssh"
    ctl.mkdir(mode=0o700, parents=True, exist_ok=True)
    return ["-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}/%C", "-o", "ControlPersist=10m",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10"]


def _remote_word(a: str) -> str:
    if a.startswith("~/"):
        return "~/" + (shlex.quote(a[2:]) if a[2:] else "")
    return shlex.quote(a)


def ssh_argv(paths, r: Remote, *remote_argv: str, tty: bool = False) -> list[str]:
    """`ssh [opts] DEST <remote command>`; remote words are quoted for the remote shell, except a leading
    `~/` which the remote shell expands (the rest of such a word is quoted: `~/'my dir'`)."""
    cmd = " ".join(_remote_word(a) for a in remote_argv)
    return ["ssh", *ssh_opts(paths), *(["-t"] if tty else []), r.ssh, cmd]


REMOTE_TMUX_SOCKET = "force"                                   # a remote runs its live Force
# where a remote's tmux is: Force's own when its release ships one (an install by the one-line installer may have no
# other), else the one on PATH. Asked once over ssh and kept (an absolute path: tmux escapes `$` in option values,
# so a variable in @force_host_tmux would never reach the remote shell).
_TMUX_PROBE = "sh -c 'p=$HOME/.local/share/force/app/share/tmux-bin/tmux; [ -x \"$p\" ] && echo \"$p\" || command -v tmux'"


def remote_tmux(paths, r: Remote, run=subprocess.run) -> str:
    """The remote's tmux binary (cached in <state>/remotes/<name>/tmux); "tmux" when it cannot be asked."""
    cache = remote_dir(paths, r.name) / "tmux"
    try:
        v = cache.read_text().strip()
        if v.startswith("/"):
            return v
    except OSError:
        pass
    try:
        out = run(["ssh", *ssh_opts(paths), r.ssh, _TMUX_PROBE], capture_output=True, text=True, timeout=20,
                  stdin=subprocess.DEVNULL)
        v = (out.stdout or "").strip().splitlines()[-1:] if out.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        v = []
    if v and v[0].startswith("/") and " " not in v[0]:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(v[0] + "\n")
        except OSError:
            pass
        return v[0]
    return "tmux"


def host_tmux_prefix(paths, r: Remote) -> str:
    """The ui window option @force_host_tmux for a remote (whitespace-separated argv prefix): its own tmux, on its
    live server (`force`, also from a development instance here, SPEC §2.1)."""
    return " ".join(["ssh", *ssh_opts(paths), r.ssh, remote_tmux(paths, r), "-L", REMOTE_TMUX_SOCKET])


# --------------------------------------------------------------------------- the host side ------------
def _release_of(app) -> str | None:
    """The release the `app` link points at, or None while it is missing or being swapped (an upgrade replaces the
    link; macOS's realpath can raise EINVAL in that moment): the next check sees the new one."""
    try:
        return os.path.realpath(app) if os.path.lexists(app) else None
    except OSError:
        return None


def watch(paths, release: str | None, ui_mode: str, interval: float = 0.25, heartbeat: float = 2.0,
          out=sys.stdout, inp=sys.stdin) -> int:
    """`force watch` (on a host): a hello line, then {"snapshot", "registry"} whenever the snapshot changes
    (and every `heartbeat` seconds: the reader takes a copy older than 5 s for a dead link). Ends when the
    reader goes away. A first stdin line {"peer": NAME} (SPEC §52) marks NAME as a peer that is watching this
    machine; its queued messages are streamed over this connection as {"prompt": {…}} lines."""
    from . import messages
    peer: str | None = None

    def _hello_peer():
        nonlocal peer
        try:
            line = inp.readline()
        except (OSError, ValueError):
            return
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if isinstance(msg, dict) and isinstance(msg.get("peer"), str) and messages.HOST_RE.match(msg["peer"]):
            messages.mark_peer(paths, msg["peer"], os.getpid())
            peer = msg["peer"]

    if inp is not None:
        threading.Thread(target=_hello_peer, daemon=True).start()
    try:
        out.write(json.dumps({"hello": {"proto": PROTO, "version": release, "host": socket.gethostname(),
                                        "ui": ui_mode}}) + "\n")
        out.flush()
        last, beat = None, 0.0
        app = paths.home / "app"
        live = _release_of(app)
        while True:
            now_live = _release_of(app)
            if live and now_live and now_live != live:
                return 0            # upgraded: the reader reconnects to the new version (its hello)
            try:
                st = os.stat(paths.snapshot)
                key = (st.st_mtime_ns, st.st_size)
            except OSError:
                key = None
            now = time.monotonic()
            if key is not None and (key != last or now - beat >= heartbeat):
                try:
                    snap = json.loads(paths.snapshot.read_text())
                    try:
                        reg = json.loads(paths.registry.read_text())
                    except (OSError, ValueError):
                        reg = {"sessions": []}
                except (OSError, ValueError):
                    time.sleep(interval)
                    continue
                out.write(json.dumps({"snapshot": snap, "registry": reg}, separators=(",", ":")) + "\n")
                out.flush()
                last, beat = key, now
            if peer:
                for item in messages.drain(paths, peer):
                    out.write(json.dumps({"prompt": item}) + "\n")
                    out.flush()
            time.sleep(interval)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    finally:
        if peer:
            messages.unmark_peer(paths, peer, os.getpid())


# --------------------------------------------------------------------------- the client side ----------
def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


def write_status(paths, name: str, state: str, detail: str = "", version: str | None = None) -> None:
    _write_json(remote_dir(paths, name) / "status.json",
                {"state": state, "detail": detail, "at": time.time(), "version": version})


def read_status(paths, name: str) -> dict:
    try:
        return json.loads((remote_dir(paths, name) / "status.json").read_text())
    except (OSError, ValueError):
        return {"state": "unknown"}


class Watcher:
    """One `ssh HOST force watch` for a remote, its lines written to the remote's local files. restart() is
    called by the daemon every tick; it reconnects with a growing pause (2 s .. 60 s) after a drop."""

    def __init__(self, paths, r: Remote):
        self.paths, self.r = paths, r
        self.proc: subprocess.Popen | None = None
        self.next_try = 0.0
        self.backoff = 2.0
        self.hello_host: str | None = None      # the remote's own host name, from its hello

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=3)      # reap it: no zombie, and poll() reports it gone
                except subprocess.TimeoutExpired:
                    pass
        self.proc = None

    def ensure(self, now: float) -> None:
        if self.alive() or now < self.next_try:
            return
        from . import messages
        argv = ssh_argv(self.paths, self.r, REMOTE_FORCE, "watch")
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            write_status(self.paths, self.r.name, "offline", str(e))
            self._later(now)
            return
        try:
            self.proc.stdin.write(json.dumps({"peer": messages.short_host()}) + "\n")
            self.proc.stdin.flush()
        except OSError:
            pass                    # the remote never reads it: no peer (SPEC §52)
        threading.Thread(target=self._read, args=(self.proc, now), daemon=True).start()

    def _later(self, now: float) -> None:
        self.next_try = now + self.backoff
        self.backoff = min(self.backoff * 2, 60.0)

    def _deliver(self, prompt: object) -> None:
        """A {"prompt": {…}} line from a peer (SPEC §52): run `force prompt NAME - --from SENDER` here, never
        blocking the reader. Bad items are ignored; the receiving prompt_command logs the outcome itself."""
        from . import messages
        if not isinstance(prompt, dict):
            return
        to, frm, text = prompt.get("to"), prompt.get("from"), prompt.get("text")
        if not (isinstance(to, str) and registry.valid_name(to)
                and isinstance(frm, str) and isinstance(text, str) and 1 <= len(text) <= 100_000):
            return
        try:
            _session, frm_host = messages.parse_from(frm)
        except Exception:
            return
        # the sender must be the host this connection reaches (its name here or the name its hello gave):
        # a host cannot speak for another one over its own watch connection
        if frm_host not in {self.r.name, (self.hello_host or "").split(".")[0]}:
            return
        def _run():
            try:
                subprocess.run([config.launcher_path(), "prompt", to, "-", "--from", frm],
                              input=text, text=True, capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _read(self, proc: subprocess.Popen, started: float) -> None:
        d = remote_dir(self.paths, self.r.name)
        version = None
        ok = False
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "hello" in msg:
                h = msg["hello"] or {}
                version = h.get("version")
                if h.get("proto") != PROTO:
                    write_status(self.paths, self.r.name, "version",
                                 f"protocol {h.get('proto')} there, {PROTO} here: force host upgrade {self.r.name}",
                                 version)
                    proc.terminate()
                    break
                ok = True
                self.backoff = 2.0
                self.hello_host = h.get("host") if isinstance(h.get("host"), str) else None
                write_status(self.paths, self.r.name, "connected", h.get("host") or "", version)
            elif ok and "snapshot" in msg:
                # stamped with the time it arrived: "stale" then means the link (or the remote's daemon) is
                # silent, whatever the two clocks say
                snap = msg["snapshot"]
                if isinstance(snap, dict):
                    snap["remote_generated_ms"] = snap.get("generated_ms")
                    snap["generated_ms"] = int(time.time() * 1000)
                _write_json(d / "snapshot.json", snap)
                _write_json(d / "registry.json", msg.get("registry") or {"sessions": []})
            elif ok and "prompt" in msg:
                self._deliver(msg["prompt"])
        proc.wait()
        err = (proc.stderr.read() if proc.stderr else "").strip().splitlines()
        if read_status(self.paths, self.r.name).get("state") != "version":
            write_status(self.paths, self.r.name, "offline", err[-1] if err else "connection ended", version)
        self._later(time.monotonic())


class Watchers:
    """The daemon's set of watchers, one per enabled remote."""

    def __init__(self, paths):
        self.paths = paths
        self.by_name: dict[str, Watcher] = {}

    def tick(self) -> None:
        wanted = {r.name: r for r in load(self.paths) if r.enabled}
        for name in list(self.by_name):
            w = self.by_name[name]
            if name not in wanted or wanted[name].ssh != w.r.ssh:
                w.stop()
                del self.by_name[name]
                if name not in wanted and remote_dir(self.paths, name).exists():
                    write_status(self.paths, name, "disabled")
        now = time.monotonic()
        for name, r in wanted.items():
            w = self.by_name.setdefault(name, Watcher(self.paths, r))
            w.ensure(now)

    def stop(self) -> None:
        for w in self.by_name.values():
            w.stop()
