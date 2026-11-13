"""Registry of managed sessions (SPEC §3)."""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

NAME_RE = r"^[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?$"

KNOWN_KEYS = ("name", "session_id", "cwd", "created_at", "order", "archived",
              "archived_at", "pending_name")

REQUIRED_KEYS = ("name", "session_id", "cwd", "created_at", "order")


class RegistryError(Exception):
    """Corrupt registry file or invalid operation (duplicate name, unknown name, invalid name)."""


@dataclass
class Entry:
    name: str
    session_id: str
    cwd: str
    created_at: str                      # ISO 8601 with timezone
    order: int
    archived: bool = False
    archived_at: str | None = None
    pending_name: str | None = None
    extra: dict = field(default_factory=dict)   # unknown keys, preserved on rewrite


def is_support(e: Entry) -> bool:
    """Chuck, the support assistant's session (SPEC §50): registry extra role = "support"."""
    return e.extra.get("role") == "support"


@dataclass
class Registry:
    entries: list[Entry]

    def get(self, name: str) -> Entry | None:
        for e in self.entries:
            if e.name == name:
                return e
        return None

    def by_session_id(self, session_id: str) -> Entry | None:
        for e in self.entries:
            if e.session_id == session_id:
                return e
        return None

    def live(self) -> list[Entry]:
        """Non-archived entries sorted by order; Chuck, the support session (SPEC §50), always last."""
        return sorted((e for e in self.entries if not e.archived), key=lambda e: (is_support(e), e.order))

    def archived(self) -> list[Entry]:
        """Archived entries sorted by archived_at descending."""
        return sorted((e for e in self.entries if e.archived),
                      key=lambda e: e.archived_at or "", reverse=True)

    def name_taken(self, name: str) -> bool:
        """True if any entry has this name or this pending_name."""
        return any(e.name == name or e.pending_name == name for e in self.entries)

    def add(self, name: str, session_id: str, cwd: str) -> Entry:
        """Validate name (valid_name, not taken), session_id (uuid), append with order=max+1 (0 if empty),
        created_at=now UTC. Raises RegistryError."""
        if not valid_name(name):
            raise RegistryError(f"invalid name {name!r}")
        if self.name_taken(name):
            raise RegistryError(f"name already taken: {name!r}")
        try:
            uuid.UUID(session_id)
        except (ValueError, AttributeError, TypeError):
            raise RegistryError(f"invalid session_id {session_id!r}")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            raise RegistryError(f"cwd must be an absolute path: {cwd!r}")
        order = max((e.order for e in self.entries), default=-1) + 1
        entry = Entry(
            name=name,
            session_id=session_id,
            cwd=cwd,
            created_at=datetime.now(timezone.utc).isoformat(),
            order=order,
        )
        self.entries.append(entry)
        return entry

    def rename(self, old: str, new: str) -> None:
        """Immediate rename (also clears pending_name). Raises RegistryError on invalid/taken/unknown."""
        entry = self.get(old)
        if entry is None:
            raise RegistryError(f"unknown session {old!r}")
        if not valid_name(new):
            raise RegistryError(f"invalid name {new!r}")
        if any(e.name == new or e.pending_name == new for e in self.entries if e.name != old):
            raise RegistryError(f"name already taken: {new!r}")
        entry.name = new
        entry.pending_name = None

    def remove(self, name: str) -> None:
        for i, e in enumerate(self.entries):
            if e.name == name:
                del self.entries[i]
                return
        raise RegistryError(f"unknown session {name!r}")


def is_shell(entry: Entry) -> bool:
    """True if the entry is a managed SHELL session (extra kind="shell"). Agent sessions have kind
    absent or "claude"."""
    return entry.extra.get("kind") == "shell"


def is_opencode(entry: Entry) -> bool:
    """True for an OpenCode session (extra kind="opencode", SPEC §36). Its OpenCode session id is
    extra["oc_session"] (once known) and its TUI's local API port extra["oc_port"]; `session_id` stays a
    uuid placeholder (registry format)."""
    return entry.extra.get("kind") == "opencode"


def is_claude(entry: Entry) -> bool:
    """True for a Claude Code session (kind absent or "claude")."""
    return entry.extra.get("kind") in (None, "claude")


def remote_control(entry: Entry) -> bool:
    """False for a Claude session created without Remote Control (`force new --no-remote-control`, SPEC §53)."""
    return entry.extra.get("remote_control") is not False


def kind(entry: Entry) -> str:
    """"claude" | "opencode" | "shell" (an unknown kind counts as claude, as before)."""
    k = entry.extra.get("kind")
    return k if k in ("opencode", "shell") else "claude"


