"""OpenCode sessions (SPEC §36): an OpenCode TUI in the session pane instead of Claude Code.

The TUI runs with `--port <p>` on 127.0.0.1, which serves OpenCode's HTTP API next to the TUI; the
daemon reads the session's state from it every tick:
  GET /session/status          {sessionID: {"type": "busy" | "retry"}}; absent = idle
  GET /permission, /question   pending permission prompts / questions ([{"sessionID": ...}, ...]; the
                               newer /api/permission/request and /api/question/request ({"data": [...]})
                               are read too, but OpenCode 1.19's TUI fills only the old ones)
  GET /api/session/<id>        the session (model id and variant, agent, time.updated)
  GET /session/<id>/message?limit=1   while busy: the running part, for the activity phrase
  GET /global/health           {"version": ...}: the version this TUI runs (vs. the installed binary)
The agent the TUI is on (Tab switches build/plan) shows only on its screen, in the prompt box's footer
(`┃  Build ·<model>`); the daemon reads it from there, like the other agents' mode line.
The OpenCode session id is learned from the first busy state (a new TUI has none until the first
message) and kept in the registry, so a restart resumes it with `--session`.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .activity import is_mid_turn_finding
from .claude import AgentStatus

_TIMEOUT = 0.4
# never hand out these (8000 is a common local LLM API port; the others are common dev servers)
_AVOID = {8000, 8080, 8787}


# Force-managed copy (config opencode_managed): <home>/opencode/<version>/opencode, <home>/opencode/current
_managed_root: "Path | None" = None


def set_managed(root) -> None:
    """Use the Force-managed copy under `root` (None: the opencode on PATH). Set once per process from
    the config (Cockpit)."""
    global _managed_root, _installed
    _managed_root = Path(root) if root else None
    _installed = (0.0, None)


def installed() -> str | None:
    """Path of the opencode binary to run: the managed copy's current version when managed (None until
    the first `force opencode-update`), else the one on PATH."""
    if _managed_root is not None:
        cur = _managed_root / "current" / "opencode"
        return str(cur) if os.access(cur, os.X_OK) else None
    return shutil.which("opencode")


def free_port() -> int:
    """A free TCP port on 127.0.0.1 for the TUI's API (the kernel's choice, never one of _AVOID)."""
    for _ in range(20):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in _AVOID:
            return port
    raise OSError("no free port")


def argv(port: int, session: str | None, cwd: str, binary: str = "opencode") -> list[str]:
    """The pane command's final exec."""
    a = [binary, "--port", str(port), "--hostname", "127.0.0.1"]
    if session:
        a += ["--session", session]
    return a + [cwd]


def _get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, urllib.error.URLError, ValueError, http.client.HTTPException):
        return None


@dataclass(frozen=True)
class Meta:
    """What the snapshot shows besides the state."""
    model: str | None          # e.g. "deepseek-v4-flash"
    question: bool             # a pending question (vs. a permission prompt)
    variant: str | None = None  # the model variant (OpenCode's reasoning effort), "default" when none chosen
    agent: str | None = None    # "build" | "plan" | a custom agent (of the last message; the screen is newer)
    activity: str | None = None  # "running a command", "editing app.py", ... while busy
    version: str | None = None  # the version this TUI runs
    title: str | None = None    # the session's own title (OpenCode names sessions after their first message)
    reply: str | None = None    # the last assistant message's findings, as "<message id>:<count>" (SPEC §5)


_installed: tuple[float, str | None] = (0.0, None)


def installed_version(max_age: float = 60.0) -> str | None:
    """`opencode --version` of the installed binary, cached for a minute (the admin updates it)."""
    global _installed
    now = time.monotonic()
    if _installed[1] is not None and now - _installed[0] < max_age:
        return _installed[1]
    path = installed()
    ver = None
    if path:
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"\d+\.\d+\.\d+\S*", out or "")
            ver = m.group(0) if m else None
        except (OSError, subprocess.TimeoutExpired):
            ver = None
    _installed = (now, ver)
    return ver


