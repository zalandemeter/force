"""Chuck, Force's support assistant (SPEC §50): his session and folder, the GitHub links and search, fix mode, and
`force mcp`, Force's MCP server."""
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from force import cli, config, mcp, menus, registry, support
from force.state import Snapshot, SessionView, State

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _pack(monkeypatch):
    """The tests' release is a temp dir without share/support: Chuck's pack comes from this checkout."""
    monkeypatch.setattr(support, "pack_dir", lambda paths: REPO / "share" / "support")


# --- links ---------------------------------------------------------------------------------------------------
def _q(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_bug_link_fills_the_form_by_field_id():
    url, short = support.issue_link("bug", "Sidebar flickers", {"what": "it flickers", "version": "force 1.0.3"})
    assert url.startswith("https://github.com/zalandemeter/force/issues/new?")
    q = _q(url)
    assert q == {"template": "bug_report.yml", "title": "Sidebar flickers", "what": "it flickers",
                 "version": "force 1.0.3"}
    # the dropdown is not in the link: GitHub documents query pre-fill for text fields only
    with pytest.raises(support.SupportError, match="unknown field"):
        support.issue_link("bug", "t", {"where": "Notifications"})
    assert short == []
    assert "+" not in url.split("?", 1)[1]            # spaces as %20 (a + would show up literally in some forms)


def test_feature_link_and_field_checks():
    url, _ = support.issue_link("feature", "Codex sessions", {"problem": "p", "idea": "i", "alternatives": ""})
    assert _q(url) == {"template": "feature_request.yml", "title": "Codex sessions", "problem": "p", "idea": "i"}
    with pytest.raises(support.SupportError, match="unknown field"):
        support.issue_link("feature", "t", {"what": "x"})
    with pytest.raises(support.SupportError, match="bug or feature"):
        support.issue_link("question", "t", {})
    with pytest.raises(support.SupportError, match="title"):
        support.issue_link("bug", "  ", {})


def test_a_long_log_is_shortened_first_and_the_link_fits():
    url, short = support.issue_link("bug", "Daemon crash", {"what": "it crashed", "logs": "é" * 9000,
                                                          "doctor": "✓ healthy"})
    assert len(url) <= support.URL_LIMIT
    assert short == ["logs"]
    q = _q(url)
    assert q["what"] == "it crashed" and q["doctor"] == "✓ healthy"
    assert q["logs"].endswith("(shortened: paste the rest after opening)")


def test_pr_link_and_conventional_titles():
    url = support.pr_link("alice", "fix/12-sidebar-flicker", "fix(sidebar): stop the flicker", "Fixes #12")
    assert url.startswith("https://github.com/zalandemeter/force/compare/main...alice:force:fix/12-sidebar-flicker?")
    assert _q(url) == {"quick_pull": "1", "title": "fix(sidebar): stop the flicker", "body": "Fixes #12"}
    assert support.CONVENTIONAL.match("fix(sidebar): stop the flicker")
    assert support.CONVENTIONAL.match("feat!: drop classic layout")
    assert not support.CONVENTIONAL.match("Fix the sidebar")
    assert support.pr_body(12, "More text.") == "More text.\n\nFixes #12\n\nMade with Chuck, Force's assistant."
    assert support.comment_link(7).endswith("/issues/7#issuecomment-new")


# --- search --------------------------------------------------------------------------------------------------
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_search_asks_github_without_a_login_and_formats_the_results():
    seen = {}

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["ua"] = req.get_header("User-agent")
        return _Resp(json.dumps({"items": [
            {"number": 3, "title": "Sidebar flickers", "state": "open", "html_url": "https://x/3", "comments": 2,
             "labels": [{"name": "bug"}]}]}).encode())

    items = support.search("sidebar flicker", opener=opener)
    q = _q(seen["url"])
    assert q["q"] == "sidebar flicker repo:zalandemeter/force is:issue"
    assert seen["auth"] is None and seen["ua"].startswith("force/")
    assert items == [{"number": 3, "title": "Sidebar flickers", "state": "open", "url": "https://x/3",
                      "comments": 2, "labels": ["bug"]}]
    assert support.format_results(items) == "#3 open [bug]: Sidebar flickers\n    https://x/3"
    assert support.format_results([]) == "no matching issues"


def test_search_explains_the_rate_limit():
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "rate limited", {}, None)

    with pytest.raises(support.SupportError, match="10 a minute"):
        support.search("x", opener=opener)


