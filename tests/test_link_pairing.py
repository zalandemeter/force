"""Unit tests for the direct-link Python side (SPEC §56.2): settings defaults/schema, the link daemon's command line,
`force link trust|untrust`, the link card, the hello, pairing/unpairing over plain ssh, and the settings CLI's
`_apply_link_setting`. No force/ or crates/ changes: everything here is driven with fakes for `run` and
`subprocess.run`; real ssh and service managers are never reached."""
import dataclasses
import io
import json
import os
import socket
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from force import cli, config, link, remote
from force.config import Config, load_config, parse_value, save_setting, schema
from force.link import valid_card


def _peers(paths):
    return json.loads(link.peers_file(paths).read_text())["peers"]


def _write_self(paths, id_="a" * 64, port=47823):
    link.self_file(paths).parent.mkdir(parents=True, exist_ok=True)
    link.self_file(paths).write_text(json.dumps({"id": id_, "port": port, "addrs": ["192.0.2.1:47823"]}))


# --- 1. settings defaults, parse_value, schema -------------------------------------------------
def test_link_settings_defaults(tmp_path):
    cfg = Config()
    assert cfg.direct_link is True
    assert cfg.link_port == 47823
    assert cfg.net_up_hint is True


def test_parse_value_linkport(tmp_path):
    assert parse_value("linkPort", "47824") == 47824
    for bad in ("1023", "65536", "0", "abc"):
        with pytest.raises(ValueError):
            parse_value("linkPort", bad)


def test_save_setting_linkport_bool_rejected(tmp_path):
    f = tmp_path / "settings.json"
    with pytest.raises(ValueError):
        save_setting(f, "linkPort", True)
    assert not f.exists()


def test_load_config_linkport_out_of_range_falls_back(tmp_path, capsys):
    f = tmp_path / "settings.json"
    f.write_text('{"linkPort": 80}')
    cfg = load_config(f)
    assert cfg.link_port == 47823
    assert "warning" in capsys.readouterr().err

    f.write_text('{"linkPort": true}')
    assert load_config(f).link_port == 47823
    assert "warning" in capsys.readouterr().err


def test_link_settings_schema(tmp_path):
    props = schema()["properties"]
    port = props["linkPort"]
    assert port["type"] == "integer"
    assert port["minimum"] == 1024
    assert port["maximum"] == 65535
    assert port["default"] == 47823
    assert props["directLink"]["type"] == "boolean" and props["directLink"]["default"] is True
    assert props["netUpHint"]["type"] == "boolean" and props["netUpHint"]["default"] is True


# --- 2. daemon_argv -------------------------------------------------------------------------
def test_daemon_argv(env):
    paths = env.paths
    cfg = load_config(paths.config_file)
    assert link.daemon_argv(paths, cfg) == [
        str(remote.link_bin(paths)), "daemon", "--state", str(paths.state),
        "--hosts", str(paths.config_file.parent / "hosts.json"),
        "--peers", str(paths.config_file.parent / "link-peers.json"),
        "--port", "47823",
    ]
    assert link.daemon_argv(paths, dataclasses.replace(cfg, link_port=50000))[-1] == "50000"


# --- 3. link_daemon_command ------------------------------------------------------------------
def test_link_daemon_off(monkeypatch, capsys):
    execv = []
    monkeypatch.setattr(link.os, "execv", lambda *a: execv.append(a))
    paths = config.Paths(Path("/x/home"), Path("/x/state"), Path("/x/claude"), Path("/x/claude"),
                        "sock", Path("/x/tmux.conf"), Path("/x/settings.json"))
    assert link.link_daemon_command(paths, Config(direct_link=False)) == 0
    assert "directLink" in capsys.readouterr().out
    assert execv == []


def test_link_daemon_missing_binary(env, monkeypatch, capsys):
    missing = env.tmp / "no-such-force-link"
    monkeypatch.setattr(link.remote, "link_bin", lambda paths: missing)
    # exit 0: the service stays down instead of restarting a program that is not there (SPEC §12)
    assert link.link_daemon_command(env.paths, Config()) == 0
    assert "missing" in capsys.readouterr().err


