"""Remote hosts (SPEC §40): force watch, the watchers, forwarding, remotes file, a ui window for a remote."""
import dataclasses
import io
import json
import os
import sys
import threading
import time
import uuid

from force import cli, remote
from force.config import get_paths
from force.registry import locked


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class Lines(io.StringIO):
    """A writer that stops the watch loop after `n` lines."""
    def __init__(self, n):
        super().__init__()
        self.n = n

    def write(self, s):
        super().write(s)
        if self.getvalue().count("\n") >= self.n:
            raise BrokenPipeError
        return len(s)


def test_watch_emits_hello_then_snapshots(env):
    paths = env.paths
    paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    paths.snapshot.write_text(json.dumps({"generated_ms": 1, "sessions": [{"name": "s1"}]}))
    out = Lines(2)
    assert remote.watch(paths, "rel-1", "split", interval=0.01, out=out) == 0
    hello, snap = [json.loads(l) for l in out.getvalue().splitlines()]
    assert hello["hello"]["proto"] == remote.PROTO and hello["hello"]["version"] == "rel-1"
    assert hello["hello"]["ui"] == "split"
    assert snap["snapshot"]["sessions"][0]["name"] == "s1"
    assert snap["registry"] == {"sessions": []}


def test_watch_sends_again_on_change(env):
    paths = env.paths
    paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    paths.snapshot.write_text(json.dumps({"generated_ms": 1, "sessions": []}))
    out = Lines(3)

    def change():
        time.sleep(0.2)
        paths.snapshot.write_text(json.dumps({"generated_ms": 2, "sessions": [{"name": "x"}]}))
    threading.Thread(target=change, daemon=True).start()
    remote.watch(paths, None, "classic", interval=0.02, heartbeat=60, out=out)
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert lines[2]["snapshot"]["generated_ms"] == 2


def test_remotes_file_roundtrip(env):
    paths = env.paths
    assert remote.load(paths) == []
    remote.save(paths, [remote.Remote("devbox", "devbox"), remote.Remote("buildbox", "alice@buildbox", False)])
    rs = remote.load(paths)
    assert [(r.name, r.ssh, r.enabled) for r in rs] == [("devbox", "devbox", True), ("buildbox", "alice@buildbox", False)]
    assert remote.get(paths, "buildbox").ssh == "alice@buildbox"
    assert remote.remotes_file(paths).name == "hosts.json"
    remote.remotes_file(paths).write_text("not json {{{")
    assert remote.load(paths) == []


def test_ssh_argv_quotes_but_keeps_home(env):
    r = remote.Remote("n", "host")
    argv = remote.ssh_argv(env.paths, r, "~/.local/bin/force", "rename", "a b", "c")
    assert argv[0] == "ssh" and argv[-2] == "host"
    assert argv[-1] == "~/.local/bin/force rename 'a b' c"
    assert "ControlMaster=auto" in argv and "BatchMode=yes" in argv
    assert remote.ssh_argv(env.paths, r, "true", tty=True)[-3] == "-t"
    # a ~/ path with a space stays one word (the remote shell still expands ~)
    assert remote.ssh_argv(env.paths, r, "dirs", "~/my dir", "p")[-1] == "dirs ~/'my dir' p"
    assert remote.ssh_argv(env.paths, r, "x", "~/")[-1] == "x ~/"


def _fake_ssh(monkeypatch, tmp_path, script):
    """ssh_argv -> run `script` (python) locally instead of ssh."""
    f = tmp_path / "fake_ssh.py"
    f.write_text(script)
    monkeypatch.setattr(remote, "ssh_argv", lambda paths, r, *a, tty=False: [sys.executable, str(f), *a])


