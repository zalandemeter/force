"""The link daemon where no service manager runs Force (SPEC §46, §56.2): Force starts it itself (`force`, the
daemon's tick), restarts it on an update, stops it on `force uninstall` and with directLink off. Its lock holds its
pid. Locks here are real flocks on tmp files and the one process stopped is this test's own child; spawning
`force link-daemon` is faked. No service manager is ever reached."""
import fcntl
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from force import cli, config, daemon as daemon_mod, link, services
from force.config import Config


@pytest.fixture
def paths(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    share = tmp_path / "app" / "share"
    share.mkdir(parents=True)
    return config.Paths(tmp_path / "home", state, tmp_path / "claude", tmp_path / "claude", "sock",
                        share / "tmux.conf", tmp_path / "settings.json")


def _binary(paths):
    b = Path(paths.tmux_conf).parent / "bin" / "force-link"
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)
    return b


def _hold(paths, pid_text="4242\n"):
    """Hold the link lock as a running daemon would (an flock on its own open file), with `pid_text` in it."""
    lock = link.lock_file(paths)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(pid_text)
    f = open(lock, "r+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


class Popen:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return types.SimpleNamespace(pid=1)


# --- the release has it ------------------------------------------------------------------------------------
def test_release_has_link(tmp_path):
    rel = tmp_path / "rel"
    (rel / "share" / "bin").mkdir(parents=True)
    (rel / "force").mkdir()
    assert not link.release_has_link(rel)
    b = rel / "share" / "bin" / "force-link"
    b.write_text("#!/bin/sh\n")
    assert not link.release_has_link(rel)            # not executable
    b.chmod(0o755)
    assert not link.release_has_link(rel)            # an A-era release: force-link, but no link-daemon command
    (rel / "force" / "link.py").write_text("")
    assert link.release_has_link(rel)


# --- the lock and the pid ------------------------------------------------------------------------------------
def test_daemon_pid_none_without_a_lock_or_when_it_is_free(paths):
    assert link.daemon_pid(paths) is None
    assert not link.lock_file(paths).exists()        # looking never creates it
    link.lock_file(paths).parent.mkdir(parents=True)
    link.lock_file(paths).write_text("4242\n")       # a daemon that has ended: its pid stays, the lock is free
    assert link.daemon_pid(paths) is None


def test_daemon_pid_is_the_one_in_the_held_lock(paths):
    f = _hold(paths, "4242\n")
    try:
        assert link.daemon_pid(paths) == 4242
    finally:
        f.close()
    assert link.daemon_pid(paths) is None


def test_daemon_pid_without_a_pid_finds_the_process_by_its_command_line(paths, monkeypatch):
    f = _hold(paths, "")
    me = os.getuid()
    procs = {
        10: (me, ["/x/force-link", "daemon", "--state", "/other", "--port", "1"]),
        11: (me + 1, ["/x/force-link", "daemon", "--state", str(paths.state)]),      # another user's
        12: (me, ["/x/force-link", "exec", "--state", str(paths.state)]),
        13: (me, ["/x/force-link", "daemon", "--state", str(paths.state), "--port", "47823"]),
    }
    monkeypatch.setattr("force.procinfo.pids", lambda: sorted(procs))
    monkeypatch.setattr("force.procinfo.uid", lambda pid: procs[pid][0])
    monkeypatch.setattr("force.procinfo.cmdline", lambda pid: procs[pid][1])
    try:
        assert link.daemon_pid(paths) == 13
    finally:
        f.close()


# --- start ----------------------------------------------------------------------------------------------------
def test_start_daemon_spawns_force_link_daemon_detached(paths, monkeypatch):
    _binary(paths)
    p = Popen()
    monkeypatch.setattr(link.subprocess, "Popen", p)
    assert link.start_daemon(paths, Config(), "/home/u/.local/bin/force") is True
    (argv, kw), = p.calls
    assert argv == ["/home/u/.local/bin/force", "link-daemon"]
    assert kw["start_new_session"] is True and kw["stdin"] == subprocess.DEVNULL
    assert Path(kw["stdout"].name) == paths.state / "link.log"


@pytest.mark.parametrize("why", ["off", "no binary", "running"])
def test_start_daemon_does_nothing(paths, monkeypatch, why):
    p = Popen()
    monkeypatch.setattr(link.subprocess, "Popen", p)
    cfg = Config(direct_link=(why != "off"))
    if why != "no binary":
        _binary(paths)
    held = _hold(paths) if why == "running" else None
    try:
        assert link.start_daemon(paths, cfg, "/l/force") is False
    finally:
        if held:
            held.close()
    assert p.calls == []


# --- stop -----------------------------------------------------------------------------------------------------
_HOLDER = """
import fcntl, os, sys, time
f = open(sys.argv[1], "a+")
fcntl.flock(f, fcntl.LOCK_EX)
f.truncate(0); f.write(f"{os.getpid()}\\n"); f.flush()
print("held", flush=True)
time.sleep(60)
"""


def test_stop_daemon_terminates_the_lock_holder(paths):
    link.lock_file(paths).parent.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, "-c", _HOLDER, str(link.lock_file(paths))], stdout=subprocess.PIPE,
                             text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        assert link.daemon_pid(paths) == child.pid
        assert link.stop_daemon(paths) is True
        assert child.wait(timeout=5) == -15
        assert link.daemon_pid(paths) is None
    finally:
        if child.poll() is None:
            child.kill()
        child.stdout.close()