def test_link_daemon_execs(env, monkeypatch):
    bin_ = env.tmp / "bin" / "force-link"
    bin_.parent.mkdir(parents=True, exist_ok=True)
    bin_.write_text("#!/bin/sh\n")
    bin_.chmod(0o755)
    execv = []
    monkeypatch.setattr(link.os, "execv", lambda p, a: execv.append((p, a)))
    cfg = load_config(env.paths.config_file)
    assert link.link_daemon_command(env.paths, cfg) == 1       # not reached after execv
    assert execv == [(str(bin_), link.daemon_argv(env.paths, cfg))]


# --- 4. trust / untrust ----------------------------------------------------------------------
def test_link_command_trust_and_untrust(env, capsys):
    paths = env.paths
    id1, id2 = "a" * 64, "b" * 64
    assert link.link_command(paths, "trust", [id1, "laptop"]) == 0
    assert capsys.readouterr().out.strip() == "ok"
    peers = _peers(paths)
    assert peers == [{"id": id1, "name": "laptop"}]
    assert os.stat(link.peers_file(paths)).st_mode & 0o777 == 0o600

    # same name, new id -> replaced (one entry)
    assert link.link_command(paths, "trust", [id2, "laptop"]) == 0
    assert _peers(paths) == [{"id": id2, "name": "laptop"}]
    # new name, same id -> replaced (one entry)
    assert link.link_command(paths, "trust", [id2, "phone"]) == 0
    assert _peers(paths) == [{"id": id2, "name": "phone"}]


def test_link_command_rejects_bad_ids(env, capsys):
    paths = env.paths
    good = "c" * 64
    assert link.link_command(paths, "trust", [good, "laptop"]) == 0
    capsys.readouterr()
    before = json.dumps(_peers(paths))
    for bad in ("d" * 63, "d" * 65, "E" * 64, good + "\n", "g" * 64):
        assert link.link_command(paths, "trust", [bad, "name"]) == 1
        capsys.readouterr()
        assert json.dumps(_peers(paths)) == before, "file unchanged for bad id"


def test_link_command_rejects_bad_names(env, capsys):
    paths = env.paths
    good = "f" * 64
    assert link.link_command(paths, "trust", [good, "laptop"]) == 0
    capsys.readouterr()
    before = json.dumps(_peers(paths))
    for bad in ("", "-x", "a b", "x" * 33, "devbox\n"):
        assert link.link_command(paths, "trust", [good, bad]) == 1
        capsys.readouterr()
        assert json.dumps(_peers(paths)) == before, "file unchanged for bad name"


def test_link_command_wrong_arg_count(env, capsys):
    paths = env.paths
    assert link.link_command(paths, "trust", ["a" * 64]) == 2
    assert link.link_command(paths, "untrust", ["a", "b"]) == 2
    assert "usage:" in capsys.readouterr().err
    assert not link.peers_file(paths).exists()


def test_link_command_untrust_by_name_and_id(env, capsys):
    paths = env.paths
    id1, id2 = "a" * 64, "b" * 64
    assert link.link_command(paths, "trust", [id1, "laptop"]) == 0
    assert link.link_command(paths, "trust", [id2, "phone"]) == 0
    capsys.readouterr()

    assert link.link_command(paths, "untrust", ["laptop"]) == 0
    assert capsys.readouterr().out.strip() == "ok"
    assert _peers(paths) == [{"id": id2, "name": "phone"}]

    assert link.link_command(paths, "untrust", [id2]) == 0
    assert capsys.readouterr().out.strip() == "ok"
    assert _peers(paths) == []

    # unknown name: still exit 0, prints ok, file stays 0600
    assert link.link_command(paths, "untrust", ["ghost"]) == 0
    assert capsys.readouterr().out.strip() == "ok"
    assert os.stat(link.peers_file(paths)).st_mode & 0o777 == 0o600


def test_link_peers_written_atomically(env):
    paths = env.paths
    assert link.link_command(paths, "trust", ["a" * 64, "laptop"]) == 0
    assert link.link_command(paths, "untrust", ["laptop"]) == 0
    assert not [p for p in paths.config_file.parent.iterdir() if ".tmp" in p.name]


