"""`force api` (SPEC §54): the protocol, the view model, notices, run, and live terminal views on the test's own
tmux server (never the live sockets)."""
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from force import api, notices, remote as remote_mod
from force.state import SessionView, Snapshot, State, write_snapshot
from api_client import ApiClient


def _snap(paths_or_file, sessions, now_ms=None, **kw):
    now_ms = now_ms or int(time.time() * 1000)
    path = paths_or_file.snapshot if hasattr(paths_or_file, "snapshot") else paths_or_file
    path.parent.mkdir(parents=True, exist_ok=True)
    write_snapshot(path, Snapshot(generated_ms=now_ms, latest_version=None, sessions=sessions, **kw))


def _view(name, state=State.IDLE, **kw):
    return SessionView(name=name, index=1, state=state, since_ms=None, version=None, outdated=False,
                       archived=kw.pop("archived", False), order=0, cwd="/tmp", session_id="s-" + name, pid=None,
                       waiting_for=None, viewed=kw.pop("viewed", False), **kw)


@pytest.fixture
def client(env):
    c = ApiClient(env.make_cockpit())
    assert c.call("hello", protocol=1)["ok"]["protocol"] == 1
    yield c
    c.close()


# --- protocol ---------------------------------------------------------------------------------------------------
def test_hello_first_and_protocol(env):
    c = ApiClient(env.make_cockpit())
    assert c.call("subscribe", to=["state"])["error"]["code"] == "bad_request", "hello first"
    ok = c.call("hello", protocol=1, client="test/1")["ok"]
    assert ok["features"] == ["state", "notices", "run", "terminal.cells", "terminal.vt.app"]
    assert ok["host"] == api.short_host()
    assert c.call("nope")["error"]["code"] == "unknown_op"
    c.close()
    c2 = ApiClient(env.make_cockpit())
    assert c2.call("hello", protocol=99)["error"]["code"] == "protocol"
    c2.close()


# --- state ------------------------------------------------------------------------------------------------------
def test_state_has_every_host(env, client):
    _snap(env.paths, [_view("api", State.NEEDS_YOU), _view("old", archived=True), _view("doc", unread=True,
                                                                                         state=State.WORKING)],
          limits=[{"provider": "claude"}])
    remote_mod.save(env.paths, [remote_mod.Remote("devbox", "devbox")])
    d = remote_mod.remote_dir(env.paths, "devbox")
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({"state": "connected", "version": "1.5.2-x"}))
    _snap(d / "snapshot.json", [_view("web", State.YOUR_TURN)])
    assert client.call("subscribe", to=["state"])["ok"]["subscribed"] == ["state"]
    st = client.event("state")
    here = api.short_host()
    assert [(h["name"], h["local"], h["status"]) for h in st["hosts"]] == [(here, True, "connected"),
                                                                            ("devbox", False, "connected")]
    assert st["hosts"][1]["version"] == "1.5.2"
    by = {s["address"]: s for s in st["sessions"]}
    assert set(by) == {f"{here}:api", f"{here}:old", f"{here}:doc", "devbox:web"}
    assert by[f"{here}:api"]["state"] == "needs-you" and by["devbox:web"]["state"] == "your-turn"
    assert by[f"{here}:old"]["archived"] is True
    assert st["attention"] == {"needs_you": 1, "unread": 1}, "archived sessions do not count"
    assert st["limits"] == [{"provider": "claude"}]


def test_state_only_on_change_and_stale_daemon(env, client):
    _snap(env.paths, [_view("a")], now_ms=int(time.time() * 1000) - 60_000)
    client.call("subscribe", to=["state"])
    st = client.event("state")
    assert st["hosts"][0]["status"] == "daemon_not_running" and st["sessions"] == []
    with pytest.raises(TimeoutError):
        client.event("state", timeout=0.8)          # nothing changed: no second event
    _snap(env.paths, [_view("a", State.WORKING)])
    assert client.event("state")["sessions"][0]["state"] == "working"


def test_a_remote_not_connected_has_no_sessions(env):
    remote_mod.save(env.paths, [remote_mod.Remote("lab", "lab")])
    d = remote_mod.remote_dir(env.paths, "lab")
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({"state": "offline"}))
    _snap(d / "snapshot.json", [_view("x")])
    doc, views = api.view_model(env.paths)
    assert doc["hosts"][1]["status"] == "offline" and views["lab"] == []


