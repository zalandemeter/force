"""Tests for force/setup.py (the one-line `curl -fsSL base/install.sh | sh` installer) and
scripts/build-release.sh's install.sh generation (via scripts/fill-installer.py)."""
import http.server
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from force import setup as S  # noqa: E402
# the system PATH the tests give setup, plus the folder of the tmux the tests run (Homebrew's on macOS)
SYS_PATH = "/usr/bin:/bin:/usr/local/bin"
if shutil.which("tmux") and os.path.dirname(shutil.which("tmux")) not in SYS_PATH.split(":"):
    SYS_PATH += ":" + os.path.dirname(shutil.which("tmux"))


class R:
    """The result object a fake `run` returns (mirrors subprocess.CompletedProcess fields setup.py reads)."""
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """Records every call and forwards `tmux` to the real binary (the parse check needs a real server)."""

    def __init__(self, handlers=None):
        self.calls = []
        self.handlers = handlers or []

    def handle(self, fn):
        self.handlers.append(fn)
        return fn

    def __call__(self, argv, **kw):
        if hasattr(argv, "split"):           # defensive: always list argv
            argv = argv.split()
        self.calls.append(([str(a) for a in argv], dict(kw)))
        if os.path.basename(argv[0]) == "tmux":
            return subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)
        for fn in self.handlers:
            r = fn([str(a) for a in argv], dict(kw))
            if r is not None:
                return r
        return R()

    def cmd(self, *parts):
        return [p for p in parts if p not in (None, "")]


def make_release(tmp, version="9.9.9"):
    """A fake release rooted at <tmp>/rel/force-<version>: the repo's force/, share/, systemd/ plus a
    VERSION and a fake static sidebar (no tmux-bin: the tests parse with the system tmux)."""
    rel = tmp / "rel" / f"force-{version}"
    rel.mkdir(parents=True)
    for d in ("force", "share", "systemd"):
        shutil.copytree(REPO / d, rel / d, dirs_exist_ok=True)
    (rel / "VERSION").write_text(version + "\n")
    (rel / "share" / "bin").mkdir(parents=True, exist_ok=True)
    sidebar = rel / "share" / "bin" / "force-sidebar"
    sidebar.write_text("#!/bin/sh\nsleep 5\n")
    sidebar.chmod(0o755)
    return rel


def make_home(tmp):
    home = tmp / "home"
    state = tmp / "state"
    data = tmp / "force-data"
    for d in (home, state, data):
        d.mkdir(parents=True, exist_ok=True)
    return home, data


def base_env(tmp, home, data):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "FORCE_HOME": str(data),
        "FORCE_STATE": str(tmp / "state"),
        "FORCE_CONFIG": str(tmp / "force" / "settings.json"),
        "FORCE_CLAUDE_BIN": str(tmp / "no" / "claude"),
        "USER": "testuser",
        "SHELL": "/bin/bash",
        "PATH": SYS_PATH,
        "DISPLAY": "",
    })
    env.pop("WAYLAND_DISPLAY", None)
    # keep the install tests deterministic whatever the developer's shell exports
    env.pop("FORCE_RELEASES_URL", None)
    env.pop("FORCE_DOWNLOAD_BASE", None)
    return env


