import os
import subprocess
import time

from force.tmux import Tmux, Pane


def run(t, *args, check=False):
    return subprocess.run([t.binary, "-L", t.socket] + list(args),
                       capture_output=True, text=True, timeout=10)


def test_new_session_and_panes(env):
    t = env.tmux
    pid = t.new_session("ab", 80, 24, "/tmp", "sleep 100", env={"FORCE_SESSION": "ab"})
    assert pid
    assert t.has_session("ab")
    panes = [p for p in t.panes() if p.session == "ab"]
    assert len(panes) == 1
    p = panes[0]
    assert p.session == "ab"
    assert p.role is None
    assert isinstance(p.pid, int) and p.pid > 0
    assert p.dead is False
    assert p.dead_status is None
    assert p.dead_signal is None


def test_roles(env):
    t = env.tmux
    pid = t.new_session("s1", 80, 24, "/tmp", "sleep 100")
    t.run("set-option", "-p", "-t", pid, "@force_role", "agent")
    panes = [p for p in t.panes() if p.session == "s1"]
    assert panes[0].role == "agent"


def test_legacy_claude_role_reads_as_agent(env):
    """A pane tagged with the pre-rebrand role "claude" is read as an agent pane (SPEC role rename)."""
    from force.tmux import LEGACY_AGENT_ROLE
    assert LEGACY_AGENT_ROLE == "claude"
    t = env.tmux
    pid = t.new_session("legacy", 80, 24, "/tmp", "sleep 100")
    t.run("set-option", "-p", "-t", pid, "@force_role", LEGACY_AGENT_ROLE)
    panes = [p for p in t.panes() if p.session == "legacy"]
    assert panes[0].role == "agent"


def test_has_session_exact_match(env):
    t = env.tmux
    t.new_session("ab", 80, 24, "/tmp", "sleep 100")
    t.new_session("a", 80, 24, "/tmp", "sleep 100")
    assert t.has_session("ab")
    assert t.has_session("a")


def test_kill_exact_match(env):
    t = env.tmux
    t.new_session("ab", 80, 24, "/tmp", "sleep 100")
    t.new_session("a", 80, 24, "/tmp", "sleep 100")
    t.kill_session("a")
    assert not t.has_session("a")
    assert t.has_session("ab")


def test_rename_exact_match(env):
    t = env.tmux
    t.new_session("ab", 80, 24, "/tmp", "sleep 100")
    t.new_session("renamea", 80, 24, "/tmp", "sleep 100")
    t.rename_session("renamea", "renamedb")
    assert t.has_session("renamedb")
    assert not t.has_session("renamea")
    assert t.has_session("ab")


def test_options_global(env):
    t = env.tmux
    t.start_server()
    assert t.get_option("@force_sidebar") is None
    t.set_option("@force_sidebar", "on")
    assert t.get_option("@force_sidebar") == "on"
    t.unset_option("@force_sidebar")
    assert t.get_option("@force_sidebar") is None


def test_options_target(env):
    t = env.tmux
    t.new_session("s1", 80, 24, "/tmp", "sleep 100")
    t.set_option("@force_next", "other", target="s1")
    out = t.run("show-options", "-t", "s1", "-qv", "@force_next", check=False)
    assert out == "other"
    t.unset_option("@force_next", target="s1")
    out2 = t.run("show-options", "-t", "s1", "-qv", "@force_next", check=False)
    assert out2 == ""


def test_clients_empty_no_attach(env):
    t = env.tmux
    t.new_session("s1", 80, 24, "/tmp", "sleep 100")
    assert t.clients() == []


def test_started_ms_and_dead_pane(env):
    t = env.tmux
    pid = t.new_session("s1", 80, 24, "/tmp", "sleep 100")
    panes = [p for p in t.panes() if p.session == "s1"]
    assert panes[0].started_ms is not None and panes[0].started_ms > 0
    t.kill_pane(panes[0].pane_id)
    # respawn with a non-zero exit
    import time
    t2 = env.tmux.new_session("s2", 80, 24, "/tmp", "true")
    time.sleep(0.3)
    deads = [p for p in t.panes() if p.session == "s2"]
    assert deads and deads[0].dead