# --- fix mode ------------------------------------------------------------------------------------------------
def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"})


def test_fix_clones_the_fork_and_branches_from_upstream_main(env, tmp_path):
    up = tmp_path / "upstream"
    up.mkdir()
    _git("init", "-q", "-b", "main", cwd=up)
    (up / "README.md").write_text("force\n")
    _git("add", ".", cwd=up)
    _git("commit", "-q", "-m", "init", cwd=up)
    fork = tmp_path / "fork.git"
    _git("clone", "-q", "--bare", str(up), str(fork), cwd=tmp_path)
    (up / "NEW.md").write_text("upstream moved on\n")          # upstream is ahead of the fork
    _git("add", ".", cwd=up)
    _git("commit", "-q", "-m", "more", cwd=up)

    src, branch = support.fix_setup(env.paths, 12, "alice", "Sidebar flickers on resize!",
                                    upstream=str(up), fork=str(fork))
    assert branch == "fix/12-sidebar-flickers-on-resize"
    assert src == env.paths.home / "support" / "src"
    assert (src / "NEW.md").exists(), "the branch starts from upstream main"
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=src, capture_output=True,
                          text=True).stdout.strip()
    assert head == branch
    # again: the clone and the branch are reused
    assert support.fix_setup(env.paths, 12, "alice", "Sidebar flickers on resize!", upstream=str(up),
                             fork=str(fork)) == (src, branch)
    # the next fix reuses the clone: no user name needed, a new branch from upstream main
    src2, branch2 = support.fix_setup(env.paths, 13, None, "Header pill wrong", upstream=str(up), fork=str(fork))
    assert src2 == src and branch2 == "fix/13-header-pill-wrong"
    # unsaved work on the branch checked out: refused, with the branch and the file
    (src / "WIP.md").write_text("half done\n")
    with pytest.raises(support.SupportError, match="fix/13-header-pill-wrong .* uncommitted changes .*WIP.md"):
        support.fix_setup(env.paths, 14, None, "Another", upstream=str(up), fork=str(fork))
    (src / "WIP.md").unlink()
    with pytest.raises(support.SupportError, match="user name"):
        support.fix_setup(env.paths.__class__(**{**env.paths.__dict__, "home": env.tmp / "elsewhere"}), 12,
                          "bad name!", "x")


# --- the folder and the launch ---------------------------------------------------------------------------------
def test_prepare_dir_writes_the_brief_and_the_mcp_config(env, monkeypatch):
    monkeypatch.setenv("FORCE_BIN", "/x/force-dev")
    d = support.prepare_dir(env.paths)
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (d / name).read_text()
        assert text.startswith("# You are Chuck")
        assert "{{version}}" not in text and "{{app}}" not in text
    assert (d / "faq.md").exists() and (d / "architecture.md").exists()
    oc = json.loads((d / "opencode.json").read_text())
    assert oc["mcp"]["force"] == {"type": "local", "command": ["/x/force-dev", "mcp"], "enabled": True}
    perm = oc["permission"]
    assert perm["force_*"] == "ask" and perm["force_settings_get"] == "allow"
    assert "force_settings_set" not in perm, "a change tool falls under force_*: ask"
    assert list(perm).index("force_*") < list(perm).index("force_status"), "the specific allows come after"


