"""Split mode (SPEC §39): sessions alone in the host tmux, the sidebar/header in a ui tmux per terminal."""
import dataclasses
import os
import subprocess
import time
import uuid
from pathlib import Path

from force import cli
from force.daemon import Daemon
from force.registry import locked


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def split_cockpit(env):
    cockpit = env.make_cockpit()
    cockpit.cfg = dataclasses.replace(cockpit.cfg, ui="split")
    return cockpit


def add(cockpit, name):
    with locked(cockpit.paths.home) as reg:
        e = reg.add(name, str(uuid.uuid4()), "/tmp")
    cockpit.create(e)
    return e


def roles(tmux, session=None):
    return sorted(p.role or "-" for p in tmux.panes() if session is None or p.session == session)


def test_split_create_is_bare(env):
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    assert roles(cockpit.tmux, "s1") == ["agent"]


def test_daemon_strips_classic_panes_in_split_mode(env):
    cockpit = env.make_cockpit()                    # classic: sidebar + header + agent
    add(cockpit, "s1")
    assert roles(cockpit.tmux, "s1") == ["agent", "header", "sidebar"]
    cockpit.tmux.run("new-window", "-d", "-t", "=s1:", "-n", "force-stash", "sleep 100")
    cockpit.cfg = dataclasses.replace(cockpit.cfg, ui="split")
    d = Daemon(cockpit)
    d.tick()
    assert roles(cockpit.tmux, "s1") == ["agent"]
    names = cockpit.tmux.run("list-windows", "-a", "-F", "#{window_name}").split()
    assert "force-stash" not in names


def test_build_ui_session_composes_the_window(env):
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    name = cli.build_ui_session(cockpit, "s1", "ui-test", 150, 40)
    ui = cockpit.ui_tmux
    assert ui.server_running()
    panes = [p for p in ui.panes() if p.session == name]
    assert sorted(p.role for p in panes) == ["agent", "header", "sidebar"]
    main = next(p for p in panes if p.role == "agent")
    tty = ui.run("display", "-p", "-t", main.pane_id, "#{pane_tty}")
    assert ui.run("show", "-wqv", "-t", main.pane_id, "@force_nested") == tty
    assert ui.run("show", "-wqv", "-t", main.pane_id, "@force_session") == "s1"
    assert ui.run("show", "-wqv", "-t", main.pane_id, "@force_host_tmux") == f"tmux -L {cockpit.paths.tmux_socket}"
    assert ui.get_option("@force_state") == str(cockpit.paths.state)
    # the main pane is active (keys go to the session), the nested command names the host session
    assert ui.run("display", "-p", "-t", name + ":", "#{pane_id}") == main.pane_id
    start = ui.run("display", "-p", "-t", main.pane_id, "#{pane_start_command}")
    assert "nested" in start and "s1" in start


def test_build_ui_session_without_sidebar(env):
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    cockpit.ui_tmux.run("start-server", ";", "set", "-g", "exit-empty", "off", ";", "set", "-g", "@force_sidebar", "off")
    name = cli.build_ui_session(cockpit, "s1", "ui-nosb", 120, 30)
    assert sorted(p.role for p in cockpit.ui_tmux.panes() if p.session == name) == ["agent", "header"]


def test_ui_command_sets_and_reports_the_mode(env, capsys):
    import json as _json
    cockpit = env.make_cockpit()
    cfg = cockpit.paths.config_file
    cfg.write_text('{"layout": "classic", "animations": "subtle"}')
    assert cli.ui_command(cockpit, "split") == 0
    assert _json.loads(cfg.read_text()) == {"layout": "split", "animations": "subtle"}
    assert cli.ui_command(cockpit, None) == 0           # reports the loaded config (classic here)
    # back to classic: a running ui server is ended (its nested views would show the host sidebars again)
    cockpit.ui_tmux.run("new-session", "-d", "-s", "x", "sleep 100")
    assert cli.ui_command(cockpit, "classic") == 0
    assert not cockpit.ui_tmux.server_running()
    assert _json.loads(cfg.read_text())["layout"] == "classic"


def test_inside_cockpit_recognises_the_ui_socket(env, monkeypatch):
    cockpit = env.make_cockpit()
    monkeypatch.setenv("TMUX", f"/tmp/tmux-1/{cockpit.paths.ui_socket},1,0")
    assert cli._inside_cockpit(cockpit)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/other,1,0")
    assert not cli._inside_cockpit(cockpit)


