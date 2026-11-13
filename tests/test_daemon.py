import dataclasses
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from force.daemon import Daemon, _parse_mode
from force.registry import load, locked
from force.state import State, load_seen, read_snapshot
from force.tmux import Client


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def _uuid():
    return str(uuid.uuid4())


def current_pid(cockpit, name):
    p = cockpit.agent_pane(name)
    return p.pid if p else None


def current_status(cockpit, name):
    p = cockpit.agent_pane(name)
    if p is None or not p.pid:
        return None
    path = cockpit.paths.claude_dir / "sessions" / f"{p.pid}.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def ctl(cockpit, sess, **kw):
    p = cockpit.agent_pane(sess)
    if p is None or not p.pid:
        return
    path = cockpit.paths.claude_dir / "sessions" / f"{p.pid}.ctl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kw))


def entry(paths, name):
    return load(paths.registry).get(name)


def add_live(cockpit, name, cwd="/tmp"):
    with locked(cockpit.paths.home) as reg:
        e = reg.add(name, _uuid(), cwd)
    cockpit.create(e)
    assert wait_for(lambda: current_pid(cockpit, name) is not None), f"{name} not running"
    return name


class FakeClock:
    def __init__(self, start=None):
        self.t = start if start is not None else time.time()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


# --- crash detection exit-reason unknown --------------------------------------
class _StubPane:
    def __init__(self, dead=True, dstat=None, dsig=None, pid=None):
        self.dead = dead
        self.dead_status = dstat
        self.dead_signal = dsig
        self.pid = pid
        self.pane_id = "%0"


def _crash_stub():
    from force.config import Config
    from force.registry import Entry

    class Stub:
        cfg = Config()

    stub = Stub()
    stub.agent_pane = lambda name: _StubPane(dead=False)
    stub.restart_calls = []
    stub.restart = lambda name: stub.restart_calls.append(name)
    stub.on_cockpit_kill = staticmethod(lambda n: None)
    stub.recently_killed_by_cockpit = lambda name: False
    stub.paths = None
    entry = Entry(name="x", session_id=_uuid(), cwd="/tmp", created_at="now", order=0)
    d = Daemon(stub, FakeClock())
    d._registry = [entry]
    return d, stub


def test_crash_unknown_then_status_known_no_restart():
    d, stub = _crash_stub()
    stub.agent_pane = lambda name: _StubPane(dead=True)          # None/None
    d._step_crash()
    d._step_crash()                                              # still unknown
    assert stub.restart_calls == []
    # tmux fills the status in -> exit 0 -> no restart
    stub.agent_pane = lambda name: _StubPane(dead=True, dstat=0)
    d._step_crash()
    assert stub.restart_calls == []


def test_crash_unknown_past_grace_restarts():
    d, stub = _crash_stub()
    stub.agent_pane = lambda name: _StubPane(dead=True)          # None/None
    d._step_crash()                                              # first seen
    d.clock.advance(6)                                        # > 5 s grace
    d._step_crash()
    assert stub.restart_calls == ["x"]


def test_crash_pane_alive_clears_unknown():
    d, stub = _crash_stub()
    stub.agent_pane = lambda name: _StubPane(dead=True)          # unknown
    d._step_crash()
    assert "x" in d._crash_unknown
    stub.agent_pane = lambda name: _StubPane(dead=False)         # alive again
    d._step_crash()
    assert "x" not in d._crash_unknown


