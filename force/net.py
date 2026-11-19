"""`force net up | down | status` (SPEC §56.2): open the direct link's UDP port (`linkPort`) in this host's firewall,
so that a remote's direct link reaches it, close it again, and say what is open. Run on the host to be reached, as
the user: the one firewall command runs under `sudo`, which asks for the password; Force itself never runs as root.

Also the hint card (`force net hint --ui HOST`, opened from the sidebar's `HOST: ssh only · force net up there`).

Every program runs through `System.run` (tests inject a fake: never a real sudo or firewall).

  <state>/net.json   {"firewall", "port", "commands", "at"} (+ "app" on macOS): what `force net up` opened here
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config, link, remote

SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"
COMMENT = "Force direct link"
# firewalls `force net up` opens itself; the others it only names the rule for
OPENS = ("ufw", "firewalld", "macos")
LABEL = {"ufw": "ufw", "firewalld": "firewalld", "macos": "the macOS application firewall",
         "nftables": "nftables", "iptables": "iptables"}


def _real_run(argv: list[str], interactive: bool = False, timeout: float = 10) -> subprocess.CompletedProcess:
    """A program's result, never an exception (missing: 127, timed out: 124). `interactive`: on this terminal
    (sudo asks for the password there), no timeout; otherwise output captured, stdin closed."""
    try:
        if interactive:
            return subprocess.run(argv)
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "timed out")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 126, "", str(e))


@dataclass
class System:
    """What `force net` asks of the machine; tests replace any part."""
    run: Callable = _real_run
    which: Callable = shutil.which
    platform: str = sys.platform
    env: dict = field(default_factory=lambda: dict(os.environ))
    root: Path = Path("/")                      # /etc/ufw/ufw.conf and /proc/version are read under it
    euid: Callable = os.geteuid
    tty: Callable = lambda: sys.stdin.isatty() and sys.stdout.isatty()


def net_file(paths) -> Path:
    return paths.state / "net.json"


def read_record(paths) -> dict | None:
    """What `force net up` recorded here: None when nothing, {} when the file cannot be read."""
    try:
        data = json.loads(net_file(paths).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_record(paths, data: dict) -> None:
    path = net_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------------------------ what is there --
def is_wsl(s: System) -> bool:
    if s.env.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in (s.root / "proc" / "version").read_text().lower()
    except OSError:
        return False


def _active(s: System, unit: str) -> bool:
    """`systemctl is-active UNIT` (the system's units: no root needed) says active."""
    r = s.run(["systemctl", "is-active", unit])
    return r.returncode == 0 and (r.stdout or "").strip() == "active"


def _ufw_enabled(s: System) -> bool | None:
    """ENABLED=yes in /etc/ufw/ufw.conf; None when the file cannot be read."""
    try:
        text = (s.root / "etc" / "ufw" / "ufw.conf").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "ENABLED":
            return value.strip().strip("'\"").lower() == "yes"
    return False


def detect(s: System) -> str | None:
    """The host firewall, found without root: "ufw" | "firewalld" | "macos" | "nftables" | "iptables" | None."""
    if s.platform == "darwin":
        r = s.run([SOCKETFILTERFW, "--getglobalstate"])
        text = (r.stdout or "").lower()
        return "macos" if r.returncode == 0 and "enabled" in text else None
    if s.which("ufw"):
        on = _ufw_enabled(s)
        if on is None:
            on = _active(s, "ufw")
        if on:
            return "ufw"
    if _active(s, "firewalld"):
        return "firewalld"
    if s.which("firewall-cmd"):
        r = s.run(["firewall-cmd", "--state"])
        if r.returncode == 0 and (r.stdout or "").strip() == "running":
            return "firewalld"
    if _active(s, "nftables"):
        return "nftables"
    if _active(s, "iptables") or _active(s, "netfilter-persistent"):
        return "iptables"
    return None


# --------------------------------------------------------------------------------------- the commands --
def open_commands(firewall: str, port: int, app: str | None = None) -> list[list[str]]:
    if firewall == "ufw":
        return [["sudo", "ufw", "allow", f"{port}/udp", "comment", COMMENT]]
    if firewall == "firewalld":
        return [["sudo", "firewall-cmd", "--permanent", f"--add-port={port}/udp"], ["sudo", "firewall-cmd", "--reload"]]
    if firewall == "macos":
        return [["sudo", SOCKETFILTERFW, "--add", app], ["sudo", SOCKETFILTERFW, "--unblockapp", app]]
    raise ValueError(firewall)


def close_commands(record: dict) -> list[list[str]]:
    fw, port, app = record.get("firewall"), record.get("port"), record.get("app")
    if fw == "ufw":
        return [["sudo", "ufw", "delete", "allow", f"{port}/udp"]]
    if fw == "firewalld":
        return [["sudo", "firewall-cmd", "--permanent", f"--remove-port={port}/udp"],
                ["sudo", "firewall-cmd", "--reload"]]
    if fw == "macos":
        return [["sudo", SOCKETFILTERFW, "--remove", app]]
    raise ValueError(fw)


def manual_rule(firewall: str, port: int) -> str:
    if firewall == "nftables":
        return f"sudo nft add rule inet filter input udp dport {port} accept"
    return f"sudo iptables -I INPUT -p udp --dport {port} -j ACCEPT"


def _valid_record(rec: dict) -> bool:
    port = rec.get("port")
    if rec.get("firewall") not in OPENS or isinstance(port, bool) or not isinstance(port, int) \
            or not 0 < port < 65536:
        return False
    return rec.get("firewall") != "macos" or isinstance(rec.get("app"), str)


def _err(msg: str) -> None:
    print(f"force: {msg}", file=sys.stderr)


def _as_root(s: System, what: str) -> bool:
    if s.euid() != 0:
        return False
    _err(f"run force net {what} as yourself, not with sudo: it asks sudo for the firewall command itself, "
         "and sudo's PATH has no force")
    return True


def _run_all(s: System, cmds: list[list[str]], what: str) -> int | None:
    """Print the commands, then run them on this terminal (sudo asks for the password). None when all succeeded,
    else the exit status to return."""
    for c in cmds:
        print("  " + shlex.join(c))
    if not s.tty():
        _err(f"sudo asks for your password: run force net {what} in a terminal (or the commands above yourself)")
        return 1
    for c in cmds:
        r = s.run(c, interactive=True)
        if r.returncode != 0:
            _err(f"{shlex.join(c)} failed (exit {r.returncode})")
            return 1
    return None


def up(paths, cfg, s: System | None = None) -> int:
    """`force net up` (SPEC §56.2)."""
    s = s or System()
    if _as_root(s, "up"):
        return 1
    if not cfg.direct_link:
        print("directLink is off: nothing listens here, so there is nothing to open "
              "(force settings set directLink true)")
        return 0
    port = cfg.link_port
    if is_wsl(s):
        print("This is WSL: incoming connections into WSL are not supported, so this machine cannot be reached "
              "directly. Reaching your remotes from here needs nothing: the link dials out. Run force net up on "
              "the machine to be reached.")
        return 0
    fw = detect(s)
    if fw is None:
        print(f"No host firewall found: nothing to open on this machine. A router or cloud firewall in front of it "
              f"is opened there (UDP {port}); otherwise remotes keep reaching it by ssh.")
        return 0
    if fw not in OPENS:
        print(f"This machine has {LABEL[fw]} rules of its own, which Force does not change. To let the direct "
              f"link in, add a rule for UDP {port}, such as:\n  {manual_rule(fw, port)}")
        return 0
    rec = read_record(paths)
    if rec is not None:
        if rec.get("firewall") == fw and rec.get("port") == port:
            print(f"UDP {port} is open in {LABEL[fw]} already (force net status)")
            return 0
        what = f"UDP {rec.get('port')} in {LABEL.get(rec.get('firewall'), rec.get('firewall'))}" \
            if _valid_record(rec) else f"a rule ({net_file(paths)})"
        _err(f"force net up opened {what} before: run force net down first")
        return 1
    app = None
    if fw == "macos":
        app = str(remote.link_bin(paths))
        if not os.path.exists(app):
            _err(f"{app} is missing: this release has no direct link")
            return 1
    cmds = open_commands(fw, port, app)
    print(f"Opening UDP {port} for Force's direct link in {LABEL[fw]}. This runs:")
    code = _run_all(s, cmds, "up")
    if code is not None:
        if s.tty():
            _err("nothing recorded")
        return code
    rec = {"firewall": fw, "port": port, "commands": [shlex.join(c) for c in cmds], "at": time.time()}
    if app:
        rec["app"] = app
    _write_record(paths, rec)
    print(f"✓ UDP {port} is open. Your remotes switch to the direct link at their next try (within a minute); "
          "ssh keeps working meanwhile. force net down closes it.")
    return 0


def down(paths, cfg, s: System | None = None) -> int:
    """`force net down` (SPEC §56.2): undo what net.json records, then delete it."""
    s = s or System()
    if _as_root(s, "down"):
        return 1
    rec = read_record(paths)
    if rec is None:
        print("Nothing to close: force net up opened nothing on this machine.")
        return 0
    if not _valid_record(rec):
        _err(f"{net_file(paths)} cannot be read: close the rule by hand, then delete the file")
        return 1
    print(f"Closing UDP {rec['port']} in {LABEL[rec['firewall']]}. This runs:")
    code = _run_all(s, close_commands(rec), "down")
    if code is not None:
        return code
    try:
        net_file(paths).unlink()
    except FileNotFoundError:
        pass
    print("✓ closed. Remotes reach this machine by ssh.")
    return 0


def status_doc(paths, cfg, s: System | None = None) -> dict:
    """`force net status --json`: the listener, the firewall found, what net up recorded, linkPort."""
    s = s or System()
    running = link.daemon_running(paths)
    card = link.self_card(paths) if running else None
    wsl = is_wsl(s)
    return {"daemon": {"running": running, "port": card["port"] if card else None},
            "link_port": cfg.link_port, "direct_link": cfg.direct_link,
            "wsl": wsl, "firewall": None if wsl else detect(s), "net": read_record(paths)}


def status(paths, cfg, as_json: bool = False, s: System | None = None) -> int:
    doc = status_doc(paths, cfg, s)
    if as_json:
        print(json.dumps(doc, indent=2))
        return 0
    d = doc["daemon"]
    if d["running"]:
        listener = f"running, UDP {d['port']}" if d["port"] else "running"
        if d["port"] and d["port"] != doc["link_port"]:
            listener += f" (linkPort is {doc['link_port']}: it restarts with it)"
    else:
        listener = "not running" + ("" if doc["direct_link"] else " (directLink is off)")
    fw = doc["firewall"]
    if doc["wsl"]:
        firewall = "WSL: no incoming connections"
    elif fw is None:
        firewall = "none found"
    elif fw in OPENS:
        firewall = LABEL[fw]
    else:
        firewall = f"{LABEL[fw]} (rules of its own: force net up prints the rule to add)"
    rec = doc["net"]
    if rec is None:
        done = "nothing opened"
    elif _valid_record(rec):
        at = rec.get("at")
        when = time.strftime(" on %Y-%m-%d %H:%M", time.localtime(at)) if isinstance(at, (int, float)) else ""
        done = f"UDP {rec['port']} opened in {LABEL[rec['firewall']]}{when} (force net down closes it)"
    else:
        done = f"{net_file(paths)} cannot be read"
    for k, v in (("listener", listener), ("firewall", firewall), ("net up", done), ("linkPort", str(doc["link_port"]))):
        print(f"{k:9} {v}")
    return 0


# -------------------------------------------------------------------------------------- the hint card --
def hint_port(paths, cfg, host: str) -> int:
    """The remote's linkPort, from the card its hello carried (link.json), else ours."""
    p = link.read_link(paths, host).get("port")
    return p if isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536 else cfg.link_port


def hint_text(host: str, port: int, color: bool = True, buttons: bool = True) -> str:
    """The hint card (SPEC §56.2): what the direct link gives, the command to run there, ssh meanwhile."""
    from .cards import c, head, keys_right
    lines = head("⇄", "Direct link", f"{host} · ssh only", color)
    lines += ["  " + c(f"{host} answers by ssh only: its direct link gets no answer.", "name", color=color),
              "  " + c("The direct link runs commands faster and keeps a session through", "grey", color=color),
              "  " + c("a change of network (a laptop waking up, another Wi-Fi).", "grey", color=color),
              "",
              "  " + c(f"On {host}, run:", "name", color=color),
              "      " + c("force net up", "coral", bold=True, color=color),
              "  " + c(f"It opens UDP {port} in that machine's firewall (sudo asks there).", "grey", color=color),
              "  " + c("ssh keeps working meanwhile.", "dim", color=color)]
    if buttons:
        lines += ["", keys_right(("d", "Don't show again"), ("Esc", "Close"), color=color), ""]
    return "\n".join(lines)


def hint_command(paths, cfg, host: str | None, ui: bool, wait_key=None) -> int:
    """`force net hint [--ui] HOST`: the hint card. In the popup (--ui) `d` turns the hint off (netUpHint false,
    as `force settings set` does); any other key closes it."""
    if not host or not remote.NAME_RE.fullmatch(host):
        _err("usage: force net hint [--ui] HOST")
        return 2
    port = hint_port(paths, cfg, host)
    if not ui:
        print(hint_text(host, port, color=False, buttons=False))
        return 0
    if wait_key is None:
        from .support import _wait_key as wait_key
    print("\x1b[?25l", end="", flush=True)                      # no blinking cursor on the card
    print(hint_text(host, port), end="", flush=True)
    if wait_key() in ("d", "D"):
        try:
            config.save_setting(paths.config_file, "netUpHint", False)
        except ValueError as e:
            _err(str(e))
            return 1
    return 0


def command(paths, cfg, action: str, host: str | None = None, as_json: bool = False, ui: bool = False,
            s: System | None = None) -> int:
    """`force net up | down | status [--json]` (and the internal `hint [--ui] HOST`)."""
    if action == "up":
        return up(paths, cfg, s)
    if action == "down":
        return down(paths, cfg, s)
    if action == "status":
        return status(paths, cfg, as_json, s)
    if action == "hint":
        return hint_command(paths, cfg, host, ui)
    _err("usage: force net up | down | status [--json]")
    return 2
