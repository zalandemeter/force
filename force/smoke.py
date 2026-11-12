"""`force smoke`: a self-test of Force against the REAL Claude Code and OpenCode (SPEC §37).

Force leans on how those tools behave (status files, the TUI API, prompts, `--resume`, memory and project
directories). An update of either can break a cockpit that the unit tests (fakes) still pass, so this runs
them for real, through Force's own lifecycle and daemon, on a separate tmux server with its own registry
and state:

  Claude:   start -> a turn (working, then your turn) -> needs you (a permission prompt) -> restart resumes
            the same conversation -> move to another folder (new CLAUDE.md, memory kept) -> it still knows
            the earlier reply
  OpenCode: the same, with the move re-homing the session

Work folders (two small git repos with their own CLAUDE.md / AGENTS.md and permission settings) live under
<state>/smoke/work and are kept between runs (OpenCode's project ids stay the same); each run's registry,
state and tmux server are thrown away, and so are the Claude transcripts and OpenCode sessions it made.
The result goes to <state>/smoke/last.json (shown by the sidebar when a check failed) and history.jsonl.

`--if-due` (the hourly timer) runs only when Claude Code, OpenCode or Force changed since the last run, the
last run failed more than 6 h ago, or it is older than a day. Sessions use the `haiku` model
(FORCE_SMOKE_MODEL) and never register for Remote Control.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import procinfo

DUE_AFTER_S = 24 * 3600
RETRY_FAILED_S = 6 * 3600
POLL_S = 0.5

LOGGED_OUT = "Claude Code logged out"

# what the work folders hold (the move checks that the second folder's instructions apply)
_CLAUDE_MD = "When asked for your codename, answer {code}.\n"
_CLAUDE_SETTINGS = {"permissions": {"ask": ["Bash(touch:*)"]}}
_OPENCODE_JSON = {"$schema": "https://opencode.ai/config.json", "permission": {"bash": "ask"}}

PROMPT_PONG = "Reply with exactly the single word PONG and nothing else."
PROMPT_TOUCH = "Use your shell tool to run exactly this command: touch smoke-marker"
PROMPT_LONG = "Write the whole numbers from 1 to 400 separated by single spaces, nothing else."
PROMPT_RECALL = ("Reply with the single word you replied with earlier in this conversation, then a space, "
                 "then your codename. Nothing else.")


class SmokeFail(Exception):
    pass


class _Lines:
    """Lines of a pipe through a reader thread (select() on a buffered text pipe misses lines Python has
    already read ahead); get() returns None on timeout or EOF."""

    def __init__(self, stream):
        import queue
        import threading
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, args=(stream,), daemon=True).start()

    def _pump(self, stream) -> None:
        for line in stream:
            self.q.put(line.rstrip("\n"))
        self.q.put(None)

    def get(self, timeout: float) -> str | None:
        import queue
        try:
            return self.q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None


# --------------------------------------------------------------------------- versions / due ------------
def versions(paths) -> dict:
    from . import opencode
    v = {"force": os.path.basename(os.path.realpath(paths.home / "app")) if (paths.home / "app").exists() else None}
    try:
        out = subprocess.run([str(paths.claude_bin), "--version"], capture_output=True, text=True, timeout=30)
        v["claude"] = out.stdout.split()[0] if out.stdout.split() else None
    except (OSError, subprocess.TimeoutExpired):
        v["claude"] = None
    v["opencode"] = opencode.installed_version()
    return v


def claude_logged_in(claude_bin) -> bool | None:
    """Whether Claude Code is logged in; None when we cannot tell (no binary, no `auth status`)."""
    try:
        out = subprocess.run([str(claude_bin), "auth", "status"], capture_output=True, text=True,
                            errors="replace", timeout=30)
        try:
            d = json.loads(out.stdout)
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        logged = d.get("loggedIn")
        return logged if isinstance(logged, bool) else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def reason(checks) -> str | None:
    """A short human reason the last smoke run failed (sidebar / doctor), None when nothing failed."""
    if not isinstance(checks, list):
        return None
    for c in checks:
        if not isinstance(c, dict) or c.get("ok"):
            continue
        detail = c.get("detail")
        if not isinstance(detail, str):
            detail = ""
        if c.get("name") == "claude: logged in" or any(
                m in detail.lower() for m in ("login expired", "please run /login", "not logged in", "/login")):
            return LOGGED_OUT
        if TOOL_CALLS_AS_TEXT in detail:
            return f"{_KIND_LABEL.get(str(c.get('name', '')).partition(':')[0].strip(), 'agent')}: tool calls fail"
        return failed_phrase(c.get("name"))
    return None


TOOL_CALLS_AS_TEXT = "tool calls as text"
_TOOL_CALL_MARKS = ("tool_call", "<invoke name=", "invoke name=\"", "<function=", "<tool_call>")


def tool_call_as_text(reply: str | None) -> bool:
    """A reply whose text is a raw tool call (`<｜DSML｜tool_call><invoke name="bash">…`, `<tool_call>{…}`): the model
    server handed the model's tool-call markup through as text instead of parsing it."""
    if not reply:
        return False
    low = reply.lower()
    return any(m in low for m in _TOOL_CALL_MARKS)