def test_stop_daemon_with_none_running(paths):
    assert link.stop_daemon(paths) is True


# --- setup without a service manager: restart on the new release -------------------------------------------
def test_restart_unmanaged(paths, monkeypatch):
    seq = []
    monkeypatch.setattr(link, "stop_daemon", lambda p: seq.append("stop"))
    monkeypatch.setattr(link, "start_daemon", lambda p, c, l: seq.append(("start", l)) or True)
    monkeypatch.setattr(link, "daemon_pid", lambda p: None)
    assert link.restart_unmanaged(paths, Config(), "/l/force", True) == "none"
    assert seq == []                                 # not running: `force` starts it later
    monkeypatch.setattr(link, "daemon_pid", lambda p: 4242)
    assert link.restart_unmanaged(paths, Config(), "/l/force", True) == "restarted"
    assert seq == ["stop", ("start", "/l/force")]
    seq.clear()
    assert link.restart_unmanaged(paths, Config(), "/l/force", False) == "stopped"
    assert seq == ["stop"]


def test_forget_local(paths):
    (paths.state / "link").mkdir()
    (paths.state / "link" / "key").write_bytes(b"k" * 32)
    link.sock_file(paths).write_text("")
    (paths.state / "link.log").write_text("kept\n")
    link.forget_local(paths)
    assert not (paths.state / "link").exists() and not link.sock_file(paths).exists()
    assert (paths.state / "link.log").exists()
    link.forget_local(paths)                         # nothing there: no error


# --- `force` without a service manager starts it; never for an instance ------------------------------------
def test_start_link_from_the_cockpit(monkeypatch):
    got = []
    monkeypatch.setattr(link, "start_daemon", lambda p, c, l: got.append((p, l)) or True)
    monkeypatch.setattr(cli, "_launcher", lambda: "/l/force")
    cockpit = types.SimpleNamespace(paths="P", cfg=Config())
    monkeypatch.delenv("FORCE_INSTANCE", raising=False)
    cli._start_link(cockpit)
    assert got == [("P", "/l/force")]
    monkeypatch.setenv("FORCE_INSTANCE", "dev")
    cli._start_link(cockpit)
    assert len(got) == 1


