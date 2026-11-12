#!/usr/bin/env python3
# force.services: the one place that talks to the service manager.
#
# Linux   -> systemd (systemctl --user, loginctl linger, journalctl), identical to what the code did before.
# macOS   -> launchd LaunchAgents in ~/Library/LaunchAgents (SPEC §51.1).
# neither -> Force starts its servers itself.
#
# Every subprocess call captures output with a timeout and swallows OSError / TimeoutExpired.
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from force import config

# The three services Force manages: API name -> systemd unit / launchd label.
SYSTEMD_UNITS = {
    "tmux": "force-tmux.service",
    "daemon": "force.service",
    "smoke": "force-smoke.timer",
}
LAUNCHD_LABELS = {
    "tmux": "cc.useforce.force-tmux",
    "daemon": "cc.useforce.force",
    "smoke": "cc.useforce.force-smoke",
}


def _say(msg: str) -> None:
    if sys.stdout.isatty():
        print("\033[1;38;2;217;119;87mforce\033[0m " + msg)
    else:
        print(f"force: {msg}")


def _real_run(argv, **kw):
    """A command's result; a missing program is exit status 127, a timeout 124, never an exception."""
    timeout = kw.pop("timeout", 15)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {e.timeout} s")


def backend(env=None, run=None) -> str | None:
    """'systemd' / 'launchd' when the machine's service manager answers, else None. Never for a development
    instance (SPEC §2.1): an instance starts its own services, not the live install's."""
    if config.instance(env) is not None:
        return None
    r = _real_run if run is None else run
    if sys.platform.startswith("linux"):
        if shutil.which("systemctl") is not None \
                and r(["systemctl", "--user", "show-environment"]).returncode == 0:
            return "systemd"
        return None
    if sys.platform == "darwin":
        if r(["launchctl", "print", f"gui/{os.getuid()}"]).returncode == 0:
            return "launchd"
        return None
    return None


def installed(any_units: bool = False) -> bool:
    """The host services are installed and usable. systemd: force-tmux.service is present (any force*.unit when
    any_units). launchd: the tmux plist is present (any cc.useforce.* plist when any_units)."""
    b = backend()
    if b == "systemd":
        user_dir = Path.home() / ".config" / "systemd" / "user"
        if any_units:
            return any(user_dir.glob("force*.service")) or any(user_dir.glob("force*.timer"))
        return (user_dir / "force-tmux.service").exists()
    if b == "launchd":
        agents = Path.home() / "Library" / "LaunchAgents"
        if any_units:
            return any(agents.glob("cc.useforce.*.plist"))
        return (agents / "cc.useforce.force-tmux.plist").exists()
    return False


