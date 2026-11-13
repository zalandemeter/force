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
    assert "is the newest release" in out
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
    assert "The update failed (exit 3)" in capsys.readouterr().out


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


def test_ui_remote_delegates(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_HOST", "h")
    calls = []

    def fake_call(argv):
        calls.append(argv)
        return 7

    monkeypatch.setattr(updates.subprocess, "call", fake_call)

    from force import remote
    monkeypatch.setattr(remote, "get", lambda paths, name: remote.Remote("h", "h"))
    assert updates.ui_command() == 7
    assert calls and calls[0][-1].endswith("update --ui") and "-t" in calls[0]


def test_ui_remote_old_force_updates_without_ui(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_HOST", "h")
    calls = []

    def fake_call(argv):
        calls.append(argv)
        return 2 if len(calls) == 1 else 0

    monkeypatch.setattr(updates.subprocess, "call", fake_call)

    from force import remote
    monkeypatch.setattr(remote, "get", lambda paths, name: remote.Remote("h", "h"))
    from force import support
    monkeypatch.setattr(support, "_wait_key", lambda: "\r")
    assert updates.ui_command() == 0
    assert len(calls) == 2
    assert calls[0][-1].endswith("update --ui") and "-t" in calls[0]
    assert calls[1][-1].endswith("update") and not calls[1][-1].endswith("--ui")
    assert "h is updated" in capsys.readouterr().out


def test_ui_remote_unreachable_says_so_before_closing(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_HOST", "h")
    calls = []
    monkeypatch.setattr(updates.subprocess, "call", lambda argv: calls.append(argv) or 255)
    from force import remote, support
    monkeypatch.setattr(remote, "get", lambda paths, name: remote.Remote("h", "h"))
    keys = []
    monkeypatch.setattr(support, "_wait_key", lambda: keys.append(1) or "x")
    assert updates.ui_command() == 255
    assert len(calls) == 1 and keys == [1]
    assert "cannot reach h over ssh" in capsys.readouterr().out


def test_ui_remote_unknown_host_returns_1(monkeypatch, capsys):
    monkeypatch.setenv("FORCE_HOST", "nope")

    from force import remote, support
    monkeypatch.setattr(remote, "get", lambda paths, name: None)
    monkeypatch.setattr(support, "_wait_key", lambda: "\r")
    assert updates.ui_command() == 1
    out = capsys.readouterr().out
    assert "no host" in out and "nope" in out

# The handler reads a module-global body so each test can set what install.sh returns.
_server_body = b""
_server_agents: list[str] = []          # the User-Agent of every request, newest last


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        _server_agents.append(self.headers.get("User-Agent", ""))
        self.send_response(200)
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
