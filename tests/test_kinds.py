"""Tests for SPEC §49: agent kinds, limits, shell status, notifications and automatic names."""
import dataclasses
import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest

from force import cli, kinds
from force.claude import AgentStatus
from force.daemon import Daemon, notify_env
from force.registry import Entry, load, locked, valid_name
from force.state import Config, PaneInfo, Seen, Snapshot, State, compute_state_why


class FakeClock:
    def __init__(self, start=None):
        self.t = start if start is not None else time.time()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _entry(name, kind_="claude", cwd="/tmp"):
    e = Entry(name=name, session_id=str(uuid.uuid4()), cwd=cwd,
              created_at="2026-01-01T00:00:00+00:00", order=0)
    if kind_ != "claude":
        e.extra["kind"] = kind_
    return e


def _pane(command, started_ms=1000):
    return SimpleNamespace(pid=os.getpid(), dead=False, started_ms=started_ms, command=command)


def _shell_daemon(clock):
    return SimpleNamespace(clock=clock, _shell_prog={})


def test_name_from_prompt():
    assert kinds.name_from_prompt("please can you fix the login bug in the api") == "fix-the-login-bug-in"
    assert kinds.name_from_prompt("Hey, refactor the DAEMON!") == "refactor-the-daemon"
    assert kinds.name_from_prompt("a" * 40) == "a" * 20
    for bad in ("", None, "!!!", "please can you"):
        assert kinds.name_from_prompt(bad) is None
    # every produced name must be a valid registry name
    for text in ("please can you fix the login bug in the api", "Hey, refactor the DAEMON!", "a" * 40):
        assert valid_name(kinds.name_from_prompt(text))


def test_free_name():
    assert kinds.free_name("free", lambda n: False) == "free"
    assert kinds.free_name("free", {"free"}) == "free-2"
    assert kinds.free_name("base", lambda n: n in {"base"}) == "base-2"
    taken = {"base"} | {f"base-{i}" for i in range(2, 10)}
    assert kinds.free_name("base", taken) is None
    assert kinds.free_name("base", taken, upto=99) == "base-10"


def test_get_of_all():
    assert isinstance(kinds.get(None), kinds.Claude)
    assert isinstance(kinds.get("bogus"), kinds.Claude)
    assert isinstance(kinds.get("claude"), kinds.Claude)
    assert isinstance(kinds.get("opencode"), kinds.OpenCode)
    assert isinstance(kinds.get("shell"), kinds.Shell)
    assert set(kinds.ALL) == {"claude", "opencode", "shell"}
    assert kinds.get("shell").direct is True
    assert kinds.get("claude").name_in_agent is True
    assert isinstance(kinds.of(_entry("s", "shell")), kinds.Shell)
    assert isinstance(kinds.of(_entry("oc", "opencode")), kinds.OpenCode)
    assert isinstance(kinds.of(_entry("c", "claude")), kinds.Claude)
    assert isinstance(kinds.of(_entry("c")), kinds.Claude)   # kind absent counts as claude


def test_shell_reading():
    sh = kinds.get("shell")
    e = _entry("s", "shell")

    # "bash" -> at the prompt
    clock = FakeClock()
    r = sh.read(_shell_daemon(clock), e, _pane("bash"))
    assert r.status.status == "idle"
    assert r.meta.program is None

    # a command equal to the pane's own comm is treated as the prompt
    from force import procinfo
    comm = procinfo.comm(os.getpid())
    r = sh.read(_shell_daemon(clock), e, _pane(comm))
    assert r.status.status == "idle"
    assert r.meta.program is None

    # "make": not busy until it has run 2 s, then busy with activity
    clock = FakeClock()
    d = _shell_daemon(clock)
    r = sh.read(d, e, _pane("make"))
    assert r.status.status == "idle"
    assert r.meta.program == "make"
    clock.advance(2.5)
    r = sh.read(d, e, _pane("make"))
    assert r.status.status == "busy"
    assert sh.card(d, e, _pane("make"), r).activity == "running make"

    # "vim" is interactive: idle with the program as the card title, no activity
    clock = FakeClock()
    d = _shell_daemon(clock)
    r = sh.read(d, e, _pane("vim"))
    clock.advance(10)
    r = sh.read(d, e, _pane("vim"))
    assert r.status.status == "idle"
    card = sh.card(d, e, _pane("vim"), r)
    assert card.title == "vim"
    assert card.activity is None

    # busy "make" back to "bash" -> idle and status_updated_ms changes
    clock = FakeClock()
    d = _shell_daemon(clock)
    sh.read(d, e, _pane("make"))
    clock.advance(2.5)
    busy = sh.read(d, e, _pane("make"))
    assert busy.status.status == "busy"
    idle = sh.read(d, e, _pane("bash"))
    assert idle.status.status == "idle"
    assert idle.status.status_updated_ms != busy.status.status_updated_ms