def test_watcher_writes_files_and_status(env, monkeypatch, tmp_path):
    _fake_ssh(monkeypatch, tmp_path, f"""
import json, sys, time
print(json.dumps({{"hello": {{"proto": {remote.PROTO}, "version": "r9", "host": "h"}}}}), flush=True)
print(json.dumps({{"snapshot": {{"generated_ms": 5, "sessions": [{{"name": "s1"}}]}}, "registry": {{"sessions": [{{"name": "s1"}}]}}}}), flush=True)
time.sleep(0.5)
""")
    paths = env.paths
    remote.save(paths, [remote.Remote("h", "h")])
    ws = remote.Watchers(paths)
    ws.tick()
    d = remote.remote_dir(paths, "h")
    assert wait_for(lambda: (d / "snapshot.json").exists())
    snap = json.loads((d / "snapshot.json").read_text())
    assert snap["remote_generated_ms"] == 5                     # stamped with the local receive time
    assert abs(snap["generated_ms"] - time.time() * 1000) < 10000
    assert json.loads((d / "registry.json").read_text())["sessions"][0]["name"] == "s1"
    assert wait_for(lambda: remote.read_status(paths, "h").get("version") == "r9")
    # the connection ends: offline, and a later tick reconnects only after the pause
    assert wait_for(lambda: remote.read_status(paths, "h")["state"] == "offline")
    w = ws.by_name["h"]
    assert w.next_try > time.monotonic() - 1
    ws.stop()


def test_watcher_rejects_another_protocol(env, monkeypatch, tmp_path):
    _fake_ssh(monkeypatch, tmp_path, """
import json, time
print(json.dumps({"hello": {"proto": 999, "version": "old"}}), flush=True)
print(json.dumps({"snapshot": {"generated_ms": 1, "sessions": []}}), flush=True)
time.sleep(5)
""")
    paths = env.paths
    remote.save(paths, [remote.Remote("h", "h")])
    ws = remote.Watchers(paths)
    ws.tick()
    assert wait_for(lambda: remote.read_status(paths, "h")["state"] == "version")
    assert "force host upgrade h" in remote.read_status(paths, "h")["detail"]
    assert not (remote.remote_dir(paths, "h") / "snapshot.json").exists()
    ws.stop()


def test_watchers_drop_removed_remotes(env, monkeypatch, tmp_path):
    _fake_ssh(monkeypatch, tmp_path, "import time; time.sleep(30)")
    paths = env.paths
    remote.save(paths, [remote.Remote("h", "h"), remote.Remote("off", "x", False)])
    ws = remote.Watchers(paths)
    ws.tick()
    assert set(ws.by_name) == {"h"} and ws.by_name["h"].alive()
    proc = ws.by_name["h"].proc
    remote.save(paths, [remote.Remote("h", "h", False)])
    ws.tick()
    assert ws.by_name == {} and proc.poll() is not None
    assert remote.read_status(paths, "h")["state"] == "disabled"     # not shown as connected any more


def test_dirs_lists_matching_subfolders(env, monkeypatch, capsys):
    home = env.tmp / "home2"
    home.mkdir()
    (home / "proj-a").mkdir()
    (home / "proj-b").mkdir()
    (home / "pz").mkdir()
    (home / "proj-file").write_text("x")
    (home / "proj-link").symlink_to(home / "proj-a", target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))

    # `dirs ~ proj`: subdirectories (incl. a dir symlink) matching proj, not the file.
    assert cli.dirs_command("~", "proj") == 0
    assert capsys.readouterr().out.splitlines() == ["proj-a", "proj-b", "proj-link"]

    # empty DIR (and a relative DIR) is the home directory.
    assert cli.dirs_command("", "p") == 0
    assert capsys.readouterr().out.splitlines() == ["proj-a", "proj-b", "proj-link", "pz"]

    # a missing dir is not an error: no output, exit 0.
    assert cli.dirs_command("~/no-such-dir", "x") == 0
    assert capsys.readouterr().out == ""

    # dialogs' dirs must be forwarded to the remote host the ui shows.
    assert "dirs" in cli._FORWARDED


