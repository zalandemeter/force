"""SPEC §56.1 part 1: the `force-link` program builds exactly today's ssh argv (golden against remote.ssh_argv),
and force/remote.py's link_argv prefers it when the release ships its binary."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from force import remote

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