def test_state_shell_branch():
    e = _entry("s", "shell")
    alive = PaneInfo(session_exists=True, pane_alive=True, pane_pid=123, pane_started_ms=1000,
                    dead_status=None, dead_signal=None)
    dead = PaneInfo(session_exists=True, pane_alive=False, pane_pid=123, pane_started_ms=1000,
                    dead_status=1, dead_signal=None)
    busy = AgentStatus(pid=123, session_id="", name=None, name_source=None, status="busy",
                      status_updated_ms=0, waiting_for=None, cwd=None)
    idle = AgentStatus(pid=123, session_id="", name=None, name_source=None, status="idle",
                      status_updated_ms=0, waiting_for=None, cwd=None)
    seen_empty = Seen()
    seen_finished = Seen(last_seen_ms=100, turn_done_ms=200)
    cfg = Config()

    st, why = compute_state_why(e, alive, busy, seen_empty, 0, cfg)
    assert st is State.WORKING
    st, _ = compute_state_why(e, alive, idle, seen_finished, 0, cfg)
    assert st is State.YOUR_TURN
    st, _ = compute_state_why(e, alive, idle, seen_empty, 0, cfg)
    assert st is State.SHELL
    st, _ = compute_state_why(e, alive, None, seen_empty, 0, cfg)
    assert st is State.SHELL
    st, _ = compute_state_why(e, dead, idle, seen_empty, 0, cfg)
    assert st is State.DOWN


def test_notify_env():
    view = SimpleNamespace(name="sess", kind="claude", state=State.NEEDS_YOU, viewed=False,
                        activity="needs permission")
    env = notify_env(view, "needs-you")
    assert env["FORCE_EVENT"] == "needs-you"
    assert env["FORCE_SESSION"] == "sess"
    assert env["FORCE_KIND"] == "claude"
    assert env["FORCE_STATE"] == "needs-you"
    assert env["FORCE_MESSAGE"] == "sess: needs permission"
    assert env["FORCE_VIEWED"] == "0"
    assert notify_env(view, "finished")["FORCE_MESSAGE"] == "sess: finished"
    assert notify_env(view, "down")["FORCE_MESSAGE"] == "sess: stopped"