# --- notices ----------------------------------------------------------------------------------------------------
def test_notices_follow_the_daemons_rule(env, client):
    _snap(env.paths, [_view("a", State.WORKING)])
    client.call("subscribe", to=["notices"])
    time.sleep(0.6)                                  # first sight: no notice
    _snap(env.paths, [_view("a", State.NEEDS_YOU, activity="Bash: rm -rf build")])
    n = client.event("notice")
    assert (n["kind"], n["session"], n["state"], n["message"]) == ("needs-you", "a", "needs-you",
                                                                   "a: Bash: rm -rf build")
    _snap(env.paths, [_view("a", State.YOUR_TURN)])
    assert client.event("notice")["kind"] == "finished"


def test_transitions_down_waits_and_skips_force_kills():
    tr = notices.Transitions(down_after=10)
    v = lambda st, note=None: SimpleNamespace(name="a", state=st, note=note, archived=False)
    assert tr.step([v(State.WORKING)], 0) == []
    assert tr.step([v(State.DOWN)], 1) == []
    assert tr.step([v(State.DOWN)], 5) == []
    assert [e for _, e in tr.step([v(State.DOWN)], 11)] == ["down"]
    tr.step([v(State.WORKING)], 12)
    tr.step([v(State.DOWN)], 13)
    assert tr.step([v(State.DOWN)], 30, killed=lambda n: True) == [], "Force stopped it: no notice"


# --- run --------------------------------------------------------------------------------------------------------
def test_run_allow_list(env):
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
    c = ApiClient(env.make_cockpit(), run=fake)
    c.call("hello", protocol=1)
    r = c.call("run", argv=["prompt", "devbox:api", "hi"])["ok"]
    assert r == {"code": 0, "stdout": "ok\n", "stderr": ""} and calls[0][1:] == ["prompt", "devbox:api", "hi"]
    for bad in (["uninstall"], ["update"], ["host", "add", "x"], ["install-to", "x"]):
        assert c.call("run", argv=bad)["error"]["code"] == "not_allowed"
    assert c.call("run", argv="prompt x")["error"]["code"] == "bad_request"
    c.close()


# --- SGR and quoting --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("line, runs", [
    ("plain", [{"text": "plain", "fg": None, "bg": None}]),
    ("\x1b[1;31mred\x1b[0m ok", [{"text": "red", "fg": "#cd0000", "bg": None, "bold": True},
                                  {"text": " ok", "fg": None, "bg": None}]),
    ("\x1b[38;5;208mo\x1b[48;2;1;2;3mx\x1b[39;49m", [{"text": "o", "fg": "#ff8700", "bg": None},
                                                    {"text": "x", "fg": "#ff8700", "bg": "#010203"}]),
    ("\x1b[7mrev\x1b[27m\x1b[4mu\x1b[24m", [{"text": "rev", "fg": None, "bg": None, "reverse": True},
                                            {"text": "u", "fg": None, "bg": None, "underline": True}]),
    ("\x1b[38;5;240ma\x1b[38;5;240mb", [{"text": "ab", "fg": "#585858", "bg": None}]),
    ("", []),
])
def test_parse_line(line, runs):
    assert api.parse_line(line) == runs


def test_tmux_quote_round_trips_through_tmux(env):
    env.tmux.run("new-session", "-d", "-s", "q", "-x", "40", "-y", "5", "cat")
    tricky = 'a$HOME"b\'c\\d\te;f\x1bg #{x} ~'
    p = subprocess.run(["tmux", "-L", env.socket, "-C", "attach", "-t", "=q"],
                       input=f"set-buffer -b t {api.tmux_quote(tricky)}\ndetach-client\n", capture_output=True,
                       text=True, timeout=10)
    assert p.returncode == 0
    assert env.tmux.run("show-buffer", "-b", "t") == tricky.strip()


# --- terminal views (cells) on the test's own tmux server -------------------------------------------------------
def _demo(env, name="demo", cmd="cat"):
    env.tmux.run("new-session", "-d", "-s", name, "-x", "60", "-y", "8", cmd)
    pane = env.tmux.run("display", "-p", "-t", f"={name}:", "#{pane_id}")
    env.tmux.run("set-option", "-p", "-t", pane, "@force_role", "agent")
    return pane


