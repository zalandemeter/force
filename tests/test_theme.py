"""Force's tmux-painted theme (SPEC §45): the exact palette lines in both conf files, and that tmux loads them."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PALETTE = ["#000000", "#cc0403", "#19cb00", "#cecb00", "#0d73cc", "#cb1ed1", "#0dcdcd", "#dddddd",
           "#767676", "#f2201f", "#23fd00", "#fffd00", "#1a8fff", "#fd28ff", "#14ffff", "#ffffff"]


def test_theme_block_in_both_conf_files():
    for f in ("tmux.conf", "tmux-ui.conf"):
        conf = (ROOT / "share" / f).read_text()
        assert 'set -g window-style "fg=#d0d0d0,bg=#262626"' in conf, f
        assert 'set -g window-active-style "fg=#d0d0d0,bg=#262626"' in conf, f
        assert 'set -g cursor-colour "#d97757"' in conf, f
        for i, col in enumerate(PALETTE):
            assert f'set -g pane-colours[{i}] "{col}"' in conf, f


def test_theme_applies_in_a_real_tmux():
    """A throwaway server built from share/tmux.conf paints the theme."""
    name = f"forcetest-theme-{os.getpid()}"
    tm = ["tmux", "-L", name]

    def gv(k):
        return subprocess.run(tm + ["show", "-gv", k], capture_output=True, text=True, timeout=10).stdout.strip()

    try:
        subprocess.run(tm + ["-f", str(ROOT / "share" / "tmux.conf"), "new-session", "-d", "-s", "t"],
                     check=True, capture_output=True, timeout=30)
        assert gv("window-style") == "fg=#d0d0d0,bg=#262626"
        assert gv("window-active-style") == "fg=#d0d0d0,bg=#262626"
        assert gv("cursor-colour") == "#d97757"
        assert gv("pane-colours[1]") == "#cc0403"
        assert gv("pane-colours[15]") == "#ffffff"
    finally:
        subprocess.run(tm + ["kill-server"], capture_output=True)
        try:
            (Path(f"/tmp/tmux-{os.getuid()}") / name).unlink()
        except OSError:
            pass


@pytest.mark.parametrize("conf", ["tmux.conf", "tmux-ui.conf"])
def test_borders_paint_the_theme_background(conf):
    """Border cells are not pane cells, so window-style does not reach them: without a bg they show the
    terminal's own background (Ubuntu's aubergine GNOME Terminal drew purple lines between the panes). The pane
    borders are drawn in the shell colour #212121 of the sidebar and header (fg too: no line, one frame), and menus
    and popups sit on that colour with the cards' #4e4e4e rule, so they stand off the agent panes behind them."""
    lines = [ln.split("#  ", 1)[0].split("   #", 1)[0].rstrip() for ln in (ROOT / "share" / conf).read_text().splitlines()]
    for line in ('set -g pane-border-style "fg=#212121,bg=#212121"',
                 'set -g pane-active-border-style "fg=#212121,bg=#212121"',
                 'set -g popup-style "bg=#212121"',
                 'set -g popup-border-style "fg=#4e4e4e,bg=#212121"',
                 'set -g menu-style "bg=#212121,fg=#c6c6c6"',
                 'set -g menu-border-style "fg=#4e4e4e,bg=#212121"'):
        assert line in lines, line
