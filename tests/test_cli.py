import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from force import cli
from force.config import Config, get_paths
from force.registry import Entry
from force import state
from force.state import SessionView, Snapshot, State


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


def add_entry(home, name, cwd, archived=False, session_id=None, order=None):
    from force.registry import locked
    import uuid
    with locked(home) as reg:
        if reg.get(name):
            return
        e = reg.add(name, session_id or str(uuid.uuid4()), cwd)
        e.archived = archived
        if order is not None:
            e.order = order


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
        # stage 2 of `force run` (stage 1 re-execs through bash; tested separately)
        "FORCE_RUN_STAGE": "2",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "settings.json").write_text(json.dumps({"layout": "classic", "sharing": True}))  # sharing is off by default (parked)
    claude = tmp_path / "bin" / "claude"          # a stand-in, so `force new` finds Claude Code on any machine
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    fake = FakeTmux()
    cockpit = cli.make_cockpit()
    cockpit.tmux = fake
    monkeypatch.setattr(cli, "make_cockpit", lambda: cockpit)
    return cockpit, fake, tmp_path


# --- parsing of every subcommand ---
@pytest.mark.parametrize("version, shown", [("1.4.2", "force 1.4.2"), (None, "force (development build)")])
def test_version_flag(monkeypatch, capsys, version, shown):
    # `force --version`: the release's VERSION file, or a plain note in a development build (for bug reports)
    monkeypatch.setattr("force.updates.current_version", lambda: version)
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == shown


def test_parser_subcommands():
    p = cli.build_parser()
    cases = [
        (["new", "foo", "--cwd", "/x", "--no-switch"], "new"),
        (["rename", "a", "b"], "rename"),
        (["archive", "a", "--force"], "archive"),
        (["unarchive", "a"], "unarchive"),
        (["restart", "a"], "restart"),
        (["upgrade", "--all"], "upgrade"),
        (["upgrade", "a"], "upgrade"),
        (["list", "--all", "--json"], "list"),
        (["switch", "--index", "2", "--client", "c1"], "switch"),
        (["switch", "--next"], "switch"),
        (["switch", "--prev"], "switch"),
        (["switch", "portal"], "switch"),
        (["forget", "a"], "forget"),
        (["import", "--dry-run", "--pid", "5", "--name", "x"], "import"),
        (["sidebar-toggle"], "sidebar-toggle"),
        (["daemon"], "daemon"),
        (["run", "a"], "run"),
        (["sidebar"], "sidebar"),
        (["dialog", "new"], "dialog"),
        (["dialog", "rename", "a"], "dialog"),
        (["dialog", "move", "a"], "dialog"),
        (["dialog", "archive", "a"], "dialog"),
        (["dialog", "archived"], "dialog"),
        (["dialog", "jump"], "dialog"),
        (["move", "a", "up"], "move"),
        (["move", "a", "down"], "move"),
    ]
    for argv, cmd in cases:
        a = p.parse_args(argv)
        assert a.command == cmd
    # no subcommand -> default attach
    a = p.parse_args([])
    assert a.command is None


# --- run ---
def test_run_new_argv(fakes, capsys):
    cockpit, fake, tmp = fakes
    cwd = str(tmp)
    add_entry(cockpit.paths.home, "portal", cwd)
    entry = __import__("force.registry", fromlist=["load"]).load(cockpit.paths.registry).get("portal")
    captured = {}
    import force.cli as c
    orig = c.os.execvp
    def fakexec(*a):
        captured["argv"] = a[1]
    c.os.execvp = fakexec
    try:
        rc = c.run_command(cockpit, "portal")
    finally:
        c.os.execvp = orig
    assert rc == 0
    argv = captured["argv"]
    from force.claude import build_argv, statusline_command
    sl = statusline_command(Path(cockpit.paths.tmux_conf).parent / "statusline.py",
                            cockpit.paths.state / "usage.json")
    assert argv == build_argv(cockpit.paths.claude_bin, cockpit.paths.claude_dir, "portal", entry.session_id, sl)
    assert argv[1] == "--session-id"


def test_run_resume_argv(fakes):
    cockpit, fake, tmp = fakes
    cwd = str(tmp)
    import uuid
    sid = str(uuid.uuid4())
    add_entry(cockpit.paths.home, "portal", cwd, session_id=sid)
    proj = cockpit.paths.claude_dir / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").touch()
    captured = {}
    import force.cli as c
    orig = c.os.execvp
    c.os.execvp = lambda *a: captured.setdefault("argv", a[1])
    try:
        c.run_command(cockpit, "portal")
    finally:
        c.os.execvp = orig
    assert captured["argv"][1] == "--resume"
    assert captured["argv"][2] == sid


def test_run_missing_entry(fakes, monkeypatch, capsys):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    rc = cli.run_command(cockpit, "nope")
    assert rc == 2
    assert "no such session" in capsys.readouterr().err


def test_run_archived(fakes, monkeypatch, capsys):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "old", str(tmp), archived=True)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.run_command(cockpit, "old") == 2
    assert "archived" in capsys.readouterr().err


def test_run_missing_cwd(fakes, monkeypatch, capsys):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "gone", str(tmp / "missing"))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.run_command(cockpit, "gone") == 2
    assert "does not exist" in capsys.readouterr().err


# --- new validation ---
def test_new_invalid_name(fakes, capsys):
    cockpit, fake, tmp = fakes
    assert cli.create_session(cockpit, "Bad Name!") == 1
    assert "invalid name" in capsys.readouterr().err


def test_new_taken_name(fakes, capsys):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    assert cli.create_session(cockpit, "portal") == 1
    assert "already taken" in capsys.readouterr().err


def test_new_missing_cwd(fakes, capsys):
    cockpit, fake, tmp = fakes
    assert cli.create_session(cockpit, "okname", str(tmp / "nodir")) == 1
    assert "not a directory" in capsys.readouterr().err


def test_new_success(fakes, capsys):
    cockpit, fake, tmp = fakes
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setenv("TMUX", "/tmp/x/forcetestsock,1234,0")
    try:
        rc = cli.create_session(cockpit, "portal", str(tmp))
    finally:
        monkeypatch.undo()
    assert rc == 0
    entry = __import__("force.registry", fromlist=["load"]).load(cockpit.paths.registry).get("portal")
    assert entry is not None
    assert ("switch_client", "portal", None) in fake.calls


# --- forget ---
def test_forget_live_rejected(fakes, capsys):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    assert cli.forget_command(cockpit, "portal") == 1
    assert "only archived" in capsys.readouterr().err


def test_forget_archived_ok(fakes, capsys):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "old", str(tmp), archived=True)
    assert cli.forget_command(cockpit, "old") == 0
    assert __import__("force.registry", fromlist=["load"]).load(cockpit.paths.registry).get("old") is None


# --- switch ---
def test_switch_index(fakes):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp), order=1)
    add_entry(cockpit.paths.home, "b", str(tmp), order=2)
    add_entry(cockpit.paths.home, "c", str(tmp), order=3)
    assert cli.switch_command(cockpit, None, 2, False, False, None) == 0
    assert fake.calls[-1] == ("switch_client", "b", None)


def test_switch_index_out_of_range(fakes, capsys):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp))
    assert cli.switch_command(cockpit, None, 5, False, False, None) == 1


def test_switch_next(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp), order=1)
    add_entry(cockpit.paths.home, "b", str(tmp), order=2)
    add_entry(cockpit.paths.home, "c", str(tmp), order=3)
    monkeypatch.setenv("TMUX_PANE", "%0")
    fake.run_out = "a"
    assert cli.switch_command(cockpit, None, None, True, False, None) == 0
    assert fake.calls[-1] == ("switch_client", "b", None)


def test_switch_prev_wraps(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp), order=1)
    add_entry(cockpit.paths.home, "b", str(tmp), order=2)
    add_entry(cockpit.paths.home, "c", str(tmp), order=3)
    monkeypatch.setenv("TMUX_PANE", "%0")
    fake.run_out = "a"
    assert cli.switch_command(cockpit, None, None, False, True, None) == 0
    assert fake.calls[-1] == ("switch_client", "c", None)


