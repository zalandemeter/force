"""A ui window opening a remote (SPEC §40, connecting): the spinner in the main pane and the remote side's
report handshake that keeps it off the session; after a dropped connection, a clean and quiet pane."""
import io
import os
import pty
import select
import shlex
import subprocess
import sys
import termios
import threading
import time
import tty
import types
from pathlib import Path

import pytest

from force import cli


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class _Out(io.StringIO):
    def fileno(self):
        raise OSError("not a terminal")


def test_frame_centres_the_spinner_and_the_text():
    c = cli.Connecting("neumann", out=_Out(), start=False)
    f = c.frame(0, 80, 24, 0.0)
    assert f.startswith("\x1b[?25l\x1b[2J"), "cursor hidden, pane cleared"
    assert "\x1b[12;" in f, "middle row"
    x = int(f.split("\x1b[12;")[1].split("H")[0])
    assert x == (80 - len("connecting to neumann") - 2) // 2 + 1
    assert "⠋" in f and "connecting to neumann" in f
    assert "⠙" in c.frame(1, 80, 24, 0.0), "the next frame turns"
    assert " s" not in f
    assert "· 12 s" in c.frame(0, 80, 24, 12.4), "after 10 s, how long it has been"


def test_reconnecting_and_a_tiny_pane():
    c = cli.Connecting("brian", again=True, out=_Out(), start=False)
    f = c.frame(3, 5, 1, 0.0)
    assert "reconnecting to brian" in f
    assert "\x1b[1;1H" in f, "never off the pane"


def test_draws_until_stopped_and_stop_clears_once():
    out = _Out()
    c = cli.Connecting("neumann", out=out)
    time.sleep(0.25)
    c.stop()
    drawn = out.getvalue()
    assert drawn.count("connecting to neumann") >= 2
    assert drawn.endswith("\x1b[2J\x1b[H\x1b[?25h"), "a clear pane, cursor back"
    c.stop()
    time.sleep(0.15)
    assert out.getvalue() == drawn, "nothing after stop, stop is idempotent"


def test_report_waits_until_the_other_side_has_read_it(tmp_path):
    path = tmp_path / "nested" / "id1"
    done = []
    t = threading.Thread(target=lambda: done.append(cli._report_tty(path, ttyname=lambda fd: "/dev/pts/7")))
    t.start()
    time.sleep(0.2)
    assert path.read_text() == "/dev/pts/7"
    assert not done, "still waiting: the spinner is drawing"
    path.unlink()                                   # the ui side read it
    t.join(2)
    assert done == [True]


def test_report_gives_up_waiting(tmp_path):
    path = tmp_path / "id2"
    t0 = time.monotonic()
    assert cli._report_tty(path, ttyname=lambda fd: "/dev/pts/7", wait=0.2) is True
    assert time.monotonic() - t0 < 1


def test_report_without_a_tty(tmp_path):
    def no_tty(fd):
        raise OSError("not a tty")
    path = tmp_path / "id3"
    assert cli._report_tty(path, ttyname=no_tty) is False
    assert not path.exists()


# --- a dropped connection (SPEC §40): the pane's terminal modes, echo and pending input -----------------------

def test_quiet_input_stops_echo_and_drops_what_arrived():
    master, slave = pty.openpty()
    try:
        os.write(master, b"junk\n")
        time.sleep(0.05)
        attrs = cli._quiet_input(slave)
        assert attrs is not None
        lflag = termios.tcgetattr(slave)[3]
        assert not lflag & termios.ECHO, "no echo"
        assert lflag & termios.ICANON, "otherwise as it was"
        assert select.select([slave], [], [], 0.1)[0] == [], "what arrived before is dropped"
        # a killed ssh left its raw mode: the same settings again
        tty.setraw(slave)
        assert cli._quiet_input(slave, attrs) == attrs
        lflag = termios.tcgetattr(slave)[3]
        assert lflag & termios.ICANON and not lflag & termios.ECHO
    finally:
        os.close(master)
        os.close(slave)


def test_quiet_input_without_a_terminal(tmp_path):
    with open(tmp_path / "f", "w") as f:
        assert cli._quiet_input(f.fileno()) is None