# --- bootstrap script syntax ------------------------------------------------------------------
def test_get_force_is_posix_sh():
    for shell in ("sh", "dash"):
        if shutil.which(shell):
            r = subprocess.run([shell, "-n", str(REPO / "share" / "get-force.sh")], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr


# --- release / tmux sanity -------------------------------------------------------------------
def test_no_setupcheck_socket_left(tmp_path):
    rel = make_release(tmp_path)
    home, data = make_home(tmp_path)
    env = base_env(tmp_path, home, data)
    fr = FakeRun()
    S.check_tmux(rel, fr, env)
    sock = f"force-setupcheck-{os.getpid()}"
    assert not os.path.exists(f"/tmp/tmux-{os.getuid()}/{sock}"), "setup check socket left behind"
    # the check really sourced both tmux configs on a throwaway socket
    assert any("source-file" in c[0] for c in fr.calls)


def test_check_release_rejects_a_bad_tree(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    with pytest.raises(S.SetupError):
        S.check_release(bad)
    good = make_release(tmp_path)
    assert S.check_release(good) == "9.9.9"


def test_tmux_bin_prefers_bundled(tmp_path):
    rel = make_release(tmp_path)
    (rel / "share" / "tmux-bin").mkdir(parents=True)
    bundled = rel / "share" / "tmux-bin" / "tmux"
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    fr = FakeRun()
    S.check_tmux(rel, fr, {})
    assert bundled in [Path(c[0][0]) for c in fr.calls if "tmux" in c[0][0]]


# --- end to end against a fake HOME -------------------------------------------------------------
def _systemd_present_handlers(fr, active="active", linger="Linger=yes", enable_linger_rc=0):
    fr.handle(lambda a, k: R(0) if a[:2] == ["systemctl", "--user"] and a[2] == "show-environment" else None)
    fr.handle(lambda a, k: R(0, active)
            if a[:3] == ["systemctl", "--user", "show"] and a[3] == "force.service" else None)
    fr.handle(lambda a, k: R(0, linger) if a[0] == "loginctl" and a[2] == "-p" else None)
    fr.handle(lambda a, k: R(enable_linger_rc) if a[0] == "loginctl" and a[1] == "enable-linger" else None)


def test_full_setup_systemd_present(tmp_path, capsys):
    rel = make_release(tmp_path)
    home, data = make_home(tmp_path)
    env = base_env(tmp_path, home, data)
    fr = FakeRun()
    _systemd_present_handlers(fr, active="active")
    rc = S.run_setup(str(rel), sys.executable, env=env, run=fr)
    assert rc == 0

    # launcher
    launcher = home / ".local" / "bin" / "force"
    assert launcher.is_file()
    assert os.stat(launcher).st_mode & 0o777 == 0o755
    content = launcher.read_text()
    assert content.startswith(f"#!{sys.executable} -I\n")
    assert f'sys.path.insert(0, "{os.path.abspath(str(data))}/app")' in content
    assert "from force.cli import main" in content

    # app symlink points at the release
    app = data / "app"
    assert os.path.islink(app)
    assert (app / "VERSION").read_text().strip() == "9.9.9"

    # units copied
    for name in ("force.service", "force-tmux.service", "force-smoke.timer"):
        assert (home / ".config" / "systemd" / "user" / name).is_file()

    # systemctl calls: enable --now exactly the two units; restart only force.service
    enable = [c[0] for c in fr.calls if c[0][2:4] == ["enable", "--now"]]
    assert enable == [["systemctl", "--user", "enable", "--now", "force-tmux.service", "force.service"]]
    restarts = [c[0] for c in fr.calls if c[0][2] == "restart"]
    assert [r[3] for r in restarts] == ["force.service"]
    # never any command touching force-tmux except the enable --now
    tmux_touches = [c[0] for c in fr.calls
                    if any("force-tmux" in p for p in c[0]) and not (c[0][2:4] == ["enable", "--now"])]
    assert tmux_touches == []
    assert "daemon-reload" in [c[0][2] for c in fr.calls]

    # PATH block at the top of ~/.bashrc; PATH note NOT printed because ~/.local/bin wasn't on our PATH
    bashrc = (home / ".bashrc").read_text()
    assert bashrc.startswith("# force: ~/.local/bin on PATH (also for ssh -t HOST force)\n")
    out = capsys.readouterr().out
    assert "open a new terminal first" in out


def test_no_duplicate_path_block_and_idempotent_units(tmp_path, capsys):
    rel = make_release(tmp_path)
    home, data = make_home(tmp_path)
    env = base_env(tmp_path, home, data)
    fr = FakeRun()
    _systemd_present_handlers(fr, active="active")
    S.run_setup(str(rel), sys.executable, env=env, run=fr)
    n_reload1 = sum(1 for c in fr.calls if c[0][2:] == ["daemon-reload"])

    fr2 = FakeRun()
    _systemd_present_handlers(fr2, active="active")
    S.run_setup(str(rel), sys.executable, env=env, run=fr2)
    bashrc = (home / ".bashrc").read_text()
    # the block appears exactly once
    assert bashrc.count("# force: ~/.local/bin on PATH (also for ssh -t HOST force)") == 1
    assert bashrc.count("# force end") == 1
    # second run: nothing changed -> units not rewritten, no daemon-reload
    assert sum(1 for c in fr2.calls if c[0][2:] == ["daemon-reload"]) == 0
    assert n_reload1 == 1


def test_old_bashrc_block_replaced(tmp_path):
    rel = make_release(tmp_path)
    home, data = make_home(tmp_path)
    (home / ".bashrc").write_text(
        "# old stuff at top\n"
        "# force: ~/.local/bin also for non-interactive ssh commands (ssh -t <host> force)\n"
        'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n'
        "# something after\n"
    )
    env = base_env(tmp_path, home, data)
    S.run_setup(str(rel), sys.executable, env=env, run=FakeRun())
    bashrc = (home / ".bashrc").read_text()
    assert "# force: ~/.local/bin on PATH (also for ssh -t HOST force)" in bashrc
    assert "also for non-interactive ssh commands" not in bashrc
    assert bashrc.count("# force end") == 1


def test_zshenv_and_fish_when_shell_matches(tmp_path):
    rel = make_release(tmp_path)
    home, data = make_home(tmp_path)
    env = base_env(tmp_path, home, data)
    env["SHELL"] = "/usr/bin/zsh"
    (home / ".zshrc").write_text("pre\n")
    # fish: no ~/.config/fish yet -> not created for zsh
    S.run_setup(str(rel), sys.executable, env=env, run=FakeRun())
    zshenv = (home / ".zshenv").read_text()
    assert "fish_add_path" not in zshenv
    assert (home / ".config" / "fish" / "conf.d" / "force.fish").exists() is False

    home2, data2 = make_home(tmp_path)
    (home2 / ".config" / "fish").mkdir(parents=True)
    env2 = base_env(tmp_path, home2, data2)
    env2["SHELL"] = "/usr/bin/fish"
    S.run_setup(str(rel), sys.executable, env=env2, run=FakeRun())
    fish = (home2 / ".config" / "fish" / "conf.d" / "force.fish").read_text()
    assert "fish_add_path -g $HOME/.local/bin" in fish


def test_path_note_only_when_missing(tmp_path, capsys):
    rel = make_release(tmp_path)
    home, data = make_home(tmp_path)
    env = base_env(tmp_path, home, data)
    env["PATH"] = f"/usr/bin:/bin:{home}/.local/bin"
    S.run_setup(str(rel), sys.executable, env=env, run=FakeRun())
    assert "open a new terminal first" not in capsys.readouterr().out


def test_systemd_missing_prints_note_no_units(tmp_path, capsys):
    rel = make_release(tmp_path)
    home, data = make_home(tmp_path)
    env = base_env(tmp_path, home, data)
    fr = FakeRun()
    fr.handle(lambda a, k: R(1, "", "systemctl not found")
            if a[:2] == ["systemctl", "--user"] and a[2] == "show-environment" else None)
    S.run_setup(str(rel), sys.executable, env=env, run=fr)
    assert "no systemd here: force starts its services itself" in capsys.readouterr().out
    assert not (home / ".config" / "systemd" / "user").exists()


def test_linger_refused_note(tmp_path, capsys):
    rel = make_release(tmp_path)
    home, data = make_home(tmp_path)
    env = base_env(tmp_path, home, data)
    fr = FakeRun()
    _systemd_present_handlers(fr, linger="Linger=no", enable_linger_rc=1)
    S.run_setup(str(rel), sys.executable, env=env, run=fr)
    out = capsys.readouterr().out
    assert "sessions stop when you log out" in out
    assert "loginctl enable-linger testuser" in out


# --- fonts -----------------------------------------------------------------------------------
def test_fonts_skipped_without_display(tmp_path):
    fr = FakeRun()
    S.setup_fonts(make_release(tmp_path), {"DISPLAY": ""}, fr, wsl=False)
    assert not any("fonts.sh" in c[0][-1] for c in fr.calls)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fontconfig desktops (Linux); macOS: SPEC §51")
def test_fonts_run_when_display_and_missing_family(tmp_path):
    rel = make_release(tmp_path)
    fr = FakeRun()
    fr.handle(lambda a, k: R(0, "DejaVu Sans\nNoto\n") if a[0].endswith("fc-list") else None)
    S.setup_fonts(rel, {"DISPLAY": ":0", "PATH": "/usr/bin:/bin"}, fr, wsl=False)
    assert any(c[0][-1].endswith("fonts.sh") for c in fr.calls)


def test_fonts_skipped_when_family_present(tmp_path):
    rel = make_release(tmp_path)
    fr = FakeRun()
    fr.handle(lambda a, k: R(0, "Symbols Nerd Font Mono:style=Regular\n") if a[0].endswith("fc-list") else None)
    S.setup_fonts(rel, {"DISPLAY": ":0", "PATH": "/usr/bin:/bin"}, fr, wsl=False)
    assert not any(c[0][-1].endswith("fonts.sh") for c in fr.calls)


def test_fonts_on_wsl_set_up_windows_terminal(tmp_path, capsys):
    # WSL: no Linux font (WSLg's DISPLAY notwithstanding); fonts.sh sets up Windows and setup names the profile
    rel = make_release(tmp_path)
    fr = FakeRun()
    fr.handle(lambda a, k: R(0, "") if a[-1].endswith("fonts.sh") else None)
    S.setup_fonts(rel, {"DISPLAY": ":0", "PATH": "/usr/bin:/bin", "WSL_DISTRO_NAME": "Ubuntu-24.04"}, fr, wsl=True)
    assert [c[0][-1].endswith("fonts.sh") for c in fr.calls] == [True]      # no fc-list: Linux fonts don't matter
    assert 'open "Force (Ubuntu-24.04)"' in capsys.readouterr().out


def test_fonts_on_wsl_hint_when_windows_is_out_of_reach(tmp_path, capsys):
    rel = make_release(tmp_path)
    fr = FakeRun()
    fr.handle(lambda a, k: R(3, "") if a[-1].endswith("fonts.sh") else None)
    S.setup_fonts(rel, {"DISPLAY": ":0", "PATH": "/usr/bin:/bin"}, fr, wsl=True)
    assert "force settings set icons on" in capsys.readouterr().out


# --- claude Code hint ------------------------------------------------------------------------
def test_claude_missing_hint(tmp_path, capsys):
    S.check_claude(type("P", (), {"claude_bin": tmp_path / "no" / "claude"})())
    out = capsys.readouterr().out
    assert "Claude Code is not installed" in out
    assert "curl -fsSL https://claude.ai/install.sh | bash" in out


# --- prune -----------------------------------------------------------------------------------
def test_prune_keeps_five_plus_live(tmp_path):
    data = tmp_path / "data"
    releases = data / "releases"
    releases.mkdir(parents=True)
    live = releases / "force-9.0.0"
    live.mkdir()
    others = []
    for v in ("9.0.1", "9.0.2", "9.0.3", "9.0.4", "9.0.5", "9.0.6"):
        d = releases / f"force-{v}"
        d.mkdir()
        others.append(d)
    time.sleep(0.01)
    # newest 5 by mtime: the last five created
    S.prune(data, live)
    remaining = sorted(p.name for p in releases.iterdir())
    assert "force-9.0.6" in remaining
    assert "force-9.0.0" in remaining          # the live one is never deleted
    assert len(remaining) == 6                   # 5 newest others + live


def test_systemd_call_records_exactly(tmp_path):
    """The enable --now is one call with exactly the two units and there is a restart only for force.service."""
    rel = make_release(tmp_path)
    home, data = make_home(tmp_path)
    env = base_env(tmp_path, home, data)
    fr = FakeRun()
    _systemd_present_handlers(fr, active="inactive")
    S.run_setup(str(rel), sys.executable, env=env, run=fr)
    enable = [c[0] for c in fr.calls if c[0][2:4] == ["enable", "--now"]]
    assert enable == [["systemctl", "--user", "enable", "--now", "force-tmux.service", "force.service"]]
    assert not any(c[0][2] == "restart" for c in fr.calls)   # not active -> no restart


# --- the one-line bootstrap, end to end -------------------------------------------------------------
@pytest.fixture
def web_root(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    return root


def _serve(root):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **k)
    import socketserver
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    assert port != 8000, "refusing to serve on the host's dev port"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _arch():
    a = platform.machine()
    return "x86_64" if a in ("x86_64", "amd64") else "aarch64"


def _make_tarball(rel, root, version, arch):
    releases = root / "releases"          # the web root's releases/ dir, like build-release.sh lays out
    releases.mkdir(parents=True, exist_ok=True)
    name = f"force-{version}-linux-{arch}.tar.gz"
    tar = releases / name
    with tarfile.open(tar, "w:gz") as t:
        t.add(rel, arcname=f"force-{version}")
    return tar


def _bootstrap_env(tmp_path, home, data, extra=None):
    """A clean env for running the installer, deterministic regardless of the developer's shell exports."""
    env = dict(os.environ)
    env.update({"HOME": str(home), "FORCE_HOME": str(data),
                "FORCE_STATE": str(tmp_path / "state"),
                "FORCE_CLAUDE_BIN": str(tmp_path / "no" / "claude"),
                "PATH": SYS_PATH})
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("FORCE_RELEASES_URL", None)
    env.pop("FORCE_DOWNLOAD_BASE", None)
    env["DISPLAY"] = ""
    if extra:
        env.update(extra)
    return env


def _run_bootstrap(filled, env):
    return subprocess.run(["sh", str(filled), "--no-services", "--no-fonts"],
                          capture_output=True, text=True, env=env, timeout=180)


def test_bootstrap_end_to_end(tmp_path, capsys):
    arch = _arch()
    version = "9.9.9"
    rel = make_release(tmp_path, version)
    root = tmp_path / "web"
    root.mkdir(exist_ok=True)
    tar = _make_tarball(rel, root, version, arch)

    home = tmp_path / "home"
    data = tmp_path / "fdata"
    for d in (home, data):
        d.mkdir(parents=True, exist_ok=True)

    # fill install.sh the same way build-release.sh does
    sha = subprocess.run(["sha256sum", str(tar)], capture_output=True, text=True).stdout.split()[0]
    filled = tmp_path / "install.sh"
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "fill-installer.py"),
                      str(REPO / "share" / "get-force.sh"), str(filled),
                      version, "http://127.0.0.1:0", "http://127.0.0.1:0/releases", sha, ""])
    assert r.returncode == 0

    httpd, port = _serve(root)
    try:
        # a fresh filler with the real random port
        filled.unlink()
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "fill-installer.py"),
                          str(REPO / "share" / "get-force.sh"), str(filled),
                          version, f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}/releases", sha, ""])
        assert r.returncode == 0
        env = dict(os.environ)
        env.update({"HOME": str(home), "FORCE_HOME": str(data),
                   "FORCE_STATE": str(tmp_path / "state"),
                   "FORCE_CLAUDE_BIN": str(tmp_path / "no" / "claude"),
                   "PATH": SYS_PATH})
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("FORCE_RELEASES_URL", None)
        env.pop("FORCE_DOWNLOAD_BASE", None)
        env["DISPLAY"] = ""
        res = subprocess.run(["sh", str(filled), "--no-services", "--no-fonts"],
                           capture_output=True, text=True, env=env, timeout=180)
        assert res.returncode == 0, res.stderr + res.stdout
        assert (home / ".local" / "bin" / "force").is_file()
        assert (data / "app" / "VERSION").read_text().strip() == version
    finally:
        httpd.shutdown()


