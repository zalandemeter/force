#!/usr/bin/env python3
"""Remake the landing page's screens with this checkout's sidebar, header, menu and Chuck's card, keeping each
screen's agent pane (a real Claude Code session, captured once; see website/README.md, "Remaking the screens").

    respin.py BIN_DIR [SITE_DIR]

BIN_DIR holds force-sidebar and force-header (crates/sidebar/target/release after `cargo build --release`);
SITE_DIR is website/ (default: this script's parent's parent). The demo sessions come from demo_snapshot.py. Each
screen is drawn in a tmux server of its own (FORCE_CAPTURE_SOCKET, default force-capture; never Force's own
servers) at the page's sizes, captured with `capture-pane -e`, and spliced into the screen files:

    cockpit-NAME.html   150x40: sidebar (columns 0-28) and header (rows 0-1 right of the border) replaced
    phone-list.html     46x27: the sidebar alone
    phone-NAME.html     46 wide: the header (rows 0-1) replaced
    menu-NAME.html      150x40: the Force menu (menu-split.tmux) over NAME, alone (an overlay)
    card-chuck.html     150x40: Chuck's card (force support --ui) alone (an overlay); phone-chuck.html: the card

Menus and the card are drawn by tmux itself, so they are captured through a client: a second tmux server whose
one pane is attached to the first. The capture machine's name becomes "devbox" (FORCE_CAPTURE_HOST).
"""
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ansi2html import CHAR_MAP, OTHER_ESC, SGR, Style   # noqa: E402
from demo_snapshot import NAMES, snapshot                 # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SOCK = os.environ.get("FORCE_CAPTURE_SOCKET", "force-capture")
HOST = os.environ.get("FORCE_CAPTURE_HOST") or socket.gethostname().split(".")[0]
PUA = re.compile("([\ue000-\uf8ff\U000f0000-\U000fffff])")
W, H, SB = 150, 40, 29          # the desktop screen; the sidebar's width
PW, PH = 46, 27                 # the phone screen


# --- grids: rows of [char, css] ------------------------------------------------------------------------------
def from_ansi(text: str, w: int, h: int) -> list[list[list[str]]]:
    text = OTHER_ESC.sub("", text).translate(CHAR_MAP)
    grid, st = [], Style()
    for line in text.split("\n")[:h]:
        row, pos = [], 0
        for m in SGR.finditer(line):
            row += [[ch, st.css()] for ch in line[pos:m.start()]]
            st.apply([int(x) if x else 0 for x in re.split("[;:]", m.group(1))] if m.group(1) else [0])
            pos = m.end()
        row += [[ch, st.css()] for ch in line[pos:]]
        grid.append((row + [[" ", ""] for _ in range(w)])[:w])
    return grid + [[[" ", ""] for _ in range(w)] for _ in range(h - len(grid))]