def test_start_host_without_systemd_starts_the_link_too(monkeypatch):
    started = []
    monkeypatch.setattr(cli, "_services_installed", lambda any_units=False: False)
    monkeypatch.setattr(cli, "_daemon_lock_held", lambda c: True)
    monkeypatch.setattr(cli, "_start_link", lambda c: started.append(c))
    tmux = types.SimpleNamespace(server_running=lambda: True)
    cockpit = types.SimpleNamespace(tmux=tmux)
    assert cli._start_host(cockpit) is True
    assert started == [cockpit]


def test_start_host_with_systemd_leaves_the_link_to_it(monkeypatch):
    started = []
    monkeypatch.setattr(cli, "_services_installed", lambda any_units=False: True)
    monkeypatch.setattr(cli.services, "is_active", lambda name, run=None: True)
    monkeypatch.setattr(cli, "_start_link", lambda c: started.append(c))
    cockpit = types.SimpleNamespace(tmux=types.SimpleNamespace(server_running=lambda: True))
    assert cli._start_host(cockpit) is True
    assert started == []


# --- the settings: directLink / linkPort without a service manager ------------------------------------------
def test_link_setting_without_a_manager_restarts_the_own_daemon(tmp_path, monkeypatch):
    seq = []
    monkeypatch.delenv("FORCE_INSTANCE", raising=False)
    monkeypatch.setattr(services, "apply_link", lambda on, run=None: None)
    monkeypatch.setattr(link, "stop_daemon", lambda p: seq.append("stop"))
    monkeypatch.setattr(link, "start_daemon", lambda p, c, l: seq.append(("start", c.direct_link)))
    f = tmp_path / "settings.json"
    f.write_text('{"directLink": false}')
    cli._apply_link_setting(f, "directLink")
    assert seq == ["stop", ("start", False)]         # start_daemon itself refuses with directLink off
    seq.clear()
    cli._apply_link_setting(f, "animations")
    assert seq == []
    monkeypatch.setattr(services, "apply_link", lambda on, run=None: "systemd")
    cli._apply_link_setting(f, "linkPort")
    assert seq == []                                 # the unit is the manager's


# --- the daemon's tick keeps it running without a service manager -------------------------------------------
class _D:
    _LINK_EVERY = daemon_mod.Daemon._LINK_EVERY
    _step_link = daemon_mod.Daemon._step_link

    def __init__(self, paths, cfg=None):
        self.paths = paths
        self.cfg = cfg or Config()
        self.cockpit = types.SimpleNamespace(cockpit_bin="/l/force")


def _tick_env(monkeypatch, managed=False, installed=False):
    got = []
    monkeypatch.delenv("FORCE_INSTANCE", raising=False)
    monkeypatch.setattr(services, "under_service_manager", lambda: managed)
    monkeypatch.setattr(services, "installed", lambda any_units=False: installed)
    monkeypatch.setattr(link, "start_daemon", lambda p, c, l: got.append(l) or True)
    return got


def test_daemon_tick_starts_the_link_daemon_every_30_s_at_most(paths, monkeypatch):
    got = _tick_env(monkeypatch)
    _binary(paths)
    d = _D(paths)
    d._step_link()
    d._step_link()                                   # throttled
    assert got == ["/l/force"]
    d._link_at -= 31
    d._step_link()
    assert got == ["/l/force", "/l/force"]


@pytest.mark.parametrize("case", ["managed", "installed", "off", "no binary", "running", "instance"])
def test_daemon_tick_leaves_the_link_daemon(paths, monkeypatch, case):
    got = _tick_env(monkeypatch, managed=case == "managed", installed=case == "installed")
    if case == "instance":
        monkeypatch.setenv("FORCE_INSTANCE", "dev")
    if case != "no binary":
        _binary(paths)
    held = _hold(paths) if case == "running" else None
    try:
        _D(paths, Config(direct_link=case != "off"))._step_link()
    finally:
        if held:
            held.close()
    assert got == []