_AGENT_RE = re.compile(r"┃\s+([A-Za-z][\w-]*)\s*·")


def screen_agent(screen: str) -> str | None:
    """The agent in the TUI's prompt-box footer (the last `┃  Build ·…` line), lowercased; None if not
    on screen (dialogs, scrolled)."""
    found = None
    for line in screen.splitlines():
        m = _AGENT_RE.search(line)
        if m:
            found = m.group(1).lower()
    return found


def _base(path) -> str | None:
    return os.path.basename(path) if isinstance(path, str) and path else None


def activity_from_parts(parts) -> str | None:
    """The common activity phrase for an assistant message's parts: its last tool part, else "writing a
    reply" for text, else "thinking" for reasoning (force/activity.py's phrases)."""
    if not isinstance(parts, list):
        return None
    for p in reversed(parts):
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "tool":
            tool = str(p.get("tool") or "")
            st = p.get("state") if isinstance(p.get("state"), dict) else {}
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            f = _base(inp.get("filePath") or inp.get("path"))
            if tool == "bash":
                return "running a command"
            if tool == "read":
                return f"reading {f}" if f else "reading files"
            if tool in ("edit", "write", "patch", "multiedit", "apply_patch"):
                return f"editing {f}" if f else "editing files"
            if tool in ("grep", "glob", "list", "ls", "codesearch"):
                return "searching files"
            if tool == "task":
                return "delegating"
            if tool in ("webfetch", "websearch"):
                return "browsing the web"
            if tool in ("todowrite", "todoread"):
                return "planning"
            if tool == "question":
                return "asks you a question"
            return f"using {tool}" if tool else "using a tool"
        if t == "text" and (p.get("text") or "").strip():
            return "writing a reply"
        if t == "reasoning":
            return "thinking"
    return None


def reply_key(message) -> str | None:
    """The findings an assistant message has written you mid-turn (SPEC §5, unread): "<message id>:<count>" for
    its text parts that are findings (`activity.is_mid_turn_finding`) with a tool part after them, else None (narration,
    a reply that ends the turn, the user's own message)."""
    if not isinstance(message, dict):
        return None
    info = message.get("info") if isinstance(message.get("info"), dict) else message
    if info.get("role") != "assistant":
        return None
    parts = message.get("parts")
    if not isinstance(parts, list):
        return None
    parts = [p for p in parts if isinstance(p, dict)]
    n = sum(1 for i, p in enumerate(parts)
            if p.get("type") == "text" and is_mid_turn_finding(str(p.get("text") or ""))
            and any(q.get("type") == "tool" for q in parts[i + 1:]))
    if not n:
        return None
    return f"{info.get('id') or ''}:{n}"


def model_label(model_id: str | None) -> str | None:
    """A short card label like Claude's "opus 5.5": the provider's id without its provider prefix, dashes as
    spaces ("deepseek-v4-flash" -> "deepseek v4 flash", "anthropic/claude-x" -> "claude x")."""
    if not model_id:
        return None
    return model_id.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").strip() or None


def _pending(port: int, paths: tuple[str, ...], sid: str | None) -> bool:
    """A pending request for the session on any of `paths`: the TUI's `/permission` and `/question` (plain
    lists; what OpenCode 1.19 fills) and the newer `/api/.../request` ({"data": [...]}, empty there)."""
    for path in paths:
        d = _get(port, path)
        items = d.get("data") if isinstance(d, dict) else d
        if isinstance(items, list) and any(isinstance(i, dict) and (sid is None or i.get("sessionID") == sid)
                                           for i in items):
            return True
    return False


