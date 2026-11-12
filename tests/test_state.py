import json
import uuid

import pytest

from force.claude import AgentStatus
from force.config import Config
from force.registry import Entry
from force.state import (PaneInfo, Seen, SessionView, Snapshot, State,
                       color, compute_state, format_age, glyph, load_seen,
                       read_snapshot, save_seen, unread, write_snapshot)


def entry(name="portal", archived=False):
    return Entry(name=name, session_id=str(uuid.uuid4()), cwd="/home",
                 created_at="now", order=1, archived=archived)


def shell_entry(name="sh", archived=False):
    e = entry(name=name, archived=archived)
    e.extra["kind"] = "shell"
    return e


def pane(alive=True, session=True, pid=100, start=0, **kw):
    return PaneInfo(session_exists=session, pane_alive=alive, pane_pid=pid,
                    pane_started_ms=start, dead_status=kw.get("dead_status"),
                    dead_signal=kw.get("dead_signal"))


def status(status="idle", updated=0, pid=100):
    return AgentStatus(pid=pid, session_id="s", name="portal", name_source="user",
                        status=status, status_updated_ms=updated, waiting_for=None, cwd="/")


def cfg(**kw):
    d = dict(stale_hours=12, upgrade_idle_minutes=10, auto_upgrade=True,
             sidebar_width=28, crash_restart_max=3, crash_window_minutes=10,
             start_grace_seconds=30)
    d.update(kw)
    return Config(**d)


NOW = 1_000_000


@pytest.mark.parametrize("entry_,pane_,status_,seen,expected", [
    # ARCHIVED
    (entry(archived=True), pane(), None, Seen(), State.ARCHIVED),
    # DOWN: no tmux session / pane None
    (entry(), None, None, Seen(), State.DOWN),
    # DOWN: pane dead
    (entry(), pane(alive=False), None, Seen(), State.DOWN),
    # DOWN: pane exists but session missing
    (entry(), pane(session=False), None, Seen(), State.DOWN),
    # STARTING: pane alive, no status, young
    (entry(), pane(start=NOW - 5_000), None, Seen(), State.STARTING),
    # DOWN: pane alive, no status, old
    (entry(), pane(start=NOW - 40_000), None, Seen(), State.DOWN),
    # DOWN: no status and pane_started_ms None
    (entry(), pane(start=None), None, Seen(), State.DOWN),
    # NEEDS_YOU
    (entry(), pane(), status("waiting"), Seen(), State.NEEDS_YOU),
    # WORKING
    (entry(), pane(), status("busy"), Seen(), State.WORKING),
    # YOUR_TURN
    (entry(), pane(), status("idle"), Seen(turn_done_ms=5, last_seen_ms=1), State.YOUR_TURN),
    # IDLE (turn_done <= last_seen)
    (entry(), pane(), status("idle"), Seen(turn_done_ms=1, last_seen_ms=5), State.IDLE),
    # STALE: idle, status updated long ago
    (entry(), pane(), status("idle", updated=NOW - 13 * 3600 * 1000), Seen(), State.STALE),
    # UNKNOWN status string
    (entry(), pane(), status("weird"), Seen(), State.UNKNOWN),
])
def test_compute_state(entry_, pane_, status_, seen, expected):
    assert compute_state(entry_, pane_, status_, seen, NOW, cfg()) is expected


def test_status_pid_mismatch_treated_as_no_status():
    # status pid differs from pane pid and pane is young -> STARTING (no live status)
    st = status("waiting", pid=999)
    p = pane(pid=100, start=NOW - 5_000)
    assert compute_state(entry(), p, st, Seen(), NOW, cfg()) is State.STARTING


def test_stale_uses_cfg_stale_hours():
    st = status("idle", updated=NOW - 2 * 3600 * 1000)
    # default 12h -> idle; custom 1h -> stale
    assert compute_state(entry(), pane(), st, Seen(), NOW, cfg()) is State.IDLE
    assert compute_state(entry(), pane(), st, Seen(), NOW, cfg(stale_hours=1)) is State.STALE