def test_step_notify(env, monkeypatch):
    cockpit = env.make_cockpit()
    clock = FakeClock()
    d = Daemon(cockpit, clock)
    fired = []
    monkeypatch.setattr(d, "_run_notify", lambda cmd, env: fired.append((cmd, env)))
    d.cfg = dataclasses.replace(env.cfg, notify_command="echo hi")

    def view(name, state, *, viewed=False, kind="claude", note=None):
        return SimpleNamespace(name=name, archived=False, state=state, viewed=viewed,
                            note=note, kind=kind, activity=None)

    def step(*views):
        d._views = list(views)
        d._step_notify()

    # first sight never notifies; IDLE->NEEDS_YOU notifies once; same state again doesn't
    step(view("a", State.IDLE))
    step(view("a", State.NEEDS_YOU))
    step(view("a", State.NEEDS_YOU))
    a_needs = [e for _c, e in fired if e["FORCE_SESSION"] == "a" and e["FORCE_EVENT"] == "needs-you"]
    assert len(a_needs) == 1
    assert a_needs[0]["FORCE_MESSAGE"] == "a: needs permission"

    # WORKING->YOUR_TURN -> "finished"
    step(view("a", State.WORKING))
    step(view("a", State.YOUR_TURN))
    a_fin = [e for _c, e in fired if e["FORCE_SESSION"] == "a" and e["FORCE_EVENT"] == "finished"]
    assert len(a_fin) == 1
    assert a_fin[0]["FORCE_MESSAGE"] == "a: finished"

    # IDLE->YOUR_TURN does NOT notify
    step(view("b", State.IDLE))
    step(view("b", State.YOUR_TURN))
    assert all(e["FORCE_SESSION"] != "b" for _c, e in fired)

    # viewed session doesn't notify unless notify_when_viewed
    step(view("c", State.IDLE, viewed=True))
    step(view("c", State.NEEDS_YOU, viewed=True))
    assert all(e["FORCE_SESSION"] != "c" for _c, e in fired)
    d.cfg = dataclasses.replace(d.cfg, notify_when_viewed=True)
    step(view("d", State.IDLE, viewed=True))
    step(view("d", State.NEEDS_YOU, viewed=True))
    assert any(e["FORCE_SESSION"] == "d" and e["FORCE_EVENT"] == "needs-you" for _c, e in fired)

    # a second needs-you within 30 s is suppressed; fires again after 31 s
    step(view("e", State.IDLE))
    step(view("e", State.NEEDS_YOU))
    n1 = len([e for _c, e in fired if e["FORCE_SESSION"] == "e" and e["FORCE_EVENT"] == "needs-you"])
    clock.advance(10)
    step(view("e", State.IDLE))
    step(view("e", State.NEEDS_YOU))
    n2 = len([e for _c, e in fired if e["FORCE_SESSION"] == "e" and e["FORCE_EVENT"] == "needs-you"])
    assert n2 == n1
    clock.advance(31)
    step(view("e", State.IDLE))
    step(view("e", State.NEEDS_YOU))
    n3 = len([e for _c, e in fired if e["FORCE_SESSION"] == "e" and e["FORCE_EVENT"] == "needs-you"])
    assert n3 == n1 + 1

    # DOWN fires only after staying DOWN >= 10 s of clock time
    step(view("f", State.WORKING))
    clock.advance(0.1)
    step(view("f", State.DOWN, note=None))
    assert all(e["FORCE_SESSION"] != "f" for _c, e in fired)
    clock.advance(11)
    step(view("f", State.DOWN, note=None))
    assert any(e["FORCE_SESSION"] == "f" and e["FORCE_EVENT"] == "down" for _c, e in fired)

    # a note set at the start of the down period suppresses the down event
    step(view("g", State.WORKING))
    step(view("g", State.DOWN, note="restarting"))
    clock.advance(20)
    step(view("g", State.DOWN, note="restarting"))
    assert all(e["FORCE_SESSION"] != "g" for _c, e in fired)

    # empty notify_command -> never calls _run_notify
    n_before = len(fired)
    d.cfg = dataclasses.replace(d.cfg, notify_command="")
    step(view("h", State.IDLE))
    step(view("h", State.NEEDS_YOU))
    assert len(fired) == n_before


def test_limits(env):
    usage = {"five_hour": {"used_percentage": 30, "resets_at": 4000000000},
             "seven_day": {"used_percentage": 10, "resets_at": 4000000000}}
    (env.paths.state / "usage.json").write_text(json.dumps(usage))
    cockpit = env.make_cockpit()
    d = Daemon(cockpit, FakeClock())

    shell = _entry("s", "shell")
    oc = _entry("oc", "opencode")
    assert d._limits([shell, oc]) == []

    cl = _entry("c", "claude")
    out = d._limits([shell, oc, cl])
    assert len(out) == 1
    assert out[0]["kind"] == "claude"
    assert out[0]["label"] == "Claude"
    assert out[0]["five_hour"]["used_percentage"] == 30
    assert out[0]["seven_day"]["used_percentage"] == 10

    snap = Snapshot(generated_ms=1, latest_version=None, sessions=[], limits=out)
    data = snap.to_json()
    assert "limits" in data and data["limits"] == out
    back = Snapshot.from_json(data)
    assert back.limits == out
    assert back.limits[0]["kind"] == "claude"


