"""Force-managed OpenCode (config `opencode_managed`, SPEC §36): Force keeps its own copy of OpenCode and
updates it to the latest release, the way Claude Code updates itself, instead of using the `opencode` on
PATH (on a shared server often the admin's, which Force never touches).

Layout under <root> (~/.local/share/force/opencode):
  <version>/opencode    one directory per installed release
  current -> <version>  what new sessions start (switched atomically)
Running sessions keep their version until the upgrade step restarts them while idle.

A release comes from GitHub (`opencode-linux-<arch>.tar.gz`), checked against the sha256 digest GitHub
lists for the asset, and only its `opencode` file is extracted; the binary must report the expected
version before it becomes current. The two newest versions are kept (and any one still running).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import procinfo

REPO = "anomalyco/opencode"        # (github.com/sst/opencode redirects here)
API = f"https://api.github.com/repos/{REPO}/releases"
MAX_BYTES = 400 * 1024 * 1024      # a release tarball is ~60 MB
KEEP = 2


class UpdateError(Exception):
    pass


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def asset_name() -> str:
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if arch is None:
        raise UpdateError(f"no OpenCode build for {platform.machine()}")
    return f"opencode-linux-{arch}.tar.gz"


def release(version: str | None = None, timeout: float = 30) -> dict:
    """{"version", "url", "sha256"} of the latest release, or of `version` ("1.18.33" / "v1.18.33")."""
    url = f"{API}/latest" if not version else f"{API}/tags/v{version.lstrip('v')}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "force-opencode-update"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rel = json.loads(r.read().decode())
    except (OSError, ValueError) as e:
        raise UpdateError(f"cannot read the release list: {e}")
    name = asset_name()
    for a in rel.get("assets") or []:
        if a.get("name") == name:
            digest = a.get("digest") or ""
            if not digest.startswith("sha256:"):
                raise UpdateError(f"release {rel.get('tag_name')}: no sha256 digest for {name}")
            dl = a.get("browser_download_url") or ""
            if not dl.startswith("https://github.com/"):
                raise UpdateError(f"unexpected download URL {dl!r}")
            return {"version": str(rel.get("tag_name", "")).lstrip("v"), "url": dl, "sha256": digest[7:]}
    raise UpdateError(f"release {rel.get('tag_name')}: no {name}")


def current_version(root: Path) -> str | None:
    cur = root / "current"
    try:
        return os.path.basename(os.readlink(cur))
    except OSError:
        return None


def _download(url: str, sha256: str, dest: Path, timeout: float = 120) -> None:
    h = hashlib.sha256()
    size = 0
    req = urllib.request.Request(url, headers={"User-Agent": "force-opencode-update"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise UpdateError("download too large")
                h.update(chunk)
                f.write(chunk)
    except OSError as e:
        raise UpdateError(f"download failed: {e}")
    if h.hexdigest() != sha256:
        raise UpdateError("checksum mismatch")


def _extract_binary(tarball: Path, dest: Path) -> None:
    """Only the `opencode` regular file, written to `dest` (no other member, no paths from the archive)."""
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            member = next((m for m in tf.getmembers() if m.isreg() and os.path.basename(m.name) == "opencode"), None)
            if member is None:
                raise UpdateError("no opencode binary in the release")
            src = tf.extractfile(member)
            with open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    except (OSError, tarfile.TarError) as e:
        raise UpdateError(f"cannot unpack the release: {e}")
    os.chmod(dest, 0o755)


def _reported_version(binary: Path) -> str | None:
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\d+\.\d+\.\d+\S*", out or "")
    return m.group(0) if m else None


def _in_use(directory: Path) -> bool:
    """Is any process running a binary from `directory`?"""
    d = str(directory.resolve())
    for pid in procinfo.pids():
        exe = procinfo.exe(pid)
        if exe is not None and exe.startswith(d + os.sep):
            return True
    return False


def prune(root: Path, keep: int = KEEP) -> list[str]:
    """Remove all but the `keep` newest versions (never `current`, never one still running)."""
    cur = current_version(root)
    versions = sorted((p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+\S*", p.name)),
                      key=lambda p: _vtuple(p.name), reverse=True)
    removed = []
    for p in versions[keep:]:
        if p.name != cur and not _in_use(p):
            shutil.rmtree(p, ignore_errors=True)
            removed.append(p.name)
    return removed


def install(root: Path, version: str | None = None) -> tuple[str, bool]:
    """Install the latest release (or `version`) under `root` and make it current.
    Returns (version, changed). Raises UpdateError."""
    root.mkdir(parents=True, exist_ok=True)
    rel = release(version)
    ver = rel["version"]
    target = root / ver
    if not (target / "opencode").exists():
        with tempfile.TemporaryDirectory(dir=root, prefix=".dl-") as tmp:
            tmpd = Path(tmp)
            _download(rel["url"], rel["sha256"], tmpd / "release.tar.gz")
            stage = tmpd / ver
            stage.mkdir()
            _extract_binary(tmpd / "release.tar.gz", stage / "opencode")
            got = _reported_version(stage / "opencode")
            if got != ver:
                raise UpdateError(f"the binary reports {got!r}, expected {ver}")
            os.replace(stage, target)
    changed = current_version(root) != ver
    if changed:
        link = root / ".current.new"
        try:
            link.unlink()
        except FileNotFoundError:
            pass
        os.symlink(ver, link)
        os.replace(link, root / "current")
    prune(root)
    return ver, changed


def newer_available(root: Path) -> str | None:
    """The latest release's version if it is newer than current (or nothing is installed), else None."""
    latest = release()["version"]
    cur = current_version(root)
    return latest if cur is None or _vtuple(latest) > _vtuple(cur) else None