def test_forward_runs_session_commands_on_the_remote(env, monkeypatch):
    remote.save(env.paths, [remote.Remote("devbox", "devbox")])
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **k: calls.append(argv) or 0)
    monkeypatch.setenv("FORCE_HOST", "devbox")
    assert cli._forward(["restart", "portal"]) == 0
    assert calls[-1][-1] == "~/.local/bin/force restart portal"
    assert cli._forward(["switch", "--index", "2", "--client", "/dev/pts/7"]) == 0
    assert calls[-1][-1].endswith("switch --index 2 --client /dev/pts/7")
    assert cli._forward(["sidebar"]) is None            # ui commands stay here
    assert cli._forward(["dialog", "rename", "x"]) is None
    monkeypatch.setenv("FORCE_HOST", "")
    assert cli._forward(["restart", "portal"]) is None
    monkeypatch.setenv("FORCE_HOST", "ghost")
    assert cli._forward(["restart", "portal"]) == 1


def test_host_files_follow_force_host(env, monkeypatch):
    cockpit = env.make_cockpit()
    monkeypatch.setenv("FORCE_HOST", "")
    assert cli._host_files(cockpit) == (cockpit.paths.snapshot, cockpit.paths.registry)
    monkeypatch.setenv("FORCE_HOST", "devbox")
    snap, reg = cli._host_files(cockpit)
    assert snap == cockpit.paths.state / "remotes" / "devbox" / "snapshot.json"
    assert reg.name == "registry.json"


def test_remote_command_add_list_remove(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    runs = []

    class R:
        def __init__(self, rc=0, out="split"):
            self.returncode, self.stdout, self.stderr = rc, out, ""
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: runs.append(argv) or R())
    assert cli.remote_command(cockpit, "add", "devbox", "devbox", False) == 0
    assert [r.name for r in remote.load(cockpit.paths)] == ["devbox"]
    assert cli.remote_command(cockpit, "add", "devbox", "devbox", False) == 1      # exists
    assert cli.remote_command(cockpit, "add", "bad name", "x", False) == 1
    assert cli.remote_command(cockpit, "list", None, None, False) == 0
    assert "devbox" in capsys.readouterr().out
    remote.write_status(cockpit.paths, "devbox", "connected")
    assert cli.remote_command(cockpit, "remove", "devbox", None, False) == 0
    assert remote.load(cockpit.paths) == []
    assert not remote.remote_dir(cockpit.paths, "devbox").exists()     # its copies go too


def test_remote_add_switches_a_classic_remote_to_split(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    runs = []

    class R:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ""
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: runs.append(argv) or R("classic"))
    assert cli.remote_command(cockpit, "add", "b", "buildbox", False) == 0
    assert any(a[-1].endswith("force ui split") for a in runs)
    out = capsys.readouterr().out
    assert "b switched to split mode" in out
    assert "this machine switched to split mode" in out and "split" in json.loads(cockpit.paths.config_file.read_text())["layout"]
    assert cli.ui_command(cockpit, "classic") == 1                # not while remotes exist


def test_ui_session_for_a_remote(env):
    cockpit = env.make_cockpit()
    cockpit.cfg = dataclasses.replace(cockpit.cfg, ui="split")
    r = remote.Remote("devbox", "devbox")
    name = cli.build_ui_session(cockpit, "portal", "ui-r", 120, 30, r)
    ui = cockpit.ui_tmux
    main = next(p for p in ui.panes() if p.session == name and p.role == "agent")
    opt = lambda k: ui.run("show", "-wqv", "-t", main.pane_id, k)
    assert opt("@force_host") == "devbox"
    assert opt("@force_nested") == ""                    # named once the remote side reports it
    # the remote's own tmux (asked over ssh; "tmux" when it cannot be asked) on its live server
    prefix = "devbox tmux -L force"
    assert opt("@force_host_tmux").startswith("ssh ") and opt("@force_host_tmux").endswith(prefix)
    assert "FORCE_HOST=devbox" in ui.run("show-environment", "-t", "=" + name, "FORCE_HOST")
    start = ui.run("display", "-p", "-t", main.pane_id, "#{pane_start_command}")
    assert "--remote" in start and "devbox" in start and "portal" in start


