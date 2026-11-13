"""One class per session kind (SPEC §49.1): what differs between Claude Code, OpenCode and a shell.

A new agent (e.g. Codex) is one more `Kind` subclass in ALL; the daemon, `force run`, the snapshot and the
dock's limits ask the kind instead of branching on it. Kept import-light: `force run` imports it.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import claude as claude_mod, opencode as opencode_mod, procinfo, usage as usage_mod
from .claude import AgentStatus
from .registry import Entry, valid_name


@dataclass
class Reading:
    """One tick's look at a session's agent pane."""
    status: AgentStatus | None = None   # what the SPEC §5 rules read; None = no answer (starting / down)
    meta: object = None                 # kind data for the card (opencode.Meta, ShellProgram)
    version: str | None = None          # the version the pane runs
    source: str | None = None           # the `why` clause saying where the status came from (§43.3)


@dataclass
class Card:
    activity: str | None = None
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    title: str | None = None            # replaces the pane title (a shell's interactive program)
    reply_ms: int | None = None         # when the agent last wrote you a finding (SPEC §5, unread)


class Kind:
    name = ""
    label = ""
    mark = ""
    direct = False            # `force run` execs it in stage 1, without the login-shell re-exec (a shell)
    name_in_agent = False     # the agent carries the session name: a live rename restarts it (§8)

    def available(self, paths, cfg) -> bool:
        return True

    def exec_argv(self, cockpit, entry: Entry) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError

    def read(self, d, entry: Entry, pane) -> Reading:
        return Reading()

    def latest_version(self, paths) -> str | None:
        return None

    def card(self, d, entry: Entry, pane, reading: Reading) -> Card:
        return Card()

    def follow(self, d, entry: Entry, reading: Reading) -> None:
        pass

    def limits(self, paths) -> dict | None:
        return None

    def first_prompt(self, paths, entry: Entry, reading: Reading) -> str | None:
        return None


# --- Claude Code ------------------------------------------------------------------------------------------
class Claude(Kind):
    name = "claude"
    label = "Claude Code"
    mark = "✻"
    limits_label = "Claude"
    name_in_agent = True

    def available(self, paths, cfg) -> bool:
        return os.access(paths.claude_bin, os.X_OK) or shutil.which("claude") is not None

    def exec_argv(self, cockpit, entry):
        statusline = claude_mod.statusline_command(Path(cockpit.paths.tmux_conf).parent / "statusline.py",
                                                   cockpit.paths.state / "usage.json")
        extra_settings, extra_args = None, None
        if entry.extra.get("role") == "support":       # Chuck (SPEC §50)
            from . import support
            model = cockpit.cfg.support_model or ""
            support.prepare_dir(cockpit.paths, model)
            extra_settings, extra_args = support.claude_launch(cockpit.paths, model)
        argv = claude_mod.build_argv(cockpit.paths.claude_bin, cockpit.paths.claude_dir, entry.name, entry.session_id,
                                     statusline, memory_dir=entry.extra.get("memory_dir"),
                                     remote_control=not os.environ.get("FORCE_NO_REMOTE_CONTROL"),
                                     extra_settings=extra_settings, extra_args=extra_args)
        return argv, {}

    def read(self, d, entry, pane):
        if pane is None or pane.dead or not pane.pid:
            return Reading()
        status = claude_mod.read_status(d.paths.claude_dir, pane.pid)
        src = None
        if status is not None:
            if status.status_updated_ms is None:
                src = "claude status file, no status yet"
            else:
                age = max(0, d._now_ms() - status.status_updated_ms) // 1000
                src = f"claude status file, updated {age} s ago"
        return Reading(status=status, version=claude_mod.process_version(pane.pid), source=src)

    def latest_version(self, paths):
        return claude_mod.latest_version(paths.claude_bin)

    def card(self, d, entry, pane, reading):
        sc = d._config_for(entry.session_id)
        from .daemon import shown_activity
        reply_ms = sc.reply_ms
        if reading.status is not None and reading.status.status == "shell":
            # SPEC §5: the reply that ends a turn into watching counts as a finding for `unread`.
            both = [x for x in (reply_ms, sc.end_reply_ms) if x is not None]
            if both:
                reply_ms = max(both)
        return Card(activity=shown_activity(reading.status, sc.activity, d._waiting.get(entry.name)),
                   model=sc.model, effort=sc.effort,
                   mode=d._mode_for(entry.name, pane), reply_ms=reply_ms)

    def follow(self, d, entry, reading):
        d._follow_claude(entry, reading.status)

    def limits(self, paths):
        return usage_mod.read_usage(paths.state / "usage.json")

    def first_prompt(self, paths, entry, reading):
        t = claude_mod.find_transcript(paths.claude_dir, entry.session_id)
        return claude_mod.first_prompt(t) if t is not None else None


# --- OpenCode ---------------------------------------------------------------------------------------------
_OC_HOLD = 10.0     # an OpenCode TUI's API that misses an answer keeps its last state this long


class OpenCode(Kind):
    name = "opencode"
    label = "OpenCode"
    mark = "▣"

    def available(self, paths, cfg) -> bool:
        return opencode_mod.installed() is not None

    def exec_argv(self, cockpit, entry):
        from . import registry
        port = opencode_mod.free_port()        # a fresh API port per start, for the daemon's status reads
        with registry.locked(cockpit.paths.home) as reg:
            e = reg.get(entry.name)
            if e is not None:
                e.extra["oc_port"] = port
        binary = opencode_mod.installed()
        if binary is None:
            raise RuntimeError("OpenCode is not installed"
                               + (" yet (force opencode-update)" if cockpit.cfg.opencode_managed else ""))
        # Force handles updates (its own copy, or restarts onto the admin's): OpenCode's own update dialog
        # would sit over the prompt and swallow the first keystrokes
        env = {"OPENCODE_DISABLE_AUTOUPDATE": "1"}
        if entry.extra.get("role") == "support":       # Chuck (SPEC §50): his folder's opencode.json and AGENTS.md
            from . import support
            support.prepare_dir(cockpit.paths, cockpit.cfg.support_model or "")
        return opencode_mod.argv(port, entry.extra.get("oc_session"), entry.cwd, binary), env

    def read(self, d, entry, pane):
        name = entry.name
        if pane is None or pane.dead or not pane.pid:
            return Reading(meta=d._oc_meta.get(name))
        status, meta = opencode_mod.read_status(entry.extra.get("oc_port"), pane.pid,
                                                entry.extra.get("oc_session"), pane.started_ms)
        now = d.clock()
        if status is not None:
            d._oc_last[name] = (status, meta, now)
            src = "opencode api answered"
        else:
            # the TUI busy for a moment (its first prompt sets up the session) misses the 0.4 s answer: keep its
            # last state for a while instead of showing it down
            last = d._oc_last.get(name)
            if last is not None and last[0].pid == pane.pid and now - last[2] < _OC_HOLD:
                status, meta = last[0], last[1]
                src = f"opencode api missed; holding last state from {now - last[2]:.1f} s ago"
            else:
                src = "opencode api no answer"
        d._oc_meta[name] = meta
        return Reading(status=status, meta=meta, version=meta.version if meta is not None else None, source=src)

    def latest_version(self, paths):
        # OpenCode's binary is the admin's (or Force's own copy): a restart only moves an idle session onto a newer
        # one already installed (SPEC §36)
        return opencode_mod.installed_version()

    def card(self, d, entry, pane, reading):
        # the same slots as the other agents' cards (SPEC §36): model, effort = the model variant unless "default",
        # the mode badge = the agent. The activity phrase only while busy, or for a question (a permission prompt
        # reads "needs permission" by itself).
        meta = reading.meta
        status = reading.status
        if meta is not None and meta.question:
            act = "asks you a question"
        else:
            act = meta.activity if meta is not None and status is not None and status.status == "busy" else None
        model = opencode_mod.model_label(meta.model) if meta is not None else None
        effort = meta.variant if meta is not None and meta.variant not in (None, "", "default") else None
        agent = d._oc_agent_for(entry.name, pane) or (meta.agent if meta is not None else None)
        mode = {"build": "oc-build", "plan": "oc-plan"}.get(agent) if agent else None
        return Card(activity=act, model=model, effort=effort, mode=mode,
                    reply_ms=d._oc_reply_for(entry.name, pane, meta))

    def follow(self, d, entry, reading):
        d._follow_opencode(entry, reading.status)

    def first_prompt(self, paths, entry, reading):
        """OpenCode titles a session itself from its first message, during the first turn: that title once it
        is there, else (the turn over and still no title) the first message's words."""
        meta = reading.meta
        if meta is not None and getattr(meta, "title", None):
            return meta.title
        sid = entry.extra.get("oc_session")
        port = entry.extra.get("oc_port")
        st = reading.status
        if not sid or not port or st is None or st.status != "idle":
            return None
        return opencode_mod.first_message(port, sid)


