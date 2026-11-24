#!/usr/bin/env python3
"""A made-up Force snapshot for the landing page's screens and the film: invented sessions in every state, so the
real sidebar and header draw them. Usage: demo_snapshot.py OUT.json [SHOWN]  (SHOWN: the session on screen,
default api)"""
import json
import os
import sys
import time
import uuid

NAMES = ["shell", "api", "billing-fix", "docs-site", "infra", "mobile", "migration-2024"]


def s(now, name, idx, state, ago_min, activity=None, model="sonnet 4.6", effort="high", mode="auto", kind="claude",
      waiting=None, unread=False, watching=False):
    return {"name": name, "index": idx, "state": state, "since_ms": now - ago_min * 60_000, "version": "2.1.318",
            "outdated": False, "archived": False, "order": idx,
            "cwd": os.path.expanduser("~") if kind == "shell" else f"/home/dev/{name}",
            "session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, name)), "pid": 1000 + idx, "waiting_for": waiting,
            "viewed": False, "activity": activity, "mode": mode if kind == "claude" else None,
            "model": model if kind == "claude" else None, "effort": effort if kind == "claude" else None,
            "shared": None, "note": None, "kind": kind, "why": None, "title": None, "support": False,
            "unread": unread, "watching": watching, "remote_control": None, "rc_connected": None}


def snapshot(shown: str = "api", now: int | None = None) -> dict:
    now = now or int(time.time() * 1000)
    sessions = [
        s(now, "shell", 1, "SHELL", 180, activity="shell ~", kind="shell"),
        s(now, "api", 2, "IDLE", 1, activity="idle", mode="edits"),
        s(now, "billing-fix", 3, "NEEDS_YOU", 1, activity="allow Bash: npm test?", waiting="permission", mode="manual"),
        s(now, "docs-site", 4, "YOUR_TURN", 6, activity="your turn", model="opus 5.5"),
        s(now, "infra", 5, "IDLE", 42, activity="idle", effort="medium"),
        # working, and wrote a finding you haven't read yet: the card turns blue (SPEC §5)
        s(now, "mobile", 6, "WORKING", 1, activity="running a command", model="opus 5.5", mode="edits", unread=True),
        s(now, "migration-2024", 7, "STALE", 60 * 30, activity="stale"),
    ]
    for x in sessions:   # the session on screen is the one you are looking at (opening it reads the finding)
        x["viewed"] = x["name"] == shown
        x["unread"] = x["unread"] and not x["viewed"]
    usage = {"five_hour": {"used_percentage": 34.0, "resets_at": now // 1000 + 3 * 3600},
             "seven_day": {"used_percentage": 18.0, "resets_at": now // 1000 + 4 * 86400}}
    return {"generated_ms": now, "latest_version": "2.1.318",
            "counts": {"needs_you": 1, "working": 1, "your_turn": 1, "idle": 2, "stale": 1, "down": 0},
            "smoke": {"ok": True, "at": now / 1000}, "sessions": sessions, "usage": usage,
            "limits": [{"kind": "claude", "label": "Claude", **usage}]}


if __name__ == "__main__":
    json.dump(snapshot(sys.argv[2] if len(sys.argv) > 2 else "api"), open(sys.argv[1], "w"), indent=1)