_KIND_LABEL = {"claude": "Claude Code", "opencode": "OpenCode", "split": "Window"}
_STEP_PHRASE = {"start": "won't start", "turn": "no reply", "needs you": "missed a prompt",
                "restart resumes": "restart lost chat", "move": "move failed", "move while busy": "move failed"}


def failed_phrase(name) -> str:
    """The sidebar's short reason for a failed check (it has about 25 columns): what broke, in words, not the
    check's name ("opencode: needs you" read like an instruction). `force smoke --last` has the details."""
    if not isinstance(name, str) or not name:
        return "self-test failed"
    kind, _, step = name.partition(":")
    label = _KIND_LABEL.get(kind.strip(), kind.strip() or "self-test")
    if kind.strip() == "split":
        return "self-test: window failed"
    return f"{label}: {_STEP_PHRASE.get(step.strip(), 'self-test failed')}"


def due(last: dict | None, now_versions: dict, now: float, logged_in=None) -> str | None:
    """Why a run is due (None: not due). `logged_in` is a no-arg callable returning bool|None, checked only
    when the last run failed because Claude was logged out."""
    if not last:
        return "no earlier run"
    if last.get("versions") != now_versions:
        return "versions changed"
    age = now - float(last.get("at", 0))
    if not last.get("ok") and reason(last.get("checks")) == LOGGED_OUT and logged_in is not None \
            and logged_in() is True:
        return "logged in again"
    if not last.get("ok") and age >= RETRY_FAILED_S:
        return "retry after a failure"
    if age >= DUE_AFTER_S:
        return "daily run"
    return None


def read_last(base: Path) -> dict | None:
    try:
        return json.loads((base / "last.json").read_text())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- the run --------------------
