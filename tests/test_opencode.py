"""Tests for the OpenCode session kind (SPEC §36): opencode.py, registry.kind, cli, daemon, lifecycle."""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from force import cli, opencode
from force.claude import AgentStatus
from force.opencode import Meta, argv, free_port, read_status
from force.registry import Entry, is_claude, is_opencode, kind, load, locked
from force.state import State, read_snapshot
from test_daemon import FakeClock, wait_for


def entry(paths, name):
    return load(paths.registry).get(name)


# --- fakes fixture (copied from tests/test_cli.py so it is visible here) ----
class FakeTmux:
    def __init__(self):
        self.calls = []
        self.server = True
        self.sessions_out = []
        self.clients_out = []
        self.run_out = ""
        self.agent_panes = {}

    def invalidate(self):
        pass

    def server_running(self):
        return self.server

    def has_session(self, name):
        return name in self.sessions_out

    def clients(self):
        return self.clients_out

    def panes(self, strict=False):
        return []

    def get_option(self, key, default=None):
        self.calls.append(("get_option", key))
        return "off"

    def new_session(self, name, width, height, cwd, command, env=None):
        self.calls.append(("new_session", name))
        return "p0"

    def split_sidebar(self, target_pane, cwd, command, width):
        return "p1"

    def set_pane_option(self, pane, key, value):
        self.calls.append(("set_pane_option", pane, key, value))

    def run(self, *args, check=True):
        self.calls.append(("run", args))
        return self.run_out

    def switch_client(self, target, client=None):
        self.calls.append(("switch_client", target, client))

    def select_pane(self, pane):
        self.calls.append(("select_pane", pane))


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    env = {
        "HOME": str(tmp_path),
        "FORCE_HOME": str(tmp_path / "home"),
        "FORCE_STATE": str(tmp_path / "state"),
        "FORCE_CLAUDE_DIR": str(tmp_path / "claude"),
        "FORCE_CLAUDE_BIN": str(tmp_path / "bin" / "claude"),
        "FORCE_CONFIG": str(tmp_path / "settings.json"),
        "FORCE_TMUX_SOCKET": "forcetestsock",
        "FORCE_UI_SOCKET": "forcetestsock-ui",
        "FORCE_RUN_STAGE": "2",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "settings.json").write_text(json.dumps({"layout": "classic", "sharing": True}))
    fake = FakeTmux()
    cockpit = cli.make_cockpit()
    cockpit.tmux = fake
    monkeypatch.setattr(cli, "make_cockpit", lambda: cockpit)
    return cockpit, fake, tmp_path


# --- tiny local fake OpenCode HTTP server -------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.routes.get(self.path, b"{}")
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):     # silence the stderr request log
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    routes = {}


@pytest.fixture
def opencode_server():
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.routes = {}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


_SES_A = {"model": {"id": "deepseek-v4-flash"}, "time": {"updated": 12345}}


def test_read_status_busy(opencode_server):
    opencode_server.routes = {
        "/session/status": {"ses_A": {"type": "busy"}},
        "/session/ses_A": _SES_A,
    }
    st, meta = read_status(opencode_server.server_port, 77, None)
    assert st.status == "busy"
    assert st.session_id == "ses_A"
    assert st.pid == 77
    assert meta.model == "deepseek-v4-flash"
    assert st.status_updated_ms == 12345


def test_read_status_idle_known_session(opencode_server):
    opencode_server.routes = {"/session/status": {}}
    st, meta = read_status(opencode_server.server_port, 1, "ses_A")
    assert st.status == "idle"
    assert st.session_id == "ses_A"


def test_read_status_idle_no_session(opencode_server):
    opencode_server.routes = {"/session/status": {}}
    st, _meta = read_status(opencode_server.server_port, 1, None)
    assert st.status == "idle"
    assert st.session_id == ""


def test_read_status_waiting_permission(opencode_server):
    opencode_server.routes = {
        "/session/status": {"ses_A": {"type": "busy"}},
        "/session/ses_A": _SES_A,
        "/api/permission/request": {"data": [{"sessionID": "ses_A"}]},
        "/api/question/request": {"data": []},
    }
    st, meta = read_status(opencode_server.server_port, 1, None)
    assert st.status == "waiting"
    assert meta.question is False


