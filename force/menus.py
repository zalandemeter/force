"""Generated tmux key files (SPEC §44): the Force menu and the copy-mode keys.

The Force menu: every action behind one key, Ctrl+Space (or Ctrl+\\), and the header's controls button.

The menu is plain tmux config, `share/menu-classic.tmux` and `share/menu-split.tmux`, which the bindings load
with `source-file`. Then tmux opens it before it reads the next key, so a letter typed right after Ctrl+Space
picks an item and never lands in the session (a menu built by a script opened too late for that). Those two
files are generated from the table below: `python -m force.menus` rewrites them (and share/copy-mode.tmux),
`--check` fails when they are out of date (tests/test_keys.py runs it).

Every item acts on the session the window shows; the menu's title names it. A short terminal gets fewer items:
the full menu needs FULL rows, the core one (items marked core) fewer; below that a note says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

SHARE = Path(__file__).resolve().parent.parent / "share"
from .cards import POPUP_STYLE
FORCE = "$FORCE_BIN"          # set by tmux.conf / tmux-ui.conf (the live launcher unless an instance, SPEC §2.1)
APP = "#{@force_app}/share"
HELP_W, HELP_H = 72, 20          # the key help card (share/help.ansi fits inside, tests/test_keys.py checks)
DIALOGS = {"jump": (64, 21), "new": (64, 18), "archived": (72, 21)}    # popup sizes (cli.overlay_close reopens them)
CHUCK_W, CHUCK_H = 72, 17        # Chuck's card (force support --ui, SPEC §50)
UPDATE_W, UPDATE_H = 72, 20       # the update card (force update --ui, SPEC §3)
CHUCK_STYLE = CARD_STYLE = POPUP_STYLE    # the cards' popups: the bars' surface, a light grey border (force/cards.py)

# (key, label, action, core). None = a separator. Keys are letters (Shift is fine on any layout), Tab and ?,
# never arrows: tmux matches an item's key before its own navigation, so arrows stay free to move in the menu.
ITEMS = [
    ("j", "Jump to session…", "jump", True),
    ("Tab", "Previous session", "prev", True),
    ("h", "Switch host…", "host", True),
    None,
    ("n", "New session…", "new", True),
    ("t", "New shell…", "newshell", False),
    ("r", "Rename…", "rename", True),
    ("f", "Move to a folder…", "folder", True),
    ("K", "Move up in the list", "up", False),
    ("J", "Move down in the list", "down", False),
    None,
    ("x", "Restart", "restart", True),
    ("u", "Upgrade now", "upgrade", False),
    ("a", "Archive…", "archive", True),
    ("d", "Delete…", "delete", False),
    ("A", "Archived sessions…", "archived", True),
    ("e", "Restart all sessions", "restart-all", False),
    None,
    ("b", "Sidebar on / off", "sidebar", False),
    ("H", "Health check…", "health", False),
    ("U", "Update Force…", "update", False),
    ("?", "Ask Chuck…", "chuck", True),
    ("k", "Keys…", "help", True),
    ("q", "Quit (sessions keep running)", "quit", True),
]


def _common() -> dict[str, str]:
    return {
        "sidebar": f"run-shell -b \"{APP}/sidebar-toggle.sh '#{{socket_path}}' '#{{window_id}}'\"",
        "health": f"display-popup {CARD_STYLE} -E -w {HELP_W} -h {HELP_H} \"{APP}/doctor-popup.sh\"",
        "help": f"display-popup {CARD_STYLE} -E -w {HELP_W} -h {HELP_H} \"{APP}/show-help.sh\"",
        "quit": "detach-client",
    }


def classic() -> tuple[str, dict[str, str]]:
    """Classic layout: one host, the client shows the session itself."""
    s = "#{session_name}"

    def pop(w: int, h: int, args: str) -> str:
        return f"display-popup -E -w {w} -h {h} \"{FORCE} dialog {args}\""

    def force(args: str) -> str:
        return f"run-shell -b \"{FORCE} {args} >/dev/null\""

    return s, {
        "chuck": f"display-popup {CHUCK_STYLE} -E -w {CHUCK_W} -h {CHUCK_H} \"{FORCE} support --ui\"",
        "update": f"display-popup {CARD_STYLE} -E -w {UPDATE_W} -h {UPDATE_H} \"{FORCE} update --ui\"",
        "jump": pop(*DIALOGS["jump"], "jump"),
        "prev": "switch-client -l",
        "host": "display-message -d 4000 \" switching hosts needs the split layout: force ui split, then run force again \"",
        "new": pop(*DIALOGS["new"], "new"),
        "newshell": "command-prompt -p \" new shell name:\" "
                    f"\"run-shell \\\"{FORCE} new '%%' --shell --no-switch\\\" ; switch-client -t '=%%'\"",
        "rename": pop(64, 13, f"rename '{s}'"),
        "folder": pop(64, 13, f"move '{s}'"),
        "up": force(f"move '{s}' up"),
        "down": force(f"move '{s}' down"),
        "restart": force(f"restart '{s}'"),
        "upgrade": force(f"upgrade '{s}'"),
        "archive": pop(64, 13, f"archive '{s}'"),
        "delete": pop(64, 13, f"delete '{s}'"),
        "archived": pop(*DIALOGS["archived"], "archived"),
        "restart-all": force("restart --all"),
        **_common(),
    }


def split() -> tuple[str, dict[str, str]]:
    """Split layout (SPEC §39/§40): the ui window shows a host session through a nested client; dialogs get the
    host and client, session actions go through ui-key.sh (it asks the host which session is shown now)."""
    s = "#{@force_session}"
    env = " ".join(f"-e \"{k}=#{{{v}}}\"" for k, v in (
        ("FORCE_HOST", "@force_host"), ("FORCE_CLIENT", "@force_nested"),
        ("FORCE_SESSION", "@force_session"), ("FORCE_GLYPHS", "@force_glyphs")))

    def pop(w: int, h: int, args: str) -> str:
        return f"display-popup {env} -E -w {w} -h {h} \"{FORCE} dialog {args}\""

    def key(action: str) -> str:
        return f"run-shell -b \"{APP}/ui-key.sh '#{{socket_path}}' '#{{window_id}}' {action}\""

    def host_first(kind: str, cmd: str) -> str:
        """Chuck shown over a remote (SPEC §50): the window's host back in the main pane first, then the dialog with
        that host's values (ui-main opens it: tmux fills a popup's -e values before a restore could change them)."""
        return (f"if-shell -F \"#{{@force_overlay}}\" \"run-shell -b '{FORCE} ui-main --window #{{window_id}} --wait "
                f"--dialog {kind} --client #{{client_name}}'\" \"{cmd.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}\"")

    return s, {
        "chuck": f"display-popup {env} -e \"FORCE_WINDOW=#{{window_id}}\" {CHUCK_STYLE} -E -w {CHUCK_W} -h {CHUCK_H} "
                 f"\"{FORCE} support --ui\"",
        "update": f"display-popup {env} {CARD_STYLE} -E -w {UPDATE_W} -h {UPDATE_H} \"{FORCE} update --ui\"",
        "jump": host_first("jump", pop(*DIALOGS["jump"], "jump")),
        "prev": key("last"),
        "host": f"run-shell -b \"{FORCE} hosts-menu --client '#{{client_name}}' --window '#{{window_id}}'\"",
        "new": host_first("new", pop(*DIALOGS["new"], "new")),
        "newshell": "command-prompt -p \" new shell name:\" "
                    f"\"run-shell -b \\\"{APP}/ui-key.sh '#{{socket_path}}' '#{{window_id}}' newshell '%%'\\\"\"",
        "rename": pop(64, 13, f"rename '{s}'"),
        "folder": pop(64, 13, f"move '{s}'"),
        "up": key("up"),
        "down": key("down"),
        "restart": key("restart"),
        "upgrade": key("upgrade"),
        "archive": pop(64, 13, f"archive '{s}'"),
        "delete": pop(64, 13, f"delete '{s}'"),
        "archived": host_first("archived", pop(*DIALOGS["archived"], "archived")),
        "restart-all": key("restart-all"),
        **_common(),
    }


def _quote(word: str) -> str:
    """A word in tmux double quotes (\\ and " escaped; $ and #{} stay live: tmux expands them)."""
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _variants() -> list[tuple[int, list]]:
    """(rows needed, items) from the fullest down: full with separators, full, core with separators, core."""
    core = [it for it in ITEMS if it is None or it[3]]
    out = []
    for items in (ITEMS, core):
        tidy = [it for i, it in enumerate(items) if it is not None or (i and items[i - 1] is not None)]
        for v in (tidy, [it for it in tidy if it is not None]):
            out.append((len(v) + 2, v))
    return out


# The menu in the cards' style (force/cards.py): a coral icon before each item in the rich tier (Nerd Font
# symbols; the plain tier has none, it may lack them), the title with Force's bolt. tmux expands the formats.
ICONS = {
    "jump": "\uf002", "prev": "\uf0ec", "host": "\uf233", "new": "\uf067", "newshell": "\uf120",
    "rename": "\uf040", "folder": "\uf07b", "up": "\uf062", "down": "\uf063", "restart": "\uf01e",
    "upgrade": "\uf0aa", "archive": "\uf187", "delete": "\uf1f8", "archived": "\uf07c", "restart-all": "\uf021",
    "sidebar": "\uf0db", "health": "\uf21e", "update": "\uf01b", "chuck": "\uf118", "help": "\uf11c", "quit": "\uf011",
}
CORAL, WHITE, GREY = "#d97757", "#ffffff", "#8a8a8a"
RICH = "#{==:#{@force_glyphs},rich}"


def _label(action: str, text: str) -> str:
    """An item: its icon in coral and two spaces (rich tier only), then the text in the menu's colour."""
    return f"#{{?{RICH},#[fg={CORAL}]{ICONS[action]}#[default]  ,}}{text}"


def render(mode: str) -> str:
    session, cmds = {"classic": classic, "split": split}[mode]()
    title = (f"#[align=centre] #{{?{RICH},#[fg={CORAL}]\uf0e7 ,}}#[fg={WHITE} bold]Force#[fg={GREY} nobold] · "
             f"#{{=/24/…:{session[2:-1]}}} ")

    def menu(items) -> str:
        words = ["display-menu", "-O", "-x", "C", "-y", "C", "-T", _quote(title)]
        for it in items:
            words += ['""'] if it is None else [_quote(_label(it[2], it[1])), _quote(it[0]), _quote(cmds[it[2]])]
        return " ".join(words)

    variants = _variants()
    smallest = variants[-1][0]
    body = f"display-message -d 4000 \" the terminal is too short for the Force menu ({smallest} rows) \""
    for rows, items in reversed(variants):
        body = f"if -F \"#{{e|>=:#{{client_height}},{rows}}}\" {{\n{menu(items)}\n}} {{\n{body}\n}}"
    return ("# GENERATED by `python -m force.menus` from force/menus.py (SPEC §44): edit that, then regenerate.\n"
            f"# The Force menu, {mode} layout. Loaded by Ctrl+Space / Ctrl+\\ and the header's controls button.\n"
            + body + "\n")


# Copy mode in a pane whose program does not take the mouse (a shell): a drag selects and the selection stays
# after the release; a right click copies it (to the clipboard through the ui tmux, SPEC §41) and returns to the
# shell; a left click or Esc returns without copying; a typed key returns and reaches the shell. tmux cannot say
# which key an `Any` binding caught (and `Any` would also swallow Ctrl+Space and the Alt keys), so every typing
# key gets its own binding. Arrows and PgUp/PgDn keep moving through the scrollback.
TYPED = [chr(c) for c in range(0x21, 0x7f)] + ["Space", "Enter", "BSpace", "Tab", "C-c", "C-d", "C-u", "C-l"]


def _key_word(k: str) -> str:
    return '"\'"' if k == "'" else f"'{k}'"


def render_copy_mode() -> str:
    lines = [
        "# GENERATED by `python -m force.menus` from force/menus.py (SPEC §44): edit that, then regenerate.",
        "# Copy mode for shells: the selection stays, right click copies, typing goes back to the shell.",
        "bind -T copy-mode MouseDragEnd1Pane send-keys -X stop-selection",
        "bind -T copy-mode MouseDown3Pane send-keys -X copy-pipe-and-cancel",
        "bind -T copy-mode MouseDown1Pane { select-pane ; send-keys -X cancel }",
        "bind -T copy-mode DoubleClick1Pane { select-pane ; send-keys -X select-word ; send-keys -X stop-selection }",
        "bind -T copy-mode TripleClick1Pane { select-pane ; send-keys -X select-line ; send-keys -X stop-selection }",
        "bind -T copy-mode WheelUpPane send-keys -X -N 3 scroll-up",
        "bind -T copy-mode WheelDownPane send-keys -X -N 3 scroll-down",
        "bind -T copy-mode Escape send-keys -X cancel",
        "bind -T copy-mode Up send-keys -X cursor-up",
        "bind -T copy-mode Down send-keys -X cursor-down",
        "bind -T copy-mode PageUp send-keys -X page-up",
        "bind -T copy-mode PageDown send-keys -X page-down",
    ]
    for k in TYPED:
        w = _key_word(k)
        lines.append(f"bind -T copy-mode {w} {{ send-keys -X cancel ; send-keys {w} }}")
    return "\n".join(lines) + "\n"


def path(mode: str) -> Path:
    return SHARE / f"menu-{mode}.tmux"


def _files() -> dict[Path, str]:
    out = {path(m): render(m) for m in ("classic", "split")}
    out[SHARE / "copy-mode.tmux"] = render_copy_mode()
    return out


def main(argv: list[str]) -> int:
    stale = {p: text for p, text in _files().items() if not p.exists() or p.read_text() != text}
    if "--check" in argv:
        for p in stale:
            print(f"{p} is out of date: run python -m force.menus", file=sys.stderr)
        return 1 if stale else 0
    for p, text in stale.items():
        p.write_text(text)
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