def test_daemon_publishes_switch_targets_on_the_ui_server(env):
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    add(cockpit, "s2")
    cli.build_ui_session(cockpit, "s1", "ui-t", 120, 30)
    Daemon(cockpit).tick()
    assert cockpit.ui_tmux.get_option("@force_idx_1") == "s1"
    assert cockpit.ui_tmux.get_option("@force_idx_2") == "s2"
    assert cockpit.tmux.get_option("@force_ui_socket") == cockpit.paths.ui_socket


def test_switched_hook_updates_the_ui_window(env):
    """The host's switch hook (switched.sh) tells the ui window whose nested client switched."""
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    add(cockpit, "s2")
    name = cli.build_ui_session(cockpit, "s1", "ui-hook", 120, 30)
    ui = cockpit.ui_tmux
    main = next(p for p in ui.panes() if p.session == name and p.role == "agent")
    nested = ui.run("show", "-wqv", "-t", main.pane_id, "@force_nested")
    cockpit.tmux.set_option("@force_ui_socket", cockpit.paths.ui_socket)
    share = Path(cli.__file__).resolve().parent.parent / "share"
    sock = cockpit.tmux.run("display", "-p", "#{socket_path}")
    subprocess.run(["sh", str(share / "switched.sh"), sock, "s2", "s1", nested], timeout=20,
                   env=dict(os.environ, PATH=os.environ["PATH"]))
    assert ui.run("show", "-wqv", "-t", main.pane_id, "@force_session") == "s2"
    # another terminal's ui window is not touched
    subprocess.run(["sh", str(share / "switched.sh"), sock, "s1", "s2", "/dev/pts/nobody"], timeout=20)
    assert ui.run("show", "-wqv", "-t", main.pane_id, "@force_session") == "s2"


def test_doctor_in_split_mode(env):
    from force.doctor import check
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    codes = {p.code for p in check(cockpit, systemd=False)}
    assert not codes & {"header-missing", "sidebar-missing", "split-extra-pane", "ui-incomplete"}, codes
    cockpit.tmux.run("split-window", "-t", "=s1:", "sleep 100")
    assert "split-extra-pane" in {p.code for p in check(cockpit, systemd=False)}
    name = cli.build_ui_session(cockpit, "s1", "ui-doc", 120, 30)
    hdr = next(p for p in cockpit.ui_tmux.panes() if p.session == name and p.role == "header")
    cockpit.ui_tmux.kill_pane(hdr.pane_id)
    probs = [p for p in check(cockpit, systemd=False) if p.code == "ui-incomplete"]
    assert probs and "header" in probs[0].detail


def test_tiers_are_recomputed_on_the_ui_server(env, monkeypatch):
    """The ui server's clients are the real terminals: the daemon's periodic tier pass covers it too."""
    import force.daemon as dm
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    cli.build_ui_session(cockpit, "s1", "ui-tier", 120, 30)
    calls = []
    real = dm.subprocess.run

    def spy(argv, *a, **k):
        if isinstance(argv, list) and argv[:1] == ["sh"] and argv[1].endswith("tier.sh"):
            calls.append(argv[2])
            return real(["true"])
        return real(argv, *a, **k)
    monkeypatch.setattr(dm.subprocess, "run", spy)
    ui = cockpit.ui_tmux
    monkeypatch.setattr(ui, "clients", lambda: [type("C", (), {"session": "ui-tier"})()])
    d = Daemon(cockpit)
    d._TIER_EVERY = 0
    d._step_tiers()
    ui_sock = real(ui._cmd(["display", "-p", "#{socket_path}"]), capture_output=True, text=True).stdout.strip()
    assert ui_sock in calls


def test_daemon_ends_ui_sessions_without_a_terminal(env, monkeypatch):
    """A terminal closed during the intro leaves its ui session unattached for good: ended after a minute."""
    import force.daemon as dm
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    cli.build_ui_session(cockpit, "s1", "ui-orphan", 120, 30)
    d = Daemon(cockpit)
    d.tick()
    assert cockpit.ui_tmux.has_session("ui-orphan")              # young: may still be about to attach
    real = time.time
    monkeypatch.setattr(dm.time, "time", lambda: real() + 120)
    d.tick()
    assert not cockpit.ui_tmux.server_running() or not cockpit.ui_tmux.has_session("ui-orphan")


