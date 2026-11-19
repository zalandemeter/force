"""force smoke (SPEC §37): the parts that need no real Claude Code / OpenCode. The run itself is exercised
against the real tools by `force smoke`."""
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

from force import remote, smoke


V = {"force": "r1", "claude": "2.1.308", "opencode": "1.19.3"}


def test_due():
    now = 1_000_000.0
    assert smoke.due(None, V, now) == "no earlier run"
    ok = {"ok": True, "at": now - 3600, "versions": V}
    assert smoke.due(ok, V, now) is None
    assert smoke.due(ok, dict(V, claude="2.1.309"), now) == "versions changed"
    assert smoke.due(ok, dict(V, force="r2"), now) == "versions changed"
    assert smoke.due(dict(ok, at=now - smoke.DUE_AFTER_S), V, now) == "daily run"
    failed = {"ok": False, "at": now - 3600, "versions": V}
    assert smoke.due(failed, V, now) is None, "a failure is retried after 6 h, not every hour"
    assert smoke.due(dict(failed, at=now - smoke.RETRY_FAILED_S), V, now) == "retry after a failure"


def test_due_logged_out():
    now = 1_000_000.0
    logged_fail = {"ok": False, "at": now - 3600, "versions": V,
                   "checks": [{"name": "claude: logged in", "ok": False}]}
    assert smoke.due(logged_fail, V, now, logged_in=lambda: True) == "logged in again"
    assert smoke.due(logged_fail, V, now, logged_in=lambda: False) is None
    assert smoke.due(logged_fail, V, now, logged_in=None) is None
    plain_fail = {"ok": False, "at": now - 3600, "versions": V,
                  "checks": [{"name": "claude: turn", "ok": False, "detail": "no WORKING"}]}

    def boom():
        raise AssertionError("logged_in must not be called for a non-login failure")

    assert smoke.due(plain_fail, V, now, logged_in=boom) is None


def test_reason():
    assert smoke.reason([]) is None
    assert smoke.reason(None) is None
    assert smoke.reason("not a list") is None
    assert smoke.reason([{"name": "claude: start", "ok": True, "detail": ""}]) is None
    assert smoke.reason([{"name": "claude: logged in", "ok": False}]) == smoke.LOGGED_OUT
    assert (smoke.reason([{"name": "claude: turn", "ok": False,
                          "detail": "● Login expired · Please run /login"}]) == smoke.LOGGED_OUT)
    assert (smoke.reason([{"name": "claude: turn", "ok": False, "detail": "no WORKING"}])
            == "Claude Code: no reply")
    # skips non-dict entries
    assert (smoke.reason(["junk", {"ok": True}, {"name": "claude: turn", "ok": False, "detail": "x"}])
            == "Claude Code: no reply")
    assert smoke.failed_phrase("opencode: needs you") == "OpenCode: missed a prompt"
    assert smoke.tool_call_as_text('<｜DSML｜tool_call>\n<｜DSML｜invoke name="bash">touch x</｜DSML｜invoke>')
    assert not smoke.tool_call_as_text("Done: I ran touch smoke-marker.")
    assert (smoke.reason([{"name": "opencode: needs you", "ok": False,
                           "detail": smoke.TOOL_CALLS_AS_TEXT + ": the model wrote its tool call as text"}])
            == "OpenCode: tool calls fail")
    assert smoke.failed_phrase("split: force watch") == "self-test: window failed"
    assert smoke.failed_phrase("codex: something new") == "codex: self-test failed"
    # a missing name falls back to "self-test failed"
    assert smoke.reason(["junk", {"ok": False}]) == "self-test failed"
    # first failure wins
    assert (smoke.reason([{"name": "claude: start", "ok": True},
                         {"name": "claude: logged in", "ok": False},
                         {"name": "claude: turn", "ok": False}]) == smoke.LOGGED_OUT)


def test_claude_logged_in(tmp_path):
    def script(text, rc=0):
        p = tmp_path / "claude"
        p.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({text!r})\nsys.exit({rc})\n")
        p.chmod(0o755)
        return p

    assert smoke.claude_logged_in(script('{"loggedIn": false}\n', 1)) is False
    assert smoke.claude_logged_in(script('{"loggedIn": true}\n', 0)) is True
    assert smoke.claude_logged_in(script("garbage\n", 0)) is None
    assert smoke.claude_logged_in(script('{"loggedIn": "yes"}\n', 0)) is None
    assert smoke.claude_logged_in(tmp_path / "missing") is None