def test_read_status_waiting_question(opencode_server):
    opencode_server.routes = {
        "/session/status": {"ses_A": {"type": "busy"}},
        "/session/ses_A": _SES_A,
        "/api/permission/request": {"data": []},
        "/api/question/request": {"data": [{"sessionID": "ses_A"}]},
    }
    st, meta = read_status(opencode_server.server_port, 1, None)
    assert st.status == "waiting"
    assert meta.question is True


def test_read_status_pending_other_session_not_waiting(opencode_server):
    """A pending item for a DIFFERENT OpenCode session does not make this one waiting."""
    opencode_server.routes = {
        "/session/status": {"ses_A": {"type": "busy"}},
        "/session/ses_A": _SES_A,
        "/api/permission/request": {"data": [{"sessionID": "ses_B"}]},
        "/api/question/request": {"data": [{"sessionID": "ses_B"}]},
    }
    st, meta = read_status(opencode_server.server_port, 1, "ses_A")
    assert st.status == "busy"
    assert meta.question is False


def test_read_status_retry_counts_as_busy(opencode_server):
    opencode_server.routes = {
        "/session/status": {"ses_A": {"type": "retry"}},
        "/session/ses_A": _SES_A,
    }
    st, _meta = read_status(opencode_server.server_port, 1, None)
    assert st.status == "busy"
    assert st.session_id == "ses_A"


def test_read_status_busy_other_than_registry(opencode_server):
    opencode_server.routes = {
        "/session/status": {"ses_B": {"type": "busy"}},
        "/session/ses_B": _SES_A,
    }
    st, _meta = read_status(opencode_server.server_port, 1, "ses_A")
    assert st.session_id == "ses_B"
    assert st.status == "busy"


def test_read_status_not_behind_free_port_and_bad_ports():
    # nothing listening on a freshly free port -> (None, None)
    port = free_port()
    assert read_status(port, 1, None) == (None, None)
    # None / "" / "abc" -> (None, None)
    assert read_status(None, 1, None) == (None, None)
    assert read_status("", 1, None) == (None, None)
    assert read_status("abc", 1, None) == (None, None)


def test_read_status_non_json_body(opencode_server):
    """A non-JSON body must not raise; /session/status non-JSON -> (None, None)."""
    opencode_server.routes = {"/session/status": "not json at all"}
    assert read_status(opencode_server.server_port, 1, None) == (None, None)


def test_read_status_permission_data_not_a_list(opencode_server):
    """/api/permission/request returning {"data": "notalist"} -> not waiting."""
    opencode_server.routes = {
        "/session/status": {"ses_A": {"type": "busy"}},
        "/session/ses_A": _SES_A,
        "/api/permission/request": {"data": "notalist"},
        "/api/question/request": {"data": []},
    }
    st, meta = read_status(opencode_server.server_port, 1, None)
    assert st.status == "busy"
    assert meta.question is False


# --- free_port / argv --------------------------------------------------------
def test_free_port_and_argv():
    p = free_port()
    assert isinstance(p, int)
    assert 1024 <= p <= 65535
    assert p != 8000
    assert argv(1234, None, "/tmp") == \
        ["opencode", "--port", "1234", "--hostname", "127.0.0.1", "/tmp"]
    assert argv(1234, "S", "/tmp") == \
        ["opencode", "--port", "1234", "--hostname", "127.0.0.1", "--session", "S", "/tmp"]


# --- registry kind helpers ----------------------------------------------------
def _entry(extra=None, kind_=None):
    d = {"kind": kind_} if kind_ is not None else {}
    if extra:
        d.update(extra)
    return Entry(name="x", session_id=str(uuid.uuid4()), cwd="/tmp",
                created_at="t", order=0, extra=d)


def test_registry_kind_and_predicates():
    # kind absent -> claude
    e = _entry()
    assert kind(e) == "claude"
    assert is_claude(e) and not is_opencode(e)
    # explicit claude
    e = _entry(kind_="claude")
    assert kind(e) == "claude"
    assert is_claude(e) and not is_opencode(e)
    # opencode
    e = _entry(kind_="opencode")
    assert kind(e) == "opencode"
    assert is_opencode(e) and not is_claude(e)
    # shell
    e = _entry(kind_="shell")
    assert kind(e) == "shell"
    assert not is_opencode(e) and not is_claude(e)
    # unknown counts as claude for kind()
    e = _entry(kind_="weird")
    assert kind(e) == "claude"
    assert not is_opencode(e)