def test_claude_launch_keeps_chuck_to_his_own_context(env):
    settings, args = support.claude_launch(env.paths)
    ex = settings["claudeMdExcludes"]
    assert str(env.paths.claude_dir / "CLAUDE.md") in ex
    home_parent = support.support_dir(env.paths).parent
    assert str(home_parent / "CLAUDE.md") in ex
    assert str(support.support_dir(env.paths) / "CLAUDE.md") not in ex, "his own brief loads"
    assert settings["autoMemoryEnabled"] is False
    allowed = settings["permissions"]["allow"]
    assert "mcp__force__status" in allowed and "mcp__force__settings_set" not in allowed
    assert args[0] == "--mcp-config" and args[2] == "--strict-mcp-config"
    assert args[3:5] == ["--permission-mode", "auto"], "auto, whatever the user's default"
    assert args[5:7] == ["--add-dir", str(support.app_dir(env.paths))]
    ask = settings["permissions"]["ask"]
    assert "mcp__force__settings_set" in ask and "mcp__force__host_add" in ask and "mcp__force__status" not in ask
    assert "Bash(git push *)" in ask and "Bash(git push)" in ask
    assert "--model" not in args
    _s, with_model = support.claude_launch(env.paths, "sonnet")
    assert with_model[-2:] == ["--model", "sonnet"]
    cfg = json.loads(args[1])
    assert cfg["mcpServers"]["force"]["args"] == ["mcp"]


def test_claude_argv_of_the_support_session(cockpit):
    from force import kinds
    e = registry.Entry(name="chuck", session_id="11111111-1111-1111-1111-111111111111", cwd="/tmp",
                       created_at="x", order=1, extra={"role": "support"})
    argv, _env = kinds.Claude().exec_argv(cockpit, e)
    assert "--strict-mcp-config" in argv and "--mcp-config" in argv
    s = json.loads(argv[argv.index("--settings") + 1])
    assert s["disableAgentView"] is True and s["autoMemoryEnabled"] is False
    plain = registry.Entry(name="p", session_id="22222222-2222-2222-2222-222222222222", cwd="/tmp",
                           created_at="x", order=0)
    argv2, _ = kinds.Claude().exec_argv(cockpit, plain)
    assert "--mcp-config" not in argv2


# --- the session ---------------------------------------------------------------------------------------------
def test_ensure_session_creates_chuck_once_and_last(cockpit):
    assert cli.create_session(cockpit, "work", "/tmp", no_switch=True) == 0
    name = support.ensure_session(cockpit)
    assert name == "chuck"
    reg = registry.load(cockpit.paths.registry)
    e = reg.get("chuck")
    assert e.extra.get("role") == "support"
    assert e.cwd == str(support.support_dir(cockpit.paths))
    assert support.ensure_session(cockpit) == "chuck"                    # reused, never a second one
    assert [x.name for x in registry.load(cockpit.paths.registry).entries if registry.is_support(x)] == ["chuck"]
    assert cli.create_session(cockpit, "later", "/tmp", no_switch=True) == 0
    assert [x.name for x in registry.load(cockpit.paths.registry).live()] == ["work", "later", "chuck"]


def test_chuck_takes_another_name_when_chuck_is_taken(cockpit):
    assert cli.create_session(cockpit, "chuck", "/tmp", no_switch=True) == 0
    assert support.ensure_session(cockpit) == "chuck-2"


def test_the_card_fits_its_popup():
    for agent in ("claude", "opencode", None):
        import re as _re
        lines = [_re.sub(r"\x1b\[[0-9;]*m", "", x) for x in support.card_text(agent, rich=True).split("\n")]
        assert max(len(x) for x in lines) <= 72 - 2 and len(lines) <= 17 - 2      # inside the border


def test_no_agent_installed_shows_the_card_and_the_way_out(cockpit, monkeypatch, capsys):
    monkeypatch.setattr(cli, "available_kinds", lambda c: ["shell"])
    assert support.open_command(cockpit, ui=False) == 1
    out = capsys.readouterr().out
    assert "Chuck" in out and "Force's assistant" in out and "claude.ai/install.sh" in out