def test_command_last_reason(tmp_path, capsys):
    from types import SimpleNamespace
    base = tmp_path / "smoke"
    base.mkdir()
    (base / "last.json").write_text(json.dumps({
        "ok": False, "at": time.time(), "duration_s": 5, "versions": V,
        "checks": [{"name": "claude: logged in", "ok": False, "s": 0, "detail": ""}]}))
    paths = SimpleNamespace(state=tmp_path)
    assert smoke.command(paths, None, if_due=False, last=True) == 1
    out = capsys.readouterr().out
    assert "reason: Claude Code logged out" in out


def test_claude_flow_logged_out(tmp_path):
    from types import SimpleNamespace
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\nimport sys\n"
                     "sys.stdout.write('{\"loggedIn\": false}\\n')\nsys.exit(1)\n")
    script.chmod(0o755)
    log = []
    r = smoke.Run.__new__(smoke.Run)
    r.live = SimpleNamespace(claude_bin=script)
    r.checks, r.log = [], SimpleNamespace(write=log.append, flush=lambda: None)
    r.say = lambda t: log.append(t)
    r.add_entry = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no entry created"))
    r.claude_flow()   # must stop before creating any session
    assert r.checks == [{"name": "claude: logged in", "ok": False, "s": 0,
                         "detail": "Claude Code is logged out: run claude, then /login"}]


def test_read_last_and_command_last(tmp_path, capsys):
    from types import SimpleNamespace
    base = tmp_path / "smoke"
    paths = SimpleNamespace(state=tmp_path)
    assert smoke.read_last(base) is None
    assert smoke.command(paths, None, if_due=False, last=True) == 1
    base.mkdir()
    (base / "last.json").write_text(json.dumps({
        "ok": False, "at": time.time(), "duration_s": 57, "versions": V,
        "checks": [{"name": "claude: start", "ok": True, "s": 2, "detail": ""},
                   {"name": "claude: turn", "ok": False, "s": 90, "detail": "no WORKING"}]}))
    assert smoke.command(paths, None, if_due=False, last=True) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "claude: turn - no WORKING" in out and "claude 2.1.308" in out
    (base / "last.json").write_text("not json")
    assert smoke.read_last(base) is None


def test_snapshot_and_doctor_report_a_failed_smoke(env):
    from force.daemon import Daemon
    from force.doctor import check
    from force.state import read_snapshot
    cockpit = env.make_cockpit()
    d = Daemon(cockpit)
    d.tick()
    assert read_snapshot(env.paths.snapshot).smoke is None
    base = env.paths.state / "smoke"
    base.mkdir()
    (base / "last.json").write_text(json.dumps({"ok": False, "at": 5.0, "checks": [
        {"name": "opencode: move", "ok": False}]}))
    d.tick()
    assert read_snapshot(env.paths.snapshot).smoke == {"ok": False, "at": 5.0,
                                                       "reason": "OpenCode: move failed"}
    probs = [p for p in check(cockpit, systemd=False) if p.code == "self-test"]
    assert probs and "opencode: move" in probs[0].detail
    assert probs[0].detail.startswith("last `force smoke` failed: OpenCode: move failed")
    (base / "last.json").write_text(json.dumps({"ok": True, "at": 6.0}))
    d.tick()
    assert read_snapshot(env.paths.snapshot).smoke == {"ok": True, "at": 6.0}
    assert not [p for p in check(cockpit, systemd=False) if p.code == "self-test"]


def test_claude_checks_skipped_without_claude(tmp_path):
    from types import SimpleNamespace
    log = []
    r = smoke.Run.__new__(smoke.Run)
    r.live = SimpleNamespace(claude_bin=tmp_path / "no-claude")
    r.checks, r.log = [], SimpleNamespace(write=log.append, flush=lambda: None)
    r.say = lambda t: log.append(t)
    r.claude_flow()
    assert r.checks == [{"name": "claude", "ok": True, "s": 0, "detail": "not installed: skipped"}]