def test_remote_side_quiets_input_around_its_report(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_quiet_input", lambda fd=0, attrs=None: calls.append(("quiet", attrs)) or ["attrs"])
    monkeypatch.setattr(cli, "_report_tty", lambda path: calls.append(("report", path.name)))

    class Attached(Exception):
        pass

    def has_session(name):
        raise Attached

    cockpit = types.SimpleNamespace(paths=types.SimpleNamespace(state=tmp_path),
                                    tmux=types.SimpleNamespace(has_session=has_session))
    with pytest.raises(Attached):
        cli.nested_command(cockpit, "s1", None, "id9")
    assert calls == [("quiet", None), ("report", "id9"), ("quiet", ["attrs"])], "flushed again before attaching"


_FAKE_SSH = r'''
import os, select, sys, time
out = sys.argv[1]
if " nested " not in " ".join(sys.argv[2:]) + " ":
    sys.exit(1)                                  # the report is never there: the connection never completes
n = len([f for f in os.listdir(out) if f.startswith("run")]) + 1
got = b""
if select.select([0], [], [], 0.3)[0]:
    got = os.read(0, 4096)
open(os.path.join(out, f"run{n}"), "wb").write(got)
# what a remote tmux client turns on in its terminal: the alternate screen, mouse tracking, focus reports,
# bracketed paste, application cursor keys and keypad
sys.stdout.write("\x1b[?1049h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?1004h\x1b[?2004h\x1b[?1h\x1b=")
sys.stdout.flush()
if n == 1:
    time.sleep(0.5)
    sys.exit(255)                                # the link drops: the client never turns them off
time.sleep(60)
'''


def test_a_dropped_connection_leaves_the_pane_clean_and_quiet(env, tmp_path):
    """The main pane runs the real `force nested s1 --remote devbox`, ssh replaced. The first connection turns the
    remote client's modes on and drops. Then the pane is back to plain modes (no clicks or wheel turns written into
    it as text), does not echo, and what is typed while it reconnects never reaches the next connection."""
    repo = Path(cli.__file__).parent.parent
    bindir, out = tmp_path / "fakebin", tmp_path / "runs"
    bindir.mkdir()
    out.mkdir()
    (bindir / "fake_ssh.py").write_text(_FAKE_SSH)
    (bindir / "ssh").write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {bindir / 'fake_ssh.py'} {out} \"$@\"\n")
    (bindir / "ssh").chmod(0o755)
    env.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    (env.paths.config_file.parent / "hosts.json").write_text(
        '{"hosts": [{"name": "devbox", "ssh": "devbox", "enabled": true}]}')
    keep = {k: v for k, v in os.environ.items() if k.startswith("FORCE_")}
    keep.update(PATH=f"{bindir}:{os.environ['PATH']}", PYTHONPATH=str(repo))
    cmd = shlex.join(["env", *[f"{k}={v}" for k, v in keep.items()],
                      sys.executable, "-P", "-m", "force.cli", "nested", "s1", "--remote", "devbox"])
    ui = ["tmux", "-L", env.paths.ui_socket]
    subprocess.run([*ui, "-f", str(env.tmp / "tmux-ui.conf"), "new", "-d", "-s", "ui", "-x", "80", "-y", "24", cmd],
                   check=True)

    def tm(*a):
        return subprocess.run([*ui, *a], capture_output=True, text=True, timeout=10).stdout.strip()

    flags = "#{mouse_any_flag}#{mouse_button_flag}#{mouse_standard_flag}#{mouse_sgr_flag}#{alternate_on}#{keypad_flag}#{keypad_cursor_flag}"
    ok = wait_for(lambda: (out / "run1").exists() and tm("display", "-p", "-t", "ui:", flags) == "1001111")
    assert ok, (os.listdir(out), tm("display", "-p", "-t", "ui:", flags), tm("capture-pane", "-p", "-t", "ui:"))
    tm("set-option", "-w", "-t", "ui:", "@force_nested", "/dev/pts/999")   # as if it had reported
    assert wait_for(lambda: "connection to devbox ended" in tm("capture-pane", "-p", "-t", "ui:"))
    assert tm("display", "-p", "-t", "ui:", flags) == "0000000", "the remote client's modes are off again"
    assert tm("show", "-wqv", "-t", "ui:", "@force_nested") == "", "no nested client until the next one reports"
    pane_tty = tm("display", "-p", "-t", "ui:", "#{pane_tty}")
    assert "-echo" in subprocess.run(["stty", "-F", pane_tty, "-a"], capture_output=True, text=True).stdout.split()
    tm("send-keys", "-t", "ui:", "-l", "junk")
    tm("send-keys", "-t", "ui:", "Enter")
    time.sleep(0.2)
    assert "junk" not in tm("capture-pane", "-p", "-t", "ui:"), "not echoed"
    assert wait_for(lambda: (out / "run2").exists(), timeout=15)
    assert (out / "run2").read_bytes() == b"", "typed while reconnecting: dropped, not sent to the remote"
