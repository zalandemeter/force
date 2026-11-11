import json
import os
from pathlib import Path

import pytest

from force.config import Config, get_paths, load_config, ensure_dirs, save_setting, unset_setting, parse_value, tmux_bin_dir


def test_get_paths_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = get_paths({})
    assert p.home == tmp_path / ".local/share/force"
    assert p.state == tmp_path / ".local/state/force"
    assert p.claude_dir == tmp_path / ".claude"
    assert p.claude_bin == tmp_path / ".local/bin/claude"
    assert p.tmux_socket == "force"
    assert p.tmux_conf == Path(__file__).resolve().parent.parent / "share" / "tmux.conf"
    assert p.config_file == tmp_path / ".config/force/settings.json"
    # properties
    assert p.registry == p.home / "registry.json"
    assert p.registry_lock == p.home / "registry.lock"
    assert p.archive == p.home / "archive"
    assert p.snapshot == p.state / "snapshot.json"
    assert p.seen == p.state / "seen.json"
    assert p.header_dir == p.state / "header"
    assert p.daemon_lock == p.state / "daemon.lock"


def test_tmux_bin_dir_none_without_binary(tmp_path):
    share = tmp_path / "share"
    share.mkdir(parents=True)
    (share / "tmux.conf").write_text("")
    assert tmux_bin_dir(get_paths({"FORCE_TMUX_CONF": str(share / "tmux.conf")})) is None


def test_tmux_bin_dir_none_when_not_executable(tmp_path):
    share = tmp_path / "share"
    tbin = share / "tmux-bin"
    tbin.mkdir(parents=True)
    (tbin / "tmux").write_text("not executable")
    (share / "tmux.conf").write_text("")
    assert tmux_bin_dir(get_paths({"FORCE_TMUX_CONF": str(share / "tmux.conf")})) is None


def test_tmux_bin_dir_finds_executable(tmp_path):
    share = tmp_path / "share"
    tbin = share / "tmux-bin"
    tbin.mkdir(parents=True)
    tmux = tbin / "tmux"
    tmux.write_text("#!/bin/sh\n")
    tmux.chmod(0o755)
    (share / "tmux.conf").write_text("")
    assert tmux_bin_dir(get_paths({"FORCE_TMUX_CONF": str(share / "tmux.conf")})) == tbin


def test_get_paths_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/nonexistent/home")
    env = {
        "HOME": str(tmp_path),
        "FORCE_HOME": "~/data",
        "FORCE_STATE": str(tmp_path / "st"),
        "FORCE_CLAUDE_DIR": str(tmp_path / "claude"),
        "FORCE_CLAUDE_BIN": "/usr/bin/claude",
        "FORCE_TMUX_SOCKET": "sock",
        "FORCE_TMUX_CONF": "~/conf.tmux",
        "FORCE_CONFIG": str(tmp_path / "cfg.json"),
    }
    p = get_paths(env)
    assert p.home == tmp_path / "data"
    assert p.state == tmp_path / "st"
    assert p.claude_dir == tmp_path / "claude"
    assert p.claude_bin == Path("/usr/bin/claude")
    assert p.tmux_socket == "sock"
    assert p.tmux_conf == tmp_path / "conf.tmux"
    assert p.config_file == tmp_path / "cfg.json"


def test_load_config_missing_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg == Config()
    assert cfg.ui == "split"


def test_load_config_valid(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"staleAfterHours": 5, "sidebarWidth": 40, "autoUpgradeSessions": False,
                           "startGraceSeconds": 7.5, "icons": "on", "layout": "classic", "unreadReplies": False}))
    cfg = load_config(f)
    assert cfg.stale_hours == 5
    assert cfg.sidebar_width == 40
    assert cfg.auto_upgrade is False
    assert cfg.start_grace_seconds == 7.5
    assert cfg.glyphs == "rich"           # icons on -> rich
    assert cfg.ui == "classic"            # layout -> ui
    assert cfg.unread_replies is False


