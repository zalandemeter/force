import http.server
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from force import updates


class _FakePopen:
    """A subprocess.Popen stand-in whose stdout yields lines and whose returncode is preset."""

    def __init__(self, argv, lines, code=0):
        self.argv = argv
        self.stdout = iter(lines)
        self.returncode = None       # like the real one: set only by wait()
        self._code = code

    def wait(self):
        self.returncode = self._code
        return self.returncode


@pytest.fixture(autouse=True)
def _no_real_hosts(monkeypatch):
    """`force update` now updates every remote in hosts.json (SPEC §46): no test may reach this machine's real ones.
    Tests that want remotes set `updates._remotes` themselves."""
    monkeypatch.setattr(updates, "_remotes", lambda paths, host: [])


def _patch_ui(monkeypatch, cur="1.0.0", latest=b'VERSION="9.9.9"\n', base="https://useforce.cc", key="\r",
              err=None, code=0):
    monkeypatch.setattr(updates, "download_base", lambda: base)
    monkeypatch.setattr(updates, "current_version", lambda: cur)
    monkeypatch.setattr(updates, "fetch_install", lambda base, timeout: (None, err) if err else (latest, None))
    from force import support
    monkeypatch.setattr(support, "_wait_key", lambda: key)
    procs = []

    def fake_popen(argv, **kw):
        p = _FakePopen(argv, ["force: Installing fonts\n", "\x1b[1mforce step one\x1b[0m\n", "\n"], code)
        procs.append(p)
        return p

    monkeypatch.setattr(updates.subprocess, "Popen", fake_popen)
    return procs


def test_ui_up_to_date(monkeypatch, capsys):
    procs = _patch_ui(monkeypatch, latest=b'VERSION="1.0.0"\n')
    assert updates.ui_command() == 0
    out = capsys.readouterr().out
    assert "the newest release" in out
    assert procs == [], "no install for an up-to-date build"


def test_ui_newer_esc_no_update(monkeypatch, capsys):
    procs = _patch_ui(monkeypatch, key="\x1b")
    assert updates.ui_command() == 0
    out = capsys.readouterr().out
    assert "Force 9.9.9 is available" in out
    assert procs == [], "Esc must not update"


def test_ui_newer_enter_installs_and_removes_tmp(monkeypatch, capsys):
    procs = _patch_ui(monkeypatch, key="\r")
    assert updates.ui_command() == 0
    out = capsys.readouterr().out
    assert "Force 9.9.9 is available" in out
    assert "installed" in out
    assert len(procs) == 1
    argv = procs[0].argv
    assert argv[:3] == ["sh", argv[1], "--no-fonts"] and argv[0] == "sh" and argv[2] == "--no-fonts"
    assert argv[1].startswith("/") and Path(argv[1]).suffix == ".sh"
    assert not Path(argv[1]).exists(), "the tmp install.sh is removed"
    # install output lines are shown without the "force: " prefix and ANSI codes
    assert "Installing fonts" in out and "force: Installing" not in out and "\x1b[1m" not in out


def test_ui_newer_enter_failure_returns_code(monkeypatch, capsys):
    _patch_ui(monkeypatch, key="\n", code=3)
    assert updates.ui_command() == 3
    assert "failed (exit 3)" in capsys.readouterr().out


def test_ui_fetch_error_shows_url_and_returns_1(monkeypatch, capsys):
    _patch_ui(monkeypatch, err="connection refused")
    assert updates.ui_command() == 1
    out = capsys.readouterr().out
    assert "✗" in out and "https://useforce.cc/install.sh" in out and "connection refused" in out


def test_ui_dev_build_returns_0(monkeypatch, capsys):
    _patch_ui(monkeypatch, cur=None)
    assert updates.ui_command() == 0
    out = capsys.readouterr().out
    assert "a development build" in out
    assert "Checking for a newer release" in out


