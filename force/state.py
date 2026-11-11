"""Session state computation (SPEC §5), seen.json, snapshot (SPEC §9)."""
from __future__ import annotations

import enum
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .claude import AgentStatus
from .config import Config
from .registry import Entry, is_shell

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class State(str, enum.Enum):
    ARCHIVED = "ARCHIVED"
    DOWN = "DOWN"
    SHELL = "SHELL"
    STARTING = "STARTING"
    NEEDS_YOU = "NEEDS_YOU"
    WORKING = "WORKING"
    YOUR_TURN = "YOUR_TURN"
    STALE = "STALE"
    IDLE = "IDLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PaneInfo:
    session_exists: bool
    pane_alive: bool            # agent pane exists and not dead
    pane_pid: int | None
    pane_started_ms: int | None
    dead_status: int | None     # exit status if dead
    dead_signal: int | None     # signal number if killed


@dataclass
class Seen:
    last_seen_ms: int = 0
    turn_done_ms: int = 0


def compute_state(entry: Entry, pane: PaneInfo | None, status: AgentStatus | None,
                  seen: Seen, now_ms: int, cfg: Config) -> State:
    """SPEC §5 table, first match wins. pane None == no tmux session."""
    return compute_state_why(entry, pane, status, seen, now_ms, cfg)[0]


def compute_state_why(entry: Entry, pane: PaneInfo | None, status: AgentStatus | None,
                      seen: Seen, now_ms: int, cfg: Config) -> tuple[State, str]:
    """compute_state plus one short clause saying which rule matched (SPEC §43.3)."""
    if entry.archived:
        return State.ARCHIVED, "archived"

    # SHELL session: no Claude status file, no grace period — alive pane is a running shell.
    if is_shell(entry):
        if pane is None or not pane.session_exists or not pane.pane_alive:
            return State.DOWN, "shell: no live pane"
        # the foreground program's synthetic status (SPEC §49.3): busy = a program runs, idle = at the prompt
        if status is not None and status.status == "busy":
            return State.WORKING, "shell: a program is running"
        if status is not None and seen.turn_done_ms > seen.last_seen_ms:
            return State.YOUR_TURN, "shell: a program finished, not viewed since"
        return State.SHELL, "shell: pane alive"

    prefix = ""
    # A status file whose pid does not match the pane's pid counts as no status file.
    if (pane is not None and pane.pane_pid is not None and status is not None
            and status.pid != pane.pane_pid):
        prefix = f"status file is from pid {status.pid}, pane runs {pane.pane_pid}: ignored; "
        status = None

    if pane is None or not pane.session_exists:
        return State.DOWN, prefix + "no tmux session"
    if not pane.pane_alive:
        if pane.dead_signal is not None:
            return State.DOWN, prefix + f"agent pane dead (signal {pane.dead_signal})"
        if pane.dead_status is not None:
            return State.DOWN, prefix + f"agent pane dead (exit {pane.dead_status})"
        return State.DOWN, prefix + "agent pane dead"

    if status is None:
        start_ms = pane.pane_started_ms
        if start_ms is None:
            return State.DOWN, prefix + "no status, pane start unknown"
        grace_ms = int(cfg.start_grace_seconds * 1000)
        if now_ms - start_ms >= grace_ms:
            return State.DOWN, (prefix + f"no status for {(now_ms - start_ms) // 1000} s after start "
                                f"(grace {int(cfg.start_grace_seconds)} s)")
        return State.STARTING, (prefix + f"starting: {(now_ms - start_ms) // 1000} s of "
                                f"{int(cfg.start_grace_seconds)} s grace")

    if status.status == "waiting":
        if status.waiting_for:
            return State.NEEDS_YOU, f"status: waiting ({status.waiting_for})"
        return State.NEEDS_YOU, "status: waiting"
    # "shell": Claude is running a shell command (seen on 2.1.292); that is work too.
    if status.status in ("busy", "shell"):
        return State.WORKING, f"status: {status.status}"
    if status.status == "idle":
        if seen.turn_done_ms > seen.last_seen_ms:
            return State.YOUR_TURN, "status: idle; turn finished, not viewed since"
        stale_ms = int(cfg.stale_hours * 3600 * 1000)
        if now_ms - status.status_updated_ms > stale_ms:
            return State.STALE, (f"status: idle, unchanged for "
                                 f"{(now_ms - status.status_updated_ms) // 3600000} h "
                                 f"(stale after {cfg.stale_hours:g} h)")
        return State.IDLE, "status: idle"
    return State.UNKNOWN, f"status: '{status.status}' unknown"