def test_default_remote_and_target(env):
    cockpit = env.make_cockpit()
    assert cli._default_remote(cockpit) is None
    remote.save(cockpit.paths, [remote.Remote("a", "a"), remote.Remote("b", "b")])
    assert cli._default_remote(cockpit) == "a"
    cli._remember_host(cockpit, "b")
    assert cli._default_remote(cockpit) == "b"
    d = remote.remote_dir(cockpit.paths, "b")
    d.mkdir(parents=True)
    (d / "snapshot.json").write_text(json.dumps({"sessions": [
        {"name": "x", "index": 1, "viewed": True}, {"name": "y", "index": 2, "viewed": False},
        {"name": "old", "index": 3, "archived": True}]}))
    assert cli._remote_target(cockpit, remote.get(cockpit.paths, "b")) == "y"


def test_ui_host_refuses_an_offline_remote(env):
    cockpit = env.make_cockpit()
    cockpit.cfg = dataclasses.replace(cockpit.cfg, ui="split")
    remote.save(cockpit.paths, [remote.Remote("n", "n")])
    remote.write_status(cockpit.paths, "n", "offline", "connection ended")
    cockpit.ui_tmux.run("new-session", "-d", "-s", "ui-x", "sleep 100")
    win = cockpit.ui_tmux.run("display", "-p", "-t", "=ui-x:", "#{window_id}")
    assert cli.ui_host_command(cockpit, "n", win) == 0
    assert cockpit.ui_tmux.run("display", "-p", "-t", "=ui-x:", "#{pane_start_command}").strip('"') == "sleep 100"
    assert cli.ui_host_command(cockpit, "ghost", win) == 0     # unknown: a note, nothing changes


def test_ui_host_local_without_sessions_starts_a_shell(env, monkeypatch):
    cockpit = env.make_cockpit()
    cockpit.ui_tmux.run("new-session", "-d", "-s", "ui-c", "sleep 100")
    win = cockpit.ui_tmux.run("display", "-p", "-t", "=ui-c:", "#{window_id}")
    assert cli.ui_host_command(cockpit, "local", win) == 0      # classic here: a note, no session made
    assert cli.registry.load(cockpit.paths.registry).live() == []
    cockpit.cfg = dataclasses.replace(cockpit.cfg, ui="split")
    monkeypatch.setattr(cli, "_start_host", lambda c: c.tmux.run("start-server", ";", "set", "-g", "exit-empty", "off") or True)
    cockpit.ui_tmux.run("new-session", "-d", "-s", "ui-y", "sleep 100")
    win = cockpit.ui_tmux.run("display", "-p", "-t", "=ui-y:", "#{window_id}")
    built = []
    monkeypatch.setattr(cli, "build_ui_session", 
                        lambda c, target, name, *a: built.append(target) or c.ui_tmux.run("new-session", "-d", "-s", name, "sleep 100")
                        or c.ui_tmux.run("kill-session", "-t", "=ui-y"))   # as destroy-unattached does on the move
    ui, real, notes = cockpit.ui_tmux, cockpit.ui_tmux.run, []
    monkeypatch.setattr(ui, "run", lambda *a: notes.append(a[-1]) if a[0] == "display-message" else real(*a))
    assert cli.ui_host_command(cockpit, "local", win) == 0
    live = cli.registry.load(cockpit.paths.registry).live()
    assert [(e.name, e.extra.get("kind")) for e in live] == [("shell", "shell")]
    assert built == ["shell"]
    assert notes == []                                         # no "cannot switch host"


