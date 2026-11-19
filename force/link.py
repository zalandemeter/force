"""The direct link's Python side (SPEC §56.2): the peers this host accepts (`force link trust|untrust`), this
host's link card for the `force watch` hello, pairing with a remote over plain ssh, and the link daemon's command
line (`force link-daemon`). The daemon itself is `force-link daemon` (crates/link).

Files:
  ~/.config/force/link-peers.json    {"peers": [{"id", "name"}]}, 0600: who may connect here
  <state>/link/self.json             {"id", "port", "addrs"}: this host's card, written by the daemon
  <state>/link.sock                  the daemon's local socket
  <state>/remotes/<name>/link.json   {"id", "addrs", "port", "paired_ms"} (pairing, here) + {"state", "reason",
                                     "since_ms", "fails"} (daemon)
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

from . import remote

# a link id: iroh's display form of the host's public key, 64 lowercase hex digits
ID_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_ADDRS = 16


def peers_file(paths) -> Path:
    """Next to the settings file, like hosts.json (SPEC §2)."""
    return paths.config_file.parent / "link-peers.json"


def self_file(paths) -> Path:
    return paths.state / "link" / "self.json"


def sock_file(paths) -> Path:
    return paths.state / "link.sock"


def link_file(paths, name: str) -> Path:
    return remote.remote_dir(paths, name) / "link.json"


def _write_private(path: Path, data: dict) -> None:
    """Atomic write, mode 0600 from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------------------- the peers file --
def read_peers(paths) -> list[dict]:
    """The accepted peers; a missing or broken file is none."""
    try:
        data = json.loads(peers_file(paths).read_text())
    except (OSError, ValueError):
        return []
    peers = data.get("peers") if isinstance(data, dict) else None
    return [p for p in peers or [] if isinstance(p, dict) and isinstance(p.get("id"), str)
            and isinstance(p.get("name"), str)]


def trust(paths, link_id: str, name: str) -> None:
    """Accept `link_id` as host `name`: adds it, replacing an entry with the same name or id. ValueError on a bad
    id or name."""
    if not ID_RE.fullmatch(link_id or ""):
        raise ValueError(f"bad link id {link_id!r} (64 lowercase hex digits)")
    if not remote.NAME_RE.fullmatch(name or ""):
        raise ValueError(f"bad host name {name!r}")
    peers = [p for p in read_peers(paths) if p["name"] != name and p["id"] != link_id]
    peers.append({"id": link_id, "name": name})
    _write_private(peers_file(paths), {"peers": peers})
    ask_reload(paths)


def untrust(paths, who: str) -> bool:
    """Stop accepting the peer named `who` (or with that id). True when one was removed."""
    peers = read_peers(paths)
    keep = [p for p in peers if p["name"] != who and p["id"] != who]
    if len(keep) == len(peers):
        return False
    _write_private(peers_file(paths), {"peers": keep})
    ask_reload(paths)
    return True