def test_load_config_invalid_json_warns_defaults(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text("not json {{{")
    cfg = load_config(f)
    assert cfg == Config()
    assert "warning" in capsys.readouterr().err


def test_load_config_top_level_array_warns_defaults(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text("[1, 2, 3]")
    cfg = load_config(f)
    assert cfg == Config()
    assert "not a JSON object" in capsys.readouterr().err


def test_load_config_bool_for_int_field_rejected(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"sidebarWidth": true}')
    cfg = load_config(f)
    assert cfg.sidebar_width == 29
    assert "warning" in capsys.readouterr().err


def test_load_config_bool_for_number_field_rejected(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"staleAfterHours": false}')
    cfg = load_config(f)
    assert cfg.stale_hours == 12
    assert "warning" in capsys.readouterr().err


def test_load_config_nan_rejected(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"staleAfterHours": NaN}')
    cfg = load_config(f)
    assert cfg.stale_hours == 12
    assert "warning" in capsys.readouterr().err


def test_load_config_zero_negative_rejected(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"staleAfterHours": 0, "sidebarWidth": -3}')
    cfg = load_config(f)
    assert cfg.stale_hours == 12
    assert cfg.sidebar_width == 29
    assert "warning" in capsys.readouterr().err


def test_load_config_intro_cell_aspect_zero_allowed(tmp_path):
    f = tmp_path / "c.json"
    f.write_text('{"introCellAspect": 0}')
    assert load_config(f).intro_cell_aspect == 0
    f.write_text('{"introCellAspect": 1.5}')
    assert load_config(f).intro_cell_aspect == 1.5


def test_load_config_unknown_key_ignored(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"bogus": 1, "staleAfterHours": 3}')
    cfg = load_config(f)
    assert cfg.stale_hours == 3
    assert "unknown settings key" in capsys.readouterr().err


def test_load_config_bool_wrong_type_rejected(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"autoUpgradeSessions": "yes"}')
    cfg = load_config(f)
    assert cfg.auto_upgrade is True
    assert "warning" in capsys.readouterr().err


def test_load_config_icons_mapping(tmp_path):
    f = tmp_path / "c.json"
    f.write_text('{"icons": "on"}')
    assert load_config(f).glyphs == "rich"
    f.write_text('{"icons": "off"}')
    assert load_config(f).glyphs == "plain"
    f.write_text('{"icons": "auto"}')
    assert load_config(f).glyphs == "auto"


def test_load_config_icons_bad_value_rejected(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"icons": "extra"}')
    assert load_config(f).glyphs == "auto"
    assert "warning" in capsys.readouterr().err


def test_load_config_schema_key_ignored(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"$schema": "file:///x.schema.json"}')
    assert load_config(f) == Config()
    assert capsys.readouterr().err == ""


def test_load_config_dev_reload_bool(tmp_path):
    f = tmp_path / "c.json"
    f.write_text('{"reloadOnChange": false}')
    assert load_config(f).dev_reload is False


def test_load_config_dev_reload_int_rejected(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"reloadOnChange": 1}')
    assert load_config(f).dev_reload is True
    assert "warning" in capsys.readouterr().err


def test_load_config_rich_terminals_nonempty(tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text('{"richTerminals": ""}')
    assert load_config(f).rich_terminals == "kitty|ghostty|wezterm"
    assert "warning" in capsys.readouterr().err
    f.write_text('{"richTerminals": "vte"}')
    assert load_config(f).rich_terminals == "vte"


def test_save_setting_keeps_other_keys_order_and_schema(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text('{"$schema": "file:///x.json", "animations": "subtle"}')
    save_setting(f, "layout", "split")
    save_setting(f, "intro", False)
    assert f.read_text() == '{\n  "$schema": "file:///x.json",\n  "animations": "subtle",\n  "layout": "split",\n  "intro": false\n}\n'
    json.loads(f.read_text())                              # valid JSON
    assert load_config(f).animations == "subtle"
    assert load_config(f).ui == "split"
    assert load_config(f).intro is False


def test_save_setting_creates_missing_file(tmp_path):
    f = tmp_path / "settings.json"
    save_setting(f, "layout", "classic")
    assert json.loads(f.read_text()) == {"layout": "classic"}
    assert f.read_text().endswith("\n")


def test_unset_setting_keeps_other_keys(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text('{"intro": false, "layout": "classic"}')
    unset_setting(f, "intro")
    assert json.loads(f.read_text()) == {"layout": "classic"}
    unset_setting(f, "intro")                             # no key: nothing changes, no error
    assert json.loads(f.read_text()) == {"layout": "classic"}


def test_save_setting_refuses_invalid_file(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text("not json")
    with pytest.raises(ValueError):
        save_setting(f, "layout", "split")
    assert f.read_text() == "not json"                     # never overwritten


def test_save_unset_unknown_key(tmp_path):
    f = tmp_path / "settings.json"
    with pytest.raises(ValueError):
        save_setting(f, "bogus", 1)
    with pytest.raises(ValueError):
        unset_setting(f, "bogus")


def test_parse_value_bool(tmp_path):
    for text, want in (("true", True), ("True", True), ("on", True), ("yes", True),
                      ("false", False), ("off", False), ("no", False), ("OFF", False)):
        assert parse_value("intro", text) is want


def test_parse_value_bool_bad(tmp_path):
    with pytest.raises(ValueError):
        parse_value("intro", "yup")


def test_parse_value_numbers(tmp_path):
    assert parse_value("sidebarWidth", "40") == 40
    assert parse_value("staleAfterHours", "5.5") == 5.5
    assert parse_value("staleAfterHours", "4") == 4
    assert parse_value("introCellAspect", "0") == 0
    with pytest.raises(ValueError):
        parse_value("sidebarWidth", "3.5")              # int, not a float
    with pytest.raises(ValueError):
        parse_value("sidebarWidth", "-1")
    with pytest.raises(ValueError):
        parse_value("staleAfterHours", "zero")


def test_parse_value_choices_and_unknown(tmp_path):
    assert parse_value("layout", "classic") == "classic"
    assert parse_value("icons", "on") == "on"
    with pytest.raises(ValueError):
        parse_value("icons", "rich")                     # public value is "on", not the Config field "rich"
    with pytest.raises(ValueError):
        parse_value("animations", "fast")
    with pytest.raises(ValueError) as e:
        parse_value("bogus", "1")
    assert "animations" in str(e.value)                # unknown: lists the public keys


def test_schema_file_in_sync():
    from force import config as cfgmod
    shipped = json.loads((Path(__file__).resolve().parent.parent / "share" / "settings.schema.json").read_text())
    assert shipped == cfgmod.schema()


def test_ensure_dirs(tmp_path):
    p = get_paths({"FORCE_HOME": str(tmp_path / "home"), "FORCE_STATE": str(tmp_path / "state")})
    ensure_dirs(p)
    assert p.home.is_dir()
    assert p.archive.is_dir()
    assert p.state.is_dir()
    assert p.header_dir.is_dir()


def test_default_tmux_conf_keeps_symlinked_path(tmp_path, monkeypatch):
    """Live releases are reached through a symlink; the conf path must not be resolved through it."""
    from force import config as cfgmod
    real = tmp_path / "releases" / "r1" / "force"
    real.mkdir(parents=True)
    link = tmp_path / "app"
    link.symlink_to(tmp_path / "releases" / "r1")
    monkeypatch.setattr(cfgmod, "__file__", str(link / "force" / "config.py"))
    p = cfgmod.get_paths({"HOME": str(tmp_path)})
    assert p.tmux_conf == link / "share" / "tmux.conf"


def test_claude_bin_falls_back_to_path(tmp_path):
    from force.config import get_paths
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    sysbin = tmp_path / "usr-bin"
    sysbin.mkdir()
    (sysbin / "claude").write_text("#!/bin/sh\n")
    (sysbin / "claude").chmod(0o755)
    env = {"HOME": str(home), "PATH": str(sysbin)}
    assert get_paths(env).claude_bin == sysbin / "claude", "a system package's claude on PATH"
    own = home / ".local/bin/claude"
    own.write_text("#!/bin/sh\n")
    own.chmod(0o755)
    assert get_paths(env).claude_bin == own, "Claude Code's own installer location wins"
    assert get_paths(dict(env, FORCE_CLAUDE_BIN="/x/claude")).claude_bin.as_posix() == "/x/claude"
    assert get_paths({"HOME": str(tmp_path / "nohome"), "PATH": str(tmp_path / "empty")}).claude_bin == \
        tmp_path / "nohome/.local/bin/claude", "nothing found: the installer location"


def test_is_wsl(tmp_path):
    from force.config import is_wsl
    proc = tmp_path / "proc"
    (proc / "sys/kernel").mkdir(parents=True)
    (proc / "sys/kernel/osrelease").write_text("6.8.0-47-generic\n")
    assert not is_wsl({}, proc)                                           # plain Linux
    assert is_wsl({"WSL_DISTRO_NAME": "Ubuntu-24.04"}, proc)              # WSL's own variables
    assert is_wsl({"WSL_INTEROP": "/run/WSL/1_interop"}, proc)
    (proc / "sys/kernel/osrelease").write_text("5.15.167.4-microsoft-standard-WSL2\n")
    assert is_wsl({}, proc)                                               # the kernel (e.g. under sudo, no env)
    assert not is_wsl({}, tmp_path / "nowhere")                           # no /proc to read
