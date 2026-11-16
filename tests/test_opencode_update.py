"""Tests for force/opencode_update.py and the `opencode-update` CLI command (SPEC §36).

These tests never touch a real tmux socket named `force`, never bind/contact port 8000, and never hit the
network (urllib.request.urlopen is monkeypatched in force.opencode_update everywhere).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile

import pytest

from force import cli, opencode, opencode_update
from force.opencode_update import UpdateError


# --- fakes fixture (copied from tests/test_cli.py / tests/test_opencode.py) ----
class FakeTmux:
    def __init__(self):
        self.calls = []
        self.server = True
        self.sessions_out = []
        self.clients_out = []
        self.run_out = ""
        self.agent_panes = {}

    def invalidate(self):
        pass

    def server_running(self):
        return self.server

    def has_session(self, name):
        return name in self.sessions_out

    def clients(self, control=False):
        return self.clients_out

    def panes(self, strict=False):
        return []

    def get_option(self, key, default=None):
        self.calls.append(("get_option", key))
        return "off"

    def new_session(self, name, width, height, cwd, command, env=None):
        self.calls.append(("new_session", name))
        return "p0"

    def split_sidebar(self, target_pane, cwd, command, width):
        return "p1"

    def set_pane_option(self, pane, key, value):
        self.calls.append(("set_pane_option", pane, key, value))

    def run(self, *args, check=True):
        self.calls.append(("run", args))
        return self.run_out

    def switch_client(self, target, client=None):
        self.calls.append(("switch_client", target, client))

    def select_pane(self, pane):
        self.calls.append(("select_pane", pane))


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    env = {
        "HOME": str(tmp_path),
        "FORCE_HOME": str(tmp_path / "home"),
        "FORCE_STATE": str(tmp_path / "state"),
        "FORCE_CLAUDE_DIR": str(tmp_path / "claude"),
        "FORCE_CLAUDE_BIN": str(tmp_path / "bin" / "claude"),
        "FORCE_CONFIG": str(tmp_path / "settings.json"),
        "FORCE_TMUX_SOCKET": "forcetestsock",
        "FORCE_UI_SOCKET": "forcetestsock-ui",
        "FORCE_RUN_STAGE": "2",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "settings.json").write_text(json.dumps({"layout": "classic", "sharing": True}))  # opencodeManaged stays off
    fake = FakeTmux()
    cockpit = cli.make_cockpit()
    cockpit.tmux = fake
    monkeypatch.setattr(cli, "make_cockpit", lambda: cockpit)
    return cockpit, fake, tmp_path


@pytest.fixture
def managed_fakes(monkeypatch, tmp_path):
    """Same as fakes but with opencodeManaged = true written before the cockpit is built.
    Resets the global opencode managed root afterwards so other tests are unaffected."""
    env = {
        "HOME": str(tmp_path),
        "FORCE_HOME": str(tmp_path / "home"),
        "FORCE_STATE": str(tmp_path / "state"),
        "FORCE_CLAUDE_DIR": str(tmp_path / "claude"),
        "FORCE_CLAUDE_BIN": str(tmp_path / "bin" / "claude"),
        "FORCE_CONFIG": str(tmp_path / "settings.json"),
        "FORCE_TMUX_SOCKET": "forcetestsock",
        "FORCE_UI_SOCKET": "forcetestsock-ui",
        "FORCE_RUN_STAGE": "2",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "settings.json").write_text(json.dumps({"layout": "classic", "opencodeManaged": True}))
    fake = FakeTmux()
    cockpit = cli.make_cockpit()                 # sets opencode.set_managed(<tmp>/home/opencode) globally
    cockpit.tmux = fake
    monkeypatch.setattr(cli, "make_cockpit", lambda: cockpit)
    try:
        yield cockpit, fake, tmp_path
    finally:
        opencode.set_managed(None)


# --- 1. asset_name -----------------------------------------------------------
def test_asset_name(monkeypatch):
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "x86_64")
    assert opencode_update.asset_name() == "opencode-linux-x64.tar.gz"
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "aarch64")
    assert opencode_update.asset_name() == "opencode-linux-arm64.tar.gz"
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "riscv64")
    with pytest.raises(UpdateError):
        opencode_update.asset_name()


# --- release() ---------------------------------------------------------------
HEX = "a" * 64
DL = "https://github.com/anomalyco/opencode/releases/download/v1.19.3/opencode-linux-x64.tar.gz"
REL_JSON = {
    "tag_name": "v1.19.3",
    "assets": [
        {"name": "opencode-linux-x64.tar.gz", "digest": f"sha256:{HEX}",
         "browser_download_url": DL},
    ],
}


class _Resp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _patch_urlopen(monkeypatch, body, exc=None):
    """Monkeypatch force.opencode_update.urllib.request.urlopen. Returns a list recording each
    Request.full_url passed (and may raise `exc`)."""
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        if exc:
            raise exc
        return _Resp(body)

    monkeypatch.setattr(opencode_update.urllib.request, "urlopen", fake)
    return calls


def test_release_success(monkeypatch):
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "x86_64")
    _patch_urlopen(monkeypatch, json.dumps(REL_JSON).encode())
    rel = opencode_update.release()
    assert rel == {"version": "1.19.3", "url": DL, "sha256": HEX}


def test_release_request_urls(monkeypatch):
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "x86_64")
    calls = _patch_urlopen(monkeypatch, json.dumps(REL_JSON).encode())
    opencode_update.release()
    opencode_update.release("1.18.33")
    opencode_update.release("v1.18.33")
    assert calls[0].endswith("/latest")
    assert calls[1].endswith("/tags/v1.18.33")
    assert calls[2].endswith("/tags/v1.18.33")


def test_release_asset_missing(monkeypatch):
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "x86_64")
    rel = dict(REL_JSON, assets=[{"name": "opencode-linux-arm64.tar.gz", "digest": f"sha256:{HEX}",
                                 "browser_download_url": DL}])
    _patch_urlopen(monkeypatch, json.dumps(rel).encode())
    with pytest.raises(UpdateError):
        opencode_update.release()


def test_release_digest_missing_or_wrong(monkeypatch):
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "x86_64")
    for digest in ("", "md5:abc"):
        rel = dict(REL_JSON, assets=[{"name": "opencode-linux-x64.tar.gz", "digest": digest,
                                     "browser_download_url": DL}])
        _patch_urlopen(monkeypatch, json.dumps(rel).encode())
        with pytest.raises(UpdateError):
            opencode_update.release()


def test_release_bad_download_url(monkeypatch):
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "x86_64")
    rel = dict(REL_JSON, assets=[{"name": "opencode-linux-x64.tar.gz", "digest": f"sha256:{HEX}",
                                 "browser_download_url": "http://evil.test/x"}])
    _patch_urlopen(monkeypatch, json.dumps(rel).encode())
    with pytest.raises(UpdateError):
        opencode_update.release()


def test_release_urlopen_oserror(monkeypatch):
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "x86_64")
    _patch_urlopen(monkeypatch, b"{}", exc=OSError("no network"))
    with pytest.raises(UpdateError):
        opencode_update.release()


def test_release_invalid_json(monkeypatch):
    monkeypatch.setattr(opencode_update.platform, "machine", lambda: "x86_64")
    _patch_urlopen(monkeypatch, b"this is not json")
    with pytest.raises(UpdateError):
        opencode_update.release()


# --- _download() --------------------------------------------------------------
class _Chunked:
    def __init__(self, data):
        self._data = data
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


def test_download_ok(monkeypatch, tmp_path):
    body = b"abc" * 1000
    monkeypatch.setattr(opencode_update.urllib.request, "urlopen", lambda req, timeout=None: _Chunked(body))
    dest = tmp_path / "f.bin"
    opencode_update._download("https://x/y", hashlib.sha256(body).hexdigest(), dest)
    assert dest.read_bytes() == body


def test_download_checksum_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_update.urllib.request, "urlopen",
                      lambda req, timeout=None: _Chunked(b"abc" * 1000))
    dest = tmp_path / "f.bin"
    with pytest.raises(UpdateError, match="checksum mismatch"):
        opencode_update._download("https://x/y", "0" * 64, dest)


def test_download_too_large(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_update.urllib.request, "urlopen",
                      lambda req, timeout=None: _Chunked(b"abc" * 1000))
    monkeypatch.setattr(opencode_update, "MAX_BYTES", 10)
    dest = tmp_path / "f.bin"
    with pytest.raises(UpdateError):
        opencode_update._download("https://x/y", hashlib.sha256(b"abc" * 1000).hexdigest(), dest)


# --- _extract_binary() --------------------------------------------------------
def _make_tar(path, entries=(), symlinks=()):
    """entries: list of (name, bytes); symlinks: list of (name, target)."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for name, target in symlinks:
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = target
            tf.addfile(ti)
    return path


