"""A change of the logged-in Claude Code account (SPEC §4, §21.1, §53): how Force notices it, the usage bars'
accept rule, and what the daemon does about Remote Control and the self-test. Tmp Claude dirs only."""
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from force import account, services
from force.daemon import RC_OFF_AFTER_MS, Daemon
from force.registry import load, locked
from force.usage import read_usage

STATUSLINE = Path(__file__).resolve().parent.parent / "share" / "statusline.py"


def _login(claude_dir: Path, acc: str | None, org: str = "org-1", **extra) -> Path:
    """Write the tmp `.claude.json` (inside the tmp Claude dir) as Claude Code does: big, with the account."""
    path = claude_dir / ".claude.json"
    data = {"numStartups": 7, "projects": {f"/p/{i}": {"allowedTools": []} for i in range(50)}, **extra}
    if acc is not None:
        data["oauthAccount"] = {"accountUuid": acc, "organizationUuid": org, "emailAddress": f"{acc}@example.com"}
    path.write_text(json.dumps(data))
    _login.n += 1                                     # a distinct mtime for every write, even within one clock tick
    os.utime(path, ns=(path.stat().st_atime_ns, (1_700_000_000 + _login.n) * 1_000_000_000))
    return path


_login.n = 0


def _hash(acc, org="org-1"):
    return hashlib.sha256(f"{acc}:{org}".encode()).hexdigest()[:16]


# --- where the account is, and its hash ------------------------------------------------------------------------
def test_config_file_inside_a_config_dir_else_next_to_it(tmp_path):
    cdir = tmp_path / ".claude"
    cdir.mkdir()
    assert account.config_file(cdir) == tmp_path / ".claude.json"          # ~/.claude -> ~/.claude.json
    (cdir / ".claude.json").write_text("{}")
    assert account.config_file(cdir) == cdir / ".claude.json"              # CLAUDE_CONFIG_DIR style


def test_account_hash_only_never_the_ids():
    h = account.account_hash({"oauthAccount": {"accountUuid": "acc-1", "organizationUuid": "org-1",
                                               "emailAddress": "a@example.com"}})
    assert h == _hash("acc-1") and len(h) == 16
    assert "acc-1" not in h and "org" not in h
    assert account.account_hash({"oauthAccount": {"accountUuid": "acc-1"}}) == \
        hashlib.sha256(b"acc-1:").hexdigest()[:16]
    for logged_out in ({}, {"oauthAccount": None}, {"oauthAccount": {"organizationUuid": "o"}}, [], "x"):
        assert account.account_hash(logged_out) is None


def test_watch_first_read_is_no_change_then_a_switch_is(tmp_path):
    _login(tmp_path, "acc-a")
    w = account.Watch(tmp_path)
    assert w.poll() is None, "the first read after start is not a change"
    assert w.poll() is None
    _login(tmp_path, "acc-b")
    assert w.poll() == (_hash("acc-a"), _hash("acc-b"))
    assert w.poll() is None
    assert w.parse_ms is not None


def test_watch_parses_only_when_the_file_changed(tmp_path, monkeypatch):
    _login(tmp_path, "acc-a")
    w = account.Watch(tmp_path)
    reads = []
    real = account._read
    monkeypatch.setattr(account, "_read", lambda p: (reads.append(p), real(p))[1])
    w.poll()
    for _ in range(20):
        w.poll()
    assert len(reads) == 1, "one parse while the file is unchanged"
    _login(tmp_path, "acc-a", numStartups=8)                   # rewritten, same account
    assert w.poll() is None and len(reads) == 2


def test_watch_logout_login(tmp_path):
    w = account.Watch(tmp_path)
    _login(tmp_path, None)
    assert w.poll() is None                                    # logged out at start
    _login(tmp_path, "acc-a")
    assert w.poll() is None, "logged out -> in is not a change"
    _login(tmp_path, None)
    assert w.poll() is None
    _login(tmp_path, "acc-a")
    assert w.poll() is None, "out and back in to the same account is not a change"
    _login(tmp_path, None)
    assert w.poll() is None
    _login(tmp_path, "acc-b")
    assert w.poll() == (_hash("acc-a"), _hash("acc-b")), "out of one, into another: a change"