def test_restore_after_kill_server(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    subprocess.run(["tmux", "-L", env.socket, "kill-server"], capture_output=True)
    assert not cockpit.tmux.has_session("portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert cockpit.tmux.has_session("portal")


def test_follow_session_id_change(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    new_sid = _uuid()
    ctl(cockpit, "portal", sessionId=new_sid)
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("sessionId") == new_sid)
    d.tick()
    assert entry(env.paths, "portal").session_id == new_sid


def test_adopt_user_rename_and_ignore_invalid(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    d = Daemon(cockpit, FakeClock())
    d.tick()            # the name Claude has now is remembered; only a change of it is adopted (§49.6)
    ctl(cockpit, "portal", name="New Name!")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("name") == "New Name!")
    d.tick()
    assert entry(env.paths, "new-name") is not None
    assert cockpit.tmux.has_session("new-name")
    old = entry(env.paths, "new-name").session_id
    # ignore invalid: name that slugifies to None
    ctl(cockpit, "new-name", name="!!!")
    assert wait_for(lambda: (current_status(cockpit, "new-name") or {}).get("name") == "!!!")
    d.tick()
    assert entry(env.paths, "new-name") is not None


def test_force_rename_without_restart_is_not_undone(env):
    """An automatic name (§49.6) renames the session while Claude still carries its start name (`-n new`):
    that unchanged name must not be adopted back."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "new")
    assert wait_for(lambda: current_status(cockpit, "new") is not None)
    ctl(cockpit, "new", name="new")
    assert wait_for(lambda: (current_status(cockpit, "new") or {}).get("name") == "new")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert d._rename_now("new", "fix-login", extra_pop=("auto_name",), title_sync=True)
    for _ in range(3):
        d.tick()
    assert entry(env.paths, "fix-login") is not None
    assert entry(env.paths, "new") is None
    assert entry(env.paths, "fix-login").extra.get("title_sync") == "fix-login"


def test_crash_restart_limited(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())

    def crash_and_restart():
        before = current_pid(cockpit, "portal")
        ctl(cockpit, "portal", exit=3)
        assert wait_for(lambda: current_pid(cockpit, "portal") == before
                     and cockpit.agent_pane("portal") is not None
                     and cockpit.agent_pane("portal").dead)
        # pump ticks, advancing the clock so the exit-reason grace (if tmux reports None/None)
        # always passes; a known non-zero status restarts on the first tick.
        for _ in range(10):
            d.clock.advance(6)
            d.tick()
            if current_pid(cockpit, "portal") not in (None, before):
                break
        assert current_pid(cockpit, "portal") != before

    for _ in range(env.cfg.crash_restart_max):
        crash_and_restart()
    # now crash again -> leave DOWN
    before = current_pid(cockpit, "portal")
    ctl(cockpit, "portal", exit=3)
    assert wait_for(lambda: current_pid(cockpit, "portal") == before
                 and cockpit.agent_pane("portal").dead)
    d.tick()
    assert cockpit.agent_pane("portal").dead


def test_exit_zero_stays_down(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    pane = cockpit.agent_pane("portal")
    ctl(cockpit, "portal", exit=0)
    assert wait_for(lambda: cockpit.agent_pane("portal") is not None
                 and cockpit.agent_pane("portal").dead)
    d.tick()
    assert cockpit.agent_pane("portal").dead


def test_pending_rename_applied_when_idle_not_busy(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    # busy -> not applied
    ctl(cockpit, "portal", status="busy")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "busy")
    with locked(env.paths.home) as reg:
        reg.get("portal").pending_name = "newname"
    d.tick()
    assert entry(env.paths, "portal").pending_name == "newname"
    # the card says so while it waits
    view = next(v for v in json.loads(env.paths.snapshot.read_text())["sessions"] if v["name"] == "portal")
    assert view["note"] == "→ newname when idle"
    # idle -> applied
    ctl(cockpit, "portal", status="idle")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "idle")
    d.tick()
    assert entry(env.paths, "newname") is not None
    assert entry(env.paths, "newname").pending_name is None


def test_auto_upgrade_idle_after_grace_and_client(env, monkeypatch):
    monkeypatch.setattr("force.claude.process_version", lambda pid: "9.9.8")
    env.cfg = dataclasses.replace(env.cfg, upgrade_idle_minutes=0.0)
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    clk = FakeClock()
    d = Daemon(cockpit, clk)
    d.tick()
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    before = current_pid(cockpit, "portal")
    # client attached -> no upgrade
    monkeypatch.setattr(env.tmux, "clients",
                      lambda: [Client("c0", "portal", 80, 24, 0)])
    clk.advance(31)
    d.tick()
    assert current_pid(cockpit, "portal") == before
    # no client -> upgrade restarts
    monkeypatch.setattr(env.tmux, "clients", lambda: [])
    clk.advance(31)
    d.tick()
    assert wait_for(lambda: current_pid(cockpit, "portal") != before)


def test_turn_done_transition_and_snapshot(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    clk = FakeClock()
    d = Daemon(cockpit, clk)
    d.tick()
    # first observation: no turn_done
    assert load_seen(env.paths.seen).get("portal") is None or \
        load_seen(env.paths.seen)["portal"].turn_done_ms == 0
    ctl(cockpit, "portal", status="busy")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "busy")
    d.tick()
    ctl(cockpit, "portal", status="idle")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "idle")
    d.tick()
    seen = load_seen(env.paths.seen)
    assert seen["portal"].turn_done_ms > seen["portal"].last_seen_ms
    snap = read_snapshot(env.paths.snapshot)
    assert snap is not None
    sv = [s for s in snap.sessions if s.name == "portal"][0]
    assert sv.state is State.YOUR_TURN


def test_shell_untouched_stays_shell(env, monkeypatch):
    """A shell pane running `sleep` stays SHELL across ticks: no follow adoption, no auto-upgrade, no
    notification, no activity, and it is never restarted."""
    monkeypatch.setattr("force.claude.process_version", lambda pid: "9.9.8")  # looks outdated
    env.cfg = dataclasses.replace(env.cfg, upgrade_idle_minutes=0.0)
    cockpit = env.make_cockpit()
    with locked(cockpit.paths.home) as reg:
        e = reg.add("sh", _uuid(), "/tmp")
        e.extra["kind"] = "shell"
    cockpit.create(e)
    assert wait_for(lambda: current_pid(cockpit, "sh") is not None)
    d = Daemon(cockpit, FakeClock())
    d.tick()
    pid0 = current_pid(cockpit, "sh")
    d.clock.advance(31)
    d.tick()
    d.tick()
    assert current_pid(cockpit, "sh") == pid0                     # never restarted
    sv = [s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "sh"][0]
    assert sv.state is State.SHELL
    assert sv.activity is None
    assert read_snapshot(env.paths.snapshot).counts() == {
        "needs_you": 0, "working": 0, "your_turn": 0,
        "idle": 0, "stale": 0, "down": 0}
    # no YOUR_TURN/NEEDS_YOU recorded in seen (never notified)
    seen = load_seen(env.paths.seen)
    assert "sh" not in seen or seen["sh"].turn_done_ms == 0


def test_snapshot_and_headers_written(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert env.paths.snapshot.exists()
    # the header is a pane now (`force-header`); the daemon writes no status-line files
    assert not (env.paths.header_dir / "portal.txt").exists()


def test_status_file_without_a_status_yet_shows_starting_with_the_pane_age(env):
    # issue #23: right after start Claude Code's file has no status / statusUpdatedAt yet. The card is STARTING
    # and its age counts from the pane's start, not from 1970 (UNKNOWN, ~20769 d).
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    pid = current_pid(cockpit, "portal")
    path = cockpit.paths.claude_dir / "sessions" / f"{pid}.json"
    path.write_text(json.dumps({"pid": pid, "sessionId": entry(env.paths, "portal").session_id, "cwd": "/tmp"}))
    d = Daemon(cockpit, FakeClock())
    d.tick()
    view = next(s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal")
    assert view.state is State.STARTING
    started = cockpit.agent_pane("portal").started_ms
    assert started and view.since_ms == started
    assert time.time() * 1000 - view.since_ms < 60_000


def test_switch_targets(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "a")
    add_live(cockpit, "b")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert env.tmux.get_option("@force_idx_1") == "a"
    assert env.tmux.get_option("@force_idx_2") == "b"
    assert env.tmux.get_option("@force_idx_3") is None
    nxt_a = env.tmux.run("show-options", "-t", "a", "-qv", "@force_next", check=False)
    assert nxt_a == "b"


def test_read_status_once_per_session_per_tick(env, monkeypatch):
    import force.claude as cmod
    cockpit = env.make_cockpit()
    for i in range(5):
        add_live(cockpit, f"s{i}")
    d = Daemon(cockpit, FakeClock())
    d.tick()  # warm up (settles _last_upgrade so step 5 is skipped on the measured tick)
    calls = []
    orig = cmod.read_status
    monkeypatch.setattr(cmod, "read_status",
                      lambda *a, **k: (calls.append(1) or orig(*a, **k)))
    d.tick()
    assert calls == [1] * 5   # at most once per session per tick


def test_tick_subprocess_count_steady_state(env, monkeypatch):
    import force.tmux as tmod
    cockpit = env.make_cockpit()
    for i in range(5):
        add_live(cockpit, f"s{i}")
    assert wait_for(lambda: sum(1 for p in cockpit.tmux.panes() if p.role == "sidebar") == 5)
    d = Daemon(cockpit, FakeClock())
    d.tick()
    d.tick()  # warm up: switch targets written, caches settled
    calls = []
    orig = tmod.subprocess.run
    monkeypatch.setattr(tmod.subprocess, "run",
                      lambda *a, **k: (calls.append(a[0]) or orig(*a, **k)))
    d.tick()  # the measured steady-state tick
    assert len(calls) <= 15


def test_pending_restart_waits_until_idle(env):
    """`force restart --all` marks busy sessions; the daemon restarts them only once they are idle."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    pid0 = cockpit.agent_pane("portal").pid
    ctl(cockpit, "portal", status="busy")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "busy")
    with locked(env.paths.home) as reg:
        reg.get("portal").extra["pending_restart"] = True
    d.tick()
    assert cockpit.agent_pane("portal").pid == pid0                  # busy: not restarted
    assert entry(env.paths, "portal").extra.get("pending_restart") is True
    ctl(cockpit, "portal", status="idle")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "idle")
    d.tick()
    assert wait_for(lambda: cockpit.agent_pane("portal") is not None
                    and not cockpit.agent_pane("portal").dead
                    and cockpit.agent_pane("portal").pid != pid0)     # restarted
    assert "pending_restart" not in entry(env.paths, "portal").extra