def valid_name(name: str) -> bool:
    return isinstance(name, str) and re.match(NAME_RE, name) is not None and len(name) <= 31


def slugify(s: str) -> str | None:
    """SPEC §3 slugify."""
    if not isinstance(s, str):
        return None
    out = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    out = out[:31].rstrip("-")
    if not out:
        return None
    return out


def _entry_from_dict(d: dict) -> Entry:
    if not isinstance(d, dict):
        raise RegistryError("session entry is not an object")
    missing = [k for k in REQUIRED_KEYS if k not in d]
    if missing:
        raise RegistryError(f"session entry missing keys: {', '.join(missing)}")
    return Entry(
        name=d["name"],
        session_id=d["session_id"],
        cwd=d["cwd"],
        created_at=d["created_at"],
        order=d["order"],
        archived=d.get("archived", False),
        archived_at=d.get("archived_at"),
        pending_name=d.get("pending_name"),
        extra={k: v for k, v in d.items() if k not in KNOWN_KEYS},
    )


def _dump(reg: Registry) -> str:
    data = {"version": 1, "sessions": []}
    for entry in reg.entries:
        d = {
            "name": entry.name,
            "session_id": entry.session_id,
            "cwd": entry.cwd,
            "created_at": entry.created_at,
            "order": entry.order,
            "archived": entry.archived,
            "archived_at": entry.archived_at,
            "pending_name": entry.pending_name,
        }
        d.update(entry.extra)
        data["sessions"].append(d)
    return json.dumps(data, indent=2)


def load(path: Path) -> Registry:
    """Missing file -> empty Registry. Bad JSON / wrong shape -> RegistryError."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return Registry([])
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        _backup_corrupt(path, raw)
        raise RegistryError(f"corrupt registry {path}: {e}")
    if not isinstance(data, dict) or "sessions" not in data or not isinstance(data["sessions"], list):
        _backup_corrupt(path, raw)
        raise RegistryError(f"corrupt registry {path}: bad top-level shape")
    entries = [_entry_from_dict(d) for d in data["sessions"]]
    return Registry(entries)


def save(path: Path, reg: Registry) -> None:
    """Atomic write (tmp + fsync + os.replace), JSON indent=2, keys of each entry in Entry field order
    followed by extra keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _dump(reg)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _snapshot(path, text)


SNAPSHOT_INTERVAL_S = 15 * 60
SNAPSHOTS_KEEP = 48
BACKUPS_KEEP = 3


def _stamp() -> str:
    return time.strftime("registry-%Y%m%d-%H%M%S.json", time.localtime())


def _snapshot(path: Path, text: str) -> None:
    """History copy of the saved registry (SPEC §43.5); never raises."""
    try:
        d = path.parent / "registry-snapshots"
        d.mkdir(parents=True, exist_ok=True)
        files = sorted(p for p in d.iterdir() if p.name.startswith("registry-") and p.name.endswith(".json"))
        if files:
            newest = files[-1]
            if time.time() - newest.stat().st_mtime < SNAPSHOT_INTERVAL_S:
                return
            if newest.read_text() == text:
                return
        target = d / _stamp()
        if target.exists():
            return
        target.write_text(text)
        files = sorted(files + [target])
        for old in files[:-SNAPSHOTS_KEEP]:
            old.unlink(missing_ok=True)
    except (OSError, UnicodeError):
        pass


def _backup_corrupt(path: Path, raw: str) -> None:
    """Keep the bytes of a corrupt registry before load() raises (SPEC §43.5); never raises."""
    try:
        d = path.parent / "registry-backups"
        d.mkdir(parents=True, exist_ok=True)
        files = sorted(p for p in d.iterdir() if p.name.startswith("registry-") and p.name.endswith(".json"))
        for f in files:
            if f.read_text() == raw:
                return
        target = d / _stamp()
        if target.exists():
            return
        target.write_text(raw)
        files = sorted(files + [target])
        for old in files[:-BACKUPS_KEEP]:
            old.unlink(missing_ok=True)
    except (OSError, UnicodeError):
        pass


@contextmanager
def locked(home: Path) -> Iterator[Registry]:
    """flock(home/'registry.lock', LOCK_EX); yield load(); on normal exit save() if modified
    (compare serialized form). Creates home if missing."""
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / "registry.lock"
    reg_path = home / "registry.json"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            before = reg_path.read_text()
        except FileNotFoundError:
            before = None
        reg = load(reg_path)
        try:
            yield reg
        except BaseException:
            raise
        else:
            if before != _dump(reg):
                save(reg_path, reg)
