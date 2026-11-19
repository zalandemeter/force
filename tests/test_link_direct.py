"""SPEC §56.2: the direct link end to end. Two (or three) `force-link daemon`s on loopback, each with its own tmp
home, state folder and an ephemeral UDP port; `force-link exec` through the client's daemon runs a fake
`~/.local/bin/force` on the other one. Never the live state, sockets or service manager; ssh is a fake on PATH that
only records that it ran. Every wait has a timeout."""
import fcntl
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="unix only")

FAKE_FORCE = """#!{py}
import fcntl, os, signal, struct, sys, termios
def size():
    r, c = struct.unpack("hh", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\\0" * 4))
    return f"{{r}} {{c}}"
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "roundtrip":
    print("args:" + "|".join(sys.argv[2:]), flush=True)
    data = sys.stdin.read()
    print("stdin:" + data, flush=True)
    print("oops", file=sys.stderr, flush=True)
    print("env:" + ",".join(sorted(k for k in os.environ if not k.startswith("LC_"))), flush=True)
    sys.exit(7)
if cmd == "tty":
    print("istty" if os.isatty(0) else "notty", flush=True)
    print("size:" + size(), flush=True)
    print("term:" + os.environ.get("TERM", ""), flush=True)
    signal.signal(signal.SIGWINCH, lambda *a: print("resized:" + size(), flush=True))
    line = sys.stdin.readline()
    print("got:" + line.strip(), flush=True)
    sys.exit(5)
sys.exit(0)
"""

FAKE_SSH = """#!/bin/sh
echo "$@" >> "{log}"
echo "fake ssh" >&2
exit 42
"""


def wait_for(pred, timeout, what):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


class Host:
    """One machine: a home, Force's state and config folders under it, and its link daemon."""

    def __init__(self, root: Path, name: str, link_bin: Path):
        self.name = name
        self.link_bin = link_bin
        self.home = root / name
        self.state = self.home / ".local/state/force"
        self.config = self.home / ".config/force"
        self.hosts = self.config / "hosts.json"
        self.peers = self.config / "link-peers.json"
        self.bin = self.home / "bin"
        for d in (self.state, self.config, self.bin, self.home / ".local/bin"):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        self.proc = None
        self.log = self.home / "daemon.log"

    def env(self):
        return {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8", "SECRET_TOKEN": "must-not-pass"}

    def start(self):
        self.proc = subprocess.Popen(
            [str(self.link_bin), "daemon", "--state", str(self.state), "--hosts", str(self.hosts),
             "--peers", str(self.peers), "--port", "0"],
            env=self.env(), stdout=subprocess.DEVNULL, stderr=open(self.log, "ab"), stdin=subprocess.DEVNULL)
        me = wait_for(lambda: read_json(self.state / "link/self.json"), 15, f"{self.name}'s self.json")
        wait_for(lambda: (self.state / "link.sock").exists(), 15, f"{self.name}'s link.sock")
        self.id, self.port = me["id"], me["port"]
        return self

    def stop(self, sig=signal.SIGTERM):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def trust(self, other: "Host"):
        """`force link trust`'s file: `other` may connect here."""
        peers = read_json(self.peers) or {"peers": []}
        peers["peers"].append({"id": other.id, "name": other.name})
        self.peers.write_text(json.dumps(peers))
        os.chmod(self.peers, 0o600)

    def pair_to(self, other: "Host", name: str = "devbox"):
        """Record `other` as remote `name` here (hosts.json + link.json, as the watcher does)."""
        self.hosts.write_text(json.dumps({"hosts": [{"name": name, "ssh": name}]}))
        d = self.state / "remotes" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "link.json").write_text(json.dumps({"id": other.id, "addrs": [f"127.0.0.1:{other.port}"],
                                                 "port": other.port}))

    def link_state(self, name="devbox"):
        return (read_json(self.state / "remotes" / name / "link.json") or {}).get("state")

    def exec_argv(self, *argv, tty=False, name="devbox"):
        return [str(self.link_bin), "exec", "--state", str(self.state), "--hosts", str(self.hosts), name,
                *(["--tty"] if tty else []), "--", *argv]