def test_starting_grace_uses_cfg():
    p = pane(start=NOW - 50_000)
    # default 30s -> DOWN; custom 60s -> STARTING
    assert compute_state(entry(), p, None, Seen(), NOW, cfg()) is State.DOWN
    assert compute_state(entry(), p, None, Seen(), NOW, cfg(start_grace_seconds=60)) is State.STARTING


def test_shell_compute_state():
    # archived -> ARCHIVED
    assert compute_state(shell_entry(archived=True), pane(), None, Seen(), NOW, cfg()) is State.ARCHIVED
    # no tmux session / pane None -> DOWN
    assert compute_state(shell_entry(), None, None, Seen(), NOW, cfg()) is State.DOWN
    # dead pane -> DOWN
    assert compute_state(shell_entry(), pane(alive=False), None, Seen(), NOW, cfg()) is State.DOWN
    # session missing -> DOWN
    assert compute_state(shell_entry(), pane(session=False), None, Seen(), NOW, cfg()) is State.DOWN
    # alive pane -> SHELL (no status file, no grace period)
    assert compute_state(shell_entry(), pane(), None, Seen(), NOW, cfg()) is State.SHELL
    assert compute_state(shell_entry(), pane(start=NOW - 1000), None, Seen(), NOW, cfg()) is State.SHELL
    # a claude entry is untouched: alive, no status, young -> STARTING (not SHELL)
    assert compute_state(entry(), pane(start=NOW - 5_000), None, Seen(), NOW, cfg()) is State.STARTING


def test_shell_glyph_color():
    assert glyph(State.SHELL, 0) == "$"
    assert color(State.SHELL) == "colour245"


def test_glyph():
    assert glyph(State.ARCHIVED, 0) == ""
    assert glyph(State.NEEDS_YOU, 0) == "●"
    assert glyph(State.DOWN, 0) == "✖"
    assert glyph(State.IDLE, 0) == "○"
    assert glyph(State.STALE, 0) == "◌"
    assert glyph(State.YOUR_TURN, 0) == "●"
    assert glyph(State.UNKNOWN, 0) == "?"
    assert glyph(State.STARTING, 0) == "…"
    # spinner frame cycles (frame = now_ms // 100 % 10)
    assert glyph(State.WORKING, 0) == "⠋"
    assert glyph(State.WORKING, 100) == "⠙"
    assert glyph(State.WORKING, 300) == "⠸"
    assert glyph(State.WORKING, 10000) == "⠋"


def test_color():
    assert color(State.NEEDS_YOU) == "red"
    assert color(State.DOWN) == "red"
    assert color(State.WORKING) == "yellow"
    assert color(State.YOUR_TURN) == "green"
    assert color(State.IDLE) == "colour245"
    for s in (State.STALE, State.STARTING, State.UNKNOWN, State.ARCHIVED):
        assert color(s) == "colour240"


def test_format_age():
    assert format_age(None, NOW) == ""
    assert format_age(NOW + 1000, NOW) == "now"   # negative
    assert format_age(NOW - 0, NOW) == "now"
    assert format_age(NOW - 5_000, NOW) == "now"
    assert format_age(NOW - 45_000, NOW) == "45s"
    assert format_age(NOW - 12 * 60_000, NOW) == "12m"
    assert format_age(NOW - 3 * 3600_000, NOW) == "3h"
    assert format_age(NOW - 2 * 86400_000, NOW) == "2d"


def test_save_seen_unchanged_not_rewritten(tmp_path):
    p = tmp_path / "seen.json"
    seen = {"portal": Seen(last_seen_ms=5, turn_done_ms=9)}
    save_seen(p, seen)
    ns = p.stat().st_mtime_ns
    save_seen(p, seen)
    assert p.stat().st_mtime_ns == ns


