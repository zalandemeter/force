"""One-line "what is this session doing" text derived from a Claude Code transcript.

A Claude Code transcript (SPEC §4) is a JSONL file (one JSON object per line). `describe` reads the
tail of the file and walks records backwards to find the newest assistant turn that carries a concrete action
(a tool_use or text block), and renders it as a single short line for the sidebar (SPEC §15/§16).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

_TAIL = 256 * 1024           # read at most this many bytes from the end of the transcript

# A finding (SPEC §5, unread): text long enough to be a report, not the narration Claude Code writes between tool
# calls ("Let me check the config…"; median 118 characters, 95 % under 200).
FINDING_CHARS = 300
FINDING_STRUCTURED_CHARS = 100      # ... or this long with a list, a heading, a table or a code block
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.M)
_HEADING = re.compile(r"^\s*#{1,6}\s+\S", re.M)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)

# Leading markdown markers to strip from a text block's first line: # heading, * bullet, - bullet,
# > quote, ` code-span, and `1.` numbered-list items.
_MD_LEAD = re.compile(r"^(?:#{1,6}\s*|[*>\-\s]+|\d+\.\s*|`+)")
_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# A trailing publish date (-YYYYMMDD) on a model id, and the family+version pattern it exposes.
_MODEL_DATE = re.compile(r"-\d{8}$")
_MODEL = re.compile(r"^([A-Za-z]+)((?:-\d+)+)$")


def _single(text: str) -> str:
    """Whitespace-collapse and strip control characters from `text`."""
    text = _CONTROL.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _first_line(text: str) -> str | None:
    """First non-empty line of `text`, with leading markdown markers stripped and whitespace collapsed."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = _MD_LEAD.sub("", line)
        line = line.replace("**", "")          # remove bold ** pairs
        line = _single(line)
        if not line:
            continue
        return line
    return None


def _basename(path: str) -> str:
    if not path:
        return ""
    return os.path.basename(str(path))


def _file(inp: dict) -> str:
    return _basename(str(inp.get("file_path") or inp.get("notebook_path") or ""))


def _tool_desc(block: dict) -> str | None:
    """A short, complete phrase from a fixed vocabulary - never a cut-off sentence (the sidebar shows
    about 20 characters). Only file names carry detail."""
    name = str(block.get("name") or "")
    inp = block.get("input")
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return "running a command"
    if name in ("Read", "NotebookRead"):
        return ("reading " + _file(inp)) if _file(inp) else "reading files"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return ("editing " + _file(inp)) if _file(inp) else "editing files"
    if name == "Write":
        return ("writing " + _file(inp)) if _file(inp) else "writing files"
    if name in ("Grep", "Glob"):
        return "searching files"
    if name in ("Agent", "Task"):
        return "delegating"
    if name == "AskUserQuestion":
        return "asks you a question"
    if name == "ExitPlanMode":
        return "plan to review"
    if name in ("WebFetch", "WebSearch"):
        return "browsing the web"
    if name == "TodoWrite":
        return "planning"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        return "using " + (parts[1] if len(parts) >= 2 and parts[1] else "a tool")
    return "using " + name.lower() if name else None