def test_cli_restart_is_not_mistaken_for_a_crash(env):
    """A restart from another process (CLI) leaves a marker; the daemon must not respawn it as a crash."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    other = env.make_cockpit()                     # e.g. `force restart` in a terminal
    other.stop_agent("portal")
    assert cockpit.agent_pane("portal").dead
    d.tick()
    assert cockpit.agent_pane("portal").dead      # left alone: the CLI respawns it itself


def test_activity_cached_until_transcript_changes(env, monkeypatch):
    import json as _json
    from force.config import Config
    sid = _uuid()
    projects = env.paths.claude_dir / "projects" / "-tmp"
    projects.mkdir(parents=True, exist_ok=True)
    tp = projects / f"{sid}.jsonl"
    tp.write_text(_json.dumps({"type": "assistant",
                             "message": {"content": [{"type": "text", "text": "one"}]}}) + "\n")

    class Stub:
        cfg = Config()
        paths = env.paths
        tmux = env.tmux
        on_cockpit_kill = staticmethod(lambda n: None)

    clk = FakeClock(start=1000.0)
    d = Daemon(Stub(), clk)
    calls = []
    monkeypatch.setattr("force.daemon.activity.scan",
                      lambda path, max_len=60:
                      (calls.append(1) or ("desc", "opus 5.5", "high", None)))
    trip = ("desc", "opus 5.5", "high", None)
    assert d._config_for(sid) == trip
    assert len(calls) == 1
    # transcript changed, but within 3 s of the last compute -> throttled: stale value returned, no call
    # (text lengths differ so the (mtime,size) key is guaranteed to change)
    tp.write_text(_json.dumps({"type": "assistant",
                             "message": {"content": [{"type": "text", "text": "two two two"}]}}) + "\n")
    assert d._config_for(sid) == trip
    assert len(calls) == 1
    # once 3 s pass, the pending change is recomputed -> one more call
    clk.advance(5)
    assert d._config_for(sid) == trip
    assert len(calls) == 2
    # unchanged since the recompute -> cached across ticks
    assert d._config_for(sid) == trip
    assert len(calls) == 2
    # changed again and past 3 s of the last compute -> recompute
    tp.write_text(_json.dumps({"type": "assistant",
                             "message": {"content": [{"type": "text", "text": "three three three three"}]}}) + "\n")
    clk.advance(4)
    assert d._config_for(sid) == trip
    assert len(calls) == 3


# --- permission-mode badge (SPEC §16) ------------------------------------------

def _mode_entry(name="x"):
    from force.registry import Entry
    return Entry(name=name, session_id=_uuid(), cwd="/tmp", created_at="now", order=0)


class _ModeStub:
    """A tiny stand-in for cockpit with a scripted tmux.capture_tail."""
    def __init__(self, tail=""):
        from force.config import Config
        self.cfg = Config()
        self.paths = None
        self.capture_calls = 0
        self.tail = tail

    def _capture(self, pane_id, lines=6):
        self.capture_calls += 1
        return self.tail


def _mode_daemon(tail="", start=0.0):
    stub = _ModeStub(tail)
    stub.tmux = SimpleNamespace(capture_tail=stub._capture)
    d = Daemon(stub, FakeClock(start=start))
    d._registry = [_mode_entry()]
    return d, stub


@pytest.mark.parametrize("phrase,mode", [
    ("auto mode on", "auto"),
    ("manual mode on", "manual"),
    ("accept edits on", "edits"),
    ("plan mode on", "plan"),
    ("bypass permissions on", "bypass"),
])
def test_parse_mode_each_mode(phrase, mode):
    assert _parse_mode("some text before " + phrase + " trailing") == mode
    assert _parse_mode("") is None
    assert _parse_mode("no phrase here") is None


def test_parse_mode_last_occurrence_wins():
    assert _parse_mode("manual mode on\nplan mode on") == "plan"
    assert _parse_mode("auto mode on\naccept edits on\nbypass permissions on") == "bypass"
    # case-sensitive: an uppercase variant is NOT a match
    assert _parse_mode("AUTO MODE ON") is None


@pytest.mark.parametrize("phrase,mode", [
    ("auto mode on", "auto"), ("manual mode on", "manual"), ("accept edits on", "edits"),
    ("plan mode on", "plan"), ("bypass permissions on", "bypass"),
])
def test_mode_for_reads_mode(phrase, mode):
    d, stub = _mode_daemon(tail=f"footer: {phrase} more text")
    pane = SimpleNamespace(pid=1, pane_id="%1", dead=False)
    assert d._mode_for("x", pane) == mode


def test_mode_no_phrase_keeps_previous():
    d, stub = _mode_daemon(tail="plan mode on", start=0.0)
    pane = SimpleNamespace(pid=1, pane_id="%1", dead=False)
    assert d._mode_for("x", pane) == "plan"
    stub.tail = "a dialog covers the footer"     # not within 2 s yet -> throttled, kept
    assert d._mode_for("x", pane) == "plan"
    assert stub.capture_calls == 1
    d.clock.advance(3)                        # past the throttle -> capture, but no phrase -> keep
    assert d._mode_for("x", pane) == "plan"
    assert stub.capture_calls == 2


def test_mode_throttle_capture_once_per_2s():
    d, stub = _mode_daemon(tail="auto mode on", start=100.0)
    pane = SimpleNamespace(pid=1, pane_id="%1", dead=False)
    d._mode_for("x", pane)
    assert stub.capture_calls == 1
    d.clock.advance(1.0)                     # within 2 s -> no capture
    assert d._mode_for("x", pane) == "auto"
    assert stub.capture_calls == 1
    d.clock.advance(1.1)                     # now 2.1 s elapsed -> capture again
    assert d._mode_for("x", pane) == "auto"
    assert stub.capture_calls == 2


def test_mode_pane_pid_change_resets():
    d, stub = _mode_daemon(tail="plan mode on", start=0.0)
    p1 = SimpleNamespace(pid=1, pane_id="%1", dead=False)
    assert d._mode_for("x", p1) == "plan"
    d.clock.advance(3)
    p2 = SimpleNamespace(pid=2, pane_id="%2", dead=False)   # pane restarted
    stub.tail = "no phrase while starting"
    assert d._mode_for("x", p2) is None                      # old mode forgotten
    d.clock.advance(3)
    stub.tail = "manual mode on"
    assert d._mode_for("x", p2) == "manual"


def test_mode_no_live_pane_keeps_last_known():
    d, stub = _mode_daemon(tail="manual mode on", start=0.0)
    d._mode_for("x", SimpleNamespace(pid=1, pane_id="%1", dead=False))
    # dead pane / None -> last known mode kept, no capture
    dead = SimpleNamespace(pid=1, pane_id="%1", dead=True)
    assert d._mode_for("x", dead) == "manual"
    assert stub.capture_calls == 1


def test_snapshot_includes_mode(env, monkeypatch):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    monkeypatch.setattr(cockpit.tmux, "capture_tail", lambda pane_id, lines=6: "plan mode on")
    d = Daemon(cockpit, FakeClock(start=1000.0))
    d.tick()
    sv = [s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal"][0]
    assert sv.mode == "plan"


def test_snapshot_includes_model_effort(env, monkeypatch):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    monkeypatch.setattr("force.daemon.activity.scan",
                      lambda path, max_len=60: ("editing a.py", "opus 5.5", "high", None))
    # the fake agent writes its status file and transcript just after it starts: tick once they exist
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    d = Daemon(cockpit, FakeClock(start=1000.0))
    d.tick()
    sv = [s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal"][0]
    assert sv.activity == "editing a.py"
    assert sv.model == "opus 5.5"
    assert sv.effort == "high"
    # cached: a second tick does not recompute from the transcript (still one scan call)
    calls = []
    monkeypatch.setattr("force.daemon.activity.scan",
                      lambda path, max_len=60: (calls.append(1) or ("x", None, None, None)))
    d.clock.advance(1)
    d.tick()
    sv2 = [s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal"][0]
    assert sv2.model == "opus 5.5" and sv2.effort == "high"   # unchanged transcript -> cached
    assert calls == []


def test_snapshot_shell_mode_none(env):
    """Shells never get a permission-mode badge (even if a capture would return a phrase)."""
    cockpit = env.make_cockpit()
    with locked(cockpit.paths.home) as reg:
        e = reg.add("sh", _uuid(), "/tmp")
        e.extra["kind"] = "shell"
    cockpit.create(e)
    assert wait_for(lambda: current_pid(cockpit, "sh") is not None)
    d = Daemon(cockpit, FakeClock(start=1000.0))
    d.tick()
    sv = [s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "sh"][0]
    assert sv.state is State.SHELL
    assert sv.mode is None


# --- session sharing (SPEC §19) ---------------------------------------------------
import signal  # noqa: E402
from force.registry import Entry as _Entry  # noqa: E402


def _share_env(tmp_path, monkeypatch):
    """A Daemon on a stub cockpit whose shares step does no real process work."""
    from force.config import Config
    state_dir = tmp_path / "state"
    tmux_conf = tmp_path / "tmux.conf"
    tmux_conf.write_text("")
    (tmp_path / "force-join").write_text("#!/usr/bin/env python3\n")
    stub = SimpleNamespace(
        paths=SimpleNamespace(state=state_dir, tmux_conf=tmux_conf),
        cfg=Config(sharing=True),
        cockpit_bin="cockpit",
        tmux=None,
    )
    starts = []

    class FakeProc:
        def __init__(self, argv, **kw):
            assert kw.get("start_new_session") is True
            assert kw.get("stdin") is subprocess.DEVNULL
            self.pid = 5000 + len(starts)
            starts.append(argv)

    monkeypatch.setattr("force.daemon.subprocess.Popen", FakeProc)
    monkeypatch.setattr("force.share.ensure_share_dir", lambda: (tmp_path / "share").mkdir(exist_ok=True))
    monkeypatch.setattr("force.share.share_dir",
                      lambda owner=None: __import__("pathlib").Path(tmp_path) / "share")
    d = Daemon(stub, FakeClock(start=1000.0))
    return d, state_dir, starts


def _sentry(name, shares=None):
    e = _Entry(name=name, session_id=_uuid(), cwd="/tmp", created_at="now", order=0)
    if shares:
        e.extra["shares"] = dict(shares)
    return e


def _track(state_dir, name, user, pid, mode):
    d = state_dir / "shares"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.{user}.json").write_text(json.dumps({"pid": pid, "mode": mode}))


def test_shares_starts_one_proxy_per_desired(tmp_path, monkeypatch):
    d, state_dir, starts = _share_env(tmp_path, monkeypatch)
    d._registry = [_sentry("portal", {"carol": "r", "daniel": "w"})]
    d._step_shares()
    assert {tuple(a[2:]) for a in starts} == {("portal", "carol", "r"), ("portal", "daniel", "w")}
    assert starts[0][:2] == ["cockpit", "share-proxy"]
    assert json.loads((state_dir / "shares" / "portal.carol.json").read_text())["mode"] == "r"
    assert json.loads((state_dir / "shares" / "portal.daniel.json").read_text())["mode"] == "w"


def test_shares_does_not_restart_live_proxy(tmp_path, monkeypatch):
    d, state_dir, starts = _share_env(tmp_path, monkeypatch)
    d._registry = [_sentry("portal", {"carol": "r"})]
    _track(state_dir, "portal", "carol", 555, "r")
    monkeypatch.setattr("force.daemon._pid_is_live_proxy", lambda pid: True)
    d._step_shares()
    assert starts == []
    assert json.loads((state_dir / "shares" / "portal.carol.json").read_text())["pid"] == 555


def test_shares_restarts_on_mode_change(tmp_path, monkeypatch):
    d, state_dir, starts = _share_env(tmp_path, monkeypatch)
    d._registry = [_sentry("portal", {"carol": "w"})]
    _track(state_dir, "portal", "carol", 555, "r")
    monkeypatch.setattr("force.daemon._pid_is_live_proxy", lambda pid: True)
    killed = []
    monkeypatch.setattr("force.daemon.os.kill", lambda pid, sig: killed.append((pid, sig)))
    d._step_shares()
    assert (555, signal.SIGTERM) in killed           # old proxy stopped
    assert len(starts) == 1 and starts[0][4] == "w"
    assert json.loads((state_dir / "shares" / "portal.carol.json").read_text())["mode"] == "w"


def test_shares_stops_on_unshare(tmp_path, monkeypatch):
    d, state_dir, starts = _share_env(tmp_path, monkeypatch)
    d._registry = [_sentry("portal")]                 # no shares any more
    _track(state_dir, "portal", "carol", 555, "r")
    killed = []
    monkeypatch.setattr("force.daemon.os.kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr("force.daemon._pid_is_live_proxy", lambda pid: pid == 555)   # it IS our proxy
    d._step_shares()
    assert killed == [555]
    assert not (state_dir / "shares" / "portal.carol.json").exists()
    assert starts == []


def test_shares_throttles_restarts(tmp_path, monkeypatch):
    d, state_dir, starts = _share_env(tmp_path, monkeypatch)
    d._registry = [_sentry("portal", {"carol": "r"})]
    d._step_shares()
    assert len(starts) == 1
    # next tick within 10 s with a dead (not-live) proxy -> throttled, no restart
    monkeypatch.setattr("force.daemon._pid_is_live_proxy", lambda pid: False)
    d.clock.advance(5)
    d._step_shares()
    assert len(starts) == 1
    # past 10 s of clock time -> restarted
    d.clock.advance(6)
    d._step_shares()
    assert len(starts) == 2


def test_shares_installs_cockpit_join(tmp_path, monkeypatch):
    d, state_dir, _starts = _share_env(tmp_path, monkeypatch)
    d._registry = [_sentry("portal", {"carol": "r"})]
    d._step_shares()
    dest = tmp_path / "share" / "force-join"
    assert dest.read_text() == "#!/usr/bin/env python3\n"
    assert (dest.stat().st_mode & 0o777) == 0o755
    mtime = dest.stat().st_mtime_ns
    monkeypatch.setattr("force.daemon._pid_is_live_proxy", lambda pid: False)
    d._step_shares()                 # identical + 0755 -> not rewritten (and proxy throttled)
    assert dest.stat().st_mtime_ns == mtime


def test_shares_skips_step_on_ensure_permission(tmp_path, monkeypatch):
    d, state_dir, _starts = _share_env(tmp_path, monkeypatch)
    d._registry = [_sentry("portal", {"carol": "r"})]
    monkeypatch.setattr("force.daemon.share_mod.ensure_share_dir", lambda: (_ for _ in ()).throw(PermissionError("boom")))
    d._step_shares()                  # must not raise; logs and skips


def test_stop_never_signals_a_reused_pid(monkeypatch):
    """Regression: a stale pidfile must not make the daemon SIGTERM an unrelated process."""
    import subprocess
    import force.daemon as dm
    p = subprocess.Popen(["sleep", "30"])          # unrelated process, cmdline has no "share-proxy"
    try:
        dm._stop(p.pid)
        assert p.poll() is None                     # still running
    finally:
        p.kill()
        p.wait()


def test_shares_disabled_stops_every_proxy(tmp_path, monkeypatch):
    import dataclasses
    d, state_dir, starts = _share_env(tmp_path, monkeypatch)
    d._registry = [_sentry("portal", {"carol": "r"})]
    d._step_shares()
    assert starts and (state_dir / "shares" / "portal.carol.json").exists()
    d.cfg = dataclasses.replace(d.cfg, sharing=False)
    n = len(starts)
    d._step_shares()
    assert len(starts) == n
    assert not (state_dir / "shares" / "portal.carol.json").exists()


def test_shell_status_shows_waiting_for_a_command():
    from types import SimpleNamespace
    from force.daemon import shown_activity
    assert shown_activity(SimpleNamespace(status="shell"), "writing a reply") == "waiting for a command"
    assert shown_activity(SimpleNamespace(status="busy"), "writing a reply") == "writing a reply"
    assert shown_activity(None, "editing files") == "editing files"
    assert shown_activity(SimpleNamespace(status="idle"), None) is None


def test_pending_restart_activity():
    from force.daemon import pending_restart_activity
    assert pending_restart_activity(State.WORKING) == "restarts after this turn"
    assert pending_restart_activity(State.NEEDS_YOU) is None     # keeps its prompt label
    assert pending_restart_activity(State.IDLE) is None


def test_restart_note():
    from force.daemon import restart_note
    assert restart_note(State.IDLE, None, False, 100) is None            # never stopped by force
    assert restart_note(State.IDLE, 3600, False, 100) is None            # long ago
    assert restart_note(State.IDLE, 0.5, True, 900) == "restarting"      # old process still dying
    assert restart_note(State.STARTING, 2, False, 1) == "restarting"     # new one coming up
    assert restart_note(State.IDLE, 5, False, 3) == "restarted"          # back, just now
    assert restart_note(State.YOUR_TURN, 5, False, 3) == "restarted"
    assert restart_note(State.IDLE, 20, False, 15) is None               # after a few seconds
    assert restart_note(State.WORKING, 5, False, 3) is None              # its activity shows instead


def test_restart_puts_note_in_snapshot(env):
    """A restarted session's card says "restarting" until Claude is back, then "restarted"."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    d.tick()
    note = lambda: next(s.note for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal")
    assert note() is None
    cockpit.restart("portal")
    d.tick()
    assert note() in ("restarting", "restarted")
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    d.tick()
    assert note() == "restarted"


def test_tiers_step_reruns_tier_for_viewed_sessions(env, monkeypatch):
    """SPEC §28: the daemon re-runs tier.sh for every session a client shows (throttled), so a client
    attached before rich_terminals changed does not keep the old tier until the next switch."""
    from force.tmux import Client
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    calls = []
    real_run = subprocess.run

    def fake_run(argv, *a, **kw):
        if argv and argv[0] == "sh" and str(argv[1]).endswith("tier.sh"):
            calls.append(argv[3:])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *a, **kw)
    monkeypatch.setattr(cockpit.tmux, "clients", lambda: [Client("/dev/pts/9", "portal", 100, 40, 1)])
    monkeypatch.setattr("force.daemon.subprocess.run", fake_run)
    d._step_tiers()
    assert calls == [["portal", "--signal"]]
    d._step_tiers()                        # throttled: not again within a few seconds
    assert calls == [["portal", "--signal"]]