def test_support_agent_setting(cockpit, monkeypatch):
    monkeypatch.setattr(cli, "available_kinds", lambda c: ["claude", "opencode", "shell"])
    assert support.agent_for(cockpit) == "claude"
    import dataclasses
    cockpit.cfg = dataclasses.replace(cockpit.cfg, support_agent="opencode")
    assert support.agent_for(cockpit) == "opencode"
    monkeypatch.setattr(cli, "available_kinds", lambda c: ["claude", "shell"])
    with pytest.raises(support.SupportError, match="not installed"):
        support.agent_for(cockpit)


def test_snapshot_marks_chuck():
    v = SessionView(name="chuck", index=3, state=State.IDLE, since_ms=None, version=None, outdated=False,
                    archived=False, order=5, cwd="/x", session_id="s", pid=None, waiting_for=None, viewed=False,
                    support=True)
    snap = Snapshot(generated_ms=1, latest_version=None, sessions=[v])
    j = snap.to_json()
    assert j["sessions"][0]["support"] is True
    assert Snapshot.from_json(j).sessions[0].support is True


# --- the menu --------------------------------------------------------------------------------------------------
def test_question_mark_asks_chuck_and_k_shows_the_keys():
    for mode in ("classic", "split"):
        text = menus.render(mode)
        assert 'Ask Chuck…" "?"' in text and "support --ui" in text      # after the item's icon format
        assert 'Keys…" "k"' in text and "show-help.sh" in text
    assert "FORCE_WINDOW=#{window_id}" in menus.render("split")


# --- the CLI ---------------------------------------------------------------------------------------------------
def test_cli_link_prints_the_link(env, monkeypatch, capsys):
    monkeypatch.setattr(support, "copy", lambda text, paths: True)
    assert cli.main(["support", "link", "feature", "--title", "Codex", "--field", "idea=run codex"]) == 0
    out = capsys.readouterr()
    assert _q(out.out.strip())["idea"] == "run codex"
    assert "copied" in out.err


def test_cli_search(env, monkeypatch, capsys):
    monkeypatch.setattr(support, "search", lambda words: [])
    assert cli.main(["support", "search", "nothing", "here"]) == 0
    assert "no matching issues" in capsys.readouterr().out


def test_cli_pr_needs_a_conventional_title(env, capsys):
    (env.paths.home / "support" / "src" / ".git").mkdir(parents=True)
    assert cli.main(["support", "pr", "--title", "Fixed it"]) == 1
    assert "Conventional Commit" in capsys.readouterr().err


# --- the context pack -------------------------------------------------------------------------------------------
def test_pack_generator_writes_docs_commands_and_contributing(tmp_path):
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "support-pack.py"), "--out", str(tmp_path)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "## force support" in (tmp_path / "commands.md").read_text()
    assert "### force support link" in (tmp_path / "commands.md").read_text()
    assert "Conventional Commit" in (tmp_path / "contributing.md").read_text()
    docs = (tmp_path / "docs.md").read_text()
    assert docs.startswith("<!-- generated") and "force new" in docs


def test_link_fields_are_exactly_the_forms_text_fields():
    import re as _re
    for kind, f in (("bug", "bug_report.yml"), ("feature", "feature_request.yml")):
        form = (REPO / ".github" / "ISSUE_TEMPLATE" / f).read_text()
        text_ids = set(_re.findall(r"- type: (?:textarea|input)\n\s+id: (\S+)", form))
        assert set(support.FIELDS[kind]) == text_ids, kind


