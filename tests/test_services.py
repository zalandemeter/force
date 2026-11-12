"""Tests for force/services.py, the one place that talks to the service manager (systemd on Linux,
launchd on macOS, SPEC §51). All launchctl/systemctl calls are faked via an injected `run`; nothing real runs."""
import os
import plistlib
import sys
from pathlib import Path

import pytest

from force import services


class R:
    """Mirrors subprocess.CompletedProcess fields force/services.py reads."""
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    def __init__(self):
        self.calls = []
        self.handlers = []

    def handle(self, fn):
        self.handlers.append(fn)
        return fn

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for fn in self.handlers:
            r = fn(argv, dict(kw))
            if r is not None:
                return r
        return R()


def _launchd_env(monkeypatch):
    monkeypatch.setattr(services.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(_tmp))


_UID = os.getuid()


# --- plists: a pure generator the tests can check on any platform ------------------------------
def test_plists_have_all_fields(tmp_path):
    home = tmp_path / "home"
    data = services.plists(home)
    assert set(data) == {"cc.useforce.force-tmux", "cc.useforce.force", "cc.useforce.force-smoke"}

    tmux = plistlib.loads(data["cc.useforce.force-tmux"])
    assert tmux["Label"] == "cc.useforce.force-tmux"
    assert isinstance(tmux["ProgramArguments"], list)
    assert tmux["RunAtLoad"] is True and tmux["KeepAlive"] is True and tmux["ThrottleInterval"] == 3
    path = tmux["EnvironmentVariables"]["PATH"]
    # PATH first entry is <app>/share/tmux-bin via the stable app symlink
    assert path.split(":")[0] == f"{home}/.local/share/force/app/share/tmux-bin"
    assert ":".join(path.split(":")[1:5]) == f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin"
    assert path.endswith(":/bin")

    daemon = plistlib.loads(data["cc.useforce.force"])
    assert daemon["Label"] == "cc.useforce.force"
    assert daemon["ProgramArguments"] == [f"{home}/.local/bin/force", "daemon"]
    assert daemon["EnvironmentVariables"]["FORCE_SERVICE"] == "launchd"
    assert daemon["KeepAlive"] is True and daemon["ThrottleInterval"] == 3
    assert daemon["AbandonProcessGroup"] is True          # like systemd's KillMode=process
    log = f"{home}/.local/state/force/daemon.log"
    assert daemon["StandardOutPath"] == log and daemon["StandardErrorPath"] == log

    smoke = plistlib.loads(data["cc.useforce.force-smoke"])
    assert smoke["Label"] == "cc.useforce.force-smoke"
    assert smoke["ProgramArguments"][-2:] == ["smoke", "--if-due"]
    assert smoke["StartInterval"] == 3600
    assert smoke["RunAtLoad"] is False
    assert smoke["Nice"] == 10 and smoke["ProcessType"] == "Background"


# --- backend -------------------------------------------------------------------------------
def test_backend_systemd(monkeypatch):
    monkeypatch.setattr(services.sys, "platform", "linux")
    monkeypatch.setattr(services.shutil, "which", lambda name: "/usr/bin/systemctl")
    fr = FakeRun()
    fr.handle(lambda a, k: R(0) if a[:2] == ["systemctl", "--user"] else None)
    assert services.backend(run=fr) == "systemd"


def test_backend_launchd_and_none(monkeypatch):
    monkeypatch.setattr(services.sys, "platform", "darwin")
    fr = FakeRun()
    fr.handle(lambda a, k: R(0) if a[:2] == ["launchctl", "print"] else None)
    assert services.backend(run=fr) == "launchd"

    fr2 = FakeRun()
    fr2.handle(lambda a, k: R(1) if a[:2] == ["launchctl", "print"] else None)
    assert services.backend(run=fr2) is None


def test_the_tests_never_reach_the_live_service_manager(tmp_path, monkeypatch):
    """A test that forgets to fake the service manager must not stop the developer's own Force (one did: it ran
    `systemctl --user disable --now` on the live units). conftest refuses the managers, as on a machine without one."""
    import subprocess
    monkeypatch.setenv("HOME", str(tmp_path))        # macOS: uninstall removes plists from HOME
    for argv in (["systemctl", "--user", "show-environment"], ["/usr/bin/systemctl", "--user", "show-environment"],
                 ["launchctl", "print", "gui/0"], ["loginctl", "show-user", "nobody"], "systemctl --user status"):
        with pytest.raises(FileNotFoundError):
            subprocess.run(argv, capture_output=True, shell=isinstance(argv, str))
    assert services.backend() is None
    assert services.is_active("daemon") is False
    services.uninstall()                             # nothing to stop here


def test_backend_never_for_instance(monkeypatch):
    monkeypatch.setattr(services.sys, "platform", "linux")
    monkeypatch.setattr(services.shutil, "which", lambda name: "/usr/bin/systemctl")
    fr = FakeRun()
    fr.handle(lambda a, k: R(0))
    assert services.backend(env={"FORCE_INSTANCE": "dev"}, run=fr) is None


# --- install on launchd ----------------------------------------------------------------------
class FakeLaunchd(FakeRun):
    """launchctl with state: `print` answers for loaded labels; bootout unloads, bootstrap loads."""
    def __init__(self, loaded):
        super().__init__()
        self.loaded = set(loaded)

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[:2] == ["launchctl", "print"]:
            target = argv[2]
            if target == f"gui/{_UID}":
                return R(0)
            label = target.rsplit("/", 1)[-1]
            return R(0, "        state = running\n") if label in self.loaded else R(113)
        if argv[:2] == ["launchctl", "bootout"]:
            self.loaded.discard(argv[2].rsplit("/", 1)[-1])
        if argv[:2] == ["launchctl", "bootstrap"]:
            self.loaded.add(Path(argv[3]).stem)
        return R(0)