def test_archiving_note_replaces_restart_feedback(env):
    """A session being archived says "archiving", not "restarting" (its stop leaves the kill mark too)."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    with locked(env.paths.home) as reg:
        reg.get("portal").extra["archiving"] = True
    cockpit.stop_agent("portal")
    d.tick()
    s = next(s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal")
    assert s.note == "archiving" and s.activity is None


def test_moving_notes_and_pending_move(env, tmp_path):
    """SPEC §7.7: a moving session says "moving", then "moved" after its restart; a pending move runs
    once the session is idle."""
    dest = tmp_path / "dest"
    dest.mkdir()
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    note = lambda: next(s.note for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal")
    with locked(env.paths.home) as reg:
        reg.get("portal").extra["moving"] = True
    d.tick()
    assert note() == "moving"
    with locked(env.paths.home) as reg:
        reg.get("portal").extra.pop("moving")
        reg.get("portal").extra["pending_move"] = str(dest)
    d.tick()                       # idle: the daemon moves it now
    e = load(env.paths.registry).get("portal")
    assert e.cwd == str(dest) and "pending_move" not in e.extra
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("cwd") == str(dest))
    d.tick()
    assert note() in ("moving", "moved")


def test_trust_question_is_needs_you(env, monkeypatch):
    """Claude's "trust this folder?" writes no status file: the card says needs you, not starting/down."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    view = lambda: next(s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal")
    real_capture = cockpit.tmux.capture_tail
    screen = "Accessing workspace:\n /tmp\n Is this a project you created or one you trust?\n ❯ No, exit\n   Yes, I trust this folder\n"
    monkeypatch.setattr(cockpit.tmux, "capture_tail", lambda pane, lines=6: screen)
    # with a status file the screen is never looked at
    d.tick()
    assert view().state != State.NEEDS_YOU
    # no status file (the question comes before Claude writes one)
    import force.daemon as dm
    monkeypatch.setattr(dm, "compute_state_why", lambda *a, **k: (State.STARTING, "patched"))
    d.tick()
    assert view().state == State.NEEDS_YOU and view().activity == "trust this folder?"
    monkeypatch.setattr(cockpit.tmux, "capture_tail", real_capture)
    d.tick()
    assert view().state == State.STARTING


