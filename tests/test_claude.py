import json
import os

import pytest

from force.claude import (DISABLE_AGENT_VIEW, build_argv, find_transcript, statusline_command,
                        latest_version, process_version, read_status)


def write_status(claude_dir, pid, **kw):
    data = {
        "pid": pid,
        "sessionId": "abc-123",
        "cwd": "/home",
        "version": "2.1.292",
        "name": "portal",
        "nameSource": "user",
        "status": "idle",
        "statusUpdatedAt": 1000,
    }
    data.update(kw)
    d = claude_dir / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(data))


def test_read_status_missing_file(tmp_path):
    assert read_status(tmp_path, 999999) is None


def test_read_status_live_pid(tmp_path):
    pid = os.getpid()
    write_status(tmp_path, pid)
    st = read_status(tmp_path, pid)
    assert st is not None
    assert st.pid == pid
    assert st.session_id == "abc-123"
    assert st.status == "idle"
    assert st.status_updated_ms == 1000


def test_read_status_dead_pid(tmp_path):
    # find a dead pid
    dead = None
    for cand in range(400000, 401000):
        try:
            os.kill(cand, 0)
        except ProcessLookupError:
            dead = cand
            break
    if dead is None:
        # fallback: spawn a child that exits
        import subprocess
        p = subprocess.Popen(["/bin/true"])
        p.wait()
        dead = p.pid
    write_status(tmp_path, dead)
    assert read_status(tmp_path, dead) is None


def test_read_status_not_json(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "5.json").write_text("not json")
    st = read_status(tmp_path, 5)
    # pid 5 likely not alive -> still None; ensure no exception
    assert st is None


def test_read_status_missing_session_id(tmp_path):
    pid = os.getpid()
    write_status(tmp_path, pid, sessionId=None)
    # overwrite with no sessionId
    (tmp_path / "sessions" / f"{pid}.json").write_text(json.dumps({"pid": pid}))
    assert read_status(tmp_path, pid) is None


def test_read_status_before_the_first_status(tmp_path):
    # issue #23: Claude Code writes the file before its first status; that is "not yet", never 1970
    pid = os.getpid()
    write_status(tmp_path, pid)
    data = {"pid": pid, "sessionId": "s-1", "extraKey": "x"}
    (tmp_path / "sessions" / f"{pid}.json").write_text(json.dumps(data))
    st = read_status(tmp_path, pid)
    assert st.session_id == "s-1"
    assert st.status == ""
    assert st.status_updated_ms is None


@pytest.mark.parametrize("bad", ["not-a-number", 0, -5, True, None, 1.5])
def test_read_status_unusable_status_updated(tmp_path, bad):
    pid = os.getpid()
    write_status(tmp_path, pid, statusUpdatedAt=bad)
    st = read_status(tmp_path, pid)
    assert st.status_updated_ms is None
    assert st.status == "idle"


def test_read_status_non_string_status_is_unknown(tmp_path):
    pid = os.getpid()
    write_status(tmp_path, pid, status=7)
    assert read_status(tmp_path, pid).status == "unknown"


def test_read_status_tolerates_extra_keys(tmp_path):
    pid = os.getpid()
    write_status(tmp_path, pid, bogus=["a", "b"], n=3)
    st = read_status(tmp_path, pid)
    assert st.session_id == "abc-123"


def test_process_version(tmp_path):
    proc = tmp_path / "proc"
    exe = proc / "100" / "exe"
    exe.parent.mkdir(parents=True)
    exe.symlink_to("/opt/claude/versions/2.1.292")
    assert process_version(100, proc) == "2.1.292"
    # non-version exe
    exe2 = proc / "101" / "exe"
    exe2.parent.mkdir(parents=True)
    exe2.symlink_to("/usr/bin/python3")
    assert process_version(101, proc) is None
    # missing
    assert process_version(999, proc) is None


def test_latest_version(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = bin_dir / "2.1.292"
    target.write_text("x")
    link = bin_dir / "claude"
    link.symlink_to(target)
    assert latest_version(link) == "2.1.292"
    plain = bin_dir / "notaversion"
    plain.write_text("x")
    assert latest_version(plain) is None


def test_find_transcript(tmp_path):
    proj = tmp_path / "projects" / "proj1"
    proj.mkdir(parents=True)
    t = proj / "s-1.jsonl"
    t.write_text("")
    assert find_transcript(tmp_path, "s-1") == t
    assert find_transcript(tmp_path, "s-2") is None
    assert find_transcript(tmp_path / "nodir", "s-1") is None


def test_build_argv_resume(tmp_path):
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "s-1.jsonl").touch()
    argv = build_argv(tmp_path / "bin/claude", tmp_path, "portal", "s-1")
    assert argv == [str(tmp_path / "bin/claude"), "--resume", "s-1", "-n", "portal",
                    "--remote-control", "portal", "--settings", DISABLE_AGENT_VIEW]


def test_build_argv_session_id(tmp_path):
    argv = build_argv(tmp_path / "bin/claude", tmp_path, "portal", "s-2")
    assert argv == [str(tmp_path / "bin/claude"), "--session-id", "s-2", "-n", "portal",
                    "--remote-control", "portal", "--settings", DISABLE_AGENT_VIEW]


