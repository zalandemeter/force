"""`force update` and the daemon's background check for a newer Force (SPEC §3).

The download base is `FORCE_DOWNLOAD_BASE` or the first line of `<app>/share/download-base`; the running
version is `<app>/VERSION`. A release advertises the latest `VERSION="X.Y.Z"` at the top of its
`<base>/install.sh`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import force

# force update: a generous timeout and the 1 MB cap the brief allows
_MAX_BYTES = 1_000_000
_FETCH_TIMEOUT = 20
# the daemon's silent background check: shorter timeout, small file
_CHECK_TIMEOUT = 10
# the daemon's cadence: first check 2 min after start, then every 24 h
_FIRST_DELAY = 120
_INTERVAL = 24 * 3600

_VERSION_RE = re.compile(r'^\s*VERSION\s*=\s*"([^"]+)"\s*$')
_VER = re.compile(r"\d+\.\d+\.\d+")


def app_dir() -> Path:
    """The directory that contains the running `force` package's parent (the release)."""
    return Path(force.__file__).resolve().parent.parent


def download_base() -> str | None:
    """The download base for this build: env FORCE_DOWNLOAD_BASE, else the first line of
    `<app>/share/download-base`. None when missing, empty or the `https://example.invalid` placeholder."""
    env = os.environ.get("FORCE_DOWNLOAD_BASE", "").strip()
    if env and env != "https://example.invalid":
        return env
    try:
        line = (app_dir() / "share" / "download-base").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return line if line and line != "https://example.invalid" else None


def current_version() -> str | None:
    """`<app>/VERSION` (X.Y.Z), or None when it is missing (a dev/deploy.sh build)."""
    try:
        text = (app_dir() / "VERSION").read_text().strip()
    except OSError:
        return None
    return text if _VER.fullmatch(text) else None


def _tuple(version: str | None) -> tuple | None:
    if not version or not _VER.fullmatch(version):
        return None
    return tuple(int(p) for p in version.split("."))


