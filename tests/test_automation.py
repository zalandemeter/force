"""Automation commands: force prompt|wait|read|keys|report|explain (SPEC §43).

Drives states by writing snapshot.json ourselves (generated_ms = now); real sessions run on the test
tmux server from the `env` fixture.
"""
import dataclasses
import json
import os
import time
import uuid

import pytest

from force import automation, cli, messages
from force import remote as remote_mod
from force.registry import locked
from force.tmux import Pane

import tests.test_daemon as daemon_helpers


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def add_entry(env, name, cwd="/tmp", kind=None, archived=False):
    with locked(env.paths.home) as reg:
        if reg.get(name):
            return reg.get(name)
        e = reg.add(name, str(uuid.uuid4()), cwd)
        if kind:
            e.extra["kind"] = kind
        e.archived = archived
        return e


def make_cat_session(env, name):
    pane = env.tmux.run("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, "-c", "/tmp", "cat")
    env.tmux.run("set-option", "-p", "-t", pane, "@force_role", "agent")
    return pane


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def write_snap(env, sessions, generated_ms=None):
    if generated_ms is None:
        generated_ms = int(time.time() * 1000)
    env.paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    env.paths.snapshot.write_text(json.dumps({"generated_ms": generated_ms, "sessions": sessions}))


def sess(name, state, **kw):
    base = {"name": name, "state": state, "since_ms": int(time.time() * 1000),
            "activity": None, "viewed": False, "kind": "claude"}
    base.update(kw)
    return base


class FK:
    """Fake clock in seconds (like time.monotonic)."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _args(argv):
    return cli.build_parser().parse_args(argv)


# --------------------------------------------------------------------------- #
# parse_state / key_alias (pure)
# --------------------------------------------------------------------------- #

def test_parse_state():
    assert automation.parse_state("your-turn") == "YOUR_TURN"
    assert automation.parse_state("your_turn") == "YOUR_TURN"
    assert automation.parse_state("YOUR_TURN") == "YOUR_TURN"
    assert automation.parse_state("needs-you") == "NEEDS_YOU"
    assert automation.parse_state("working") == "WORKING"
    assert automation.parse_state("idle") == "IDLE"
    assert automation.parse_state("stale") == "STALE"
    assert automation.parse_state("down") == "DOWN"
    assert automation.parse_state("starting") == "STARTING"
    assert automation.parse_state("shell") == "SHELL"
    assert automation.parse_state("unknown") == "UNKNOWN"
    with pytest.raises(ValueError):
        automation.parse_state("bogus")


def test_key_alias_mapping():
    cases = {
        "esc": "Escape", "escape": "Escape", "ESC": "Escape",
        "enter": "Enter", "return": "Enter", "Enter": "Enter",
        "tab": "Tab", "space": "Space",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "backspace": "BSpace", "shift+tab": "BTab",
        "ctrl+c": "C-c", "C-c": "C-c", "c-c": "C-c", "ctrl+x": "C-x",
        "alt+x": "M-x", "m-x": "M-x",
        "j": "j", "C-Space": "C-Space", "PageDown": "PageDown",
    }
    for word, want in cases.items():
        assert automation.key_alias(word) == want, word


# --------------------------------------------------------------------------- #
# prompt refusals send nothing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("setup,expect", [
    # blocked: state NEEDS_YOU (answer the dialog)
    ("blocked", "blocked"),
    ("down", "down"),
])
def test_prompt_refusal_sends_nothing(env, capsys, setup, expect):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "s1")
    add_entry(env, "s1")
    write_snap(env, [sess("s1", "NEEDS_YOU" if setup == "blocked" else "DOWN")])
    args = _args(["prompt", "s1", "hello"])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, args)
    assert ei.value.code == expect
    capsys.readouterr()
    out = cockpit.tmux.run("capture-pane", "-p", "-t", pane)
    assert "hello" not in out


def test_prompt_not_agent(env):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "sh")
    add_entry(env, "sh", kind="shell")
    write_snap(env, [sess("sh", "IDLE", kind="shell")])
    args = _args(["prompt", "sh", "hello"])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, args)
    assert ei.value.code == "not_agent"
    assert "hello" not in cockpit.tmux.run("capture-pane", "-p", "-t", pane)


def test_prompt_no_session(env):
    cockpit = env.make_cockpit()
    args = _args(["prompt", "nope", "hello"])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, args)
    assert ei.value.code == "no_session"


def test_prompt_daemon_not_running(env):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "s1")
    add_entry(env, "s1")
    # no snapshot at all
    args = _args(["prompt", "s1", "hello"])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, args)
    assert ei.value.code == "daemon_not_running"
    assert "hello" not in cockpit.tmux.run("capture-pane", "-p", "-t", pane)


def test_prompt_archived(env):
    cockpit = env.make_cockpit()
    add_entry(env, "old", archived=True)
    write_snap(env, [])
    args = _args(["prompt", "old", "hello"])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, args)
    assert ei.value.code == "archived"


def test_prompt_not_ready_and_not_agent_and_empty(env):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "s1")
    add_entry(env, "s1")
    write_snap(env, [sess("s1", "STARTING")])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, _args(["prompt", "s1", "x"]))
    assert ei.value.code == "not_ready"
    write_snap(env, [sess("s1", "IDLE")])
    # '-' reads stdin; an empty stdin is an empty prompt
    import io
    old_stdin, automation.sys.stdin = automation.sys.stdin, io.StringIO("")
    try:
        with pytest.raises(automation.CmdError) as ei:
            automation.prompt_command(cockpit, _args(["prompt", "s1", "-"]))
    finally:
        automation.sys.stdin = old_stdin
    assert ei.value.code == "empty_prompt"
    assert "x" not in cockpit.tmux.run("capture-pane", "-p", "-t", pane)


# --------------------------------------------------------------------------- #
# prompt sends text + Enter into a running cat
# --------------------------------------------------------------------------- #

def test_prompt_sends_text_into_cat(env, capsys):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "cat")
    wait_for(lambda: cockpit.agent_pane("cat") is not None)
    add_entry(env, "cat")
    write_snap(env, [sess("cat", "IDLE")])
    rc = automation.prompt_command(cockpit, _args(["prompt", "cat", "hello", "world"]))
    assert rc == 0
    assert wait_for(lambda: "hello world" in cockpit.tmux.run("capture-pane", "-p", "-t", pane),
                  timeout=5.0)
    assert capsys.readouterr().out == ""


def test_prompt_until_needs_wait(env):
    cockpit = env.make_cockpit()
    add_entry(env, "s1")
    write_snap(env, [sess("s1", "IDLE")])
    with pytest.raises(automation.UsageError):
        automation.prompt_command(cockpit, _args(["prompt", "s1", "x", "--until", "your-turn"]))


# --------------------------------------------------------------------------- #
# prompt --wait with a fake clock
# --------------------------------------------------------------------------- #

def _cat(env, cockpit):
    pane = make_cat_session(env, "cat")
    wait_for(lambda: cockpit.agent_pane("cat") is not None)
    add_entry(env, "cat")
    return pane


def test_prompt_wait_sends_then_waits(env, capsys):
    """--wait sends first (text in the pane), then sees working and the end of the turn."""
    cockpit = env.make_cockpit()
    pane = _cat(env, cockpit)
    fk = FK()
    states = iter(["WORKING", "WORKING", "YOUR_TURN"])

    def fake_sleep(s):
        fk.advance(s)
        write_snap(env, [sess("cat", next(states, "YOUR_TURN"))])

    write_snap(env, [sess("cat", "IDLE")])
    rc = automation.prompt_command(cockpit, _args(["prompt", "cat", "hi", "there", "--wait"]), clock=fk, sleep=fake_sleep)
    assert rc == 0
    assert capsys.readouterr().out.strip() == "your-turn"
    assert wait_for(lambda: "hi there" in cockpit.tmux.run("capture-pane", "-p", "-t", pane))


def test_prompt_wait_refuses_blocked_without_sending(env):
    cockpit = env.make_cockpit()
    pane = _cat(env, cockpit)
    write_snap(env, [sess("cat", "NEEDS_YOU")])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, _args(["prompt", "cat", "zzsecret", "--wait"]), clock=FK(), sleep=lambda s: None)
    assert ei.value.code == "blocked"
    time.sleep(0.3)
    assert "zzsecret" not in cockpit.tmux.run("capture-pane", "-p", "-t", pane)


def test_prompt_wait_success_json(env, capsys):
    cockpit = env.make_cockpit()
    _cat(env, cockpit)
    fk = FK()

    def fake_sleep(s):
        fk.advance(s)
        write_snap(env, [sess("cat", "YOUR_TURN", activity="a1")])

    write_snap(env, [sess("cat", "WORKING")])
    rc = automation.prompt_command(cockpit, _args(["prompt", "cat", "hi", "--wait", "--json"]), clock=fk, sleep=fake_sleep)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "cat" and data["state"] == "YOUR_TURN" and data["activity"] == "a1"


def test_prompt_wait_stalled(env):
    cockpit = env.make_cockpit()
    _cat(env, cockpit)
    fk = FK()

    def fake_sleep(s):
        fk.advance(s)
        write_snap(env, [sess("cat", "YOUR_TURN")])

    write_snap(env, [sess("cat", "YOUR_TURN")])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, _args(["prompt", "cat", "hi", "--wait"]), clock=fk, sleep=fake_sleep)
    assert ei.value.code == "prompt_stalled"


def test_daemon_check_uses_the_wall_clock(env):
    """A snapshot 30 s old by the wall clock is stale whatever the wait clock says."""
    cockpit = env.make_cockpit()
    add_entry(env, "s1")
    write_snap(env, [sess("s1", "WORKING")], generated_ms=int(time.time() * 1000) - 30_000)
    with pytest.raises(automation.CmdError) as ei:
        automation.wait_command(cockpit, _args(["wait", "s1"]), clock=FK(start=0.0), sleep=lambda s: None)
    assert ei.value.code == "daemon_not_running"


# --------------------------------------------------------------------------- #
# wait
# --------------------------------------------------------------------------- #

def test_wait_returns_at_once_when_matching(env, capsys):
    cockpit = env.make_cockpit()
    add_entry(env, "cat")
    write_snap(env, [sess("cat", "YOUR_TURN")])
    called = {"n": 0}

    def fake_sleep(s):
        called["n"] += 1

    rc = automation.wait_command(cockpit, _args(["wait", "cat"]), clock=FK(), sleep=fake_sleep)
    assert rc == 0
    assert called["n"] == 0
    assert capsys.readouterr().out.strip() == "your-turn"


def test_wait_timeout(env):
    cockpit = env.make_cockpit()
    add_entry(env, "cat")
    fk = FK(start=0.0)

    def fake_sleep(s):
        fk.advance(s)
        write_snap(env, [sess("cat", "WORKING")])

    write_snap(env, [sess("cat", "WORKING")])
    with pytest.raises(automation.CmdError) as ei:
        automation.wait_command(cockpit, _args(["wait", "cat", "--timeout", "0.5"]), clock=fk, sleep=fake_sleep)
    assert ei.value.code == "timeout"


# --------------------------------------------------------------------------- #
# read / keys
# --------------------------------------------------------------------------- #

def test_read_returns_pane_lines(env, capsys):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "cat")
    wait_for(lambda: cockpit.agent_pane("cat") is not None)
    add_entry(env, "cat")
    cockpit.tmux.run("send-keys", "-t", pane, "read-line-one")
    assert wait_for(lambda: "read-line-one" in cockpit.tmux.run("capture-pane", "-p", "-t", pane))
    rc = automation.read_command(cockpit, _args(["read", "cat", "--lines", "20"]))
    assert rc == 0
    assert "read-line-one" in capsys.readouterr().out


def test_read_lines_zero_usage_error(env):
    cockpit = env.make_cockpit()
    add_entry(env, "cat")
    with pytest.raises(automation.UsageError):
        automation.read_command(cockpit, _args(["read", "cat", "--lines", "0"]))


def test_keys_literal(env):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "cat")
    add_entry(env, "cat")
    assert automation.keys_command(cockpit, _args(["keys", "cat", "hello world", "--literal"])) == 0
    assert wait_for(lambda: "hello world" in cockpit.tmux.run("capture-pane", "-p", "-t", pane))


def test_keys_sends_aliases(env, capsys):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "cat")
    add_entry(env, "cat")
    # Enter via the alias: cat shows nothing more, but the call must not error and must use the alias
    assert automation.keys_command(cockpit, _args(["keys", "cat", "enter"])) == 0


def test_keys_down_refused(env):
    cockpit = env.make_cockpit()
    add_entry(env, "gone")
    with pytest.raises(automation.CmdError) as ei:
        automation.keys_command(cockpit, _args(["keys", "gone", "j"]))
    assert ei.value.code == "down"


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def test_report_default_name_error_outside_force(env, capsys, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    rc = cli.main(["report", "--state", "working"])
    assert rc == 1
    assert "not inside a Force session" in capsys.readouterr().err


def test_report_write_and_clear(env):
    add_entry(env, "n")
    assert cli.main(["report", "n", "--state", "working", "--summary", "x", "--pid", "0"]) == 0
    p = env.paths.state / "reports" / "n.json"
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["state"] == "working" and data["summary"] == "x" and data["pid"] == 0
    assert cli.main(["report", "n", "--clear"]) == 0
    assert not p.exists()


def test_report_idle_is_clear(env):
    add_entry(env, "n")
    assert cli.main(["report", "n", "--state", "idle", "--summary", "x"]) == 0
    assert not (env.paths.state / "reports" / "n.json").exists()


def test_report_bad_state_usage(env, capsys):
    add_entry(env, "n")
    rc = cli.main(["report", "n", "--state", "bogus"])
    assert rc == 2
    assert "--state" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# explain
# --------------------------------------------------------------------------- #

def test_explain_json_keys(env, capsys):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "cat")
    wait_for(lambda: cockpit.agent_pane("cat") is not None)
    add_entry(env, "cat")
    now = int(time.time() * 1000)
    write_snap(env, [sess("cat", "WORKING", since_ms=now - 3000, activity="act", viewed=True,
                          kind="claude", title="t", why="status: busy")], generated_ms=now)
    rc = automation.explain_command(cockpit, _args(["explain", "cat", "--json"]))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    for k in ("name", "kind", "state", "since_ms", "viewed", "activity", "title", "why",
              "snapshot_age_s", "pane", "pid", "command", "dead", "report", "screen"):
        assert k in data, k
    assert data["name"] == "cat"
    assert data["state"] == "WORKING"
    assert data["kind"] == "claude"
    assert isinstance(data["screen"], list)


def test_explain_no_session(env):
    cockpit = env.make_cockpit()
    with pytest.raises(automation.CmdError) as ei:
        automation.explain_command(cockpit, _args(["explain", "nope"]))
    assert ei.value.code == "no_session"


def test_explain_stale_warning(env, capsys):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "cat")
    add_entry(env, "cat")
    now = int(time.time() * 1000)
    write_snap(env, [sess("cat", "WORKING")], generated_ms=now - 60_000)
    automation.explain_command(cockpit, _args(["explain", "cat"]))
    out, err = capsys.readouterr()
    assert "snapshot is 60 s old" in err
    assert "screen:" in out


# --------------------------------------------------------------------------- #
# messages between hosts (SPEC §52): receiving --from
# --------------------------------------------------------------------------- #

def _log_lines(env):
    p = env.paths.state / "messages.log"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_prompt_from_refused_without_setting(env, capsys):
    cockpit = env.make_cockpit()
    pane = make_cat_session(env, "s1")
    add_entry(env, "s1")
    write_snap(env, [sess("s1", "IDLE")])
    args = _args(["prompt", "s1", "secret", "--from", "other@lap"])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, args)
    assert ei.value.code == "not_accepted"
    assert "secret" not in cockpit.tmux.run("capture-pane", "-p", "-t", pane)     # nothing sent
    assert _log_lines(env)[-1]["result"] == "not_accepted"


def test_prompt_from_accepted_delivers_header(env, capsys):
    cockpit = env.make_cockpit()
    cockpit.cfg = dataclasses.replace(cockpit.cfg, accept_prompts_from="lap")
    pane = make_cat_session(env, "s1")
    add_entry(env, "s1")
    write_snap(env, [sess("s1", "IDLE")])
    rc = automation.prompt_command(cockpit, _args(["prompt", "s1", "hello", "--from", "sess@lap"]))
    assert rc == 0
    out = cockpit.tmux.run("capture-pane", "-p", "-t", pane)
    assert 'hello' in out and '[from sess on lap' in out and 'reply: force prompt lap:sess' in out
    assert _log_lines(env)[-1]["result"] == "delivered"


def test_prompt_from_blocked_still_refused(env):
    cockpit = env.make_cockpit()
    cockpit.cfg = dataclasses.replace(cockpit.cfg, accept_prompts_from="*")
    make_cat_session(env, "s1")
    add_entry(env, "s1")
    write_snap(env, [sess("s1", "NEEDS_YOU")])
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, _args(["prompt", "s1", "zz", "--from", "s@s"]))
    assert ei.value.code == "blocked"
    assert _log_lines(env)[-1]["result"] == "blocked"


def test_keys_from_refused(env):
    cockpit = env.make_cockpit()
    make_cat_session(env, "s1")
    add_entry(env, "s1")
    with pytest.raises(automation.CmdError) as ei:
        automation.keys_command(cockpit, _args(["keys", "s1", "j", "--from", "other@lap"]))
    assert ei.value.code == "not_accepted"
    assert _log_lines(env)[-1]["result"] == "not_accepted"


def test_keys_from_accepted(env):
    cockpit = env.make_cockpit()
    cockpit.cfg = dataclasses.replace(cockpit.cfg, accept_prompts_from="lap")
    pane = make_cat_session(env, "s1")
    add_entry(env, "s1")
    assert automation.keys_command(cockpit, _args(["keys", "s1", "hello", "--literal", "--from", "s@lap"])) == 0
    assert wait_for(lambda: "hello" in cockpit.tmux.run("capture-pane", "-p", "-t", pane))
    assert _log_lines(env)[-1]["result"] == "delivered"


# --------------------------------------------------------------------------- #
# messages between hosts (SPEC §52): the sending side
# --------------------------------------------------------------------------- #

def _recording_ssh(monkeypatch):
    calls = []
    monkeypatch.setattr(automation.remote_mod, "ssh_argv",
                       lambda paths, r, *a, tty=False: calls.append((r, a)) or ["true"])
    monkeypatch.setattr(automation.subprocess, "call", lambda argv: calls and 7)
    return calls


def test_prompt_sender_remote_records_argv(env, monkeypatch):
    cockpit = env.make_cockpit()
    remote_mod.save(env.paths, [remote_mod.Remote("build", "build")])
    calls = _recording_ssh(monkeypatch)
    rc = automation.prompt_command(cockpit, _args(["prompt", "build:portal", "hi"]))
    assert rc == 7                                       # the fake's exit code
    r, a = calls[0]
    assert a[0] == "~/.local/bin/force"
    assert a[1:3] == ("prompt", "portal")
    i = a.index("--from")
    assert a[i + 1].endswith("@" + messages.short_host())


def test_prompt_sender_remote_keeps_dash_words_as_text(env, monkeypatch):
    # options first, then --, then the text: "-v" must arrive as text, not as an option of the remote's prompt
    cockpit = env.make_cockpit()
    remote_mod.save(env.paths, [remote_mod.Remote("build", "build")])
    calls = _recording_ssh(monkeypatch)
    automation.prompt_command(cockpit, _args(["prompt", "build:portal", "--wait", "--", "-v", "run", "it"]))
    _r, a = calls[0]
    remote_args = cli.build_parser().parse_args(list(a[1:]))       # what the remote's force parses
    assert remote_args.name == "portal" and remote_args.text == ["-v", "run", "it"]
    assert remote_args.wait and remote_args.from_.endswith("@" + messages.short_host())


def test_prompt_sender_remote_host_form(env, monkeypatch):
    cockpit = env.make_cockpit()
    remote_mod.save(env.paths, [remote_mod.Remote("build", "build")])
    calls = _recording_ssh(monkeypatch)
    automation.prompt_command(cockpit, _args(["prompt", "portal", "hi", "--host", "build"]))
    r, a = calls[0]
    assert a[0] == "~/.local/bin/force" and a[1:3] == ("prompt", "portal") and "hi" in a


def test_prompt_peer_queues(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    messages.mark_peer(env.paths, "lap", os.getpid())
    rc = automation.prompt_command(cockpit, _args(["prompt", "lap:portal", "hi"]))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "queued for lap"
    files = list((env.paths.state / "outbox" / "lap").iterdir())
    assert len(files) == 1
    item = json.loads(files[0].read_text())
    assert item["to"] == "portal" and item["text"] == "hi" and item["from"].endswith("@" + messages.short_host())
    assert not files[0].name.startswith(".")


def test_prompt_peer_wait_usage(env):
    cockpit = env.make_cockpit()
    messages.mark_peer(env.paths, "lap", os.getpid())
    with pytest.raises(automation.UsageError):
        automation.prompt_command(cockpit, _args(["prompt", "lap:portal", "hi", "--wait"]))
    # other commands never reach a peer
    with pytest.raises(automation.CmdError) as ei:
        automation.wait_command(cockpit, _args(["wait", "lap:portal"]))
    assert ei.value.code == "no_route"


def test_prompt_no_route(env):
    cockpit = env.make_cockpit()
    with pytest.raises(automation.CmdError) as ei:
        automation.prompt_command(cockpit, _args(["prompt", "ghost:portal", "hi"]))
    assert ei.value.code == "no_route"


def test_prompt_bad_target_usage(env):
    cockpit = env.make_cockpit()
    with pytest.raises(automation.UsageError):
        automation.prompt_command(cockpit, _args(["prompt", "a:b:c", "hi"]))


def test_prompt_host_and_target_disagree(env, monkeypatch):
    cockpit = env.make_cockpit()
    remote_mod.save(env.paths, [remote_mod.Remote("build", "build")])
    with pytest.raises(automation.UsageError):
        automation.prompt_command(cockpit, _args(["prompt", "other:portal", "hi", "--host", "build"]))


def test_forward_does_not_forward_host_colon_name(env, monkeypatch):
    remote_mod.save(env.paths, [remote_mod.Remote("devbox", "devbox")])
    monkeypatch.setenv("FORCE_HOST", "devbox")
    called = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **k: called.append(argv) or 0)
    assert cli._forward(["prompt", "other:portal", "hi"]) is None      # HOST:NAME stays here
    assert cli._forward(["prompt", "portal", "hi", "--host", "other"]) is None
    assert cli._forward(["prompt", "portal", "hi"]) == 0              # still forwards a bare NAME
    assert called
