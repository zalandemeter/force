"""Shift+Enter / Ctrl+Enter (share/tmux.conf, share/tmux-ui.conf) and the help popup's size."""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# raw stdin bytes, hex per read, into a file (the app in the pane)
DUMP = """import os, sys, termios, tty, select, time
fd = 0; tty.setraw(fd); end = time.time() + 20
with open(sys.argv[1], "w") as f:
    while time.time() < end:
        if select.select([fd], [], [], 0.1)[0]:
            f.write(os.read(fd, 64).hex() + "\\n"); f.flush()
"""


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


@pytest.mark.parametrize("conf", ["tmux.conf", "tmux-ui.conf"])
def test_shift_and_ctrl_enter_become_a_new_line(tmp_path, conf):
    """A terminal's CSI u Shift+Enter / Ctrl+Enter (what Force's kitty and Konsole profiles send) reach the
    app as Ctrl+J; a plain Enter stays Enter, Alt+Enter stays ESC Enter."""
    dump, out = tmp_path / "dump.py", tmp_path / "keys.txt"
    dump.write_text(DUMP)
    inner, outer = f"forcetest-keys-in-{os.getpid()}", f"forcetest-keys-out-{os.getpid()}"
    ti, to = ["tmux", "-L", inner], ["tmux", "-L", outer]
    # @force_app normally points at the live app dir; in the test point it at the repo so tmux.conf's parse-time
    # source-files of share/*.tmux resolve without a deployed ~/.local/share/force/app.
    boot = tmp_path / "boot.conf"
    boot.write_text(f"set -g @force_app {ROOT}\nsource {ROOT / 'share' / conf}\n")
    try:
        subprocess.run(ti + ["-f", str(boot), "new-session", "-d", "-s", "k", "-x", "80", "-y", "10",
                             f"{sys.executable} {dump} {out}"], check=True, capture_output=True, timeout=30)
        # the terminal: a client of the inner server running in a pane of the outer one
        subprocess.run(to + ["-f", "/dev/null", "new-session", "-d", "-s", "t", "-x", "80", "-y", "12",
                             f"TMUX= tmux -L {inner} attach -t k"], check=True, capture_output=True, timeout=30)
        assert _wait(lambda: subprocess.run(ti + ["list-clients"], capture_output=True, text=True).stdout.strip())
        time.sleep(0.3)
        for keys in ("1b5b31333b3275", "1b5b31333b3575", "0d", "1b0d"):    # S-Enter, C-Enter, Enter, M-Enter
            subprocess.run(to + ["send-keys", "-t", "t:", "-H", *re.findall("..", keys)], check=True, timeout=10)
            time.sleep(0.3)
        assert _wait(lambda: out.exists() and len(out.read_text().split()) >= 4)
        assert out.read_text().split() == ["0a", "0a", "0d", "1b0d"]
    finally:
        subprocess.run(to + ["kill-server"], capture_output=True)
        subprocess.run(ti + ["kill-server"], capture_output=True)


def test_menu_files_are_generated_from_the_table():
    from force import menus
    assert menus.main(["--check"]) == 0, "run python -m force.menus"


def test_update_menu_item_opens_the_card():
    from force import menus
    item = next(it for it in menus.ITEMS if it and it[2] == "update")
    assert (item[0], item[1]) == ("U", "Update Force…")
    assert "update --ui" in menus.classic()[1]["update"]
    split = menus.split()[1]["update"]
    assert "update --ui" in split and "FORCE_HOST" in split
    for f in ("menu-classic.tmux", "menu-split.tmux"):
        assert "update --ui" in (ROOT / "share" / f).read_text()


def test_menu_items_never_take_arrows_and_have_unique_keys():
    from force import menus
    keys = [it[0] for it in menus.ITEMS if it]
    assert len(keys) == len(set(keys))
    assert not {"Up", "Down", "Left", "Right", "Enter", "Escape"} & set(keys)
    for mode in ("classic", "split"):
        _, cmds = getattr(menus, mode)()
        assert {it[2] for it in menus.ITEMS if it} <= set(cmds), mode


def test_no_prefix_and_alt_page_keys_switch():
    for f in ("tmux.conf", "tmux-ui.conf"):
        conf = (ROOT / "share" / f).read_text()
        assert "set -g prefix None" in conf and "set -g prefix2 None" in conf, f
        assert not re.search(r"^bind (?!-n |-T )", conf, re.M), f"{f}: a prefix binding is left"
        assert re.search(r"^bind -n C-Space source-file -F .*menu-", conf, re.M), f
        assert re.search(r"^bind -n M-NPage ", conf, re.M) and re.search(r"^bind -n M-PPage ", conf, re.M), f


