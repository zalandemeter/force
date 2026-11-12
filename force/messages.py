"""Messages between hosts (SPEC §52).

An agent on one host drives a session on another with the §43.1 commands, addressed `HOST:NAME` (or `--host
HOST`). Force's own ssh connection (§40) carries it: a command goes to an enabled remote over `ssh HOST force …`, or is
queued to a host that watches this one (a peer) over its `force watch` connection. A delivered prompt/keys carries a
`--from SESSION@HOST` header; the receiving host refuses it unless its `acceptPromptsFrom` setting lists the sender.

The message log (`<state>/messages.log`) records every cross-host prompt and keys, sent, queued, delivered or refused,
one JSON line each, never the text.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import socket
import time
from pathlib import Path

from . import automation, registry, remote

# a host name: what appears before ':' in a HOST:NAME target, or after '@' in a SENDER. (Longer and more
# permissive than a Force session name — a machine name, not a Force name.)
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# a --from value: an optional SESSION (a Force name, no '@'), then '@', then a host. Composes the receiver's own
# target in the reply header.
FROM_RE = r"^([a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?)?@[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_FROM = re.compile(FROM_RE)


def parse_target(word: str) -> tuple[str | None, str]:
    """`HOST:NAME` -> (HOST, NAME); `NAME` -> (None, NAME) unchecked. With a ':' exactly one, HOST must match
    HOST_RE and NAME be a valid Force session name, else a usage error."""
    if ":" in word:
        host, _, name = word.partition(":")
        if ":" in name or not HOST_RE.match(host) or not registry.valid_name(name):
            raise automation.UsageError(f"bad target: {word!r}")
        return host, name
    return None, word            # a plain name: as today (an unknown one is no_session)


def short_host() -> str:
    """This machine's short host name (before the first '.')."""
    return socket.gethostname().split(".")[0]


def resolve(paths, host: str):
    """Where `host` is: ('local', None) for this machine, ('remote', Remote) for an enabled force remote (named
    directly or by the host name its last hello reported), ('peer', host) for a host that watches this one. Nothing
    else: a no_route error."""
    if host == short_host():
        return ("local", None)
    r = remote.get(paths, host)
    if r is not None and r.enabled:
        return ("remote", r)
    for other in remote.load(paths):
        if other.enabled and remote.read_status(paths, other.name).get("detail", "").split(".")[0] == host:
            return ("remote", other)
    if peer_alive(paths, host):
        return ("peer", host)
    raise automation.CmdError("no_route",
                            f"no route to host {host!r}: not in force host list and not watching this machine")


def sender(cockpit) -> str:
    """`SESSION@HOST`: the calling pane's Force session (§43.2), or just `@HOST`, and this machine's short name."""
    try:
        session = automation._default_report_name(cockpit)
    except automation.CmdError:
        session = ""
    return f"{session}@{short_host()}"


def parse_from(value: str) -> tuple[str, str]:
    """A --from value -> (SESSION or '', HOST); a bad value is a usage error."""
    m = _FROM.match(value)
    if m is None:
        raise automation.UsageError(f"bad --from: {value!r}")
    return m.group(1) or "", value.rsplit("@", 1)[1]


def accepts(cfg, host: str) -> bool:
    """Whether `cfg.accept_prompts_from` (space/comma-separated; '*' = any) lists `host`."""
    items = [w for w in re.split(r"[,\s]+", (cfg.accept_prompts_from or "").strip()) if w]
    return "*" in items or host in items


def header(session: str, host: str) -> str:
    """The line a delivered prompt is prefixed with (§52)."""
    if session:
        return f'[from {session} on {host} · reply: force prompt {host}:{session} "…"]'
    return f"[from {host}]"


def log(paths, **fields) -> None:
    """Append one JSON line to <state>/messages.log: a timestamp and the caller's fields, never the message text.
    Failures are ignored (logging never breaks a command)."""
    try:
        p = paths.state / "messages.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **fields}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- peers and outbox ------------
def peers_dir(paths) -> Path:
    return paths.state / "peers"


def _peer_path(paths, name: str) -> Path:
    return peers_dir(paths) / f"{name}.json"


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def mark_peer(paths, name: str, pid: int) -> None:
    """Record that a live `force watch` (pid) is streaming this machine to `name`."""
    d = peers_dir(paths)
    d.mkdir(parents=True, exist_ok=True)
    p = _peer_path(paths, name)
    tmp = p.with_name(f"{p.name}.{pid}.tmp")
    tmp.write_text(json.dumps({"pid": pid, "at": time.time()}))
    os.replace(tmp, p)


def unmark_peer(paths, name: str, pid: int) -> None:
    """Remove the peer record only when it still holds `pid`."""
    try:
        data = json.loads(_peer_path(paths, name).read_text())
    except (OSError, ValueError):
        return
    if data.get("pid") == pid:
        _unlink(_peer_path(paths, name))


def peer_alive(paths, name: str) -> bool:
    """The peer file exists and its pid is alive (a live `force watch`)."""
    try:
        data = json.loads(_peer_path(paths, name).read_text())
    except (OSError, ValueError):
        return False
    pid = data.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                       # alive but owned by someone else
        return True
    return True


def queue(paths, host: str, item: dict) -> None:
    """Drop a message into <state>/outbox/HOST/ (atomic tmp + replace), to be streamed by a live watch."""
    d = paths.state / "outbox" / host
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{os.getpid()}.json"
    tmp = d / f"{name}.tmp"
    tmp.write_text(json.dumps(item))
    os.replace(tmp, d / name)


def drain(paths, host: str, max_age_s: float = 600, now=None) -> list[dict]:
    """Read queued messages oldest first, delete each one read; items older than max_age_s (item["at"], epoch s) are
    dropped and logged, unreadable files deleted."""
    d = paths.state / "outbox" / host
    try:
        entries = sorted([p for p in d.iterdir() if p.name.endswith(".json")], key=lambda p: p.name)
    except OSError:
        return []
    if now is None:
        now = time.time()
    out: list[dict] = []
    for p in entries:
        try:
            item = json.loads(p.read_text())
        except (OSError, ValueError):
            _unlink(p)
            continue
        at = item.get("at")
        if not isinstance(at, (int, float)) or now - float(at) > max_age_s:
            log(paths, dir="out", cmd="prompt", **{"from": item.get("from")}, to=item.get("to"),
                length=len(item.get("text") or "") if isinstance(item.get("text"), str) else 0,
                result="expired")
            _unlink(p)
            continue
        out.append(item)
        _unlink(p)
    return out