def test_environment_sets_ui_socket(tmp_path, monkeypatch):
    """The smoke run sets its own ui tmux socket so split checks never touch the live ui server; its one live
    file is the usage.json its Claude session's status line writes (SPEC §37)."""
    from types import SimpleNamespace
    for k in ("FORCE_HOME", "FORCE_STATE", "FORCE_CONFIG", "FORCE_TMUX_SOCKET", "FORCE_UI_SOCKET", "FORCE_TMUX_CONF",
              "FORCE_CLAUDE_DIR", "FORCE_CLAUDE_BIN", "FORCE_NO_REMOTE_CONTROL", "FORCE_USAGE_FILE",
              "ANTHROPIC_MODEL"):
        monkeypatch.setenv(k, os.environ.get(k, ""))      # restored after the test
    r = smoke.Run.__new__(smoke.Run)
    r.run_dir = tmp_path / "run"
    r.base = tmp_path
    r.work = tmp_path / "work"
    r.live = SimpleNamespace(home=tmp_path / "home", claude_dir=tmp_path / "claude",
                           claude_bin=tmp_path / "claude-bin", state=tmp_path / "live-state")
    r.opencode_managed = False
    saved = {}
    r.run_dir.mkdir()
    r._environment()
    assert os.environ["FORCE_UI_SOCKET"] == f"forcetest-smoke-ui-{os.getpid()}"
    assert os.environ["FORCE_TMUX_SOCKET"] == f"forcetest-smoke-{os.getpid()}"
    assert (r.run_dir / "tmux-ui.conf").is_file()
    assert os.environ["FORCE_USAGE_FILE"] == str(tmp_path / "live-state" / "usage.json")
    assert os.environ["FORCE_STATE"] == str(r.run_dir / "state"), "everything else stays the run's own"


def _splitcockpit_script(tmp, proto):
    """A launcher for the split smoke test: `run` starts an interactive shell, `nested` attaches to the host
    tmux (creating the nested client), sidebar/header sleep, `watch` serves hello + the host's snapshot."""
    lines = [
        f"#!{sys.executable}",
        "import json, os, sys, time",
        "argv = sys.argv[1:]",
        "if not argv:",
        "    sys.exit(2)",
        "cmd = argv[0]",
        "if cmd in ('sidebar', 'header'):",
        "    os.execvp('sleep', ['sleep', '1000000'])",
        "if cmd == 'nested':",
        "    sock = os.environ.get('FORCE_TMUX_SOCKET', '')",
        "    os.execvp('tmux', ['tmux', '-L', sock, 'attach', '-t', '=' + argv[1]])",
        "if cmd == 'run':",
        "    os.environ['FORCE_SESSION'] = argv[1]",
        "    os.execvpe('bash', ['bash', '-l'], dict(os.environ, TMUX='', TMUX_PANE=''))",
        f"if cmd == 'watch':",
        "    snap = os.path.join(os.environ['FORCE_STATE'], 'snapshot.json')",
        "    reg = os.path.join(os.environ['FORCE_HOME'], 'registry.json')",
        "    out = sys.stdout",
        "    out.write('{\"hello\":{\"proto\":%d,\"version\":\"x\",\"host\":\"h\",\"ui\":\"split\"}}\\n' "
        f"% {proto})",
        "    out.flush()",
        "    last = None",
        "    end = time.monotonic() + 25",
        "    while time.monotonic() < end:",
        "        try:",
        "            data = open(snap).read()",
        "            rdata = open(reg).read()",
        "        except OSError:",
        "            time.sleep(0.2); continue",
        "        key = (len(data), len(rdata))",
        "        if key != last:",
        "            try:",
        "                snapobj = json.loads(data)",
        "                regobj = json.loads(rdata)",
        "            except ValueError:",
        "                time.sleep(0.2); continue",
        "            out.write(json.dumps({'snapshot': snapobj, 'registry': regobj}, "
        "                               separators=(',', ':')) + '\\n')",
        "            out.flush()",
        "            last = key",
        "        time.sleep(0.2)",
        "    sys.exit(0)",
        "sys.exit(2)",
    ]
    script = tmp / "splitcockpit"
    script.write_text("\n".join(lines))
    script.chmod(0o755)
    return script