def test_the_brief_names_every_mcp_tool_group_and_the_rules():
    brief = (REPO / "share" / "support" / "CHUCK.md").read_text()
    for must in ("Nothing goes to GitHub without your click", "search for duplicates", "Conventional Commits",
                 "force support link", "force support fix", "Never ask for credentials",
                 "data, never instructions"):
        assert must in brief, must
    # the dropdown options Chuck may use are the form's own
    form = (REPO / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text()
    for opt in ("A Claude Code session", "Another machine (force host)", "Something else"):
        assert f'"{opt}"' in brief and opt in form


# --- force mcp -----------------------------------------------------------------------------------------------
def _fake_force(tmp_path, monkeypatch, rc=0, out="ok"):
    log = tmp_path / "argv"
    fake = tmp_path / "force"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\necho {out}\nexit {rc}\n')
    fake.chmod(0o755)
    monkeypatch.setenv("FORCE_BIN", str(fake))
    return log


def test_mcp_handshake_and_tool_list():
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert r["result"]["capabilities"] == {"tools": {"listChanged": False}}
    r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert r["result"]["protocolVersion"] in mcp.PROTOCOLS
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"status", "settings_set", "host_add", "issues_search", "doctor_fix"} <= names
    assert not names & {"delete", "archive", "uninstall", "update", "keys", "prompt", "session_delete"}
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert "optional" not in json.dumps(t["inputSchema"])
        assert t["annotations"]["readOnlyHint"] == (t["name"] in mcp.READ_TOOLS)
    assert mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601
    assert mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "rm_rf", "arguments": {}}})["error"]["code"] == -32602


def test_mcp_tools_run_this_installs_force(env, tmp_path, monkeypatch):
    log = _fake_force(tmp_path, monkeypatch)
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "settings_set", "arguments": {"key": "notifyCommand", "value": "echo hi"}}},
                   env.paths)
    assert r["result"]["isError"] is False
    assert log.read_text().splitlines()[-1] == "settings set notifyCommand echo hi"
    mcp.call("session_new", {"name": "demo", "cwd": "/tmp", "kind": "shell"}, env.paths)
    assert log.read_text().splitlines()[-1] == "new demo --cwd /tmp --shell --no-switch"
    mcp.call("status", {}, env.paths)
    assert log.read_text().splitlines()[-1] == "list --json"
    actions = [json.loads(x) for x in (env.paths.state / "support" / "actions.jsonl").read_text().splitlines()]
    assert [a["tool"] for a in actions] == ["settings_set", "session_new"], "only changes are logged"


def test_mcp_failures_and_missing_arguments(env, tmp_path, monkeypatch):
    _fake_force(tmp_path, monkeypatch, rc=1, out="bad value")
    ok, text = mcp.call("settings_set", {"key": "icons", "value": "maybe"}, env.paths)
    assert not ok and "bad value" in text
    ok, text = mcp.call("settings_set", {"value": "x"}, env.paths)
    assert not ok and "key is required" in text
    ok, _ = mcp.call("doctor", {}, env.paths)
    assert ok, "problems found are an answer"


def test_mcp_host_add_checks_ssh_first(env, tmp_path, monkeypatch):
    log = _fake_force(tmp_path, monkeypatch)
    ok, text = mcp.call("host_add", {"name": "box", "ssh": "me@box"}, env.paths,
                        ssh_check=lambda t: (False, "Permission denied (publickey)"))
    assert not ok and "ssh-copy-id me@box" in text and not log.exists()
    ok, _ = mcp.call("host_add", {"name": "box", "ssh": "me@box"}, env.paths, ssh_check=lambda t: (True, ""))
    assert ok and log.read_text().splitlines()[-1] == "host add box me@box"


def test_mcp_issue_link_tool(env, monkeypatch):
    monkeypatch.setattr(support, "copy", lambda text, paths: False)
    ok, text = mcp.call("issue_link", {"kind": "bug", "title": "T", "fields": {"what": "w"}}, env.paths)
    assert ok and text.startswith("https://github.com/zalandemeter/force/issues/new?")
    ok, text = mcp.call("issue_link", {"kind": "bug", "title": "T", "fields": {"nope": "w"}}, env.paths)
    assert not ok and "unknown field" in text


def test_mcp_serves_line_delimited_json(env):
    inp = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    ]) + "\n")
    out = io.StringIO()
    assert mcp.serve(inp, out) == 0
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [x.get("id") for x in lines] == [1, None, 2]
    assert lines[1]["error"]["code"] == -32700