def _text(screen_lines, rows):
    out = {ln["y"]: "".join(r["text"] for r in ln["runs"]) for ln in screen_lines}
    return [out.get(y, "") for y in range(rows)]


def test_terminal_open_input_and_frames(env, client):
    _demo(env)
    here = api.short_host()
    r = client.call("terminal.open", session="demo")["ok"]
    assert r["session"] == f"{here}:demo" and r["mode"] == "cells"
    first = client.event("terminal.screen")
    assert (first["cols"], first["rows"]) == (60, 8) and len(first["lines"]) == 8, "the whole screen first"
    assert first["cursor"]["visible"] is True
    assert client.call("terminal.input", term=r["term"], keys=[{"text": 'hello $USER "x"'}, "Enter"])["ok"] == {}
    scr = client.event("terminal.screen", where=lambda e: any('hello $USER "x"' in "".join(x["text"] for x in ln["runs"])
                                                              for ln in e["lines"]))
    assert len(scr["lines"]) < 8, "only the rows that changed"
    assert scr["seq"] > first["seq"]
    assert client.call("terminal.input", term=r["term"], keys=["rm -rf /"])["error"]["code"] == "bad_request"
    clients = env.tmux.run("list-clients", "-F", "#{client_control_mode} #{client_session}")
    assert "1 demo" in clients, "an open view is a control client of the session (it counts as viewed)"
    assert env.tmux.run("display", "-p", "-t", "=demo:", "#{window_width}x#{window_height}") == "60x8", \
        "it never sizes the window"
    assert client.call("terminal.close", term=r["term"])["ok"] == {}
    time.sleep(0.3)
    assert "1 demo" not in env.tmux.run("list-clients", "-F", "#{client_control_mode} #{client_session}")


def test_a_resized_view_gives_the_size_back(env, client):
    _demo(env)
    env.tmux.run("set-option", "-g", "window-size", "smallest")
    t = client.call("terminal.open", session="demo")["ok"]["term"]
    client.event("terminal.screen")
    client.call("terminal.resize", term=t, cols=30, rows=5)
    client.event("terminal.screen", where=lambda e: e["cols"] == 30)
    client.call("terminal.close", term=t)
    time.sleep(0.4)
    # no other client: tmux keeps the last size; with one, `smallest` gives it that client's (tested by tmux)
    assert "1 demo" not in env.tmux.run("list-clients", "-F", "#{client_control_mode} #{client_session}")


def test_terminal_paste_keeps_newlines_as_one_paste(env, client):
    _demo(env)
    t = client.call("terminal.open", session="demo")["ok"]["term"]
    client.event("terminal.screen")
    client.call("terminal.input", term=t, keys=[{"text": "one\ntwo"}])
    client.event("terminal.screen", where=lambda e: "two" in json.dumps(e["lines"]))
    rows = env.tmux.run("capture-pane", "-p", "-t", "=demo:").splitlines()
    # the terminal echoes "one", the newline ends that line (cat prints it back after "two" was typed)
    assert rows[0] == "one" and rows[1].startswith("two") and "one" in rows[1]


def test_terminal_switch_hidden_and_scroll(env, client):
    _demo(env, "a")
    _demo(env, "b")
    env.tmux.run("send-keys", "-t", "=b:", "-l", "in b")
    t = client.call("terminal.open", session="a")["ok"]["term"]
    client.event("terminal.screen")
    assert client.call("terminal.switch", term=t, session="b")["ok"]["session"].endswith(":b")
    e = client.event("terminal.screen", where=lambda e: "in b" in json.dumps(e["lines"]))
    assert len(e["lines"]) == 8, "a switch sends the whole screen"
    assert client.call("terminal.hidden", term=t, hidden=True)["ok"] == {}
    time.sleep(0.3)
    assert "1 " not in env.tmux.run("list-clients", "-F", "#{client_control_mode} #{client_session}"), \
        "hidden: detached, so not viewed"
    client.call("terminal.hidden", term=t, hidden=False)
    client.event("terminal.screen")
    assert client.call("terminal.scroll", term=t, lines=-5)["ok"] == {}
    assert client.call("terminal.resize", term=t, cols=40, rows=6)["ok"] == {"cols": 40, "rows": 6}
    e = client.event("terminal.screen", where=lambda e: (e["cols"], e["rows"]) == (40, 6))
    assert len(e["lines"]) == 6, "a new size sends the whole screen"
    assert env.tmux.run("display", "-p", "-t", "=b:", "#{window_width}x#{window_height}") == "40x6"
    assert client.call("terminal.resize", term=t, cols=5, rows=6)["error"]["code"] == "bad_request"
    client.call("terminal.close", term=t)