# --- Shell ------------------------------------------------------------------------------------------------
SHELLS = frozenset({"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh", "nu", "elvish", "xonsh"})
# programs you sit in: "working" would be wrong for them (SPEC §49.3)
INTERACTIVE = frozenset({
    "vim", "nvim", "vi", "view", "nano", "emacs", "micro", "helix", "hx", "kak", "joe", "mcedit", "ed",
    "less", "more", "most", "man", "info", "bat",
    "top", "htop", "btop", "atop", "nvtop", "glances", "iotop", "watch",
    "ssh", "mosh", "mosh-client", "telnet", "screen", "tmux", "su", "sudo",
    "python", "python3", "ipython", "node", "irb", "psql", "mysql", "sqlite3", "redis-cli", "ghci", "R",
    "lazygit", "tig", "gitui", "ranger", "nnn", "lf", "mc", "yazi", "fzf", "k9s", "lazydocker",
    "claude", "opencode", "codex",
})
SHELL_MIN_BUSY_S = 2.0      # a program shows as working once it has run this long (a prompt hook never does)


@dataclass
class ShellProgram:
    program: str | None = None      # the pane's foreground program when it is not the shell
    interactive: bool = False


def _comm(pid: int) -> str | None:
    return procinfo.comm(pid)


def _base_program(cmd: str | None) -> str | None:
    if not cmd:
        return None
    cmd = cmd.strip().lstrip("-")          # a login shell shows as "-bash"
    return os.path.basename(cmd) or None