# --- 5. valid_card ---------------------------------------------------------------------------
def test_valid_card_keeps_good_addrs():
    card = valid_card({"id": "a" * 64, "port": 47823,
                     "addrs": ["192.0.2.1:47823", "[2001:db8::1]:47823", "2001:db8::2", "192.0.2.9"]})
    assert card == {"id": "a" * 64, "port": 47823,
                  "addrs": ["192.0.2.1:47823", "[2001:db8::1]:47823", "2001:db8::2", "192.0.2.9"]}


def test_valid_card_drops_bad_addrs_keeps_card():
    card = valid_card({"id": "a" * 64, "port": 47823,
                     "addrs": ["192.0.2.1:47823", "example.com:1", "1.2.3.4:0", "1.2.3.4:70000",
                               5, "", "x" * 80]})
    assert card is not None
    assert card["addrs"] == ["192.0.2.1:47823"]


def test_valid_card_rejects_bad_cards():
    assert valid_card({"id": "a" * 63, "port": 47823, "addrs": ["192.0.2.1:47823"]}) is None   # bad id
    assert valid_card({"port": 47823, "addrs": ["192.0.2.1:47823"]}) is None                       # missing id
    for port in (None, True, False, 0, 70000):
        assert valid_card({"id": "a" * 64, "port": port, "addrs": ["192.0.2.1:47823"]}) is None
    assert valid_card({"id": "a" * 64, "port": 47823, "addrs": "192.0.2.1"}) is None            # not a list
    assert valid_card(["a" * 64, 47823, ["192.0.2.1:47823"]]) is None                            # not a dict


def test_valid_card_keeps_at_most_16():
    addrs = [f"192.0.2.{i}:47823" for i in range(1, 21)]
    card = valid_card({"id": "a" * 64, "port": 47823, "addrs": addrs})
    assert len(card["addrs"]) == 16


# --- 6. hello -------------------------------------------------------------------------------
class Lines(io.StringIO):
    """A writer that stops the watch loop after `n` lines (mirrors tests/test_remote.py)."""
    def __init__(self, n):
        super().__init__()
        self.n = n

    def write(self, s):
        super().write(s)
        if self.getvalue().count("\n") >= self.n:
            raise BrokenPipeError
        return len(s)


def test_watch_hello_carries_link_card(env):
    card = {"id": "a" * 64, "port": 47823, "addrs": ["192.0.2.9:47823"]}
    out = Lines(1)
    assert remote.watch(env.paths, None, "split", out=out, link=card) == 0
    hello = json.loads(out.getvalue().splitlines()[0])["hello"]
    assert hello["link"] == card

    out2 = Lines(1)
    assert remote.watch(env.paths, None, "split", out=out2, link=None) == 0
    hello2 = json.loads(out2.getvalue().splitlines()[0])["hello"]
    assert "link" not in hello2


def test_hello_link_none_when_direct_link_off(env):
    cfg = dataclasses.replace(load_config(env.paths.config_file), direct_link=False)
    assert link.hello_link(env.paths, cfg) is None


def test_hello_link_none_without_daemon_socket(env):
    assert link.hello_link(env.paths, load_config(env.paths.config_file)) is None


def test_hello_link_returns_self_card_when_daemon_listens():
    short = tempfile.mkdtemp(dir="/tmp")
    try:
        paths = config.get_paths({"FORCE_STATE": short, "FORCE_CONFIG": short + "/settings.json",
                                "FORCE_HOME": short + "/home"})
        _write_self(paths)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(link.sock_file(paths)))
        s.listen(1)
        try:
            card = link.hello_link(paths, Config())
        finally:
            s.close()
        assert card is not None and card["id"] == "a" * 64 and card["port"] == 47823
    finally:
        shutil.rmtree(short, ignore_errors=True)


# --- 7. pairing ------------------------------------------------------------------------------
class FakeRun:
    """Records argv; each call returns the `result` object given (or raises the given exception)."""
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.exc is not None:
            raise self.exc
        return self.result


class Res:
    def __init__(self, rc=0, out="ok\n"):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def _pair_env(env, self_id="a" * 64):
    _write_self(env.paths, self_id)
    r = remote.Remote("devbox", "devbox")
    card = {"id": "b" * 64, "port": 47823, "addrs": ["192.0.2.2:47823"]}
    return r, card