def ask_reload(paths, timeout: float = 1.0) -> bool:
    """Ask this host's link daemon to reload link-peers.json now (`{"op":"reload"}` on link.sock), so a peer just
    trusted is accepted at once rather than at the daemon's next 2 s check. True when it answered `ok`. No daemon,
    or an older one (it answers `ssh`), is fine: its check picks the file up."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_file(paths)))
        s.sendall(b'{"v":1,"op":"reload"}\n')
        answer = b""
        while not answer.endswith(b"\n") and len(answer) < 64:
            chunk = s.recv(64)
            if not chunk:
                break
            answer += chunk
        return answer.strip() == b"ok"
    except OSError:
        return False
    finally:
        s.close()


def link_command(paths, action: str, args: list[str]) -> int:
    """`force link trust ID NAME | untrust NAME` (internal: run over ssh by the pairing host)."""
    try:
        if action == "trust" and len(args) == 2:
            trust(paths, args[0], args[1])
        elif action == "untrust" and len(args) == 1:
            untrust(paths, args[0])
        else:
            print("usage: force link trust ID NAME | untrust NAME", file=sys.stderr)
            return 2
    except ValueError as e:
        print(f"force: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"force: {peers_file(paths)}: {e}", file=sys.stderr)
        return 1
    print("ok")
    return 0


# ------------------------------------------------------------------------------------ this host's card --
def _valid_addr(a) -> bool:
    """`ip:port`, `[v6]:port` or a bare ip."""
    if not isinstance(a, str) or not 0 < len(a) <= 64:
        return False
    host, port = a, None
    if a.startswith("["):
        host, sep, rest = a[1:].partition("]")
        if not sep or not rest.startswith(":"):
            return False
        port = rest[1:]
    elif a.count(":") == 1:
        host, port = a.split(":")
    if port is not None and not (port.isdigit() and 0 < int(port) < 65536):
        return False
    try:
        ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return True


def valid_card(card) -> dict | None:
    """A link card `{"id", "port", "addrs"}` with a valid id, port and at most MAX_ADDRS valid addresses (the bad
    ones dropped), else None."""
    if not isinstance(card, dict) or not isinstance(card.get("id"), str) or not ID_RE.fullmatch(card["id"]):
        return None
    port = card.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        return None
    addrs = card.get("addrs")
    if not isinstance(addrs, list):
        return None
    return {"id": card["id"], "port": port, "addrs": [a for a in addrs if _valid_addr(a)][:MAX_ADDRS]}


def daemon_running(paths) -> bool:
    """The link daemon answers on its socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(str(sock_file(paths)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def self_card(paths) -> dict | None:
    """This host's card from self.json (written by its daemon), or None."""
    try:
        return valid_card(json.loads(self_file(paths).read_text()))
    except (OSError, ValueError):
        return None


def hello_link(paths, cfg) -> dict | None:
    """What `force watch`'s hello carries as `link`: the card, while the daemon runs and directLink is on."""
    if not cfg.direct_link or not daemon_running(paths):
        return None
    return self_card(paths)


def my_name() -> str:
    """This host's name in a peer's link-peers.json: its short host name, made a valid host name."""
    short = socket.gethostname().split(".")[0]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", short).lstrip("._-")[:32]
    return name or "host"


def daemon_argv(paths, cfg) -> list[str]:
    """`force-link daemon --state … --hosts … --peers … --port <linkPort>`."""
    return [str(remote.link_bin(paths)), "daemon", "--state", str(paths.state),
            "--hosts", str(remote.remotes_file(paths)), "--peers", str(peers_file(paths)),
            "--port", str(cfg.link_port)]


def link_daemon_command(paths, cfg) -> int:
    """`force link-daemon` (the service's command): exec the link daemon with this install's paths and settings.
    Exit 0 (the service then stays down, SPEC §12) with directLink off and in a release without force-link."""
    if not cfg.direct_link:
        print("force: directLink is off: no link daemon (force settings set directLink true)")
        return 0
    argv = daemon_argv(paths, cfg)
    if not os.access(argv[0], os.X_OK):
        print(f"force: {argv[0]} is missing: this release has no direct link", file=sys.stderr)
        return 0
    os.execv(argv[0], argv)
    return 1                                                         # not reached


# ------------------------------------------------- the link daemon without a service manager (SPEC §46) --
def lock_file(paths) -> Path:
    """The daemon's lock (crates/link): held while it runs, its pid inside."""
    return paths.state / "link" / "lock"


def log_file(paths) -> Path:
    return paths.state / "link.log"


def release_has_link(rel) -> bool:
    """The release ships the link daemon: its force-link program and the `force link-daemon` command."""
    rel = Path(rel)
    return os.access(rel / "share" / "bin" / "force-link", os.X_OK) and (rel / "force" / "link.py").is_file()


def _lock_held(path: Path) -> bool:
    """Another process holds the flock on `path`. Never creates the file and never blocks."""
    import fcntl
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def daemon_pid(paths) -> int | None:
    """The running link daemon's pid (the one in its lock file), else None. A daemon that wrote no pid (older
    force-link) is found by its command line: this user's `force-link daemon --state <state> …`."""
    lock = lock_file(paths)
    if not _lock_held(lock):
        return None
    try:
        text = lock.read_text().strip()
    except OSError:
        text = ""
    if text.isdigit():
        return int(text)
    from . import procinfo
    state = str(paths.state)
    for pid in procinfo.pids():
        if procinfo.uid(pid) != os.getuid():
            continue
        argv = procinfo.cmdline(pid) or []
        if len(argv) >= 4 and os.path.basename(argv[0]) == "force-link" and argv[1] == "daemon" \
                and "--state" in argv and argv[argv.index("--state") + 1:argv.index("--state") + 2] == [state]:
            return pid
    return None


def start_daemon(paths, cfg, launcher: str) -> bool:
    """Start `force link-daemon` detached (output appended to <state>/link.log) where no service manager runs it:
    with directLink on, when the release has force-link and no link daemon runs yet (its lock is free; a second
    one would exit at once anyway). True when one was started."""
    if not cfg.direct_link or not os.access(remote.link_bin(paths), os.X_OK) or _lock_held(lock_file(paths)):
        return False
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        log = open(log_file(paths), "a")
    except OSError:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen([launcher, "link-daemon"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        return True
    except OSError:
        return False
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def stop_daemon(paths, wait: float = 5.0, sleep=None) -> bool:
    """Stop the running link daemon (SIGTERM to the pid in its lock) and wait up to `wait` seconds for its lock to
    be free. True when none runs afterwards."""
    import signal
    import time
    pid = daemon_pid(paths)
    if pid is None:
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    nap = time.sleep if sleep is None else sleep
    for _ in range(max(1, int(wait / 0.1))):
        if not _lock_held(lock_file(paths)):
            return True
        nap(0.1)
    return not _lock_held(lock_file(paths))


def restart_unmanaged(paths, cfg, launcher: str, on: bool) -> str:
    """Setup without a service manager (an update, SPEC §46): a link daemon Force started itself is restarted, so
    the new release's force-link runs (stopped for good with `on` false). One that does not run is left for
    `force` to start. Returns "restarted" | "stopped" | "none"."""
    if daemon_pid(paths) is None:
        return "none"
    stop_daemon(paths)
    if on and start_daemon(paths, cfg, launcher):
        return "restarted"
    return "stopped"


def forget_local(paths) -> None:
    """`force uninstall`: this host's link identity and socket go (<state>/link/, <state>/link.sock)."""
    import shutil
    shutil.rmtree(paths.state / "link", ignore_errors=True)
    try:
        sock_file(paths).unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------------------------- pairing --
def read_link(paths, name: str) -> dict:
    try:
        data = json.loads(link_file(paths, name).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_link(paths, name: str, card: dict, now_ms: int | None = None) -> None:
    """Record a remote's card: id, addrs, port. The daemon's state fields stay while the id is the same; a new id
    starts afresh with `paired_ms` (when it was recorded, unix ms), which tells the daemon that a refused dial
    may only mean the remote has not reloaded its peers yet (SPEC §56.2 dialing)."""
    old = read_link(paths, name)
    if old.get("id") == card["id"]:
        data = dict(old)
    else:
        import time
        data = {"paired_ms": int(time.time() * 1000) if now_ms is None else now_ms}
    data.update({"id": card["id"], "addrs": card["addrs"], "port": card["port"]})
    if data != old:
        _write_private(link_file(paths, name), data)


def pair(paths, r, card: dict, run=None) -> bool:
    """Pair with remote `r` whose hello carried `card`: over plain ssh (never the link), make the remote accept
    this host's id (`force link trust ID NAME` there), then record the remote's id here. Needs this host's own
    card (its daemon has run); without one nothing happens."""
    me = self_card(paths)
    if me is None:
        return False
    argv = remote.ssh_argv(paths, r, remote.REMOTE_FORCE, "link", "trust", me["id"], my_name())
    try:
        res = (run or subprocess.run)(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if res.returncode != 0 or (res.stdout or "").strip() != "ok":
        return False
    write_link(paths, r.name, card)
    return True


def on_hello(paths, r, link, cfg, run=None) -> str:
    """A remote's hello: its `link` card (or none). Records the addresses when the pair is known, pairs when it
    is not (or the remote's id changed). Returns what it did: "none" | "updated" | "paired" | "unpaired"."""
    card = valid_card(link)
    if card is None or not cfg.direct_link:
        return "none"
    if read_link(paths, r.name).get("id") == card["id"]:
        write_link(paths, r.name, card)
        return "updated"
    return "paired" if pair(paths, r, card, run=run) else "unpaired"


def unpair(paths, r, run=None) -> None:
    """`force host remove`: the remote stops accepting this host (`force link untrust NAME` there, over plain
    ssh, best effort) and this host forgets the remote's id."""
    if link_file(paths, r.name).exists():
        try:
            (run or subprocess.run)(remote.ssh_argv(paths, r, remote.REMOTE_FORCE, "link", "untrust", my_name()),
                capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        link_file(paths, r.name).unlink()
    except OSError:
        pass


# ------------------------------------------------------------------- what the link does: hint, column --
HINT_FAILS = 2          # dial timeouts in a row before the `force net up` hint (SPEC §56.2)


def _port(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 65536 else None


def _ssh_only(paths, name: str) -> dict | None:
    """The file side of the hint for remote `name`: connected, its last hello carried `link`, and its link.json
    says ssh after HINT_FAILS or more dial timeouts in a row. {"host", "port"} (port: the remote's linkPort from
    its card, None when unknown), else None."""
    st = remote.read_status(paths, name)
    if st.get("state") != "connected" or st.get("link") is not True:
        return None
    lj = read_link(paths, name)
    fails = lj.get("fails")
    if lj.get("state") != "ssh" or lj.get("reason") != "timeout" or isinstance(fails, bool) \
            or not isinstance(fails, int) or fails < HINT_FAILS:
        return None
    return {"host": name, "port": _port(lj.get("port"))}


def ssh_only_hosts(paths, cfg, running=None) -> list[dict]:
    """The remotes reachable by ssh only because their direct dial gets no answer (SPEC §56.2): every condition
    of the hint except the netUpHint setting (doctor's `link-ssh-only`). `running(paths)`: whether this machine's
    link daemon answers (default: its socket), asked only when some remote qualifies."""
    if not cfg.direct_link:
        return []
    try:
        names = [r.name for r in remote.load(paths) if r.enabled]
    except Exception:
        return []
    found = [h for h in (_ssh_only(paths, n) for n in names) if h is not None]
    if not found or not (running or daemon_running)(paths):
        return []
    for h in found:
        h["port"] = h["port"] or cfg.link_port
    return found


def link_hints(paths, cfg, running=None) -> list[dict]:
    """The snapshot's `link_hints` (SPEC §56.2): ssh_only_hosts while netUpHint is on."""
    return ssh_only_hosts(paths, cfg, running) if cfg.net_up_hint else []


def link_column(paths, cfg, name: str, running: bool, status: dict | None = None) -> str | None:
    """A remote's link as shown (`force host list`, the host picker, the API): "direct" | "ssh", or None when
    directLink is off, this machine's link daemon does not answer (`running`) or the remote's last hello carried
    no link (no link support there)."""
    if not cfg.direct_link or not running:
        return None
    st = remote.read_status(paths, name) if status is None else status
    if st.get("link") is not True:
        return None
    return "direct" if read_link(paths, name).get("state") == "direct" else "ssh"