def test_switch_name(fakes):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    assert cli.switch_command(cockpit, "portal", None, False, False, "cli0") == 0
    assert fake.calls[-1] == ("switch_client", "portal", "cli0")


# --- list from fixture snapshot ---
def test_list_from_snapshot(fakes, capsys):
    cockpit, fake, tmp = fakes
    now = int(time.time() * 1000)
    snap = Snapshot(now, "2.1.292", [
        SessionView("portal", 1, State.NEEDS_YOU, now - 5000, "2.1.289",
                   True, False, 1, str(tmp), "s", 123, "input needed", False),
        SessionView("old", None, State.ARCHIVED, None, None, False, True, 2,
                  str(tmp), "s2", None, None, False),
    ])
    state.write_snapshot(cockpit.paths.snapshot, snap)
    assert cli.list_command(cockpit, False, False) == 0
    out = capsys.readouterr().out
    assert "portal" in out and "NEEDS_YOU" in out
    assert "old" not in out
    capsys.readouterr()
    assert cli.list_command(cockpit, True, False) == 0
    assert "old" in capsys.readouterr().out


def test_list_json(fakes, capsys):
    cockpit, fake, tmp = fakes
    now = int(time.time() * 1000)
    snap = Snapshot(now, "2.1.292", [
        SessionView("portal", 1, State.IDLE, now, "2.1.292", False, False,
                   1, str(tmp), "s", 1, None, False)])
    state.write_snapshot(cockpit.paths.snapshot, snap)
    assert cli.list_command(cockpit, False, True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["sessions"][0]["name"] == "portal"
    assert data["latest_version"] == "2.1.292"



# --- attach refuses inside cockpit ---
def _load_reg(cockpit):
    return __import__("force.registry", fromlist=["load"]).load(cockpit.paths.registry)


# --- move ---
def test_move_up_swaps(fakes):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp), order=1)
    add_entry(cockpit.paths.home, "b", str(tmp), order=2)
    add_entry(cockpit.paths.home, "c", str(tmp), order=3)
    assert cli.move_command(cockpit, "c", "up") == 0
    assert [e.name for e in _load_reg(cockpit).live()] == ["a", "c", "b"]


def test_move_down_swaps(fakes):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp), order=1)
    add_entry(cockpit.paths.home, "b", str(tmp), order=2)
    add_entry(cockpit.paths.home, "c", str(tmp), order=3)
    assert cli.move_command(cockpit, "a", "down") == 0
    assert [e.name for e in _load_reg(cockpit).live()] == ["b", "a", "c"]


def test_move_top_noop(fakes):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp), order=1)
    add_entry(cockpit.paths.home, "b", str(tmp), order=2)
    assert cli.move_command(cockpit, "a", "up") == 0
    assert [e.name for e in _load_reg(cockpit).live()] == ["a", "b"]


def test_move_bottom_noop(fakes):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp), order=1)
    add_entry(cockpit.paths.home, "b", str(tmp), order=2)
    assert cli.move_command(cockpit, "b", "down") == 0
    assert [e.name for e in _load_reg(cockpit).live()] == ["a", "b"]


def test_move_unknown_error(fakes, capsys):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp), order=1)
    assert cli.move_command(cockpit, "nope", "up") == 1
    assert "unknown session" in capsys.readouterr().err


def test_move_archived_error(fakes, capsys):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "old", str(tmp), archived=True, order=1)
    add_entry(cockpit.paths.home, "a", str(tmp), order=2)
    assert cli.move_command(cockpit, "old", "up") == 1
    assert "archived" in capsys.readouterr().err


def test_attach_refuses_inside_cockpit(fakes, monkeypatch, capsys):
    cockpit, fake, tmp = fakes
    monkeypatch.setenv("TMUX", "/run/user/1000/forcetestsock,9,0")
    assert cli.attach_command(cockpit, None) == 1
    assert "already inside Force" in capsys.readouterr().err


