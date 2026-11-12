"""Migration of existing Claude sessions into force (SPEC §11)."""
from __future__ import annotations

from .lifecycle import queue_title_sync
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .claude import find_transcript, first_prompt as claude_first_prompt, process_version, read_status
from . import procinfo
from .lifecycle import Cockpit
from .registry import load as _load_registry, locked as _locked, slugify, valid_name

_SCREEN_LINE = re.compile(r"^\t(\d+)\.([^\t]+)")


class _NotIdle(Exception):
    """The candidate stopped being idle between discovery and import; nothing was touched."""


@dataclass
class Candidate:
    pid: int                    # claude pid
    screen_pid: int | None      # SCREEN pid when found in a screen session
    screen_name: str | None
    session_id: str | None
    claude_name: str | None
    status: str | None
    cwd: str | None
    target_name: str | None     # resolved force name, None if none valid/free
    skip_reason: str | None     # None -> importable


def parse_screen_ls(text: str) -> list[tuple[int, str]]:
    """Parse `screen -ls` output. Socket lines look like
    "\\t1474794.webshop\\t(10/07/2026 09:29:26 AM)\\t(Attached)" -> (1474794, "webshop").
    Names may contain dots. Other lines (headers, "No Sockets found") are ignored."""
    result = []
    for line in text.splitlines():
        m = _SCREEN_LINE.match(line)
        if m:
            result.append((int(m.group(1)), m.group(2)))
    return result


def children_map(proc: Path | None = None) -> dict[int, list[int]]:
    """ppid -> list of child pids (SPEC §51), via the one process-facts module."""
    return procinfo.children_map(proc)


def descendants(pid: int, cmap: dict[int, list[int]]) -> list[int]:
    """BFS over the children map, excluding `pid` itself."""
    result: list[int] = []
    queue = list(cmap.get(pid, []))
    while queue:
        cur = queue.pop(0)
        result.append(cur)
        queue.extend(cmap.get(cur, []))
    return result


def _ppid(pid: int, proc: Path | None) -> int | None:
    """Parent pid of `pid` (SPEC §51), else None."""
    return procinfo.ppid(pid, proc)


def is_claude_pid(pid: int, proc: Path | None = None) -> bool:
    """True if the process executable basename looks like a Claude version string."""
    return process_version(pid, proc) is not None


def ancestors(pid: int, proc: Path) -> set[int]:
    """Ancestor pids walking ppid up to (and excluding) pid 1."""
    result: set[int] = set()
    cur = pid
    while cur > 1:
        ppid = _ppid(cur, proc)
        if ppid is None:
            break
        result.add(ppid)
        cur = ppid
    return result


def _name_suffixes(base: str | None) -> list[str]:
    if base is None:
        return []
    return [base] + [f"{base}-{i}" for i in range(2, 10)]


def _resolve_name(explicit_name: str | None, claude_name: str | None,
                 screen_name: str | None, reg, used: set[str]) -> tuple[str | None, str | None]:
    """Return (target_name, skip_reason). An explicit name is used verbatim or the candidate is
    skipped; slug-derived names try "-2" .. "-9" fallbacks."""
    if explicit_name is not None:
        if (not valid_name(explicit_name) or reg.name_taken(explicit_name)
                or explicit_name in used):
            return None, f"name '{explicit_name}' invalid or taken"
        return explicit_name, None
    base = slugify(claude_name) if claude_name else None
    if base is None:
        base = slugify(screen_name) if screen_name else None
    for cand in _name_suffixes(base):
        if valid_name(cand) and not reg.name_taken(cand) and cand not in used:
            return cand, None
    return None, "no free name"


def _name_candidates(running_name, custom, summary, cwd, screen_name=None) -> list[str | None]:
    """The name-derivation chain for a single import: status name, transcript customTitle,
    transcript summary, cwd basename (in priority order). Values are raw; slugify happens later."""
    cands = [running_name, custom, summary]
    if cwd:
        cands.append(os.path.basename(os.path.normpath(cwd)))
    return cands


def _name_from_chain(explicit_name: str | None, candidates: list, reg,
                    used: set | None = None) -> tuple[str | None, str | None]:
    """SPEC §11 name chain for a single import: `--name` verbatim (invalid/taken -> skip), else the
    first candidate whose slugify is non-None becomes the base, then its -2..-9 suffix fallbacks."""
    if used is None:
        used = set()
    if explicit_name is not None:
        if (not valid_name(explicit_name) or reg.name_taken(explicit_name)
                or explicit_name in used):
            return None, f"name '{explicit_name}' invalid or taken"
        return explicit_name, None
    base = None
    for cand in candidates:
        slug = slugify(cand) if cand else None
        if slug:
            base = slug
            break
    for cand in _name_suffixes(base):
        if valid_name(cand) and not reg.name_taken(cand) and cand not in used:
            return cand, None
    return None, "no free name"