# --- available_kinds ---------------------------------------------------------
def test_available_kinds(fakes, monkeypatch):
    cockpit, _fake, _tmp = fakes
    # claude unavailable, opencode unavailable -> only shell
    monkeypatch.setattr(cli.os, "access", lambda path, mode: False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli.opencode, "installed", lambda: None)
    assert cli.available_kinds(cockpit) == ["shell"]
    # opencode installed -> opencode before shell
    monkeypatch.setattr(cli.opencode, "installed", lambda: "/usr/bin/opencode")
    assert cli.available_kinds(cockpit) == ["opencode", "shell"]
    # claude available too -> claude, opencode, shell
    monkeypatch.setattr(cli.os, "access", lambda path, mode: True)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/claude")
    assert cli.available_kinds(cockpit) == ["claude", "opencode", "shell"]


# --- create_session(kind="opencode") -----------------------------------------
def test_create_session_opencode_installed(fakes, monkeypatch):
    cockpit, _fake, tmp = fakes
    monkeypatch.setattr(cli.opencode, "installed", lambda: "/usr/bin/opencode")
    monkeypatch.setenv("TMUX", "/tmp/x/forcetestsock,1234,0")
    try:
        rc = cli.create_session(cockpit, "oc", str(tmp), kind="opencode")
    finally:
        monkeypatch.undo()
    assert rc == 0
    e = load(cockpit.paths.registry).get("oc")
    assert e is not None
    assert e.extra.get("kind") == "opencode"


def test_create_session_opencode_not_installed(fakes, monkeypatch, capsys):
    cockpit, _fake, tmp = fakes
    monkeypatch.setattr(cli.opencode, "installed", lambda: None)
    rc = cli.create_session(cockpit, "oc", str(tmp), kind="opencode")
    assert rc == 1
    assert "not installed" in capsys.readouterr().err
    assert load(cockpit.paths.registry).get("oc") is None


def test_create_session_unknown_kind(fakes, capsys):
    cockpit, _fake, tmp = fakes
    rc = cli.create_session(cockpit, "oc", str(tmp), kind="bogus")
    assert rc == 1
    assert "unknown session kind" in capsys.readouterr().err
    assert load(cockpit.paths.registry).get("oc") is None