class Shell(Kind):
    name = "shell"
    label = "Shell"
    mark = ""
    direct = True

    def exec_argv(self, cockpit, entry):
        return ["bash", "-l"], {}

    def read(self, d, entry, pane):
        """A synthetic status from the pane's foreground program (SPEC §49.3): busy while a program has run for
        SHELL_MIN_BUSY_S, idle otherwise; the busy -> idle step is a finished turn like an agent's."""
        if pane is None or pane.dead or not pane.pid:
            d._shell_prog.pop(entry.name, None)
            return Reading()
        prog = _base_program(getattr(pane, "command", None))
        now = d.clock()
        now_ms = int(now * 1000)
        prev = d._shell_prog.get(entry.name)          # (pid, program, since clock, last change ms)
        if prev is None or prev[0] != pane.pid:
            prev = (pane.pid, None, now, pane.started_ms or now_ms)
        _pid, prev_prog, since, changed_ms = prev
        if prog in SHELLS or (prog is not None and prog == _comm(pane.pid)):
            prog = None         # at the prompt: the shell itself, or the pane's own first program
        if prog != prev_prog:
            since, changed_ms = now, now_ms
        d._shell_prog[entry.name] = (pane.pid, prog, since, changed_ms)
        interactive = prog is not None and prog in INTERACTIVE
        busy = prog is not None and not interactive and now - since >= SHELL_MIN_BUSY_S
        status = AgentStatus(pid=pane.pid, session_id="", name=None, name_source=None,
                             status="busy" if busy else "idle", status_updated_ms=changed_ms,
                             waiting_for=None, cwd=None)
        src = f"foreground program {prog}" if prog else "shell prompt"
        return Reading(status=status, meta=ShellProgram(prog, interactive), source=src)

    def card(self, d, entry, pane, reading):
        meta = reading.meta
        if not isinstance(meta, ShellProgram) or meta.program is None:
            return Card()
        if meta.interactive:
            return Card(title=meta.program)
        st = reading.status
        return Card(activity=f"running {meta.program}" if st is not None and st.status == "busy" else None)


ALL: dict[str, Kind] = {k.name: k for k in (Claude(), OpenCode(), Shell())}


def get(name: str | None) -> Kind:
    """The kind by name; an unknown or missing kind counts as Claude, as the registry always did."""
    return ALL.get(name or "claude", ALL["claude"])


def of(entry: Entry) -> Kind:
    return get(entry.extra.get("kind"))


# --- automatic names (SPEC §49.6) -------------------------------------------------------------------------
AUTO_NAME_BASE = "new"
NAME_FROM_PROMPT_MAX = 20
_FILLERS = {"please", "pls", "hey", "hi", "hello", "ok", "okay", "so", "can", "could", "would", "will", "you",
            "u", "i", "we", "want", "need", "like", "to", "lets", "let", "s", "the", "a", "an", "now",
            "just", "help", "me", "us", "go", "ahead", "and", "also", "then", "yo", "claude"}


def name_from_prompt(text: str | None) -> str | None:
    """A session name from what the user first typed: the slug of its words with leading fillers dropped
    ("please can you fix the login bug" -> "fix-the-login-bug"), cut at a word boundary to at most 20 characters
    (a single longer word is cut). None when nothing usable is left."""
    if not text:
        return None
    # slugify's own 31-character cut would come before the fillers are dropped: split the words here
    words = [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]
    while words and words[0] in _FILLERS:
        words.pop(0)
    out = ""
    for w in words:
        cand = f"{out}-{w}" if out else w
        if len(cand) > NAME_FROM_PROMPT_MAX:
            break
        out = cand
    if not out and words:
        out = words[0][:NAME_FROM_PROMPT_MAX]
    out = out.strip("-")
    return out if out and valid_name(out) else None


def free_name(base: str, taken, upto: int = 9) -> str | None:
    """base, else base-2 … base-<upto>: the first not in `taken` (a callable or a container)."""
    is_taken = taken if callable(taken) else (lambda n: n in taken)
    for cand in [base] + [f"{base}-{i}" for i in range(2, upto + 1)]:
        if valid_name(cand) and not is_taken(cand):
            return cand
    return None
