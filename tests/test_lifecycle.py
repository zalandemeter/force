import shlex
import sys
import json
import os
import signal
import time

import pytest

from force.registry import RegistryError, load, locked
from force.state import load_seen


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def current_status(cockpit, name):
    pane = cockpit.agent_pane(name)
    if pane is None or not pane.pid:
        return None
    p = cockpit.paths.claude_dir / "sessions" / f"{pane.pid}.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def ctl(cockpit, name, **kw):
    pane = cockpit.agent_pane(name)
    if pane is None or not pane.pid:
        return
    p = cockpit.paths.claude_dir / "sessions" / f"{pane.pid}.ctl"
    p.write_text(json.dumps(kw))


def make_entry(paths, name):
    with locked(paths.home) as reg:
        return {k: v for k, v in reg.add(name, _uuid(), "/tmp").__dict__.items()
                if k != "extra"}


def _uuid():
    import uuid
    return str(uuid.uuid4())


def entry(paths, name):
    return load(paths.registry).get(name)


def test_pane_command_quoting(env):
    cockpit = env.make_cockpit()
    cmd = cockpit.pane_command("portal")
    # `force run` itself scrubs the env and re-execs through `bash -lic` (cli.run_command).
    # exec: the pane's process is Force (then the agent) whatever the user's shell is (dash would stay in between)
    assert shlex.split(cmd) == ["exec", cockpit.cockpit_bin, "run", "portal"]