def test_dialog_acts_on_the_session_shown_now(env, monkeypatch):
    """A rename/archive/delete/move dialog asks the host which session the nested client shows: the ui
    window's @force_session can still name the one shown before a switch."""
    cockpit = env.make_cockpit()
    remote.save(cockpit.paths, [remote.Remote("n", "n")])
    ran = []
    monkeypatch.setattr(cli, "rust_dialog_argv", lambda c, kind, name: [kind] + ([name] if name else []))
    monkeypatch.setattr(cli, "_exec_rust", lambda argv, what: ran.append(argv) or 0)
    monkeypatch.setenv("FORCE_HOST", "n")
    monkeypatch.setenv("FORCE_CLIENT", "/dev/pts/4")
    monkeypatch.setattr(cli, "_shown_session", lambda c, host, client: "zz-rt")
    assert cli.dialog_command(cockpit, "rename", "host") == 0
    assert "zz-rt" in ran[-1] and "host" not in ran[-1]
    assert os.environ["FORCE_SESSION"] == "zz-rt"
    # the remote does not answer: no dialog on a name that may be stale
    monkeypatch.setattr(cli, "_shown_session", lambda c, host, client: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.dialog_command(cockpit, "delete", "host") == 1 and len(ran) == 1
    # dialogs without a session (jump) are not held up
    assert cli.dialog_command(cockpit, "jump", None) == 0 and len(ran) == 2


def test_shown_session_asks_the_remote(env, monkeypatch, tmp_path):
    cockpit = env.make_cockpit()
    remote.save(cockpit.paths, [remote.Remote("n", "n")])
    seen = []
    _fake_ssh(monkeypatch, tmp_path, "import sys; print('zz-rt')")
    real = remote.ssh_argv
    monkeypatch.setattr(remote, "ssh_argv", lambda paths, r, *a, tty=False: seen.append(a) or real(paths, r, *a))
    assert cli._shown_session(cockpit, "n", "/dev/pts/4") == "zz-rt"
    assert seen[-1][-2:] == ("/dev/pts/4", "#{client_session}")
    assert cli._shown_session(cockpit, "ghost", "/dev/pts/4") is None
    assert cli._shown_session(cockpit, "n", "") is None


def test_attention_note(tmp_path):
    f = tmp_path / "snapshot.json"
    assert cli._attention_note(f) == ""
    f.write_text(json.dumps({"sessions": [{"state": "NEEDS_YOU"}, {"state": "NEEDS_YOU"}, {"state": "YOUR_TURN"},
                                          {"state": "NEEDS_YOU", "archived": True}, {"state": "WORKING"}]}))
    assert cli._attention_note(f) == " · 2 need you, 1 finished"
    f.write_text(json.dumps({"sessions": [{"state": "IDLE"}]}))
    assert cli._attention_note(f) == ""


def test_watch_ends_when_the_release_changes(env):
    paths = env.paths
    rel = paths.home / "releases"
    (rel / "r1").mkdir(parents=True)
    (rel / "r2").mkdir()
    os.symlink(rel / "r1", paths.home / "app")
    out = io.StringIO()

    def upgrade():
        time.sleep(0.2)
        os.unlink(paths.home / "app")
        os.symlink(rel / "r2", paths.home / "app")
    threading.Thread(target=upgrade, daemon=True).start()
    assert remote.watch(paths, "r1", "split", interval=0.02, out=out) == 0
    assert json.loads(out.getvalue().splitlines()[0])["hello"]["version"] == "r1"


def test_attention_target(tmp_path):
    f = tmp_path / "snapshot.json"
    assert cli._attention_target(f) is None
    f.write_text(json.dumps({"sessions": [
        {"name": "a", "index": 1, "state": "YOUR_TURN"}, {"name": "b", "index": 3, "state": "NEEDS_YOU"},
        {"name": "c", "index": 2, "state": "NEEDS_YOU"}, {"name": "d", "index": 0, "state": "NEEDS_YOU", "archived": True}]}))
    assert cli._attention_target(f) == "c"
    f.write_text(json.dumps({"sessions": [{"name": "a", "index": 1, "state": "IDLE"},
                                          {"name": "e", "index": 4, "state": "YOUR_TURN"}]}))
    assert cli._attention_target(f) == "e"


def test_install_to_fails_fast_when_the_host_is_unreachable(env, monkeypatch):
    """ssh ending without reading the release (host down) must not leave tar blocked on the pipe."""
    cockpit = env.make_cockpit()
    rel = cockpit.paths.home / "releases" / "r1"
    rel.mkdir(parents=True)
    (rel / "force").mkdir(parents=True, exist_ok=True)
    (rel / "force" / "setup.py").write_text("")
    (rel / "blob").write_bytes(os.urandom(4 * 1024 * 1024))     # more than a pipe buffer, incompressible
    (cockpit.paths.home / "app").symlink_to(rel)
    monkeypatch.setattr(cli.remote_mod, "ssh_argv", lambda *a, **k: ["sh", "-c", "exit 255"])
    t0 = time.monotonic()
    assert cli.install_to_command(cockpit, "nowhere") == 1
    assert time.monotonic() - t0 < 20


def test_hosts_menu_lists_hosts_and_hints_without_remotes(env, monkeypatch):
    cockpit = env.make_cockpit()
    calls = []
    real = cockpit.ui_tmux.run

    def run(*args):
        if args and args[0] == "display-menu":
            calls.append(args)
            return ""
        if args and args[0] == "display":
            return ""
        return real(*args)
    monkeypatch.setattr(cockpit.ui_tmux, "run", run)
    assert cli.hosts_menu_command(cockpit, "/dev/pts/1", "@1") == 0
    items = calls[-1]
    assert items[1] == "-O"                       # opened by a click: stays open when the button is released
    assert any("this machine" in str(a) for a in items)
    assert "-add a host: force host add NAME [SSH]" in items
    remote.save(cockpit.paths, [remote.Remote("buildbox", "buildbox")])
    cli.hosts_menu_command(cockpit, "/dev/pts/1", "@1")
    items = calls[-1]
    assert any(str(a).startswith("  ") and "buildbox" in str(a) for a in items)
    assert not any("add a remote" in str(a) for a in items)


def test_remote_target_tells_empty_from_unreachable(env, monkeypatch):
    cockpit = env.make_cockpit()
    remote.save(cockpit.paths, [remote.Remote("b", "b")])
    r = remote.get(cockpit.paths, "b")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no ssh")))
    assert cli._remote_target(cockpit, r) is None                  # no snapshot and ssh fails: cannot ask
    d = remote.remote_dir(cockpit.paths, "b")
    d.mkdir(parents=True)
    (d / "snapshot.json").write_text(json.dumps({"sessions": [{"name": "old", "archived": True}]}))
    assert cli._remote_target(cockpit, r) == ""                    # answered: no live sessions


def test_remote_shell_starts_a_shell_session_there(env, monkeypatch):
    cockpit = env.make_cockpit()
    r = remote.Remote("b", "b")
    seen = []

    class Done:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "", ""
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: seen.append(argv) or Done(0))
    assert cli._remote_shell(cockpit, r) == "shell"
    assert seen[-1][-1].endswith(f"{remote.REMOTE_FORCE} new shell --kind shell --no-switch")
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: Done(1))
    assert cli._remote_shell(cockpit, r) is None


