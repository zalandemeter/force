"""Development instances (SPEC §2.1): FORCE_INSTANCE gives a second, separate Force its own paths and sockets,
FORCE_BIN names its launcher, and nothing it runs touches what the live install owns."""
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from force import cli, config, menus, setup

REPO = Path(__file__).resolve().parent.parent


def _env(**kw):
    env = {"HOME": "/home/u"}
    env.update(kw)
    return env


# --- config ------------------------------------------------------------------------------------------------
def test_no_instance_is_the_live_install():
    assert config.instance(_env()) is None
    assert config.instance(_env(FORCE_INSTANCE="")) is None
    p = config.get_paths(_env())
    assert (p.home, p.state, p.config_file) == (Path("/home/u/.local/share/force"),
                                                 Path("/home/u/.local/state/force"),
                                                 Path("/home/u/.config/force/settings.json"))
    assert (p.tmux_socket, p.ui_socket) == ("force", "force-ui")


def test_instance_gets_its_own_paths_and_sockets():
    p = config.get_paths(_env(FORCE_INSTANCE="dev"))
    assert p.home == Path("/home/u/.local/share/force-dev")
    assert p.state == Path("/home/u/.local/state/force-dev")
    assert p.config_file == Path("/home/u/.config/force-dev/settings.json")
    assert (p.tmux_socket, p.ui_socket) == ("force-dev", "force-dev-ui")
    assert p.claude_dir == Path("/home/u/.claude")            # the same Claude Code


def test_explicit_variables_win_over_instance_defaults():
    p = config.get_paths(_env(FORCE_INSTANCE="dev", FORCE_HOME="/x/home", FORCE_TMUX_SOCKET="s",
                              FORCE_UI_SOCKET="s-ui"))
    assert p.home == Path("/x/home")
    assert (p.tmux_socket, p.ui_socket) == ("s", "s-ui")
    assert p.state == Path("/home/u/.local/state/force-dev")


@pytest.mark.parametrize("bad", ["Dev", "1dev", "dev-1", "dev/x", "../x", "a" * 17, "dev "])
def test_invalid_instance_names_are_refused(bad):
    with pytest.raises(ValueError, match="FORCE_INSTANCE"):
        config.instance(_env(FORCE_INSTANCE=bad))
    with pytest.raises(ValueError):
        config.get_paths(_env(FORCE_INSTANCE=bad))


def test_launcher_path():
    assert config.launcher_path(_env()) == os.path.expanduser("~/.local/bin/force")
    assert config.launcher_path(_env(FORCE_BIN="/x/force-dev")) == "/x/force-dev"


# --- cli -----------------------------------------------------------------------------------------------------
def test_invalid_instance_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_INSTANCE", "Bad!")
    assert cli.main(["list"]) == 2
    assert "FORCE_INSTANCE must be" in capsys.readouterr().err


@pytest.mark.parametrize("cmd, hint", [(["update"], "./dev.sh"), (["update", "--check"], "./dev.sh"),
                                       (["uninstall", "--yes"], "./dev.sh reset"), (["fonts"], "live install"),
                                       (["terminals"], "live install")])
def test_instance_refuses_live_install_commands(monkeypatch, capsys, cmd, hint):
    monkeypatch.setenv("FORCE_INSTANCE", "dev")
    assert cli.main(cmd) == 2
    err = capsys.readouterr().err
    assert "this is the dev instance" in err and hint in err


def test_instance_never_uses_systemd(monkeypatch):
    monkeypatch.setenv("FORCE_INSTANCE", "dev")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: 0)
    assert cli._services_installed() is False
    assert cli._services_installed(any_units=True) is False


def test_cockpit_bin_and_launcher_follow_force_bin(monkeypatch, env, tmp_path):
    fake = tmp_path / "force-dev"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("FORCE_BIN", str(fake))
    assert cli.make_cockpit().cockpit_bin == str(fake)
    assert cli._launcher() == str(fake)


def test_doctor_checks_units_only_where_systemd_is_used(monkeypatch, cockpit):
    seen = {}
    import force.doctor as doctor

    def fake_run(c, fix=False, out=print, systemd=True):
        seen["systemd"] = systemd
        return 0

    monkeypatch.setattr(doctor, "run_doctor", fake_run)
    monkeypatch.setattr(cli, "_services_installed", lambda any_units=False: False)
    cli.doctor_command(cockpit, False)
    assert seen["systemd"] is False
    monkeypatch.setattr(cli, "_services_installed", lambda any_units=False: True)
    cli.doctor_command(cockpit, False)
    assert seen["systemd"] is True


def test_lock_check_keeps_the_daemon_pid(cockpit):
    cockpit.paths.state.mkdir(parents=True, exist_ok=True)
    cockpit.paths.daemon_lock.write_text("4242\n")
    assert cli._daemon_lock_held(cockpit) is False
    assert cockpit.paths.daemon_lock.read_text() == "4242\n"


