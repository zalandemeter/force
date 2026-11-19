"""`force net up | down | status`, the ssh-only hint and its card, the link column, doctor and the API field
(SPEC §56.2). sudo and the firewalls are faked (an injected `run`, or fake programs on PATH): nothing here runs a
real sudo, a firewall, ssh or a service manager."""
import dataclasses
import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from force import api, cli, config, daemon, doctor, link, net, remote
from force.state import Snapshot

ID = "b" * 64


# --------------------------------------------------------------------------------------------- helpers --
class FakeSystem(net.System):
    """A machine for `force net`: `answers` maps an argv (tuple) to (code, stdout); the rest exits 1. Commands run
    on the terminal (sudo) are recorded in `ran`, the queries in `asked`."""

    def __init__(self, root: Path, answers=None, which=(), platform="linux", env=None, euid=1000, tty=True,
                 sudo_code=0):
        super().__init__(run=self._run, which=lambda p: f"/usr/sbin/{p}" if p in which else None,
                         platform=platform, env=dict(env or {}), root=root, euid=lambda: euid, tty=lambda: tty)
        self.answers = {tuple(k): v for k, v in (answers or {}).items()}
        self.asked, self.ran, self.sudo_code = [], [], sudo_code

    def _run(self, argv, interactive=False, timeout=10):
        if interactive:
            self.ran.append(list(argv))
            return subprocess.CompletedProcess(argv, self.sudo_code, None, None)
        self.asked.append(list(argv))
        code, out = self.answers.get(tuple(argv), (1, ""))
        return subprocess.CompletedProcess(argv, code, out, "")


def active(*units):
    return {("systemctl", "is-active", u): (0, "active\n") for u in units}


def ufw_root(tmp_path, enabled="yes"):
    root = tmp_path / "root"
    (root / "etc" / "ufw").mkdir(parents=True)
    (root / "etc" / "ufw" / "ufw.conf").write_text(f"# ufw.conf\n\nENABLED={enabled}\nLOGLEVEL=low\n")
    return root


def cfg_of(env, **kw):
    return dataclasses.replace(env.cfg, **kw)


def record(env):
    return json.loads(net.net_file(env.paths).read_text())


# ------------------------------------------------------------------------------------------- force net up --
def test_up_ufw_runs_one_command_under_sudo_and_records_it(env, tmp_path, capsys):
    s = FakeSystem(ufw_root(tmp_path), which=("ufw",))
    assert net.up(env.paths, env.cfg, s) == 0
    assert s.ran == [["sudo", "ufw", "allow", "47823/udp", "comment", "Force direct link"]]
    out = capsys.readouterr().out
    assert "sudo ufw allow 47823/udp comment 'Force direct link'" in out, "says what it runs first"
    rec = record(env)
    assert (rec["firewall"], rec["port"]) == ("ufw", 47823)
    assert rec["commands"] == ["sudo ufw allow 47823/udp comment 'Force direct link'"]
    assert isinstance(rec["at"], float)
    # again: already open, nothing run
    s2 = FakeSystem(ufw_root(tmp_path / "again"), which=("ufw",))
    assert net.up(env.paths, env.cfg, s2) == 0
    assert s2.ran == [] and "already" in capsys.readouterr().out
    # down undoes it and forgets it
    assert net.down(env.paths, env.cfg, s2) == 0
    assert s2.ran == [["sudo", "ufw", "delete", "allow", "47823/udp"]]
    assert not net.net_file(env.paths).exists()


def test_up_uses_the_link_port_setting(env, tmp_path):
    s = FakeSystem(ufw_root(tmp_path), which=("ufw",))
    assert net.up(env.paths, cfg_of(env, link_port=50001), s) == 0
    assert s.ran == [["sudo", "ufw", "allow", "50001/udp", "comment", "Force direct link"]]
    assert record(env)["port"] == 50001


def test_ufw_conf_decides_and_systemctl_only_without_it(tmp_path):
    # ENABLED=no: ufw's service may still be "active" (it ran ufw-init), the file wins
    s = FakeSystem(ufw_root(tmp_path, "no"), which=("ufw",), answers=active("ufw"))
    assert net.detect(s) is None
    # the file cannot be read: systemctl is-active ufw
    s = FakeSystem(tmp_path / "empty", which=("ufw",), answers=active("ufw"))
    assert net.detect(s) == "ufw"
    # no ufw program: no ufw, whatever the file says
    assert net.detect(FakeSystem(ufw_root(tmp_path / "x"))) is None