def test_ui_whats_new_link_only_from_useforce(monkeypatch, capsys):
    _patch_ui(monkeypatch, base="https://useforce.cc", key="\x1b")
    updates.ui_command()
    assert "releases/tag/v9.9.9" in capsys.readouterr().out
    _patch_ui(monkeypatch, base="https://example.com", key="\x1b")
    updates.ui_command()
    assert "releases/tag/v9.9.9" not in capsys.readouterr().out


def test_ui_no_terminal_does_not_update(monkeypatch, capsys):
    procs = _patch_ui(monkeypatch, key="")
    assert updates.ui_command() == 0
    assert procs == [], "no terminal ('' key) must not update"


# The handler reads a module-global body so each test can set what install.sh returns.
_server_body = b""
_server_agents: list[str] = []          # the User-Agent of every request, newest last


_server_conditional: list[str | None] = []   # the If-None-Match of every request, newest last


def _etag() -> str:
    import hashlib
    return 'W/"' + hashlib.sha256(_server_body).hexdigest()[:12] + '"'


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        _server_agents.append(self.headers.get("User-Agent", ""))
        _server_conditional.append(self.headers.get("If-None-Match"))
        if self.headers.get("If-None-Match") == _etag():
            self.send_response(304)          # as GitHub Pages: unchanged, no body
            self.send_header("ETag", _etag())
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("ETag", _etag())
        self.send_header("Last-Modified", "Fri, 13 Nov 2026 11:29:04 GMT")
        self.send_header("Content-Length", str(len(_server_body)))
        self.end_headers()
        self.wfile.write(_server_body)

    def log_message(self, *a):
        pass


@pytest.fixture
def http_server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def set_body(body: bytes) -> None:
    global _server_body
    _server_body = body


@pytest.fixture
def fake_app(tmp_path, monkeypatch):
    """A fake release: app_dir() points there; share/ and VERSION under it."""
    share = tmp_path / "share"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(updates, "app_dir", lambda: tmp_path)
    return tmp_path


# --- download base -----------------------------------------------------------
def test_download_base_from_env(fake_app, monkeypatch):
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", "https://example.com/xyz")
    assert updates.download_base() == "https://example.com/xyz"


def test_download_base_from_file(fake_app, monkeypatch):
    monkeypatch.delenv("FORCE_DOWNLOAD_BASE", raising=False)
    (fake_app / "share" / "download-base").write_text("https://files.example.com/force\n")
    assert updates.download_base() == "https://files.example.com/force"


def test_download_base_missing_or_placeholder(fake_app, monkeypatch):
    monkeypatch.delenv("FORCE_DOWNLOAD_BASE", raising=False)
    assert updates.download_base() is None
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", "https://example.invalid")
    assert updates.download_base() is None
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", "  ")
    assert updates.download_base() is None


# --- force update ------------------------------------------------------------
def test_no_base_exit1(fake_app, monkeypatch, capsys):
    (fake_app / "VERSION").write_text("1.0.0")
    monkeypatch.delenv("FORCE_DOWNLOAD_BASE", raising=False)
    assert updates.update_command() == 1
    err = capsys.readouterr().err
    assert "no download address" in err


def test_dev_build_exit0(fake_app, monkeypatch, capsys, http_server):
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", http_server)   # a base, but no VERSION
    assert updates.update_command() == 0
    assert "development build (deploy.sh): not updated" in capsys.readouterr().out


def test_up_to_date(fake_app, monkeypatch, capsys, http_server):
    (fake_app / "VERSION").write_text("1.0.0")
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", http_server)
    set_body(b'#!/bin/sh\nVERSION="1.0.0"\n')
    assert updates.update_command() == 0
    assert "Force 1.0.0 is up to date" in capsys.readouterr().out


def test_check_newer(fake_app, monkeypatch, capsys, http_server):
    (fake_app / "VERSION").write_text("1.0.0")
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", http_server)
    set_body(b'#!/bin/sh\nVERSION="1.2.3"\n')
    assert updates.update_command(check=True) == 0
    assert capsys.readouterr().out == "force: update available: 1.0.0 -> 1.2.3 (run: force update)\n"