def _truncate(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len == 1:
        return "…"
    return text[: max_len - 1] + "…"


def _describe_blocks(blocks: list) -> str | None:
    """Phrase for one assistant record: its last tool_use block if any, else "writing a reply" for a
    text block, else "thinking" for a thinking-only record."""
    tool = None
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool = block
    if tool is not None:
        return _tool_desc(tool)
    types = {b.get("type") for b in blocks if isinstance(b, dict)}
    if "text" in types:
        return "writing a reply"
    if "thinking" in types or "redacted_thinking" in types:
        return "thinking"
    return None


def _model_short(model: object) -> str | None:
    """Short label for a model id: strip a leading `claude-`, drop a trailing `-YYYYMMDD` date, then join
    the family and dotted version parts ("claude-opus-5-5" -> "opus 5.5"). Anything that no longer
    matches `<family>-<digits>(-<digits>)*` is returned as the stripped id unchanged. None for a missing or
    non-string value."""
    if not isinstance(model, str) or not model:
        return None
    stripped = model[len("claude-"):] if model.startswith("claude-") else model
    base = _MODEL_DATE.sub("", stripped)
    m = _MODEL.match(base)
    if m:
        family = m.group(1)
        version = ".".join(m.group(2).split("-")[1:])
        return f"{family} {version}"
    return stripped


def _scan_tail(transcript: Path) -> list | None:
    """The record lines of the last ≤256 KiB of `transcript` (newest last), or None if unreadable."""
    try:
        st = transcript.stat()
        size = st.st_size
        with open(transcript, "rb") as f:
            if size > _TAIL:
                f.seek(size - _TAIL)
            buf = f.read()
    except OSError:
        return None
    text = buf.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > _TAIL and lines:
        lines = lines[1:]        # drop the partial first line cut at the 256 KiB seek boundary
    return lines


def _timestamp_ms(value: object) -> int | None:
    """Epoch ms of a record's ISO-8601 `timestamp` ("2026-11-11T14:48:12.345Z"); None if absent or bad."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp() * 1000)


def is_finding(text: str) -> bool:
    """Text worth reading (SPEC §5, unread): at least FINDING_CHARS long, or FINDING_STRUCTURED_CHARS with two
    list items, a heading, a table or a code block. Shorter text is narration."""
    t = text.strip()
    if len(t) >= FINDING_CHARS:
        return True
    if len(t) < FINDING_STRUCTURED_CHARS:
        return False
    return (len(_LIST_ITEM.findall(t)) >= 2 or _HEADING.search(t) is not None
            or len(_TABLE_ROW.findall(t)) >= 2 or "```" in t)


class Background(NamedTuple):
    """One background task a transcript launched and has not reported ended (SPEC §5)."""
    tool_use_id: str
    tool: str            # "Bash" | "Agent" | "Monitor" (or another tool with run_in_background)
    description: str
    command: str         # "" unless Bash
    started_ms: int | None   # the record's timestamp


class Scan(NamedTuple):
    """The result of `scan`: the single-line activity, the newest model/effort, the unread reply times (SPEC §5)
    and the `background` tasks (oldest first) the transcript tail shows still running, plus `launched_any` (whether
    any background record appeared there at all)."""
    activity: str | None
    model: str | None
    effort: str | None
    reply_ms: int | None
    end_reply_ms: int | None
    background: tuple[Background, ...] = ()
    launched_any: bool = False


def _task_desc(task: Background) -> str:
    """The one-line description of a counting task, with a fallback when it has no description (SPEC §5)."""
    if task.description:
        return task.description
    return {"Bash": "a background command", "Agent": "a subagent", "Monitor": "a monitor"}.get(task.tool,
                                                                                               "a background command")


def waiting_tasks(status_word: str, background, process_start_ms: int | None, now_ms: int | None) -> list[Background]:
    """The counting tasks of a watching session (SPEC §5): the launched, not-ended background tasks that make the
    session watching. Tasks with a `started_ms` older than the process it runs in (they died with an earlier process)
    or older than 24 h stay (see below) never count; a Bash task counts as running only while the status is `shell`;
    endless commands (`is_endless`) never count. Agent and Monitor always count."""
    out = []
    for t in background:
        if t.started_ms is not None:
            if now_ms is not None and t.started_ms < now_ms - 24 * 3600 * 1000:
                continue
            if process_start_ms is not None and t.started_ms < process_start_ms:
                continue
        if t.tool == "Bash" and (status_word != "shell" or is_endless(t.command)):
            continue
        out.append(t)
    return out


# --- background-task notices (SPEC §5) -----------------------------------------
# A user record that ends a background task holds one or more <task-notification> blocks, each with a
# <tool-use-id> and a <status>; the launched task is "ended" only when that status is a final one.
_TASK_NOTICE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
_TOOL_USE_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>", re.I)
_TASK_STATUS = re.compile(r"<status>([^<]+)</status>", re.I)
_ENDED_STATUSES = ("completed", "failed", "killed", "stopped", "expired")


def _ended_ids(record: dict) -> set[str]:
    """The background tool-use ids a user record reports as ended (a final task-notification status)."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    else:
        text = ""
    ended = set()
    for block in _TASK_NOTICE.findall(text) or ([text] if text else []):
        m = _TOOL_USE_ID.search(block)
        s = _TASK_STATUS.search(block)
        if m and s and s.group(1).lower() in _ENDED_STATUSES:
            ended.add(m.group(1))
    return ended


def _launched_tasks(record: dict) -> list[Background]:
    """Background tasks an assistant record launches: any tool_use with `input.run_in_background` true, or a
    Monitor tool_use (SPEC §5). Carries the record's timestamp as `started_ms`."""
    message = record.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    out = []
    if not isinstance(blocks, list):
        return out
    ts = _timestamp_ms(record.get("timestamp"))
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        name = str(b.get("name") or "")
        inp = b.get("input")
        inp = inp if isinstance(inp, dict) else {}
        if not (inp.get("run_in_background") or name == "Monitor"):
            continue
        command = str(inp.get("command") or "") if name == "Bash" else ""
        out.append(Background(
            tool_use_id=str(b.get("id") or ""),
            tool=name,
            description=str(inp.get("description") or "").strip(),
            command=command,
            started_ms=ts,
        ))
    return out


# --- endless commands (SPEC §5): servers and followers that run until stopped -------------------------------
# Each pattern has a comment naming the case it covers.
_ENDLESS: tuple[re.Pattern, ...] = (
    # `npm|pnpm|yarn|bun run dev|start|serve`: a dev/build server that runs until stopped.
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+run\s+(?:dev|start|serve)\b"),
    # `next dev`: the Next.js dev server.
    re.compile(r"\bnext\s+dev\b"),
    # Python web servers / REPLs that run until stopped.
    re.compile(r"\buvicorn\b"),
    re.compile(r"\bgunicorn\b"),
    re.compile(r"\bflask\s+run\b"),
    re.compile(r"\bmanage\.py\s+runserver\b"),
    re.compile(r"\bpython\s+-m\s+http\.server\b"),
    re.compile(r"\bjupyter\b"),
    # log/port/stream followers that run until stopped.
    re.compile(r"\btail\s+-(?:f|F)\b"),
    re.compile(r"\bjournalctl\s+-f\b"),
    re.compile(r"\bkubectl\s+logs\s+-f\b"),
    # `cargo watch` re-runs on every change forever.
    re.compile(r"\bcargo\s+watch\b"),
)


def is_endless(command: str) -> bool:
    """True when `command` runs until stopped (a server or follower), so it never makes a session watching
    (SPEC §5). Everything else ends by itself and counts."""
    c = command.strip()
    if any(p.search(c) for p in _ENDLESS):
        return True
    # `vite` alone is a dev server that runs until stopped; `vite build` is a build that ends.
    if re.search(r"\bvite\b", c) and not re.search(r"\bvite\s+build\b", c):
        return True
    # `docker compose up` / `docker-compose up` run a stack until stopped, unless detached.
    if re.search(r"\b(?:docker\s+compose|docker-compose)\s+up\b", c) \
            and not re.search(r"(?:-d|--detach)\b", c):
        return True
    # a `--watch` of a builder (tsc/webpack/jest/vitest/nodemon) follows files until stopped; `gh`'s
    # `--watch` is a poll that ends, so it never counts.
    return bool(re.search(r"\b(?:tsc|webpack|jest|vitest|nodemon)\b", c)
               and "--watch" in c
               and not re.search(r"\bgh\b", c))


def _is_prompt(record: dict) -> bool:
    """A user record that starts a turn: anything but the tool results Claude Code also records as user."""
    message = record.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    return not (isinstance(blocks, list)
                and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks))


