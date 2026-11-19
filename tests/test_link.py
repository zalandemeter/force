"""SPEC §56.1 part 1: the `force-link` program builds exactly today's ssh argv (golden against remote.ssh_argv),
and force/remote.py's link_argv prefers it when the release ships its binary. Every caller that reaches a remote
goes through the link; adding/installing/updating a host stays on plain ssh."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import types

import pytest

from force import cli, remote

ROOT = Path(__file__).resolve().parent.parent
CARGO = shutil.which("cargo") or os.path.expanduser("~/.cargo/bin/cargo")

pytestmark = pytest.mark.skipif(not os.path.exists(CARGO), reason="no cargo")

ARGV_CASES = [
    ["force", "list", "--json"],
    ["~/.local/bin/force", "watch"],
    ["~/"],
    ["tmux", "-L", "force", "display", "-p", "#{session_name}"],
    ["sh", "-c", 'cat "$HOME/x"'],
    ["a b", "it's", ""],
    ["é"],
    ["--", "-x"],
]


@pytest.fixture(scope="module")
def link_bin(tmp_path_factory):
    """Build force-link once per session and return the path to the binary."""
    target = tmp_path_factory.mktemp("linktarget")
    env = dict(os.environ, CARGO_TARGET_DIR=str(target))
    subprocess.run([CARGO, "build", "-q", "--manifest-path", str(ROOT / "crates/link/Cargo.toml")],
                  check=True, env=env, cwd=ROOT)
    return target / "debug" / "force-link"


def _hosts_with_devbox(env):
    hosts_file = remote.remotes_file(env.paths)
    hosts_file.parent.mkdir(parents=True, exist_ok=True)
    hosts_file.write_text(json.dumps({"hosts": [{"name": "devbox", "ssh": "devbox"}]}))
    return hosts_file


def _link_exec(link_bin, env, *argv, tty=False, host="devbox", hosts=None, state=None):
    cmd = [str(link_bin), "exec", "--state", state or str(env.paths.state), "--hosts",
           hosts or str(remote.remotes_file(env.paths)), host]
    if tty:
        cmd.append("--tty")
    cmd.append("--print")
    cmd.append("--")
    cmd.extend(argv)
    return subprocess.run(cmd, capture_output=True, text=True)


def test_exec_print_matches_ssh_argv(env, link_bin):
    _hosts_with_devbox(env)
    r = remote.load(env.paths)[0]
    for argv in ARGV_CASES:
        for tty in (False, True):
            p = _link_exec(link_bin, env, *argv, tty=tty)
            assert p.returncode == 0, (argv, tty, p.stderr)
            got = json.loads(p.stdout)
            want = remote.ssh_argv(env.paths, r, *argv, tty=tty)
            assert got == want, (argv, tty, got, want)


def test_unknown_host_no_host(env, link_bin):
    _hosts_with_devbox(env)
    p = _link_exec(link_bin, env, "true", host="missing")
    assert p.returncode == 255
    assert "force-link: no host missing" in p.stderr


def test_missing_hosts_file_no_host(env, link_bin):
    p = _link_exec(link_bin, env, "true", hosts=str(env.tmp / "nope.json"))
    assert p.returncode == 255
    assert "force-link: no host" in p.stderr


def test_usage_errors_exit_2(env, link_bin):
    for args in (
        ["exec", "--state", str(env.paths.state), "--hosts", str(remote.remotes_file(env.paths)), "devbox"],  # no --
        ["exec", "--state", str(env.paths.state), "--hosts", str(remote.remotes_file(env.paths)), "--", "x"],   # no HOST
        ["exec", "--state", str(env.paths.state), "devbox", "--", "x"],                                       # no --hosts
        ["exec", "--bogus", "x"],                                                                             # unknown flag
        [],                                                                                                    # no command
        ["nonsense"],                                                                                          # unknown command
    ):
        p = subprocess.run([str(link_bin), *args], capture_output=True, text=True)
        assert p.returncode == 2, args
        assert "usage:" in p.stderr.lower() or "usage:" in p.stdout.lower(), args


def test_version(link_bin):
    p = subprocess.run([str(link_bin), "version"], capture_output=True, text=True, check=True)
    assert p.stdout.startswith("force-link ")


def test_creates_ssh_dir_with_print(env, link_bin):
    _hosts_with_devbox(env)
    p = _link_exec(link_bin, env, "true")
    assert p.returncode == 0
    ssh = env.paths.state / "ssh"
    assert ssh.is_dir() and (ssh.stat().st_mode & 0o777) == 0o700


def test_link_argv_falls_back_to_ssh_argv(env):
    # no bin/force-link in the fake release -> plain ssh_argv (a dev checkout keeps working)
    r = remote.Remote("devbox", "devbox")
    assert remote.link_argv(env.paths, r, "force", "list", tty=True) == \
        remote.ssh_argv(env.paths, r, "force", "list", tty=True)


def test_link_argv_uses_binary_when_present(env, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "tmux.conf").write_text("")
    bin_dir = share / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "force-link"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    fake_paths = type("P", (), {
        "state": env.paths.state,
        "tmux_conf": share / "tmux.conf",
        "config_file": env.paths.config_file,
    })()
    r = remote.Remote("devbox", "devbox")
    argv = remote.link_argv(fake_paths, r, "force", "list", tty=True)
    assert argv[0] == str(stub)
    assert argv == [str(stub), "exec", "--state", str(env.paths.state), "--hosts",
                   str(remote.remotes_file(fake_paths)), "devbox", "--tty", "--", "force", "list"]


def _fake_paths(env, tmp_path, with_bin):
    """A paths-like object with a release folder that does or (with_bin False) does not ship bin/force-link."""
    share = tmp_path / "share"
    share.mkdir(exist_ok=True)
    (share / "tmux.conf").write_text("")
    stub = None
    if with_bin:
        bin_dir = share / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "force-link"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    return type("P", (), {
        "state": env.paths.state,
        "tmux_conf": share / "tmux.conf",
        "config_file": env.paths.config_file,
    })(), stub


def _tmux_cache(env, name="devbox"):
    """A cached remote tmux path, so link_tmux_prefix/host_tmux_prefix do not probe over real ssh."""
    p = remote.remote_dir(env.paths, name) / "tmux"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("/usr/bin/tmux\n")
    return "/usr/bin/tmux"


def test_link_tmux_prefix_without_binary(env):
    _tmux_cache(env)
    r = remote.Remote("devbox", "devbox")
    assert remote.link_tmux_prefix(env.paths, r) == remote.host_tmux_prefix(env.paths, r)


def test_link_tmux_prefix_uses_binary_when_present(env, tmp_path):
    tmux = _tmux_cache(env)
    fake_paths, stub = _fake_paths(env, tmp_path, with_bin=True)
    r = remote.Remote("devbox", "devbox")
    prefix = remote.link_tmux_prefix(fake_paths, r)
    assert prefix == f"{stub} exec --state {env.paths.state} --hosts {remote.remotes_file(fake_paths)} devbox -- " \
                     f"{tmux} -L {remote.REMOTE_TMUX_SOCKET}"


def _wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_watcher_uses_link_argv(env, monkeypatch, tmp_path):
    script = tmp_path / "fake_link.py"
    script.write_text("import time; time.sleep(3)\n")
    calls = []
    monkeypatch.setattr(remote, "link_argv",
                       lambda paths, r, *a, tty=False: calls.append(a) or [sys.executable, str(script)])
    remote.save(env.paths, [remote.Remote("h", "h")])
    ws = remote.Watchers(env.paths)
    ws.tick()
    assert _wait_for(lambda: ws.by_name["h"].alive())
    assert calls and calls[-1] == (remote.REMOTE_FORCE, "watch")
    ws.stop()


def test_forward_uses_link_argv_when_binary_exists(env, monkeypatch, tmp_path):
    remote.save(env.paths, [remote.Remote("devbox", "devbox")])
    fake_paths, stub = _fake_paths(env, tmp_path, with_bin=True)
    monkeypatch.setattr(cli.config, "get_paths", lambda: fake_paths)
    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, **k: calls.append(argv) or 0)
    monkeypatch.setenv("FORCE_HOST", "devbox")
    assert cli._forward(["restart", "portal"]) == 0
    assert calls[-1][0] == str(stub)
    assert calls[-1][calls[-1].index("--") + 1:] == [remote.REMOTE_FORCE, "restart", "portal"]


def test_updates_still_use_ssh_argv(env, monkeypatch):
    import force.updates as updates
    r = remote.Remote("devbox", "devbox")
    ssh_calls, link_calls = [], []
    monkeypatch.setattr(remote, "ssh_argv", lambda paths, r, *a, tty=False: ssh_calls.append(a) or ["ssh", *a])
    monkeypatch.setattr(remote, "link_argv", lambda paths, r, *a, tty=False: link_calls.append(a) or ["force-link", *a])
    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(updates.subprocess, "run", lambda argv, **k: R())
    assert updates.update_remote(env.paths, r) == (True, "done")
    assert ssh_calls and not link_calls                       # an update bootstraps Force there: plain ssh (SPEC §56.1)
    assert ssh_calls[0] == (remote.REMOTE_FORCE, "update", "--here")


def test_nested_report_prints_reads_misses_and_removes(env, capsys):
    state = env.paths.state
    d = state / "nested"
    d.mkdir(parents=True)
    (d / "id1").write_text("/dev/pts/7\n")
    assert cli.nested_report_command(state, "id1") == 0
    assert capsys.readouterr().out == "/dev/pts/7\n"
    # a missing report: exit 1, no output
    assert cli.nested_report_command(state, "nope") == 1
    assert capsys.readouterr().out == ""
    # an empty one: exit 1, no output
    (d / "empty").write_text("")
    assert cli.nested_report_command(state, "empty") == 1
    assert capsys.readouterr().out == ""
    # with --remove it deletes it (and tolerates a missing file), exit 0
    assert cli.nested_report_command(state, "id1", remove=True) == 0
    assert not (d / "id1").exists()
    assert cli.nested_report_command(state, "nope", remove=True) == 0
    # an invalid id: exit 2 with a message on stderr
    assert cli.nested_report_command(state, "bad id!") == 2
    assert "bad nested-report id" in capsys.readouterr().err
    assert cli.nested_report_command(state, "x" * 65) == 2


def test_reconnect_falls_back_to_sh_cat_on_usage_error(env, monkeypatch):
    """A remote too old for `nested-report` (usage error, exit 2) is read the old way over plain ssh for the rest
    of the run (SPEC §56.1)."""
    remote.save(env.paths, [remote.Remote("devbox", "devbox")])
    r = remote.get(env.paths, "devbox")
    child = type("C", (), {"poll": lambda self: None})()
    calls = []
    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""
    def fake_run(argv, **k):
        calls.append(argv)
        joined = " ".join(argv)
        if "nested-report" in joined and "--remove" not in joined:
            return R(2)                       # an old Force: unknown command (argparse usage error)
        if "cat" in joined:
            return R(0, "/dev/pts/7\n")
        return R(0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    ui = type("UI", (), {"run": lambda self, *a, **k: "0"})()
    monkeypatch.setattr(cli, "_signal_ui_panes", lambda ui, pane: None)
    cockpit = types.SimpleNamespace(paths=env.paths, ui_tmux=ui)
    cli._name_remote_client(cockpit, r, "rid1", "@1", child, stale_read={"old": False})
    assert _wait_for(lambda: any("cat" in " ".join(a) for a in calls))
    assert any("nested-report" in " ".join(a) for a in calls)  # tried the new command first
    assert any("rm" in " ".join(a) for a in calls)           # and removed the report the old way too