def test_extract_simple(tmp_path):
    script = b"#!/bin/sh\necho hi\n"
    tar = _make_tar(tmp_path / "a.tar.gz", [("opencode", script), ("other", b"x"), ("bin/helper", b"y")])
    dest = tmp_path / "opencode"
    opencode_update._extract_binary(tar, dest)
    assert dest.read_bytes() == script
    assert os.access(dest, os.X_OK)


def test_extract_subdir_basename(tmp_path):
    tar = _make_tar(tmp_path / "b.tar.gz", [("dist/bin/opencode", b"#!/bin/sh\n")])
    dest = tmp_path / "opencode"
    opencode_update._extract_binary(tar, dest)
    assert dest.read_bytes() == b"#!/bin/sh\n"


def test_extract_ignores_evil_paths(tmp_path):
    script = b"scripts"
    tar = _make_tar(tmp_path / "c.tar.gz",
                    [("opencode", script), ("../../evil", b"x"), ("/abs/evil", b"y")])
    dest = tmp_path / "opencode"
    opencode_update._extract_binary(tar, dest)
    assert dest.read_bytes() == script
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path.parent / "evil").exists()


def test_extract_no_opencode(tmp_path):
    tar = _make_tar(tmp_path / "d.tar.gz", [("opencode-linux", b"x")])
    with pytest.raises(UpdateError):
        opencode_update._extract_binary(tar, tmp_path / "opencode")