def test_create_first_start_updates_registry_and_status(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert cockpit.tmux.has_session("portal")
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    st = current_status(cockpit, "portal")
    assert st["sessionId"] == e.session_id


def test_restart_resumes_same_session(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    first = current_status(cockpit, "portal")
    sid = first["sessionId"]
    # transcript now exists so restart will --resume
    cockpit.restart("portal")
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    assert current_status(cockpit, "portal")["sessionId"] == sid


def test_restart_creates_when_missing(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    import subprocess
    subprocess.run(["tmux", "-L", env.socket, "kill-server"], capture_output=True)
    cockpit.restart("portal")
    assert cockpit.tmux.has_session("portal")
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)


def test_restart_many_restarts_together(env):
    """`restart --all`: every session is signalled at once, so three take about as long as one."""
    cockpit = env.make_cockpit()
    names = ["a", "b", "c"]
    for n in names:
        with locked(env.paths.home) as reg:
            e = reg.add(n, _uuid(), "/tmp")
        cockpit.create(e)
    for n in names:
        assert wait_for(lambda n=n: current_status(cockpit, n) is not None)
    before = {n: cockpit.agent_pane(n).pid for n in names}
    t0 = time.monotonic()
    assert cockpit.restart_many(names + ["missing"]) == []
    assert time.monotonic() - t0 < 5.0
    for n in names:
        assert wait_for(lambda n=n: cockpit.agent_pane(n) is not None and not cockpit.agent_pane(n).dead
                        and cockpit.agent_pane(n).pid != before[n])


def test_restart_shell_does_not_wait_out_sigterm(env):
    """An interactive bash ignores SIGTERM; a shell session is stopped with SIGHUP instead of sitting
    out the 15 s timeout."""
    cockpit = env.make_cockpit()
    # like the live `bash -l` at its prompt: SIGTERM ignored (the test env's own shell stand-in is sleep)
    cockpit.pane_command = lambda name: "exec sh -c 'trap \"\" TERM; exec sleep 1000000'"
    with locked(env.paths.home) as reg:
        e = reg.add("sh", _uuid(), "/tmp")
        e.extra["kind"] = "shell"
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("sh") is not None and cockpit.agent_pane("sh").pid)
    pid0 = cockpit.agent_pane("sh").pid
    if sys.platform.startswith("linux"):     # the mask is in /proc; macOS's ps does not show it
        assert wait_for(lambda: _sig_ignored(cockpit.agent_pane('sh').pid) & (1 << 14))   # TERM ignored
    t0 = time.monotonic()
    cockpit.restart("sh")
    assert time.monotonic() - t0 < 5.0
    assert wait_for(lambda: cockpit.agent_pane("sh") is not None and not cockpit.agent_pane("sh").dead
                    and cockpit.agent_pane("sh").pid != pid0)


def test_archive_copies_transcript_and_unarchive_missing_only(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    sid = current_status(cockpit, "portal")["sessionId"]

    cockpit.archive("portal")
    assert not cockpit.tmux.has_session("portal")
    assert entry(env.paths, "portal").archived
    # transcript copied to archive/<sid>/<project>/
    archive_dir = env.paths.archive / sid
    jsonls = list(archive_dir.rglob("*.jsonl"))
    assert jsonls, "no transcript archived"
    assert jsonls[0].name == f"{sid}.jsonl"

    # delete the original so unarchive must restore it
    proj = env.paths.claude_dir / "projects" / "-tmp"
    orig = proj / f"{sid}.jsonl"
    assert orig.exists()
    orig.unlink()

    returned = cockpit.unarchive("portal")
    assert returned.name == "portal"
    assert not entry(env.paths, "portal").archived
    assert entry(env.paths, "portal").archived_at is None
    assert orig.exists(), "transcript not restored"
    # created a new session
    assert cockpit.tmux.has_session("portal")


def test_unarchive_unknown_raises(env):
    cockpit = env.make_cockpit()
    with pytest.raises(RegistryError):
        cockpit.unarchive("nope")


def test_unarchive_non_archived_raises(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        reg.add("portal", _uuid(), "/tmp")
    with pytest.raises(RegistryError):
        cockpit.unarchive("portal")


def test_rename_live_shell_immediate(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("sh", _uuid(), "/tmp")
        e.extra["kind"] = "shell"
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("sh") is not None)
    cockpit.rename("sh", "newname")
    assert entry(env.paths, "sh") is None
    assert entry(env.paths, "newname") is not None
    assert entry(env.paths, "newname").pending_name is None
    assert cockpit.tmux.has_session("newname")


def test_rename_live_sets_pending(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    assert "as soon as it is idle" in cockpit.rename("portal", "newname")
    assert entry(env.paths, "portal").pending_name == "newname"
    assert entry(env.paths, "newname") is None
    # the same rename again is not "taken" by its own pending name; back to the old name cancels it
    assert "already queued" in cockpit.rename("portal", "newname")
    assert "cancelled" in cockpit.rename("portal", "portal")
    assert entry(env.paths, "portal").pending_name is None
    # another session's pending name stays taken
    cockpit.rename("portal", "newname")
    with locked(env.paths.home) as reg:
        reg.add("other", _uuid(), "/tmp")
    with pytest.raises(RegistryError):
        cockpit.rename("other", "newname")


def test_rename_down_immediate(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    # kill the claude process -> pane dead -> down
    pane = cockpit.agent_pane("portal")
    os.kill(pane.pid, signal.SIGKILL)
    wait_for(lambda: cockpit.agent_pane("portal") is not None and cockpit.agent_pane("portal").dead)
    cockpit.rename("portal", "newname")
    assert entry(env.paths, "portal") is None
    assert entry(env.paths, "newname") is not None
    assert cockpit.tmux.has_session("newname")


def test_rename_invalid(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        reg.add("portal", _uuid(), "/tmp")
    with pytest.raises(RegistryError):
        cockpit.rename("portal", "Bad Name")


def test_apply_rename(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    with locked(env.paths.home) as reg:
        reg.get("portal").pending_name = "newname"
    new = cockpit.apply_rename("portal")
    assert new == "newname"
    assert cockpit.tmux.has_session("newname")
    assert entry(env.paths, "newname").pending_name is None


def test_sidebar_toggle_off_on(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("portal") is not None)
    # sidebar should exist by default
    assert any(p.session == "portal" and p.role == "sidebar" for p in cockpit.tmux.panes())
    assert cockpit.sidebar_toggle() is False
    assert cockpit.tmux.get_option("@force_sidebar") == "off"
    assert not any(p.session == "portal" and p.role == "sidebar" for p in cockpit.tmux.panes())
    assert cockpit.sidebar_toggle() is True
    assert any(p.session == "portal" and p.role == "sidebar" for p in cockpit.tmux.panes())


def test_concurrent_ensure_sidebar_adds_exactly_one(env):
    """The CLI toggle and the daemon both call ensure_sidebar; racing must not create two sidebars."""
    import threading
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("portal") is not None)
    assert cockpit.sidebar_toggle() is False                     # off: no sidebar
    cockpit.tmux.set_option("@force_sidebar", "on")
    others = [env.make_cockpit() for _ in range(4)]             # independent callers, like CLI + daemon
    threads = [threading.Thread(target=c.ensure_sidebar, args=("portal",)) for c in others]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sidebars = [p for p in cockpit.tmux.panes() if p.session == "portal" and p.role == "sidebar"]
    assert len(sidebars) == 1


def test_dedupe_sidebars_keeps_one_per_session(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("portal") is not None)
    claude = cockpit.agent_pane("portal")
    cockpit.tmux.run("split-window", "-h", "-b", "-l", "10", "-t", claude.pane_id, "sleep 100",
                 ";", "set-option", "-p", "@force_role", "sidebar")
    assert len([p for p in cockpit.tmux.panes() if p.role == "sidebar"]) == 2
    assert cockpit.dedupe_sidebars() == 1
    assert len([p for p in cockpit.tmux.panes() if p.role == "sidebar"]) == 1
    assert cockpit.agent_pane("portal") is not None


def _geom(cockpit, name):
    """role -> (left, top, width, height) of the panes in the session's first window."""
    out = cockpit.tmux.run("list-panes", "-t", f"={name}:0", "-F",
                       "#{@force_role} #{pane_left} #{pane_top} #{pane_width} #{pane_height} #{window_height}")
    g = {}
    for line in out.splitlines():
        role, l, t, w, h, wh = line.split()
        g[role] = (int(l), int(t), int(w), int(h), int(wh))
    return g


def test_layout_full_height_sidebar_header_above_agent(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("portal") is not None)
    g = _geom(cockpit, "portal")
    assert set(g) == {"sidebar", "header", "agent"}
    sl, st, sw, sh, wh = g["sidebar"]
    assert (sl, st, sh) == (0, 0, wh)                 # sidebar: left edge, top row, full height
    hl, ht, hw, hh, _ = g["header"]
    cl, ctop, cw, ch, _ = g["agent"]
    assert hh == 2 and ht == 0 and hl > sw - 1        # header: top, 2 rows, right of the sidebar
    assert cl == hl and ctop == hh + 1                # agent: under the header (1 border row)


def test_sidebar_toggle_script_hides_and_shows_without_restarts(env):
    import subprocess
    from pathlib import Path
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("portal") is not None)
    script = Path(__file__).resolve().parent.parent / "share" / "sidebar-toggle.sh"
    sock = cockpit.tmux.run("display", "-p", "#{socket_path}")
    pids = sorted(p.pid for p in cockpit.tmux.panes())
    subprocess.run([str(script), sock, ""], check=True)          # hide
    cockpit.tmux.invalidate()
    g = _geom(cockpit, "portal")
    assert "sidebar" not in g                                    # parked in force-stash
    assert g["agent"][0] == 0 and g["header"][0] == 0           # header + agent span full width
    assert cockpit.tmux.get_option("@force_sidebar_hidden") == "1"
    subprocess.run([str(script), sock, ""], check=True)          # show
    cockpit.tmux.invalidate()
    g = _geom(cockpit, "portal")
    assert g["sidebar"][0] == 0 and g["sidebar"][3] == g["sidebar"][4]   # back, full height
    assert g["sidebar"][2] == 29
    assert sorted(p.pid for p in cockpit.tmux.panes()) == pids       # nothing restarted


def test_new_session_while_hidden_parks_its_sidebar(env):
    cockpit = env.make_cockpit()
    cockpit.tmux.start_server()
    cockpit.tmux.set_option("@force_sidebar_hidden", "1")
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("portal") is not None)
    g = _geom(cockpit, "portal")
    assert "sidebar" not in g and "header" in g
    assert any(p.session == "portal" and p.role == "sidebar" for p in cockpit.tmux.panes())


def test_ensure_never_creates_on_a_failed_listing(env, monkeypatch):
    """Regression: a failed `list-panes` used to look like 'no sidebar' and created duplicates."""
    from force.tmux import TmuxError
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: cockpit.agent_pane("portal") is not None)
    before = len(cockpit.tmux.panes())
    monkeypatch.setattr(cockpit.tmux, "panes", lambda strict=False: (_ for _ in ()).throw(TmuxError("boom"))
                        if strict else [])
    with pytest.raises(TmuxError):
        cockpit.ensure_sidebar("portal")
    with pytest.raises(TmuxError):
        cockpit.ensure_header("portal")
    monkeypatch.undo()
    assert len(cockpit.tmux.panes()) == before


def test_rapid_concurrent_toggles_keep_each_sidebar_in_its_own_session(env):
    """Regression: overlapping toggles raced, and break-pane without a target moved every sidebar into
    one session's stash window."""
    import subprocess
    from pathlib import Path
    cockpit = env.make_cockpit()
    for n in ("aa", "bb", "cc"):
        with locked(env.paths.home) as reg:
            e = reg.add(n, _uuid(), "/tmp")
        cockpit.create(e)
        assert wait_for(lambda n=n: cockpit.agent_pane(n) is not None)
    script = Path(__file__).resolve().parent.parent / "share" / "sidebar-toggle.sh"
    sock = cockpit.tmux.run("display", "-p", "#{socket_path}")
    procs = [subprocess.Popen([str(script), sock, ""]) for _ in range(5)]   # odd -> hidden
    for p in procs:
        p.wait()
    cockpit.tmux.invalidate()
    for n in ("aa", "bb", "cc"):
        sb = [p for p in cockpit.tmux.panes() if p.role == "sidebar" and p.session == n]
        assert len(sb) == 1                                   # still its own session's sidebar
        assert "sidebar" not in _geom(cockpit, n)                 # parked, not in the agent window
    assert cockpit.tmux.get_option("@force_sidebar_hidden") == "1"
    subprocess.run([str(script), sock, ""], check=True)       # show
    cockpit.tmux.invalidate()
    for n in ("aa", "bb", "cc"):
        g = _geom(cockpit, n)
        assert g["sidebar"][0] == 0 and g["sidebar"][2] == 29 and g["sidebar"][3] == g["sidebar"][4]


def test_delete_live_session_stops_it_and_removes_everything_of_cockpit(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
        reg.add("other", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    sid = current_status(cockpit, "portal")["sessionId"]
    pid = cockpit.agent_pane("portal").pid
    cockpit.delete("portal")
    assert not cockpit.tmux.has_session("portal")
    assert entry(env.paths, "portal") is None and entry(env.paths, "other") is not None
    assert wait_for(lambda: not _alive(pid))
    # Claude Code's own transcript stays
    assert (env.paths.claude_dir / "projects" / "-tmp" / f"{sid}.jsonl").exists()


def test_delete_archived_removes_cockpit_copy(env):
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("old", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "old") is not None)
    cockpit.archive("old")
    sid = entry(env.paths, "old").session_id
    assert (env.paths.archive / sid).is_dir()
    cockpit.delete("old")
    assert entry(env.paths, "old") is None
    assert not (env.paths.archive / sid).exists()


def test_delete_unknown_raises(env):
    with pytest.raises(RegistryError):
        env.make_cockpit().delete("nope")


def test_archive_flags_archiving_until_archived(env, monkeypatch):
    """While archive() stops the session the registry says "archiving" (the sidebar shows that instead of
    the restart feedback the stop would trigger); the flag is gone once archived, or after a failure."""
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    seen = []
    real_stop = cockpit.stop_agent
    monkeypatch.setattr(cockpit, "stop_agent", lambda n, *a, **k: (seen.append(entry(env.paths, n).extra.get("archiving")), real_stop(n, *a, **k)))
    cockpit.archive("portal")
    assert seen == [True]
    after = entry(env.paths, "portal")
    assert after.archived and "archiving" not in after.extra

    # a failure clears the flag and leaves the session live
    with locked(env.paths.home) as reg:
        reg.add("docs", _uuid(), "/tmp")

    def boom(n, *a, **k):
        raise OSError("stop failed")
    monkeypatch.setattr(cockpit, "stop_agent", boom)
    import pytest
    with pytest.raises(OSError):
        cockpit.archive("docs")
    docs = entry(env.paths, "docs")
    assert not docs.archived and "archiving" not in docs.extra


def _cmdline(cockpit, name):
    from force import procinfo
    return procinfo.cmdline(cockpit.agent_pane(name).pid) or []


def _sig_ignored(pid):
    """The process's ignored-signal mask (bit n-1 = signal n), from /proc (Linux)."""
    return int(open(f"/proc/{pid}/status").read().split("SigIgn:")[1].split()[0], 16)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _settings(cockpit, name):
    argv = _cmdline(cockpit, name)
    return json.loads(argv[argv.index("--settings") + 1])


def test_move_claude_session_keeps_conversation_and_memory(env, tmp_path):
    """SPEC §7.7: the session continues in the new folder (same session id, resumed there), pinned to
    the auto memory of the project it ran in so far; a second move keeps that memory."""
    from force.claude import find_transcript
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for d in (a, b, c):
        d.mkdir()
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), str(a))
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    sid = current_status(cockpit, "portal")["sessionId"]
    memory = find_transcript(env.paths.claude_dir, sid).parent / "memory"
    assert "autoMemoryDirectory" not in _settings(cockpit, "portal")

    cockpit.move("portal", str(b))
    after = entry(env.paths, "portal")
    assert after.cwd == str(b)
    assert after.extra["memory_dir"] == str(memory)
    assert "moving" not in after.extra and after.extra["moved_at"] > 0
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("cwd") == str(b))
    assert current_status(cockpit, "portal")["sessionId"] == sid
    assert "--resume" in _cmdline(cockpit, "portal")
    assert _settings(cockpit, "portal")["autoMemoryDirectory"] == str(memory)

    cockpit.move("portal", str(c))
    assert entry(env.paths, "portal").extra["memory_dir"] == str(memory), "the first project's memory stays"
    assert wait_for(lambda: (current_status(cockpit, "portal") or {}).get("cwd") == str(c))


def test_move_respects_a_global_memory_directory(env, tmp_path):
    (env.paths.claude_dir / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/mem"}))
    b = tmp_path / "b"
    b.mkdir()
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("portal", _uuid(), "/tmp")
    cockpit.create(e)
    assert wait_for(lambda: current_status(cockpit, "portal") is not None)
    cockpit.move("portal", str(b))
    assert "memory_dir" not in entry(env.paths, "portal").extra


def test_move_refuses_missing_folder_and_archived(env, tmp_path):
    from force.lifecycle import MoveError
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        reg.add("portal", _uuid(), "/tmp")
        arch = reg.add("old", _uuid(), "/tmp")
        arch.archived = True
    with pytest.raises(MoveError):
        cockpit.move("portal", str(tmp_path / "nope"))
    assert entry(env.paths, "portal").cwd == "/tmp"
    with pytest.raises(RegistryError):
        cockpit.move("old", str(tmp_path))
    with pytest.raises(RegistryError):
        cockpit.move("ghost", str(tmp_path))


def test_move_opencode_rehomes_the_session(env, tmp_path, monkeypatch):
    """An OpenCode session is exported in the old folder and imported in the new one before it restarts
    there; a failed import leaves it in the old folder."""
    from force import opencode as oc
    from force.lifecycle import MoveError
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    log = tmp_path / "oc.log"
    fake = tmp_path / "opencode"
    fake.write_text(f"""#!/bin/sh
echo "$1 $2 $(pwd)" >> {log}
if [ "$1" = export ]; then echo '{{"info": {{"id": "'$2'"}}, "messages": []}}'; exit 0; fi
if [ "$1" = import ]; then [ -f "$2" ] || exit 3; grep -q ses_1 "$2" || exit 4; exit ${{FAKE_IMPORT_RC:-0}}; fi
""")
    fake.chmod(0o755)
    monkeypatch.setattr(oc, "installed", lambda: str(fake))
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("oc", _uuid(), str(a))
        e.extra["kind"] = "opencode"
        e.extra["oc_session"] = "ses_1"
    stops, restarts = [], []
    monkeypatch.setattr(cockpit, "stop_agent", lambda n, *x, **k: stops.append(n))
    monkeypatch.setattr(cockpit, "restart", lambda n: restarts.append((n, entry(env.paths, n).cwd)))

    cockpit.move("oc", str(b))
    lines = log.read_text().splitlines()
    assert lines[0] == f"export ses_1 {a}"
    assert lines[1].startswith("import ") and lines[1].endswith(f" {b}")
    assert stops == ["oc"] and restarts == [("oc", str(b))]
    assert entry(env.paths, "oc").cwd == str(b)
    assert "memory_dir" not in entry(env.paths, "oc").extra
    assert not list(env.paths.state.glob("*.json")), "the export file is removed"

    monkeypatch.setenv("FAKE_IMPORT_RC", "1")
    stops.clear(); restarts.clear()
    with pytest.raises(MoveError):
        cockpit.move("oc", str(a))
    assert entry(env.paths, "oc").cwd == str(b), "failed move: stays where it was"
    assert "moving" not in entry(env.paths, "oc").extra
    assert restarts == [("oc", str(b))], "restarted in its old folder"


def test_move_carries_the_transcript(env, tmp_path, monkeypatch):
    """The transcript (and its sibling dir) moves to the new folder's project: a never-used one is found by
    `claude --resume` only there, and Claude's own picker in the new folder lists a moved conversation."""
    from force.claude import project_slug
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    cockpit = env.make_cockpit()
    monkeypatch.setattr(cockpit, "stop_agent", lambda n, *x, **k: None)
    monkeypatch.setattr(cockpit, "restart", lambda n: None)
    projects = env.paths.claude_dir / "projects"
    with locked(env.paths.home) as reg:
        new = reg.add("fresh", _uuid(), str(a))
        old = reg.add("talked", _uuid(), str(a))
    src = projects / project_slug(str(a))
    src.mkdir(parents=True)
    (src / f"{new.session_id}.jsonl").write_text('{"type":"custom-title"}\n{"type":"permission-mode"}\n')
    (src / new.session_id).mkdir()
    (src / f"{old.session_id}.jsonl").write_text('{"type":"custom-title"}\n{"type":"user","message":{}}\n')

    cockpit.move("fresh", str(b))
    dst = projects / project_slug(str(b))
    assert (dst / f"{new.session_id}.jsonl").is_file() and (dst / new.session_id).is_dir()
    assert not (src / f"{new.session_id}.jsonl").exists()
    assert entry(env.paths, "fresh").extra["memory_dir"] == str(src / "memory"), "memory stays the first project's"

    # a conversation goes along too (Claude's own /resume picker in the new folder lists it)
    (src / old.session_id).mkdir()
    (src / old.session_id / "agent.jsonl").write_text("x\n")
    (dst / old.session_id).mkdir()
    (dst / old.session_id / "other.jsonl").write_text("y\n")
    cockpit.move("talked", str(b))
    assert (dst / f"{old.session_id}.jsonl").read_text().count('"user"') == 1
    assert not (src / f"{old.session_id}.jsonl").exists() and not (src / old.session_id).exists()
    assert sorted(p.name for p in (dst / old.session_id).iterdir()) == ["agent.jsonl", "other.jsonl"]


def test_create_that_loses_the_race_to_the_daemon_is_not_an_error(env, monkeypatch):
    """`force new` and the daemon's tick can both create a new entry's session; the second new-session gets tmux's
    "duplicate session". The session is there, so the loser returns quietly (it failed `force new` on macOS CI)."""
    from force.tmux import TmuxError
    cockpit = env.make_cockpit()
    with locked(env.paths.home) as reg:
        e = reg.add("raced", "00000000-0000-4000-8000-000000000001", "/tmp")
    real_has = cockpit.tmux.has_session
    calls = {"n": 0}

    def has_session(name):              # the check before new-session misses the daemon's session ...
        calls["n"] += 1
        return False if calls["n"] == 1 else real_has(name)
    monkeypatch.setattr(cockpit.tmux, "has_session", has_session)
    cockpit.create(e)                   # ... the daemon wins the race
    monkeypatch.setattr(cockpit.tmux, "new_session",
                        lambda *a, **k: (_ for _ in ()).throw(TmuxError("duplicate session: raced")))
    calls["n"] = 0
    cockpit.create(e)                   # the loser: no exception
    monkeypatch.setattr(cockpit.tmux, "new_session",
                        lambda *a, **k: (_ for _ in ()).throw(TmuxError("no server")))
    calls["n"] = 0
    with pytest.raises(TmuxError):
        cockpit.create(e)               # any other failure still raises