def test_run_shell_execs_bash_l_in_cwd_with_scrubbed_env(fakes, monkeypatch):
    """A SHELL session in stage 1 chdir(cwd) and execs `bash -l` with the scrubbed env plus
    FORCE_SESSION — no claude, no stage 2."""
    from force.registry import locked as _locked
    import uuid as _uuid
    cockpit, _fake, tmp = fakes
    cwd = str(tmp)
    with _locked(cockpit.paths.home) as reg:
        reg.add("sh", str(_uuid.uuid4()), cwd).extra["kind"] = "shell"
    monkeypatch.delenv("FORCE_RUN_STAGE", raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CLAUDE_PID", "123")
    monkeypatch.setenv("KEEP_ME", "yes")
    calls = {}
    monkeypatch.setattr(cli.os, "chdir", lambda d: calls.setdefault("cwd", d))

    def fake_execvpe(file, argv, env):
        calls.setdefault("exec", (file, argv, env))
        raise SystemExit(0)

    monkeypatch.setattr(cli.os, "execvpe", fake_execvpe)
    with pytest.raises(SystemExit):
        cli.run_command(cockpit, "sh")
    assert calls["cwd"] == cwd
    file, argv, env = calls["exec"]
    assert file == "bash"
    assert argv == ["bash", "-l"]
    assert env["FORCE_SESSION"] == "sh"
    assert env["KEEP_ME"] == "yes"
    assert not any(k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_") or k == "CLAUDE_PID" for k in env)
    # stage 2 is never entered / no FORCE_RUN_STAGE is set for the shell exec
    assert "FORCE_RUN_STAGE" not in env


def test_new_shell_registry_kind(fakes):
    from force.registry import load as _load
    cockpit, _fake, tmp = fakes
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setenv("TMUX", "/tmp/x/forcetestsock,1234,0")
    try:
        rc = cli.create_session(cockpit, "sh", str(tmp), shell=True)
    finally:
        monkeypatch.undo()
    assert rc == 0
    e = _load(cockpit.paths.registry).get("sh")
    assert e is not None and e.extra.get("kind") == "shell"


def test_parser_new_shell_flag():
    p = cli.build_parser()
    assert p.parse_args(["new", "s", "--shell"]).shell is True
    assert p.parse_args(["new", "s"]).shell is False


def test_run_stage1_scrubs_claude_session_env(fakes, monkeypatch):
    """Stage 1 drops inherited Claude session variables and re-execs through bash -lic."""
    cockpit, _fake, _tmp = fakes
    monkeypatch.delenv("FORCE_RUN_STAGE", raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_x")
    monkeypatch.setenv("CLAUDE_PID", "123")
    monkeypatch.setenv("KEEP_ME", "yes")
    captured = {}

    def fake_execvpe(file, argv, env):
        captured.update(file=file, argv=argv, env=env)
        raise SystemExit(0)

    monkeypatch.setattr(cli.os, "execvpe", fake_execvpe)
    with pytest.raises(SystemExit):
        cli.run_command(cockpit, "portal")
    assert captured["file"] == "bash"
    assert captured["argv"][:2] == ["bash", "-lic"]
    assert captured["argv"][2].startswith("exec ") and captured["argv"][2].endswith(" run portal")
    env = captured["env"]
    assert env["FORCE_RUN_STAGE"] == "2" and env["KEEP_ME"] == "yes"
    assert not any(k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_") or k == "CLAUDE_PID" for k in env)


def test_prepend_tmux_bin_puts_force_tmux_first_exactly_once(tmp_path, monkeypatch):
    """With a shipped tmux (<tmp>/share/tmux-bin/tmux) and FORCE_TMUX_CONF pointing at its tmux.conf,
    _prepend_tmux_bin puts the dir first on PATH exactly once (a second call does not add it twice)."""
    share = tmp_path / "share"
    tbin = share / "tmux-bin"
    tbin.mkdir(parents=True)
    tmux = tbin / "tmux"
    tmux.write_text("#!/bin/sh\n")
    tmux.chmod(0o755)
    conf = share / "tmux.conf"
    conf.write_text("")
    monkeypatch.setenv("FORCE_TMUX_CONF", str(conf))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    cli._prepend_tmux_bin()
    path = os.environ["PATH"]
    assert path.split(os.pathsep)[0] == str(tbin)
    assert path.count(str(tbin)) == 1
    cli._prepend_tmux_bin()                     # idempotent
    assert os.environ["PATH"] == path


def test_prepend_tmux_bin_leaves_path_when_not_shipped(tmp_path, monkeypatch):
    """On a dev deploy with no shipped tmux, the PATH is left exactly as it was."""
    share = tmp_path / "share"
    share.mkdir(parents=True)
    conf = share / "tmux.conf"
    conf.write_text("")
    monkeypatch.setenv("FORCE_TMUX_CONF", str(conf))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    cli._prepend_tmux_bin()
    assert os.environ["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_attach_target_prefers_a_session_no_other_terminal_shows(fakes):
    """A second terminal opening cockpit lands on a different session than the first."""
    from force import state as st
    from force.tmux import Client
    cockpit, fake, tmp = fakes
    for n in ("a", "b", "c"):
        add_entry(cockpit.paths.home, n, str(tmp))
    st.save_seen(cockpit.paths.seen, {"a": st.Seen(last_seen_ms=300), "b": st.Seen(last_seen_ms=200),
                                  "c": st.Seen(last_seen_ms=100)})
    assert cli._target_session(cockpit, None) == "a"
    fake.clients_out = [Client("/dev/pts/1", "a", 200, 50, 1)]
    assert cli._target_session(cockpit, None) == "b"
    fake.clients_out = [Client("/dev/pts/1", "a", 200, 50, 1), Client("/dev/pts/2", "b", 200, 50, 2),
                        Client("/dev/pts/3", "c", 200, 50, 3)]
    assert cli._target_session(cockpit, None) == "a"   # everything on screen: most recently seen


def test_restart_all_restarts_idle_and_marks_busy(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "idle1", str(tmp))
    add_entry(cockpit.paths.home, "busy1", str(tmp))
    with __import__("force.registry", fromlist=["locked"]).locked(cockpit.paths.home) as reg:
        reg.add("sh", "00000000-0000-4000-8000-000000000001", str(tmp)).extra["kind"] = "shell"
    restarted = []
    monkeypatch.setattr(cockpit, "restart", lambda n: pytest.fail("one by one"))
    monkeypatch.setattr(cockpit, "restart_many", lambda ns: restarted.append(list(ns)) or [])
    monkeypatch.setattr(cli, "_current_state_for",
                        lambda c, n: State.WORKING if n == "busy1" else State.IDLE)
    assert cli.main(["restart", "--all"]) == 0
    assert restarted == [["idle1"]]                 # together; shells are left alone
    reg = __import__("force.registry", fromlist=["load"]).load(cockpit.paths.registry)
    assert reg.get("busy1").extra.get("pending_restart") is True
    assert "pending_restart" not in reg.get("idle1").extra


# --- share / unshare / shares (SPEC §19) -----------------------------------------
import getpass as _getpass  # noqa: E402


def _load(cockpit):
    return __import__("force.registry", fromlist=["load"]).load(cockpit.paths.registry)


def test_share_unknown_session(fakes, capsys):
    cockpit, _fake, tmp = fakes
    assert cli.share_command(cockpit, "nope", "carol", False) == 1
    assert "unknown session" in capsys.readouterr().err


def test_share_archived_rejected(fakes, capsys):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "old", str(tmp), archived=True)
    assert cli.share_command(cockpit, "old", "carol", False) == 1
    assert "archived" in capsys.readouterr().err


def test_share_no_such_user(fakes, capsys, monkeypatch):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    def boom(user):
        raise KeyError(user)
    monkeypatch.setattr(cli.share, "guest_uid", boom)
    assert cli.share_command(cockpit, "portal", "ghost", False) == 1
    assert "no such user" in capsys.readouterr().err


def test_share_self_rejected(fakes, capsys, monkeypatch):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    monkeypatch.setattr(cli.share, "guest_uid", lambda user: 1000)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "me")
    assert cli.share_command(cockpit, "portal", "me", False) == 1
    assert "you already have access" in capsys.readouterr().err


def test_share_write_message_and_warning(fakes, capsys, monkeypatch):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    monkeypatch.setattr(cli.share, "guest_uid", lambda user: 1000)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "alice")
    assert cli.share_command(cockpit, "portal", "carol", True) == 0
    out = capsys.readouterr().out
    assert "shared portal with carol (read/write)" in out
    assert "python3 /tmp/force-share-alice/force-join alice/portal" in out
    assert "carol can type into this session. The agent acts with YOUR account:" in out
    assert _load(cockpit).get("portal").extra["shares"] == {"carol": "w"}


def test_share_read_only_no_warning(fakes, capsys, monkeypatch):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    monkeypatch.setattr(cli.share, "guest_uid", lambda user: 1000)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "alice")
    assert cli.share_command(cockpit, "portal", "carol", False) == 0
    out = capsys.readouterr().out
    assert "shared portal with carol (read-only)" in out
    assert "can type into this session" not in out
    assert "read/write" not in out
    assert _load(cockpit).get("portal").extra["shares"] == {"carol": "r"}


def test_share_reshares_changes_mode(fakes, capsys, monkeypatch):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    monkeypatch.setattr(cli.share, "guest_uid", lambda user: 1000)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "alice")
    assert cli.share_command(cockpit, "portal", "carol", False) == 0
    assert _load(cockpit).get("portal").extra["shares"] == {"carol": "r"}
    capsys.readouterr()
    assert cli.share_command(cockpit, "portal", "carol", True) == 0
    capsys.readouterr()
    assert _load(cockpit).get("portal").extra["shares"] == {"carol": "w"}


def test_share_shell_allowed(fakes, capsys, monkeypatch):
    cockpit, _fake, tmp = fakes
    with __import__("force.registry", fromlist=["locked"]).locked(cockpit.paths.home) as reg:
        import uuid as _uuid
        reg.add("sh", str(_uuid.uuid4()), str(tmp)).extra["kind"] = "shell"
    monkeypatch.setattr(cli.share, "guest_uid", lambda user: 1000)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "alice")
    assert cli.share_command(cockpit, "sh", "carol", False) == 0
    assert "shared sh with carol" in capsys.readouterr().out


def test_unshare_removes_one_user(fakes):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    with __import__("force.registry", fromlist=["locked"]).locked(cockpit.paths.home) as reg:
        reg.get("portal").extra["shares"] = {"carol": "r", "daniel": "r"}
    assert cli.unshare_command(cockpit, "portal", "carol") == 0
    assert _load(cockpit).get("portal").extra["shares"] == {"daniel": "r"}


def test_unshare_all_without_user(fakes):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    with __import__("force.registry", fromlist=["locked"]).locked(cockpit.paths.home) as reg:
        reg.get("portal").extra["shares"] = {"carol": "r", "daniel": "r"}
    assert cli.unshare_command(cockpit, "portal", None) == 0
    assert "shares" not in _load(cockpit).get("portal").extra


def test_unshare_unknown_share(fakes, capsys):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    with __import__("force.registry", fromlist=["locked"]).locked(cockpit.paths.home) as reg:
        reg.get("portal").extra["shares"] = {"carol": "r"}
    assert cli.unshare_command(cockpit, "portal", "daniel") == 1
    assert "not shared" in capsys.readouterr().err


def test_unshare_not_shared_any(fakes, capsys):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    assert cli.unshare_command(cockpit, "portal", None) == 1
    assert "not shared" in capsys.readouterr().err


def test_shares_table(fakes, capsys, monkeypatch):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    add_entry(cockpit.paths.home, "docs", str(tmp))
    with __import__("force.registry", fromlist=["locked"]).locked(cockpit.paths.home) as reg:
        reg.get("portal").extra["shares"] = {"carol": "r", "daniel": "w"}
        reg.get("docs").extra["shares"] = {"bob": "r"}
    shares_dir = cockpit.paths.state / "shares"
    shares_dir.mkdir(parents=True, exist_ok=True)
    (shares_dir / "portal.carol.json").write_text(json.dumps({"pid": 123, "mode": "r"}))
    monkeypatch.setattr("force.daemon._pid_is_live_proxy", lambda pid: True)
    assert cli.shares_command(cockpit) == 0
    out = capsys.readouterr().out
    assert "session" in out and "user" in out and "proxy" in out
    assert "portal" in out and "docs" in out
    assert "carol" in out and "daniel" in out and "bob" in out
    assert "read-only" in out and "read/write" in out
    assert "yes" in out and "no" in out