def test_bootstrap_wrong_sha_installs_nothing(tmp_path):
    arch = _arch()
    version = "9.9.9"
    rel = make_release(tmp_path, version)
    root = tmp_path / "web2"
    root.mkdir(exist_ok=True)
    tar = _make_tarball(rel, root, version, arch)

    home = tmp_path / "home2"
    data = tmp_path / "fdata2"
    home.mkdir(exist_ok=True)

    filled = tmp_path / "install-wrong.sh"
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "fill-installer.py"),
                      str(REPO / "share" / "get-force.sh"), str(filled),
                      version, "http://127.0.0.1:0", "http://127.0.0.1:0/releases", "0" * 64, ""])
    assert r.returncode == 0
    httpd, port = _serve(root)
    try:
        filled.unlink()
        subprocess.run([sys.executable, str(REPO / "scripts" / "fill-installer.py"),
                      str(REPO / "share" / "get-force.sh"), str(filled),
                      version, f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}/releases", "0" * 64, ""])
        env = dict(os.environ)
        env.update({"HOME": str(home), "FORCE_HOME": str(data),
                   "FORCE_STATE": str(tmp_path / "state2"),
                   "PATH": SYS_PATH})
        env.pop("FORCE_RELEASES_URL", None)
        env.pop("FORCE_DOWNLOAD_BASE", None)
        res = subprocess.run(["sh", str(filled), "--no-services", "--no-fonts"],
                           capture_output=True, text=True, env=env, timeout=180)
        assert res.returncode != 0
        assert "checksum mismatch" in (res.stderr + res.stdout)
        assert not (home / ".local" / "bin" / "force").exists()
        assert not (data / "app").exists()
    finally:
        httpd.shutdown()


def test_fill_writes_releases_url_leaves_no_placeholders(tmp_path):
    version = "9.9.9"
    filled = tmp_path / "install.sh"
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "fill-installer.py"),
                      str(REPO / "share" / "get-force.sh"), str(filled),
                      version, "http://127.0.0.1:0", "http://releases.example/x", "0" * 64, ""])
    assert r.returncode == 0
    text = filled.read_text()
    assert "http://releases.example/x" in text
    for ph in ("@VERSION@", "@BASE_URL@", "@RELEASES_URL@", "@SHA_x86_64@", "@SHA_aarch64@"):
        assert ph not in text