def test_a_window_showing_a_remote_shows_chuck_over_it(cockpit, monkeypatch):
    seen = {}
    monkeypatch.setenv("FORCE_HOST", "devbox")
    monkeypatch.setenv("FORCE_WINDOW", "@3")
    monkeypatch.setattr(cli, "overlay_open", lambda c, window, name: seen.update(window=window, name=name) or 0)
    assert support._switch(cockpit, "chuck") == 0
    assert seen == {"window": "@3", "name": "chuck"}


def test_the_repo_override_is_for_testing_and_checked():
    code = "from force import support; print(support.REPO)"
    run = lambda v: subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                                   env={**os.environ, "FORCE_SUPPORT_REPO": v}).stdout.strip()
    assert run("tmux/tmux") == "tmux/tmux"
    assert run("not a repo; rm -rf") == "zalandemeter/force"
    assert run("") == "zalandemeter/force"


# --- Chuck is not in the session list (SPEC §50) ---------------------------------------------------------------
def _views():
    mk = lambda name, idx, st, sup=False: SessionView(
        name=name, index=idx, state=st, since_ms=None, version=None, outdated=False, archived=False, order=idx or 99,
        cwd="/x", session_id=name, pid=None, waiting_for=None, viewed=False, support=sup)
    return [mk("a", 1, State.NEEDS_YOU), mk("b", 2, State.WORKING), mk("chuck", None, State.NEEDS_YOU, True)]


def test_counts_leave_chuck_out():
    c = Snapshot(generated_ms=1, latest_version=None, sessions=_views()).counts()
    assert c["needs_you"] == 1 and c["working"] == 1


def test_force_list_hides_chuck_but_json_keeps_him(cockpit, monkeypatch, capsys):
    snap = Snapshot(generated_ms=cli._now_ms(), latest_version=None, sessions=_views())
    monkeypatch.setattr(cli, "_read_fresh_snapshot", lambda c: snap)
    assert cli.list_command(cockpit, False, False) == 0
    out = capsys.readouterr().out
    assert " a " in out and " b " in out and "chuck" not in out
    assert cli.list_command(cockpit, False, True) == 0
    assert any(s["name"] == "chuck" and s["support"] for s in json.loads(capsys.readouterr().out)["sessions"])


def test_numbers_and_next_prev_skip_chuck(cockpit, monkeypatch):
    for n in ("a", "b"):
        assert cli.create_session(cockpit, n, "/tmp", no_switch=True) == 0
    assert support.ensure_session(cockpit) == "chuck"
    went = []
    monkeypatch.setattr(cockpit.tmux, "switch_client", lambda target, client=None: went.append(target))
    assert cli.switch_command(cockpit, None, 3, False, False, None) == 1, "Chuck has no number"
    monkeypatch.setattr(cli, "_current_session", lambda c, client: "chuck")
    cli.switch_command(cockpit, None, None, True, False, None)
    cli.switch_command(cockpit, None, None, False, True, None)
    assert went == ["a", "b"], "from Chuck, next is the first and previous the last"
    monkeypatch.setattr(cli, "_current_session", lambda c, client: "b")
    cli.switch_command(cockpit, None, None, True, False, None)
    assert went[-1] == "a", "wraps around without passing through Chuck"


def test_daemon_gives_chuck_no_number(cockpit):
    from test_daemon import FakeClock
    from force.daemon import Daemon
    for n in ("a", "b"):
        assert cli.create_session(cockpit, n, "/tmp", no_switch=True) == 0
    support.ensure_session(cockpit)
    d = Daemon(cockpit, FakeClock())
    d.tick()
    snap = json.loads(cockpit.paths.snapshot.read_text())
    idx = {s["name"]: s["index"] for s in snap["sessions"]}
    assert idx == {"a": 1, "b": 2, "chuck": None}