def test_ui_host_on_an_empty_remote_starts_a_shell_there(env, monkeypatch):
    cockpit = env.make_cockpit()
    cockpit.cfg = dataclasses.replace(cockpit.cfg, ui="split")
    remote.save(cockpit.paths, [remote.Remote("n", "n")])
    remote.write_status(cockpit.paths, "n", "connected", "N")
    d = remote.remote_dir(cockpit.paths, "n")
    d.mkdir(parents=True, exist_ok=True)
    (d / "snapshot.json").write_text(json.dumps({"sessions": []}))
    ui, real, notes, built = cockpit.ui_tmux, cockpit.ui_tmux.run, [], []
    monkeypatch.setattr(ui, "run", lambda *a: notes.append(a) if a[0] == "display-message" else real(*a))
    ui.run("new-session", "-d", "-s", "ui-e", "sleep 100")
    win = ui.run("display", "-p", "-t", "=ui-e:", "#{window_id}")
    # the shell cannot be started: a note, nothing rebuilt
    monkeypatch.setattr(cli, "_remote_shell", lambda c, r: None)
    monkeypatch.setattr(cli, "build_ui_session", lambda c, target, name, *a: built.append(target))
    assert cli.ui_host_command(cockpit, "n", win) == 0
    assert built == [] and "cannot start a shell session" in notes[-1][-1]
    # it starts: the window shows it
    monkeypatch.setattr(cli, "_remote_shell", lambda c, r: "shell")
    monkeypatch.setattr(cli, "build_ui_session",
                        lambda c, target, name, *a: built.append(target) or real("new-session", "-d", "-s", name, "sleep 100"))
    notes.clear()
    assert cli.ui_host_command(cockpit, "n", win) == 0
    assert built == ["shell"] and notes == []


