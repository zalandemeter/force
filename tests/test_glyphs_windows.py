"""The plain glyph tier draws only characters Windows Terminal's default font has (SPEC §28).

Cascadia Mono lacks a character → Windows draws a stand-in from another font, wider than the cell; Windows Terminal
paints each cell in its own colour, so the part that spills over takes the next cell's colour (a half-coral ✻), or
Windows picks a colour emoji twice as wide. tests/cascadia-mono.txt is the font's character set.
"""
from __future__ import annotations

import re
from pathlib import Path

from force import cards, menus

ROOT = Path(__file__).resolve().parent.parent
# RICH-only characters that are not Nerd Font icons: ✻ and ⇄ (their next cell takes their colour, model.rs
# kind_mark_after); the slim usage bars (box drawing, which Windows Terminal draws itself)
RICH_ONLY = set("✻⇄━╸")


def _cascadia() -> set[int]:
    cps: set[int] = set()
    for line in (ROOT / "tests" / "cascadia-mono.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        a, _, b = line.partition("-")
        cps.update(range(int(a, 16), int(b or a, 16) + 1))
    return cps


def _missing(chars) -> set[str]:
    have = _cascadia()
    return {c for c in chars
            if ord(c) > 0x7e and ord(c) not in have
            and not 0xE000 <= ord(c) <= 0xF8FF                  # Nerd Font icons: the rich tier only
            and not 0x2500 <= ord(c) <= 0x259F}                 # box drawing and blocks: the terminal draws them


def test_the_menus_plain_icons_are_in_cascadia_mono():
    assert not _missing("".join(menus.PLAIN_ICONS.values()))
    assert set(menus.PLAIN_ICONS) == set(menus.ICONS), "every rich icon has its plain one"


def test_the_hosts_menu_plain_icons_are_in_cascadia_mono():
    assert not _missing("⌂◫●")
    src = (ROOT / "force" / "cli.py").read_text()
    assert "icon(chr(0xF109), '⌂')" in src and "icon(chr(0xF233), '◫')" in src


def test_the_cards_draw_cascadia_mono_characters():
    text = cards.plain(cards.keys_text() + cards.card_text("claude", rich=False) + cards.doctor_head())
    assert not _missing(text)


def test_the_rust_panes_plain_characters_are_in_cascadia_mono():
    # every non-ASCII character literal outside tests and comments: the rich tier's own are Nerd Font icons
    # (exempt) or listed in RICH_ONLY
    found: dict[str, str] = {}
    for src in sorted((ROOT / "crates" / "sidebar" / "src").glob("*.rs")):
        code = src.read_text().split("#[cfg(test)]")[0]
        for line in code.splitlines():
            line = re.sub(r"//.*", "", line)
            for c in line:
                if ord(c) > 0x7e:
                    found.setdefault(c, src.name)
    bad = {c: f for c, f in found.items() if c in _missing(c) and c not in RICH_ONLY}
    assert not bad, f"not in Cascadia Mono (Windows Terminal draws a spilling stand-in): {bad}"