def test_write_snapshot_unchanged_not_rewritten(tmp_path):
    p = tmp_path / "snapshot.json"
    sv = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                    "/home", "s", 10, None, False)
    write_snapshot(p, Snapshot(1, "2.1.2", [sv]))
    ns = p.stat().st_mtime_ns
    # same content, different generated_ms only -> not rewritten
    write_snapshot(p, Snapshot(999, "2.1.2", [sv]))
    assert p.stat().st_mtime_ns == ns


def test_write_snapshot_rewritten_after_2s_unchanged(tmp_path, monkeypatch):
    import os
    import time as _time
    fake = {"t": 1000.0}
    monkeypatch.setattr(_time, "time", lambda: fake["t"])
    replacements = []
    orig = os.replace
    monkeypatch.setattr(os, "replace",
                      lambda a, b, orig=orig: (replacements.append(str(b)) or orig(a, b)))
    p = tmp_path / "snapshot.json"
    sv = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                    "/home", "s", 10, None, False)
    write_snapshot(p, Snapshot(1, "2.1.2", [sv]))
    assert len(replacements) == 1
    # same content, under 2 s -> no rewrite
    write_snapshot(p, Snapshot(2, "2.1.2", [sv]))
    assert len(replacements) == 1
    # same content, > 2 s -> rewritten so readers' dead-daemon check keeps working
    fake["t"] += 2.1
    write_snapshot(p, Snapshot(3, "2.1.2", [sv]))
    assert len(replacements) == 2


def test_writers_drop_fsync(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fsync should not be called")
    monkeypatch.setattr("os.fsync", boom)
    save_seen(tmp_path / "seen.json", {"a": Seen(last_seen_ms=1, turn_done_ms=2)})
    sv = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                    "/home", "s", 10, None, False)
    write_snapshot(tmp_path / "snapshot.json", Snapshot(1, "2.1.2", [sv]))


def test_seen_roundtrip(tmp_path):
    seen = {"portal": Seen(last_seen_ms=5, turn_done_ms=9)}
    p = tmp_path / "seen.json"
    save_seen(p, seen)
    assert json.loads(p.read_text()) == {"portal": {"last_seen_ms": 5, "turn_done_ms": 9}}
    assert not (tmp_path / "seen.json.tmp").exists()
    loaded = load_seen(p)
    assert loaded["portal"].last_seen_ms == 5
    assert loaded["portal"].turn_done_ms == 9


def test_load_seen_missing(tmp_path):
    assert load_seen(tmp_path / "nope.json") == {}


def test_load_seen_corrupt_warns(tmp_path, capsys):
    p = tmp_path / "seen.json"
    p.write_text("{bad")
    assert load_seen(p) == {}
    assert "warning" in capsys.readouterr().err


def test_snapshot_to_from_json_roundtrip(tmp_path):
    sv = SessionView(name="portal", index=1, state=State.NEEDS_YOU, since_ms=5,
                     version="2.1.1", outdated=True, archived=False, order=1,
                     cwd="/home", session_id="s", pid=10, waiting_for="input", viewed=False)
    snap = Snapshot(generated_ms=123, latest_version="2.1.2", sessions=[sv])
    d = snap.to_json()
    assert d["counts"] == {"needs_you": 1, "working": 0, "your_turn": 0,
                           "idle": 0, "stale": 0, "down": 0}
    assert d["sessions"][0]["state"] == "NEEDS_YOU"
    loaded = Snapshot.from_json(d)
    assert loaded.generated_ms == 123
    assert loaded.latest_version == "2.1.2"
    assert loaded.sessions[0].name == "portal"
    assert loaded.sessions[0].state is State.NEEDS_YOU
    assert loaded.sessions[0].since_ms == 5


