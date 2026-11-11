"""Force's popup cards (SPEC §50, §44): Chuck's card and the key help, drawn the same way: a title with its mark,
a rule, rows with a coral key or icon and grey text, a quiet note, the keys that close it. Colours are Force's
palette; the popups take the bars' surface (#212121) with a rounded light grey border (POPUP_STYLE).

  python3 -m force.cards           write share/help.ansi (the key help) when it changed
  python3 -m force.cards --check   exit 1 when share/help.ansi is out of date (tests/test_keys.py)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RGB = {"coral": (217, 119, 87), "white": (255, 255, 255), "name": (198, 198, 198), "grey": (138, 138, 138),
       "dim": (95, 95, 95), "rule": (60, 60, 60), "ink": (38, 38, 38),
       "ok": (135, 192, 143), "bad": (240, 100, 94)}       # the sidebar's your-turn green, needs-you red
# display-popup options for the cards: the bars' surface, a rounded light grey border (tmux.rs CHUCK_POPUP_STYLE)
POPUP_STYLE = '-b rounded -S "fg=#4e4e4e,bg=#212121" -s "bg=#212121"'
CARD_W = 68          # text width; the popups are 72 wide (border + one column of air on each side)


def c(text: str, fg: str | None = None, bg: str | None = None, bold: bool = False, color: bool = True) -> str:
    if not color:
        return text
    codes = (["1"] if bold else []) + ([f"38;2;{';'.join(map(str, RGB[fg]))}"] if fg else []) \
        + ([f"48;2;{';'.join(map(str, RGB[bg]))}"] if bg else [])
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if codes else text


def plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def head(mark: str, title: str, tag: str, color: bool = True) -> list[str]:
    """The title row (mark in coral, title in white, tag on the right in grey) and its rule."""
    w = 2 + len(mark) + (2 if mark else 0) + len(title)
    return ["",
            "  " + (c(mark, "coral", color=color) + "  " if mark else "") + c(title, "white", bold=True, color=color)
            + " " * (CARD_W - w - len(tag)) + c(tag, "grey", color=color),
            "  " + c("─" * (CARD_W - 2), "rule", color=color),
            ""]


def keys_right(*pairs: tuple[str, str], button: str | None = None, color: bool = True) -> str:
    """The closing row, right-aligned: an optional coral button, then key + action pairs in grey."""
    parts = []
    width = 0
    if button:
        parts.append(c(f" {button} ", "ink", "coral", bold=True, color=color))
        width += len(button) + 2
    for k, what in pairs:
        parts.append(c(f"{k}  {what}", "grey", color=color))
        width += len(k) + 2 + len(what)
    width += 3 * (len(parts) - 1)
    return " " * (CARD_W - width) + "   ".join(parts)


# --------------------------------------------------------------------------- Chuck's card ----------------
USES = (  # (rich icon, plain icon, what, how)
    ("\uf059", "?", "How does something work?", "answers from your version's docs"),
    ("\uf013", "*", "Set it up for me", "hosts, notifications, settings"),
    ("\uf188", "!", "Report a bug or an idea", "checks for duplicates first"),
    ("\uf0ad", "+", "Help fix it", "builds the fix and the pull request"),
    ("\uf023", "#", "A security problem", "an email, never in public"),
)


def card_text(agent: str | None, rich: bool, color: bool = True, buttons: bool = True) -> str:
    """Chuck's card: who he is, what he does, what it costs, and (in the popup) the two keys."""
    label = {"claude": "Claude Code", "opencode": "OpenCode"}.get(agent or "", "agent")
    lines = head("\uf118" if rich else ":)", "Chuck", "Force's assistant", color)
    for icon_r, icon_p, what, how in USES:
        lines.append("  " + c(icon_r if rich else icon_p, "coral", color=color) + "  "
                     + c(f"{what:<27}", "name", color=color) + c(how, "grey", color=color))
    lines += ["",
              "  " + c(f"Runs on your {label} and plan, so each answer uses your tokens.", "dim", color=color),
              "  " + c("Nothing goes to GitHub without your click.", "dim", color=color)]
    if buttons:
        lines += ["", keys_right(("Esc", "Close"), button="Enter  Talk to Chuck", color=color), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- the key help ----------------
KEYS = (  # groups of (key, what); a blank line between groups
    (("Ctrl+Space", "the menu: every action, each with its letter"),
     ("Ctrl+Space ?", "ask Chuck, Force's assistant"),
     ("Alt+↑ ↓", "previous / next session (also Alt+PgUp PgDn)"),
     ("Alt+1…9", "session N (Tilix and Terminator keep these)"),
     ("click", "a sidebar row, a header chip, the host chip")),
    (("drag", "selects; right click copies"),
     ("Alt+Enter", "a new line (also Ctrl+J)"),
     ("Ctrl+Space q", "quit; sessions keep running (also × in the top bar)")),
    (("mode badge", "A auto · E edits · P plan · M manual · B bypass"),),
)
KEY_COL = 16


def keys_text(color: bool = True) -> str:
    """The key help (menu `k`): the same card as Chuck's, plain glyphs only (it is one file for every terminal)."""
    lines = head("", "Force keys", "Ctrl+\\ opens the menu too", color)
    for i, group in enumerate(KEYS):
        if i:
            lines.append("")
        for key, what in group:
            lines.append("  " + c(f"{key:<{KEY_COL}}", "coral", color=color) + c(what, "name", color=color))
    lines += ["", keys_right(("any key", "Close"), color=color), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- the health check ------------
def doctor_head(color: bool = True) -> str:
    return "\n".join(head("", "Health check", "force doctor --fix", color))


def doctor_line(line: str, color: bool = True) -> list[str]:
    """One line of `force doctor` as card rows: a green ✓ or a red ✗ in the key column, the text wrapped."""
    mark, rest = (line[:1], line[1:].strip()) if line[:1] in ("✓", "✗") else ("", line.strip())
    fg = {"✓": "ok", "✗": "bad"}.get(mark)
    width = CARD_W - 4
    words, rows, cur = rest.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            rows.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    rows.append(cur)
    out = []
    for i, r in enumerate(rows):
        lead = c(mark, fg, bold=True, color=color) if (i == 0 and mark) else " "
        out.append("  " + lead + " " + c(r[:width], "name", color=color))
    return out


def doctor_foot(color: bool = True) -> str:
    return "\n".join(["", keys_right(("any key", "Close"), color=color)])


HELP_FILE = Path(__file__).resolve().parent.parent / "share" / "help.ansi"


def main(argv: list[str]) -> int:
    text = keys_text() + "\n"
    current = HELP_FILE.read_text() if HELP_FILE.exists() else ""
    if "--check" in argv:
        if current != text:
            print(f"{HELP_FILE} is out of date: run python3 -m force.cards", file=sys.stderr)
            return 1
        return 0
    if current != text:
        HELP_FILE.write_text(text)
        print(f"wrote {HELP_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