def test_daemon_retags_legacy_claude_panes(env):
    """A pre-rebrand pane tagged @force_role=claude is found as the agent pane, and the daemon's
    re-tag step turns it into "agent" on the live server (SPEC role rename)."""
    from force.tmux import LEGACY_AGENT_ROLE
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    p = cockpit.agent_pane("portal")
    assert p is not None and p.role == "agent"
    # simulate a live server that still carries the legacy role "claude" on the pane
    cockpit.tmux.run("set-option", "-p", "-t", p.pane_id, "@force_role", LEGACY_AGENT_ROLE)
    cockpit.tmux.invalidate()
    raw = cockpit.tmux.run("list-panes", "-a", "-F", "#{session_name} #{@force_role}")
    assert LEGACY_AGENT_ROLE in [l.split()[-1] for l in raw.splitlines()]
    # compatibility: the legacy-tagged pane is still found as the agent pane
    assert cockpit.agent_pane("portal") is not None and cockpit.agent_pane("portal").pane_id == p.pane_id
    # the daemon's re-tag step turns the live server's role into "agent"
    d = Daemon(cockpit, FakeClock())
    d.tick()
    raw = cockpit.tmux.run("list-panes", "-a", "-F", "#{pane_id} #{session_name} #{@force_role}")
    for line in raw.splitlines():
        pid, sess, role = line.split()
        if pid == p.pane_id:
            assert sess == "portal" and role == "agent" and LEGACY_AGENT_ROLE not in (role,)
    # and the pane is still read back as the agent pane
    assert cockpit.agent_pane("portal") is not None and cockpit.agent_pane("portal").pane_id == p.pane_id