# --- security problems go by email (SECURITY.md) ---------------------------------------------------------------
def test_security_report_is_a_mailto_never_an_issue(env, monkeypatch, capsys, tmp_path):
    url = support.security_mailto("Token in a world-readable file", "force 1.0.6\nAn attacker can …")
    assert url.startswith("mailto:hello@useforce.cc?")
    q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
    assert q["subject"] == "Security: Token in a world-readable file" and q["body"].startswith("force 1.0.6")
    long = support.security_mailto("x", "a" * 5000)
    assert len(long) <= support.MAILTO_LIMIT and "clipboard" in long
    draft = tmp_path / "draft.txt"
    draft.write_text("force 1.0.6\nsteps")
    copied = []
    monkeypatch.setattr(support, "copy", lambda text, paths: copied.append(text) or True)
    assert cli.main(["support", "security", "--title", "T", "--file", str(draft)]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("mailto:hello@useforce.cc?") and "never as a public issue" in out.err
    assert copied == ["force 1.0.6\nsteps"]


def test_the_brief_sends_vulnerabilities_by_email():
    brief = (REPO / "share" / "support" / "CHUCK.md").read_text()
    assert "hello@useforce.cc" in brief and "force support security" in brief
    assert "no issue, no comment, no pull request" in brief
    policy = (REPO / ".github" / "SECURITY.md").read_text()
    assert "hello@useforce.cc" in policy


# --- Chuck over a remote: his session in the main pane, the remote's list stays (SPEC §50) ------------------------
def test_chuck_over_a_remote_and_back(env, monkeypatch):
    import dataclasses
    from force import remote
    cockpit = env.make_cockpit()
    cockpit.cfg = dataclasses.replace(cockpit.cfg, ui="split")
    monkeypatch.setattr(remote, "remote_tmux", lambda paths, r, run=None: "tmux")
    remote.save(cockpit.paths, [remote.Remote("devbox", "devbox")])
    name = cli.build_ui_session(cockpit, "portal", "ui-r", 120, 30, remote.get(cockpit.paths, "devbox"))
    ui = cockpit.ui_tmux
    main = next(p for p in ui.panes() if p.session == name and p.role == "agent")
    win = ui.run("display", "-p", "-t", main.pane_id, "#{window_id}")
    opt = lambda k: ui.run("show", "-wqv", "-t", win, k)
    start = lambda: ui.run("display", "-p", "-t", main.pane_id, "#{pane_start_command}")
    remote_tmux = opt("@force_host_tmux")

    assert cli.overlay_open(cockpit, win, "chuck") == 0
    assert opt("@force_overlay") == "chuck" and opt("@force_session") == "chuck" and opt("@force_host") == ""
    assert opt("@force_host_tmux") == f"tmux -L {cockpit.paths.tmux_socket}"
    assert opt("@force_nested").startswith("/dev/")
    assert "nested chuck" in start() and "--remote" not in start()
    assert json.loads(opt("@force_overlay_back"))["host"] == "devbox"
    # the sidebar and header panes are left as they are (they keep the remote's list)
    assert {p.role for p in ui.panes() if p.session == name} >= {"agent", "sidebar", "header"}

    assert cli.overlay_close(cockpit, win, target="api") == 0
    assert opt("@force_overlay") == "" and opt("@force_overlay_back") == ""
    assert opt("@force_host") == "devbox" and opt("@force_session") == "api"
    assert opt("@force_host_tmux") == remote_tmux
    assert "nested api --remote devbox" in start()
    assert cli.overlay_close(cockpit, win) == 0, "nothing to do without an overlay"


def test_menu_dialogs_restore_the_host_first():
    split = menus.render("split")
    for kind in ("jump", "new", "archived"):
        assert f"ui-main --window #{{window_id}} --wait --dialog {kind} --client #{{client_name}}" in split
    assert "ui-main" not in menus.render("classic")


# --- the agent and the model follow the settings (SPEC §50) ------------------------------------------------------
def test_change_tools_require_the_users_click_in_every_mode():
    for t in mcp.tool_list():
        needs = t.get("_meta", {}).get("anthropic/requiresUserInteraction") is True
        assert needs == (t["name"] not in mcp.READ_TOOLS), t["name"]


def test_opencode_gets_the_model(env):
    assert "model" not in support.opencode_config()
    assert support.opencode_config("deepseek/deepseek-v4")["model"] == "deepseek/deepseek-v4"
    d = support.prepare_dir(env.paths, "openai/gpt-5")
    assert json.loads((d / "opencode.json").read_text())["model"] == "openai/gpt-5"


def test_a_new_support_agent_starts_a_fresh_chuck(cockpit, monkeypatch):
    import dataclasses
    monkeypatch.setattr(cli, "available_kinds", lambda c: ["claude", "opencode", "shell"])
    assert support.ensure_session(cockpit) == "chuck"
    before = registry.load(cockpit.paths.registry).get("chuck")
    assert registry.kind(before) == "claude"
    restarted = []
    monkeypatch.setattr(cli, "restart_command", lambda c, name, all_=False: restarted.append(name) or 0)
    cockpit.cfg = dataclasses.replace(cockpit.cfg, support_agent="opencode")
    assert support.ensure_session(cockpit) == "chuck", "the same session, not chuck-2"
    after = registry.load(cockpit.paths.registry).get("chuck")
    assert registry.kind(after) == "opencode" and after.session_id != before.session_id
    assert registry.is_support(after) and restarted == ["chuck"]


def test_new_starts_chuck_on_a_new_conversation(cockpit, monkeypatch):
    assert support.ensure_session(cockpit) == "chuck"
    with registry.locked(cockpit.paths.home) as r:
        r.get("chuck").extra["oc_session"] = "ses_old"
    before = registry.load(cockpit.paths.registry).get("chuck")
    restarted = []
    monkeypatch.setattr(cli, "restart_command", lambda c, name, all_=False: restarted.append(name) or 0)
    assert support.ensure_session(cockpit) == "chuck"
    assert restarted == [] and registry.load(cockpit.paths.registry).get("chuck").session_id == before.session_id
    assert support.ensure_session(cockpit, new=True) == "chuck", "the same session, not chuck-2"
    after = registry.load(cockpit.paths.registry).get("chuck")
    assert after.session_id != before.session_id and "oc_session" not in after.extra
    assert registry.is_support(after) and registry.kind(after) == registry.kind(before) and restarted == ["chuck"]


def test_n_on_the_card_opens_a_new_conversation(cockpit, monkeypatch, capsys):
    monkeypatch.setattr(cli, "available_kinds", lambda c: ["claude", "shell"])
    seen = []
    monkeypatch.setattr(support, "ensure_session", lambda c, new=False: seen.append(new) or "chuck")
    monkeypatch.setattr(support, "_switch", lambda c, name: 0)
    for key, want in (("\r", [False]), ("n", [True]), ("\x1b", [])):
        seen.clear()
        monkeypatch.setattr(support, "_wait_key", lambda k=key: k)
        assert support.open_command(cockpit, ui=True) == 0
        assert seen == want, key
    assert "n  New conversation" in support.card_text("claude", rich=False, color=False)


def test_a_new_support_model_restarts_chuck_with_his_conversation(cockpit, monkeypatch):
    import dataclasses
    assert support.ensure_session(cockpit) == "chuck"
    restarted = []
    monkeypatch.setattr(cli, "restart_command", lambda c, name, all_=False: restarted.append(name) or 0)
    support.ensure_session(cockpit)
    assert restarted == [], "same model: nothing to do"
    cockpit.cfg = dataclasses.replace(cockpit.cfg, support_model="sonnet")
    support.ensure_session(cockpit)
    assert restarted == ["chuck"]
    assert registry.load(cockpit.paths.registry).get("chuck").extra["support_model"] == "sonnet"
    support.ensure_session(cockpit)
    assert restarted == ["chuck"], "once"