def test_garbage_install_exit1(fake_app, monkeypatch, capsys, http_server):
    (fake_app / "VERSION").write_text("1.0.0")
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", http_server)
    set_body(b'#!/bin/sh\nBOGUS="nope"\n')
    assert updates.update_command() == 1
    assert "no VERSION line" in capsys.readouterr().err


def test_update_runs_sh_with_downloaded_file(fake_app, monkeypatch, capsys, http_server):
    (fake_app / "VERSION").write_text("1.0.0")
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", http_server)
    set_body(b'#!/bin/sh\nVERSION="1.2.3"\n')
    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        return SimpleNamespace(returncode=5)

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    assert updates.update_command() == 5
    out, _ = capsys.readouterr()
    assert "updating 1.0.0 -> 1.2.3 (sessions keep running)" in out
    argv = calls["argv"]
    assert argv[0] == "sh"
    assert argv[2] == "--no-fonts"
    tmp = Path(argv[1])
    assert not tmp.exists(), "the downloaded file is deleted afterwards"


# --- the daemon's background check -------------------------------------------
def test_check_daemon_writes_update_json(fake_app, monkeypatch, tmp_path, http_server):
    state = tmp_path / "state"
    (fake_app / "VERSION").write_text("1.0.0")
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", http_server)
    set_body(b'#!/bin/sh\nVERSION="1.5.0"\n')
    assert updates.check_daemon(state) == "1.5.0"
    data = json.loads((state / "update.json").read_text())
    assert data["latest"] == "1.5.0"
    assert data["checked_ms"] > 0


def test_check_daemon_is_conditional_after_the_first(fake_app, monkeypatch, tmp_path, http_server):
    """SPEC §3: the hourly check sends the last ETag; an unchanged install.sh is a 304 that keeps the latest and
    refreshes checked_ms; a new release (new ETag) is read again."""
    state = tmp_path / "state"
    (fake_app / "VERSION").write_text("1.0.0")
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", http_server)
    set_body(b'#!/bin/sh\nVERSION="1.5.0"\n')
    _server_conditional.clear()
    assert updates.check_daemon(state) == "1.5.0"
    first = json.loads((state / "update.json").read_text())
    assert first["etag"] == _etag() and first["last_modified"]
    assert _server_conditional == [None]                      # the first check is a plain GET
    (state / "update.json").write_text(json.dumps({**first, "checked_ms": 1}))
    assert updates.check_daemon(state) == "1.5.0"             # 304: the known latest
    again = json.loads((state / "update.json").read_text())
    assert _server_conditional[-1] == first["etag"]
    assert again["latest"] == "1.5.0" and again["checked_ms"] > 1 and again["etag"] == first["etag"]
    set_body(b'#!/bin/sh\nVERSION="1.6.0"\n')                # a release: new body, new ETag
    assert updates.check_daemon(state) == "1.6.0"
    assert json.loads((state / "update.json").read_text())["etag"] == _etag()