def test_snapshot_counts_live_only():
    def one(name, state, archived=False):
        return SessionView(name, None, state, 0, None, False, archived, 0, "/",
                          "s", None, None, False)
    snap = Snapshot(0, None, [
        one("n", State.NEEDS_YOU),
        one("w", State.WORKING),
        one("y", State.YOUR_TURN),
        one("i", State.IDLE),
        one("s", State.STALE),
        one("d", State.DOWN),
        one("a", State.ARCHIVED, archived=True),
        one("st", State.STARTING),
        one("u", State.UNKNOWN),
    ])
    c = snap.counts()
    assert c == {"needs_you": 1, "working": 1, "your_turn": 1,
                 "idle": 1, "stale": 1, "down": 1}
    # archiving a working session removes it from being counted
    snap2 = Snapshot(0, None, [one("w2", State.WORKING, archived=True)])
    assert snap2.counts()["working"] == 0


def test_shell_not_counted():
    def one(name, state):
        return SessionView(name, None, state, 0, None, False, False, 0, "/", "s",
                       None, None, False)
    snap = Snapshot(0, None, [one("sh", State.SHELL), one("sh2", State.SHELL)])
    assert snap.counts() == {"needs_you": 0, "working": 0, "your_turn": 0,
                           "idle": 0, "stale": 0, "down": 0}


def test_write_read_snapshot(tmp_path):
    sv = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                     "/home", "s", 10, None, False)
    p = tmp_path / "snapshot.json"
    write_snapshot(p, Snapshot(1, "2.1.2", [sv]))
    assert not (tmp_path / "snapshot.json.tmp").exists()
    snap = read_snapshot(p)
    assert snap.sessions[0].name == "portal"
    assert snap.sessions[0].state is State.WORKING


def test_read_snapshot_missing_and_corrupt(tmp_path):
    assert read_snapshot(tmp_path / "nope.json") is None
    p = tmp_path / "snapshot.json"
    p.write_text("{bad")
    assert read_snapshot(p) is None


def test_session_view_activity_field_last_and_optional():
    # positional construction (13 args) still works; activity defaults to None
    sv = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                    "/home", "s", 10, None, False)
    assert sv.activity is None
    # keyword construction sets it
    sv2 = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                     "/home", "s", 10, None, False, activity="running pytest")
    assert sv2.activity == "running pytest"


def test_session_view_mode_field_last_and_optional():
    # positional construction still works: mode defaults to None
    sv = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                    "/home", "s", 10, None, False)
    assert sv.mode is None
    # keyword construction sets it
    sv2 = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                     "/home", "s", 10, None, False, activity=None, mode="plan")
    assert sv2.mode == "plan"


def test_session_view_model_effort_fields_last_and_optional():
    # positional construction still works: model/effort default to None
    sv = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                    "/home", "s", 10, None, False)
    assert sv.model is None and sv.effort is None
    sv2 = SessionView("portal", 1, State.WORKING, 0, "2.1.1", True, False, 1,
                     "/home", "s", 10, None, False, activity=None, mode="plan",
                     model="opus 5.5", effort="high")
    assert sv2.model == "opus 5.5" and sv2.effort == "high"


def test_session_view_model_effort_json_roundtrip():
    sv = SessionView(name="portal", index=1, state=State.WORKING, since_ms=5,
                   version="2.1.1", outdated=True, archived=False, order=1,
                   cwd="/home", session_id="s", pid=10, waiting_for=None, viewed=False,
                   mode="plan", model="opus 5.5", effort="high")
    d = Snapshot(1, "2.1.2", [sv]).to_json()
    assert d["sessions"][0]["model"] == "opus 5.5"
    assert d["sessions"][0]["effort"] == "high"
    loaded = Snapshot.from_json(d)
    assert loaded.sessions[0].model == "opus 5.5"
    assert loaded.sessions[0].effort == "high"
    # missing in JSON -> None
    d2 = json.loads(json.dumps(d))
    del d2["sessions"][0]["model"]
    del d2["sessions"][0]["effort"]
    loaded2 = Snapshot.from_json(d2)
    assert loaded2.sessions[0].model is None
    assert loaded2.sessions[0].effort is None