def test_split_flow_real(env, monkeypatch, tmp_path):
    """`Run.split_flow` for real: a shell session in the host, shown through a ui session's nested client
    (keys reach it), and `force watch` serving that host's snapshot. Claude/OpenCode flows are skipped."""
    monkeypatch.setenv("FORCE_SMOKE_FORCE", str(_splitcockpit_script(tmp_path, remote.PROTO)))
    monkeypatch.setattr(smoke.Run, "claude_flow", lambda self: None)
    monkeypatch.setattr(smoke.Run, "opencode_flow", lambda self: None)
    monkeypatch.setattr(smoke, "versions", lambda paths: {"force": "r", "claude": "2.0.0",
                                                        "opencode": "1.0.0"})
    log = io.StringIO()
    result = smoke.run(env.paths, env.cfg, log=log)
    split = [c for c in result["checks"] if c["name"].startswith("split:")]
    assert len(split) == 2, [c["name"] for c in result["checks"]]
    assert all(c["ok"] for c in split), [(c["name"], c["detail"]) for c in result["checks"]]
    assert result["ok"]
    # the host and ui smoke servers are gone after cleanup
    from force.tmux import Tmux
    assert not Tmux(f"forcetest-smoke-{os.getpid()}", None).server_running()
    assert not Tmux(f"forcetest-smoke-ui-{os.getpid()}", None).server_running()


class _Dialog:
    """Claude's trust dialog: the first Down is lost (drawn too late), the second selects Yes."""
    def __init__(self, lose=1):
        self.lose, self.yes, self.keys = lose, False, []

    def key(self, name, k):
        self.keys.append(k)
        if k == "Down":
            if self.lose:
                self.lose -= 1
            else:
                self.yes = True

    def screen(self, name):
        return "Quick safety check | No, exit | ❯ Yes, I trust this folder" if self.yes else \
            "Quick safety check | ❯ No, exit | Yes, I trust this folder"


def test_trust_presses_enter_only_once_yes_is_selected(monkeypatch):
    monkeypatch.setattr(smoke.time, "sleep", lambda s: None)
    clock = iter(range(0, 10_000))
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(clock) * 0.5)
    d = _Dialog(lose=1)
    smoke.Run.trust(d, "s", "move")
    assert d.keys == ["Down", "Down", "Enter"], "Enter never lands on 'No, exit'"


def test_trust_gives_up_without_pressing_enter(monkeypatch):
    monkeypatch.setattr(smoke.time, "sleep", lambda s: None)
    clock = iter(range(0, 10_000))
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(clock) * 0.5)
    d = _Dialog(lose=99)
    with pytest.raises(smoke.SmokeFail):
        smoke.Run.trust(d, "s", "move")
    assert "Enter" not in d.keys


# --- the plan's usage limit and account changes (SPEC §37) -------------------------------------------------------
LIMIT_SCREEN = "❯ Reply with exactly PONG | ⎿ You've hit your session limit · resets 3pm (Europe/Paris) | ❯"


@pytest.mark.parametrize("text, hit", [
    (LIMIT_SCREEN, True),
    ("You've hit your 5-hour limit", True),
    ("YOU'VE HIT YOUR WEEKLY LIMIT · resets Mon", True),
    ("Usage limit reached · continuing automatically at 3pm", True),
    ("Claude usage limit reached|1791561000", True),
    ("no WORKING within 90 s (states: IDLE)", False),
    ("Context limit reached · /compact or /clear to continue", False),
    ("", False),
    (None, False),
])
def test_limit_text(text, hit):
    assert smoke.limit_text(text) is hit


def _limited_run(tmp_path, screen, usage=None):
    from types import SimpleNamespace
    state = tmp_path / "live-state"
    state.mkdir(exist_ok=True)
    if usage is not None:
        (state / "usage.json").write_text(json.dumps(usage))
    r = smoke.Run.__new__(smoke.Run)
    r.live = SimpleNamespace(state=state, claude_dir=tmp_path / "claude")
    r.checks, r.log = [], io.StringIO()
    r.screen = lambda name: screen
    return r


def _no_reply():
    raise smoke.SmokeFail("a turn (working): no WORKING within 90 s (states: IDLE)")


def test_a_limit_screen_stops_the_check_without_failing(tmp_path):
    r = _limited_run(tmp_path, LIMIT_SCREEN)
    assert r.check("claude: turn", _no_reply) is False, "the flow stops there"
    assert r.checks[0]["ok"] is None and r.checks[0]["detail"].startswith(smoke.USAGE_LIMIT)
    assert smoke.reason(r.checks) is None, "not a failure"
    # without the words on screen it is a plain failure
    r = _limited_run(tmp_path, "❯ PONG")
    r.check("claude: turn", _no_reply)
    assert r.checks[0]["ok"] is False and smoke.reason(r.checks) == "Claude Code: no reply"
    # an OpenCode check is never a Claude usage limit
    r = _limited_run(tmp_path, LIMIT_SCREEN)
    r.check("opencode: turn", _no_reply)
    assert r.checks[0]["ok"] is False