def is_active(name: str, run=None) -> bool:
    r = _real_run if run is None else run
    b = backend(run=r)
    if b == "systemd":
        try:
            return r(["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNITS[name]],
                    timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    if b == "launchd":
        uid = os.getuid()
        res = r(["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABELS[name]}"], timeout=10)
        if res.returncode != 0:
            return False
        return any(line.lstrip().startswith("state = running")
                 for line in (res.stdout or "").splitlines())
    return False


def start(*names: str, run=None) -> None:
    r = _real_run if run is None else run
    b = backend(run=r)
    if b == "systemd":
        r(["systemctl", "--user", "start"] + [SYSTEMD_UNITS[n] for n in names])
    elif b == "launchd":
        uid = os.getuid()
        for n in names:
            r(["launchctl", "kickstart", f"gui/{uid}/{LAUNCHD_LABELS[n]}"])


def restart(name: str, run=None) -> None:
    r = _real_run if run is None else run
    b = backend(run=r)
    if b == "systemd":
        r(["systemctl", "--user", "restart", SYSTEMD_UNITS[name]])
    elif b == "launchd":
        r(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABELS[name]}"])


def plists(home) -> dict[str, bytes]:
    """The three LaunchAgent plists (label -> plist bytes). Pure: tests check them on any platform."""
    home = Path(home)
    app = home / ".local" / "share" / "force" / "app"
    state = home / ".local" / "state" / "force"
    path = (f"{app}/share/tmux-bin:{home}/.local/bin:"
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    tmux = {
        "Label": LAUNCHD_LABELS["tmux"],
        "ProgramArguments": ["/bin/sh", "-c",
                          'exec tmux -L force -f "$HOME/.local/share/force/app/share/tmux.conf" -D'],
        "EnvironmentVariables": {"PATH": path},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 3,
    }
    daemon = {
        "Label": LAUNCHD_LABELS["daemon"],
        "ProgramArguments": [f"{home}/.local/bin/force", "daemon"],
        "EnvironmentVariables": {"PATH": path, "FORCE_SERVICE": "launchd"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 3,
        "AbandonProcessGroup": True,
        "StandardOutPath": str(state / "daemon.log"),
        "StandardErrorPath": str(state / "daemon.log"),
    }
    smoke = {
        "Label": LAUNCHD_LABELS["smoke"],
        "ProgramArguments": [f"{home}/.local/bin/force", "smoke", "--if-due"],
        "EnvironmentVariables": {"PATH": path},
        "StartInterval": 3600,
        "RunAtLoad": False,
        "Nice": 10,
        "ProcessType": "Background",
    }
    return {d["Label"]: plistlib.dumps(d) for d in (tmux, daemon, smoke)}


def _write_plist(path: Path, data: bytes) -> bool:
    """Atomic write when the bytes changed; returns whether they changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return False
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


def _launchd_loaded(uid, run, label) -> bool:
    return run(["launchctl", "print", f"gui/{uid}/{label}"]).returncode == 0


def _install_launchd(home, run, self_test) -> None:
    if backend(run=run) != "launchd":
        _say("no launchd session here: force starts its services itself")
        return
    home = Path(home)
    agents = home / "Library" / "LaunchAgents"
    # launchd opens the daemon's log itself and does not create its folder: without it the job never starts
    (home / ".local" / "state" / "force").mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    for label, data in plists(home).items():
        if label == LAUNCHD_LABELS["smoke"] and not self_test:
            continue
        path = agents / f"{label}.plist"
        changed = _write_plist(path, data)
        loaded = _launchd_loaded(uid, run, label)
        # a loaded, unchanged agent stays as it is (the tmux one holds the sessions; the daemon reloads itself
        # when the release changes, as under systemd)
        if loaded and not changed:
            continue
        if loaded:
            run(["launchctl", "bootout", f"gui/{uid}/{label}"])
            _wait_unloaded(uid, run, label)
        run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])


def _wait_unloaded(uid, run, label, sleep=None, tries: int = 50) -> None:
    """bootout returns before launchd has removed the job; a bootstrap of the same label until then fails."""
    import time
    nap = time.sleep if sleep is None else sleep
    for _ in range(tries):
        if not _launchd_loaded(uid, run, label):
            return
        nap(0.1)


def _linger(run, user) -> None:
    r = run(["loginctl", "show-user", user, "-p", "Linger"])
    if r.returncode == 0 and r.stdout.strip() == "Linger=yes":
        return
    rr = run(["loginctl", "enable-linger", user])
    if rr.returncode != 0:
        _say(f"note: sessions stop when you log out (ask an admin for: loginctl enable-linger {user})")


def install(rel, home, run, self_test, user) -> None:
    """Install (or refresh) the host services. Linux: systemd units + linger, exactly as before. macOS: LaunchAgents."""
    rel = Path(rel)
    if sys.platform == "darwin":
        _install_launchd(home, run, self_test)
        return
    # --- systemd (Linux): today's setup_services body --------------------------------------------
    if run(["systemctl", "--user", "show-environment"]).returncode != 0:
        _say("no systemd here: force starts its services itself")
        return
    user_sys = home / ".config" / "systemd" / "user"
    user_sys.mkdir(parents=True, exist_ok=True)
    units = sorted((rel / "systemd").glob("*.service")) + sorted((rel / "systemd").glob("*.timer"))
    changed = False
    for unit in units:
        dest = user_sys / unit.name
        text = unit.read_text()
        if dest.exists() and dest.read_text() == text:
            continue
        _atomic_write(dest, text)
        changed = True
    if changed:
        run(["systemctl", "--user", "daemon-reload"])
    active = False
    r = run(["systemctl", "--user", "show", "force.service", "-p", "ActiveState", "--value"])
    if r.returncode == 0 and r.stdout.strip() == "active":
        active = True
    # never restart/stop force-tmux.service: it holds the sessions
    run(["systemctl", "--user", "enable", "--now", "force-tmux.service", "force.service"])
    if active and changed:
        run(["systemctl", "--user", "restart", "force.service"])
    if self_test:
        run(["systemctl", "--user", "enable", "--now", "force-smoke.timer"])
    _linger(run, user)


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def uninstall(run=None) -> None:
    r = _real_run if run is None else run
    b = backend(run=r)
    if b == "systemd":
        r(["systemctl", "--user", "disable", "--now",
           "force.service", "force-smoke.timer", "force-smoke.service", "force-tmux.service"])
        for unit in list(Path.home().glob(".config/systemd/user/force*.service")) \
                + list(Path.home().glob(".config/systemd/user/force*.timer")):
            try:
                unit.unlink()
            except OSError:
                pass
        r(["systemctl", "--user", "daemon-reload"])
    elif b == "launchd":
        uid = os.getuid()
        for label in LAUNCHD_LABELS.values():
            r(["launchctl", "bootout", f"gui/{uid}/{label}"])
    remove_launch_agents()


def remove_launch_agents() -> None:
    """macOS: delete Force's LaunchAgent files, even when launchd does not answer here (an ssh login): they would
    start Force at the next login. Never runs a command; nothing to do on Linux."""
    if sys.platform != "darwin":
        return
    for plist_file in (Path.home() / "Library" / "LaunchAgents").glob("cc.useforce.*.plist"):
        try:
            plist_file.unlink()
        except OSError:
            pass


def logs(state_dir, n: int) -> str:
    """Tail <state>/daemon.log (the daemon logs there in both managers). Linux falls back to journalctl."""
    log = Path(state_dir) / "daemon.log"
    if log.exists():
        try:
            return "\n".join(log.read_text(errors="replace").splitlines()[-n:]) or "(empty)"
        except OSError as e:
            return f"cannot read {log}: {e}"
    if sys.platform == "darwin":
        return "(no log lines)"
    try:
        r = subprocess.run(["journalctl", "--user", "-u", "force.service", "-n", str(n),
                           "--no-pager", "-o", "short-iso"],
                          capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or (r.stderr.strip() or "(no log lines)")
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"no log available: {e}"


def under_service_manager() -> bool:
    """This process was started by the service manager? systemd sets INVOCATION_ID; the launchd daemon plist sets
    FORCE_SERVICE=launchd."""
    return os.environ.get("INVOCATION_ID") is not None or os.environ.get("FORCE_SERVICE") == "launchd"