def test_session_view_mode_json_roundtrip():
    sv = SessionView(name="portal", index=1, state=State.WORKING, since_ms=5,
                   version="2.1.1", outdated=True, archived=False, order=1,
                   cwd="/home", session_id="s", pid=10, waiting_for=None, viewed=False,
                   mode="bypass")
    snap = Snapshot(1, "2.1.2", [sv])
    d = snap.to_json()
    assert d["sessions"][0]["mode"] == "bypass"
    loaded = Snapshot.from_json(d)
    assert loaded.sessions[0].mode == "bypass"
    # missing in JSON -> None
    d2 = json.loads(json.dumps(d))
    del d2["sessions"][0]["mode"]
    loaded2 = Snapshot.from_json(d2)
    assert loaded2.sessions[0].mode is None


def test_session_view_activity_json_roundtrip():
    sv = SessionView(name="portal", index=1, state=State.WORKING, since_ms=5,
                   version="2.1.1", outdated=True, archived=False, order=1,
                   cwd="/home", session_id="s", pid=10, waiting_for=None, viewed=False,
                   activity="running pytest")
    snap = Snapshot(1, "2.1.2", [sv])
    d = snap.to_json()
    assert d["sessions"][0]["activity"] == "running pytest"
    loaded = Snapshot.from_json(d)
    assert loaded.sessions[0].activity == "running pytest"
    # missing in JSON -> None
    d2 = json.loads(json.dumps(d))
    del d2["sessions"][0]["activity"]
    assert "activity" not in d2["sessions"][0]
    loaded2 = Snapshot.from_json(d2)
    assert loaded2.sessions[0].activity is None


# --- compute_state_why (SPEC §43.3) and clean_title (§43.4) -------------------
from force.state import clean_title, compute_state_why  # noqa: E402


def _why(entry_, pane_, status_, seen=None, c=None, now=NOW):
    return compute_state_why(entry_, pane_, status_, seen or Seen(), now, c or cfg())


def test_why_idle_yourturn_starting_down():
    st, why = _why(entry(), pane(), status("idle", updated=NOW - 1000))
    assert st is State.IDLE and why == "status: idle"
    st, why = _why(entry(), pane(), status("idle", updated=NOW), Seen(last_seen_ms=1, turn_done_ms=5))
    assert st is State.YOUR_TURN and "not viewed" in why
    st, why = _why(entry(), pane(start=NOW - 5_000), None)
    assert st is State.STARTING and why == "starting: 5 s of 30 s grace"
    st, why = _why(entry(), pane(start=NOW - 42_000), None)
    assert st is State.DOWN and why == "no status for 42 s after start (grace 30 s)"
    st, why = _why(entry(), pane(alive=False, dead_status=3), None)
    assert st is State.DOWN and why == "agent pane dead (exit 3)"
    st, why = _why(entry(), pane(alive=False, dead_signal=9), None)
    assert why == "agent pane dead (signal 9)"
    assert _why(entry(), None, None)[1] == "no tmux session"
    assert _why(entry(archived=True), pane(), None) == (State.ARCHIVED, "archived")
    assert _why(shell_entry(), pane(), None) == (State.SHELL, "shell: pane alive")
    assert _why(shell_entry(), None, None) == (State.DOWN, "shell: no live pane")
    assert _why(entry(), pane(), status("busy"))[1] == "status: busy"
    assert _why(entry(), pane(), status("weird"))[1] == "status: 'weird' unknown"
    assert _why(entry(), pane(), status("waiting"))[1] == "status: waiting"