@pytest.fixture
def world(force_link_bin):
    # a short folder: unix socket paths are limited to about 100 bytes
    root = Path(tempfile.mkdtemp(prefix="fl-", dir="/tmp"))
    hosts = []

    def make(name):
        h = Host(root, name, force_link_bin)
        hosts.append(h)
        ssh_log = root / f"{name}-ssh.log"
        (h.bin / "ssh").write_text(FAKE_SSH.format(log=ssh_log))
        os.chmod(h.bin / "ssh", 0o755)
        h.ssh_log = ssh_log
        f = h.home / ".local/bin/force"
        f.write_text(FAKE_FORCE.format(py=sys.executable))
        os.chmod(f, 0o755)
        return h

    try:
        yield make
    finally:
        for h in hosts:
            h.stop(signal.SIGKILL)
        if os.environ.get("FORCE_KEEP_LINK_TEST"):
            print("kept", root)
        else:
            shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def paired(world):
    """laptop -> devbox, paired both ways (devbox accepts laptop's id; laptop dials devbox's id)."""
    laptop = world("laptop").start()
    devbox = world("devbox")
    devbox.peers.write_text(json.dumps({"peers": [{"id": laptop.id, "name": "laptop"}]}))
    os.chmod(devbox.peers, 0o600)
    devbox.start()
    laptop.pair_to(devbox)
    wait_for(lambda: laptop.link_state() == "direct", 15, "the direct link")
    return laptop, devbox


def test_exec_round_trip_over_the_direct_link(paired):
    laptop, devbox = paired
    p = subprocess.run(laptop.exec_argv("~/.local/bin/force", "roundtrip", "a b", "~/x"), input=b"hello\nworld",
                       capture_output=True, env=laptop.env(), timeout=30)
    out = p.stdout.decode()
    assert p.returncode == 7, (p.returncode, p.stderr)
    assert f"args:a b|{devbox.home}/x\n" in out          # every leading ~/ expanded there, as the remote shell does
    assert "stdin:hello\nworld\n" in out
    assert p.stderr == b"oops\n"
    # only the short list of variables reaches the program (none of the daemon's other ones)
    env_line = next(line for line in out.splitlines() if line.startswith("env:"))
    assert env_line == "env:HOME,LANG,PATH", env_line
    assert not laptop.ssh_log.exists(), "went over ssh"