def test_fill_requires_seven_args(tmp_path):
    filled = tmp_path / "install.sh"
    # 6 args (template, out, version, base_url, releases_url, sha_x86_64) missing the last sha
    args = [str(REPO / "scripts" / "fill-installer.py"),
            str(REPO / "share" / "get-force.sh"), str(filled),
            "9.9.9", "http://127.0.0.1:0", "http://127.0.0.1:0/releases", "0" * 64]
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True)
    assert r.returncode == 2
    assert "usage:" in r.stderr
    assert not filled.exists()


def test_bootstrap_falls_back_to_download_base_releases(tmp_path):
    """FORCE_DOWNLOAD_BASE (no FORCE_RELEASES_URL) fetches the tarball from <base>/releases/ even when the
    baked RELEASES_URL is unreachable."""
    arch = _arch()
    version = "9.9.9"
    rel = make_release(tmp_path, version)
    root = tmp_path / "web"
    root.mkdir(exist_ok=True)
    tar = _make_tarball(rel, root, version, arch)
    sha = subprocess.run(["sha256sum", str(tar)], capture_output=True, text=True).stdout.split()[0]

    filled = tmp_path / "install.sh"
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "fill-installer.py"),
                      str(REPO / "share" / "get-force.sh"), str(filled),
                      version, "http://127.0.0.1:0", "http://127.0.0.1:1/x", sha, ""])
    assert r.returncode == 0

    httpd, port = _serve(root)
    try:
        home = tmp_path / "home"
        data = tmp_path / "fdata"
        for d in (home, data):
            d.mkdir(parents=True, exist_ok=True)
        env = _bootstrap_env(tmp_path, home, data,
                             {"FORCE_DOWNLOAD_BASE": f"http://127.0.0.1:{port}"})
        res = _run_bootstrap(filled, env)
        assert res.returncode == 0, res.stderr + res.stdout
        assert (home / ".local" / "bin" / "force").is_file()
        assert (data / "app" / "VERSION").read_text().strip() == version
    finally:
        httpd.shutdown()