def _build_candidate(cockpit, pid, screen_pid, screen_name, proc, reg, explicit_name, used):
    st = read_status(cockpit.paths.claude_dir, pid)
    if st is None:
        return Candidate(pid, screen_pid, screen_name, None, None, None, None, None,
                        "no status file")

    # Already managed: an agent pane with this pid -> not listed at all.
    for pane in cockpit.tmux.panes():
        if pane.role == "agent" and pane.pid == pid:
            return None

    # Never touch the process running `force import` or its ancestors.
    me = os.getpid()
    if pid == me or pid in ancestors(me, proc):
        return Candidate(pid, screen_pid, screen_name, st.session_id, st.name, st.status,
                        st.cwd, None, "this is the session running force import")

    existing = reg.by_session_id(st.session_id)
    if existing is not None:
        return Candidate(pid, screen_pid, screen_name, st.session_id, st.name, st.status,
                        st.cwd, None, f"already managed as {existing.name}")

    target, reason = _resolve_name(explicit_name, st.name, screen_name, reg, used)
    if reason:
        return Candidate(pid, screen_pid, screen_name, st.session_id, st.name, st.status,
                        st.cwd, None, reason)
    used.add(target)

    if st.status != "idle":
        return Candidate(pid, screen_pid, screen_name, st.session_id, st.name, st.status,
                        st.cwd, None, f"busy ({st.status}), retry later")

    return Candidate(pid, screen_pid, screen_name, st.session_id, st.name, st.status,
                    st.cwd, target, None)