def test_watch_waits_out_a_half_written_file(tmp_path):
    _login(tmp_path, "acc-a")
    w = account.Watch(tmp_path)
    w.poll()
    (tmp_path / ".claude.json").write_text('{"oauthAccount": {"accountUu')
    assert w.poll() is None
    _login(tmp_path, "acc-b")
    assert w.poll() == (_hash("acc-a"), _hash("acc-b"))


def test_watch_parse_is_cheap(tmp_path):
    # a .claude.json of the usual size (a few hundred KB): one parse stays well under a tick
    _login(tmp_path, "acc-a", projects={f"/p/{i}": {"allowedTools": ["x"] * 20, "history": ["y" * 40] * 5}
                                        for i in range(600)})
    assert (tmp_path / ".claude.json").stat().st_size > 200_000
    w = account.Watch(tmp_path)
    w.poll()
    assert w.parse_ms < 200


def test_state_file_roundtrip(tmp_path):
    assert account.read_state(tmp_path) is None
    account.write_state(tmp_path, "abc", 123)
    assert account.read_state(tmp_path) == {"account": "abc", "since_ms": 123}
    assert oct((tmp_path / "account.json").stat().st_mode & 0o777) == "0o600"
    (tmp_path / "account.json").write_text('{"account": "abc", "since_ms": true}')
    assert account.read_state(tmp_path) is None


# --- the status line's accept rule ---------------------------------------------------------------------------
def _w(pct, reset):
    return {"used_percentage": pct, "resets_at": reset}