def test_daemon_points_the_ui_server_at_the_live_app(env):
    """A ui server started by an older release holds @force_app as that release's path: the daemon resets it to
    the app path at start, so its bindings run the live release's scripts."""
    cockpit = env.make_cockpit()
    ui = cockpit.ui_tmux
    ui.run("new-session", "-d", "-s", "ui-old", "sleep 60")
    ui.set_option("@force_app", "/old/releases/20200101-000000-dead")
    Daemon(cockpit, FakeClock()).tick()
    assert ui.get_option("@force_app") == os.path.abspath(Path(cockpit.paths.tmux_conf).parent.parent)


def test_daemon_respawns_ui_panes_of_an_older_release_at_start(env, tmp_path):
    """An upgrade that restarts the daemon is never seen by the live reload: at start the daemon respawns sidebar and
    header panes whose program is another release's (a pane running anything else is left alone)."""
    import shutil
    import tempfile
    cockpit = env.make_cockpit()
    ui = cockpit.ui_tmux
    # outside the test's app tree (which is under the base temp dir, like tmp_path)
    rel = Path(tempfile.mkdtemp(prefix="forcetest-rel-"))
    old = rel / "releases" / "20200101-000000-dead" / "share" / "bin"
    old.mkdir(parents=True)
    shutil.copy(shutil.which("sleep"), old / "force-sidebar")
    ui.run("new-session", "-d", "-s", "ui-old", f"exec {old / 'force-sidebar'} 60")   # exec, as Force starts panes
    ui.run("split-window", "-d", "-t", "=ui-old:", "exec sleep 60")
    panes = ui.run("list-panes", "-t", "=ui-old:", "-F", "#{pane_id} #{pane_pid}").splitlines()
    (stale, stale_pid), (other, other_pid) = (l.split() for l in panes)
    for pane in (stale, other):
        ui.run("set-option", "-p", "-t", pane, "@force_role", "sidebar")
    ui.invalidate()
    respawned = []
    real_run = ui.run
    ui.run = lambda *a: (respawned.append(a[3]) if a[:2] == ("respawn-pane", "-k") else None) or real_run(*a)
    try:
        Daemon(cockpit, FakeClock()).tick()
    finally:
        shutil.rmtree(rel, ignore_errors=True)
    assert respawned == [stale]


# --- reported state (SPEC §43.2), why and title (§43.3/§43.4) -----------------
from force import report as report_mod  # noqa: E402
from force.state import Seen as dm_Seen, save_seen  # noqa: E402


def _shell(cockpit, name="sh"):
    with locked(cockpit.paths.home) as reg:
        e = reg.add(name, _uuid(), "/tmp")
        e.extra["kind"] = "shell"
    cockpit.create(e)
    assert wait_for(lambda: current_pid(cockpit, name) is not None)
    return name


def _view(env, name):
    return next(s for s in read_snapshot(env.paths.snapshot).sessions if s.name == name)


def test_report_working_on_shell(env):
    cockpit = env.make_cockpit()
    _shell(cockpit)
    d = Daemon(cockpit, FakeClock())
    report_mod.write(env.paths, "sh", "working", "building", os.getpid(), d._now_ms())
    d.tick()
    v = _view(env, "sh")
    assert v.state is State.WORKING and v.activity == "building"
    assert "reported by pid" in v.why and "working" in v.why


