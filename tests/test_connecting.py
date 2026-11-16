"""A ui window opening a remote (SPEC §40, connecting): the spinner in the main pane and the remote side's
report handshake that keeps it off the session."""
import io
import threading
import time

from force import cli


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