def test_check_daemon_sends_no_validator_without_a_known_latest(fake_app, monkeypatch, tmp_path, http_server):
    """An update.json from before (no latest, or a bad one) never turns a check into a 304 with nothing known."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "update.json").write_text(json.dumps({"etag": "W/\"x\"", "checked_ms": 1}))
    (fake_app / "VERSION").write_text("1.0.0")
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", http_server)
    set_body(b'#!/bin/sh\nVERSION="1.5.0"\n')
    _server_conditional.clear()
    assert updates.check_daemon(state) == "1.5.0"
    assert _server_conditional == [None]


def test_check_daemon_failure_keeps_old_file(fake_app, monkeypatch, tmp_path, http_server):
    state = tmp_path / "state"
    state.mkdir()
    (state / "update.json").write_text(json.dumps({"latest": "9.9.9", "checked_ms": 1}))
    (fake_app / "VERSION").write_text("1.0.0")
    monkeypatch.setenv("FORCE_DOWNLOAD_BASE", http_server)
    set_body(b'BOGUS')
    assert updates.check_daemon(state) is None
    assert json.loads((state / "update.json").read_text())["latest"] == "9.9.9"
    # no base -> nothing
    monkeypatch.delenv("FORCE_DOWNLOAD_BASE", raising=False)
    assert updates.check_daemon(state) is None


def test_snapshot_update_only_when_newer(fake_app, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (fake_app / "VERSION").write_text("1.0.0")
    (state / "update.json").write_text(json.dumps({"latest": "0.9.0"}))
    assert updates.snapshot_update(state) is None
    (state / "update.json").write_text(json.dumps({"latest": "1.0.0"}))
    assert updates.snapshot_update(state) is None
    (state / "update.json").write_text(json.dumps({"latest": "1.1.0"}))
    assert updates.snapshot_update(state) == "1.1.0"
    (state / "update.json").unlink()
    assert updates.snapshot_update(state) is None


def test_fetch_names_force_as_the_user_agent(fake_app, http_server):
    # Cloudflare's browser check refuses Python's default "Python-urllib/x.y" (error 1010): Force names itself
    (fake_app / "VERSION").write_text("1.0.2\n")
    set_body(b'VERSION="1.0.2"\n')
    content, err = updates.fetch_install(http_server, 5)
    assert err is None and content
    assert _server_agents[-1] == "force/1.0.2"


# --- every host (SPEC §46) ---------------------------------------------------------------------------------------
class _Run:
    """A fake subprocess.run for `ssh … force update …`: answers per remote name, records the argv."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        cmd = argv[-1]
        for name, (code, out, err) in self.answers.items():
            if name in " ".join(argv[:-1]):
                if code == "older":                 # a Force from before --here
                    if "--here" in cmd:
                        return SimpleNamespace(returncode=2, stdout="",
                                               stderr="force: error: unrecognized arguments: --here")
                    return SimpleNamespace(returncode=0, stdout=out, stderr=err)
                return SimpleNamespace(returncode=code, stdout=out, stderr=err)
        return SimpleNamespace(returncode=255, stdout="", stderr="ssh: no route")


def _remote(name):
    from force import remote
    return remote.Remote(name, name)


@pytest.mark.parametrize("code, text, want", [
    (0, "force: updating 1.4.1 -> 1.4.2 (sessions keep running)\nforce: ready.", "1.4.1 → 1.4.2"),
    (0, "force: Force 1.4.2 is up to date", "already 1.4.2"),
    (0, "force: update available: 1.4.1 -> 1.4.2 (run: force update)", "1.4.1 → 1.4.2 available"),
    (3, "force: updating 1.4.1 -> 1.4.2 (sessions keep running)\nboom", "1.4.1 → 1.4.2 failed (exit 3)"),
    (255, "ssh: connect to host lab port 22: No route to host", "unreachable (ssh failed)"),
    (1, "force: cannot fetch https://useforce.cc/install.sh: timed out", "cannot fetch https://useforce.cc/install.sh: timed out"),
])
def test_remote_summary(code, text, want):
    assert updates._summary(code, text) == want


def test_update_remote_asks_an_older_force_again_without_here(tmp_path):
    from force import config
    run = _Run({"old": ("older", "force: updating 1.0.0 -> 1.4.2 (sessions keep running)", "")})
    ok, summary = updates.update_remote(config.get_paths(), _remote("old"), run=run)
    assert ok and summary == "1.0.0 → 1.4.2"
    assert run.calls[0][-1].endswith("update --here") and run.calls[1][-1].endswith("update")


def test_update_command_does_every_remote_then_this_machine(monkeypatch, capsys):
    run = _Run({"devbox": (0, "force: updating 1.4.1 -> 1.4.2 (sessions keep running)", ""),
                "pi": (0, "force: Force 1.4.2 is up to date", ""),
                "lab": (255, "", "ssh: no route")})
    monkeypatch.setattr(updates, "_remotes", lambda paths, host: [_remote(n) for n in ("devbox", "pi", "lab")
                                                                   if host in (None, n)])
    monkeypatch.setattr(updates.subprocess, "run", run)
    order = []
    monkeypatch.setattr(updates, "_update_here", lambda check: order.append("here") or 0)
    assert updates.update_command() == 1, "one host failed"
    out = capsys.readouterr().out
    assert "✓ devbox: 1.4.1 → 1.4.2" in out and "✓ pi: already 1.4.2" in out and "✗ lab: unreachable" in out
    assert order == ["here"], "this machine last, after every remote, even when one failed"
    assert all("--here" in c[-1] for c in run.calls if "update" in c[-1])