def test_report_dead_pid_ignored_and_deleted(env):
    cockpit = env.make_cockpit()
    _shell(cockpit)
    d = Daemon(cockpit, FakeClock())
    p = subprocess.Popen(["true"])
    p.wait()
    report_mod.write(env.paths, "sh", "working", "x", p.pid, d._now_ms())
    d.tick()
    assert _view(env, "sh").state is State.SHELL
    assert report_mod.read(env.paths, "sh") is None
    assert not report_mod.path_for(env.paths, "sh").exists()


def test_report_your_turn_until_viewed(env, monkeypatch):
    cockpit = env.make_cockpit()
    _shell(cockpit)
    d = Daemon(cockpit, FakeClock())
    report_mod.write(env.paths, "sh", "your-turn", "done", 0, d._now_ms())
    d.tick()
    assert _view(env, "sh").state is State.YOUR_TURN
    save_seen(env.paths.seen, {"sh": dm_Seen(last_seen_ms=d._now_ms() + 5000)})
    d.tick()
    assert _view(env, "sh").state is State.SHELL
    assert not report_mod.path_for(env.paths, "sh").exists()


def test_report_orphan_deleted(env):
    cockpit = env.make_cockpit()
    _shell(cockpit)
    d = Daemon(cockpit, FakeClock())
    report_mod.write(env.paths, "ghost", "working", "x", 0, d._now_ms())
    report_mod.reports_dir(env.paths).joinpath("sh.json").write_text("garbage")
    d.tick()
    assert not report_mod.path_for(env.paths, "ghost").exists()
    assert not report_mod.path_for(env.paths, "sh").exists()      # unreadable file of a live entry


def test_report_on_agent_keeps_state_and_fills_activity(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    d.tick()
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    d.tick()
    before = _view(env, "portal")
    report_mod.write(env.paths, "portal", "working", "from script", 0, d._now_ms())
    d.tick()
    v = _view(env, "portal")
    assert v.state is before.state
    assert v.activity == "from script" or before.activity is not None
    assert "claude status file, updated" in v.why and "reported" in v.why
    assert v.title is None or isinstance(v.title, str)


def test_snapshot_update_only_when_newer_force(env, monkeypatch):
    import json
    from force import updates
    monkeypatch.setattr(updates, "current_version", lambda: "1.0.0")
    cockpit = env.make_cockpit()
    d = Daemon(cockpit, FakeClock())
    # no update.json yet -> no "update" key
    d.tick()
    snap = read_snapshot(env.paths.snapshot)
    assert snap.update is None
    # an older/equal latest -> no key
    env.paths.state.mkdir(parents=True, exist_ok=True)
    (env.paths.state / "update.json").write_text(json.dumps({"latest": "1.0.0"}))
    d.tick()
    snap = read_snapshot(env.paths.snapshot)
    assert snap.update is None
    # a newer latest -> the snapshot carries "update"
    (env.paths.state / "update.json").write_text(json.dumps({"latest": "1.2.0"}))
    d.tick()
    snap = read_snapshot(env.paths.snapshot)
    assert snap.update == "1.2.0"
    # and it is written into the JSON file
    data = json.loads(env.paths.snapshot.read_text())
    assert data.get("update") == "1.2.0"


def test_title_sync_waits_for_remote_control_and_an_empty_input():
    from force.daemon import title_sync_ready
    connected = "/remote-control is active · Continue here, on your phone\n❯ /rename old\n  ⎿  Session renamed\n────\n❯ \n────"
    assert title_sync_ready(connected)
    assert title_sync_ready(connected.replace("❯ \n────", '❯ Try "refactor <filepath>"\n────'))
    assert not title_sync_ready(connected.replace("❯ \n────", "❯ half a sentence\n────")), "never mixes into a draft"
    assert not title_sync_ready("❯ \n────"), "not before Remote Control is connected"


def test_applied_rename_queues_the_phone_title(env):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    with locked(env.paths.home) as reg:
        reg.get("portal").pending_name = "newname"
    cockpit.apply_rename("portal")
    e = entry(env.paths, "newname")
    assert e.extra.get("title_sync") == "newname" and e.extra.get("title_sync_at")


def test_title_sync_types_rename_once_when_ready(env, monkeypatch):
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    ctl(cockpit, "portal", status="idle")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "idle")
    from force.lifecycle import queue_title_sync
    with locked(env.paths.home) as reg:
        queue_title_sync(reg.get("portal"), "portal")
    screen = {"text": "❯ \n"}            # not connected yet
    monkeypatch.setattr(cockpit.tmux, "capture_tail", lambda pane_id, lines=6: screen["text"])
    sent = []
    real_run = cockpit.tmux.run
    def run(*a, **k):
        if a and a[0] == "send-keys":
            sent.append(a)
            return ""
        return real_run(*a, **k)
    monkeypatch.setattr(cockpit.tmux, "run", run)
    d.tick()
    assert sent == [] and entry(env.paths, "portal").extra.get("title_sync") == "portal"
    screen["text"] = "/remote-control is active · Continue here\n────\n❯ \n────"
    d.tick()
    assert [a for a in sent if "-l" in a] == [("send-keys", "-t", sent[0][2], "-l", "/rename portal")]
    assert "title_sync" not in entry(env.paths, "portal").extra
    d.tick()
    assert len([a for a in sent if "-l" in a]) == 1, "only once"