def _counting(monkeypatch, tmux):
    import force.tmux as tmod
    calls = []
    orig = tmod.subprocess.run

    def counting(*a, **k):
        calls.append(a[0])
        return orig(*a, **k)
    monkeypatch.setattr(tmod.subprocess, "run", counting)
    return calls


def test_capture_tail_empty_on_error(env):
    assert env.tmux.capture_tail("%9999") == ""


def test_capture_tail_last_non_empty_lines(env):
    t = env.tmux
    pane = t.new_session("cap", 80, 24, "/tmp",
                       "printf 'one\\ntwo\\nthree\\nfour\\nfive\\nsix\\n'; sleep 2")
    import time
    time.sleep(0.3)
    tail = t.capture_tail(pane, 3)
    assert tail == "four\nfive\nsix"
    # blank lines within the visible screen are skipped when collecting the tail
    t2 = t.new_session("cap2", 80, 24, "/tmp",
                      "printf '\\nskip\\n\\nkeep\\nkeep2\\n\\nlast\\n'; sleep 2")
    time.sleep(0.3)
    tail2 = t.capture_tail(t2, 3)
    assert tail2 == "keep\nkeep2\nlast"


def test_cache_panes_once(env, monkeypatch):
    t = env.tmux
    t.new_session("a", 80, 24, "/tmp", "sleep 100")
    calls = _counting(monkeypatch, t)
    with t.cached():
        for _ in range(5):
            t.panes()
            assert t.has_session("a")
            t.sessions()
    lp = [c for c in calls if "list-panes" in c]
    assert len(lp) == 1


def test_cache_claude_pane_once(env, monkeypatch):
    import uuid
    from force.registry import locked
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("a", str(uuid.uuid4()), "/tmp")
    cockpit.create(e)
    calls = _counting(monkeypatch, cockpit.tmux)
    with cockpit.tmux.cached():
        for _ in range(5):
            assert cockpit.agent_pane("a") is not None
            assert cockpit.tmux.has_session("a")
    lp = [c for c in calls if "list-panes" in c]
    assert len(lp) == 1


def test_cache_mutation_refetch(env, monkeypatch):
    t = env.tmux
    t.new_session("a", 80, 24, "/tmp", "sleep 100")
    calls = _counting(monkeypatch, t)
    with t.cached():
        t.panes()
        t.new_session("b", 80, 24, "/tmp", "sleep 50")
        t.panes()
        t.panes()
    lp = [c for c in calls if "list-panes" in c]
    assert len(lp) == 2


def test_cache_off_refetches(env, monkeypatch):
    t = env.tmux
    t.new_session("a", 80, 24, "/tmp", "sleep 100")
    calls = _counting(monkeypatch, t)
    for _ in range(3):
        t.panes()
    lp = [c for c in calls if "list-panes" in c]
    assert len(lp) == 3


def test_pane_title_parsed(env):
    t = env.tmux
    t.new_session("tt", 80, 24, "/tmp", "sleep 100")
    pane = [p for p in t.panes() if p.session == "tt"][0]
    run(t, "select-pane", "-t", pane.pane_id, "-T", "Fix login bug")
    t.invalidate()
    assert [p for p in t.panes() if p.session == "tt"][0].title == "Fix login bug"
    assert Pane("%1", "s", None, 1, False, None, None, None).title is None


def test_pane_title_with_tab_joined(env, monkeypatch):
    t = env.tmux
    line = "\t".join(["s", "%1", "agent", "123", "0", "", "", "make", "Fix\tlogin\tbug"])
    monkeypatch.setattr(t, "_run_rc", lambda args: (0, line + "\n", ""))
    t.invalidate()
    p = t.panes(strict=True)[0]
    assert p.title == "Fix\tlogin\tbug"
    assert p.command == "make"


def test_pane_current_command(env):
    t = env.tmux
    t.new_session("tc", 80, 24, "/tmp", "exec sleep 100")   # exec: no shell in between, whatever the default shell
    # until the exec has happened the pane still runs the shell (a loaded machine takes a moment)
    deadline = time.time() + 5
    while True:
        pane = [p for p in t.panes() if p.session == "tc"][0]
        if pane.command == "sleep" or time.time() > deadline:
            break
        time.sleep(0.05)
    assert pane.command == "sleep"