def _install_fake(monkeypatch, home, daemon_changed=True, loaded=("cc.useforce.force-tmux", "cc.useforce.force")):
    monkeypatch.setattr(services.sys, "platform", "darwin")
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    data = services.plists(home)
    (agents / "cc.useforce.force-tmux.plist").write_bytes(data["cc.useforce.force-tmux"])
    (agents / "cc.useforce.force.plist").write_bytes(b"old daemon plist" if daemon_changed
                                                     else data["cc.useforce.force"])
    return FakeLaunchd(loaded), agents, data


def _ops(fr, op):
    return [c[-1].rsplit("/", 1)[-1].removesuffix(".plist") for c in fr.calls if c[1] == op]


def test_install_launchd_writes_and_bootstraps(tmp_path, monkeypatch):
    home = tmp_path / "home"
    fr, agents, data = _install_fake(monkeypatch, home)
    services.install(Path(tmp_path), home, fr, True, "whoami")
    for label in ("cc.useforce.force-tmux", "cc.useforce.force", "cc.useforce.force-smoke"):
        assert (agents / f"{label}.plist").read_bytes() == data[label]
    # the loaded, unchanged tmux agent is left alone (it holds the sessions)
    assert "cc.useforce.force-tmux" not in _ops(fr, "bootout") + _ops(fr, "bootstrap")
    # the changed daemon is reloaded: bootout, then bootstrap once launchd has let go of it
    assert _ops(fr, "bootout") == ["cc.useforce.force"]
    assert _ops(fr, "bootstrap") == ["cc.useforce.force", "cc.useforce.force-smoke"]
    i_out = next(i for i, c in enumerate(fr.calls) if c[1] == "bootout")
    i_in = next(i for i, c in enumerate(fr.calls) if c[1] == "bootstrap")
    assert i_out < i_in
    # launchd opens the daemon's log itself: its folder must exist
    assert (home / ".local" / "state" / "force").is_dir()


def test_install_launchd_unchanged_and_loaded_left_alone(tmp_path, monkeypatch):
    home = tmp_path / "home"
    fr, agents, data = _install_fake(monkeypatch, home, daemon_changed=False)
    services.install(Path(tmp_path), home, fr, False, "whoami")
    assert _ops(fr, "bootout") == [] and _ops(fr, "bootstrap") == [] and _ops(fr, "kickstart") == []
    assert not (agents / "cc.useforce.force-smoke.plist").exists()      # no self-test: no smoke agent


def test_install_launchd_first_install_bootstraps_all(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(services.sys, "platform", "darwin")
    fr = FakeLaunchd(())
    services.install(Path(tmp_path), home, fr, False, "whoami")
    assert _ops(fr, "bootout") == []
    assert sorted(_ops(fr, "bootstrap")) == ["cc.useforce.force", "cc.useforce.force-tmux"]


def test_install_launchd_without_a_gui_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(services.sys, "platform", "darwin")
    fr = FakeRun()
    fr.handle(lambda a, k: R(1) if a[:2] == ["launchctl", "print"] else None)
    services.install(Path(tmp_path), tmp_path / "home", fr, False, "whoami")
    assert "no launchd session here" in capsys.readouterr().out
    assert not (tmp_path / "home" / "Library").exists()


# --- uninstall on launchd ---------------------------------------------------------------------
def test_uninstall_launchd_boots_out_and_removes(tmp_path, monkeypatch):
    monkeypatch.setattr(services.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in services.plists(tmp_path):
        (agents / f"{label}.plist").write_bytes(b"x")
    fr = FakeRun()
    fr.handle(lambda a, k: R(0) if a[:2] == ["launchctl", "print"] else None)
    services.uninstall(run=fr)
    bootouts = [c for c in fr.calls if c[1] == "bootout"]
    assert len(bootouts) == 3
    assert all(c[2].rsplit("/", 1)[-1] in services.LAUNCHD_LABELS.values() for c in bootouts)
    assert not list(agents.glob("cc.useforce.*.plist")), "plists removed"


def test_uninstall_on_macos_removes_plists_without_launchd(tmp_path, monkeypatch):
    # an ssh login: launchd does not answer, the files still go (they would start Force at the next login)
    monkeypatch.setattr(services.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "cc.useforce.force.plist").write_bytes(b"x")
    fr = FakeRun()
    fr.handle(lambda a, k: R(1) if a[:2] == ["launchctl", "print"] else None)
    services.uninstall(run=fr)
    assert [c for c in fr.calls if c[1] == "bootout"] == []
    assert not list(agents.glob("cc.useforce.*.plist"))


# --- is_active parses state ---------------------------------------------------------------
def test_is_active_launchd_state_running(monkeypatch):
    monkeypatch.setattr(services.sys, "platform", "darwin")
    fr = FakeRun()
    fr.handle(lambda a, k: R(0, "        state = running\n")
            if a[:2] == ["launchctl", "print"] and a[2].endswith("/cc.useforce.force") else None)
    fr.handle(lambda a, k: R(0) if a[:2] == ["launchctl", "print"] else None)
    assert services.is_active("daemon", run=fr) is True
    fr2 = FakeRun()
    fr2.handle(lambda a, k: R(0, "        state = waiting\n")
            if a[:2] == ["launchctl", "print"] and a[2].endswith("/cc.useforce.force") else None)
    fr2.handle(lambda a, k: R(0) if a[:2] == ["launchctl", "print"] else None)
    assert services.is_active("daemon", run=fr2) is False