_BARE_TITLES = {"claude", "opencode", "bash", "zsh", "sh", "fish"}


def clean_title(raw: str | None, name: str, host: str) -> str | None:
    """Pane title for a card (SPEC §43.4): leading glyphs/spaces stripped, 'OC | ' dropped, <= 60 chars;
    None when empty or just the session name, host name or a bare program name."""
    if not raw:
        return None
    i = 0
    while i < len(raw) and not raw[i].isalnum():
        i += 1
    t = raw[i:]
    if t.startswith("OC | "):
        t = t[5:]
    elif t.startswith("OC |"):
        t = t[4:]
    t = t.strip()[:60].strip()
    if not t:
        return None
    low = t.lower()
    if low in (name.lower(), host.lower()) or low in _BARE_TITLES:
        return None
    return t


def glyph(state: State, now_ms: int) -> str:
    """Glyph per SPEC §5 ('' for ARCHIVED); WORKING uses SPINNER[now_ms // 100 % 10]."""
    if state is State.ARCHIVED:
        return ""
    if state is State.WORKING:
        return SPINNER[now_ms // 100 % 10]
    return {
        State.DOWN: "✖",
        State.SHELL: "$",
        State.STARTING: "…",
        State.NEEDS_YOU: "●",
        State.YOUR_TURN: "●",
        State.STALE: "◌",
        State.IDLE: "○",
        State.UNKNOWN: "?",
    }[state]


def color(state: State) -> str:
    """tmux colour name per state: NEEDS_YOU red (bold handled by caller), WORKING yellow, YOUR_TURN green,
    DOWN red, IDLE colour245, STALE/STARTING/UNKNOWN/ARCHIVED colour240."""
    if state is State.NEEDS_YOU or state is State.DOWN:
        return "red"
    if state is State.WORKING:
        return "yellow"
    if state is State.YOUR_TURN:
        return "green"
    if state is State.SHELL:
        return "colour245"
    if state is State.IDLE:
        return "colour245"
    return "colour240"


def unread(state: State, reply_ms: int | None, seen: Seen, viewed: bool) -> bool:
    """SPEC §5 `unread`: a WORKING session, not on any client's screen, whose agent wrote you text since you
    last looked at it."""
    return (state is State.WORKING and not viewed and reply_ms is not None
            and reply_ms > seen.last_seen_ms)


def format_age(since_ms: int | None, now_ms: int) -> str:
    """'' if None; <10 s 'now'; <60 s '45s'; <60 m '12m'; <48 h '3h'; else '2d'. Negative -> 'now'."""
    if since_ms is None:
        return ""
    diff = now_ms - since_ms
    if diff < 0:
        return "now"
    sec = diff // 1000
    if sec < 10:
        return "now"
    if sec < 60:
        return f"{sec}s"
    if sec < 60 * 60:
        return f"{sec // 60}m"
    if sec < 48 * 60 * 60:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def load_seen(path: Path) -> dict[str, Seen]:
    """Missing or corrupt -> {} (corrupt: warning on stderr)."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        print(f"force: warning: corrupt seen.json {path}; ignoring", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"force: warning: corrupt seen.json {path}; ignoring", file=sys.stderr)
        return {}
    result: dict[str, Seen] = {}
    for name, d in data.items():
        if not isinstance(d, dict):
            continue
        result[name] = Seen(
            last_seen_ms=d.get("last_seen_ms", 0) or 0,
            turn_done_ms=d.get("turn_done_ms", 0) or 0,
        )
    return result


# In-memory cache of what this process last wrote to each path, so the per-second writers skip
# unchanged rewrites. These files are fully regenerated from the registry after a crash, so we drop
# os.fsync for them (registry.save keeps its fsync as the source of truth).
_written: dict[str, str] = {}


def _needs_write(path: Path, content: str) -> bool:
    """True if `content` differs from the last content this process wrote to `path`; on the first
    write, compare against the file on disk (if it exists)."""
    key = str(path)
    if key in _written:
        return _written[key] != content
    try:
        existing = path.read_text()
    except OSError:
        existing = None
    changed = existing != content
    _written[key] = content
    return changed


def _atomic_write(path: Path, text: str) -> None:
    """Atomic tmp + os.replace write, but only when the content actually changed (see `_needs_write`).
    No os.fsync: these files are rewritten every second and fully regenerated after a crash."""
    if not _needs_write(path, text):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save_seen(path: Path, seen: dict[str, Seen]) -> None:
    """Atomic write."""
    data = {name: {"last_seen_ms": s.last_seen_ms, "turn_done_ms": s.turn_done_ms}
            for name, s in seen.items()}
    _atomic_write(path, json.dumps(data, indent=2))


@dataclass
class SessionView:
    name: str
    index: int | None           # 1-based among live sessions in order; None if archived
    state: State
    since_ms: int | None
    version: str | None
    outdated: bool
    archived: bool
    order: int
    cwd: str
    session_id: str
    pid: int | None
    waiting_for: str | None
    viewed: bool                # a tmux client currently shows this session
    activity: str | None = None   # one-line "what is this session doing" (SPEC §15/§16)
    mode: str | None = None      # permission-mode badge: auto/manual/edits/plan/bypass, else None
    model: str | None = None     # short model label, e.g. "opus 5.5"
    effort: str | None = None    # effort level as-is: low/medium/high/xhigh/max
    shared: dict | None = None   # user -> "r"|"w" for who this session is shared with (SPEC §19)
    note: str | None = None      # brief feedback in place of the state label: restarting, restarted, archiving
    kind: str = "claude"         # claude | opencode | shell (SPEC §36): the card's kind mark
    support: bool = False        # Chuck, the support session (SPEC §50): the sidebar shows it last, with its face
    why: str | None = None       # why the card shows its state (SPEC §43.3)
    title: str | None = None     # cleaned pane title (SPEC §43.4)
    unread: bool = False         # WORKING with a reply you have not seen (SPEC §5)


@dataclass
class Snapshot:
    generated_ms: int
    latest_version: str | None
    sessions: list[SessionView]
    usage: dict | None = None    # Claude plan usage windows (SPEC §21), see force.usage.normalize
    smoke: dict | None = None    # the last `force smoke` result: {"ok", "at"} (SPEC §37)
    update: str | None = None    # a newer Force version available (SPEC §3): set when one was found
    limits: list | None = None   # plan limits per provider: [{"kind", "label", "five_hour", "seven_day"}] (§49.2)

    def counts(self) -> dict[str, int]:
        """Keys needs_you, working, your_turn, idle, stale, down (live sessions only; STARTING/UNKNOWN not counted)."""
        counts = {"needs_you": 0, "working": 0, "your_turn": 0, "idle": 0, "stale": 0, "down": 0}
        for s in self.sessions:
            if s.support:          # Chuck (SPEC §50) is not in the session list: his state shows on his own row
                continue
            if s.archived:
                continue
            if s.state is State.NEEDS_YOU:
                counts["needs_you"] += 1
            elif s.state is State.WORKING:
                counts["working"] += 1
            elif s.state is State.YOUR_TURN:
                counts["your_turn"] += 1
            elif s.state is State.IDLE:
                counts["idle"] += 1
            elif s.state is State.STALE:
                counts["stale"] += 1
            elif s.state is State.DOWN:
                counts["down"] += 1
        return counts

    def to_json(self) -> dict:
        data = {
            "generated_ms": self.generated_ms,
            "latest_version": self.latest_version,
            "counts": self.counts(),
            "usage": self.usage,
            "limits": self.limits or [],
            "smoke": self.smoke,
            "sessions": [{
                "name": s.name,
                "index": s.index,
                "state": s.state.value,
                "since_ms": s.since_ms,
                "version": s.version,
                "outdated": s.outdated,
                "archived": s.archived,
                "order": s.order,
                "cwd": s.cwd,
                "session_id": s.session_id,
                "pid": s.pid,
                "waiting_for": s.waiting_for,
                "viewed": s.viewed,
                "activity": s.activity,
                "mode": s.mode,
                "model": s.model,
                "effort": s.effort,
                "shared": s.shared,
                "note": s.note,
                "kind": s.kind,
                "why": s.why,
                "title": s.title,
                "support": s.support,
                "unread": s.unread,
            } for s in self.sessions],
        }
        if self.update:
            data["update"] = self.update
        return data

    @staticmethod
    def from_json(d: dict) -> "Snapshot":
        sessions = []
        for s in d.get("sessions", []):
            sessions.append(SessionView(
                name=s["name"],
                index=s.get("index"),
                state=State(s["state"]),
                since_ms=s.get("since_ms"),
                version=s.get("version"),
                outdated=s.get("outdated", False),
                archived=s.get("archived", False),
                order=s.get("order", 0),
                cwd=s.get("cwd", ""),
                session_id=s.get("session_id", ""),
                pid=s.get("pid"),
                waiting_for=s.get("waiting_for"),
                viewed=s.get("viewed", False),
                activity=s.get("activity"),
                mode=s.get("mode"),
                model=s.get("model"),
                effort=s.get("effort"),
                shared=s.get("shared"),
                note=s.get("note"),
                kind=s.get("kind") or "claude",
                support=bool(s.get("support")),
                why=s.get("why"),
                title=s.get("title"),
                unread=bool(s.get("unread")),
            ))
        return Snapshot(generated_ms=d.get("generated_ms", 0),
                        latest_version=d.get("latest_version"),
                        sessions=sessions,
                        usage=d.get("usage") if isinstance(d.get("usage"), dict) else None,
                        limits=[x for x in d.get("limits") or [] if isinstance(x, dict)]
                        if isinstance(d.get("limits"), list) else None,
                        smoke=d.get("smoke") if isinstance(d.get("smoke"), dict) else None,
                        update=d.get("update") if isinstance(d.get("update"), str) else None)


# Per-path (serialised content without generated_ms, clock() time of the last write) so the snapshot
# is only rewritten when its content apart from generated_ms changes, or the previous write is more than
# 2 s old. generated_ms is kept in the file (the sidebar and CLI use it to detect a dead daemon with a
# 5 s staleness check), but it alone never triggers a rewrite.
_snap_written: dict[str, tuple[str, float]] = {}


def write_snapshot(path: Path, snap: Snapshot) -> None:
    """Atomic write of to_json(). Rewritten only when the content apart from `generated_ms` changed,
    or the previous write is older than 2 s (so readers' dead-daemon staleness check keeps working)."""
    data = snap.to_json()
    full = json.dumps(data, indent=2)
    non_gen = dict(data)
    non_gen.pop("generated_ms", None)
    non_gen_json = json.dumps(non_gen, indent=2)
    key = str(path)
    last = _snap_written.get(key)
    if last is not None:
        if last[0] == non_gen_json and time.time() - last[1] < 2.0:
            return
    else:
        # first write this process: compare against the file on disk (ignoring generated_ms).
        try:
            disk = json.loads(path.read_text())
            same = isinstance(disk, dict)
            if same:
                disk.pop("generated_ms", None)
                same = json.dumps(disk, indent=2) == non_gen_json
        except (ValueError, TypeError, OSError):
            same = False
        if same:
            _snap_written[key] = (non_gen_json, time.time())
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(full)
    os.replace(tmp, path)
    _snap_written[key] = (non_gen_json, time.time())


def read_snapshot(path: Path) -> Snapshot | None:
    """None if missing or unreadable."""
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Snapshot.from_json(data)
    except (KeyError, ValueError, TypeError):
        return None