def test_why_stale_and_waiting_for():
    st, why = _why(entry(), pane(), status("idle", updated=0), now=30 * 3600 * 1000, c=cfg(stale_hours=24))
    assert st is State.STALE and why == "status: idle, unchanged for 30 h (stale after 24 h)"
    s = status("waiting")
    s = AgentStatus(**{**s.__dict__, "waiting_for": "permission"})
    assert _why(entry(), pane(), s)[1] == "status: waiting (permission)"


def test_why_pid_mismatch_prefix():
    st, why = _why(entry(), pane(pid=200, start=NOW - 5_000), status(pid=100))
    assert st is State.STARTING
    assert why.startswith("status file is from pid 100, pane runs 200: ignored")
    assert why.endswith("starting: 5 s of 30 s grace")


@pytest.mark.parametrize("raw,name,host,expected", [
    ("✳ force", "force", "devbox", None),
    ("⠂ Fix login bug", "force", "devbox", "Fix login bug"),
    ("OC | Refactor parser", "x", "devbox", "Refactor parser"),
    ("devbox", "x", "devbox", None),
    ("", "x", "devbox", None),
    (None, "x", "devbox", None),
    ("bash", "x", "devbox", None),
    ("✳ ", "x", "devbox", None),
    ("x" * 80, "y", "devbox", "x" * 60),
    # tmux's default title, the full host name (macOS: name.local): not a title
    ("Zalans-MacBook-Pro.local", "x", "Zalans-MacBook-Pro", None),
    ("devbox.lan", "x", "devbox", None),
    ("devbox.local build", "x", "devbox", "devbox.local build"),
    ("devbox2.local", "x", "devbox", "devbox2.local"),
])
def test_clean_title(raw, name, host, expected):
    assert clean_title(raw, name, host) == expected


def test_snapshot_why_title_roundtrip():
    sv = SessionView(name="a", index=1, state=State.IDLE, since_ms=1, version=None, outdated=False,
                     archived=False, order=1, cwd="/", session_id="s", pid=1, waiting_for=None,
                     viewed=False, why="status: idle", title="Fix it")
    d = Snapshot(generated_ms=1, latest_version=None, sessions=[sv]).to_json()
    assert d["sessions"][0]["why"] == "status: idle" and d["sessions"][0]["title"] == "Fix it"
    back = Snapshot.from_json(json.loads(json.dumps(d))).sessions[0]
    assert back.why == "status: idle" and back.title == "Fix it"
    del d["sessions"][0]["why"], d["sessions"][0]["title"]
    old = Snapshot.from_json(d).sessions[0]
    assert old.why is None and old.title is None


@pytest.mark.parametrize("state, reply, last_seen, viewed, expected", [
    (State.WORKING, 2000, 1000, False, True),       # wrote after you last looked
    (State.WORKING, 1000, 1000, False, False),      # not after
    (State.WORKING, 500, 1000, False, False),
    (State.WORKING, None, 0, False, False),         # never wrote
    (State.WORKING, 2000, 1000, True, False),       # on screen now
    (State.YOUR_TURN, 2000, 1000, False, False),    # only working sessions
    (State.NEEDS_YOU, 2000, 1000, False, False),
    (State.IDLE, 2000, 1000, False, False),
])
def test_unread(state, reply, last_seen, viewed, expected):
    assert unread(state, reply, Seen(last_seen_ms=last_seen), viewed) is expected


def test_snapshot_unread_roundtrip():
    sv = SessionView(name="portal", index=1, state=State.WORKING, since_ms=5, version=None, outdated=False,
                     archived=False, order=1, cwd="/home", session_id="s", pid=10, waiting_for=None,
                     viewed=False, unread=True)
    d = Snapshot(generated_ms=1, latest_version=None, sessions=[sv]).to_json()
    assert d["sessions"][0]["unread"] is True
    assert Snapshot.from_json(d).sessions[0].unread is True
    del d["sessions"][0]["unread"]                  # a snapshot from an older daemon
    assert Snapshot.from_json(d).sessions[0].unread is False