def test_pair_trusts_remote_and_records_its_card(env, monkeypatch):
    paths = env.paths
    r, card = _pair_env(env)
    run = FakeRun(Res(0, "ok\n"))
    got = link.on_hello(paths, r, card, Config(), run=run)
    assert got == "paired"
    assert run.calls == [remote.ssh_argv(paths, r, remote.REMOTE_FORCE, "link", "trust", "a" * 64, link.my_name())]
    lf = link.link_file(paths, "devbox")
    assert json.loads(lf.read_text()) == {"id": "b" * 64, "addrs": ["192.0.2.2:47823"], "port": 47823}
    assert os.stat(lf).st_mode & 0o777 == 0o600


def test_pair_without_self_card_is_unpaired(env, monkeypatch):
    paths = env.paths
    r = remote.Remote("devbox", "devbox")
    card = {"id": "b" * 64, "port": 47823, "addrs": ["192.0.2.2:47823"]}
    run = FakeRun(Res(0, "ok\n"))
    assert link.on_hello(paths, r, card, Config(), run=run) == "unpaired"
    assert run.calls == []


def test_pair_known_id_updates_addrs_keeps_state(env, monkeypatch):
    paths = env.paths
    r, card = _pair_env(env)
    d = link.link_file(paths, "devbox")
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(json.dumps({"id": "b" * 64, "addrs": ["192.0.2.2:47823"], "port": 47823,
                           "state": "direct", "reason": "", "since_ms": 5}))
    new_card = dict(card, addrs=["192.0.2.99:47823"])
    run = FakeRun(Res(0, "ok\n"))
    assert link.on_hello(paths, r, new_card, Config(), run=run) == "updated"
    assert run.calls == []
    data = json.loads(d.read_text())
    assert data["addrs"] == ["192.0.2.99:47823"]
    assert data["state"] == "direct" and data["reason"] == "" and data["since_ms"] == 5


def test_pair_changed_id_pairs_again_dropping_state(env, monkeypatch):
    paths = env.paths
    r, _card = _pair_env(env)
    d = link.link_file(paths, "devbox")
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(json.dumps({"id": "c" * 64, "addrs": [], "port": 47823,
                           "state": "direct", "reason": "", "since_ms": 5}))
    card = {"id": "b" * 64, "port": 47823, "addrs": ["192.0.2.2:47823"]}
    run = FakeRun(Res(0, "ok\n"))
    assert link.on_hello(paths, r, card, Config(), run=run) == "paired"
    assert len(run.calls) == 1
    data = json.loads(d.read_text())
    assert data["id"] == "b" * 64
    assert not any(k in data for k in ("state", "reason", "since_ms"))


def test_pair_run_failure_is_unpaired(env, monkeypatch):
    paths = env.paths
    r, card = _pair_env(env)
    run = FakeRun(Res(1, "ok\n"))
    assert link.on_hello(paths, r, card, Config(), run=run) == "unpaired"
    assert not link.link_file(paths, "devbox").exists()

    run2 = FakeRun(Res(0, "not ok\n"))
    assert link.on_hello(paths, r, card, Config(), run=run2) == "unpaired"


def test_pair_run_timeout_is_unpaired(env, monkeypatch):
    paths = env.paths
    r, card = _pair_env(env)
    run = FakeRun(exc=subprocess.TimeoutExpired(["ssh"], 30))
    assert link.on_hello(paths, r, card, Config(), run=run) == "unpaired"


def test_pair_direct_link_off_is_none(env, monkeypatch):
    paths = env.paths
    r, card = _pair_env(env)
    run = FakeRun(Res(0, "ok\n"))
    assert link.on_hello(paths, r, card, Config(direct_link=False), run=run) == "none"
    assert run.calls == []


def test_pair_invalid_card_is_none(env, monkeypatch):
    paths = env.paths
    _write_self(paths := env.paths)
    r = remote.Remote("devbox", "devbox")
    run = FakeRun(Res(0, "ok\n"))
    assert link.on_hello(paths, r, {"id": "Z" * 64, "port": 47823, "addrs": []},
                       Config(), run=run) == "none"
    assert run.calls == []