def test_help_fits_its_popup():
    lines = (ROOT / "share" / "help.ansi").read_text().rstrip("\n").split("\n")
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in lines]
    heights = set()
    for f in ("menu-classic.tmux", "menu-split.tmux"):
        for w, h in re.findall(r"-w (\d+) -h (\d+) [\\\"']*[^\s]*show-help\.sh", (ROOT / "share" / f).read_text()):
            assert max(len(p) for p in plain) <= int(w) - 2, f
            heights.add(int(h))
    assert len(heights) == 1 and len(lines) <= heights.pop() - 2


def test_classic_layout_explains_the_host_picker():
    from force import menus
    assert "force ui split" in menus.classic()[1]["host"]


def test_menus_stay_open_after_a_click():
    """Menus opened by a click (controls button, host chip) stay open when the button is released (-O): a click
    toggles them and their items are clicked, not dragged to."""
    calls = []
    for f in ("share/tmux.conf", "share/tmux-ui.conf", "share/menu-classic.tmux", "share/menu-split.tmux", "force/cli.py"):
        for line in (ROOT / f).read_text().splitlines():
            if "display-menu" in line and not line.lstrip().startswith("#"):
                calls.append((f, line))
    assert len(calls) >= 4
    for f, line in calls:
        assert re.search(r"display-menu\"?,? \"?-O\b", line), (f, line)


def _sgr(button: int, x: int, y: int, release: bool = False) -> list[str]:
    return [f"{b:02x}" for b in f"\x1b[<{button};{x + 1};{y + 1}{'m' if release else 'M'}".encode()]


def test_a_shell_selection_stays_until_a_right_click_copies_it(tmp_path):
    """A shell takes no mouse, so a drag is tmux's copy mode: the selection stays after the release, a right click
    copies it and returns to the shell, a typed key returns and reaches the shell, a left click just returns."""
    inner, outer = f"forcetest-sel-in-{os.getpid()}", f"forcetest-sel-out-{os.getpid()}"
    ti, to = ["tmux", "-L", inner], ["tmux", "-L", outer]

    def run(*a):
        return subprocess.run(ti + list(a), capture_output=True, text=True, timeout=10).stdout.strip()

    def mouse(*events):
        for e in events:
            subprocess.run(to + ["send-keys", "-t", "t:", "-H", *e], check=True, timeout=10)
            time.sleep(0.1)

    def drag():
        mouse(_sgr(0, 0, 0), _sgr(32, 4, 0), _sgr(32, 9, 0), _sgr(0, 9, 0, release=True))

    try:
        subprocess.run(ti + ["-f", str(ROOT / "share" / "tmux.conf"), "new-session", "-d", "-s", "k", "-x", "80",
                             "-y", "10", "env PS1='$ ' bash --norc --noprofile"], check=True, capture_output=True,
                       timeout=30)
        run("set", "-g", "@force_app", str(ROOT))
        run("source-file", str(ROOT / "share" / "copy-mode.tmux"))
        run("send-keys", "-t", "k:", "clear; printf 'hello-world\\n'", "Enter")
        assert _wait(lambda: run("capture-pane", "-p", "-t", "k:").startswith("hello-world\n$"))
        subprocess.run(to + ["-f", "/dev/null", "new-session", "-d", "-s", "t", "-x", "80", "-y", "12",
                             f"TMUX= tmux -L {inner} attach -t k"], check=True, capture_output=True, timeout=30)
        assert _wait(lambda: run("list-clients"))
        time.sleep(0.3)
        mode = lambda: run("display", "-p", "-t", "k:", "#{pane_in_mode}#{selection_present}")

        drag()
        assert _wait(lambda: mode() == "11"), mode()
        time.sleep(0.4)
        assert mode() == "11", "the selection stays after the release"
        mouse(_sgr(2, 5, 0), _sgr(2, 5, 0, release=True))          # right click: copy, back to the shell
        assert _wait(lambda: mode().startswith("0"))
        assert run("show-buffer").startswith("hello-wor"), run("show-buffer")

        run("delete-buffer")
        drag()
        assert _wait(lambda: mode() == "11")
        subprocess.run(to + ["send-keys", "-t", "t:", "x"], check=True, timeout=10)   # typing: back + typed
        assert _wait(lambda: mode().startswith("0"))
        assert _wait(lambda: run("capture-pane", "-p", "-t", "k:").rstrip().endswith("$ x"))

        drag()
        assert _wait(lambda: mode() == "11")
        mouse(_sgr(0, 20, 5), _sgr(0, 20, 5, release=True))         # left click: back, nothing copied
        assert _wait(lambda: mode().startswith("0"))
        assert run("list-buffers") == ""
    finally:
        subprocess.run(to + ["kill-server"], capture_output=True)
        subprocess.run(ti + ["kill-server"], capture_output=True)


def test_help_card_is_generated():
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "force.cards", "--check"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