def test_update_command_here_and_host(monkeypatch, capsys):
    run = _Run({"devbox": (0, "force: Force 1.4.2 is up to date", "")})
    monkeypatch.setattr(updates, "_remotes", lambda paths, host: [_remote(n) for n in ("devbox", "pi")
                                                                   if host in (None, n)])
    monkeypatch.setattr(updates.subprocess, "run", run)
    order = []
    monkeypatch.setattr(updates, "_update_here", lambda check: order.append(("here", check)) or 0)
    assert updates.update_command(here=True) == 0
    assert run.calls == [] and order == [("here", False)], "--here: this machine only"
    order.clear()
    assert updates.update_command(host="devbox") == 0
    assert order == [] and len(run.calls) == 1 and "devbox" in " ".join(run.calls[0]), "--host: that remote only"
    assert updates.update_command(host="nope") == 1
    assert "no enabled host" in capsys.readouterr().err
    order.clear()
    assert updates.update_command(check=True) == 1          # pi did not answer
    assert order == [("here", True)] and run.calls[-1][-1].endswith("update --check --here")


def test_snapshot_update_counts_remotes_behind(fake_app, tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    (state / "update.json").write_text(json.dumps({"latest": "1.4.2"}))
    (fake_app / "VERSION").write_text("1.4.2")
    assert updates.snapshot_update(state) is None
    assert updates.snapshot_update(state, ("1.4.2",)) is None
    assert updates.snapshot_update(state, ("1.4.1-feat-x-abc1234",)) == "1.4.2", "a remote on a branch build of 1.4.1"
    assert updates.snapshot_update(state, ("garbage",)) is None


def test_card_lists_every_host_and_updates_the_ones_behind(monkeypatch, capsys):
    procs = _patch_ui(monkeypatch, cur="1.4.1", latest=b'VERSION="1.4.2"\n')
    from force import remote
    monkeypatch.setattr(updates, "_remotes", lambda paths, host: [_remote(n) for n in ("devbox", "pi", "lab")])
    states = {"devbox": {"state": "connected", "version": "1.4.1"}, "pi": {"state": "connected", "version": "1.4.2"},
              "lab": {"state": "down"}}
    monkeypatch.setattr(remote, "read_status", lambda paths, name: states[name])
    run = _Run({"devbox": (0, "force: updating 1.4.1 -> 1.4.2 (sessions keep running)", ""),
                "lab": (255, "", "")})
    monkeypatch.setattr(updates.subprocess, "run", run)
    assert updates.ui_command() == 1, "lab could not be reached"
    out = capsys.readouterr().out
    assert "devbox" in out and "pi" in out and "offline" in out and "Update all" in out
    assert "pi" not in " ".join(" ".join(c) for c in run.calls), "pi already runs 1.4.2: not asked"
    assert "devbox" in " ".join(" ".join(c) for c in run.calls) and "lab" in " ".join(" ".join(c) for c in run.calls)
    assert len(procs) == 1, "this machine installed last"
    assert "1.4.1 → 1.4.2" in out and "Not every host was updated" in out


def test_card_says_every_host_is_current(monkeypatch, capsys):
    _patch_ui(monkeypatch, cur="1.4.2", latest=b'VERSION="1.4.2"\n')
    from force import remote
    monkeypatch.setattr(updates, "_remotes", lambda paths, host: [_remote("pi")])
    monkeypatch.setattr(remote, "read_status", lambda paths, name: {"state": "connected", "version": "1.4.2"})
    assert updates.ui_command() == 0
    assert "Every host runs Force 1.4.2" in capsys.readouterr().out