def test_parser_share_subcommands():
    p = cli.build_parser()
    assert p.parse_args(["share", "portal", "--user", "carol"]).command == "share"
    assert p.parse_args(["share", "portal", "--user", "carol", "--write"]).write is True
    assert p.parse_args(["unshare", "portal", "--user", "carol"]).command == "unshare"
    assert p.parse_args(["unshare", "portal"]).user is None
    assert p.parse_args(["shares"]).command == "shares"
    assert p.parse_args(["share-proxy", "portal", "carol", "w"]).command == "share-proxy"


def test_share_refused_while_sharing_disabled(fakes, capsys):
    import dataclasses
    cockpit, _fake, _tmp = fakes
    cockpit.cfg = dataclasses.replace(cockpit.cfg, sharing=False)
    assert cli.share_command(cockpit, "nope", "carol", False) == 1
    assert "sharing is disabled" in capsys.readouterr().err


# --- delete ---
def test_delete_needs_yes_without_tty(fakes, capsys, monkeypatch):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "old", str(tmp), archived=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.delete_command(cockpit, "old", False, False) == 1
    assert "--yes" in capsys.readouterr().err
    assert __import__("force.registry", fromlist=["load"]).load(cockpit.paths.registry).get("old") is not None


def test_delete_prompt_no_keeps_it(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "old", str(tmp), archived=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    assert cli.delete_command(cockpit, "old", False, False) == 1
    assert __import__("force.registry", fromlist=["load"]).load(cockpit.paths.registry).get("old") is not None


def test_delete_yes_archived(fakes):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "old", str(tmp), archived=True)
    assert cli.delete_command(cockpit, "old", False, True) == 0
    assert __import__("force.registry", fromlist=["load"]).load(cockpit.paths.registry).get("old") is None


def test_delete_busy_refused_without_force(fakes, capsys, monkeypatch):
    cockpit, fake, tmp = fakes
    add_entry(cockpit.paths.home, "busy", str(tmp))
    monkeypatch.setattr(cli, "_current_state_for", lambda _c, _n: cli.State.WORKING)
    assert cli.delete_command(cockpit, "busy", False, True) == 1
    assert "--force" in capsys.readouterr().err


def test_delete_unknown(fakes, capsys):
    cockpit, fake, tmp = fakes
    assert cli.delete_command(cockpit, "nope", False, True) == 1
    assert "unknown session" in capsys.readouterr().err


# --- Rust sidebar switch (SPEC §22) ---
def _fake_binary(cockpit):
    b = Path(cockpit.paths.tmux_conf).parent / "bin" / "force-sidebar"
    return b


def test_rust_sidebar_argv_none_when_not_built(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: False)
    assert cli.rust_sidebar_argv(cockpit) is None