def test_status_lists_the_remote(paired):
    laptop, devbox = paired
    p = subprocess.run([str(laptop.link_bin), "status", "--state", str(laptop.state), "--json"],
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0, p.stderr
    st = json.loads(p.stdout)
    assert st["running"] is True and st["id"] == laptop.id
    (r,) = st["remotes"]
    assert r["name"] == "devbox" and r["state"] == "direct" and r["rtt_ms"] is not None and r["since_ms"] > 0
    link = read_json(laptop.state / "remotes/devbox/link.json")
    # the daemon wrote state/reason/since_ms and kept the Python side's fields
    assert (link["state"], link["reason"], link["id"], link["port"]) == ("direct", "", devbox.id, devbox.port)
    assert link["since_ms"] == r["since_ms"]


def test_tty_round_trip_with_resize(paired):
    laptop, _ = paired
    master, slave = os.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    before = termios.tcgetattr(slave)
    # the pty becomes the controlling terminal of a new session (SIGWINCH reaches the client), then force-link runs
    # in its foreground process group, as under a shell. The session leader waits for a line after force-link has
    # exited: on macOS (and the BSDs) the leader's exit revokes its controlling terminal, and the test's descriptor
    # of it then fails every ioctl (ENOTTY), so the mode is read while the session still stands.
    ctty = ("import fcntl, os, sys, termios\n"
            "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os.execv(sys.argv[1], sys.argv[1:])\n"
            "code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])\n"
            "os.write(1, b'client exited %d\\n' % code)\n"
            "sys.stdin.readline()\n"
            "sys.exit(code)\n")
    env = dict(laptop.env(), TERM="xterm-256color")
    proc = subprocess.Popen([sys.executable, "-c", ctty, *laptop.exec_argv("~/.local/bin/force", "tty", tty=True)],
                            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    buf = b""

    def until(text, timeout=15):
        nonlocal buf
        end = time.monotonic() + timeout
        while text.encode() not in buf:
            if time.monotonic() > end:
                raise AssertionError(f"no {text!r} in {buf!r}")
            os.set_blocking(master, False)
            try:
                buf += os.read(master, 4096)
            except BlockingIOError:
                time.sleep(0.05)
            except OSError:
                time.sleep(0.05)

    try:
        until("size:30 100")
        assert b"istty" in buf and b"term:xterm-256color" in buf
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        until("resized:40 120")
        os.write(master, b"hello\r")
        until("got:hello")
        until("client exited 5")
        # raw mode was undone on the way out
        after = termios.tcgetattr(slave)
        assert after[3] & termios.ICANON and after[3] & termios.ECHO
        assert after == before
        os.write(master, b"\n")
        # keep reading: on macOS a session leader's exit waits until its terminal's output (the echo) is read
        end = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < end:
            try:
                os.read(master, 4096)
            except OSError:
                time.sleep(0.05)
        assert proc.wait(timeout=1) == 5
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)              # the leader and force-link under it
            except OSError:
                pass
        os.close(master)                                         # nothing left to drain
        if proc.poll() is None:
            proc.wait(timeout=10)
        os.close(slave)
    assert not laptop.ssh_log.exists()


def test_not_allowed_is_refused_not_run(paired):
    laptop, devbox = paired
    marker = devbox.home / "ran"
    for argv in (["sh", "-c", f"touch {marker}"], ["tmux", "-L", "force", "-c", f"touch {marker}"],
                 ["/bin/sh", "-c", f"touch {marker}"]):
        p = subprocess.run(laptop.exec_argv(*argv), capture_output=True, text=True, env=laptop.env(), timeout=30)
        assert p.returncode == 255, (argv, p.stderr)
        assert "not_allowed" in p.stderr
    assert not marker.exists()
    assert not laptop.ssh_log.exists()


def test_unpaired_daemon_is_refused_and_falls_back_to_ssh(paired, world):
    laptop, devbox = paired
    stranger = world("stranger").start()
    stranger.pair_to(devbox)                                # it knows devbox's id, but devbox does not know it
    wait_for(lambda: stranger.link_state() == "ssh"
             and (read_json(stranger.state / "remotes/devbox/link.json") or {}).get("reason") == "refused",
             15, "the refused dial")
    wait_for(lambda: "refused unknown id" in devbox.log.read_text(), 10, "devbox's log line")
    p = subprocess.run(stranger.exec_argv("~/.local/bin/force", "roundtrip"), capture_output=True, text=True,
                       env=stranger.env(), timeout=30)
    assert p.returncode == 42 and "fake ssh" in p.stderr
    assert "~/.local/bin/force roundtrip" in stranger.ssh_log.read_text()


def test_a_changed_id_is_refused_until_paired_again(paired, world):
    laptop, devbox = paired
    # devbox's link.json on laptop now names another id (a reinstalled devbox): no direct link to the old key
    other = world("other").start()
    link = laptop.state / "remotes/devbox/link.json"
    data = read_json(link)
    data["id"] = other.id
    link.write_text(json.dumps(data))
    wait_for(lambda: laptop.link_state() == "ssh", 15, "ssh after the id changed")
    p = subprocess.run(laptop.exec_argv("~/.local/bin/force", "roundtrip"), capture_output=True, env=laptop.env(),
                       timeout=30)
    assert p.returncode == 42


def test_untrust_closes_the_link(paired):
    laptop, devbox = paired
    devbox.peers.write_text(json.dumps({"peers": []}))
    os.chmod(devbox.peers, 0o600)
    wait_for(lambda: laptop.link_state() == "ssh", 20, "ssh after untrust")


def test_falls_back_to_ssh_when_the_other_daemon_stops(paired):
    laptop, devbox = paired
    devbox.stop()
    wait_for(lambda: laptop.link_state() == "ssh", 20, "ssh after devbox stopped")
    p = subprocess.run(laptop.exec_argv("~/.local/bin/force", "roundtrip"), capture_output=True, text=True,
                       env=laptop.env(), timeout=30)
    assert p.returncode == 42 and "fake ssh" in p.stderr


def test_no_daemon_is_plain_ssh(world):
    laptop = world("laptop")
    laptop.hosts.write_text(json.dumps({"hosts": [{"name": "devbox", "ssh": "devbox"}]}))
    p = subprocess.run(laptop.exec_argv("~/.local/bin/force", "roundtrip"), capture_output=True, text=True,
                       env=laptop.env(), timeout=30)
    assert p.returncode == 42
    p = subprocess.run([str(laptop.link_bin), "status", "--state", str(laptop.state), "--json"],
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 1 and json.loads(p.stdout) == {"running": False, "error": "daemon not running"}


def test_second_daemon_on_the_same_state_refuses(world):
    laptop = world("laptop").start()
    p = subprocess.run([str(laptop.link_bin), "daemon", "--state", str(laptop.state), "--hosts", str(laptop.hosts),
                        "--peers", str(laptop.peers), "--port", "0"], env=laptop.env(), capture_output=True,
                       text=True, timeout=15)
    assert p.returncode == 1 and "another force-link daemon" in p.stderr
    assert laptop.proc.poll() is None
    st = os.stat(laptop.state / "link.sock")
    assert st.st_mode & 0o777 == 0o600
    assert os.stat(laptop.state / "link/key").st_mode & 0o777 == 0o600


def test_a_key_others_can_read_is_refused(world):
    laptop = world("laptop").start()
    laptop.stop()
    os.chmod(laptop.state / "link/key", 0o644)
    p = subprocess.run([str(laptop.link_bin), "daemon", "--state", str(laptop.state), "--hosts", str(laptop.hosts),
                        "--peers", str(laptop.peers), "--port", "0"], env=laptop.env(), capture_output=True,
                       text=True, timeout=15)
    assert p.returncode == 1 and "group or other" in p.stderr
