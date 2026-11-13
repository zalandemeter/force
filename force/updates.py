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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import force

# force update: a generous timeout and the 1 MB cap the brief allows
_MAX_BYTES = 1_000_000
_FETCH_TIMEOUT = 20
# the daemon's silent background check: shorter timeout, small file
_CHECK_TIMEOUT = 10
# the daemon's cadence (SPEC §3): first check 2 min after start, then hourly, and when a terminal opens the cockpit
# (at most one check per 10 min). Each check after the first is conditional (If-None-Match / If-Modified-Since), so
# an unchanged install.sh costs a 304 with no body.
_FIRST_DELAY = 120
_INTERVAL = 3600
_MIN_GAP = 600

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


def _update_here(check: bool) -> int:
    """This machine only (`force update --here`): installs the newer release (runs its install.sh --no-fonts) or
    reports."""
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


# a remote's `force update --here` answer, as one short line (SPEC §46)
_UPDATING = re.compile(r"updating (\S+) -> (\S+)")
_UP_TO_DATE = re.compile(r"Force (\S+) is up to date")
_AVAILABLE = re.compile(r"update available: (\S+) -> (\S+)")
_REMOTE_TIMEOUT = 900


def _summary(code: int, text: str) -> str:
    """One line for a host's update: `1.4.1 → 1.4.2`, `already 1.4.2`, `1.4.1 → 1.4.2 available`, or why not."""
    from .cards import plain
    lines = [plain(ln).strip() for ln in text.splitlines() if plain(ln).strip()]
    joined = "\n".join(lines)
    if code == 255:
        return "unreachable (ssh failed)"
    if (m := _UPDATING.search(joined)) is not None:
        return f"{m.group(1)} → {m.group(2)}" + ("" if code == 0 else f" failed (exit {code})")
    if (m := _AVAILABLE.search(joined)) is not None:
        return f"{m.group(1)} → {m.group(2)} available"
    if (m := _UP_TO_DATE.search(joined)) is not None:
        return f"already {m.group(1)}"
    last = lines[-1] if lines else ""
    if last.startswith("force: "):
        last = last[len("force: "):]
    return last or (f"exit {code}" if code else "done")