def test_extract_symlink_only(tmp_path):
    tar = _make_tar(tmp_path / "e.tar.gz", symlinks=[("opencode", "somewhere")])
    with pytest.raises(UpdateError):
        opencode_update._extract_binary(tar, tmp_path / "opencode")


def test_extract_corrupt_non_gzip(tmp_path):
    corrupt = tmp_path / "bad.tar.gz"
    corrupt.write_bytes(b"this is definitely not a gzip file" * 10)
    with pytest.raises(UpdateError):
        opencode_update._extract_binary(corrupt, tmp_path / "opencode")


# --- install() ---------------------------------------------------------------
def _script_tar(tmp_path, name, version):
    return _make_tar(tmp_path / name, [("opencode", f"#!/bin/sh\necho {version}\n".encode())])


def _fake_download(tarball, downloads=None):
    def fake(url, sha256, dest, timeout=120):
        if downloads is not None:
            downloads.append(url)
        shutil.copyfile(tarball, dest)
    return fake


def test_install_first_and_idempotent(monkeypatch, tmp_path):
    tarball = _script_tar(tmp_path, "t.tar.gz", "1.19.3")
    monkeypatch.setattr(opencode_update, "release",
                      lambda v=None: {"version": "1.19.3", "url": "https://github.com/x", "sha256": "h"})
    downloads = []
    monkeypatch.setattr(opencode_update, "_download", _fake_download(tarball, downloads))

    root = tmp_path / "oc"
    ver, changed = opencode_update.install(root)
    assert (ver, changed) == ("1.19.3", True)
    assert (root / "1.19.3" / "opencode").is_file()
    assert os.access(root / "1.19.3" / "opencode", os.X_OK)
    assert os.readlink(root / "current") == "1.19.3"
    assert len(downloads) == 1

    # second install: already current, download not run again
    ver2, changed2 = opencode_update.install(root)
    assert (ver2, changed2) == ("1.19.3", False)
    assert len(downloads) == 1


def test_install_binary_version_mismatch(monkeypatch, tmp_path):
    tarball = _script_tar(tmp_path, "m.tar.gz", "1.0.0")
    monkeypatch.setattr(opencode_update, "release",
                      lambda v=None: {"version": "1.19.3", "url": "https://github.com/x", "sha256": "h"})
    monkeypatch.setattr(opencode_update, "_download", _fake_download(tarball))
    root = tmp_path / "oc"
    with pytest.raises(UpdateError):
        opencode_update.install(root)
    assert not (root / "current").exists()
    assert not (root / "1.19.3").exists()


def test_install_explicit_downgrade(monkeypatch, tmp_path):
    tarball = _script_tar(tmp_path, "d.tar.gz", "1.18.33")
    monkeypatch.setattr(opencode_update, "release",
                      lambda v=None: {"version": "1.18.33", "url": "https://github.com/x", "sha256": "h"})
    monkeypatch.setattr(opencode_update, "_download", _fake_download(tarball))
    root = tmp_path / "oc"
    ver, changed = opencode_update.install(root, "1.18.33")
    assert (ver, changed) == ("1.18.33", True)
    assert os.readlink(root / "current") == "1.18.33"


# --- prune() ----------------------------------------------------------------
def _make_version_dirs(root, versions):
    for v in versions:
        d = root / v
        d.mkdir(parents=True)
        (d / "opencode").write_text("x")