def scan(transcript: Path, max_len: int = 60) -> Scan:
    """A `Scan` from the tail of `transcript`: `activity` from the newest concrete assistant turn,
    `model_short`/`effort` from the newest non-sidechain assistant record, `reply_ms` (SPEC §5, unread) from the
    newest finding (`is_finding`) written mid-turn (a tool call follows it before the next prompt), `end_reply_ms` from
    the newest finding in the newest turn (whether or not a tool call follows it), and `background` the launched,
    not-ended background tasks (oldest first) with `launched_any` whether any background record appeared. Everything
    is read from the file in a single pass; each is empty/None when the file is missing/unreadable or no suitable
    record is found."""
    lines = _scan_tail(transcript)
    if lines is None:
        return Scan(None, None, None, None, None, (), False)
    model = effort = reply_ms = end_reply_ms = None
    newest = replied = tool_after = False
    end_turn = True     # still in the newest turn until we walk back past its starting prompt
    desc = None
    launched: list[Background] = []
    ended: set[str] = set()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue            # malformed line: ignore
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        if record.get("type") == "user" and _is_prompt(record):
            tool_after = False          # walking back past a prompt: the tool calls seen belong to a later turn
            end_turn = False            # walking back past a prompt: the records seen belong to an older turn
        if record.get("type") == "user":
            ended.update(_ended_ids(record))
            continue
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        # Claude Code records API errors (e.g. no network right after a reboot) as assistant messages
        # with model "<synthetic>": not a real turn, so they never set the model, effort or activity.
        if record.get("isApiErrorMessage") or (
                isinstance(message, dict) and str(message.get("model", "")).startswith("<")):
            continue
        launched.extend(_launched_tasks(record))
        if not newest:
            newest = True
            if isinstance(message, dict):
                model = _model_short(message.get("model"))
            eff = record.get("effort")
            effort = eff if isinstance(eff, str) else None
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        if desc is None:
            d = _describe_blocks(blocks)
            if d:
                desc = _truncate(_single(d), max_len)
        for b in reversed(blocks):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tool_after = True
            elif (not replied and tool_after and b.get("type") == "text"
                  and is_finding(str(b.get("text") or ""))):
                replied = True
                reply_ms = _timestamp_ms(record.get("timestamp"))
            if end_turn and end_reply_ms is None and b.get("type") == "text" \
                    and is_finding(str(b.get("text") or "")):
                end_reply_ms = _timestamp_ms(record.get("timestamp"))
    # oldest first (we walked backwards); drop the launched tasks a later notice reported ended
    background = tuple(t for t in reversed(launched) if t.tool_use_id not in ended)
    return Scan(desc, model, effort, reply_ms, end_reply_ms, background, bool(launched))


def describe(transcript: Path, max_len: int = 60) -> str | None:
    """A single-line description of the newest concrete assistant turn in `transcript`, or None when the file
    is missing/unreadable or no suitable record is found."""
    return scan(transcript, max_len)[0]


def describe_config(transcript: Path) -> tuple[str | None, str | None]:
    """(model_short, effort) for the newest non-sidechain assistant record in `transcript`."""
    return scan(transcript)[1:3]