def update_remote(paths, r, check: bool = False, timeout: int = _REMOTE_TIMEOUT, run=None) -> tuple[bool, str]:
    """`force update --here [--check]` on remote `r` over Force's ssh (it fetches its own platform's release).
    An older Force there has no `--here` (argparse: exit 2): ask again without it, which meant the same then.
    -> (ok, one-line summary)."""
    from . import remote
    run = run or subprocess.run         # looked up now, not when the module loaded (tests replace it)
    words = ["update", *(["--check"] if check else []), "--here"]
    try:
        p = run(remote.ssh_argv(paths, r, remote.REMOTE_FORCE, *words), capture_output=True, text=True,
                timeout=timeout, stdin=subprocess.DEVNULL)
        if p.returncode == 2 and "--here" in (p.stderr or ""):
            p = run(remote.ssh_argv(paths, r, remote.REMOTE_FORCE, *words[:-1]), capture_output=True, text=True,
                    timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"no answer within {timeout // 60} min"
    except OSError as e:
        return False, f"ssh: {e}"
    return p.returncode == 0, _summary(p.returncode, (p.stdout or "") + "\n" + (p.stderr or ""))


def _remotes(paths, host: str | None):
    from . import remote
    rs = [r for r in remote.load(paths) if r.enabled]
    return [r for r in rs if r.name == host] if host else rs


def update_remotes(paths, rs, check: bool, on_done, run=None) -> bool:
    """Update remotes `rs` in parallel; `on_done(name, ok, summary)` as each finishes (from worker threads, one at a
    time). -> True when all of them were fine."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    lock = threading.Lock()
    ok_all = True

    def one(r):
        nonlocal ok_all
        ok, summary = update_remote(paths, r, check=check, run=run)
        with lock:
            ok_all = ok_all and ok
            on_done(r.name, ok, summary)
    if rs:
        with ThreadPoolExecutor(max_workers=min(8, len(rs))) as pool:
            list(pool.map(one, rs))
    return ok_all


def update_command(check: bool = False, here: bool = False, host: str | None = None) -> int:
    """`force update [--check] [--here | --host NAME]` (SPEC §46): every enabled remote first (in parallel, each
    its own `force update --here`), then this machine, last because its update restarts the daemon. `--here`: this
    machine only; `--host NAME`: that remote only. Exit 1 when any host failed."""
    from . import config
    if here:
        return _update_here(check)
    paths = config.get_paths()
    rs = _remotes(paths, host)
    if host and not rs:
        print(f"force: no enabled host {host!r} (force host list)", file=sys.stderr)
        return 1

    def report(name, ok, summary):
        print(f"{'✓' if ok else '✗'} {name}: {summary}", flush=True)
    remotes_ok = update_remotes(paths, rs, check, report)
    if host:
        return 0 if remotes_ok else 1
    code = _update_here(check)
    return code or (0 if remotes_ok else 1)


def _host_rows(paths) -> list[tuple[str, str | None, bool]]:
    """The update card's hosts (SPEC §46): (name, the X.Y.Z it runs or None, connected) for this machine and each
    enabled remote, the remote's version from its last hello (`status.json`)."""
    import socket
    from . import remote
    rows = [(socket.gethostname().split(".")[0] + " (this machine)", current_version(), True)]
    for r in _remotes(paths, None):
        st = remote.read_status(paths, r.name)
        rows.append((r.name, base_version(st.get("version")), st.get("state") == "connected"))
    return rows


def _ui_local() -> int:
    """`force update --ui` (SPEC §46): the update card, always on this machine, for every host: what each runs, a
    live check, Enter updates them all (the remotes in parallel, then this machine), one line per host. `_wait_key`
    from force.support: '' (no terminal) means Esc: nothing is updated."""
    from . import config
    from .cards import CARD_W, c, doctor_line, head, keys_right, plain
    from .support import _wait_key

    paths = config.get_paths()
    hosts = _host_rows(paths)
    name_w = min(max(len(h[0]) for h in hosts) + 2, 34)
    print("\x1b[?25l", end="", flush=True)                       # no blinking cursor on the card
    print("\n".join(head("↑", "Update Force", "force update")), flush=True)
    for name, ver, connected in hosts:
        shown = (ver or "a development build") if connected else "offline"
        print("  " + c(f"{name[:name_w - 2]:<{name_w}}", "name") + c(shown, "grey" if connected else "dim"), flush=True)
    print("", flush=True)
    sys.stdout.write("  " + c("Checking for a newer release…", "grey"))
    sys.stdout.flush()

    cur, latest, content, problem = find_latest(_FETCH_TIMEOUT)

    def close(rows: list[str]) -> None:
        print("\r\x1b[K", end="", flush=True)                   # erase the "Checking…" line
        for row in rows:
            print(row, flush=True)
        print("", flush=True)
        print(keys_right(("any key", "Close")), flush=True)
        _wait_key()

    if latest is None:                                           # no answer from the site: nothing to compare with
        close(doctor_line("✗ " + (problem or "no newest release found")))
        return 0 if (problem or "").startswith("development build") else 1
    behind = [h for h in hosts[1:] if h[1] is None or _tuple(h[1]) < _tuple(latest)]   # offline counts: try it
    here_behind = cur is not None and _tuple(cur) < _tuple(latest)
    if not behind and not here_behind:
        close(doctor_line(f"✓ Every host runs Force {latest}, the newest release."))
        return 0

    print("\r\x1b[K", end="", flush=True)                       # erase the "Checking…" line
    print("  " + c(f"Force {latest} is available.", "white", bold=True), flush=True)
    print("  " + c("Sessions keep running; each cockpit reloads with the new version.", "dim"), flush=True)
    base = download_base()
    if base and urllib.parse.urlparse(base).hostname == "useforce.cc":
        print("  " + c(f"What's new: https://github.com/zalandemeter/force/releases/tag/v{latest}", "dim"),
              flush=True)
    print("", flush=True)
    print(keys_right(("Esc", "Close"), button="Enter  Update all"), flush=True)
    if _wait_key() not in ("\r", "\n"):
        return 0                                                 # Esc or no terminal: no update

    print("\x1b[1A\r\x1b[K" + "  " + c("Updating…", "grey"), flush=True)     # in place of the keys row

    def report(name, ok, summary):
        print("  " + c("✓" if ok else "✗", "ok" if ok else "bad", bold=True) + " "
              + c(f"{name[:name_w - 2]:<{name_w}}", "name") + c(summary[:CARD_W - name_w - 4], "grey"), flush=True)
    remotes_ok = update_remotes(paths, [r for r in _remotes(paths, None) if any(h[0] == r.name for h in behind)],
                                False, report)
    code = 0
    if here_behind:
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
        report(hosts[0][0], code == 0, f"{cur} → {latest}" + ("" if code == 0 else f" failed (exit {code})"))
    if code == 0 and remotes_ok:
        rows = doctor_line(f"✓ Force {latest} installed. Sessions keep running.")
    else:
        rows = doctor_line("✗ Not every host was updated (above). Run force update in a shell for the details.")
    close(rows)
    return code or (0 if remotes_ok else 1)


def ui_command() -> int:
    """`force update --ui`: the update card in the popup (SPEC §46), always on this machine, for every host."""
    return _ui_local()


def _write_tmp(content: bytes) -> str:
    import tempfile
    fd, path = tempfile.mkstemp(prefix="force-update-", suffix=".sh")
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


def _fetch_conditional(base: str, timeout: int, etag: str | None, modified: str | None
                       ) -> tuple[int, bytes | None, str | None, str | None]:
    """GET `<base>/install.sh` with If-None-Match / If-Modified-Since -> (status, content, etag, last_modified).
    status 304 = unchanged (content None), 200 = new content, 0 = any failure."""
    url = base.rstrip("/") + "/install.sh"
    headers = {"User-Agent": f"force/{current_version() or 'dev'}"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            data = resp.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                return 0, None, None, None
            return 200, data, resp.headers.get("ETag"), resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, etag, modified
        return 0, None, None, None
    except Exception:
        return 0, None, None, None


def check_daemon(state: Path) -> str | None:
    """One silent background check: write `<state>/update.json` `{"latest": "B", "checked_ms": …, "etag": …,
    "last_modified": …}` and return B. The request is conditional on the validators of the last check: a 304 keeps
    the known latest and only refreshes checked_ms. Any failure returns None and keeps the old file."""
    base = download_base()
    if base is None or current_version() is None:
        return None
    path = state / "update.json"
    try:
        old = json.loads(path.read_text())
        old = old if isinstance(old, dict) else {}
    except (OSError, ValueError):
        old = {}
    known = old.get("latest") if isinstance(old.get("latest"), str) else None
    etag = old.get("etag") if known and isinstance(old.get("etag"), str) else None
    modified = old.get("last_modified") if known and isinstance(old.get("last_modified"), str) else None
    status, content, etag, modified = _fetch_conditional(base, _CHECK_TIMEOUT, etag, modified)
    if status == 304:
        latest = known
    elif status == 200 and content is not None:
        latest = parse_install_version(content)
    else:
        return None
    if latest is None:
        return None
    record = {"latest": latest, "checked_ms": int(time.time() * 1000)}
    if etag:
        record["etag"] = etag
    if modified:
        record["last_modified"] = modified
    try:
        state.mkdir(parents=True, exist_ok=True)
        tmp = state / "update.json.tmp"
        tmp.write_text(json.dumps(record))
        os.replace(tmp, path)
    except OSError:
        return None
    return latest


def base_version(v: str | None) -> str | None:
    """The `X.Y.Z` a release name starts with (`1.4.1-feat-x-abc123` -> `1.4.1`), else None."""
    m = re.match(r"(\d+\.\d+\.\d+)", v or "")
    return m.group(1) if m else None


def latest_known(state: Path) -> str | None:
    """The newest release the last check found (`<state>/update.json`), or None."""
    try:
        latest = json.loads((state / "update.json").read_text()).get("latest")
    except (OSError, ValueError, AttributeError):
        return None
    return latest if isinstance(latest, str) and _tuple(latest) is not None else None


def snapshot_update(state: Path, others: tuple = ()) -> str | None:
    """The value for the snapshot's `"update"` key (SPEC §3): the checked latest when this machine, or any of
    `others` (the versions the enabled remotes run), runs an older one; else None (no key)."""
    if current_version() is None:
        return None
    latest = latest_known(state)
    if latest is None:
        return None
    for v in (current_version(), *others):
        b = _tuple(base_version(v))
        if b is not None and _tuple(latest) > b:
            return latest
    return None