class _Cells(HTMLParser):
    """A screen file back into cells: each character with its outermost span's style (nf spans only wrap icons)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.stack = [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.stack.append(a.get("style") if "style" in a else (self.stack[-1] if self.stack else ""))

    def handle_endtag(self, tag):
        self.stack.pop()

    def handle_data(self, data):
        css = self.stack[-1] if self.stack else ""
        for ch in data:
            if ch == "\n":
                self.rows.append(self.row)
                self.row = []
            else:
                self.row.append([ch, css or ""])


def from_html(path: Path, w: int, h: int) -> list[list[list[str]]]:
    p = _Cells()
    p.feed(path.read_text(encoding="utf-8"))
    rows = p.rows + [p.row]
    return [(r + [[" ", ""] for _ in range(w)])[:w] for r in rows[:h]] + \
           [[[" ", ""] for _ in range(w)] for _ in range(h - len(rows))]


def to_html(grid) -> str:
    out = []
    for row in grid:
        parts, cur, buf = [], None, []

        def flush():
            if buf:
                t = PUA.sub(r'<span class="nf">\1</span>', html.escape("".join(buf), quote=False))
                parts.append(f'<span style="{cur}">{t}</span>' if cur else t)
        for ch, css in row:
            if css != cur:
                flush()
                cur, buf = css, []
            buf.append(ch)
        flush()
        out.append("".join(parts))
    while out and not re.sub(r"<[^>]+>", "", out[-1]).strip():
        out.pop()
    return "\n".join(out)


def host_as(g, w: int, name: str = "devbox"):
    """The machine's rule in the sidebar names the capture machine: show a neutral name, re-centred the way the
    sidebar centres it (model::icon_gap / label_x0: the rule over columns 1..w-1, a 1- or 2-cell icon gap)."""
    for row in g:
        text = "".join(c[0] for c in row[:w])
        if HOST not in text or "─" not in text:
            continue
        rule = row[1][1]
        icon = next(c for c in row[:w] if PUA.match(c[0]))
        label_st = row[text.index(HOST)][1]
        cells = w - 1
        gap = 2 if (cells - (3 + len(name)) - 2) % 2 == 0 else 1
        label = [[" ", label_st], icon[:]] + [[" ", label_st]] * gap + [[ch, label_st] for ch in name] + [[" ", label_st]]
        x0 = 1 + (cells - len(label)) // 2
        for x in range(1, w):
            row[x] = ["─", rule]
        for i, c in enumerate(label):
            row[x0 + i] = c[:]
    return g


# --- tmux -----------------------------------------------------------------------------------------------------
class Tmux:
    def __init__(self, sock: str, conf: str = ""):
        self.sock = sock
        self.conf = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
        self.conf.write("set -g status off\nset -g default-terminal tmux-256color\n"
                        "set -as terminal-overrides ',*:RGB'\nset -g pane-border-style 'fg=#3c3c3c'\n"
                        "set -g pane-active-border-style 'fg=#3c3c3c'\n" + conf)
        self.conf.close()

    def __call__(self, *args, check=True, wait=True) -> str:
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        if not wait:      # a command that returns only when its popup closes
            subprocess.Popen(["tmux", "-L", self.sock, "-f", self.conf.name, *args], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return ""
        r = subprocess.run(["tmux", "-L", self.sock, "-f", self.conf.name, *args], capture_output=True, text=True,
                           env=env)
        if check and r.returncode:
            raise SystemExit(f"tmux {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    def capture(self, target: str) -> str:
        return self("capture-pane", "-e", "-p", "-N", "-t", target)

    def kill(self):
        self("kill-server", check=False)
        os.unlink(self.conf.name)


def bars(bin_dir: Path, snap: Path, sock: str) -> tuple[str, str]:
    flags = f"--snapshot {snap} --glyphs rich --animations off"
    return (f"{bin_dir}/force-sidebar {flags} --force-bin /bin/true; sleep 999",
            f"{bin_dir}/force-header {flags} --share-dir {REPO}/share; sleep 999")


def main(argv):
    bin_dir = Path(argv[0]).resolve()
    site = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[1]
    work = Path(tempfile.mkdtemp(prefix="force-respin-"))
    snap = work / "snapshot.json"
    t = Tmux(SOCK)
    stop, shown = threading.Event(), {"name": "api"}

    def fresh():
        """Keep the snapshot young: the sidebar and header call one older than 5 s a stopped daemon."""
        while not stop.is_set():
            tmp = snap.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot(shown["name"])))
            os.replace(tmp, snap)
            stop.wait(.5)
    snap.write_text(json.dumps(snapshot("api")))
    threading.Thread(target=fresh, daemon=True).start()
    side, head = bars(bin_dir, snap, SOCK)
    try:
        # desktop: the sidebar | the header over the agent pane, in Force's split layout
        for name in NAMES:
            shown["name"] = name
            time.sleep(.6)
            # every pane starts at its final size (a bar drawn at another size first may keep stale cells)
            agent = t("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, "-x", str(W), "-y", str(H),
                      "sleep 999").strip()
            sbp = t("split-window", "-P", "-F", "#{pane_id}", "-h", "-b", "-l", str(SB), "-t", agent, side).strip()
            hdp = t("split-window", "-P", "-F", "#{pane_id}", "-v", "-b", "-l", "2", "-t", agent, head).strip()
            time.sleep(2.5)
            sb = from_ansi(t.capture(sbp), SB, H)
            hd = from_ansi(t.capture(hdp), W - SB - 1, 2)
            t("kill-session", "-t", name)
            path = site / f"cockpit-{name}.html"
            g = from_html(path, W, H)
            for r in range(H):
                g[r][:SB] = sb[r]
            for r in range(2):
                g[r][SB + 1:] = hd[r]
            path.write_text(to_html(host_as(g, SB)), encoding="utf-8")
            # phone: the header over the agent, 46 wide
            agent = t("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, "-x", str(PW), "-y", str(PH + 1),
                      "sleep 999").strip()
            hdp = t("split-window", "-P", "-F", "#{pane_id}", "-v", "-b", "-l", "2", "-t", agent, head).strip()
            time.sleep(2.5)
            hd = from_ansi(t.capture(hdp), PW, 2)
            t("kill-session", "-t", name)
            path = site / f"phone-{name}.html"
            g = from_html(path, PW, PH)
            g[:2] = hd
            path.write_text(to_html(g), encoding="utf-8")
        # the phone's session list: the sidebar alone, nothing selected
        shown["name"] = ""
        time.sleep(.6)
        t("new-session", "-d", "-s", "list", "-x", str(PW), "-y", str(PH), side)
        time.sleep(2.5)
        g = from_ansi(t.capture("list:0.0"), PW, PH)
        t("kill-session", "-t", "list")
        (site / "phone-list.html").write_text(to_html(host_as(g, PW)), encoding="utf-8")
    finally:
        stop.set()
        t.kill()
    overlays(site)
    shutil.rmtree(work, ignore_errors=True)


def overlays(site: Path):
    """The menu over each session, and Chuck's card, drawn by tmux for a client (a pane of a second server)."""
    styles = "".join(line + "\n" for line in (REPO / "share" / "tmux-ui.conf").read_text().splitlines()
                     if re.match(r"set -g (menu|popup)-", line))
    inner = Tmux(SOCK, styles + f"set -g @force_glyphs rich\nset -g @force_app {REPO}\n"
                 f"bind -n C-Space source-file -F '#{{@force_app}}/share/menu-split.tmux'\n")
    outer = Tmux(SOCK + "-client")
    from force.menus import CHUCK_H, CHUCK_STYLE, CHUCK_W   # noqa: E402  (the popup as the menu opens it)
    card = (f"cd {REPO} && FORCE_GLYPHS=rich python3 -c 'from force.cards import card_text; "
            f"print(\"\\x1b[?25l\" + card_text(\"claude\", True), end=\"\", flush=True)'; sleep 999")
    try:
        for name in NAMES + ["chuck"]:
            inner("new-session", "-d", "-s", name, "-x", str(W), "-y", str(H), "sleep 999")
            inner("set", "-t", name, "@force_session", "api" if name == "chuck" else name)
            outer("new-session", "-d", "-s", "c", "-x", str(W), "-y", str(H),
                  f"env -u TMUX tmux -L {SOCK} -f {inner.conf.name} attach -t {name}")
            time.sleep(1.5)
            if name == "chuck":
                client = inner("list-clients", "-F", "#{client_name}").split()[0]
                inner("display-popup", "-c", client, *shlex_split(CHUCK_STYLE), "-E", "-w", str(CHUCK_W),
                      "-h", str(CHUCK_H), card, wait=False)
            else:
                outer("send-keys", "-t", "c", "C-Space")
            time.sleep(2)
            g = from_ansi(outer.capture("c"), W, H)
            outer("kill-session", "-t", "c")
            inner("kill-session", "-t", name)
            # the overlay alone: the box's bounding cells, nothing else
            box = [(r, c) for r in range(H) for c in range(W) if g[r][c][0] in "╭╮╰╯"]
            r0, r1 = min(r for r, _ in box), max(r for r, _ in box)
            c0, c1 = min(c for _, c in box), max(c for _, c in box)
            out = [[[" ", ""] if not (r0 <= r <= r1 and c0 <= c <= c1) else g[r][c] for c in range(W)] for r in range(H)]
            dst = site / ("card-chuck.html" if name == "chuck" else f"menu-{name}.html")
            dst.write_text(to_html(out), encoding="utf-8")
            if name == "chuck":     # the phone shows the card alone
                (site / "phone-chuck.html").write_text(to_html([row[c0:c1 + 1] for row in g[r0:r1 + 1]]),
                                                       encoding="utf-8")
    finally:
        outer.kill()
        inner.kill()


def shlex_split(s: str) -> list[str]:
    import shlex
    return shlex.split(s)


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    main(sys.argv[1:])
