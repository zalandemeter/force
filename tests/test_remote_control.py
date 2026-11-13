"""Remote Control per session (SPEC §53): the default setting, `force new`'s flags, `force remote-control`, the
daemon following `/remote-control` typed in a session, and `force list --json`."""
import dataclasses
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from force import cli, config, mcp
from force.daemon import RC_OFF_AFTER_MS, Daemon, rc_follow
from force.registry import load, locked
from force.state import State


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def _live(cockpit, name, extra=None):
    with locked(cockpit.paths.home) as reg:
        e = reg.add(name, str(uuid.uuid4()), "/tmp")
        e.extra.update(extra or {})
    cockpit.create(e)
    assert _wait(lambda: (p := cockpit.agent_pane(name)) is not None and p.pid and _status(cockpit, name)), \
        f"{name} not running"
    return name


def _ctl(cockpit, name, **kw):
    pid = cockpit.agent_pane(name).pid
    (cockpit.paths.claude_dir / "sessions" / f"{pid}.ctl").write_text(json.dumps(kw))


def _status(cockpit, name):
    pane = cockpit.agent_pane(name)
    if pane is None or not pane.pid:
        return {}
    pid = pane.pid
    try:
        return json.loads((cockpit.paths.claude_dir / "sessions" / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return {}


def _extra(env, name):
    return load(env.paths.registry).get(name).extra


# --- the setting and `force new` ------------------------------------------------------------------------------
def test_remote_control_setting_defaults_on():
    assert config.Config().remote_control is True
    assert any(s.key == "remoteControl" and s.field == "remote_control" and s.public for s in config.SETTINGS)


@pytest.mark.parametrize("default, flag, stored", [
    (True, None, None),          # on by default: nothing stored
    (False, None, False),        # the setting says off
    (False, True, None),         # --remote-control against an "off" default
    (True, False, False),        # --no-remote-control
])
def test_new_takes_the_setting_or_the_flag(env, monkeypatch, default, flag, stored):
    cockpit = env.make_cockpit()
    monkeypatch.setattr(cockpit, "create", lambda entry: None)
    monkeypatch.setattr(cli, "available_kinds", lambda c: ["claude", "shell"])
    monkeypatch.setattr(cockpit, "cfg", dataclasses.replace(cockpit.cfg, remote_control=default))
    assert cli.new_command(cockpit, "s", str(env.tmp), True, remote_control=flag) == 0
    assert _extra(env, "s").get("remote_control") is stored


@pytest.mark.parametrize("flag", [True, False])
def test_new_refuses_either_flag_for_a_shell(env, monkeypatch, capsys, flag):
    cockpit = env.make_cockpit()
    monkeypatch.setattr(cockpit, "create", lambda entry: None)
    assert cli.new_command(cockpit, "sh", str(env.tmp), True, kind="shell", remote_control=flag) == 1
    assert "Claude Code sessions only" in capsys.readouterr().err
    assert load(env.paths.registry).get("sh") is None


def test_new_shell_without_a_flag_ignores_an_off_default(env, monkeypatch):
    cockpit = env.make_cockpit()
    monkeypatch.setattr(cockpit, "create", lambda entry: None)
    monkeypatch.setattr(cockpit, "cfg", dataclasses.replace(cockpit.cfg, remote_control=False))
    assert cli.new_command(cockpit, "sh", str(env.tmp), True, kind="shell") == 0
    assert "remote_control" not in _extra(env, "sh")


@pytest.mark.parametrize("value, flag", [(True, "--remote-control"), (False, "--no-remote-control"), (None, None)])
def test_mcp_session_new_passes_the_choice(monkeypatch, value, flag):
    seen = []
    monkeypatch.setattr(mcp, "_force", lambda *argv: (seen.append(argv), (0, "ok"))[1])
    args = {"name": "s"} if value is None else {"name": "s", "remote_control": value}
    mcp.call("session_new", args)
    argv = seen[-1]
    assert ("--remote-control" in argv) == (flag == "--remote-control")
    assert ("--no-remote-control" in argv) == (flag == "--no-remote-control")


# --- `force remote-control` -------------------------------------------------------------------------------------
def test_remote_control_unknown_and_non_claude(env, capsys):
    cockpit = env.make_cockpit()
    assert cli.remote_control_command(cockpit, "nope", "toggle") == 1
    assert "no session" in capsys.readouterr().err
    with locked(env.paths.home) as reg:
        reg.add("sh", str(uuid.uuid4()), "/tmp").extra["kind"] = "shell"
    assert cli.remote_control_command(cockpit, "sh", "off") == 1
    assert "Claude Code sessions" in capsys.readouterr().err


def test_remote_control_on_a_down_session_only_stores(env, capsys):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        reg.add("down", str(uuid.uuid4()), "/tmp")
    assert cli.remote_control_command(cockpit, "down", "off") == 0
    assert capsys.readouterr().out.strip() == "down: Remote Control off (applies when it starts)"
    assert _extra(env, "down")["remote_control"] is False
    assert cli.remote_control_command(cockpit, "down", "off") == 0
    assert "already off" in capsys.readouterr().out
    assert cli.remote_control_command(cockpit, "down", "toggle") == 0
    assert "remote_control" not in _extra(env, "down"), "on is stored as absent"


def test_remote_control_restarts_an_idle_session(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    _live(cockpit, "idle1")
    _ctl(cockpit, "idle1", status="idle")
    assert _wait(lambda: _status(cockpit, "idle1").get("status") == "idle")
    restarted = []
    monkeypatch.setattr(cockpit, "restart", lambda name: restarted.append(name))
    assert cli.remote_control_command(cockpit, "idle1", "off") == 0
    assert capsys.readouterr().out.strip() == "idle1: Remote Control off (restarting)"
    assert restarted == ["idle1"]
    assert "pending_restart" not in _extra(env, "idle1")


def test_remote_control_waits_for_a_working_session(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    _live(cockpit, "busy1")
    _ctl(cockpit, "busy1", status="busy")
    assert _wait(lambda: _status(cockpit, "busy1").get("status") == "busy")
    monkeypatch.setattr(cockpit, "restart", lambda name: pytest.fail("a working session must not restart now"))
    assert cli.remote_control_command(cockpit, "busy1", "toggle") == 0
    assert capsys.readouterr().out.strip() == "busy1: Remote Control off (restarts after this turn)"
    assert _extra(env, "busy1")["pending_restart"] is True
    assert _extra(env, "busy1")["remote_control"] is False


def test_remote_control_status(env, capsys):
    cockpit = env.make_cockpit()
    _live(cockpit, "st")
    pid = cockpit.agent_pane("st").pid
    path = env.paths.claude_dir / "sessions" / f"{pid}.json"
    assert cli.remote_control_command(cockpit, "st", "status") == 0
    assert capsys.readouterr().out.strip() == "on (not connected)"
    assert _wait(lambda: path.exists())
    path.write_text(json.dumps({**json.loads(path.read_text()), "bridgeSessionId": "session_x"}))
    assert cli.remote_control_command(cockpit, "st", "status") == 0
    assert capsys.readouterr().out.strip() == "on (connected)"


def test_remote_control_is_forwarded_to_the_shown_host():
    assert "remote-control" in cli._FORWARDED


# --- following /remote-control (the daemon) ----------------------------------------------------------------------
@pytest.mark.parametrize("on, connected, seen, gone_ms, new", [
    (False, True, True, None, True),                    # turned on in the session
    (True, True, True, None, None),                     # nothing to change
    (True, False, True, RC_OFF_AFTER_MS - 1000, None),  # a short drop
    (True, False, True, RC_OFF_AFTER_MS, False),        # the panel's Disconnect
    (True, False, False, 10 * RC_OFF_AFTER_MS, None),   # never connected: left alone
    (False, False, True, 10 * RC_OFF_AFTER_MS, None),   # already off
])
def test_rc_follow(on, connected, seen, gone_ms, new):
    assert rc_follow(on, connected, seen, gone_ms) is new


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_daemon_follows_remote_control_typed_in_the_session(env, monkeypatch):
    cockpit = env.make_cockpit()
    clock = _Clock()
    d = Daemon(cockpit, clock)
    with locked(env.paths.home) as reg:
        reg.add("s", str(uuid.uuid4()), "/tmp").extra["remote_control"] = False
    live = {"connected": True}
    monkeypatch.setattr(d, "_rc_connected", lambda pi: live["connected"])
    pi = SimpleNamespace(pane_pid=4242)
    now = lambda: int(clock.t * 1000)            # noqa: E731

    e = load(env.paths.registry).get("s")
    connected, why = d._follow_rc(e, pi, now())
    assert connected and why == "remote control: turned on in the session"
    assert "remote_control" not in _extra(env, "s")

    live["connected"] = False                   # Disconnect in the panel
    e = load(env.paths.registry).get("s")
    assert d._follow_rc(e, pi, now()) == (False, None)
    clock.t += 59
    assert d._follow_rc(e, pi, now()) == (False, None), "59 s is still a drop"
    clock.t += 1
    connected, why = d._follow_rc(e, pi, now())
    assert why == "remote control: turned off in the session"
    assert _extra(env, "s")["remote_control"] is False


def test_daemon_does_not_turn_off_a_new_process_that_never_connected(env, monkeypatch):
    cockpit = env.make_cockpit()
    clock = _Clock()
    d = Daemon(cockpit, clock)
    with locked(env.paths.home) as reg:
        reg.add("s", str(uuid.uuid4()), "/tmp")
    live = {"connected": True}
    monkeypatch.setattr(d, "_rc_connected", lambda pi: live["connected"])
    e = load(env.paths.registry).get("s")
    d._follow_rc(e, SimpleNamespace(pane_pid=1), int(clock.t * 1000))       # seen connected in pid 1
    live["connected"] = False
    clock.t += 600
    assert d._follow_rc(e, SimpleNamespace(pane_pid=2), int(clock.t * 1000)) == (False, None)   # restarted
    clock.t += 600
    assert d._follow_rc(e, SimpleNamespace(pane_pid=2), int(clock.t * 1000)) == (False, None)
    assert "remote_control" not in _extra(env, "s"), "pid 2 never connected: its choice is left on"


def test_list_json_carries_the_choice_and_the_connection(env):
    cockpit = env.make_cockpit()
    _live(cockpit, "on1")
    _live(cockpit, "off1", extra={"remote_control": False})
    d = Daemon(cockpit)
    d.tick()
    by = {s["name"]: s for s in json.loads(json.dumps(cli._read_fresh_snapshot(cockpit).to_json()))["sessions"]}
    assert by["on1"]["remote_control"] is True and by["off1"]["remote_control"] is False
    assert by["on1"]["rc_connected"] is False