def test_setup_refuses_in_an_instance(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_INSTANCE", "dev")
    assert setup.main(["--release", "/nowhere"]) == 2
    assert "./dev.sh" in capsys.readouterr().err


# --- tmux: menus and configs ---------------------------------------------------------------------------------
def test_menus_run_the_launcher_tmux_names():
    for mode in ("classic", "split"):
        text = menus.render(mode)
        assert ".local/bin/force" not in text
        assert "$FORCE_BIN dialog" in text


@pytest.mark.parametrize("conf", ["tmux.conf", "tmux-ui.conf"])
@pytest.mark.parametrize("given", [None, "/x/force-dev"])
def test_configs_default_force_bin_to_the_live_launcher(conf, given, tmp_path):
    """The real config, started without FORCE_BIN, sets the live launcher; an instance's FORCE_BIN is kept."""
    sock = f"forcetest-bin-{uuid.uuid4().hex[:8]}-{os.getpid()}"
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "FORCE_BIN")}
    env["HOME"] = str(tmp_path)
    if given:
        env["FORCE_BIN"] = given
    try:
        subprocess.run(["tmux", "-L", sock, "-f", str(REPO / "share" / conf), "start-server", ";",
                        "new-session", "-d", "-s", "probe"], env=env, check=True, capture_output=True, timeout=15)
        out = subprocess.run(["tmux", "-L", sock, "show-environment", "-g", "FORCE_BIN"], env=env,
                             capture_output=True, text=True, timeout=15).stdout.strip()
        assert out == f"FORCE_BIN={given or str(tmp_path) + '/.local/bin/force'}"
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], env=env, capture_output=True, timeout=15)


# --- scripts -------------------------------------------------------------------------------------------------
def test_doctor_popup_runs_force_bin(tmp_path):
    log = tmp_path / "argv"
    fake = tmp_path / "force-dev"
    fake.write_text(f'#!/bin/sh\necho "$@" > "{log}"\n')
    fake.chmod(0o755)
    env = dict(os.environ, FORCE_BIN=str(fake), HOME=str(tmp_path))
    subprocess.run(["bash", str(REPO / "share" / "doctor-popup.sh")], env=env, stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=15)
    assert log.read_text().strip() == "doctor --fix --card"


def _dev(args, home, instance=None):
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_INSTANCE", "FORCE_BIN", "TMUX")}
    env["HOME"] = str(home)
    if instance is not None:
        env["FORCE_INSTANCE"] = instance
    return subprocess.run(["bash", str(REPO / "dev.sh"), *args], env=env, capture_output=True, text=True,
                          timeout=30)


def test_dev_sh_refuses_a_bad_instance_name(tmp_path):
    r = _dev(["reset"], tmp_path, instance="Bad!")
    assert r.returncode == 2 and "FORCE_INSTANCE" in r.stderr


def test_dev_sh_reset_removes_only_the_instance(tmp_path):
    inst = f"t{uuid.uuid4().hex[:8]}"
    live = [tmp_path / ".local/share/force", tmp_path / ".local/state/force", tmp_path / ".config/force"]
    mine = [tmp_path / f".local/share/force-{inst}", tmp_path / f".local/state/force-{inst}",
            tmp_path / f".config/force-{inst}"]
    for d in live + mine:
        d.mkdir(parents=True)
        (d / "keep").write_text("x")
    (tmp_path / ".local/bin").mkdir(parents=True)
    (tmp_path / ".local/bin/force").write_text("live")
    (tmp_path / f".local/bin/force-{inst}").write_text("dev")
    r = _dev(["reset"], tmp_path, instance=inst)
    assert r.returncode == 0, r.stderr
    assert all(not d.exists() for d in mine)
    assert not (tmp_path / f".local/bin/force-{inst}").exists()
    assert all((d / "keep").exists() for d in live)
    assert (tmp_path / ".local/bin/force").read_text() == "live"


def test_dev_sh_stop_without_anything_running(tmp_path):
    r = _dev(["stop"], tmp_path, instance=f"t{uuid.uuid4().hex[:8]}")
    assert r.returncode == 0 and "stopped" in r.stdout


def test_dev_sh_parses():
    assert subprocess.run(["bash", "-n", str(REPO / "dev.sh")]).returncode == 0


# --- upgrades: servers started by an older release ------------------------------------------------------------
def test_daemon_publishes_force_bin_to_servers_started_without_it(env, monkeypatch):
    """`force update` restarts the daemon but not the tmux servers: their config (which sets FORCE_BIN for the
    menus) is the old one. The new daemon sets FORCE_BIN on the host and the ui server."""
    from test_daemon import FakeClock
    from force.daemon import Daemon
    cockpit = env.make_cockpit()
    monkeypatch.delenv("FORCE_BIN", raising=False)
    # both servers up with a config that knows nothing about FORCE_BIN (the fixture's)
    cockpit.tmux.start_server()
    subprocess.run(["tmux", "-L", env.socket + "-ui", "-f", str(env.tmp / "tmux-ui.conf"), "new-session", "-d",
                    "-s", "u"], capture_output=True, timeout=15)
    for sock in (env.socket, env.socket + "-ui"):
        subprocess.run(["tmux", "-L", sock, "set-environment", "-gu", "FORCE_BIN"], capture_output=True, timeout=15)
    Daemon(cockpit, FakeClock()).tick()
    want = f"FORCE_BIN={config.launcher_path()}"
    for sock in (env.socket, env.socket + "-ui"):
        out = subprocess.run(["tmux", "-L", sock, "show-environment", "-g", "FORCE_BIN"], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        assert out == want, sock
