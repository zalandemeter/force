"""Menus opened by a click (SPEC §41, §44): tmux's MouseDown1Pane binding opens the Force menu (share/menu-*.tmux)
or the host picker (share/menu.sh + source-file) itself when the click lands on the rectangle a pane publishes, so
the menu has the click: it stays open on release, its items take clicks, a click on the button again closes it.
The keyboard opens the same menu with Ctrl+Space."""
import os
import subprocess
import sys
import time
from pathlib import Path

from force import cli

ROOT = Path(__file__).resolve().parent.parent


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _sgr(button: int, x: int, y: int, release: bool = False) -> list[str]:
    """An SGR mouse report (0-based cell x/y) as hex bytes for send-keys -H."""
    return [f"{b:02x}" for b in f"\x1b[<{button};{x + 1};{y + 1}{'m' if release else 'M'}".encode()]


class Nested:
    """A ui server with the real tmux-ui.conf, shown by a client running in a pane of an outer server."""

    def __init__(self, tmp_path):
        pid = os.getpid()
        self.inner, self.outer = ["tmux", "-L", f"forcetest-menu-in-{pid}"], ["tmux", "-L", f"forcetest-menu-out-{pid}"]
        subprocess.run(self.inner + ["-f", str(ROOT / "share" / "tmux-ui.conf"), "new-session", "-d", "-s", "u",
                                     "-x", "100", "-y", "38", "sleep 120"], check=True, capture_output=True, timeout=30)
        self.run_in("set", "-g", "@force_app", str(ROOT))
        self.pane = self.run_in("display", "-p", "-t", "u:", "#{pane_id}")
        subprocess.run(self.outer + ["-f", "/dev/null", "new-session", "-d", "-s", "t", "-x", "100", "-y", "40",
                                     f"TMUX= {' '.join(self.inner)} attach -t u"],
                       check=True, capture_output=True, timeout=30)
        assert _wait(lambda: self.run_in("list-clients"))
        time.sleep(0.3)

    def run_in(self, *args) -> str:
        return subprocess.run(self.inner + list(args), capture_output=True, text=True, timeout=10).stdout.strip()

    def screen(self) -> str:
        return subprocess.run(self.outer + ["capture-pane", "-p", "-t", "t:"], capture_output=True, text=True,
                              timeout=10).stdout

    def click(self, x: int, y: int):
        for keys in (_sgr(35, x, y), _sgr(0, x, y), _sgr(0, x, y, release=True)):   # move there, press, release
            subprocess.run(self.outer + ["send-keys", "-t", "t:", "-H", *keys], check=True, timeout=10)
            time.sleep(0.15)

    def close(self):
        subprocess.run(self.outer + ["kill-server"], capture_output=True)
        subprocess.run(self.inner + ["kill-server"], capture_output=True)


def test_a_click_on_the_published_button_toggles_a_clickable_menu(tmp_path):
    t = Nested(tmp_path)
    try:
        # the pane publishes a "controls" button at columns 5..9 of rows 0..1
        for k, v in (("kind", "controls"), ("x0", "5"), ("x1", "10"), ("y0", "0"), ("y1", "2")):
            t.run_in("set", "-p", "-t", t.pane, f"@force_menu_{k}", v)
        t.click(30, 10)                                       # not on the button: no menu
        time.sleep(0.5)
        assert "Force ·" not in t.screen()
        t.click(6, 1)                                         # on it: the menu opens and stays after the release
        assert _wait(lambda: "Force ·" in t.screen())
        time.sleep(0.5)
        assert "Force ·" in t.screen()
        t.click(6, 1)                                         # the button again: closed, not reopened
        assert _wait(lambda: "Force ·" not in t.screen())
        time.sleep(0.6)
        assert "Force ·" not in t.screen()
        t.click(9, 0)                                         # open again, click an item: it runs
        assert _wait(lambda: "Force ·" in t.screen())
        row = next(i for i, line in enumerate(t.screen().splitlines()) if "Keys…" in line)
        col = t.screen().splitlines()[row].index("Keys…")
        t.click(col + 2, row)
        assert _wait(lambda: "Force keys" in t.screen())
        # nothing published (kind empty): every click goes to the pane
        t.run_in("send-keys", "-t", t.pane, "q")
        t.run_in("set", "-p", "-t", t.pane, "@force_menu_kind", "")
        _wait(lambda: "Force keys" not in t.screen())
        t.click(6, 1)
        time.sleep(0.6)
        assert "Force ·" not in t.screen()
    finally:
        t.close()