def test_prune_removes_oldest_not_current(monkeypatch, tmp_path):
    root = tmp_path / "p"
    _make_version_dirs(root, ["1.18.32", "1.18.33", "1.19.0", "1.19.3"])
    os.symlink("1.18.32", root / "current")
    (root / ".dl-x").mkdir()
    (root / "notes").mkdir()
    monkeypatch.setattr(opencode_update, "_in_use", lambda d: False)
    removed = opencode_update.prune(root, keep=2)
    assert removed == ["1.18.33"]
    assert (root / "1.18.32").is_dir()
    assert (root / "1.19.0").is_dir()
    assert (root / "1.19.3").is_dir()
    assert not (root / "1.18.33").exists()
    assert (root / ".dl-x").is_dir() and (root / "notes").is_dir()


def test_prune_keeps_in_use(monkeypatch, tmp_path):
    root = tmp_path / "p"
    _make_version_dirs(root, ["1.18.32", "1.18.33", "1.19.0", "1.19.3"])
    os.symlink("1.18.32", root / "current")
    monkeypatch.setattr(opencode_update, "_in_use", lambda d: d.name == "1.18.33")
    removed = opencode_update.prune(root, keep=2)
    assert removed == []
    assert (root / "1.18.33").is_dir()


def test_prune_keeps_non_version_dirs(monkeypatch, tmp_path):
    root = tmp_path / "p"
    _make_version_dirs(root, ["1.19.3"])
    (root / ".dl-x").mkdir()
    (root / "notes").mkdir()
    monkeypatch.setattr(opencode_update, "_in_use", lambda d: False)
    opencode_update.prune(root, keep=2)
    assert (root / ".dl-x").is_dir() and (root / "notes").is_dir()


# --- newer_available() -------------------------------------------------------
def _current(root, version):
    root.mkdir(parents=True, exist_ok=True)
    cur = root / "current"
    try:
        cur.unlink()
    except FileNotFoundError:
        pass
    os.symlink(version, cur)


def test_newer_available(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_update, "release",
                      lambda v=None: {"version": "1.19.3", "url": "https://github.com/x", "sha256": "h"})
    root = tmp_path / "n"
    assert opencode_update.newer_available(root) == "1.19.3"     # nothing installed
    _current(root, "1.18.33")
    assert opencode_update.newer_available(root) == "1.19.3"
    _current(root, "1.19.3")
    assert opencode_update.newer_available(root) is None
    _current(root, "1.20.0")
    assert opencode_update.newer_available(root) is None


def test_newer_available_numeric_compare(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_update, "release",
                      lambda v=None: {"version": "1.10.0", "url": "https://github.com/x", "sha256": "h"})
    root = tmp_path / "n"
    _current(root, "1.9.9")
    assert opencode_update.newer_available(root) == "1.10.0"


# --- CLI: opencode-update ----------------------------------------------------
def test_opencode_update_not_managed_refused(fakes, capsys):
    rc = cli.main(["opencode-update"])
    assert rc == 1
    assert "opencodeManaged" in capsys.readouterr().err


def test_opencode_update_installs(managed_fakes, monkeypatch, capsys):
    out = []
    monkeypatch.setattr(opencode_update, "install",
                      lambda root, version=None: out.append((root, version)) or ("1.19.3", True))
    rc = cli.main(["opencode-update"])
    assert rc == 0
    assert "opencode 1.19.3 installed (current)" in capsys.readouterr().out
    root, version = out[0]
    assert version is None


def test_opencode_update_error(managed_fakes, monkeypatch, capsys):
    def boom(root, version=None):
        raise UpdateError("boom")
    monkeypatch.setattr(opencode_update, "install", boom)
    rc = cli.main(["opencode-update"])
    assert rc == 1
    assert "boom" in capsys.readouterr().err


def test_opencode_update_check_available(managed_fakes, monkeypatch, capsys):
    monkeypatch.setattr(opencode_update, "newer_available", lambda root: "1.19.3")
    rc = cli.main(["opencode-update", "--check"])
    assert rc == 0
    assert "available" in capsys.readouterr().out


def test_opencode_update_check_up_to_date(managed_fakes, monkeypatch, capsys):
    monkeypatch.setattr(opencode_update, "newer_available", lambda root: None)
    rc = cli.main(["opencode-update", "--check"])
    assert rc == 0
    assert "up to date" in capsys.readouterr().out


def test_opencode_update_parser_version_flag(managed_fakes):
    a = cli.build_parser().parse_args(["opencode-update", "--version", "1.18.33"])
    assert (a.command, a.version, a.check) == ("opencode-update", "1.18.33", False)
