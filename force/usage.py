"""Claude Code rate-limit usage bars for the sidebar (SPEC §usage).

Claude Code passes `rate_limits` to a status-line command; force saves it to
`$FORCE_STATE/usage.json`. The daemon puts it into the snapshot and the sidebar shows
two bars (five-hour "session", seven-day "week") at its bottom. This module
normalises the untrusted JSON and renders the Rich (Textual Static) markup.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

# English 3-letter weekdays (a fixed list, never the process locale).
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Bar glyph/colour rule: filled before empty, then a space, then the percentage.
_RULE_COLOUR = "#3a3a3a"
_LABEL_COLOUR = "#808080"
_PCT_COLOUR = "#c6c6c6"
_RESET_COLOUR = "#6c6c6c"
_FILL_EMPTY_COLOUR = "#3a3a3a"
_EMPTY = "░"
_FILL = "█"


def _window(raw: object) -> dict | None:
    """Validate one window (used_percentage + resets_at) and return W or None."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    reset = raw.get("resets_at")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    if isinstance(reset, bool) or not isinstance(reset, (int, float)):
        return None
    if not math.isfinite(pct) or not math.isfinite(reset):
        return None
    if reset <= 0 or reset > 4_102_444_800:      # beyond 2100: datetime would overflow in markup()
        return None
    return {
        "used_percentage": max(0.0, min(100.0, float(pct))),
        "resets_at": int(reset),
    }


def normalize(raw: object) -> dict | None:
    """Validate untrusted JSON -> {"five_hour": W|None, "seven_day": W|None}.

    Returns None when raw is not a dict, or both windows are invalid/missing.
    """
    if not isinstance(raw, dict):
        return None
    five_hour = _window(raw.get("five_hour"))
    seven_day = _window(raw.get("seven_day"))
    if five_hour is None and seven_day is None:
        return None
    return {"five_hour": five_hour, "seven_day": seven_day}


def read_usage(path: Path) -> dict | None:
    """Read + json.loads + normalize. Missing/unreadable/bad JSON -> None. Never raises.
    After a Claude Code account change (<state>/account.json next to it, SPEC §21), figures saved for another
    account, or for none, are no data."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        from .account import read_state
        acct = read_state(Path(path).parent)
        if acct is not None and (not isinstance(raw, dict) or raw.get("account") != acct["account"]):
            return None
        return normalize(raw)
    except Exception:
        return None


def _local(ts: float, tz=None):
    """Aware datetime for a Unix-epoch second, in tz (None -> the local timezone)."""
    if tz is None:
        return datetime.fromtimestamp(ts).astimezone()
    return datetime.fromtimestamp(ts, tz)


def _fill(pct: float, full: int) -> int:
    """Number of filled cells for a percentage, per the spec's rounding rules."""
    if pct <= 0:
        return 0
    n = round(pct / 100.0 * full)
    if pct >= 99.5:
        return full
    return max(1, min(full, n))


def _bar_colour(pct: float) -> str:
    if pct >= 90:
        return "#e8806f"
    if pct >= 70:
        return "#d7af5f"
    return "#8a8a8a"


def _pct_colour(pct: float) -> str:
    if pct >= 70:
        return _bar_colour(pct)
    return _PCT_COLOUR


def _line_a(label: str, win: dict, width: int, now: float) -> str:
    barw = max(4, width - 13)
    expired = win["resets_at"] <= now
    pct = 0.0 if expired else win["used_percentage"]
    filled = 0 if expired else _fill(pct, barw)
    bar = (
        f"[{_bar_colour(pct)}]{_FILL * filled}[/]"
        f"[{_FILL_EMPTY_COLOUR}]{_EMPTY * (barw - filled)}[/]"
    )
    if expired:
        pcttext = "  –"
    else:
        pcttext = f"{round(pct)}%"
    return (
        f"[{_LABEL_COLOUR}]{label:<8}[/]{bar} "
        f"[{_pct_colour(pct)}]{pcttext:>4}[/]"
    )


def _line_b(resets_at: int, width: int, now: float, tz=None) -> str:
    if resets_at <= now:
        text = "new window"
    else:
        dt = _local(resets_at, tz)
        if dt.date() == _local(now, tz).date():
            text = f"resets {dt:%H:%M}"
        else:
            text = f"resets {_WEEKDAYS[dt.weekday()]} {dt:%H:%M}"
    return f"[{_RESET_COLOUR}]{((' ' * 8) + text)[:width]}[/]"


def markup(usage: dict | None, now: float, width: int, tz=None) -> str:
    """Rich console markup for the panel, lines joined with "\\n". No trailing newline."""
    if usage is None:
        return ""
    lines = [f"[{_RULE_COLOUR}]{'─' * width}[/]"]
    for key, label in (("five_hour", "session"), ("seven_day", "week")):
        win = usage.get(key)
        if win is None:
            continue
        lines.append(_line_a(label, win, width, now))
        lines.append(_line_b(win["resets_at"], width, now, tz))
    return "\n".join(lines)


def height(usage: dict | None) -> int:
    """Number of lines markup() produces: 0 for None; else 1 + 2 * present windows."""
    if usage is None:
        return 0
    present = sum(1 for k in ("five_hour", "seven_day") if usage.get(k) is not None)
    return 1 + 2 * present