def read_status(port, pid: int, session: str | None,
                since_ms: int | None = None) -> tuple[AgentStatus | None, Meta | None]:
    """The session's state as a AgentStatus (busy | waiting | idle, session_id = the OpenCode id or ""),
    plus Meta; (None, None) while the TUI's API does not answer (starting, or no port yet)."""
    if not port:
        return None, None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None, None
    status = _get(port, "/session/status")
    if not isinstance(status, dict):
        return None, None
    busy = [k for k, v in status.items() if isinstance(v, dict) and v.get("type") in ("busy", "retry")]
    # the API belongs to this pane's TUI alone, so a busy session is the one it shows (also after the user
    # switched sessions inside the TUI); idle, the one the registry knows
    sid = busy[0] if busy else session
    question = _pending(port, ("/question", "/api/question/request"), sid)
    waiting = question or _pending(port, ("/permission", "/api/permission/request"), sid)
    state = "waiting" if waiting else ("busy" if sid is not None and sid in busy else "idle")
    model = variant = agent = title = None
    # last activity: the session's time.updated; without a session yet, since the TUI started (so a fresh,
    # untouched session counts as idle for the upgrade step, as an idle agent session does)
    updated = since_ms if since_ms else int(time.time() * 1000)
    if sid:
        info = _get(port, f"/api/session/{sid}")       # {"data": {model: {id, variant}, agent, time}}
        info = info.get("data") if isinstance(info, dict) else None
        if not isinstance(info, dict):                  # older OpenCode: the session itself
            info = _get(port, f"/session/{sid}")
        if isinstance(info, dict):
            m = info.get("model")
            if isinstance(m, dict):
                model = m.get("id") or m.get("modelID")
                variant = m.get("variant") if isinstance(m.get("variant"), str) else None
            elif isinstance(info.get("modelID"), str):
                model = info["modelID"]
            agent = info.get("agent") if isinstance(info.get("agent"), str) else None
            title = real_title(info.get("title"))
            t = info.get("time")
            if isinstance(t, dict) and isinstance(t.get("updated"), (int, float)):
                updated = int(t["updated"])
    if model is None:
        # no session yet (a fresh TUI) or none known: the configured default model, as the other agents' cards show theirs
        cfg = _get(port, "/config")
        if isinstance(cfg, dict) and isinstance(cfg.get("model"), str):
            model = cfg["model"].rsplit("/", 1)[-1] or None
    act = reply = None
    if state == "busy" and sid:
        msgs = _get(port, f"/session/{sid}/message?limit=1")
        if isinstance(msgs, list) and msgs and isinstance(msgs[-1], dict):
            act = activity_from_parts(msgs[-1].get("parts"))
            reply = reply_key(msgs[-1])
    health = _get(port, "/global/health")
    version = health.get("version") if isinstance(health, dict) and isinstance(health.get("version"), str) else None
    st = AgentStatus(pid=pid, session_id=sid or "", name=None, name_source=None, status=state,
                      status_updated_ms=updated, waiting_for=None, cwd=None)
    return st, Meta(model=model, question=question, variant=variant, agent=agent, activity=act,
                    version=version, title=title, reply=reply)


_DEFAULT_TITLE_RE = re.compile(r"^(new|child) session\b", re.I)


def real_title(title) -> str | None:
    """An OpenCode session title that it generated from the conversation; None for its placeholder
    ("New session - 2026-…", "Child session - …") and for anything that is not a non-empty string."""
    if not isinstance(title, str) or not title.strip() or _DEFAULT_TITLE_RE.match(title.strip()):
        return None
    return title.strip()


def session_title(port, sid: str) -> str | None:
    """The session's own title (see real_title), or None."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    info = _get(port, f"/api/session/{sid}")
    info = info.get("data") if isinstance(info, dict) else None
    if not isinstance(info, dict):
        info = _get(port, f"/session/{sid}")
    return real_title(info.get("title")) if isinstance(info, dict) else None


def first_message(port, sid: str, words: int = 12) -> str | None:
    """The first words of the first user message's text in the session, or None."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    msgs = _get(port, f"/session/{sid}/message")
    if not isinstance(msgs, list):
        return None
    for m in msgs:
        if not isinstance(m, dict):
            continue
        info = m.get("info") if isinstance(m.get("info"), dict) else m
        if info.get("role") != "user":
            continue
        for part in m.get("parts") or ():
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) \
                    and not part.get("synthetic"):
                text = " ".join(part["text"].split())
                if text:
                    return " ".join(text.split(" ")[:words])
    return None