def test_ui_host_notes_go_to_the_window_s_terminal(env, monkeypatch):
    cockpit = env.make_cockpit()
    ui, real, notes = cockpit.ui_tmux, cockpit.ui_tmux.run, []
    monkeypatch.setattr(ui, "run", lambda *a: notes.append(a) if a[0] == "display-message" else real(*a))
    ui.run("new-session", "-d", "-s", "ui-n", "sleep 100")
    win = ui.run("display", "-p", "-t", "=ui-n:", "#{window_id}")
    assert cli.ui_host_command(cockpit, "ghost", win) == 0
    assert notes[-1][:1] == ("display-message",) and "-c" not in notes[-1]     # no terminal: tmux picks one
    Client = type("Client", (), {})
    a, b = Client(), Client()
    a.name, a.session, b.name, b.session = "/dev/pts/7", "ui-n", "/dev/pts/8", "other"
    monkeypatch.setattr(ui, "clients", lambda: [a, b])
    notes.clear()
    assert cli.ui_host_command(cockpit, "ghost", win) == 0
    assert [n[1:3] for n in notes] == [("-c", "/dev/pts/7")]


def test_hosts_menu_offers_a_new_shell_on_an_empty_remote(env, monkeypatch):
    cockpit = env.make_cockpit()
    calls, real = [], cockpit.ui_tmux.run
    monkeypatch.setattr(cockpit.ui_tmux, "run", lambda *a: calls.append(a) or "" if a[0] in ("display-menu", "display")
                        else real(*a))
    remote.save(cockpit.paths, [remote.Remote("e", "e"), remote.Remote("f", "f")])
    for n, sessions in (("e", []), ("f", [{"name": "x", "state": "NEEDS_YOU"}])):
        remote.write_status(cockpit.paths, n, "connected", n)
        d = remote.remote_dir(cockpit.paths, n)
        d.mkdir(parents=True, exist_ok=True)
        (d / "snapshot.json").write_text(json.dumps({"sessions": sessions}))
    cli.hosts_menu_command(cockpit, "/dev/pts/1", "@1")
    items = [str(a) for a in calls[-1]]
    assert any("e#[fg=#8a8a8a] (new shell)" in str(a) for a in items)
    assert any("f#[fg=#8a8a8a] · 1 need you" in str(i) for i in items)


def test_remote_tmux_is_asked_once_and_kept(tmp_path, monkeypatch):
    from force import config
    paths = config.get_paths({"HOME": str(tmp_path), "FORCE_HOME": str(tmp_path / "h"),
                              "FORCE_STATE": str(tmp_path / "s"), "FORCE_CONFIG": str(tmp_path / "c.json")})
    r = remote.Remote("box", "me@box")
    calls = []

    class Out:
        returncode, stdout = 0, "/home/me/.local/share/force/app/share/tmux-bin/tmux\n"

    run = lambda argv, **k: calls.append(argv) or Out()
    assert remote.remote_tmux(paths, r, run) == "/home/me/.local/share/force/app/share/tmux-bin/tmux"
    assert remote.remote_tmux(paths, r, run) == "/home/me/.local/share/force/app/share/tmux-bin/tmux"
    assert len(calls) == 1 and calls[0][-2] == "me@box" and "tmux-bin/tmux" in calls[0][-1]

    class Fail:
        returncode, stdout = 255, ""

    assert remote.remote_tmux(paths, remote.Remote("other", "x"), lambda a, **k: Fail()) == "tmux"