def fetch_install(base: str, timeout: int) -> tuple[bytes | None, str | None]:
    """Fetch `<base>/install.sh` (<= 1 MB) -> (content, error). `content` None on any failure."""
    url = base.rstrip("/") + "/install.sh"
    try:
        # Force names itself: a site's bot check may refuse Python's default user agent ("Python-urllib")
        req = urllib.request.Request(url, headers={"User-Agent": f"force/{current_version() or 'dev'}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(_MAX_BYTES + 1)
    except Exception as e:
        return None, str(e)
    if len(data) > _MAX_BYTES:
        return None, f"{url}: install.sh is larger than 1 MB"
    return data, None


def parse_install_version(content: bytes) -> str | None:
    """The `VERSION="X.Y.Z"` line near the top of install.sh, or None."""
    head = content[:8192].decode("utf-8", "replace")
    for line in head.splitlines():
        m = _VERSION_RE.match(line.rstrip("\r"))
        if m and _VER.fullmatch(m.group(1)):
            return m.group(1)
    return None


def find_latest(timeout: int) -> tuple[str | None, str | None, bytes | None, str | None]:
    """One check for a newer Force: (current, latest, install_sh content, problem). `problem` is a human sentence
    for no download base, a development build (current None), a fetch error (with the URL) or a missing VERSION line."""
    base = download_base()
    if base is None:
        return None, None, None, ("this Force build has no download address (built without FORCE_DOWNLOAD_BASE); "
                                 "update it the way it was installed")
    cur = current_version()
    if cur is None:
        return None, None, None, "development build (deploy.sh): not updated"
    content, err = fetch_install(base, timeout)
    if content is None:
        return cur, None, None, f"cannot fetch {base.rstrip('/')}/install.sh: {err}"
    latest = parse_install_version(content)
    if latest is None:
        return cur, None, None, "no VERSION line in <base>/install.sh"
    return cur, latest, content, None


def update_command(check: bool = False) -> int:
    """`force update [--check]`. Installs the newer release (runs its install.sh --no-fonts) or reports."""
    cur, latest, content, problem = find_latest(_FETCH_TIMEOUT)
    if problem is not None:
        if problem.startswith("development build"):
            print("force: development build (deploy.sh): not updated")
            return 0
        print(f"force: {problem}", file=sys.stderr)
        return 1
    if _tuple(latest) <= _tuple(cur):
        print(f"force: Force {cur} is up to date")
        return 0
    if check:
        print(f"force: update available: {cur} -> {latest} (run: force update)")
        return 0
    print(f"force: updating {cur} -> {latest} (sessions keep running)")
    tmp = _write_tmp(content)
    try:
        return subprocess.run(["sh", tmp, "--no-fonts"]).returncode
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _ui_local() -> int:
    """`force update --ui` on this machine (SPEC §3): the update card, live in the popup. `_wait_key` from
    force.support: '' (no terminal) means Esc: nothing is updated."""
    from .cards import CARD_W, c, doctor_line, head, keys_right, plain
    from .support import _wait_key

    cur = current_version() or "a development build"
    print("\x1b[?25l", end="", flush=True)                       # no blinking cursor on the card
    out = "\n".join(head("↑", "Update Force", "force update")) + "\n"
    out += "  " + c(f"This machine runs Force {cur}.", "name") + "\n"
    out += "  " + c("Checking for a newer release…", "grey")
    sys.stdout.write(out)                                            # no trailing newline: the row is erased later
    sys.stdout.flush()

    cur, latest, content, problem = find_latest(_FETCH_TIMEOUT)

    def close(rows: list[str]) -> None:
        print("\r\x1b[K", end="", flush=True)                   # erase the "Checking…" line
        for row in rows:
            print(row, flush=True)
        print("", flush=True)
        print(keys_right(("any key", "Close")), flush=True)
        _wait_key()

    if problem is not None:
        close(doctor_line("✗ " + problem))
        return 0 if problem.startswith("development build") else 1
    if _tuple(latest) <= _tuple(cur):
        close(doctor_line(f"✓ Force {cur} is the newest release."))
        return 0

    print("\r\x1b[K", end="", flush=True)                       # erase the "Checking…" line
    print("  " + c(f"Force {latest} is available.", "white", bold=True), flush=True)
    print("", flush=True)
    print("  " + c("Sessions keep running; the cockpit reloads with the new version.", "dim"), flush=True)
    base = download_base()
    if base and urllib.parse.urlparse(base).hostname == "useforce.cc":
        print("  " + c(f"What's new: https://github.com/zalandemeter/force/releases/tag/v{latest}", "dim"),
              flush=True)
    print("", flush=True)
    print(keys_right(("Esc", "Close"), button="Enter  Update"), flush=True)
    if _wait_key() not in ("\r", "\n"):
        return 0                                                 # Esc or no terminal: no update

    print("\x1b[1A\r\x1b[K" + "  " + c("Updating…", "grey"), flush=True)     # in place of the keys row
    tmp = None
    try:
        tmp = _write_tmp(content)
        proc = subprocess.Popen(["sh", tmp, "--no-fonts"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, text=True, errors="replace")
        for line in (proc.stdout or ()):
            text = plain(line).strip()
            if text.startswith("force: "):
                text = text[len("force: "):]
            elif text.startswith("force "):
                text = text[6:]
            if text:
                print("  " + c(text[:CARD_W - 2], "grey"), flush=True)
        code = proc.wait()
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if code == 0:
        rows = doctor_line(f"✓ Force {latest} installed. Sessions keep running.")
    else:
        rows = doctor_line(f"✗ The update failed (exit {code}). Run force update in a shell for the details.")
    close(rows)
    return code


def _ui_remote(host: str) -> int:
    """`force update --ui` on a remote host (FORCE_HOST set, SPEC §3): the remote's own card runs there; only a
    host whose Force predates the card (argparse refuses --ui, exit 2) is handled here."""
    from . import config, remote
    from .cards import c, doctor_line, head, keys_right
    from .support import _wait_key

    paths = config.get_paths()
    r = remote.get(paths, host)
    if r is None:
        print("\x1b[?25l", end="", flush=True)
        print("\n".join(head("↑", "Update Force", "force update")), flush=True)
        for row in doctor_line(f"✗ no host {host!r}"):
            print(row, flush=True)
        print("", flush=True)
        print(keys_right(("any key", "Close")), flush=True)
        _wait_key()
        return 1
    code = subprocess.call(remote.ssh_argv(paths, r, remote.REMOTE_FORCE, "update", "--ui", tty=True))
    if code == 255:                                              # ssh itself failed: say so before the popup closes
        print("\x1b[?25l", end="", flush=True)
        print("\n".join(head("↑", "Update Force", "force update")), flush=True)
        for row in doctor_line(f"✗ cannot reach {host} over ssh (ssh {r.ssh} must work without a password)"):
            print(row, flush=True)
        print("", flush=True)
        print(keys_right(("any key", "Close")), flush=True)
        _wait_key()
        return code
    if code != 2:
        return code                                              # the remote's card already waited for its key
    print("\x1b[?25l", end="", flush=True)
    print("\n".join(head("↑", "Update Force", "force update")), flush=True)
    print("  " + c(f"{host} runs an older Force; updating it with force update…", "grey"), flush=True)
    code = subprocess.call(remote.ssh_argv(paths, r, remote.REMOTE_FORCE, "update"))
    if code == 0:
        rows = doctor_line(f"✓ {host} is updated.")
    else:
        rows = doctor_line(f"✗ The update on {host} failed (exit {code}).")
    for row in rows:
        print(row, flush=True)
    print("", flush=True)
    print(keys_right(("any key", "Close")), flush=True)
    _wait_key()
    return code


def ui_command() -> int:
    """`force update --ui`: the update card in the popup (SPEC §3). With FORCE_HOST it runs on the remote."""
    host = os.environ.get("FORCE_HOST", "").strip()
    if host:
        return _ui_remote(host)
    return _ui_local()


def _write_tmp(content: bytes) -> str:
    import tempfile
    fd, path = tempfile.mkstemp(prefix="force-update-", suffix=".sh")
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


def check_daemon(state: Path) -> str | None:
    """One silent background check: fetch+parse the latest, write `<state>/update.json`
    `{"latest": "B", "checked_ms": …}`, return B. Any failure returns None and keeps the old file."""
    base = download_base()
    if base is None or current_version() is None:
        return None
    content, _err = fetch_install(base, _CHECK_TIMEOUT)
    if content is None:
        return None
    latest = parse_install_version(content)
    if latest is None:
        return None
    try:
        state.mkdir(parents=True, exist_ok=True)
        tmp = state / "update.json.tmp"
        tmp.write_text(json.dumps({"latest": latest, "checked_ms": int(time.time() * 1000)}))
        os.replace(tmp, state / "update.json")
    except OSError:
        return None
    return latest


def snapshot_update(state: Path) -> str | None:
    """The value for the snapshot's `"update"` key: the checked latest when it is newer than the running
    VERSION, else None (no key)."""
    if current_version() is None:
        return None
    try:
        data = json.loads((state / "update.json").read_text())
        latest = data.get("latest")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(latest, str):
        return None
    a, b = _tuple(latest), _tuple(current_version())
    if a is None or b is None or a <= b:
        return None
    return latest