def test_up_firewalld(env, tmp_path, capsys):
    s = FakeSystem(tmp_path, answers=active("firewalld"))
    assert net.up(env.paths, env.cfg, s) == 0
    assert s.ran == [["sudo", "firewall-cmd", "--permanent", "--add-port=47823/udp"],
                     ["sudo", "firewall-cmd", "--reload"]]
    assert record(env)["firewall"] == "firewalld"
    assert net.down(env.paths, env.cfg, s) == 0
    assert s.ran[2:] == [["sudo", "firewall-cmd", "--permanent", "--remove-port=47823/udp"],
                         ["sudo", "firewall-cmd", "--reload"]]
    assert not net.net_file(env.paths).exists()


def test_firewalld_found_by_firewall_cmd_state(tmp_path):
    s = FakeSystem(tmp_path, which=("firewall-cmd",), answers={("firewall-cmd", "--state"): (0, "running\n")})
    assert net.detect(s) == "firewalld"
    s = FakeSystem(tmp_path, which=("firewall-cmd",), answers={("firewall-cmd", "--state"): (252, "not running\n")})
    assert net.detect(s) is None


def test_up_macos_application_firewall(env, tmp_path, capsys):
    app = Path(env.paths.tmux_conf).parent / "bin" / "force-link"
    app.parent.mkdir(parents=True, exist_ok=True)
    app.write_text("")
    state = {(net.SOCKETFILTERFW, "--getglobalstate"): (0, "Firewall is enabled. (State = 1)\n")}
    s = FakeSystem(tmp_path, platform="darwin", answers=state)
    assert net.up(env.paths, env.cfg, s) == 0
    assert s.ran == [["sudo", net.SOCKETFILTERFW, "--add", str(app)], ["sudo", net.SOCKETFILTERFW, "--unblockapp", str(app)]]
    assert record(env)["app"] == str(app)
    assert net.down(env.paths, env.cfg, s) == 0
    assert s.ran[2:] == [["sudo", net.SOCKETFILTERFW, "--remove", str(app)]]
    # off: no host firewall
    off = FakeSystem(tmp_path, platform="darwin",
                     answers={(net.SOCKETFILTERFW, "--getglobalstate"): (0, "Firewall is disabled. (State = 0)\n")})
    assert net.detect(off) is None


def test_up_nftables_or_iptables_only_prints_the_rule(env, tmp_path, capsys):
    s = FakeSystem(tmp_path, answers=active("nftables"))
    assert net.up(env.paths, env.cfg, s) == 0
    out = capsys.readouterr().out
    assert "sudo nft add rule inet filter input udp dport 47823 accept" in out
    assert s.ran == [] and not net.net_file(env.paths).exists(), "changes nothing"
    s = FakeSystem(tmp_path, answers=active("iptables"))
    assert net.up(env.paths, env.cfg, s) == 0
    assert "sudo iptables -I INPUT -p udp --dport 47823 -j ACCEPT" in capsys.readouterr().out
    assert s.ran == []
    assert net.detect(FakeSystem(tmp_path, answers=active("netfilter-persistent"))) == "iptables"


def test_up_without_a_host_firewall(env, tmp_path, capsys):
    s = FakeSystem(tmp_path)
    assert net.up(env.paths, env.cfg, s) == 0
    out = capsys.readouterr().out
    assert "nothing to open on this machine" in out and "router or cloud firewall" in out
    assert s.ran == [] and not net.net_file(env.paths).exists()


@pytest.mark.parametrize("how", ["env", "proc"])
def test_up_on_wsl_explains_and_does_nothing(env, tmp_path, capsys, how):
    root = tmp_path / "root"
    (root / "proc").mkdir(parents=True)
    (root / "proc" / "version").write_text("Linux version 5.15.153.1-microsoft-standard-WSL2 (gcc)\n"
                                           if how == "proc" else "Linux version 6.8.0-generic\n")
    s = FakeSystem(root, which=("ufw",), answers=active("ufw", "firewalld"),
                   env={"WSL_DISTRO_NAME": "Ubuntu"} if how == "env" else {})
    assert net.up(env.paths, env.cfg, s) == 0
    out = capsys.readouterr().out
    assert "incoming connections into WSL are not supported" in out and "needs nothing" in out
    assert s.ran == [] and s.asked == [], "no firewall looked at, nothing run"