# --- kinds --json ------------------------------------------------------------
def test_kinds_json(fakes, monkeypatch, capsys):
    _cockpit, _fake, _tmp = fakes
    monkeypatch.setattr(cli.opencode, "installed", lambda: None)
    monkeypatch.setattr(cli.os, "access", lambda path, mode: False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    rc = cli.main(["kinds", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert data[-1] == "shell"
    assert "opencode" not in data


# --- upgrade reason ----------------------------------------------------------
def test_upgrade_reason_opencode(fakes):
    cockpit, _fake, tmp = fakes
    with locked(cockpit.paths.home) as reg:
        e = reg.add("oc", str(uuid.uuid4()), str(tmp))
        e.extra["kind"] = "opencode"
    assert cli._upgrade_reason(cockpit, "oc") == "not running"      # no pane


def test_upgrade_reason_opencode_versions(fakes, monkeypatch):
    """An OpenCode session is restarted onto the installed binary when that is newer than the running
    TUI (the admin updates OpenCode); not when equal, busy or waiting."""
    from types import SimpleNamespace
    cockpit, _fake, tmp = fakes
    with locked(cockpit.paths.home) as reg:
        e = reg.add("oc", str(uuid.uuid4()), str(tmp))
        e.extra["kind"] = "opencode"
        e.extra["oc_port"] = 4242
    monkeypatch.setattr(cockpit, "agent_pane", lambda n: SimpleNamespace(pid=77, pane_id="%1", dead=False))
    state = {"status": "idle", "running": "1.18.31"}

    def fake_read(port, pid, session, since_ms=None):
        st = AgentStatus(pid=pid, session_id="", name=None, name_source=None, status=state["status"],
                          status_updated_ms=0, waiting_for=None, cwd=None)
        return st, Meta(model=None, question=False, version=state["running"])
    monkeypatch.setattr(cli.opencode, "read_status", fake_read)
    monkeypatch.setattr(cli.opencode, "installed_version", lambda max_age=60.0: "1.18.32")
    assert cli._upgrade_reason(cockpit, "oc") is None
    state["status"] = "busy"
    assert cli._upgrade_reason(cockpit, "oc") == "busy"
    state.update(status="idle", running="1.18.32")
    assert cli._upgrade_reason(cockpit, "oc") == "not outdated"


def test_screen_agent_and_activity_phrases():
    assert opencode.screen_agent("history\n  ┃  Build ·DeepSeek-V4-Flash on devbox\n") == "build"
    assert opencode.screen_agent("┃  Build · x\n┃  Plan ·DeepSeek") == "plan", "the last (prompt box) wins"
    assert opencode.screen_agent("no footer here") is None
    tool = lambda name, **inp: {"type": "tool", "tool": name, "state": {"status": "running", "input": inp}}
    assert opencode.activity_from_parts([{"type": "reasoning"}, tool("bash", command="ls")]) == "running a command"
    assert opencode.activity_from_parts([tool("edit", filePath="/a/b/app.py")]) == "editing app.py"
    assert opencode.activity_from_parts([tool("read")]) == "reading files"
    assert opencode.activity_from_parts([tool("grep")]) == "searching files"
    assert opencode.activity_from_parts([{"type": "reasoning"}]) == "thinking"
    assert opencode.activity_from_parts([{"type": "text", "text": "hi"}]) == "writing a reply"
    assert opencode.activity_from_parts([{"type": "step-start"}]) is None
    assert opencode.activity_from_parts("junk") is None


# --- daemon: snapshot / follow ----------------------------------------------
def test_daemon_opencode_snapshot_and_follow(env, monkeypatch):
    from force.daemon import Daemon, opencode_mod
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("oc", str(uuid.uuid4()), "/tmp")
        e.extra["kind"] = "opencode"
        e.extra["oc_port"] = 12345
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("oc") is not None and cockpit.agent_pane("oc").pid)
    now_ms = int(time.time() * 1000)

    def fake_read(port, pane_pid, session, since_ms=None):
        return (AgentStatus(pid=pane_pid, session_id="ses_X", name=None, name_source=None,
                             status="busy", status_updated_ms=now_ms, waiting_for=None, cwd=None),
                Meta(model="deepseek-v4-flash", question=False, variant="high", agent="build"))

    monkeypatch.setattr(opencode_mod, "read_status", fake_read)
    d = Daemon(cockpit, FakeClock())
    d.tick()

    assert load(env.paths.registry).get("oc").extra.get("oc_session") == "ses_X"
    sv = [s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "oc"][0]
    assert sv.state is State.WORKING
    # the same slots as a Claude card: model label, effort = variant, mode badge = agent, kind mark
    assert sv.model == "deepseek v4 flash"
    assert sv.effort == "high"
    assert sv.mode == "oc-build"
    assert sv.kind == "opencode"
    assert sv.activity is None

    # now a pending question: waiting state + activity
    def fake_read2(port, pane_pid, session, since_ms=None):
        return (AgentStatus(pid=pane_pid, session_id="ses_X", name=None, name_source=None,
                             status="waiting", status_updated_ms=now_ms, waiting_for=None, cwd=None),
                Meta(model="deepseek-v4-flash", question=True, variant="default", agent="plan"))

    monkeypatch.setattr(opencode_mod, "read_status", fake_read2)
    d.clock.advance(1)
    d.tick()
    sv = [s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "oc"][0]
    assert sv.state is State.NEEDS_YOU
    assert sv.activity == "asks you a question"
    assert sv.effort is None, "the default variant is not shown"
    assert sv.mode == "oc-plan"


# --- lifecycle rename immediate ----------------------------------------------
def test_rename_live_opencode_immediate(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("oc", str(uuid.uuid4()), "/tmp")
        e.extra["kind"] = "opencode"
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("oc") is not None and not cockpit.agent_pane("oc").dead)
    cockpit.rename("oc", "newname")
    assert entry(env.paths, "oc") is None
    assert entry(env.paths, "newname") is not None
    assert entry(env.paths, "newname").pending_name is None
    assert cockpit.tmux.has_session("newname")


def test_read_status_model_falls_back_to_config(opencode_server):
    """A fresh TUI has no session yet: the configured default model fills the model slot."""
    opencode_server.routes = {"/session/status": {}, "/config": {"model": "devbox/deepseek-v4-flash"}}
    st, meta = read_status(opencode_server.server_port, 5, None)
    assert st.status == "idle" and meta.model == "deepseek-v4-flash"


def test_read_status_waiting_from_the_tui_endpoints(opencode_server):
    """OpenCode 1.19's TUI lists pending prompts on /permission and /question (plain lists); the newer
    /api/... endpoints stay empty (seen live 2026-10-20)."""
    opencode_server.routes = {
        "/session/status": {"ses_A": {"type": "busy"}},
        "/permission": [{"id": "per_1", "sessionID": "ses_A", "permission": "bash"}],
        "/question": [],
        "/api/permission/request": {"data": []},
        "/api/question/request": {"data": []},
    }
    st, meta = read_status(opencode_server.server_port, 1, "ses_A")
    assert st.status == "waiting" and meta.question is False
    opencode_server.routes["/permission"] = []
    opencode_server.routes["/question"] = [{"id": "que_1", "sessionID": "ses_A"}]
    st, meta = read_status(opencode_server.server_port, 1, "ses_A")
    assert st.status == "waiting" and meta.question is True
    opencode_server.routes["/question"] = [{"id": "que_2", "sessionID": "ses_OTHER"}]
    st, meta = read_status(opencode_server.server_port, 1, "ses_A")
    assert st.status == "busy", "another session's prompt"


def test_daemon_keeps_an_opencode_state_when_its_api_misses_an_answer(env, monkeypatch):
    """The TUI setting up its first session misses the 0.4 s status answer for a moment: the card keeps its last
    state (not down) for a while; after a longer silence the usual rules apply again."""
    from force.daemon import Daemon, opencode_mod, _OC_HOLD
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("oc", str(uuid.uuid4()), "/tmp")
        e.extra["kind"] = "opencode"
        e.extra["oc_port"] = 12345
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("oc") is not None and cockpit.agent_pane("oc").pid)
    now_ms = int(time.time() * 1000)
    answer = {"on": True}

    def fake_read(port, pane_pid, session, since_ms=None):
        if not answer["on"]:
            return None, None
        return (AgentStatus(pid=pane_pid, session_id="ses_X", name=None, name_source=None, status="busy",
                            status_updated_ms=now_ms, waiting_for=None, cwd=None),
                Meta(model="m", question=False))

    monkeypatch.setattr(opencode_mod, "read_status", fake_read)
    clock = FakeClock()
    d = Daemon(cockpit, clock)
    state = lambda: next(s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "oc").state
    d.tick()
    assert state() is State.WORKING
    answer["on"] = False
    clock.advance(1)
    d.tick()
    assert state() is State.WORKING                   # one missed answer: still working, not down
    clock.advance(_OC_HOLD + 1)
    d.tick()
    assert state() is not State.WORKING               # silent for longer: no longer held (the usual rules)


def test_reply_key():
    """SPEC §5 unread: what the agent wrote in a message, by message id and text length."""
    text = lambda t: {"type": "text", "text": t}
    msg = {"info": {"id": "m1", "role": "assistant"}, "parts": [text("hello"), {"type": "tool", "tool": "bash"}]}
    assert opencode.reply_key(msg) == "m1:5"
    msg["parts"].append(text(" there"))
    assert opencode.reply_key(msg) == "m1:11"               # streaming text grows the key
    assert opencode.reply_key({"info": {"id": "u", "role": "user"}, "parts": [text("do it")]}) is None
    assert opencode.reply_key({"info": {"id": "m2", "role": "assistant"},
                               "parts": [{"type": "tool", "tool": "bash"}, text("  ")]}) is None
    assert opencode.reply_key({"id": "m3", "role": "assistant", "parts": [text("x")]}) == "m3:1"
    assert opencode.reply_key(None) is None