def test_hosts_menu_writes_its_command_for_a_click(env, monkeypatch, tmp_path):
    """With FORCE_MENU_OUT (share/menu.sh) the host picker writes its display-menu command to that file instead of
    showing it; tmux parses the file back to the same words."""
    cockpit = env.make_cockpit()
    real = cockpit.ui_tmux.run
    monkeypatch.setattr(cockpit.ui_tmux, "run", lambda *a: "" if a[0] == "display" else real(*a))
    out = tmp_path / "menu.tmux"
    monkeypatch.setenv("FORCE_MENU_OUT", str(out))
    assert cli.hosts_menu_command(cockpit, "/dev/pts/9", "@3") == 0
    text = out.read_text()
    assert text.startswith('display-menu "-O" "-c" "/dev/pts/9"') and "ui-host" in text
    sock = ["tmux", "-L", f"forcetest-menu-parse-{os.getpid()}"]
    try:
        r = subprocess.run(sock + ["-f", "/dev/null", "start-server", ";", "source-file", "-n", str(out)],
                           capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stderr
    finally:
        subprocess.run(sock + ["kill-server"], capture_output=True)


def test_tmux_quote_round_trips_through_the_tmux_parser(tmp_path):
    word = 'run-shell -b "x \'a b\' $HOME \\\\ #{y}" ; "q"'
    f = tmp_path / "w.tmux"
    f.write_text(f"set-buffer -b w {cli._tmux_quote(word)}\n")
    sock = ["tmux", "-L", f"forcetest-menu-quote-{os.getpid()}"]
    try:
        subprocess.run(sock + ["-f", "/dev/null", "new-session", "-d", "sleep 30"], check=True, timeout=10)
        subprocess.run(sock + ["source-file", str(f)], check=True, timeout=10)
        # read back raw (show and display escape $ in what they print)
        got = subprocess.run(sock + ["save-buffer", "-b", "w", "-"], capture_output=True, text=True, timeout=10).stdout
        assert got == word
    finally:
        subprocess.run(sock + ["kill-server"], capture_output=True)


def test_ctrl_space_opens_the_menu_and_the_next_letter_picks_an_item(tmp_path):
    """No prefix key: Ctrl+Space opens the Force menu at once, so a letter typed right after it picks an item
    (here k = Keys) instead of reaching the session. Ctrl+\\ does the same; Esc closes the menu."""
    t = Nested(tmp_path)
    try:
        assert t.run_in("show", "-gv", "prefix") == "None" and t.run_in("show", "-gv", "prefix2") == "None"
        assert t.run_in("list-keys", "-T", "prefix") == ""
        for key in ("00", "1c"):                               # Ctrl+Space (NUL), Ctrl+\
            subprocess.run(t.outer + ["send-keys", "-t", "t:", "-H", key], check=True, timeout=10)
            time.sleep(0.005)
            subprocess.run(t.outer + ["send-keys", "-t", "t:", "k"], check=True, timeout=10)
            assert _wait(lambda: "Force keys" in t.screen())
            subprocess.run(t.outer + ["send-keys", "-t", "t:", "q"], check=True, timeout=10)
            assert _wait(lambda: "Force keys" not in t.screen())
        subprocess.run(t.outer + ["send-keys", "-t", "t:", "-H", "00"], check=True, timeout=10)
        assert _wait(lambda: "Force ·" in t.screen() and "Jump to session" in t.screen())
        subprocess.run(t.outer + ["send-keys", "-t", "t:", "Escape"], check=True, timeout=10)
        assert _wait(lambda: "Force ·" not in t.screen())
    finally:
        t.close()


def test_a_short_terminal_gets_the_core_menu(tmp_path):
    t = Nested(tmp_path)
    try:
        subprocess.run(t.outer + ["resize-window", "-t", "t:", "-y", "18"], check=True, timeout=10)
        time.sleep(0.4)
        subprocess.run(t.outer + ["send-keys", "-t", "t:", "-H", "00"], check=True, timeout=10)
        assert _wait(lambda: "Jump to session" in t.screen())
        screen = t.screen()
        assert "Rename" in screen and "Move up in the list" not in screen
    finally:
        t.close()