def test_create_session_names(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    monkeypatch.setattr(cockpit, "create", lambda entry: None)
    monkeypatch.setattr(kinds.Claude, "available", lambda self, paths, cfg: True)

    assert cli.create_session(cockpit, None, cwd=str(env.tmp), no_switch=True) == 0
    assert cli.create_session(cockpit, None, cwd=str(env.tmp), no_switch=True) == 0
    reg = load(env.paths.registry)
    assert reg.get("new").extra.get("auto_name") is True
    assert reg.get("new-2").extra.get("auto_name") is True
    out = capsys.readouterr().out
    assert "new" in out and "new-2" in out

    proj = env.tmp / "proj-x"
    proj.mkdir()
    assert cli.create_session(cockpit, None, cwd=str(proj), no_switch=True, kind="shell") == 0
    reg = load(env.paths.registry)
    se = reg.get("proj-x")
    assert se.extra.get("kind") == "shell"
    assert "auto_name" not in se.extra
    assert "proj-x" in capsys.readouterr().out

    assert cli.create_session(cockpit, "myname", cwd=str(env.tmp), no_switch=True) == 0
    reg = load(env.paths.registry)
    me = reg.get("myname")
    assert "auto_name" not in me.extra
    assert "myname" not in capsys.readouterr().out


def test_step_auto_name(env, monkeypatch):
    cockpit = env.make_cockpit()
    clock = FakeClock()
    d = Daemon(cockpit, clock)

    def add_auto(name):
        with locked(env.paths.home) as reg:
            e = reg.add(name, str(uuid.uuid4()), "/tmp")
            e.extra["auto_name"] = True

    # a usable first prompt -> named, auto_name dropped, title sync queued
    add_auto("new")
    d._registry = list(load(env.paths.registry).entries)
    monkeypatch.setattr(kinds.Claude, "first_prompt",
                      lambda self, paths, entry, reading: "please fix the flaky upload test")
    d._step_auto_name()
    e = load(env.paths.registry).get("fix-the-flaky-upload")
    assert e is not None
    assert "auto_name" not in e.extra
    assert e.extra.get("title_sync") == "fix-the-flaky-upload"
    clock.advance(3)

    # nothing usable -> name kept, auto_name dropped
    add_auto("new2")
    d._registry = list(load(env.paths.registry).entries)
    monkeypatch.setattr(kinds.Claude, "first_prompt",
                      lambda self, paths, entry, reading: "!!!")
    d._step_auto_name()
    e = load(env.paths.registry).get("new2")
    assert e is not None
    assert e.extra.get("auto_name") is None
    clock.advance(3)

    # no first prompt yet -> nothing changes, auto_name stays
    add_auto("new3")
    d._registry = list(load(env.paths.registry).entries)
    monkeypatch.setattr(kinds.Claude, "first_prompt",
                      lambda self, paths, entry, reading: None)
    d._step_auto_name()
    e = load(env.paths.registry).get("new3")
    assert e.extra.get("auto_name") is True
    assert e.name == "new3"


def test_create_session_without_remote_control(env, monkeypatch, capsys):
    """SPEC §53: `force new --no-remote-control` keeps `remote_control: false` and the launch drops the flag."""
    cockpit = env.make_cockpit()
    monkeypatch.setattr(cockpit, "create", lambda entry: None)
    monkeypatch.setattr(kinds.Claude, "available", lambda self, paths, cfg: True)
    monkeypatch.delenv("FORCE_NO_REMOTE_CONTROL", raising=False)
    assert cli.create_session(cockpit, "quiet", cwd=str(env.tmp), no_switch=True, remote_control=False) == 0
    assert cli.create_session(cockpit, "loud", cwd=str(env.tmp), no_switch=True) == 0
    reg = load(env.paths.registry)
    assert reg.get("quiet").extra.get("remote_control") is False
    assert "remote_control" not in reg.get("loud").extra, "on is the default: nothing stored"
    quiet, _ = kinds.Claude().exec_argv(cockpit, reg.get("quiet"))
    loud, _ = kinds.Claude().exec_argv(cockpit, reg.get("loud"))
    assert "--remote-control" not in quiet and quiet[quiet.index("-n") + 1] == "quiet"
    assert loud[loud.index("--remote-control") + 1] == "loud"


def test_no_remote_control_is_for_claude_only(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    monkeypatch.setattr(cockpit, "create", lambda entry: None)
    assert cli.create_session(cockpit, "sh", cwd=str(env.tmp), no_switch=True, kind="shell",
                              remote_control=False) == 1
    assert "Claude Code sessions only" in capsys.readouterr().err
    assert load(env.paths.registry).get("sh") is None


def test_new_parser_no_remote_control():
    args = cli.build_parser().parse_args(["new", "x", "--no-remote-control"])
    assert args.remote_control is False
    assert cli.build_parser().parse_args(["new", "x"]).remote_control is True