def test_live_usage_at_100_is_the_second_signal(tmp_path):
    future = time.time() + 3600
    r = _limited_run(tmp_path, "❯", usage={"five_hour": {"used_percentage": 100, "resets_at": future}})
    r.check("claude: turn", _no_reply)
    assert r.checks[0]["ok"] is None
    r = _limited_run(tmp_path, "❯", usage={"five_hour": {"used_percentage": 100, "resets_at": time.time() - 5}})
    r.check("claude: turn", _no_reply)
    assert r.checks[0]["ok"] is False, "a window that has reset is no limit"


def test_at_limit():
    now = 1000.0
    assert smoke.at_limit(None, now) is None
    assert smoke.at_limit({"five_hour": {"used_percentage": 99.5, "resets_at": 2000}}, now) is None
    assert smoke.at_limit({"five_hour": {"used_percentage": 100.0, "resets_at": 2000},
                           "seven_day": {"used_percentage": 100.0, "resets_at": 9000}}, now) == 9000
    assert smoke.at_limit({"five_hour": {"used_percentage": 100.0, "resets_at": 900}, "seven_day": None}, now) is None


def _fake_run(monkeypatch, env, claude_flow):
    monkeypatch.setattr(smoke.Run, "setup", lambda self: None)
    monkeypatch.setattr(smoke.Run, "cleanup", lambda self: None)
    monkeypatch.setattr(smoke.Run, "claude_flow", claude_flow)
    monkeypatch.setattr(smoke.Run, "opencode_flow", lambda self: None)
    monkeypatch.setattr(smoke.Run, "split_flow", lambda self: None)
    monkeypatch.setattr(smoke, "versions", lambda paths: V)