def test_up_as_root_is_refused(env, tmp_path, capsys):
    s = FakeSystem(ufw_root(tmp_path), which=("ufw",), euid=0)
    assert net.up(env.paths, env.cfg, s) == 1
    err = capsys.readouterr().err
    assert "not with sudo" in err and "PATH" in err
    assert s.ran == [] and not net.net_file(env.paths).exists()
    assert net.down(env.paths, env.cfg, s) == 1


def test_up_without_a_terminal_is_refused_with_the_commands(env, tmp_path, capsys):
    s = FakeSystem(ufw_root(tmp_path), which=("ufw",), tty=False)
    assert net.up(env.paths, env.cfg, s) == 1
    out, err = capsys.readouterr()
    assert "sudo ufw allow 47823/udp" in out
    assert "in a terminal" in err
    assert s.ran == [] and not net.net_file(env.paths).exists()


def test_up_records_nothing_when_sudo_fails(env, tmp_path, capsys):
    s = FakeSystem(tmp_path, answers=active("firewalld"), sudo_code=1)
    assert net.up(env.paths, env.cfg, s) == 1
    assert len(s.ran) == 1, "stops at the first failure"
    assert not net.net_file(env.paths).exists()
    assert "failed (exit 1)" in capsys.readouterr().err


def test_up_with_direct_link_off_does_nothing(env, tmp_path, capsys):
    s = FakeSystem(ufw_root(tmp_path), which=("ufw",))
    assert net.up(env.paths, cfg_of(env, direct_link=False), s) == 0
    assert "directLink is off" in capsys.readouterr().out
    assert s.ran == [] and s.asked == []


def test_up_with_another_rule_recorded_asks_for_down_first(env, tmp_path, capsys):
    s = FakeSystem(ufw_root(tmp_path), which=("ufw",))
    assert net.up(env.paths, env.cfg, s) == 0
    assert net.up(env.paths, cfg_of(env, link_port=50001), s) == 1
    assert "force net down first" in capsys.readouterr().err
    assert len(s.ran) == 1


def test_down_with_nothing_recorded_or_no_terminal(env, tmp_path, capsys):
    s = FakeSystem(tmp_path, tty=False)
    assert net.down(env.paths, env.cfg, s) == 0
    assert "Nothing to close" in capsys.readouterr().out
    net.net_file(env.paths).parent.mkdir(parents=True, exist_ok=True)
    net.net_file(env.paths).write_text(json.dumps({"firewall": "ufw", "port": 47823, "commands": [], "at": 1.0}))
    assert net.down(env.paths, env.cfg, s) == 1, "sudo needs a terminal"
    assert s.ran == [] and net.net_file(env.paths).exists(), "kept until it is closed"
    net.net_file(env.paths).write_text("{broken")
    assert net.down(env.paths, env.cfg, FakeSystem(tmp_path)) == 1
    assert "cannot be read" in capsys.readouterr().err