def test_terminal_errors(env, client):
    assert client.call("terminal.open", session="nope")["error"]["code"] == "no_session"
    assert client.call("terminal.open", session="nohost:x")["error"]["code"] == "no_route"
    assert client.call("terminal.open", session="demo", mode="vt")["error"]["code"] == "bad_request"
    assert client.call("terminal.input", term=99, keys=[])["error"]["code"] == "bad_request"


def test_terminal_closed_when_the_session_ends(env, client):
    _demo(env, "gone")
    t = client.call("terminal.open", session="gone")["ok"]["term"]
    client.event("terminal.screen")
    env.tmux.run("kill-session", "-t", "=gone")
    e = client.event("terminal.closed")
    assert e["term"] == t


def test_force_api_command_end_to_end(env):
    """`force api` as a process (the way a frontend runs it), on the test's own state."""
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parent.parent)
    c = ApiClient(argv=[sys.executable, "-c", f"import sys; sys.path.insert(0, {root!r}); from force import cli; "
                                              "sys.exit(cli.main(['api']))"])
    assert c.call("hello", protocol=1)["ok"]["protocol"] == 1
    _snap(env.paths, [_view("a", State.WORKING)])
    c.call("subscribe", to=["state"])
    assert c.event("state")["sessions"][0]["name"] == "a"
    c.close()


@pytest.mark.parametrize("key, ok", [
    ("Enter", True), ("BTab", True), ("F5", True), ("C-Up", True), ("S-Up", True), ("M-x", True), ("C-[", True),
    ("C-\\", True), ("M-/", True), ("a", True), ("~", True), ("C-M-S-x", True),
    ("rm -rf /", False), ("", False), ("Enter;kill", False), ("C-", False), ("x" * 40, False),
])
def test_key_names(key, ok):
    assert bool(api._KEY.match(key)) is ok


# --- the whole app in a pty (vt mode) -----------------------------------------------------------------------------
def _vt_client(env, script):
    c = ApiClient(env.make_cockpit(), app_argv=["sh", "-c", script])
    c.call("hello", protocol=1)
    return c


def _vt_text(c, want, timeout=10):
    import base64
    got = b""
    end = time.monotonic() + timeout
    while want.encode() not in got:
        e = c.event("terminal.data", timeout=max(0.1, end - time.monotonic()))
        got += base64.b64decode(e["data"])
    return got


def test_vt_app_runs_in_a_pty_of_the_asked_size(env):
    import base64
    c = _vt_client(env, 'stty size; tty >/dev/null && echo TTY; stty -echo; cat')
    r = c.call("terminal.open", mode="vt", target="app", cols=90, rows=33)["ok"]
    assert (r["mode"], r["target"], r["cols"], r["rows"]) == ("vt", "app", 90, 33)
    assert b"33 90" in _vt_text(c, "TTY")
    # input is bytes as a terminal sends them; the app's echo comes back
    assert c.call("terminal.input", term=r["term"], data=base64.b64encode(b"hello\r").decode())["ok"] == {}
    assert b"hello" in _vt_text(c, "hello")
    assert c.call("terminal.input", term=r["term"], data="not base64!")["error"]["code"] == "bad_request"
    c.close()


def test_vt_app_resize_reaches_the_app(env):
    c = _vt_client(env, 'trap "stty size" WINCH; echo READY; while :; do sleep 0.1; done')
    t = c.call("terminal.open", mode="vt", target="app", cols=80, rows=24)["ok"]["term"]
    _vt_text(c, "READY")
    assert c.call("terminal.resize", term=t, cols=132, rows=50)["ok"] == {"cols": 132, "rows": 50}
    assert b"50 132" in _vt_text(c, "50 132")
    assert c.call("terminal.close", term=t)["ok"] == {}
    c.close()