def test_a_run_stopped_by_the_limit_is_ok_null(env, monkeypatch):
    from force import account
    from force.daemon import Daemon
    from force.doctor import check, run_doctor
    from force.state import read_snapshot
    (env.paths.claude_dir / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": "acc-a", "organizationUuid": "org"}}))
    reset = int(time.time()) + 3600
    (env.paths.state / "usage.json").write_text(json.dumps(
        {"five_hour": {"used_percentage": 100, "resets_at": reset}}))

    def claude_flow(self):
        self.screen = lambda name: LIMIT_SCREEN
        self.check("claude: start", lambda: None)
        self.check("claude: turn", _no_reply)
    _fake_run(monkeypatch, env, claude_flow)
    res = smoke.run(env.paths, env.cfg, log=io.StringIO())
    assert res["ok"] is None and res["reason"] == smoke.USAGE_LIMIT
    assert res["limit_resets_at"] == reset
    assert res["account"] == account.current(env.paths.claude_dir) and len(res["account"]) == 16
    last = smoke.read_last(env.paths.state / "smoke")
    assert last["ok"] is None
    # no ✖ in the sidebar (the snapshot's ok is not false), no doctor problem, an info line
    cockpit = env.make_cockpit()
    Daemon(cockpit).tick()
    snap = read_snapshot(env.paths.snapshot).smoke
    assert snap["ok"] is None and snap["reason"] == smoke.USAGE_LIMIT
    assert not [p for p in check(cockpit, systemd=False) if p.code == "self-test"]
    lines = []
    run_doctor(cockpit, out=lines.append, systemd=False)
    assert any("usage limit reached" in ln and not ln.startswith(("✓", "✗")) for ln in lines)
    # --last says so and exits 0
    assert smoke.command(env.paths, env.cfg, if_due=False, last=True) == 0


def test_a_real_failure_wins_over_a_limit(env, monkeypatch):
    def claude_flow(self):
        self.screen = lambda name: LIMIT_SCREEN
        self.check("claude: turn", _no_reply)
        self.checks.append({"name": "opencode: move", "ok": False, "s": 1, "detail": "x"})
    _fake_run(monkeypatch, env, claude_flow)
    res = smoke.run(env.paths, env.cfg, log=io.StringIO())
    assert res["ok"] is False and res["reason"] == "OpenCode: move failed"


def test_due_after_a_limit_run_and_on_an_account_change():
    now = 1_000_000.0
    limited = {"ok": None, "at": now - 600, "versions": V, "account": "aaaa", "limit_resets_at": now + 100,
               "reason": smoke.USAGE_LIMIT}
    assert smoke.due(limited, V, now, account="aaaa") is None, "not before the reset"
    assert smoke.due(limited, V, now + 100, account="aaaa") == "usage limit reset"
    assert smoke.due(limited, V, now, account="bbbb") == "account changed"
    unknown = dict(limited, limit_resets_at=None)
    assert smoke.due(unknown, V, now) is None
    assert smoke.due(dict(unknown, at=now - smoke.RETRY_FAILED_S), V, now) == "retry after the usage limit"
    # a limit run is not a failure for the failure rules
    assert smoke.due(dict(limited, at=now - smoke.RETRY_FAILED_S + 10), V, now, logged_in=lambda: True) is None
    # the account rule: a failed run from another account (its ✖ clears once the new run passes)
    failed = {"ok": False, "at": now - 60, "versions": V, "account": "aaaa",
              "checks": [{"name": "claude: turn", "ok": False, "detail": "no WORKING"}]}
    assert smoke.due(failed, V, now, account="bbbb") == "account changed"
    assert smoke.due(failed, V, now, account="aaaa") is None
    assert smoke.due(failed, V, now, account=None) is None, "logged out: no account to compare"
    assert smoke.due({"ok": True, "at": now - 60, "versions": V}, V, now, account="bbbb") is None, \
        "a run from before the account was recorded"


def _if_due(env, monkeypatch, last, usage=None):
    base = env.paths.state / "smoke"
    base.mkdir(exist_ok=True)
    (base / "last.json").write_text(json.dumps(last))
    if usage is not None:
        (env.paths.state / "usage.json").write_text(json.dumps(usage))
    ran = []
    monkeypatch.setattr(smoke, "versions", lambda paths: V)
    monkeypatch.setattr(smoke, "run", lambda *a, **k: (ran.append(1), {"ok": True})[1])
    rc = smoke.command(env.paths, env.cfg, if_due=True, last=False)
    return rc, ran, (base / "last.json").read_text()


def test_a_scheduled_run_is_skipped_at_the_limit(env, monkeypatch, capsys):
    old = {"ok": True, "at": time.time() - smoke.DUE_AFTER_S - 1, "versions": V}
    rc, ran, after = _if_due(env, monkeypatch, old, usage={
        "five_hour": {"used_percentage": 40, "resets_at": int(time.time()) + 600},
        "seven_day": {"used_percentage": 100, "resets_at": int(time.time()) + 86400}})
    assert rc == 0 and ran == [], "skipped while a window is at 100 %"
    assert json.loads(after) == old, "and nothing recorded"
    assert "skipped: usage limit reached" in capsys.readouterr().out
    rc, ran, _ = _if_due(env, monkeypatch, old, usage={
        "five_hour": {"used_percentage": 100, "resets_at": int(time.time()) - 1}})
    assert ran == [1], "a window that has reset does not hold it back"


def test_an_account_change_makes_it_due(env, monkeypatch):
    (env.paths.claude_dir / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"accountUuid": "acc-b", "organizationUuid": "org"}}))
    recent = {"ok": False, "at": time.time() - 60, "versions": V, "account": "0123456789abcdef",
              "checks": [{"name": "claude: turn", "ok": False, "detail": "no WORKING"}]}
    rc, ran, _ = _if_due(env, monkeypatch, recent)
    assert ran == [1]


def test_start_smoke_uses_the_installed_schedule(monkeypatch):
    from force import services
    from subprocess import CompletedProcess
    calls = []

    def fake(enabled):
        def run(argv, **kw):
            calls.append(argv)
            if argv[:3] == ["systemctl", "--user", "is-enabled"]:
                return CompletedProcess(argv, 0 if enabled else 1, "enabled\n" if enabled else "disabled\n", "")
            return CompletedProcess(argv, 0, "", "")
        return run
    monkeypatch.setattr(services, "backend", lambda env=None, run=None: "systemd")
    assert services.start_smoke(run=fake(True)) is True
    assert calls[-1] == ["systemctl", "--user", "start", "--no-block", "force-smoke.service"]
    calls.clear()
    assert services.start_smoke(run=fake(False)) is False
    assert all("start" not in c for c in calls), "no self-test schedule installed: nothing started"
    monkeypatch.setattr(services, "backend", lambda env=None, run=None: None)
    assert services.start_smoke(run=fake(True)) is False