def test_sudo_and_ufw_faked_on_path(env, tmp_path, monkeypatch, capsys):
    """The real `run`, with fake sudo and ufw programs first on PATH: the argv reaches sudo as printed."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log = tmp_path / "sudo.log"
    for name in ("sudo", "ufw"):
        p = bin_dir / name
        p.write_text(f"#!/bin/sh\nprintf '%s\\n' \"{name} $*\" >> {log}\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    s = net.System(root=ufw_root(tmp_path), env={}, euid=lambda: 1000, tty=lambda: True, platform="linux")
    assert net.up(env.paths, env.cfg, s) == 0
    assert log.read_text() == "sudo ufw allow 47823/udp comment Force direct link\n"
    assert net.down(env.paths, env.cfg, s) == 0
    assert log.read_text().splitlines()[-1] == "sudo ufw delete allow 47823/udp"


# ---------------------------------------------------------------------------------------- force net status --
def test_status_human_and_json(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(link, "daemon_running", lambda paths: True)
    link.self_file(env.paths).parent.mkdir(parents=True, exist_ok=True)
    link.self_file(env.paths).write_text(json.dumps({"id": ID, "port": 47823, "addrs": ["192.0.2.1:47823"]}))
    s = FakeSystem(ufw_root(tmp_path), which=("ufw",))
    assert net.up(env.paths, env.cfg, s) == 0
    capsys.readouterr()
    assert net.status(env.paths, env.cfg, True, s) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["daemon"] == {"running": True, "port": 47823}
    assert (doc["firewall"], doc["link_port"], doc["wsl"]) == ("ufw", 47823, False)
    assert doc["net"]["firewall"] == "ufw" and doc["net"]["port"] == 47823
    assert net.status(env.paths, env.cfg, False, s) == 0
    out = capsys.readouterr().out
    assert "running, UDP 47823" in out and "UDP 47823 opened in ufw" in out and "linkPort  47823" in out
    # no daemon, nothing recorded, nothing found
    monkeypatch.setattr(link, "daemon_running", lambda paths: False)
    net.net_file(env.paths).unlink()
    assert net.status(env.paths, env.cfg, False, FakeSystem(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "not running" in out and "none found" in out and "nothing opened" in out


def test_cli_parses_net(env):
    a = cli.build_parser().parse_args(["net", "status", "--json"])
    assert (a.command, a.action, a.json) == ("net", "status", True)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["net", "open"])


# ------------------------------------------------------------------------------------------------ the hint --
def setup_remote(env, *, status="connected", hello_link=True, state="ssh", reason="timeout", fails=2,
                 port=47900, enabled=True):
    remote.save(env.paths, [remote.Remote("devbox", "devbox", enabled)])
    remote.write_status(env.paths, "devbox", status, "", "1.0.0", link=hello_link)
    lj = {"id": ID, "addrs": ["192.0.2.7:47900"], "state": state, "reason": reason, "fails": fails}
    if port is not None:
        lj["port"] = port
    p = link.link_file(env.paths, "devbox")
    p.write_text(json.dumps(lj))


def running(_paths):
    return True


def test_hint_when_every_condition_holds(env):
    setup_remote(env)
    assert link.link_hints(env.paths, env.cfg, running) == [{"host": "devbox", "port": 47900}]
    setup_remote(env, port=None)
    assert link.link_hints(env.paths, env.cfg, running) == [{"host": "devbox", "port": 47823}], \
        "without the remote's port: linkPort"


@pytest.mark.parametrize("change", [
    {"status": "offline"}, {"hello_link": False}, {"state": "direct"}, {"reason": "refused"},
    {"reason": "no addresses"}, {"fails": 1}, {"fails": True}, {"enabled": False},
])
def test_no_hint_when_a_condition_fails(env, change):
    setup_remote(env, **change)
    assert link.link_hints(env.paths, env.cfg, running) == []
    assert link.ssh_only_hosts(env.paths, env.cfg, running) == []


def test_no_hint_without_a_running_link_daemon(env):
    """A link.json left by a daemon that stopped is not trusted: the daemon is asked on its socket."""
    setup_remote(env)
    assert link.link_hints(env.paths, env.cfg, lambda p: False) == []
    assert link.link_hints(env.paths, env.cfg) == [], "no socket: not running"
    path = str(link.sock_file(env.paths))
    if len(path) > 100:
        pytest.skip("socket path too long here")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen(4)
        assert link.link_hints(env.paths, env.cfg) == [{"host": "devbox", "port": 47900}]
    finally:
        srv.close()
        os.unlink(path)


def test_no_hint_with_the_setting_off_but_still_ssh_only(env):
    setup_remote(env)
    off = cfg_of(env, net_up_hint=False)
    assert link.link_hints(env.paths, off, running) == []
    assert link.ssh_only_hosts(env.paths, off, running) == [{"host": "devbox", "port": 47900}]
    assert link.ssh_only_hosts(env.paths, cfg_of(env, direct_link=False), running) == []


def test_status_keeps_the_link_flag_while_offline(env):
    remote.write_status(env.paths, "devbox", "connected", "", "1.0.0", link=True)
    remote.write_status(env.paths, "devbox", "offline", "connection ended", "1.0.0")
    assert remote.read_status(env.paths, "devbox")["link"] is True
    remote.write_status(env.paths, "devbox", "connected", "", "1.0.0", link=False)
    assert remote.read_status(env.paths, "devbox")["link"] is False
    remote.write_status(env.paths, "fresh", "offline")
    assert "link" not in remote.read_status(env.paths, "fresh")


def test_the_watcher_records_whether_the_hello_carried_link(env):
    class Proc:
        def __init__(self, hello):
            self.stdout = iter([json.dumps({"hello": hello}) + "\n"])
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass
    w = remote.Watcher(env.paths, remote.Remote("devbox", "devbox"))
    w._read(Proc({"proto": remote.PROTO, "version": "1.0.0", "host": "devbox", "ui": "split",
                  "link": {"id": "not-valid", "port": 1, "addrs": []}}), 0.0)
    assert remote.read_status(env.paths, "devbox")["link"] is True
    w._read(Proc({"proto": remote.PROTO, "version": "1.0.0", "host": "devbox", "ui": "split"}), 0.0)
    assert remote.read_status(env.paths, "devbox")["link"] is False


def test_snapshot_carries_link_hints(env, monkeypatch):
    snap = Snapshot(generated_ms=1, latest_version=None, sessions=[], link_hints=[{"host": "devbox", "port": 47900}])
    data = snap.to_json()
    assert data["link_hints"] == [{"host": "devbox", "port": 47900}]
    assert Snapshot.from_json(data).link_hints == [{"host": "devbox", "port": 47900}]
    assert "link_hints" not in Snapshot(generated_ms=1, latest_version=None, sessions=[]).to_json()
    bad = Snapshot.from_json({"sessions": [], "link_hints": [{"host": 1, "port": 2}, {"host": "x", "port": True}]})
    assert bad.link_hints is None
    # the daemon's step: link.link_hints with its own paths and settings, never an exception
    setup_remote(env)
    monkeypatch.setattr(link, "daemon_running", lambda paths: True)
    me = type("D", (), {"paths": env.paths, "cfg": env.cfg})()
    assert daemon.Daemon._link_hints(me) == [{"host": "devbox", "port": 47900}]
    me.cfg = cfg_of(env, net_up_hint=False)
    assert daemon.Daemon._link_hints(me) is None
    monkeypatch.setattr(link, "link_hints", lambda *a: 1 / 0)
    assert daemon.Daemon._link_hints(me) is None


# -------------------------------------------------------------------------------------------- the hint card --
def test_hint_card_says_what_and_where(env, capsys):
    setup_remote(env)
    assert net.hint_command(env.paths, env.cfg, "devbox", ui=True, wait_key=lambda: "\x1b") == 0
    out = capsys.readouterr().out
    for want in ("devbox", "force net up", "UDP 47900", "ssh keeps working", "faster", "network",
                 "Don't show again", "Esc"):
        assert want in out, want
    plain = net.hint_text("devbox", 47900, color=False)
    assert all(len(line) <= 68 for line in plain.splitlines()), "fits the 72-wide popup"
    assert len(plain.splitlines()) <= 15, "fits the 17-high popup"
    assert config.load_config(env.paths.config_file).net_up_hint is True, "Esc changes nothing"


def test_hint_card_dont_show_again_sets_the_setting(env, capsys):
    setup_remote(env)
    assert net.hint_command(env.paths, env.cfg, "devbox", ui=True, wait_key=lambda: "d") == 0
    assert config.load_config(env.paths.config_file).net_up_hint is False
    assert json.loads(env.paths.config_file.read_text())["netUpHint"] is False
    assert json.loads(env.paths.config_file.read_text())["layout"] == "classic", "the other settings stay"


def test_hint_card_needs_a_host(env, capsys):
    assert net.hint_command(env.paths, env.cfg, None, ui=True, wait_key=lambda: "d") == 2
    assert net.hint_command(env.paths, env.cfg, "bad host", ui=False) == 2
    assert config.load_config(env.paths.config_file).net_up_hint is True
    assert cli.main(["net", "hint", "devbox"]) == 0
    assert "force net up" in capsys.readouterr().out


# ----------------------------------------------------------------------------- uninstall, list, picker --
def test_uninstall_reminds_to_close_the_port(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.uninstall_command(cockpit, False, False) == 1          # not confirmed: nothing removed
    assert "force net down" not in capsys.readouterr().out
    net.net_file(env.paths).parent.mkdir(parents=True, exist_ok=True)
    net.net_file(env.paths).write_text(json.dumps({"firewall": "ufw", "port": 47823}))
    assert cli.uninstall_command(cockpit, False, False) == 1
    assert "run force net down first to close the firewall port" in capsys.readouterr().out
    assert net.net_file(env.paths).exists(), "reminded, not run"


def test_host_list_link_column(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    setup_remote(env, state="direct", reason="", fails=0)
    monkeypatch.setattr(link, "daemon_running", lambda paths: True)

    def line():
        assert cli.remote_command(cockpit, "list", None, None, False) == 0
        return capsys.readouterr().out.splitlines()[0].split()
    assert line()[3:5] == ["connected", "direct"]
    setup_remote(env)
    assert line()[4] == "ssh"
    setup_remote(env, hello_link=False)
    assert line()[4] == "-", "no link support there"
    setup_remote(env)
    monkeypatch.setattr(link, "daemon_running", lambda paths: False)
    assert line()[4] == "-", "no link daemon here"


def test_host_picker_marks_a_direct_remote(env, monkeypatch):
    cockpit = env.make_cockpit()
    calls = []
    real = cockpit.ui_tmux.run

    def run(*args):
        if args and args[0] in ("display-menu", "display"):
            calls.append(args)
            return ""
        return real(*args)
    monkeypatch.setattr(cockpit.ui_tmux, "run", run)
    monkeypatch.setattr(link, "daemon_running", lambda paths: True)
    setup_remote(env, state="direct", reason="", fails=0)
    cli.hosts_menu_command(cockpit, "/dev/pts/1", "@1")
    item = next(str(a) for a in calls[-1] if "devbox" in str(a) and "#[" in str(a))
    assert " · direct" in item
    setup_remote(env)
    cli.hosts_menu_command(cockpit, "/dev/pts/1", "@1")
    item = next(str(a) for a in calls[-1] if "devbox" in str(a) and "#[" in str(a))
    assert "direct" not in item


# ------------------------------------------------------------------------------------------------- doctor --
def test_doctor_link_ssh_only_whatever_the_setting(env, monkeypatch):
    cockpit = env.make_cockpit()
    cockpit.cfg = cfg_of(env, net_up_hint=False)
    setup_remote(env)
    monkeypatch.setattr(link, "daemon_running", lambda paths: True)
    probs = doctor._check_link(cockpit)
    assert [(p.code, p.warning) for p in probs] == [("link-ssh-only", True)]
    assert "on devbox run: force net up" in probs[0].detail and "UDP 47900" in probs[0].detail
    setup_remote(env, fails=1)
    assert doctor._check_link(cockpit) == []


def test_doctor_link_daemon_down(env, monkeypatch):
    cockpit = env.make_cockpit()
    monkeypatch.setattr(link, "daemon_running", lambda paths: False)
    assert doctor._check_link(cockpit) == [], "a release without force-link has no direct link"
    app = remote.link_bin(env.paths)
    app.parent.mkdir(parents=True, exist_ok=True)
    app.write_text("")
    assert [p.code for p in doctor._check_link(cockpit)] == ["link-daemon-down"]
    cockpit.cfg = cfg_of(env, direct_link=False)
    assert doctor._check_link(cockpit) == []
    cockpit.cfg = env.cfg
    monkeypatch.setattr(link, "daemon_running", lambda paths: True)
    assert doctor._check_link(cockpit) == []


def test_doctor_warnings_do_not_fail(env, monkeypatch):
    cockpit = env.make_cockpit()
    warn = doctor.Problem("link-ssh-only", None, "devbox: ssh only; on devbox run: force net up", False, warning=True)
    monkeypatch.setattr(doctor, "check", lambda c, systemd=True: [warn])
    lines = []
    assert doctor.run_doctor(cockpit, out=lines.append, systemd=False) == 0
    assert lines == ["! - link-ssh-only: devbox: ssh only; on devbox run: force net up", "✓ healthy"]
    fault = doctor.Problem("tmux", None, "tmux server not running", False)
    monkeypatch.setattr(doctor, "check", lambda c, systemd=True: [warn, fault])
    lines = []
    assert doctor.run_doctor(cockpit, out=lines.append, systemd=False) == 1
    assert lines[1] == "✗ - tmux: tmux server not running"
    from force import cards
    assert "!" in cards.plain("\n".join(cards.doctor_line(lines[0])))


# ---------------------------------------------------------------------------------------------------- API --
def test_api_state_hosts_carry_link(env, monkeypatch):
    setup_remote(env, state="direct", reason="", fails=0)
    monkeypatch.setattr(link, "daemon_running", lambda paths: True)
    doc, _ = api.view_model(env.paths)
    assert doc["hosts"][0]["link"] is None, "this machine"
    assert doc["hosts"][1]["name"] == "devbox" and doc["hosts"][1]["link"] == "direct"
    setup_remote(env)
    assert api.view_model(env.paths)[0]["hosts"][1]["link"] == "ssh"
    monkeypatch.setattr(link, "daemon_running", lambda paths: False)
    assert api.view_model(env.paths)[0]["hosts"][1]["link"] is None
    monkeypatch.setattr(link, "daemon_running", lambda paths: True)
    setup_remote(env, hello_link=False)
    assert api.view_model(env.paths, cfg=env.cfg)[0]["hosts"][1]["link"] is None