def test_build_argv_with_statusline(tmp_path):
    import json as _json
    cmd = statusline_command(tmp_path / "share dir/statusline.py", tmp_path / "usage.json")
    assert cmd.startswith("python3 -I -S '") and cmd.endswith("usage.json")
    argv = build_argv(tmp_path / "bin/claude", tmp_path, "portal", "s-3", cmd)
    settings = _json.loads(argv[argv.index("--settings") + 1])
    assert settings == {"disableAgentView": True, "statusLine": {"type": "command", "command": cmd}}


def test_statusline_script_saves_only_rate_limits(tmp_path):
    import json as _json
    import subprocess
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "share" / "statusline.py"
    out = tmp_path / "usage.json"
    rl = {"five_hour": {"used_percentage": 42, "resets_at": 1791561000},
          "seven_day": {"used_percentage": 13, "resets_at": 1792087200}}

    def run(payload):
        return subprocess.run(["python3", "-I", "-S", str(script), str(out)], input=payload,
                              capture_output=True, text=True, timeout=10)

    r = run(_json.dumps({"cwd": "/x", "session_id": "secret-ish", "rate_limits": rl}))
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert _json.loads(out.read_text()) == rl
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    for bad in ("not json", "[]", _json.dumps({"rate_limits": "x"}), _json.dumps({"cwd": "/x"})):
        r = run(bad)
        assert r.returncode == 0 and r.stdout == ""
        assert _json.loads(out.read_text()) == rl          # untouched
    assert list(tmp_path.glob("*.tmp")) == []


def test_statusline_script_merges_windows_across_sessions(tmp_path):
    # every session writes the same file; an idle session's older, lower figure must not win
    import json as _json
    import subprocess
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "share" / "statusline.py"
    out = tmp_path / "usage.json"

    def run(five, seven=None):
        rl = {"five_hour": five}
        if seven is not None:
            rl["seven_day"] = seven
        r = subprocess.run(["python3", "-I", "-S", str(script), str(out)],
                           input=_json.dumps({"rate_limits": rl}), capture_output=True, text=True, timeout=10)
        assert r.returncode == 0 and r.stdout == ""
        return _json.loads(out.read_text())

    w = lambda pct, reset: {"used_percentage": pct, "resets_at": reset}
    assert run(w(34, 1000), w(23, 9000)) == {"five_hour": w(34, 1000), "seven_day": w(23, 9000)}
    # same window, lower (stale) figure from another session: kept at 34
    assert run(w(31, 1000), w(22, 9000)) == {"five_hour": w(34, 1000), "seven_day": w(23, 9000)}
    # same window, higher figure: taken
    assert run(w(36, 1000))["five_hour"] == w(36, 1000)
    # a session without the week window keeps the saved one
    assert run(w(36, 1000))["seven_day"] == w(23, 9000)
    # a stale report from the previous window is ignored; a new window replaces even a higher figure
    assert run(w(90, 500))["five_hour"] == w(36, 1000)
    assert run(w(2, 6000))["five_hour"] == w(2, 6000)
    # a corrupt saved file is replaced
    out.write_text("{oops")
    assert run(w(5, 7000), w(1, 9500)) == {"five_hour": w(5, 7000), "seven_day": w(1, 9500)}


def test_build_argv_memory_dir_and_helpers(tmp_path):
    import json as _json
    from force.claude import build_argv, global_memory_dir, memory_dir_for, project_slug
    argv = build_argv(tmp_path / "claude", tmp_path, "portal", "s-1")
    assert argv[argv.index("--settings") + 1] == '{"disableAgentView":true}'      # unchanged without one
    argv = build_argv(tmp_path / "claude", tmp_path, "portal", "s-1", memory_dir="/m/memory")
    assert _json.loads(argv[argv.index("--settings") + 1]) == {"disableAgentView": True,
                                                              "autoMemoryDirectory": "/m/memory"}
    assert project_slug("/home/alice/my.repo_x") == "-home-alice-my-repo-x"
    assert project_slug("/" + "a" * 250) is None
    # memory next to the transcript, else the cwd's project
    assert memory_dir_for(tmp_path, "s-1", "/home/alice") == tmp_path / "projects" / "-home-alice" / "memory"
    proj = tmp_path / "projects" / "-srv-x"
    proj.mkdir(parents=True)
    (proj / "s-1.jsonl").write_text("{}\n")
    assert memory_dir_for(tmp_path, "s-1", "/home/alice") == proj / "memory"
    assert global_memory_dir(tmp_path) is None
    (tmp_path / "settings.json").write_text(_json.dumps({"autoMemoryDirectory": "~/mem"}))
    assert global_memory_dir(tmp_path) == "~/mem"
    (tmp_path / "settings.json").write_text("not json")
    assert global_memory_dir(tmp_path) is None



def test_build_argv_without_remote_control(tmp_path):
    from force.claude import build_argv
    assert "--remote-control" in build_argv(tmp_path / "claude", tmp_path, "p", "s-1")
    argv = build_argv(tmp_path / "claude", tmp_path, "p", "s-1", remote_control=False)
    assert "--remote-control" not in argv and argv[argv.index("-n") + 1] == "p"