def test_vt_app_end_is_reported_and_cells_ops_refused(env):
    c = _vt_client(env, 'echo BYE')
    t = c.call("terminal.open", mode="vt", target="app", cols=80, rows=24)["ok"]["term"]
    assert c.event("terminal.closed")["term"] == t
    t2 = _vt_client(env, "cat")
    term = t2.call("terminal.open", mode="vt", target="app")["ok"]["term"]
    for op, extra in (("terminal.scroll", {"lines": 1}), ("terminal.switch", {"session": "x"}),
                      ("terminal.hidden", {"hidden": "yes"})):
        assert t2.call(op, term=term, **extra)["error"]["code"] == "bad_request", op
    assert t2.call("terminal.open", mode="vt", target="app", cols=5, rows=5)["error"]["code"] == "bad_request"
    t2.close()
    c.close()


def test_vt_app_default_is_this_installs_force_ui(env):
    a = api.Api(env.make_cockpit(), inp=iter(()), out=open(os.devnull, "w"))
    assert a.app_argv[1:] == ["--no-intro", "--glyphs", "rich"]


def test_vt_app_a_tmux_client_draws(env):
    """tmux's client needs the pty as its controlling terminal (a shell that only opens it by name is not enough:
    the client then draws nothing)."""
    import base64
    c = ApiClient(env.make_cockpit(), app_argv=["tmux", "-L", env.socket + "-vt", "-f", "/dev/null", "new-session",
                                                "sh", "-c", "echo HELLO-FROM-PANE; sleep 5"])
    c.call("hello", protocol=1)
    t = c.call("terminal.open", mode="vt", target="app", cols=80, rows=24)["ok"]["term"]
    try:
        assert b"HELLO-FROM-PANE" in _vt_text(c, "HELLO-FROM-PANE")
    finally:
        c.call("terminal.close", term=t)
        subprocess.run(["tmux", "-L", env.socket + "-vt", "kill-server"], capture_output=True)
        c.close()


def test_vt_app_hidden_marks_its_client_on_the_sessions_host(env):
    """Hidden, the app keeps running, and the client its ui window shows a session through (@force_nested, on the
    host @force_host_tmux reaches) is marked hidden there with its creation time; shown or closed, unmarked."""
    ui = env.socket + "-ui"
    env.tmux.run("new-session", "-d", "-s", "a", "-x", "80", "-y", "24", "cat")
    nested = subprocess.Popen(["tmux", "-L", env.socket, "-C", "attach", "-t", "=a"], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, text=True)
    c = ApiClient(env.make_cockpit(), app_argv=["tmux", "-L", ui, "-f", "/dev/null", "new-session", "-s", "app",
                                                "sh", "-c", "echo UP; sleep 30"])
    c.call("hello", protocol=1)
    t = c.call("terminal.open", mode="vt", target="app", cols=80, rows=24)["ok"]["term"]
    try:
        _vt_text(c, "UP")
        name = next(cl.name for cl in env.tmux.clients(control=True) if cl.session == "a")
        created = env.tmux.run("display", "-p", "-c", name, "#{client_created}")
        subprocess.run(["tmux", "-L", ui, "set-option", "-w", "-t", "=app:", "@force_nested", name], check=True)
        subprocess.run(["tmux", "-L", ui, "set-option", "-w", "-t", "=app:", "@force_host_tmux",
                        f"tmux -L {env.socket}"], check=True)
        mark = lambda: env.tmux.run("show-options", "-gqv", api.hidden_option(name))
        assert c.call("terminal.hidden", term=t, hidden=True)["ok"] == {}
        assert mark() == created
        assert c.call("terminal.hidden", term=t, hidden=False)["ok"] == {}
        assert mark() == ""
        c.call("terminal.hidden", term=t, hidden=True)
        assert mark() == created
        c.call("terminal.close", term=t)
        assert wait_for_value(mark, "")
    finally:
        nested.stdin.close()
        nested.wait(timeout=5)
        c.close()


def wait_for_value(fn, want, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn() == want:
            return True
        time.sleep(0.1)
    return fn() == want
