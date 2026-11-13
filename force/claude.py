"""Facts about Claude Code processes and files (SPEC §4, §7.2)."""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from . import procinfo

DISABLE_AGENT_VIEW = '{"disableAgentView":true}'

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")


@dataclass(frozen=True)
class AgentStatus:
    pid: int
    session_id: str
    name: str | None
    name_source: str | None
    status: str                 # busy | waiting | idle | <other> | "" (not written yet)
    status_updated_ms: int | None   # None: not written yet
    waiting_for: str | None
    cwd: str | None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_status(claude_dir: Path, pid: int) -> AgentStatus | None:
    """Read claude_dir/sessions/<pid>.json. None if missing, unreadable, not JSON, missing sessionId,
    or the pid is not alive (os.kill(pid, 0) raises ProcessLookupError). Claude Code writes the file before its
    first status (issue #23): a missing status is "" and a missing statusUpdatedAt None, so the session counts as
    starting and its age comes from the pane, never from 1970."""
    path = claude_dir / "sessions" / f"{pid}.json"
    try:
        raw = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or "sessionId" not in data:
        return None
    if not _pid_alive(pid):
        return None
    sua = data.get("statusUpdatedAt")
    if not isinstance(sua, int) or isinstance(sua, bool) or sua <= 0:
        sua = None
    status = data.get("status", "")
    if not isinstance(status, str):
        status = "unknown"
    return AgentStatus(
        pid=pid,
        session_id=data["sessionId"],
        name=data.get("name"),
        name_source=data.get("nameSource"),
        status=status,
        status_updated_ms=sua,
        waiting_for=data.get("waitingFor"),
        cwd=data.get("cwd"),
    )


def process_version(pid: int, proc: Path | None = None) -> str | None:
    """Basename of the executable if it looks like a version (^\\d+\\.\\d+\\.\\d+), else None."""
    exe = procinfo.exe(pid, proc)
    if exe is None:
        return None
    base = os.path.basename(exe)
    if VERSION_RE.match(base):
        return base
    return None


def latest_version(claude_bin: Path) -> str | None:
    """Basename of realpath(claude_bin) if it looks like a version, else None."""
    try:
        base = os.path.basename(os.path.realpath(claude_bin))
    except (OSError, TypeError):
        return None
    if VERSION_RE.match(base):
        return base
    return None


def find_transcript(claude_dir: Path, session_id: str) -> Path | None:
    """First match of claude_dir/projects/*/<session_id>.jsonl, else None."""
    projects = claude_dir / "projects"
    if not projects.is_dir():
        return None
    try:
        children = sorted(projects.iterdir())
    except OSError:
        return None
    for p in children:
        if not p.is_dir():
            continue
        t = p / f"{session_id}.jsonl"
        if t.is_file():
            return t
    return None


def first_prompt(path: Path, words: int = 12) -> str | None:
    """The first words of the first thing the user typed in a transcript (what Claude Code's own resume picker
    shows), from its first 256 KiB; command and caveat records (text starting with "<" or "Caveat:") are
    skipped."""
    try:
        with open(path, "rb") as f:
            buf = f.read(256 * 1024)
    except OSError:
        return None
    for raw in buf.splitlines():
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "user" or obj.get("isMeta"):
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, list):
            content = next((b.get("text") for b in content
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")), None)
        if not isinstance(content, str):
            continue
        text = " ".join(content.split())
        if not text or text.startswith("<") or text.startswith("Caveat:"):
            continue
        return " ".join(text.split(" ")[:words])
    return None


def statusline_command(script: Path, usage_file: Path) -> str:
    """The status-line command that saves Claude's plan usage for the sidebar (SPEC §21)."""
    return f"python3 -I -S {shlex.quote(str(script))} {shlex.quote(str(usage_file))}"


def build_argv(claude_bin: Path, claude_dir: Path, name: str, session_id: str,
               statusline: str | None = None, memory_dir: str | None = None,
               remote_control: bool = True, extra_settings: dict | None = None,
               extra_args: list[str] | None = None) -> list[str]:
    """SPEC §7.2: --resume if a transcript exists, else --session-id; then -n NAME --remote-control NAME
    --settings DISABLE_AGENT_VIEW (plus a statusLine command when given, SPEC §21, and the auto memory
    directory a moved session keeps, SPEC §7.7)."""
    argv = [str(claude_bin)]
    if find_transcript(claude_dir, session_id):
        argv += ["--resume", session_id]
    else:
        argv += ["--session-id", session_id]
    settings: dict = {"disableAgentView": True}
    if statusline:
        settings["statusLine"] = {"type": "command", "command": statusline}
    if memory_dir:
        settings["autoMemoryDirectory"] = memory_dir
    if extra_settings:          # Chuck, the support session (SPEC §50): his own context and MCP permissions
        settings.update(extra_settings)
    argv += ["-n", name]
    if remote_control:       # off only for `force smoke` (its sessions must not show up in claude.ai)
        argv += ["--remote-control", name]
    argv += ["--settings", json.dumps(settings, separators=(",", ":"))]
    argv += list(extra_args or [])
    return argv


def project_slug(cwd: str) -> str | None:
    """Claude Code's project directory name for `cwd` (every character but letters and digits -> "-");
    None for a path so long that Claude Code shortens it its own way."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    return slug if len(slug) <= 200 else None


def memory_dir_for(claude_dir: Path, session_id: str, cwd: str) -> Path | None:
    """The auto memory directory a session uses now: next to its transcript (the project it was started
    in), else the one of `cwd`'s project. None when it cannot be told."""
    t = find_transcript(claude_dir, session_id)
    if t is not None:
        return t.parent / "memory"
    slug = project_slug(cwd)
    return claude_dir / "projects" / slug / "memory" if slug else None


def global_memory_dir(claude_dir: Path) -> str | None:
    """`autoMemoryDirectory` from the user's settings: memory that does not depend on the folder."""
    try:
        v = json.loads((claude_dir / "settings.json").read_text()).get("autoMemoryDirectory")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and v else None