class Run:
    def __init__(self, live_paths, live_cfg, log):
        self.live = live_paths
        self.base = live_paths.state / "smoke"
        self.work = self.base / "work"
        self.run_dir = self.base / "run"
        self.log = log
        self.checks: list[dict] = []
        self.made_claude: list[str] = []      # session ids whose transcripts we remove
        self.made_oc: list[str] = []          # OpenCode session ids we delete
        self.opencode_managed = live_cfg.opencode_managed

    # --- setup ---------------------------------------------------------------------------------------
    def _work_dirs(self) -> tuple[Path, Path]:
        a, b = self.work / "a", self.work / "b"
        for d, code in ((a, "ALPHA"), (b, "BRAVO")):
            d.mkdir(parents=True, exist_ok=True)
            (d / "CLAUDE.md").write_text(_CLAUDE_MD.format(code=code))
            (d / "AGENTS.md").write_text(_CLAUDE_MD.format(code=code))
            (d / ".claude").mkdir(exist_ok=True)
            (d / ".claude" / "settings.json").write_text(json.dumps(_CLAUDE_SETTINGS))
            (d / "opencode.json").write_text(json.dumps(_OPENCODE_JSON))
            try:
                (d / "smoke-marker").unlink()
            except FileNotFoundError:
                pass
            if not (d / ".git").exists():      # separate projects for OpenCode (its id = the root commit)
                subprocess.run(["git", "init", "-q"], cwd=d, check=True)
                subprocess.run(["git", "-c", "user.email=smoke@force", "-c", "user.name=force smoke", "commit",
                                "-q", "--allow-empty", "-m", f"force smoke {d.name}"], cwd=d, check=True)
        return a, b

    def _environment(self) -> None:
        """This process becomes the smoke cockpit's client: its own home, state, config and tmux server."""
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)
        home, state = self.run_dir / "home", self.run_dir / "state"
        home.mkdir(parents=True)
        state.mkdir(parents=True)
        if (self.live.home / "opencode").is_dir():      # Force's own OpenCode copy (opencode_managed)
            (home / "opencode").symlink_to(self.live.home / "opencode")
        cfg = self.run_dir / "settings.json"
        cfg.write_text(json.dumps({
            "autoUpgradeSessions": False, "reloadOnChange": False,
            "opencodeManaged": self.opencode_managed,
        }))
        conf = self.run_dir / "tmux.conf"
        conf.write_text("set -g exit-empty off\nset -g remain-on-exit on\nset -g history-limit 2000\n")
        # the ui server (split mode: SPEC §39) sources its own conf next to the host's
        (conf.parent / "tmux-ui.conf").write_text(
            "set -g exit-empty off\nset -g remain-on-exit on\nset -g @force_sidebar on\n")
        os.environ.pop("INVOCATION_ID", None)           # not the service manager's: never start the live tmux unit
        os.environ.pop("FORCE_SERVICE", None)           # or the launchd agent
        os.environ.update({
            "FORCE_HOME": str(home), "FORCE_STATE": str(state), "FORCE_CONFIG": str(cfg),
            "FORCE_TMUX_SOCKET": f"forcetest-smoke-{os.getpid()}", "FORCE_TMUX_CONF": str(conf),
            "FORCE_UI_SOCKET": f"forcetest-smoke-ui-{os.getpid()}",
            "FORCE_CLAUDE_DIR": str(self.live.claude_dir), "FORCE_CLAUDE_BIN": str(self.live.claude_bin),
            "FORCE_NO_REMOTE_CONTROL": "1",
            "ANTHROPIC_MODEL": os.environ.get("FORCE_SMOKE_MODEL", "haiku"),
        })

    def setup(self):
        from .config import get_paths, load_config
        from .daemon import Daemon
        from .lifecycle import Cockpit
        from .tmux import Tmux
        from . import opencode
        self.a, self.b = self._work_dirs()
        self._environment()
        paths = get_paths()
        cfg = load_config(paths.config_file)
        opencode.set_managed(paths.home / "opencode" if cfg.opencode_managed else None)
        # the panes run `force run NAME`: the live launcher (FORCE_SMOKE_FORCE: another one, for development)
        force = os.environ.get("FORCE_SMOKE_FORCE") or shutil.which("force") or str(Path.home() / ".local/bin/force")
        self.cockpit = Cockpit(paths, cfg, Tmux(paths.tmux_socket, paths.tmux_conf), force)
        self.daemon = Daemon(self.cockpit, validator=lambda *a, **k: None, reexec=lambda *a, **k: None)
        self.daemon._step_sidebar = lambda: None     # no sidebar or header panes on the smoke server
        self.paths = paths

    # --- helpers -------------------------------------------------------------------------------------
    def say(self, text: str) -> None:
        print(text, file=self.log, flush=True)

    def view(self, name: str):
        from .state import read_snapshot
        self.daemon.tick()
        snap = read_snapshot(self.paths.snapshot)
        return next((s for s in snap.sessions if s.name == name), None) if snap else None

    def wait(self, name: str, want: set, timeout: float, what: str):
        """Tick the daemon until the session's state is one of `want`; returns the states seen."""
        seen = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            v = self.view(name)
            st = v.state.value if v else "MISSING"
            if not seen or seen[-1] != st:
                seen.append(st)
                self.say(f"    {name}: {st}")
            if st in want:
                return seen
            time.sleep(POLL_S)
        raise SmokeFail(f"{what}: no {'/'.join(sorted(want))} within {timeout:.0f} s (states: "
                        f"{' -> '.join(seen)}); screen: {self.screen(name)!r}")

    def screen(self, name: str) -> str:
        p = self.cockpit.agent_pane(name)
        try:
            return " | ".join(ln.strip() for ln in self.cockpit.tmux.capture_tail(p.pane_id, 12).splitlines()
                              if ln.strip())[-600:] if p else "(no pane)"
        except Exception as e:
            return f"(capture failed: {e})"

    def send(self, name: str, text: str) -> None:
        p = self.cockpit.agent_pane(name)
        if p is None:
            raise SmokeFail("no pane to type into")
        self.cockpit.tmux.run("send-keys", "-t", p.pane_id, "-l", text)
        time.sleep(0.5)
        self.cockpit.tmux.run("send-keys", "-t", p.pane_id, "Enter")

    def key(self, name: str, key: str) -> None:
        p = self.cockpit.agent_pane(name)
        if p is not None:
            self.cockpit.tmux.run("send-keys", "-t", p.pane_id, key)

    def turn(self, name: str, prompt: str, what: str, timeout: float = 240) -> None:
        """Send a prompt; the card must go WORKING, then finish."""
        self.send(name, prompt)
        self.wait(name, {"WORKING"}, 90, what + " (working)")
        self.wait(name, {"YOUR_TURN", "IDLE"}, timeout, what + " (done)")

    def started(self, name: str, what: str) -> None:
        """Wait until the session is up; answers Claude's "trust this folder?" for the smoke work folders
        (the card must say NEEDS_YOU meanwhile: that is a check of its own)."""
        seen = self.wait(name, {"IDLE", "YOUR_TURN", "NEEDS_YOU"}, 180, what)
        if seen[-1] == "NEEDS_YOU":
            v = self.view(name)
            if (v.activity or "") != "trust this folder?":
                raise SmokeFail(f"{what}: needs you before any prompt ({v.activity!r}); screen: {self.screen(name)!r}")
            self.say("    trusting the smoke work folder")
            self.trust(name, what)
            self.wait(name, {"IDLE", "YOUR_TURN"}, 120, what + " (after trusting)")

    def trust(self, name: str, what: str) -> None:
        """Pick "Yes, I trust this folder" in Claude's dialog: Down, then Enter only once the screen shows Yes
        selected. A Down sent while the dialog was still drawing got lost (Claude Code 2.1.318), and Enter on
        the default "No, exit" quit Claude, which came back with the same question."""
        for _ in range(4):
            self.key(name, "Down")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if re.search(r"❯\s*(\d+\.\s*)?Yes", self.screen(name)):
                    self.key(name, "Enter")
                    return
                time.sleep(0.2)
        raise SmokeFail(f"{what}: could not select 'Yes, I trust this folder'; screen: {self.screen(name)!r}")

    def check(self, name: str, fn) -> bool:
        t0 = time.monotonic()
        self.say(f"  {name} ...")
        try:
            detail = fn() or ""
            ok = True
        except SmokeFail as e:
            ok, detail = False, str(e)
        except Exception as e:                        # a bug in the smoke code or Force itself
            ok, detail = False, f"{type(e).__name__}: {e}"
        dt = round(time.monotonic() - t0, 1)
        self.checks.append({"name": name, "ok": ok, "s": dt, "detail": detail})
        self.say(f"  {name}: {'ok' if ok else 'FAILED'} ({dt} s){' - ' + detail if detail else ''}")
        return ok

    def add_entry(self, name: str, cwd: Path, kind: str):
        from .registry import locked
        with locked(self.paths.home) as reg:
            e = reg.add(name, str(uuid.uuid4()), str(cwd))
            if kind != "claude":
                e.extra["kind"] = kind
            return e

    # --- Claude --------------------------------------------------------------------------------------
    def transcript_text(self, sid: str, after: str | None = None) -> str:
        """The assistant's text in the transcript; with `after`, only what follows the last user message
        containing that text (the reply to that prompt)."""
        from .claude import find_transcript
        t = find_transcript(self.live.claude_dir, sid)
        if t is None:
            return ""
        out = []
        for line in t.read_text(errors="replace").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            content = (d.get("message") or {}).get("content")
            if after and d.get("type") == "user" and after in json.dumps(content):
                out = []
            if d.get("type") != "assistant" or not isinstance(content, list):
                continue
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    out.append(c.get("text", ""))
        return "\n".join(out)

    def reply(self, read, want: tuple[str, ...], timeout: float = 30) -> str:
        """`read()` until it contains every word of `want` (the transcript / export can lag the idle card)."""
        text = ""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            text = read().upper()
            if all(w in text for w in want):
                break
            time.sleep(1)
        return text

    def recall(self, name: str, read, what: str) -> None:
        """Ask for the earlier reply and the codename; `read()` gives the reply to that prompt. The reply can
        land a moment after the card goes idle; a model answering off topic gets asked once more."""
        reply = ""
        for attempt in (1, 2):
            self.turn(name, PROMPT_RECALL, what)
            reply = self.reply(read, ("PONG", "BRAVO"))
            if "PONG" in reply and "BRAVO" in reply:
                return
            self.say(f"    attempt {attempt}: reply {reply[-120:]!r}")
        raise SmokeFail(f"{what}: the reply was {reply[-200:]!r} (want PONG and the new folder's BRAVO)")

    def live_session_id(self, name: str) -> str | None:
        from .claude import read_status
        p = self.cockpit.agent_pane(name)
        st = read_status(self.live.claude_dir, p.pid) if p and p.pid else None
        return st.session_id if st else None

    def claude_flow(self) -> None:
        if not os.access(self.live.claude_bin, os.X_OK):
            # e.g. a laptop that only shows remote hosts (SPEC §40): nothing to test here
            self.checks.append({"name": "claude", "ok": True, "s": 0, "detail": "not installed: skipped"})
            self.say("  claude: not installed, skipped")
            return
        if claude_logged_in(self.live.claude_bin) is False:
            self.checks.append({"name": "claude: logged in", "ok": False, "s": 0,
                               "detail": "Claude Code is logged out: run claude, then /login"})
            self.say("  claude: logged out (run claude, then /login)")
            return
        name = "smoke-claude"
        e = self.add_entry(name, self.a, "claude")
        sid = e.session_id
        self.made_claude.append(sid)

        def start():
            self.cockpit.create(e)
            self.started(name, "start")

        def turn():
            self.turn(name, PROMPT_PONG, "a turn")
            text = self.reply(lambda: self.transcript_text(sid, after=PROMPT_PONG[:40]), ("PONG",))
            if "PONG" not in text:
                raise SmokeFail(f"reply without PONG: {text[-200:]!r}; screen: {self.screen(name)!r}")

        def needs_you():
            self.send(name, PROMPT_TOUCH)
            self.wait(name, {"NEEDS_YOU"}, 240, "permission prompt")
            self.key(name, "Escape")                  # deny
            self.wait(name, {"IDLE", "YOUR_TURN"}, 120, "after denying")

        def restart():
            self.cockpit.restart(name)
            self.started(name, "restart")
            got = self.live_session_id(name)
            if got != sid:
                raise SmokeFail(f"resumed {got}, expected {sid}")

        def move():
            from .claude import project_slug
            self.cockpit.move(name, str(self.b))
            self.started(name, "move")
            p = self.cockpit.agent_pane(name)
            argv = procinfo.cmdline(p.pid)
            settings = json.loads(argv[argv.index("--settings") + 1])
            if "autoMemoryDirectory" not in settings:
                raise SmokeFail(f"no autoMemoryDirectory in --settings: {settings}")
            if not (self.live.claude_dir / "projects" / project_slug(str(self.b)) / f"{sid}.jsonl").is_file():
                raise SmokeFail("the transcript did not move to the new folder's project")
            self.recall(name, lambda: self.transcript_text(sid, after=PROMPT_RECALL[:40]), "after the move")

        def move_busy():
            # a move asked for during a turn waits for it ("moves after this turn"), then the daemon moves it
            from .cli import move_folder_command
            from .registry import load
            self.send(name, PROMPT_LONG)
            self.wait(name, {"WORKING"}, 90, "a long turn (working)")
            if move_folder_command(self.cockpit, name, str(self.a)) != 0:
                raise SmokeFail("force move refused")
            e = load(self.paths.registry).get(name)
            if e.extra.get("pending_move") != str(self.a) or e.cwd != str(self.b):
                raise SmokeFail(f"not deferred: cwd {e.cwd}, pending {e.extra.get('pending_move')!r}")
            v = self.view(name)
            if v.state.value == "WORKING" and v.activity != "moves after this turn":
                raise SmokeFail(f"card says {v.activity!r}, want 'moves after this turn'")
            end = time.monotonic() + 240
            while load(self.paths.registry).get(name).cwd != str(self.a):
                if time.monotonic() > end:
                    raise SmokeFail(f"the pending move never happened; screen: {self.screen(name)!r}")
                self.daemon.tick()
                time.sleep(POLL_S)
            self.started(name, "after the pending move")
            if self.live_session_id(name) != sid:
                raise SmokeFail("a different conversation after the pending move")

        for n, fn in (("claude: start", start), ("claude: turn", turn), ("claude: needs you", needs_you),
                      ("claude: restart resumes", restart), ("claude: move", move),
                      ("claude: move while busy", move_busy)):
            if not self.check(n, fn):
                break

    # --- OpenCode ------------------------------------------------------------------------------------
    def oc_session(self, name: str) -> str | None:
        from .registry import load
        e = load(self.paths.registry).get(name)
        return e.extra.get("oc_session") if e else None

    def oc_text(self, sid: str, cwd: Path, after: str | None = None) -> str:
        from . import opencode
        out = subprocess.run([opencode.installed(), "export", sid], cwd=cwd, capture_output=True, text=True,
                             timeout=120, env=dict(os.environ, OPENCODE_DISABLE_AUTOUPDATE="1"))
        try:
            data = json.loads(out.stdout)
        except ValueError:
            return ""
        texts = []
        for m in data.get("messages") or []:
            role = (m.get("info") or {}).get("role")
            parts = m.get("parts") or []
            if after and role == "user" and any(after in (p.get("text") or "") for p in parts):
                texts = []
            if role != "assistant":
                continue
            for p in parts:
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))
        return "\n".join(texts)

    def opencode_flow(self) -> None:
        from . import opencode
        if opencode.installed() is None:
            self.checks.append({"name": "opencode", "ok": True, "s": 0, "detail": "not installed: skipped"})
            self.say("  opencode: not installed, skipped")
            return
        name = "smoke-opencode"
        e = self.add_entry(name, self.a, "opencode")

        def start():
            self.cockpit.create(e)
            self.wait(name, {"IDLE", "YOUR_TURN"}, 180, "start")
            time.sleep(3)                             # the TUI drops keys right after it draws

        def turn():
            self.turn(name, PROMPT_PONG, "a turn")
            end = time.monotonic() + 30
            while not self.oc_session(name) and time.monotonic() < end:
                self.daemon.tick()
                time.sleep(POLL_S)
            sid = self.oc_session(name)
            if not sid:
                raise SmokeFail("the daemon never learnt the OpenCode session id")
            self.made_oc.append(sid)
            text = self.reply(lambda: self.oc_text(sid, self.a, after=PROMPT_PONG[:40]), ("PONG",))
            if "PONG" not in text:
                raise SmokeFail(f"reply without PONG: {text[-200:]!r}")

        def needs_you():
            self.send(name, PROMPT_TOUCH)
            try:
                self.wait(name, {"NEEDS_YOU"}, 240, "permission prompt")
            except SmokeFail:
                # the model may have written its tool call as text (a model server that does not parse this model's
                # tool calls): no command reached OpenCode, so nothing asked. Say that, not "no prompt".
                sid = self.oc_session(name)
                reply = self.oc_text(sid, self.a, after=PROMPT_TOUCH[:40]) if sid else ""
                v = self.view(name)
                over = v is not None and getattr(v.state, "value", v.state) in ("IDLE", "YOUR_TURN")
                if tool_call_as_text(reply) or (over and not reply.strip()):
                    raise SmokeFail(f"{TOOL_CALLS_AS_TEXT}: the model's tool call never reached OpenCode (reply "
                                    f"{reply.strip()[:160]!r}), so no command ran; the model server does not "
                                    "parse this model's tool calls")
                raise
            self.key(name, "Escape")
            self.wait(name, {"IDLE", "YOUR_TURN"}, 120, "after denying")

        def move():
            sid = self.oc_session(name)
            self.cockpit.move(name, str(self.b))
            self.wait(name, {"IDLE", "YOUR_TURN"}, 180, "move")
            time.sleep(3)
            self.recall(name, lambda: self.oc_text(sid, self.b, after=PROMPT_RECALL[:40]), "after the move")

        for n, fn in (("opencode: start", start), ("opencode: turn", turn), ("opencode: needs you", needs_you),
                      ("opencode: move", move)):
            if not self.check(n, fn):
                break

    # --- split mode / remotes (SPEC §39, §40) -------------------------------------------------------
    def split_flow(self) -> None:
        """Split mode (SPEC §39) and remotes (SPEC §40), exercised with what a machine always has: a shell
        session in a bare host tmux, a ui session (one per terminal) showing it through a nested client, and
        the launcher's `force watch` stream serving that host's snapshot. No Claude/OpenCode needed."""
        from . import remote
        from .cli import build_ui_session

        def wait(cond, timeout: float, what: str) -> bool:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if cond():
                    return True
                time.sleep(0.2)
            return False

        def check1():
            e = self.add_entry("smoke-shell", self.a, "shell")
            self.cockpit.create(e)
            if not wait(lambda: self.cockpit.agent_pane("smoke-shell") is not None, 20, "shell session"):
                raise SmokeFail("the shell session did not start")
            time.sleep(0.3)
            build_ui_session(self.cockpit, "smoke-shell", "smoke-ui", 120, 30)
            ui = self.cockpit.ui_tmux
            mains = [p for p in ui.panes() if p.session == "smoke-ui" and p.role == "agent"]
            if not mains:
                raise SmokeFail("no ui session 'smoke-ui' with an agent main pane")
            main = mains[0]
            if ui.run("show", "-wqv", "-t", main.pane_id, "@force_session") != "smoke-shell":
                raise SmokeFail("ui @force_session does not name smoke-shell")
            host_pane = self.cockpit.agent_pane("smoke-shell")
            if not wait(
                lambda: ui.run("display", "-p", "-t", main.pane_id, "#{pane_current_command}").strip()
                and any(c.session == "smoke-shell" for c in self.cockpit.tmux.clients()),
                10, "nested client attach"):   # noqa: E501
                cmd = ui.run("display", "-p", "-t", main.pane_id, "#{pane_current_command}")
                clients = [c.session for c in self.cockpit.tmux.clients()]
                raise SmokeFail(f"the nested client did not attach: main cmd {cmd!r}, "
                              f"host clients {clients!r}")
            ui.run("send-keys", "-t", main.pane_id, "-l", "echo smoke-ui-$((6*7))")
            ui.run("send-keys", "-t", main.pane_id, "Enter")
            if not wait(
                lambda: "smoke-ui-42" in self.cockpit.tmux.capture_tail(host_pane.pane_id, 20),
                20, "typed key reach the host"):
                cap = self.cockpit.tmux.capture_tail(host_pane.pane_id, 20)
                raise SmokeFail(f"echo did not reach the host shell; capture tail: {cap!r}")
            return ""

        def check2():
            proc = subprocess.Popen([self.cockpit.cockpit_bin, "watch"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, env=os.environ)
            lines = _Lines(proc.stdout)
            try:
                line = lines.get(20.0)
                if line is None:
                    raise SmokeFail("force watch: no hello within 20 s")
                try:
                    hello = json.loads(line).get("hello") or {}
                except ValueError:
                    raise SmokeFail(f"force watch: bad hello JSON: {line!r}")
                if hello.get("proto") != remote.PROTO:
                    raise SmokeFail(f"force watch: wrong proto {hello.get('proto')!r} "
                                  f"(want {remote.PROTO})")
                seen = False
                end = time.monotonic() + 20.0
                while time.monotonic() < end:
                    self.daemon.tick()          # a fresh snapshot is then served by watch
                    line = lines.get(min(1.0, end - time.monotonic()))
                    if line is None:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if "snapshot" not in msg:
                        continue
                    sessions = (msg.get("snapshot") or {}).get("sessions") or []
                    if any(s.get("name") == "smoke-shell" for s in sessions):
                        seen = True
                        break
                if not seen:
                    raise SmokeFail("force watch: no snapshot with smoke-shell within 20 s")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            return ""

        for n, fn in (("split: keys reach a session through the ui", check1),
                      ("split: force watch", check2)):
            if not self.check(n, fn):
                break

    # --- cleanup -------------------------------------------------------------------------------------
    def cleanup(self) -> None:
        from . import opencode
        from .claude import find_transcript
        if not hasattr(self, "cockpit"):                  # setup failed before anything ran
            shutil.rmtree(self.run_dir, ignore_errors=True)
            return
        # stop the tools first: Claude writes its transcript's metadata again while it exits
        for name in ("smoke-claude", "smoke-opencode", "smoke-shell"):
            try:
                self.cockpit.stop_agent(name, timeout=20)
            except Exception:
                pass
        try:
            self.cockpit.tmux.kill_server()
        except Exception:
            pass
        try:
            os.unlink(f"/tmp/tmux-{os.getuid()}/{self.paths.tmux_socket}")
        except OSError:
            pass
        try:
            self.cockpit.ui_tmux.kill_server()
        except Exception:
            pass
        try:
            os.unlink(f"/tmp/tmux-{os.getuid()}/{self.paths.ui_socket}")
        except OSError:
            pass
        time.sleep(1)
        for sid in self.made_claude:
            while (t := find_transcript(self.live.claude_dir, sid)) is not None:
                shutil.rmtree(t.parent / sid, ignore_errors=True)
                t.unlink(missing_ok=True)
        binary = opencode.installed()
        if binary:
            # every OpenCode session in the smoke work folders (they are the smoke test's alone), also the
            # ones of a run that failed before the daemon learnt their id
            env = dict(os.environ, OPENCODE_DISABLE_AUTOUPDATE="1")
            for cwd in (self.a, self.b):
                try:
                    out = subprocess.run([binary, "session", "list", "--format", "json"], cwd=cwd,
                                         capture_output=True, text=True, timeout=60, env=env)
                    ids = [x.get("id") for x in json.loads(out.stdout or "[]") if isinstance(x, dict)]
                except (OSError, subprocess.TimeoutExpired, ValueError):
                    ids = list(self.made_oc)
                for sid in ids:
                    if sid and sid.startswith("ses_"):
                        try:
                            subprocess.run([binary, "session", "delete", sid], cwd=cwd, capture_output=True,
                                           timeout=60, env=env)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
        shutil.rmtree(self.run_dir, ignore_errors=True)


def run(live_paths, live_cfg, log=sys.stdout) -> dict:
    """One full smoke run; returns (and saves) the result."""
    base = live_paths.state / "smoke"
    base.mkdir(parents=True, exist_ok=True)
    started = time.time()
    v = versions(live_paths)
    r = Run(live_paths, live_cfg, log)
    r.say(f"force smoke {time.strftime('%Y-%m-%d %H:%M:%S')}: claude {v.get('claude')}, opencode "
          f"{v.get('opencode')}, force {v.get('force')}")
    saved = {k: os.environ.get(k) for k in ("FORCE_HOME", "FORCE_STATE", "FORCE_CONFIG", "FORCE_TMUX_SOCKET",
                                             "FORCE_UI_SOCKET", "FORCE_TMUX_CONF", "FORCE_CLAUDE_DIR",
                                             "FORCE_CLAUDE_BIN", "FORCE_NO_REMOTE_CONTROL", "ANTHROPIC_MODEL",
                                             "INVOCATION_ID", "FORCE_SERVICE")}
    try:
        r.setup()
        r.claude_flow()
        r.opencode_flow()
        r.split_flow()
    except Exception as e:
        r.checks.append({"name": "setup", "ok": False, "s": 0, "detail": f"{type(e).__name__}: {e}"})
    finally:
        try:
            r.cleanup()
        except Exception as e:
            r.say(f"  cleanup: {e}")
        for k, val in saved.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
    result = {"ok": bool(r.checks) and all(c["ok"] for c in r.checks), "at": started,
              "duration_s": round(time.time() - started), "versions": v, "checks": r.checks,
              "reason": reason(r.checks)}
    tmp = base / "last.json.tmp"
    tmp.write_text(json.dumps(result, indent=1))
    os.replace(tmp, base / "last.json")
    with open(base / "history.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")
    r.say(f"force smoke: {'all checks passed' if result['ok'] else 'FAILED'} in {result['duration_s']} s")
    return result


def command(live_paths, live_cfg, if_due: bool, last: bool) -> int:
    base = live_paths.state / "smoke"
    if last:
        res = read_last(base)
        if res is None:
            print("force: no smoke run yet (force smoke)")
            return 1
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(res.get("at", 0)))
        print(f"last smoke run {when}: {'passed' if res.get('ok') else 'FAILED'} "
              f"({res.get('duration_s')} s; {', '.join(f'{k} {v}' for k, v in (res.get('versions') or {}).items())})")
        if not res.get("ok"):
            why = res.get("reason") or reason(res.get("checks"))
            if why:
                print(f"  reason: {why}")
        for c in res.get("checks") or []:
            print(f"  {'ok    ' if c['ok'] else 'FAILED'} {c['name']}{' - ' + c['detail'] if c.get('detail') else ''}")
        return 0 if res.get("ok") else 1
    base.mkdir(parents=True, exist_ok=True)
    with open(base / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("force: a smoke run is already going", file=sys.stderr)
            return 1
        if if_due:
            why = due(read_last(base), versions(live_paths), time.time(),
                     logged_in=lambda: claude_logged_in(live_paths.claude_bin))
            if why is None:
                return 0
            print(f"force smoke: due ({why})")
        (base / "log").mkdir(exist_ok=True)
        logf = base / "log" / f"{time.strftime('%Y%m%d-%H%M%S')}.log"
        with open(logf, "w") as log:
            class Tee:
                def write(self, s):
                    log.write(s)
                    sys.stdout.write(s)

                def flush(self):
                    log.flush()
                    sys.stdout.flush()
            res = run(live_paths, live_cfg, Tee())
        for old in sorted((base / "log").glob("*.log"))[:-30]:
            old.unlink(missing_ok=True)
        return 0 if res["ok"] else 1