def test_rust_sidebar_argv(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    argv = cli.rust_sidebar_argv(cockpit)
    assert argv == [str(_fake_binary(cockpit)), "--snapshot", str(cockpit.paths.snapshot), "--force-bin", cockpit.cockpit_bin,
                    "--animations", "full", "--glyphs", "auto"]



def test_sidebar_command_execs_rust_when_chosen(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setenv("TMUX_PANE", "%9")
    fake.run_out = "rust"
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    seen = {}

    def fake_execv(path, argv):
        seen["argv"] = argv
        raise SystemExit(0)
    monkeypatch.setattr(cli.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        cli.sidebar_command(cockpit)
    assert seen["argv"][0].endswith("bin/force-sidebar")




def test_header_execs_rust_when_sidebar_impl_is_rust(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.delenv("TMUX_PANE", raising=False)          # config default "rust"
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    seen = {}

    def fake_execv(path, argv):
        seen["argv"] = argv
        raise SystemExit(0)
    monkeypatch.setattr(cli.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        cli.header_command(cockpit)
    share_dir = str(Path(cockpit.paths.tmux_conf).parent)
    assert seen["argv"] == [share_dir + "/bin/force-header", "--snapshot", str(cockpit.paths.snapshot),
                            "--share-dir", share_dir, "--animations", "full", "--glyphs", "auto"]


def test_dialog_execs_rust_popup(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    seen = {}

    def fake_execv(path, argv):
        seen["argv"] = argv
        raise SystemExit(0)
    monkeypatch.setattr(cli.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        cli.dialog_command(cockpit, "rename", "portal")
    a = seen["argv"]
    assert a[0].endswith("bin/force-dialog") and a[1:3] == ["rename", "portal"]
    assert a[a.index("--tmux-socket") + 1] == cockpit.paths.tmux_socket
    assert a[a.index("--glyphs") + 1] == "auto"


@pytest.mark.parametrize("value,expected", [("rich", "rich"), ("plain", "plain"), ("auto", "auto"),
                                            ("fancy", "auto"), ("", "auto")])
def test_glyphs_flag_from_config(value, expected):
    from types import SimpleNamespace
    from force.config import Config
    assert cli._glyphs(SimpleNamespace(cfg=Config(glyphs=value))) == expected


def test_config_intro_default_true():
    from force.config import Config
    assert Config().intro is True


def test_config_intro_false_loads(fakes):
    from force.config import load_config
    cockpit, _fake, tmp = fakes
    p = tmp / "introcfg.json"
    p.write_text('{"intro": false}\n')
    assert load_config(p).intro is False
    p.write_text('{"staleAfterHours": 5}\n')
    assert load_config(p).intro is True


# --- startup intro (SPEC §31) ------------------------------------------------
def _intro_binary(cockpit):
    return Path(cockpit.paths.tmux_conf).parent / "bin" / "force-intro"


def test_intro_argv_none_when_not_built(fakes, monkeypatch):
    cockpit, _fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: False)
    assert cli.intro_argv(cockpit) is None


def test_intro_argv_exact(fakes, monkeypatch):
    cockpit, _fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    monkeypatch.setattr(cli.os, "environ", {"TERM": "xterm-256color", "LANG": "en_US.UTF-8"})
    argv = cli.intro_argv(cockpit)
    assert argv == [str(_intro_binary(cockpit)), "--animations", "full", "--color", "truecolor",
                  "--fade-to", cli.THEME_BG, "--handoff"]


def test_intro_argv_animations_follows_config(fakes, monkeypatch):
    from types import SimpleNamespace
    from force.config import Config
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    monkeypatch.setattr(cli.os, "environ", {})
    cockpit, _fake, tmp = fakes
    cockpit.cfg = Config(animations="subtle", intro=True)
    assert cli.intro_argv(cockpit)[cli.intro_argv(cockpit).index("--animations") + 1] == "subtle"


@pytest.mark.parametrize("env,expected", [
    ({"TERM": "xterm-256color", "LANG": "en_US.UTF-8"}, "truecolor"),
    ({"TERM": "xterm-kitty", "LANG": "C.UTF-8"}, "truecolor"),
    ({"TERM": "alacritty", "LC_ALL": "en_US.utf8"}, "truecolor"),
    ({"TERM": "screen-256color", "LANG": "UTF-8"}, "auto"),
    ({"TERM": "xterm-256color", "LANG": "C"}, "auto"),
    ({"TERM": "xterm", "LC_ALL": "C.UTF-8", "LANG": "C"}, "truecolor"),
    ({}, "auto"),
])
def test_intro_color(env, expected):
    assert cli._intro_color(env) == expected


def test_play_intro_not_called(fakes, monkeypatch):
    import dataclasses
    cockpit, _fake, tmp = fakes
    called = []
    monkeypatch.setattr(cli, "subprocess", type("S", (), {"call": lambda a: called.append(a) or 0}))
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    cli.play_intro(cockpit, True)                       # no_intro
    cockpit.cfg = dataclasses.replace(cockpit.cfg, intro=False)
    cli.play_intro(cockpit, False)                      # intro False
    cockpit.cfg = dataclasses.replace(cockpit.cfg, animations="off")
    cli.play_intro(cockpit, False)                      # animations off
    monkeypatch.setattr(cli.os, "access", lambda p, m: False)
    cockpit.cfg = dataclasses.replace(cockpit.cfg, animations="full")
    cli.play_intro(cockpit, False)                      # binary missing
    assert called == []


@pytest.mark.parametrize("rc", [0, 1, 143])
def test_play_intro_returns_nonctrl(fakes, monkeypatch, rc):
    cockpit, _fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    monkeypatch.setattr(cli, "subprocess", type("S", (), {"call": lambda a: rc}))
    assert cli.play_intro(cockpit, False) is None


def test_play_intro_ctrl_c_raises(fakes, monkeypatch):
    cockpit, _fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    monkeypatch.setattr(cli, "subprocess", type("S", (), {"call": lambda a: 130}))
    with pytest.raises(KeyboardInterrupt):
        cli.play_intro(cockpit, False)


def test_play_intro_oserror_returns(fakes, monkeypatch):
    cockpit, _fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    monkeypatch.setattr(cli, "subprocess", None)

    def boom(a):
        raise OSError("no binary")
    monkeypatch.setattr(cli, "subprocess", type("S", (), {"call": boom}))
    assert cli.play_intro(cockpit, False) is None


def test_parser_no_intro():
    p = cli.build_parser()
    assert p.parse_args(["--no-intro"]).no_intro is True
    assert getattr(p.parse_args(["attach", "--no-intro"]), "no_intro") is True
    assert getattr(p.parse_args(["attach", "name", "--no-intro"]), "no_intro") is True
    assert getattr(p.parse_args(["attach"]), "no_intro", False) is False


def test_attach_plays_intro_before_execvp(fakes, monkeypatch):
    import dataclasses
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "portal", str(tmp))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    cockpit.cfg = dataclasses.replace(cockpit.cfg, animations="full")
    order = []
    monkeypatch.setattr(cli, "play_intro", lambda *a: order.append("play_intro"))
    monkeypatch.setattr(cli.os, "execvp", lambda *a: order.append("execvp"))
    cli.attach_command(cockpit, None)
    assert order == ["play_intro", "execvp"]



def test_intro_cell_aspect_only_when_terminal_does_not_report_pixels(fakes, monkeypatch):
    """VTE (Tilix) reports no pixel size, so the intro would guess 2.0; the config's value is passed then,
    and never over a size the terminal does report (kitty)."""
    from force.config import Config
    cockpit, _fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    monkeypatch.setattr(cli.os, "environ", {})
    cockpit.cfg = Config(intro_cell_aspect=2.2)
    monkeypatch.setattr(cli, "_tty_reports_pixels", lambda: False)
    assert cli.intro_argv(cockpit)[-2:] == ["--cell-aspect", "2.2"]
    monkeypatch.setattr(cli, "_tty_reports_pixels", lambda: True)
    assert "--cell-aspect" not in cli.intro_argv(cockpit)
    cockpit.cfg = Config()                                   # 0 = unset: the intro's own guess
    monkeypatch.setattr(cli, "_tty_reports_pixels", lambda: False)
    assert "--cell-aspect" not in cli.intro_argv(cockpit)


def test_attach_registers_the_client_glyph_tier(fakes, monkeypatch):
    """`force --glyphs rich` (client kit) records "<tty>=rich" in @force_client_glyphs for tier.sh; entries of
    ttys without a client go, and `auto` drops this tty's own entry."""
    from force.tmux import Client
    cockpit, fake, tmp = fakes
    opts = {"@force_client_glyphs": "/dev/pts/3=plain /dev/pts/9=rich /dev/pts/5=rich"}
    monkeypatch.setattr(fake, "get_option", lambda k, default=None: opts.get(k), raising=False)
    monkeypatch.setattr(fake, "set_option", lambda k, v, target=None: opts.__setitem__(k, v), raising=False)
    fake.clients_out = [Client("/dev/pts/3", "a", 80, 24, 1)]
    monkeypatch.setattr(cli.os, "ttyname", lambda fd: "/dev/pts/5")
    cli._register_client_glyphs(cockpit, "rich")
    assert opts["@force_client_glyphs"] == "/dev/pts/3=plain /dev/pts/5=rich"
    cli._register_client_glyphs(cockpit, "auto")
    assert opts["@force_client_glyphs"] == "/dev/pts/3=plain"


def test_attach_client_args_parse():
    p = cli.build_parser()
    a = p.parse_args(["--glyphs", "rich", "--cell-aspect", "2.2"])
    assert (a.command, a.glyphs, a.cell_aspect) == (None, "rich", 2.2)
    a = p.parse_args(["attach", "portal", "--glyphs", "plain"])
    assert (a.command, a.glyphs, a.name) == ("attach", "plain", "portal")
    with pytest.raises(SystemExit):
        p.parse_args(["--cell-aspect", "9"])


def test_intro_cell_aspect_from_the_client_beats_the_config(fakes, monkeypatch):
    from force.config import Config
    cockpit, _fake, tmp = fakes
    monkeypatch.setattr(cli.os, "access", lambda p, m: True)
    monkeypatch.setattr(cli.os, "environ", {})
    monkeypatch.setattr(cli, "_tty_reports_pixels", lambda: False)
    cockpit.cfg = Config(intro_cell_aspect=2.2)
    assert cli.intro_argv(cockpit, 2.05)[-2:] == ["--cell-aspect", "2.05"]


# --- client glyph tier and cell aspect (SPEC §28, §41) ------------------------
def test_client_glyphs_explicit_stays(tmp_path, monkeypatch):
    assert cli._client_glyphs("rich") == "rich"
    assert cli._client_glyphs("plain") == "plain"
    assert cli._client_glyphs("bogus") == "auto"


def test_client_glyphs_rich_on_a_desktop_with_the_symbols_font(tmp_path, monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(cli.subprocess, "run",
                      lambda *a, **k: type("R", (), {"stdout": "DejaVu Sans Mono\nSymbols Nerd Font Mono\n"})())
    assert cli._client_glyphs("auto") == "rich"


def test_client_glyphs_auto_over_ssh(tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 22 5.6.7.8 22")
    monkeypatch.setattr(cli.subprocess, "run",
                      lambda *a, **k: type("R", (), {"stdout": "Symbols Nerd Font Mono\n"})())
    assert cli._client_glyphs("auto") == "auto"


def test_client_glyphs_auto_when_fc_list_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no fc-list")))
    assert cli._client_glyphs("auto") == "auto"


def test_client_glyphs_auto_when_no_symbol_family(tmp_path, monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(cli.subprocess, "run",
                      lambda *a, **k: type("R", (), {"stdout": "DejaVu Sans Mono\nLiberation Mono\n"})())
    assert cli._client_glyphs("auto") == "auto"


@pytest.mark.parametrize("reply,rows,cols,want", [
    (b"\x1b[4;528;800t\x1b[?62;c", 24, 80, 2.2),     # VTE: reported px, cells -> 2.2
    (b"no da1 reply", 24, 80, None),                        # no 14t reply
    (b"\x1b[4;garbage;x", 24, 80, None),                # garbage
    (b"\x1b[4;200;80t\x1b[?62;c", 24, 80, None),     # aspect = 8.3, out of range
    (b"\x1b[4;0;800t\x1b[?62;c", 24, 80, None),      # zero px
])
def test_parse_cell_aspect(reply, rows, cols, want):
    assert cli._parse_cell_aspect(reply, rows, cols) == want


@pytest.mark.parametrize("argv", [["sidebar"], ["header"], ["dialog", "new"], ["dialog", "rename", "x"],
                                  ["run", "x"], ["daemon"], ["kinds"], ["opencode-update", "--check"]])
def test_internal_subcommands_parse(argv):
    """The panes and popups run these: a missing subparser kills every header (happened 2026-10-20)."""
    a = cli.build_parser().parse_args(argv)
    assert a.command == argv[0]


# --- move to another folder (SPEC §7.7) ---
def test_move_dispatches_up_down_and_folders(fakes, monkeypatch):
    cockpit, _fake, tmp = fakes
    calls = []
    monkeypatch.setattr(cli, "move_command", lambda c, n, d: calls.append(("order", n, d)) or 0)
    monkeypatch.setattr(cli, "move_folder_command", lambda c, n, d: calls.append(("folder", n, d)) or 0)
    for args in (["move", "a", "up"], ["move", "a", "down"], ["move", "a", str(tmp)], ["move", "a", "./up"]):
        assert cli.main(args) == 0
    assert calls == [("order", "a", "up"), ("order", "a", "down"), ("folder", "a", str(tmp)), ("folder", "a", "./up")]


def test_move_folder_moves_an_idle_session(fakes, monkeypatch, capsys):
    cockpit, _fake, tmp = fakes
    dest = tmp / "dest"
    dest.mkdir()
    add_entry(cockpit.paths.home, "a", str(tmp))
    monkeypatch.setattr(cli, "_current_state_for", lambda _c, _n: cli.State.IDLE)
    moved = []
    monkeypatch.setattr(cockpit, "move", lambda n, d: moved.append((n, d)))
    assert cli.move_folder_command(cockpit, "a", str(dest) + "/") == 0
    assert moved == [("a", str(dest))]
    assert "moved to" in capsys.readouterr().out


def test_move_folder_waits_for_a_busy_session(fakes, monkeypatch, capsys):
    cockpit, _fake, tmp = fakes
    dest = tmp / "dest"
    dest.mkdir()
    add_entry(cockpit.paths.home, "a", str(tmp))
    monkeypatch.setattr(cli, "_current_state_for", lambda _c, _n: cli.State.WORKING)
    monkeypatch.setattr(cockpit, "move", lambda n, d: pytest.fail("moved while busy"))
    assert cli.move_folder_command(cockpit, "a", str(dest)) == 0
    assert _load_reg(cockpit).get("a").extra["pending_move"] == str(dest)
    assert "when idle" in capsys.readouterr().out


def test_move_folder_errors(fakes, monkeypatch, capsys):
    cockpit, _fake, tmp = fakes
    add_entry(cockpit.paths.home, "a", str(tmp))
    add_entry(cockpit.paths.home, "old", str(tmp), archived=True)
    monkeypatch.setattr(cockpit, "move", lambda n, d: pytest.fail("must not move"))
    assert cli.move_folder_command(cockpit, "a", str(tmp / "missing")) == 1
    assert cli.move_folder_command(cockpit, "ghost", str(tmp)) == 1
    assert cli.move_folder_command(cockpit, "old", str(tmp)) == 1
    assert cli.move_folder_command(cockpit, "a", str(tmp)) == 0          # already there: nothing to do
    err = capsys.readouterr().err
    assert "no such folder" in err and "unknown session" in err and "archived session" in err


def test_run_passes_the_pinned_memory_dir(fakes):
    cockpit, fake, tmp = fakes
    from force.registry import locked
    add_entry(cockpit.paths.home, "portal", str(tmp))
    with locked(cockpit.paths.home) as reg:
        reg.get("portal").extra["memory_dir"] = "/m/memory"
    captured = {}
    import force.cli as c
    orig = c.os.execvp
    c.os.execvp = lambda *a: captured.setdefault("argv", a[1])
    try:
        c.run_command(cockpit, "portal")
    finally:
        c.os.execvp = orig
    argv = captured["argv"]
    assert json.loads(argv[argv.index("--settings") + 1])["autoMemoryDirectory"] == "/m/memory"


def test_parse_bg_reads_osc11_replies_with_st_or_bel():
    assert cli._parse_bg(b"\x1b]11;rgb:3030/0a0a/2424\x1b\\\x1b[?62;c") == "#300a24"
    assert cli._parse_bg(b"\x1b[4;528;800t\x1b]11;rgb:2323/2626/2727\x07\x1b[?6c") == "#232627"
    assert cli._parse_bg(b"\x1b]11;rgb:f/0/8\x07") == "#ff0088"
    assert cli._parse_bg(b"\x1b]11;rgb:ff/00/80\x07") == "#ff0080"
    assert cli._parse_bg(b"\x1b[?62;c") is None
    assert cli._parse_bg(b"\x1b]11;rgb:zz/00/00\x07") is None


def test_query_terminal_asks_nothing_without_a_terminal(monkeypatch):
    monkeypatch.setattr(cli.os, "isatty", lambda fd: False)
    monkeypatch.setattr(cli.os, "open", lambda *a: (_ for _ in ()).throw(AssertionError("opened the tty")))
    assert cli._query_terminal(True) == (None, None, None)


def test_parse_fg_reads_osc10_replies_beside_osc11():
    reply = b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\\x1b]11;rgb:1818/1818/1818\x1b\\\x1b[?62;c"
    assert cli._parse_fg(reply) == "#ffffff"
    assert cli._parse_bg(reply) == "#181818"
    assert cli._parse_fg(b"\x1b]11;rgb:1818/1818/1818\x07") is None


def test_query_terminal_asks_text_colour_and_background_in_one_round_trip(monkeypatch):
    asked = []
    reply = b"\x1b]10;rgb:ffff/ffff/ffff\x07\x1b]11;rgb:1818/1818/1818\x07\x1b[?62;c"
    monkeypatch.setattr(cli, "_ask_terminal", lambda req: asked.append(req) or (reply, 45, 160))
    assert cli._query_terminal(False) == (None, "#181818", "#ffffff")
    assert asked == [b"\x1b]10;?\x1b\\\x1b]11;?\x1b\\"]


def _run_client_sh(tmp_path, argv, rc):
    fake = tmp_path / "tmux"
    fake.write_text(f"#!/bin/sh\necho \"client $*\"\nexit {rc}\n")
    fake.chmod(0o755)
    return subprocess.run(argv, capture_output=True, env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}"))


RESET = b"\x1b[?1049l\x1b[?25h\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\x1b[0m"


def test_exec_tmux_clean_exit_leaves_the_terminal_alone(monkeypatch, tmp_path):
    """A clean client exit: no reset, no background (none was painted), the status kept."""
    calls = []
    monkeypatch.setattr(cli.os, "execvp", lambda f, a: calls.append((f, a)))
    cli._exec_tmux(["tmux", "-L", "s", "attach", "-t", "=x"], None)
    (f, argv), = calls
    assert f == "sh" and argv[:2] == ["sh", "-c"] and argv[3:] == ["force", "-L", "s", "attach", "-t", "=x"]
    assert os.environ["FORCE_TERM_BG"] == "" and os.environ["FORCE_TERM_FG"] == ""
    r = _run_client_sh(tmp_path, argv, 0)
    assert r.returncode == 0 and r.stdout == b"client -L s attach -t =x\n"


def test_exec_tmux_failed_client_resets_the_terminal(monkeypatch, tmp_path):
    """The server went away under the client (exit 1): leave the alternate screen and turn the modes off."""
    calls = []
    monkeypatch.setattr(cli.os, "execvp", lambda f, a: calls.append((f, a)))
    cli._exec_tmux(["tmux", "-L", "s", "attach", "-t", "=x"], None)
    (_, argv), = calls
    r = _run_client_sh(tmp_path, argv, 1)
    assert r.returncode == 1 and r.stdout == b"client -L s attach -t =x\n" + RESET


def test_exec_tmux_restores_the_terminal_background_after_the_client(monkeypatch, tmp_path):
    """sh waits for the client, then puts the terminal's own background back and keeps the client's status."""
    calls = []
    monkeypatch.setattr(cli.os, "execvp", lambda f, a: calls.append((f, a)))
    monkeypatch.setenv("FORCE_TERM_BG", "")
    cli._exec_tmux(["tmux", "-L", "s", "attach", "-t", "=x"], "rgb:3030/0a0a/2424")
    (f, argv), = calls
    assert f == "sh" and argv[:2] == ["sh", "-c"] and argv[3:] == ["force", "-L", "s", "attach", "-t", "=x"]
    assert os.environ["FORCE_TERM_BG"] == "rgb:3030/0a0a/2424"
    r = _run_client_sh(tmp_path, argv, 3)
    assert r.returncode == 3
    assert r.stdout == b"client -L s attach -t =x\n" + RESET + b"\x1b]11;rgb:3030/0a0a/2424\x07"


def test_exec_tmux_restores_the_terminal_text_colour_too(monkeypatch, tmp_path):
    """SPEC §45 text colour: the terminal's own text colour comes back after the client, before its background."""
    calls = []
    monkeypatch.setattr(cli.os, "execvp", lambda f, a: calls.append((f, a)))
    cli._exec_tmux(["tmux", "-L", "s", "attach", "-t", "=x"], "#181818", "#ffffff")
    (_, argv), = calls
    assert os.environ["FORCE_TERM_FG"] == "#ffffff"
    r = _run_client_sh(tmp_path, argv, 0)
    assert r.returncode == 0
    assert r.stdout == b"client -L s attach -t =x\n\x1b]10;#ffffff\x07\x1b]11;#181818\x07"


def test_paint_terminal_only_what_the_terminal_told_its_own(monkeypatch):
    written = []
    monkeypatch.setattr(cli.os, "write", lambda fd, b: written.append((fd, b)))
    monkeypatch.setattr(cli, "_ask_terminal", lambda req: pytest.fail("asked without painting a text colour"))
    assert cli._paint_terminal(None, None) is False
    assert written == []
    assert cli._paint_terminal("rgb:3030/0a0a/2424", None) is False
    assert written == [(1, b"\x1b]11;#262626\x1b\\")]


@pytest.mark.parametrize("answer, own", [
    (b"\x1b]10;rgb:d0d0/d0d0/d0d0\x1b\\\x1b[?62;c", True),       # took it
    (b"\x1b]10;rgb:cfcf/cfcf/cfcf\x1b\\\x1b[?62;c", True),       # took it, reported rounded (urxvt)
    (b"\x1b]10;rgb:caca/caca/caca\x1b\\\x1b[?62;c", False),      # something else
    (b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\\x1b[?62;c", False),      # answers, but kept its own (as Tilix does its bg)
    (b"\x1b[?62;c", False),                                          # no answer the second time
])
def test_paint_terminal_text_colour_counts_only_once_the_terminal_reports_it(monkeypatch, answer, own):
    """SPEC §45: a ui window may leave the text colour to the terminal only when the terminal, asked again after
    being set, reports the theme's."""
    written, asked = [], []
    monkeypatch.setattr(cli.os, "write", lambda fd, b: written.append((fd, b)))
    monkeypatch.setattr(cli, "_ask_terminal", lambda req: asked.append(req) or (answer, 45, 160))
    assert cli._paint_terminal("#181818", "#ffffff") is own
    assert written == [(1, b"\x1b]11;#262626\x1b\\\x1b]10;#d0d0d0\x1b\\")]
    assert asked == [b"\x1b]10;?\x1b\\"]


def _attach_ui_fakes(monkeypatch, paint=False, build_fails=False):
    """_attach_ui with the terminal, the ui session and the client faked: what it decided and did."""
    got = {"paint": [], "restore": [], "build": [], "exec": []}
    monkeypatch.setattr(cli.os, "get_terminal_size", lambda fd: (160, 45))
    monkeypatch.setattr(cli, "_paint_terminal", lambda bg, fg: got["paint"].append((bg, fg)) or paint)
    monkeypatch.setattr(cli, "_restore_terminal", lambda bg, fg: got["restore"].append((bg, fg)))

    def build(c, target, name, cols, rows, remote, glyphs, own_fg=False):
        if build_fails:
            raise cli.TmuxError("no ui server")
        got["build"].append(own_fg)
        return name
    monkeypatch.setattr(cli, "build_ui_session", build)
    for f in ("_register_client_glyphs", "play_intro", "_spawn_welcome"):
        monkeypatch.setattr(cli, f, lambda *a, **k: None)
    monkeypatch.setattr(cli, "_exec_tmux", lambda argv, bg, fg=None: got["exec"].append((bg, fg)))
    return got


def test_attach_ui_leaves_the_text_colour_to_a_terminal_that_took_the_theme_s(fakes, monkeypatch):
    cockpit, _fake, _tmp = fakes
    got = _attach_ui_fakes(monkeypatch, paint=True)
    assert cli._attach_ui(cockpit, "s1", "plain", True, None, term_bg="#181818", term_fg="#ffffff") == 0
    assert got["paint"] == [("#181818", "#ffffff")] and got["build"] == [True]
    assert got["exec"] == [("#181818", "#ffffff")]             # both put back after the client


def test_attach_ui_keeps_the_ui_text_colour_where_the_terminal_did_not_take_it(fakes, monkeypatch):
    cockpit, _fake, _tmp = fakes
    got = _attach_ui_fakes(monkeypatch, paint=False)
    assert cli._attach_ui(cockpit, "s1", "plain", True, None, term_bg="#181818", term_fg="#ffffff") == 0
    assert got["build"] == [False]
    got = _attach_ui_fakes(monkeypatch, paint=False)
    assert cli._attach_ui(cockpit, "s1", "plain", True, None, term_bg=None, term_fg=None) == 0   # mosh, PuTTY
    assert got["paint"] == [(None, None)] and got["build"] == [False]


def test_attach_ui_a_terminal_already_in_the_theme_s_text_colour(fakes, monkeypatch):
    cockpit, _fake, _tmp = fakes
    got = _attach_ui_fakes(monkeypatch, paint=False)
    assert cli._attach_ui(cockpit, "s1", "plain", True, None, term_bg="#181818", term_fg="#d0d0d0") == 0
    assert got["paint"] == [] and got["build"] == [True]
    assert got["exec"] == [("#181818", None)]                  # nothing of its text colour to put back


def test_attach_ui_puts_the_terminal_back_when_the_ui_does_not_start(fakes, monkeypatch):
    cockpit, _fake, _tmp = fakes
    got = _attach_ui_fakes(monkeypatch, paint=True, build_fails=True)
    assert cli._attach_ui(cockpit, "s1", "plain", True, None, term_bg="#181818", term_fg="#ffffff") == 2
    assert got["restore"] == [("#181818", "#ffffff")] and got["exec"] == []


# --- _start_host: no-systemd fallback (SPEC §1) -----------------------------
def test_start_host_no_systemd_starts_server_and_spawns_daemon(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli, "_systemd_usable", lambda any_units=False: False)
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: calls.append(a[0] if a else []) or 0)
    monkeypatch.setattr(cli, "_daemon_lock_held", lambda c: False)
    spawned = []
    monkeypatch.setattr(cli, "_start_daemon", lambda c: spawned.append(c))
    n = {"v": 0}

    def server_running():
        n["v"] += 1
        return n["v"] >= 2

    monkeypatch.setattr(fake, "server_running", server_running)
    assert cli._start_host(cockpit) is True
    start = [c for c in calls if c and "start-server" in c]
    assert start, "tmux start-server was called"
    argv = start[0]
    assert argv[argv.index("-L") + 1] == cockpit.paths.tmux_socket
    assert argv[argv.index("-f") + 1] == str(cockpit.paths.tmux_conf)
    assert len(spawned) == 1


def test_start_host_no_systemd_lock_held_skips_daemon(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli, "_systemd_usable", lambda any_units=False: False)
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "_daemon_lock_held", lambda c: True)
    spawned = []
    monkeypatch.setattr(cli, "_start_daemon", lambda c: spawned.append(c))
    n = {"v": 0}

    def server_running():
        n["v"] += 1
        return n["v"] >= 2

    monkeypatch.setattr(fake, "server_running", server_running)
    assert cli._start_host(cockpit) is True
    assert spawned == []


def test_start_host_systemd_starts_units(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli, "_systemd_usable", lambda any_units=False: True)
    calls = []
    # is-active returns 1 (not active) so the units are started
    monkeypatch.setattr(cli.subprocess, "call",
                      lambda *a, **k: calls.append(a[0] if a else []) or (1 if "is-active" in (a[0] or []) else 0))
    assert cli._start_host(cockpit) is True
    assert any(c and c[0] == "systemctl" and c[1] == "--user" and c[2] == "start" for c in calls)


# --- first run: a shell session instead of the prompt (SPEC §2) ---------------
def test_attach_first_run_creates_shell_session(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli, "_systemd_usable", lambda any_units=False: False)
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "_daemon_lock_held", lambda c: True)
    monkeypatch.setattr(cli, "play_intro", lambda *a: None)
    monkeypatch.setattr(cli, "_register_client_glyphs", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_paint_terminal", lambda *a, **k: False)
    from force.registry import load as _load

    def raise_exit(*a, **k):
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_exec_tmux", raise_exit)
    with pytest.raises(SystemExit):
        cli.attach_command(cockpit, None)
    e = _load(cockpit.paths.registry).get("shell")
    assert e is not None and e.extra.get("kind") == "shell"


def test_attach_welcome_spawned_once(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli, "_systemd_usable", lambda any_units=False: False)
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "_daemon_lock_held", lambda c: True)
    monkeypatch.setattr(cli.os, "ttyname", lambda fd: "/dev/pts/99")
    spawned = []

    class P:
        def __init__(self, *a, **k):
            spawned.append(a)

    monkeypatch.setattr(cli.subprocess, "Popen", P)
    monkeypatch.setattr(cli, "_client_glyphs", lambda g: g)   # no fc-list through the fake Popen
    monkeypatch.setattr(cli, "_exec_tmux", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_register_client_glyphs", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_paint_terminal", lambda *a, **k: False)
    cli.attach_command(cockpit, None)
    assert len(spawned) == 1
    assert "display-message" in spawned[0][0][2]
    # the marker was written: a second attach does not spawn again
    cli.attach_command(cockpit, None)
    assert len(spawned) == 1


# --- force new --kind claude when Claude Code is not installed (SPEC §2) ------
def test_new_claude_fails_when_not_installed(fakes, monkeypatch, capsys):
    import dataclasses
    cockpit, fake, tmp = fakes
    cockpit.paths = dataclasses.replace(cockpit.paths, claude_bin=tmp / "no-claude")
    monkeypatch.setattr(cli.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(cli.os, "access", lambda p, m: False)
    assert cli.create_session(cockpit, "x", str(tmp), kind="claude") == 1
    err = capsys.readouterr().err
    assert "Claude Code is not installed" in err
    assert "curl -fsSL https://claude.ai/install.sh | bash" in err


def test_new_shell_still_works_when_claude_absent(fakes, monkeypatch):
    import dataclasses
    cockpit, fake, tmp = fakes
    cockpit.paths = dataclasses.replace(cockpit.paths, claude_bin=tmp / "no-claude")
    monkeypatch.setattr(cli.os, "access", lambda p, m: False)
    assert cli.create_session(cockpit, "sh", str(tmp), kind="shell") == 0
    from force.registry import load as _load
    assert _load(cockpit.paths.registry).get("sh").extra.get("kind") == "shell"


# --- force uninstall (SPEC §5) --------------------------------------------
def test_uninstall_refuses_without_yes_non_tty(fakes, monkeypatch, capsys):
    cockpit, fake, tmp = fakes
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.uninstall_command(cockpit, False, False) == 1
    assert "run force uninstall --yes to confirm" in capsys.readouterr().err


def test_uninstall_no_systemd_removes_path_blocks_and_keeps_data(fakes, monkeypatch, capsys):
    cockpit, fake, tmp = fakes
    monkeypatch.setenv("HOME", str(tmp))
    bin_dir = tmp / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "force"
    launcher.write_text("#!/bin/sh\nfrom force.cli import main\n")
    launcher.chmod(0o755)
    bashrc = tmp / ".bashrc"
    bashrc.write_text("# other line\n# force: ~/.local/bin on PATH (also for ssh -t HOST force)\n"
                    'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac\n'
                    "# force end\n# keep me\n")
    zshenv = tmp / ".zshenv"
    zshenv.write_text("# z\n# force: ~/.local/bin on PATH (also for ssh -t HOST force)\n"
                     'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac\n'
                     "# force end\n")
    fish = tmp / ".config" / "fish" / "conf.d" / "force.fish"
    fish.parent.mkdir(parents=True)
    fish.write_text("fish_add_path -g $HOME/.local/bin\n")
    data = cockpit.paths.home
    for name in ("app", "releases", "python", "opencode"):
        (data / name).mkdir(parents=True, exist_ok=True)
    (data / "registry.json").write_text("{}")
    monkeypatch.setattr(cli, "_systemd_usable", lambda any_units=False: False)
    killed = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **k: killed.append(list(argv)) or 0)
    monkeypatch.setattr(cli, "_stop_daemon", lambda c: None)
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", "https://example.com/base")
    assert cli.uninstall_command(cockpit, False, True) == 0
    remaining = bashrc.read_text()
    assert "force:" not in remaining and "case" not in remaining
    assert "# other line" in remaining and "# keep me" in remaining
    zs = zshenv.read_text()
    assert "force:" not in zs
    assert not fish.exists()
    assert not launcher.exists()
    assert not (data / "app").exists()
    assert not (data / "releases").exists()
    assert not (data / "python").exists()
    assert not (data / "opencode").exists()
    assert (data / "registry.json").exists(), "registry is kept without --purge"
    out = capsys.readouterr().out
    assert "kept your data" in out
    assert "curl -fsSL https://example.com/base/install.sh | sh" in out


def test_uninstall_systemd_disables_and_removes_units(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setenv("HOME", str(tmp))
    user = tmp / ".config" / "systemd" / "user"
    user.mkdir(parents=True)
    svc = user / "force.service"
    svc.write_text("x")
    timer = user / "force-smoke.timer"
    timer.write_text("x")
    calls = []
    monkeypatch.setattr(cli, "_systemd_usable", lambda any_units=False: True)
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **k: calls.append(list(argv)) or 0)
    assert cli.uninstall_command(cockpit, False, True) == 0
    assert not svc.exists()
    assert not timer.exists()
    assert any(c and c[0] == "systemctl" and "disable" in c and "--now" in c for c in calls)
    assert any(c and c[0] == "systemctl" and "daemon-reload" in c for c in calls)
    assert any(c and c[:2] == ["tmux", "-L"] and c[-1] == "kill-server" for c in calls), "ui server killed too"


def test_uninstall_purge_removes_everything(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setenv("HOME", str(tmp))
    data = cockpit.paths.home
    data.mkdir(parents=True, exist_ok=True)
    (data / "registry.json").write_text("{}")
    cockpit.paths.state.mkdir(parents=True, exist_ok=True)
    conf = cockpit.paths.config_file.parent
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "marker").write_text("y")
    launcher = tmp / ".local" / "bin"
    launcher.mkdir(parents=True)
    (launcher / "force").write_text("from force.cli import main")
    monkeypatch.setattr(cli, "_systemd_usable", lambda any_units=False: False)
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **k: 0)
    monkeypatch.setattr(cli, "_stop_daemon", lambda c: None)
    assert cli.uninstall_command(cockpit, True, True) == 0
    assert not data.exists()
    assert not cockpit.paths.state.exists()
    assert not conf.exists()


def test_uninstall_keeps_foreign_launcher(fakes, monkeypatch):
    cockpit, fake, tmp = fakes
    monkeypatch.setenv("HOME", str(tmp))
    bin_dir = tmp / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "force"
    launcher.write_text("#!/bin/sh\n# some other tool, not force\n")
    monkeypatch.setattr(cli, "_systemd_usable", lambda any_units=False: False)
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **k: 0)
    monkeypatch.setattr(cli, "_stop_daemon", lambda c: None)
    assert cli.uninstall_command(cockpit, False, True) == 0
    assert launcher.exists(), "a launcher that is not Force's is left alone"


def test_client_glyphs_ignores_linux_fonts_on_wsl(monkeypatch):
    # on WSL fc-list lists Linux's fonts, but Windows Terminal draws with Windows' own: stay auto (tier.sh -> plain)
    for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "Symbols Nerd Font Mono\n", ""))
    monkeypatch.setattr(cli.config, "is_wsl", lambda: False)
    assert cli._client_glyphs("auto") == "rich"
    monkeypatch.setattr(cli.config, "is_wsl", lambda: True)
    assert cli._client_glyphs("auto") == "auto"
    assert cli._client_glyphs("rich") == "rich"                           # `force settings set icons on` still wins