def find_candidates(cockpit: Cockpit, pid: int | None = None, name: str | None = None) -> list[Candidate]:
    """SPEC §11 discovery (screen sessions of the current user, or the explicit pid)."""
    proc = None
    used: set[str] = set()
    targets: list[tuple[int, int | None, str | None]] = []

    if pid is not None:
        if not is_claude_pid(pid, proc):
            return [Candidate(pid, None, None, None, None, None, None, None,
                             "not a claude process")]
        targets = [(pid, None, None)]
    else:
        try:
            res = subprocess.run(["screen", "-ls"], capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return []
        if "No Sockets found" in res.stdout or not res.stdout.strip():
            return []
        cmap = children_map(proc)
        for spid, sname in parse_screen_ls(res.stdout):
            for claude in descendants(spid, cmap):
                if is_claude_pid(claude, proc):
                    targets.append((claude, spid, sname))
                    break

    reg = _load_registry(cockpit.paths.registry)
    result = []
    for claude, spid, sname in targets:
        cand = _build_candidate(cockpit, claude, spid, sname, proc, reg, name, used)
        if cand is not None:
            result.append(cand)
    return result


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_claude(cockpit, session_id: str, target_name: str, sleep, clock,
                     timeout: float = 30.0) -> bool:
    deadline = clock() + timeout
    while clock() < deadline:
        for pane in cockpit.tmux.panes():
            if pane.role == "agent" and pane.session == target_name and pane.pid is not None:
                st = read_status(cockpit.paths.claude_dir, pane.pid)
                if st is not None and st.session_id == session_id:
                    return True
        sleep(0.2)
    return False


def _import_one(cockpit, cand, sleep, clock) -> None:
    """Kill the old process, register the session, create the tmux session, wait for it to come up,
    then quit the old screen. Raises on failure (after the kill the registry entry is kept)."""
    # Status was read during discovery; the session may have started working since then.
    st = read_status(cockpit.paths.claude_dir, cand.pid)
    if st is None or st.status != "idle" or st.session_id != cand.session_id:
        raise _NotIdle(f"no longer idle ({st.status if st else 'gone'}), retry later")
    os.kill(cand.pid, signal.SIGTERM)
    deadline = clock() + 20.0
    sleep(0.2)
    while clock() < deadline and _alive(cand.pid):
        sleep(0.2)
    if _alive(cand.pid):
        os.kill(cand.pid, signal.SIGKILL)
        sleep(2.0)

    cwd = cand.cwd or os.path.expanduser("~")
    with _locked(cockpit.paths.home) as reg:
        entry = reg.add(cand.target_name, cand.session_id, cwd)
        queue_title_sync(entry, cand.target_name)    # its Remote Control title is the old one (SPEC §8)
    cockpit.create(entry)
    if not _wait_for_claude(cockpit, cand.session_id, cand.target_name, sleep, clock):
        raise RuntimeError(f"claude session {cand.target_name} did not start in time")
    if cand.screen_pid:
        subprocess.run(["screen", "-S", str(cand.screen_pid), "-X", "quit"])


def run_import(cockpit: Cockpit, dry_run: bool = False, pid: int | None = None,
               name: str | None = None, out=print,
               sleep=time.sleep, clock=time.monotonic) -> int:
    """SPEC §11. Prints one line per candidate via `out`. Returns number of failures."""
    candidates = find_candidates(cockpit, pid=pid, name=name)

    if dry_run:
        for c in candidates:
            if c.skip_reason:
                out(f"skip {c.pid}: {c.skip_reason}")
            else:
                out(f"would import {c.pid} ({c.screen_name or '-'}) as {c.target_name} "
                    f"[{c.session_id}]")
        return 0

    failures = 0
    for c in candidates:
        if c.skip_reason:
            out(f"skipped {c.pid}: {c.skip_reason}")
            continue
        try:
            _import_one(cockpit, c, sleep=sleep, clock=clock)
        except _NotIdle as e:
            out(f"skipped {c.pid}: {e}")
            continue
        except Exception as e:
            failures += 1
            out(f"failed {c.target_name}: {e}")
            continue
        if c.screen_name:
            out(f"imported {c.target_name} (was screen {c.screen_name})")
        else:
            out(f"imported {c.target_name}")
    return failures


# ---------------------------------------------------------------------------
# force import SESSION / force import (list) / force import --screen (SPEC §11)
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class _ResolveError(Exception):
    pass


class _Ambiguous(_ResolveError):
    def __init__(self, sel, matches):
        super().__init__(sel)
        self.matches = matches


@dataclass
class Running:
    pid: int
    session_id: str
    name: str | None
    status: str
    cwd: str | None
    in_force: object           # registry Entry if already managed, else None


@dataclass
class Stopped:
    session_id: str
    transcript: Path
    cwd: str | None
    custom: str | None        # last customTitle (raw, unslugified)
    summary: str | None       # last summary (raw, unslugified)
    title: str | None        # last customTitle, else last summary, else the first prompt's first words (raw)
    mtime: float
    in_force: object
    is_smoke: bool
    has_user: bool


def _screen_for_pid(pid: int, proc: Path | None = None) -> tuple[int | None, str | None]:
    """The screen session (spid, sname) whose process tree contains `pid`, else (None, None)."""
    try:
        res = subprocess.run(["screen", "-ls"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None, None
    if "No Sockets found" in res.stdout or not res.stdout.strip():
        return None, None
    cmap = children_map(proc)
    for spid, sname in parse_screen_ls(res.stdout):
        if pid in descendants(spid, cmap):
            return spid, sname
    return None, None


def scan_running(cockpit, reg, proc: Path | None = None) -> list[Running]:
    """Every live Claude status file of this user that is not a Force agent pane."""
    panes = cockpit.tmux.panes()
    out: list[Running] = []
    sessions = cockpit.paths.claude_dir / "sessions"
    if sessions.is_dir():
        try:
            files = sorted(sessions.glob("*.json"))
        except OSError:
            files = []
        for p in files:
            if not p.stem.isdigit():
                continue
            pid = int(p.stem)
            if any(pan.role == "agent" and pan.pid == pid for pan in panes):
                continue
            st = read_status(cockpit.paths.claude_dir, pid)
            if st is None:
                continue
            entry = reg.by_session_id(st.session_id) if st.session_id else None
            out.append(Running(pid, st.session_id, st.name, st.status, st.cwd, entry))
    return out


def _under(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(base)]) == os.path.abspath(base)
    except ValueError:
        return False


def _transcript_cwd(path: Path) -> str | None:
    """First JSON line with a string "cwd", read from the first 64 KiB (transcripts can be large)."""
    try:
        with open(path, "rb") as f:
            buf = f.read(64 * 1024)
    except OSError:
        return None
    for line in buf.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
            return obj["cwd"]
    return None


def _transcript_titles(path: Path) -> tuple[str | None, str | None]:
    """(last customTitle, last summary) read from the last 256 KiB of the transcript."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > 256 * 1024:
                f.seek(size - 256 * 1024)
            buf = f.read()
    except OSError:
        return None, None
    last_custom = None
    last_summary = None
    for line in buf.decode("utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "custom-title" and isinstance(obj.get("customTitle"), str):
            last_custom = obj["customTitle"]
        elif obj.get("type") == "summary" and isinstance(obj.get("summary"), str):
            last_summary = obj["summary"]
    return last_custom, last_summary


def _transcript_title(path: Path) -> str | None:
    """The last customTitle, else the last summary, read from the last 256 KiB."""
    custom, summary = _transcript_titles(path)
    return custom if custom is not None else summary


def _first_prompt(path: Path) -> str | None:
    """The first five words of the first thing the user typed (claude.first_prompt)."""
    return claude_first_prompt(path, words=5)


def _has_user_message(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict) and obj.get("type") == "user":
                    # a non-interactive run (`claude -p`, the SDK: entrypoint "sdk-…", e.g. delegated jobs)
                    # is nothing anyone would open again
                    return not str(obj.get("entrypoint") or "cli").startswith("sdk")
    except OSError:
        return False
    return False


def scan_stopped(cockpit, reg) -> list[Stopped]:
    """Every transcript claude_dir/projects/*/<uuid>.jsonl (top level of each project dir only)."""
    smoke = os.path.abspath(str(cockpit.paths.state / "smoke"))
    out: list[Stopped] = []
    projects = cockpit.paths.claude_dir / "projects"
    if projects.is_dir():
        try:
            dirs = sorted(projects.iterdir())
        except OSError:
            dirs = []
        for pdir in dirs:
            if not pdir.is_dir():
                continue
            try:
                trans = sorted(pdir.glob("*.jsonl"))
            except OSError:
                continue
            for t in trans:
                if _UUID_RE.match(t.stem) is None:
                    continue
                try:
                    mtime = t.stat().st_mtime
                except OSError:
                    continue
                cwd = _transcript_cwd(t)
                custom, summary = _transcript_titles(t)
                if custom is None and summary is None:
                    summary = _first_prompt(t)          # an untitled session: name it after its first prompt
                entry = reg.by_session_id(t.stem)
                out.append(Stopped(
                    session_id=t.stem,
                    transcript=t,
                    cwd=cwd,
                    custom=custom,
                    summary=summary,
                    title=custom if custom is not None else summary,
                    mtime=mtime,
                    in_force=entry,
                    is_smoke=bool(cwd) and _under(cwd, smoke),
                    has_user=_has_user_message(t),
                ))
    return out


def _home_short(p: str | None) -> str:
    if not p:
        return ""
    home = os.path.expanduser("~")
    p = os.path.normpath(p)
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def _age(mtime: float, now: float) -> str:
    secs = max(0, int(now - mtime))
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _title_col(v) -> str:
    if not v:
        return ""
    s = str(v)
    return s if len(s) <= 24 else s[:24]


def run_list(cockpit, all_: bool = False, out=print, now: float | None = None) -> int:
    """`force import` with no arguments: list runnable and stopped sessions, import nothing."""
    if now is None:
        now = time.time()
    reg = _load_registry(cockpit.paths.registry)
    running = [r for r in scan_running(cockpit, reg) if r.in_force is None]
    running_ids = {r.session_id for r in running}
    stopped = []
    for s in scan_stopped(cockpit, reg):
        if s.in_force is not None or s.session_id in running_ids or s.is_smoke or not s.has_user:
            continue
        stopped.append(s)
    stopped.sort(key=lambda s: s.mtime, reverse=True)
    if not all_:
        stopped = stopped[:15]

    if not running and not stopped:
        out("force: nothing to import")
        return 0

    if running:
        out("running (not in Force):")
        for r in running:
            out(f"  {r.session_id[:8]:8s}  {_title_col(r.status):8s}  "
                f"{_title_col(r.name):<24s}  {_home_short(r.cwd):<16s}  "
                f"force import {r.session_id[:8]}")
    if stopped:
        out("stopped (newest first):")
        for s in stopped:
            title = s.title if s.title else (
                os.path.basename(os.path.normpath(s.cwd)) if s.cwd else None)
            out(f"  {s.session_id[:8]:8s}  {_age(s.mtime, now):8s}  "
                f"{_title_col(title):<24s}  {_home_short(s.cwd):<16s}  "
                f"force import {s.session_id[:8]}")
    return 0


def _resolve_target(reg, running, stopped, sel, proc) -> tuple[str, Running | Stopped]:
    """Resolve SESSION to a ("running", Running) or ("stopped", Stopped) pair."""
    sel = (sel or "").strip()
    if sel.isdigit():
        pid = int(sel)
        for r in running:
            if r.pid == pid:
                return "running", r
        raise _ResolveError(f"no running claude with pid {pid}")
    for r in running:
        if r.name == sel:
            return "running", r
    if len(sel) < 6:
        raise _ResolveError("session id or prefix must be at least 6 characters")
    lower = sel.lower()
    bysid: dict[str, tuple[str, object]] = {}
    for r in running:
        bysid.setdefault(r.session_id, ("running", r))
    for s in stopped:
        bysid.setdefault(s.session_id, ("stopped", s))
    matched = [sid for sid in bysid if sid.lower().startswith(lower)]
    if not matched:
        raise _ResolveError(f"no Claude session matching {sel!r}")
    if len(matched) > 1:
        raise _Ambiguous(sel, sorted(matched))
    kind, obj = bysid[matched[0]]
    return kind, obj


def _import_running(cockpit, r, explicit_name, dry_run, out, sleep, clock, proc) -> int:
    me = os.getpid()
    if r.pid == me or r.pid in ancestors(me, proc):
        print("force: refusing to import the session running force import", file=sys.stderr)
        return 1
    reg = _load_registry(cockpit.paths.registry)
    tpath = find_transcript(cockpit.paths.claude_dir, r.session_id)
    custom = summary = None
    if tpath is not None:
        custom, summary = _transcript_titles(tpath)
    target, reason = _name_from_chain(
        explicit_name, _name_candidates(r.name, custom, summary, r.cwd), reg)
    if reason:
        print(f"force: cannot import: {reason}", file=sys.stderr)
        return 1
    id8 = r.session_id[:8]
    if r.status != "idle":
        print(f"force: {target} is busy ({r.status}); import it when it is idle",
              file=sys.stderr)
        return 1
    if dry_run:
        out(f"would import {id8} as {target} (running, {_home_short(r.cwd)})")
        return 0
    spid, sname = _screen_for_pid(r.pid, proc)
    cand = Candidate(r.pid, spid, sname, r.session_id, r.name, r.status, r.cwd, target, None)
    try:
        _import_one(cockpit, cand, sleep=sleep, clock=clock)
    except _NotIdle as e:
        print(f"force: {target}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"force: failed {target}: {e}", file=sys.stderr)
        return 1
    out(f"imported {target} ({id8}): open it with force {target}")
    return 0


def _import_stopped(cockpit, s, explicit_name, dry_run, out, sleep, clock) -> int:
    if not s.cwd:
        print("force: its transcript has no cwd", file=sys.stderr)
        return 1
    if not os.path.isdir(s.cwd):
        print(f"force: its folder {s.cwd} no longer exists", file=sys.stderr)
        return 1
    target, reason = _name_from_chain(
        explicit_name, _name_candidates(None, s.custom, s.summary, s.cwd),
        _load_registry(cockpit.paths.registry))
    if reason:
        print(f"force: cannot import: {reason}", file=sys.stderr)
        return 1
    id8 = s.session_id[:8]
    if dry_run:
        out(f"would import {id8} as {target} (stopped, {_home_short(s.cwd)})")
        return 0
    with _locked(cockpit.paths.home) as reg:
        entry = reg.add(target, s.session_id, s.cwd)
        queue_title_sync(entry, target)
    cockpit.create(entry)
    if not _wait_for_claude(cockpit, s.session_id, target, sleep, clock, timeout=30.0):
        print(f"force: failed {target}: claude session {target} did not start in time",
              file=sys.stderr)
        return 1
    out(f"imported {target} ({id8}): open it with force {target}")
    return 0


def import_session(cockpit, sel: str, explicit_name: str | None = None, dry_run: bool = False,
                  out=print, sleep=time.sleep, clock=time.monotonic,
                  proc: Path | None = None) -> int:
    """`force import SESSION [--name NAME] [--dry-run]`: import one running or stopped session."""
    reg = _load_registry(cockpit.paths.registry)
    running = scan_running(cockpit, reg, proc=proc)
    stopped = scan_stopped(cockpit, reg)
    try:
        kind, obj = _resolve_target(reg, running, stopped, sel, proc)
    except _Ambiguous as e:
        print(f"force: ambiguous: {', '.join(sid[:8] for sid in e.matches)}", file=sys.stderr)
        return 1
    except _ResolveError as e:
        print(f"force: {e}", file=sys.stderr)
        return 1
    if obj.in_force is not None:
        print(f"force: already in Force as {obj.in_force.name}", file=sys.stderr)
        return 1
    if kind == "running":
        return _import_running(cockpit, obj, explicit_name, dry_run, out, sleep, clock, proc)
    return _import_stopped(cockpit, obj, explicit_name, dry_run, out, sleep, clock)
