"""Marked unread (SPEC §5, `force mark-unread`, menu `m`): an idle session shows as your turn again until you come
back to it, or until a turn finishes after the mark."""
import time

from force import cli, menus
from force.daemon import Daemon
from force.registry import load, locked
from force.state import Seen, State, compute_state, compute_state_why, read_snapshot

from test_daemon import FakeClock, add_live, ctl, current_status, wait_for
from test_state import NOW, cfg, entry, pane, shell_entry, status


def _marked(e, ms=NOW - 1000):
    e.extra["marked_unread_ms"] = ms
    return e


def _state(env, name):
    return next(s for s in read_snapshot(env.paths.snapshot).sessions if s.name == name).state


def _extra(env, name):
    return load(env.paths.registry).get(name).extra


def _idle(cockpit, name):
    add_live(cockpit, name)
    assert wait_for(lambda: (current_status(cockpit, name) or {}).get("status") == "idle")


# --- the rule -----------------------------------------------------------------------------------------------------
def test_a_marked_idle_session_is_your_turn():
    seen = Seen(turn_done_ms=1, last_seen_ms=NOW)          # opened after its turn: idle without the mark
    assert compute_state(entry(), pane(), status("idle"), seen, NOW, cfg()) is State.IDLE
    st, why = compute_state_why(_marked(entry()), pane(), status("idle"), seen, NOW, cfg())
    assert st is State.YOUR_TURN and "marked unread" in why
    stale = status("idle", updated=NOW - 13 * 3600 * 1000)
    assert compute_state(_marked(entry()), pane(), stale, seen, NOW, cfg()) is State.YOUR_TURN


def test_a_new_turn_wins_over_the_mark():
    for st, expected in (("busy", State.WORKING), ("waiting", State.NEEDS_YOU)):
        assert compute_state(_marked(entry()), pane(), status(st), Seen(), NOW, cfg()) is expected
    assert compute_state(_marked(entry()), None, None, Seen(), NOW, cfg()) is State.DOWN
    assert compute_state(_marked(entry(archived=True)), pane(), None, Seen(), NOW, cfg()) is State.ARCHIVED


def test_a_marked_shell_is_your_turn_until_a_program_runs():
    assert compute_state(_marked(shell_entry()), pane(), None, Seen(), NOW, cfg()) is State.YOUR_TURN
    assert compute_state(_marked(shell_entry()), pane(), status("busy"), Seen(), NOW, cfg()) is State.WORKING


def test_a_bad_mark_is_no_mark():
    for bad in (0, -5, True, "123", None):
        e = entry()
        e.extra["marked_unread_ms"] = bad
        assert compute_state(e, pane(), status("idle"), Seen(), NOW, cfg()) is State.IDLE


# --- the daemon ----------------------------------------------------------------------------------------------------
def test_mark_while_viewed_lasts_until_you_leave_and_come_back(env, monkeypatch):
    cockpit = env.make_cockpit()
    _idle(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    viewed = {"portal"}
    monkeypatch.setattr(d, "_viewed_sessions", lambda: set(viewed))
    d.tick()
    assert _state(env, "portal") is State.IDLE
    assert cli.mark_unread_command(cockpit, "portal") == 0
    for _ in range(2):                                      # still looking at it: stays your turn
        d.tick()
        assert _state(env, "portal") is State.YOUR_TURN
        assert "marked_unread_away" not in _extra(env, "portal")
    viewed.clear()                                          # you left it
    d.tick()
    assert _state(env, "portal") is State.YOUR_TURN
    assert _extra(env, "portal")["marked_unread_away"] is True
    viewed.add("portal")                                    # and came back: the mark is gone
    d.tick()
    assert _state(env, "portal") is State.IDLE
    assert "marked_unread_ms" not in _extra(env, "portal")
    assert "marked_unread_away" not in _extra(env, "portal")


def test_a_turn_finished_after_the_mark_ends_it(env, monkeypatch):
    cockpit = env.make_cockpit()
    _idle(cockpit, "portal")
    clk = FakeClock()
    d = Daemon(cockpit, clk)
    monkeypatch.setattr(d, "_viewed_sessions", lambda: set())
    d.tick()
    with locked(env.paths.home) as reg:
        reg.get("portal").extra["marked_unread_ms"] = int(clk() * 1000)
    d.tick()
    assert _state(env, "portal") is State.YOUR_TURN
    ctl(cockpit, "portal", status="busy")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "busy")
    clk.advance(5)
    d.tick()
    assert _state(env, "portal") is State.WORKING
    assert "marked_unread_ms" in _extra(env, "portal")
    ctl(cockpit, "portal", status="idle")
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("status") == "idle")
    clk.advance(5)
    d.tick()
    assert "marked_unread_ms" not in _extra(env, "portal")
    assert _state(env, "portal") is State.YOUR_TURN            # the usual rule: finished, not viewed since


def test_marking_sends_no_finished_notice(env, monkeypatch):
    cockpit = env.make_cockpit()
    _idle(cockpit, "portal")
    d = Daemon(cockpit, FakeClock())
    monkeypatch.setattr(d, "_viewed_sessions", lambda: set())
    d.tick()
    events = []
    orig = d._notices.step
    monkeypatch.setattr(d._notices, "step", lambda *a, **k: (events.extend(r := orig(*a, **k)) or r))
    assert cli.mark_unread_command(cockpit, "portal") == 0
    d.tick()
    assert _state(env, "portal") is State.YOUR_TURN
    assert events == []


# --- the command ---------------------------------------------------------------------------------------------------
def test_mark_unread_command_refusals(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        reg.add("portal", "00000000-0000-0000-0000-000000000001", "/tmp")
    assert cli.mark_unread_command(cockpit, "nope") == 1
    for st, word in ((State.WORKING, "working"), (State.NEEDS_YOU, "needs you"), (State.DOWN, "down"),
                     (None, "unknown")):
        monkeypatch.setattr(cli, "_current_state_for", lambda c, n, now_ms=None, st=st: st)
        assert cli.mark_unread_command(cockpit, "portal") == 1
        assert word in capsys.readouterr().err
        assert "marked_unread_ms" not in _extra(env, "portal")
    monkeypatch.setattr(cli, "_current_state_for", lambda c, n, now_ms=None: State.YOUR_TURN)
    assert cli.mark_unread_command(cockpit, "portal") == 0
    assert capsys.readouterr().out == "portal: already your turn\n"
    for st in (State.IDLE, State.STALE, State.SHELL):
        monkeypatch.setattr(cli, "_current_state_for", lambda c, n, now_ms=None, st=st: st)
        before = int(time.time() * 1000)
        assert cli.mark_unread_command(cockpit, "portal") == 0
        assert capsys.readouterr().out == "portal: marked unread\n"
        assert _extra(env, "portal")["marked_unread_ms"] >= before


def test_mark_unread_is_forwarded_and_in_the_menu():
    assert "mark-unread" in cli._FORWARDED
    assert ("m", "Mark unread", "unread", False) in menus.ITEMS
    _s, classic = menus.classic()
    _s, split = menus.split()
    assert "mark-unread '#{session_name}'" in classic["unread"]
    assert "ui-key.sh" in split["unread"] and split["unread"].endswith(' unread"')