def test_watcher_link_hello_calls_on_hello_and_swallows_errors(env, monkeypatch, capsys):
    paths = env.paths
    r = remote.Remote("devbox", "devbox")
    w = remote.Watcher(paths, r)
    card = {"id": "b" * 64, "port": 47823, "addrs": ["192.0.2.2:47823"]}
    seen = []
    monkeypatch.setattr(link, "on_hello",
                       lambda paths, r, card, cfg: seen.append(card) or "paired")
    w._link_hello(card)
    assert seen == [card]

    monkeypatch.setattr(link, "on_hello", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    w._link_hello(card)                 # must not raise
    assert "boom" in capsys.readouterr().err


# --- 8. host remove ---------------------------------------------------------------------------
def test_remote_remove_unpairs_and_forgets(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    paths, r = cockpit.paths, remote.Remote("devbox", "devbox")
    remote.save(paths, [r])
    d = link.link_file(paths, "devbox")
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(json.dumps({"id": "b" * 64, "addrs": ["192.0.2.2:47823"], "port": 47823}))
    calls = []
    class Res:
        returncode, stdout, stderr = 0, "ok\n", ""
    monkeypatch.setattr(link.subprocess, "run", lambda argv, **k: calls.append(argv) or Res())
    assert cli.remote_command(cockpit, "remove", "devbox", None, False) == 0
    assert calls == [remote.ssh_argv(cockpit.paths, r, "~/.local/bin/force", "link", "untrust", link.my_name())]
    assert not d.exists()
    assert not remote.remote_dir(cockpit.paths, "devbox").exists()
    assert remote.load(cockpit.paths) == []


def test_remote_remove_without_link_json_runs_no_ssh(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    paths = cockpit.paths
    remote.save(paths, [remote.Remote("devbox", "devbox")])
    calls = []
    monkeypatch.setattr(link.subprocess, "run", lambda argv, **k: calls.append(argv))
    assert cli.remote_command(cockpit, "remove", "devbox", None, False) == 0
    assert calls == []
    assert remote.load(cockpit.paths) == []


def test_remote_remove_survives_oserror(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    paths = cockpit.paths
    remote.save(paths, [remote.Remote("devbox", "devbox")])
    d = link.link_file(paths, "devbox")
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(json.dumps({"id": "b" * 64, "addrs": [], "port": 47823}))
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(link.subprocess, "run", boom)
    assert cli.remote_command(cockpit, "remove", "devbox", None, False) == 0
    assert not d.exists()
    assert remote.load(cockpit.paths) == []


# --- 9. settings CLI --------------------------------------------------------------------------
def test_settings_direct_link_applies(env, monkeypatch, capsys):
    cockpit = env.make_cockpit()
    calls = []
    monkeypatch.setattr(cli.services, "apply_link", lambda on, run=None: calls.append(on))
    # linkPort is applied with the effective directLink (on by default)
    assert cli.settings_command(cockpit, "set", "linkPort", "50000", False, False) == 0
    assert calls == [True]
    capsys.readouterr()

    assert cli.settings_command(cockpit, "set", "animations", "off", False, False) == 0
    assert calls == [True]                           # animations is not a link setting
    capsys.readouterr()

    assert cli.settings_command(cockpit, "set", "directLink", "false", False, False) == 0
    assert calls == [True, False]
    capsys.readouterr()

    assert cli.settings_command(cockpit, "unset", "directLink", None, False, False) == 0
    assert calls == [True, False, True]             # back to the default: on


# --- 11. systemd/force-link.service ---------------------------------------------------------------
def test_force_link_service_unit(tmp_path):
    root = Path(__file__).resolve().parent.parent
    text = (root / "systemd" / "force-link.service").read_text()
    assert "ExecStart=%h/.local/bin/force link-daemon" in text
    assert "Restart=always" in text
    assert "RestartSec=3" in text
    assert "RestartPreventExitStatus=0" in text
    assert "WantedBy=default.target" in text
    force = (root / "systemd" / "force.service").read_text()
    link_env = next(l for l in text.splitlines() if l.startswith("Environment="))
    force_env = next(l for l in force.splitlines() if l.startswith("Environment="))
    assert link_env == force_env
