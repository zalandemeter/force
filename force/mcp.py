"""`force mcp`: Force's local MCP server (SPEC §50), stdio, for Chuck's session only.

JSON-RPC 2.0, one message per line on stdin/stdout (the MCP stdio transport). Every tool runs this install's
own `force` command (config.launcher_path(): FORCE_BIN for a development instance) as a subprocess, so the
server and the CLI never disagree. Read tools change nothing; change tools are announced as such (the agent
asks the user for each call); deleting, archiving, uninstalling, updating and typing into other sessions are
not offered at all. Every change is logged to <state>/support/actions.jsonl.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TIMEOUT = 120


def _s(desc: str, **props) -> dict:
    required = [k for k, v in props.items() if not v.pop("optional", False)]
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False,
            "description": desc} if props else {"type": "object", "properties": {}, "additionalProperties": False}


def _str(desc: str, optional: bool = False, enum: list[str] | None = None) -> dict:
    d = {"type": "string", "description": desc}
    if enum:
        d["enum"] = enum
    if optional:
        d["optional"] = True
    return d


def _int(desc: str, optional: bool = True) -> dict:
    return {"type": "integer", "description": desc, "optional": optional}


# name -> (description, input schema, read-only?)
TOOLS: dict[str, tuple[str, dict, bool]] = {
    "status": ("Every session with its state, kind, folder and age, the plan limits, and the counts "
               "(force list --json).", _s(""), True),
    "settings_get": ("All settings with their current values, the advanced ones too (force settings --all "
                     "--json).", _s(""), True),
    "hosts_list": ("The remote hosts Force knows, and whether each is connected (force host list).", _s(""), True),
    "doctor": ("Force's health check (force doctor), without repairing anything.", _s(""), True),
    "version": ("The installed Force version (force --version).", _s(""), True),
    "logs_tail": ("The last lines of Force's daemon log (the systemd journal of force.service, or "
                  "<state>/daemon.log without systemd).", _s("", lines=_int("how many lines (default 60, max 400)")),
                  True),
    "issues_search": ("Search Force's GitHub issues, open and closed (no login; 10 searches a minute).",
                      _s("", words=_str("words to search for")), True),
    "issue_link": ("Build the pre-filled GitHub link for a new issue and copy it to the user's clipboard. "
                   "Fields (text only): bug: what, expected, steps, version, environment, doctor, logs; feature: "
                   "problem, idea, alternatives. The bug form's 'Which part' is picked by the user in the form. "
                   "Filing is the user's click.",
                   {"type": "object", "additionalProperties": False, "required": ["kind", "title"],
                    "properties": {"kind": {"type": "string", "enum": ["bug", "feature"]},
                                   "title": {"type": "string"},
                                   "fields": {"type": "object", "additionalProperties": {"type": "string"}}}},
                   True),
    "settings_set": ("Change a setting (force settings set KEY VALUE). Say what you change first.",
                     _s("", key=_str("the setting's key, camelCase (settings_get lists them)"),
                        value=_str("its new value")), False),
    "settings_unset": ("Put a setting back to its default (force settings unset KEY).",
                       _s("", key=_str("the setting's key")), False),
    "host_add": ("Add a remote host (force host add NAME SSH): checks the ssh connection first and installs "
                 "Force there when missing (unless no_install).",
                 _s("", name=_str("a short name for it in Force"),
                    ssh=_str("how ssh reaches it: a ~/.ssh/config host or user@host", optional=True),
                    no_install={"type": "boolean", "description": "only add it if Force is already installed "
                                "there", "optional": True}), False),
    "host_remove": ("Remove a remote host from Force (its sessions there keep running).",
                    _s("", name=_str("the host's name in Force")), False),
    "host_upgrade": ("Update Force on a remote host to this machine's version (force host upgrade NAME).",
                     _s("", name=_str("the host's name in Force")), False),
    "session_new": ("Start a new session (force new), without switching to it.",
                    _s("", name=_str("its name (omit: named after the first prompt)", optional=True),
                       cwd=_str("its folder (default: home)", optional=True),
                       kind=_str("claude, opencode or shell", optional=True, enum=["claude", "opencode", "shell"])),
                    False),
    "session_rename": ("Rename a session (force rename OLD NEW).",
                       _s("", old=_str("its name now"), new=_str("the new name")), False),
    "session_move": ("Move a session up or down in the sidebar, or to another folder (force move NAME "
                     "up|down|FOLDER).", _s("", name=_str("the session"), to=_str("up, down or a folder")), False),
    "doctor_fix": ("Force's health check and repair (force doctor --fix).", _s(""), False),
}
READ_TOOLS = tuple(n for n, t in TOOLS.items() if t[2])


def _schema(s: dict) -> dict:
    """Drop the `optional` helper keys (already turned into `required`)."""
    s = json.loads(json.dumps(s))
    for v in (s.get("properties") or {}).values():
        if isinstance(v, dict):
            v.pop("optional", None)
    s.pop("description", None)
    return s


def tool_list() -> list[dict]:
    out = []
    for name, (desc, schema, ro) in TOOLS.items():
        tool = {"name": name, "description": desc, "inputSchema": _schema(schema),
                "annotations": {"readOnlyHint": ro, "destructiveHint": False, "idempotentHint": ro,
                                "openWorldHint": name in ("issues_search", "host_add", "host_upgrade")}}
        if not ro:
            # a change to the user's Force always asks, whatever the agent's permission mode (Claude Code honours
            # this in auto and bypass mode too)
            tool["_meta"] = {"anthropic/requiresUserInteraction": True}
        out.append(tool)
    return out


# --------------------------------------------------------------------------- running tools ---------------
def _force(*args: str, timeout: int = TIMEOUT) -> tuple[int, str]:
    try:
        r = subprocess.run([config.launcher_path(), *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f"force {' '.join(args)}: no answer after {timeout} s"
    except OSError as e:
        return 127, f"cannot run force: {e}"
    text = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
    return r.returncode, text.strip() or ("done" if r.returncode == 0 else f"exit {r.returncode}")


def _log_action(paths, tool: str, args: dict, rc: int) -> None:
    d = paths.state / "support"
    try:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "actions.jsonl", "a") as f:
            f.write(json.dumps({"at": time.time(), "tool": tool, "args": args, "ok": rc == 0}) + "\n")
    except OSError:
        pass


def _need(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{key} is required")
    return v.strip()


def call(name: str, args: dict, paths=None, ssh_check=None) -> tuple[bool, str]:
    """Run one tool; (ok, text)."""
    paths = paths or config.get_paths()
    if name not in TOOLS:
        return False, f"unknown tool {name}"
    args = args or {}
    try:
        if name == "status":
            rc, out = _force("list", "--json")
        elif name == "settings_get":
            rc, out = _force("settings", "--all", "--json")
        elif name == "hosts_list":
            rc, out = _force("host", "list")
        elif name == "doctor":
            rc, out = _force("doctor")
            rc = 0                     # problems found are an answer, not a failure of the tool
        elif name == "version":
            rc, out = _force("--version")
        elif name == "logs_tail":
            n = args.get("lines", 60)
            n = 60 if not isinstance(n, int) else max(1, min(400, n))
            rc, out = 0, _logs(paths, n)
        elif name == "issues_search":
            from . import support
            words = _need(args, "words")
            try:
                rc, out = 0, support.format_results(support.search(words))
            except support.SupportError as e:
                rc, out = 1, str(e)
        elif name == "issue_link":
            from . import support
            fields = args.get("fields") or {}
            if not isinstance(fields, dict):
                raise ValueError("fields must be an object of field id -> text")
            try:
                url, short = support.issue_link(_need(args, "kind"), _need(args, "title"),
                                                {str(k): str(v) for k, v in fields.items()})
            except support.SupportError as e:
                return False, str(e)
            copied = support.copy(url, paths)
            note = (f"\n(shortened to fit a link: {', '.join(short)}; the user pastes the full text after opening)"
                    if short else "")
            return True, url + ("\n(copied to the clipboard)" if copied else "") + note
        elif name == "settings_set":
            rc, out = _force("settings", "set", _need(args, "key"), str(args.get("value", "")))
        elif name == "settings_unset":
            rc, out = _force("settings", "unset", _need(args, "key"))
        elif name == "host_add":
            hname = _need(args, "name")
            target = (args.get("ssh") or hname).strip()
            ok, why = (ssh_check or _ssh_check)(target)
            if not ok:
                return False, (f"ssh cannot reach {target} without a password: {why}\nSet up a key first "
                               f"(ssh-keygen, then ssh-copy-id {target}), or a Host entry in ~/.ssh/config.")
            argv = ["host", "add", hname] + ([args["ssh"].strip()] if args.get("ssh") else [])
            if args.get("no_install"):
                argv.append("--no-install")
            rc, out = _force(*argv, timeout=600)
        elif name == "host_remove":
            rc, out = _force("host", "remove", _need(args, "name"))
        elif name == "host_upgrade":
            rc, out = _force("host", "upgrade", _need(args, "name"), timeout=600)
        elif name == "session_new":
            argv = ["new"]
            if args.get("name"):
                argv.append(str(args["name"]).strip())
            if args.get("cwd"):
                argv += ["--cwd", str(args["cwd"])]
            if args.get("kind") == "shell":
                argv.append("--shell")
            elif args.get("kind") in ("claude", "opencode"):
                argv += ["--kind", args["kind"]]
            argv.append("--no-switch")
            rc, out = _force(*argv)
        elif name == "session_rename":
            rc, out = _force("rename", _need(args, "old"), _need(args, "new"))
        elif name == "session_move":
            rc, out = _force("move", _need(args, "name"), _need(args, "to"))
        elif name == "doctor_fix":
            rc, out = _force("doctor", "--fix")
        else:                                  # pragma: no cover (every TOOLS name is handled above)
            return False, f"unknown tool {name}"
    except ValueError as e:
        return False, str(e)
    if not TOOLS[name][2]:
        _log_action(paths, name, args, rc)
    return rc == 0, out


def _ssh_check(target: str) -> tuple[bool, str]:
    if not re.fullmatch(r"[A-Za-z0-9._@:\[\]-]+", target):
        return False, "not a host name"
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, "true"],
                           capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr.strip().splitlines() or ["no answer"])[-1]


def _logs(paths, n: int) -> str:
    log = paths.state / "daemon.log"
    if log.exists():
        try:
            return "\n".join(log.read_text(errors="replace").splitlines()[-n:]) or "(empty)"
        except OSError as e:
            return f"cannot read {log}: {e}"
    try:
        r = subprocess.run(["journalctl", "--user", "-u", "force.service", "-n", str(n), "--no-pager", "-o",
                            "short-iso"], capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or (r.stderr.strip() or "(no log lines)")
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"no log available: {e}"


# --------------------------------------------------------------------------- the protocol ----------------
def handle(msg: dict, paths=None) -> dict | None:
    """One JSON-RPC message -> its response (None for a notification)."""
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if mid is None:
        return None                                       # notifications (initialized, cancelled, …)

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if method == "initialize":
        want = params.get("protocolVersion")
        from .updates import current_version
        return ok({"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[1],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "force", "title": "Force", "version": current_version() or "dev"},
                   "instructions": "Force's own tools: read the install's state freely; every change tool "
                                   "changes the user's Force, so say what it will do before calling it."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return err(-32602, f"unknown tool: {name}")
        good, text = call(name, params.get("arguments") or {}, paths)
        return ok({"content": [{"type": "text", "text": text}], "isError": not good})
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, f"method not found: {method}")


def serve(inp=None, out=None) -> int:
    inp = inp or sys.stdin
    out = out or sys.stdout
    paths = config.get_paths()
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "parse error"}}) + "\n")
            out.flush()
            continue
        batch = msg if isinstance(msg, list) else [msg]
        replies = [r for r in (handle(m, paths) for m in batch if isinstance(m, dict)) if r is not None]
        if replies:
            out.write(json.dumps(replies if isinstance(msg, list) else replies[0]) + "\n")
            out.flush()
    return 0