def test_bootstrap_fetches_from_env_releases_url(tmp_path):
    """FORCE_RELEASES_URL pointing at a different server dir fetch the tarball from there."""
    arch = _arch()
    version = "9.9.9"
    rel = make_release(tmp_path, version)
    root = tmp_path / "webrel"
    root.mkdir(exist_ok=True)
    tar = _make_tarball(rel, root, version, arch)
    sha = subprocess.run(["sha256sum", str(tar)], capture_output=True, text=True).stdout.split()[0]

    filled = tmp_path / "install.sh"
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "fill-installer.py"),
                      str(REPO / "share" / "get-force.sh"), str(filled),
                      version, "http://127.0.0.1:0", "http://127.0.0.1:1/x", sha, ""])
    assert r.returncode == 0

    httpd, port = _serve(root)
    try:
        home = tmp_path / "home"
        data = tmp_path / "fdata"
        for d in (home, data):
            d.mkdir(parents=True, exist_ok=True)
        env = _bootstrap_env(tmp_path, home, data,
                             {"FORCE_RELEASES_URL": f"http://127.0.0.1:{port}/releases"})
        res = _run_bootstrap(filled, env)
        assert res.returncode == 0, res.stderr + res.stdout
        assert (home / ".local" / "bin" / "force").is_file()
        assert (data / "app" / "VERSION").read_text().strip() == version
    finally:
        httpd.shutdown()


def test_real_runner_reports_a_missing_program_as_127():
    """In a container there is no systemctl: the runner must say so, not raise (setup crashed there)."""
    from force.setup import _make_run
    r = _make_run()(["force-no-such-program-xyz", "--user"])
    assert r.returncode == 127


def test_a_development_release_without_version_is_accepted(tmp_path):
    """deploy.sh releases (pushed by force install-to) have no VERSION file: setup names them after their dir."""
    from force.setup import check_release
    rel = tmp_path / "20261103-120000-abc1234"
    for f in ("force/cli.py", "share/tmux.conf", "share/bin/force-sidebar"):
        (rel / f).parent.mkdir(parents=True, exist_ok=True)
        (rel / f).write_text("")
    assert check_release(rel) == "dev 20261103-120000-abc1234"