def test_title_sync_without_remote_control_does_not_wait_for_it(env, monkeypatch):
    """SPEC §53: a session started without Remote Control never connects; its /rename goes in once idle."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "quiet")
    d = Daemon(cockpit, FakeClock())
    ctl(cockpit, "quiet", status="idle")
    assert wait_for(lambda: (current_status(cockpit, "quiet") or {}).get("status") == "idle")
    from force.lifecycle import queue_title_sync
    with locked(env.paths.home) as reg:
        e = reg.get("quiet")
        e.extra["remote_control"] = False
        queue_title_sync(e, "quiet")
    monkeypatch.setattr(cockpit.tmux, "capture_tail", lambda pane_id, lines=6: "────\n❯ \n────")
    sent = []
    real_run = cockpit.tmux.run
    def run(*a, **k):
        if a and a[0] == "send-keys":
            sent.append(a)
            return ""
        return real_run(*a, **k)
    monkeypatch.setattr(cockpit.tmux, "run", run)
    d.tick()
    assert [a[-1] for a in sent if "-l" in a] == ["/rename quiet"]
    assert "title_sync" not in entry(env.paths, "quiet").extra


def test_title_sync_ready_when_the_status_file_says_connected():
    from force.daemon import title_sync_ready
    assert title_sync_ready("long conversation…\n────\n❯ \n────", connected=True)
    assert not title_sync_ready("❯ 1. docs/ in o\n  2. other", connected=True), "an open menu is not an empty input"
    assert not title_sync_ready("────\n❯ yes, merge it\n────", connected=True), "a draft"


_TRUST_SCREEN = ("Accessing workspace:\n /tmp\n Is this a project you created or one you trust?\n ❯ No, exit\n"
                 "   Yes, I trust this folder\n")


def _decline_trust(cockpit, name, d, monkeypatch, status=1):
    """The agent exits at the trust question: its screen still shows it (SPEC §6.4)."""
    before = current_pid(cockpit, name)
    monkeypatch.setattr(cockpit.tmux, "capture_tail", lambda pane, lines=6: _TRUST_SCREEN)
    ctl(cockpit, name, exit=status)
    assert wait_for(lambda: cockpit.agent_pane(name) is not None and cockpit.agent_pane(name).dead)
    for _ in range(3):
        d.clock.advance(6)          # past the exit-reason grace, should tmux report none
        d.tick()
    return before


@pytest.mark.parametrize("status", [1, 0])      # "No, exit" exits 1, Esc 0
def test_trust_declined_deletes_a_new_session(env, monkeypatch, status):
    """Declining a new session's folder deletes the session instead of restarting it into the question again."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    monkeypatch.setattr("force.daemon.claude_mod.find_transcript", lambda claude_dir, sid: None)   # no conversation
    d = Daemon(cockpit, FakeClock())
    _decline_trust(cockpit, "portal", d, monkeypatch, status)
    assert load(env.paths.registry).get("portal") is None
    assert not cockpit.tmux.has_session("portal")
    assert all(s.name != "portal" for s in read_snapshot(env.paths.snapshot).sessions)


def test_trust_declined_keeps_a_session_with_a_conversation(env, monkeypatch):
    """A session moved to a folder Claude does not trust yet is never deleted: it stays down, not restarted."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    before = _decline_trust(cockpit, "portal", d, monkeypatch)
    assert load(env.paths.registry).get("portal") is not None
    assert current_pid(cockpit, "portal") == before and cockpit.agent_pane("portal").dead     # not restarted
    v = next(s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal")
    assert v.state is State.DOWN and v.activity == "folder not trusted"
    assert "trust-folder question was declined" in v.why


def test_trust_phrase_after_a_status_file_is_a_crash():
    """A pane that wrote a status file got past the question: the phrase on its screen is conversation text, and a
    crash restarts it as usual."""
    d, stub = _crash_stub()
    stub.tmux = SimpleNamespace(capture_tail=lambda pane, lines=6: _TRUST_SCREEN)
    stub.delete = lambda name: pytest.fail("deleted")
    stub.agent_pane = lambda name: _StubPane(dead=True, dstat=1, pid=42)
    d._seen_pid["x"] = 42
    d._step_crash()
    assert stub.restart_calls == ["x"]


def test_unread_reply_while_working(env, monkeypatch):
    """SPEC §5 unread: a working session that wrote a finding after you last looked is flagged until you open
    it."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    clk = FakeClock(start=1_800_000_000.0)
    d = Daemon(cockpit, clk)
    monkeypatch.setattr(cockpit.tmux, "clients", lambda: [])
    ctl(cockpit, "portal", status="busy")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "busy")
    reply = {"ms": None}
    monkeypatch.setattr("force.daemon.activity.scan", lambda path, max_len=60: (None, None, None, reply["ms"]))

    def view():
        return next(s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal")

    d.tick()
    assert view().state is State.WORKING and view().unread is False     # nothing written yet
    reply["ms"] = int(clk() * 1000)
    d._activity.clear()                                                   # the transcript changed
    clk.advance(1)
    d.tick()
    v = view()
    assert v.state is State.WORKING and v.unread is True
    assert "unread" in v.why
    # you open the session: cleared, and it stays cleared once you leave again
    monkeypatch.setattr(cockpit.tmux, "clients", lambda: [Client("/dev/pts/9", "portal", 100, 40, 1)])
    clk.advance(1)
    d.tick()
    assert view().unread is False
    monkeypatch.setattr(cockpit.tmux, "clients", lambda: [])
    clk.advance(1)
    d.tick()
    assert view().unread is False
    # a newer reply marks it again, unless the user turned it off (unreadReplies)
    reply["ms"] = int(clk() * 1000)
    d._activity.clear()
    clk.advance(1)
    d.tick()
    assert view().unread is True
    d.cfg = dataclasses.replace(d.cfg, unread_replies=False)
    clk.advance(1)
    d.tick()
    assert view().unread is False and "unread" not in view().why


def test_unread_forgotten_with_the_session(env, monkeypatch):
    """A session deleted and created again under its name starts without its predecessor's finding (SPEC §5)."""
    cockpit = env.make_cockpit()
    add_live(cockpit, "portal")
    clk = FakeClock(start=1_800_000_000.0)
    d = Daemon(cockpit, clk)
    monkeypatch.setattr(cockpit.tmux, "clients", lambda: [])
    monkeypatch.setattr("force.daemon.activity.scan",
                        lambda path, max_len=60: (None, None, None, int(clk() * 1000) - 500))
    ctl(cockpit, "portal", status="busy")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "busy")
    d.tick()
    old = entry(env.paths, "portal").session_id
    assert old in d._reply
    cockpit.delete("portal")
    d.tick()
    assert old not in d._reply
    monkeypatch.setattr("force.daemon.activity.scan", lambda path, max_len=60: (None, None, None, None))
    add_live(cockpit, "portal")
    ctl(cockpit, "portal", status="busy")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "busy")
    clk.advance(1)
    d.tick()
    v = next(s for s in read_snapshot(env.paths.snapshot).sessions if s.name == "portal")
    assert v.state is State.WORKING and v.unread is False


def test_opencode_reply_only_on_change():
    """An OpenCode TUI's reply time is the tick its text changed, never its first sight (SPEC §5)."""
    d = SimpleNamespace(_oc_reply={}, _now_ms=lambda: 42)
    pane = SimpleNamespace(pid=7, dead=False)

    def reply(key, p=pane):
        return Daemon._oc_reply_for(d, "oc", p, SimpleNamespace(reply=key))
    assert reply("m1:5") is None                                          # first sight
    assert reply("m1:5") is None                                          # unchanged
    assert reply("m1:9") == 42                                            # more text
    assert reply(None) is None                                            # not busy: nothing read
    assert reply("m2:1", SimpleNamespace(pid=8, dead=False)) is None      # restarted: first sight again