def _report(usage: Path, transcript: Path | None, five=None, seven=None):
    rl = {}
    if five is not None:
        rl["five_hour"] = five
    if seven is not None:
        rl["seven_day"] = seven
    payload = {"rate_limits": rl}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    r = subprocess.run(["python3", "-I", "-S", str(STATUSLINE), str(usage)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    try:
        return json.loads(usage.read_text())
    except OSError:
        return None


def _touch(path: Path, ms: int) -> Path:
    path.write_text("{}\n")
    os.utime(path, ns=(ms * 1_000_000, ms * 1_000_000))
    return path


def test_statusline_accept_rule(tmp_path):
    usage = tmp_path / "usage.json"
    since = 2_000_000_000_000
    old_t = _touch(tmp_path / "old.jsonl", since - 60_000)        # an idle session of the old account
    new_t = _touch(tmp_path / "new.jsonl", since + 5_000)         # a session that made a request since
    # before any change: the plain merge (no account.json, nothing tagged)
    assert _report(usage, old_t, _w(99, 9000), _w(40, 90000)) == {"five_hour": _w(99, 9000),
                                                                  "seven_day": _w(40, 90000)}
    account.write_state(tmp_path, "acct-new", since)
    usage.unlink()                                                # the daemon clears it on the change
    # stale transcript: dropped
    assert _report(usage, old_t, _w(99, 9000), _w(40, 90000)) is None
    # no transcript yet: dropped
    assert _report(usage, tmp_path / "missing.jsonl", _w(99, 9000)) is None
    # fresh: accepted and tagged
    got = _report(usage, new_t, _w(3, 5000), _w(13, 80000))
    assert got == {"five_hour": _w(3, 5000), "seven_day": _w(13, 80000), "account": "acct-new"}
    # then the normal merge: a lower figure of the same window does not win, an earlier window is ignored
    assert _report(usage, new_t, _w(2, 5000))["five_hour"] == _w(3, 5000)
    assert _report(usage, new_t, _w(50, 4000))["five_hour"] == _w(3, 5000)
    assert _report(usage, new_t, _w(4, 5000))["five_hour"] == _w(4, 5000)
    # a stale old-account process still cannot write
    assert _report(usage, old_t, _w(99, 9000))["five_hour"] == _w(4, 5000)


def test_statusline_first_accepted_write_replaces_whatever_resets_at(tmp_path):
    usage = tmp_path / "usage.json"
    since = 2_000_000_000_000
    new_t = _touch(tmp_path / "new.jsonl", since + 1)
    # the old account's figures got written (untagged, or tagged with the old account) after the clear
    for left in ({"five_hour": _w(99, 9000), "seven_day": _w(40, 90000)},
                 {"five_hour": _w(99, 9000), "seven_day": _w(40, 90000), "account": "acct-old"}):
        usage.write_text(json.dumps(left))
        account.write_state(tmp_path, "acct-new", since)
        assert read_usage(usage) is None, "the daemon shows no data meanwhile"
        got = _report(usage, new_t, _w(3, 5000))                  # an EARLIER resets_at than the old 9000
        assert got == {"five_hour": _w(3, 5000), "seven_day": None, "account": "acct-new"}
        assert read_usage(usage)["five_hour"]["used_percentage"] == 3.0


def test_statusline_without_transcript_path_is_accepted(tmp_path):
    usage = tmp_path / "usage.json"
    account.write_state(tmp_path, "acct-new", 2_000_000_000_000)
    assert _report(usage, None, _w(3, 5000))["five_hour"] == _w(3, 5000)


# --- the daemon --------------------------------------------------------------------------------------------------
class _Clock:
    def __init__(self, t=None):
        self.t = time.time() if t is None else t

    def __call__(self):
        return self.t


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def _status(env, name):
    pane = env.cockpit.agent_pane(name)
    if pane is None or not pane.pid:
        return {}
    try:
        return json.loads((env.paths.claude_dir / "sessions" / f"{pane.pid}.json").read_text())
    except (OSError, ValueError):
        return {}


def _live(env, name, status="idle", **extra):
    with locked(env.paths.home) as reg:
        e = reg.add(name, str(uuid.uuid4()), "/tmp")
        e.extra.update(extra)
    env.cockpit.create(e)
    assert _wait(lambda: _status(env, name).get("status")), f"{name} not running"
    pid = env.cockpit.agent_pane(name).pid
    (env.paths.claude_dir / "sessions" / f"{pid}.ctl").write_text(json.dumps({"status": status}))
    assert _wait(lambda: _status(env, name).get("status") == status)


@pytest.fixture
def daemon(env, monkeypatch):
    env.make_cockpit()
    _login(env.paths.claude_dir, "acc-a")
    kicked = []
    monkeypatch.setattr(services, "start_smoke", lambda run=None: (kicked.append(1), True)[1])
    d = Daemon(env.cockpit, _Clock())
    d.kicked = kicked
    restarted = []
    monkeypatch.setattr(env.cockpit, "restart_many", lambda names, timeout=15.0: (restarted.extend(names), [])[1])
    d.restarted = restarted
    return d


def test_daemon_account_change(env, daemon, capsys):
    _live(env, "idle1", "idle")
    _live(env, "busy1", "busy")
    _live(env, "ask1", "waiting")
    _live(env, "norc", "idle", remote_control=False)
    _live(env, "chuck", "idle", role="support")
    (env.paths.state / "usage.json").write_text(json.dumps({"five_hour": _w(99, 9000)}))
    daemon.tick()                                                 # first read: no change
    assert daemon.restarted == [] and daemon.kicked == []
    assert not (env.paths.state / "account.json").exists()
    assert (env.paths.state / "usage.json").exists()

    _login(env.paths.claude_dir, "acc-b")
    daemon.tick()
    st = account.read_state(env.paths.state)
    assert st == {"account": _hash("acc-b"), "since_ms": int(daemon.clock() * 1000)}
    raw = (env.paths.state / "account.json").read_text()
    assert "acc-b" not in raw and "example.com" not in raw, "only the hash is stored"
    assert not (env.paths.state / "usage.json").exists(), "the bars are cleared"
    assert daemon.restarted == ["idle1"], "idle: now; no Remote Control or Chuck: never"
    reg = load(env.paths.registry)
    assert reg.get("busy1").extra.get("pending_restart") is True, "working: after this turn"
    assert reg.get("ask1").extra.get("pending_restart") is True, "needs you: once answered"
    assert not reg.get("norc").extra.get("pending_restart")
    assert not reg.get("chuck").extra.get("pending_restart")
    assert daemon.kicked == [1], "the self-test is due at once"
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "account changed" in ln]
    assert len(lines) == 1 and "1 restarting, 2 after their turn" in lines[0]
    snap = json.loads(env.paths.snapshot.read_text())
    why = {s["name"]: s.get("why", "") for s in snap["sessions"]}
    for n in ("idle1", "busy1", "ask1"):
        assert Daemon.ACCOUNT_WHY in why[n]
    assert Daemon.ACCOUNT_WHY not in why["norc"]

    daemon.tick()                                                 # nothing more on later ticks
    assert daemon.restarted == ["idle1"] and daemon.kicked == [1]


def test_daemon_logout_login_same_account_is_no_change(env, daemon):
    daemon.tick()
    _login(env.paths.claude_dir, None)
    daemon.tick()
    _login(env.paths.claude_dir, "acc-a")
    daemon.tick()
    assert not (env.paths.state / "account.json").exists() and daemon.kicked == []


def _rc(daemon, env, name, pid, connected, started_ms=None):
    daemon._rc_connected = lambda pi: connected
    e = load(env.paths.registry).get(name)
    return daemon._follow_rc(e, SimpleNamespace(pane_pid=pid, pane_started_ms=started_ms), int(daemon.clock() * 1000))


def test_no_auto_off_within_ten_minutes_of_a_change(env, daemon):
    with locked(env.paths.home) as reg:
        reg.add("s", str(uuid.uuid4()), "/tmp")
    daemon.tick()
    _rc(daemon, env, "s", 11, True)                               # seen connected
    _login(env.paths.claude_dir, "acc-b")
    daemon._step_account()
    _rc(daemon, env, "s", 11, False)                              # the bridge dropped with the change
    daemon.clock.t += 9 * 60
    assert _rc(daemon, env, "s", 11, False) == (False, None)
    assert "remote_control" not in load(env.paths.registry).get("s").extra, "not off within 10 min"
    daemon.clock.t += 61
    connected, why = _rc(daemon, env, "s", 11, False)
    assert why == "remote control: turned off in the session", "the rule's normal behaviour after the grace"


def test_grace_survives_a_daemon_restart(env, daemon):
    with locked(env.paths.home) as reg:
        reg.add("s", str(uuid.uuid4()), "/tmp")
    account.write_state(env.paths.state, "x", int(daemon.clock() * 1000))
    d2 = Daemon(env.cockpit, daemon.clock)
    _rc(d2, env, "s", 11, True)
    _rc(d2, env, "s", 11, False)
    d2.clock.t += 2 * RC_OFF_AFTER_MS / 1000
    assert _rc(d2, env, "s", 11, False) == (False, None)
    assert "remote_control" not in load(env.paths.registry).get("s").extra


def test_auto_off_still_works_without_a_change(env, daemon):
    with locked(env.paths.home) as reg:
        reg.add("s", str(uuid.uuid4()), "/tmp")
    daemon.tick()
    _rc(daemon, env, "s", 11, True)
    _rc(daemon, env, "s", 11, False)
    daemon.clock.t += RC_OFF_AFTER_MS / 1000
    assert _rc(daemon, env, "s", 11, False)[1] == "remote control: turned off in the session"
    assert load(env.paths.registry).get("s").extra["remote_control"] is False
