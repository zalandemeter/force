"""Claude Code status-line command for force sessions (SPEC §21).

    python3 -I -S statusline.py <usage.json>

Claude Code runs it with a JSON description of the session on stdin. force only wants `rate_limits`
(plan usage: five_hour / seven_day, used_percentage + resets_at) and saves it for the sidebar. It prints
nothing, so Claude shows no status line. Standard library only; never fails (Claude would show the error).

Every session writes the same file, and an idle session still reports the usage of its last request, so a
window is merged, not overwritten: within one window (same resets_at) usage only grows, so the higher
percentage wins; a later resets_at is a new window and replaces it; an earlier one is stale and is ignored.

After the Claude Code account changed (the daemon wrote <state>/account.json, next to usage.json), a session that
made no request since then still reports the old account's figures: a write is dropped while its transcript
(`transcript_path`) is older than the change. Accepted writes carry the account; the saved figures of another account
(or of none) are replaced whole, whatever their resets_at, and the normal merge resumes from there.
"""
import json
import os
import sys


def _merge(old, new):
    """One window: the saved one (`old`) merged with this session's report (`new`)."""
    def ok(w):
        return isinstance(w, dict) and isinstance(w.get("resets_at"), (int, float)) \
            and isinstance(w.get("used_percentage"), (int, float))
    if not ok(new):
        return old if ok(old) else new
    if not ok(old) or new["resets_at"] > old["resets_at"]:
        return new
    if new["resets_at"] < old["resets_at"]:
        return old
    return new if new["used_percentage"] >= old["used_percentage"] else old


def _account(usage_path):
    """<state>/account.json next to usage.json: {"account", "since_ms"}, or None (no change seen)."""
    try:
        with open(os.path.join(os.path.dirname(usage_path) or ".", "account.json")) as f:
            d = json.loads(f.read(4096))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("account"), str):
        return None
    since = d.get("since_ms")
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        return None
    return d


def main() -> int:
    if len(sys.argv) != 2:
        return 0
    try:
        data = json.loads(sys.stdin.read(1 << 20))
    except Exception:
        return 0
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return 0
    out = {k: limits.get(k) for k in ("five_hour", "seven_day")}
    if not any(isinstance(v, dict) for v in out.values()):
        return 0
    path = sys.argv[1]
    acct = _account(path)
    if acct is not None:
        transcript = data.get("transcript_path")
        if isinstance(transcript, str) and transcript:
            try:
                mtime_ms = os.stat(transcript).st_mtime * 1000
            except OSError:
                return 0                     # no request yet
            if mtime_ms < acct["since_ms"]:
                return 0                     # no request since the account changed: the old account's figures
    saved = ""
    try:
        with open(path) as f:
            saved = f.read()
        old = json.loads(saved)
        if isinstance(old, dict) and (acct is None or old.get("account") == acct["account"]):
            out = {k: _merge(old.get(k), out[k]) for k in out}
    except (OSError, ValueError):
        pass
    if acct is not None:
        out["account"] = acct["account"]
    text = json.dumps(out, sort_keys=True)
    if text == saved:
        return 0                             # unchanged: no write
    try:
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