def test_ui_server_app_path_follows_upgrades(env, tmp_path):
    """@force_app on the ui server is the app symlink as given, not the release it points at today: the
    bindings run the live release's scripts after an upgrade."""
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    app = tmp_path / "app"
    app.symlink_to(Path(cockpit.paths.tmux_conf).resolve().parent.parent)
    cockpit.paths = dataclasses.replace(cockpit.paths, tmux_conf=app / "share" / "tmux.conf")
    cli.build_ui_session(cockpit, "s1", "ui-app", 150, 40)
    assert cockpit.ui_tmux.get_option("@force_app") == str(app)


def test_build_ui_session_sets_the_glyph_tier_before_the_panes(env):
    """A host switch builds a new ui session: with the tier set at creation its sidebar and header start in
    it, instead of the plain look until their next tier read (SPEC §45)."""
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    rich = cli.build_ui_session(cockpit, "s1", "ui-rich", 150, 40, glyphs="rich")
    assert cockpit.ui_tmux.run("show", "-qv", "-t", "=" + rich + ":", "@force_glyphs") == "rich"
    auto = cli.build_ui_session(cockpit, "s1", "ui-auto", 150, 40, glyphs="auto")
    assert cockpit.ui_tmux.run("show", "-qv", "-t", "=" + auto + ":", "@force_glyphs") == ""


# --- the text colour (SPEC §45) -------------------------------------------------------------------
def _style(tmux, target):
    """A window's own window-style ("" when it has none and the server's global one applies)."""
    return tmux.run("show", "-wqv", "-t", target, "window-style")


def test_build_ui_session_leaves_the_text_colour_to_a_terminal_that_took_the_theme_s(env):
    """Dim text (Claude Code's prompt suggestion) is drawn at full strength in VTE when tmux gives it an RGB colour:
    the window of a terminal whose own text colour is the theme's forces none."""
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    own = cli.build_ui_session(cockpit, "s1", "ui-own", 150, 40, own_fg=True)
    ui = cockpit.ui_tmux
    assert _style(ui, "=" + own + ":") == "bg=#262626"
    assert ui.run("show", "-wqv", "-t", "=" + own + ":", "window-active-style") == "bg=#262626"
    assert ui.run("show", "-qv", "-t", "=" + own + ":", "@force_term_fg") == "on"
    other = cli.build_ui_session(cockpit, "s1", "ui-other", 150, 40)
    assert _style(ui, "=" + other + ":") == ""                       # the ui server's fg=#d0d0d0, as before
    assert ui.run("show", "-qv", "-t", "=" + other + ":", "@force_term_fg") == ""


def test_ui_host_keeps_the_terminal_s_text_colour(env, monkeypatch):
    """A host switch builds a new ui session for the same terminal: it keeps that terminal's text colour choice."""
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    built = []
    real = cli.build_ui_session
    monkeypatch.setattr(cli, "build_ui_session", lambda *a, **k: built.append(a[-1]) or real(*a, **k))
    for own in (True, False):
        name = real(cockpit, "s1", f"ui-keep-{own}", 150, 40, own_fg=own)
        win = cockpit.ui_tmux.run("display", "-p", "-t", "=" + name + ":", "#{window_id}")
        assert cli.ui_host_command(cockpit, "local", win) == 0
    assert built == [True, False]


def test_daemon_host_forces_no_text_colour_in_split_mode(env):
    """Split mode: the host server leaves default-coloured text default, so it reaches the ui windows (which
    decide per terminal); classic keeps tmux.conf's text colour, its clients being the terminals themselves."""
    cockpit = split_cockpit(env)
    add(cockpit, "s1")
    d = Daemon(cockpit)
    d.tick()
    gv = lambda k: cockpit.tmux.run("show", "-gv", k)
    assert gv("window-style") == "bg=#262626" and gv("window-active-style") == "bg=#262626"
    cockpit.cfg = dataclasses.replace(cockpit.cfg, ui="classic")
    d.tick()
    assert gv("window-style") == "fg=#d0d0d0,bg=#262626" and gv("window-active-style") == "fg=#d0d0d0,bg=#262626"
