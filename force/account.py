"""Which Claude Code account is logged in, and the record of its last change (SPEC §4, §21).

Claude Code keeps the logged-in account in its global config: `oauthAccount.accountUuid` and `organizationUuid` in
`.claude.json`. Force only ever stores a short hash of the two, never the ids or the e-mail address. The daemon
watches the file (parsed only when it changed) and, when one known account gives way to another, writes
`<state>/account.json` = {"account": hash, "since_ms"}: the status-line script reads it to drop usage figures a
session reports from before the change, and `force smoke` records the hash with each run.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

STATE_FILE = "account.json"
GRACE_MS = 10 * 60_000          # after a change, a Remote Control drop is the change's, not a choice (SPEC §53)


def config_file(claude_dir: Path) -> Path:
    """Claude Code's global config: `.claude.json` inside the config dir when Claude runs with one of its own
    (CLAUDE_CONFIG_DIR), else next to it (`~/.claude.json` for `~/.claude`)."""
    inside = Path(claude_dir) / ".claude.json"
    return inside if inside.is_file() else Path(claude_dir).parent / ".claude.json"


def account_hash(data: object) -> str | None:
    """The logged-in account of a parsed `.claude.json`: sha256 of "accountUuid:organizationUuid", its first 16
    hex digits. None when logged out (no oauthAccount, no accountUuid)."""
    oa = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oa, dict):
        return None
    acc, org = oa.get("accountUuid"), oa.get("organizationUuid")
    if not isinstance(acc, str) or not acc:
        return None
    org = org if isinstance(org, str) else ""
    return hashlib.sha256(f"{acc}:{org}".encode()).hexdigest()[:16]


_UNREADABLE = object()


def _read(path: Path):
    """account_hash of the file, None when logged out or missing, _UNREADABLE when it is not JSON (mid-write)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE
    try:
        return account_hash(json.loads(raw))
    except ValueError:
        return _UNREADABLE


def current(claude_dir: Path) -> str | None:
    """The account logged in now (a full read; `force smoke` uses it once per run)."""
    v = _read(config_file(claude_dir))
    return None if v is _UNREADABLE else v


def read_state(state_dir: Path) -> dict | None:
    """<state>/account.json: {"account": str, "since_ms": int}, or None."""
    try:
        d = json.loads((Path(state_dir) / STATE_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("account"), str):
        return None
    since = d.get("since_ms")
    if isinstance(since, bool) or not isinstance(since, int):
        return None
    return {"account": d["account"], "since_ms": since}


def write_state(state_dir: Path, account: str, since_ms: int) -> None:
    path = Path(state_dir) / STATE_FILE
    tmp = path.with_name(f"{STATE_FILE}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"account": account, "since_ms": int(since_ms)}, f)
    os.replace(tmp, path)


class Watch:
    """The daemon's view of the logged-in account. `poll()` costs one stat while the file is unchanged; when its
    mtime, size or inode changed it parses it (a few ms for a few hundred KB) and returns (old, new) when one known
    account gave way to another. The first read after start is never a change, nor is logging in after being
    logged out of nothing; logged out and back in to the same account is not one either."""

    def __init__(self, claude_dir: Path):
        self.claude_dir = Path(claude_dir)
        self._key = None
        self._started = False
        self.known: str | None = None     # the last account seen logged in
        self.parse_ms: float | None = None

    def poll(self) -> tuple[str, str] | None:
        path = config_file(self.claude_dir)
        try:
            st = os.stat(path)
            key = (str(path), st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            key = None
        if self._started and key == self._key:
            return None
        t0 = time.perf_counter()
        cur = _read(path) if key is not None else None
        self.parse_ms = (time.perf_counter() - t0) * 1000
        self._key = key
        if cur is _UNREADABLE:
            return None                       # half written: read again once it changes
        first, self._started = not self._started, True
        if cur is None:
            return None                       # logged out: the account before it stays known
        if first or self.known is None:
            self.known = cur
            return None
        if cur == self.known:
            return None
        old, self.known = self.known, cur
        return old, cur
