"""The direct link's Python side (SPEC §56.2): the peers this host accepts (`force link trust|untrust`), this
host's link card for the `force watch` hello, pairing with a remote over plain ssh, and the link daemon's command
line (`force link-daemon`). The daemon itself is `force-link daemon` (crates/link).

Files:
  ~/.config/force/link-peers.json    {"peers": [{"id", "name"}]}, 0600: who may connect here
  <state>/link/self.json             {"id", "port", "addrs"}: this host's card, written by the daemon
  <state>/link.sock                  the daemon's local socket
  <state>/remotes/<name>/link.json   {"id", "addrs", "port"} (pairing, here) + {"state", "reason", "since_ms"} (daemon)
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


def untrust(paths, who: str) -> bool:
    """Stop accepting the peer named `who` (or with that id). True when one was removed."""
    peers = read_peers(paths)
    keep = [p for p in peers if p["name"] != who and p["id"] != who]
    if len(keep) == len(peers):
        return False
    _write_private(peers_file(paths), {"peers": keep})
    return True


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
    """`force link-daemon` (the service's command): exec the link daemon with this install's paths and settings."""
    if not cfg.direct_link:
        print("force: directLink is off: no link daemon (force settings set directLink true)")
        return 0
    argv = daemon_argv(paths, cfg)
    if not os.access(argv[0], os.X_OK):
        print(f"force: {argv[0]} is missing: this release has no direct link", file=sys.stderr)
        return 1
    os.execv(argv[0], argv)
    return 1                                                         # not reached


# --------------------------------------------------------------------------------------------- pairing --
def read_link(paths, name: str) -> dict:
    try:
        data = json.loads(link_file(paths, name).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_link(paths, name: str, card: dict) -> None:
    """Record a remote's card: id, addrs, port. The daemon's state fields stay while the id is the same."""
    old = read_link(paths, name)
    data = dict(old) if old.get("id") == card["id"] else {}
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
