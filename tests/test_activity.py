import json
import os

import pytest

from force.activity import Background, Scan, _model_short, describe, describe_config, is_endless, is_finding, scan, waiting_tasks


def write(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def assistant(content):
    return {"type": "assistant", "message": {"content": content}}


def tool(name, input=None):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": input or {}}]}}


def test_bash_fixed_phrase(tmp_path):
    assert describe(write(tmp_path / "t.jsonl",
                      [tool("Bash", {"description": "list files", "command": "ls"})])) == "running a command"


def test_read_notebook_read(tmp_path):
    assert describe(write(tmp_path / "r.jsonl",
                      [tool("Read", {"file_path": "/home/alice/cockpit/force/state.py"})])) == "reading state.py"
    assert describe(write(tmp_path / "n.jsonl",
                      [tool("NotebookRead", {"notebook_path": "/tmp/a.ipynb"})])) == "reading a.ipynb"
    assert describe(write(tmp_path / "rn.jsonl", [tool("Read", {})])) == "reading files"


def test_edit_multiedit_notebookedit(tmp_path):
    assert describe(write(tmp_path / "e.jsonl",
                      [tool("Edit", {"file_path": "/tmp/notes.md"})])) == "editing notes.md"
    assert describe(write(tmp_path / "m.jsonl",
                      [tool("MultiEdit", {"file_path": "/tmp/a.py"})])) == "editing a.py"
    assert describe(write(tmp_path / "ne.jsonl",
                      [tool("NotebookEdit", {"notebook_path": "/tmp/nb.ipynb"})])) == "editing nb.ipynb"
    assert describe(write(tmp_path / "en.jsonl", [tool("Edit", {})])) == "editing files"


def test_write(tmp_path):
    assert describe(write(tmp_path / "w.jsonl",
                      [tool("Write", {"file_path": "/tmp/out.txt"})])) == "writing out.txt"
    assert describe(write(tmp_path / "wn.jsonl", [tool("Write", {})])) == "writing files"


def test_searching_files(tmp_path):
    assert describe(write(tmp_path / "g.jsonl", [tool("Grep", {})])) == "searching files"
    assert describe(write(tmp_path / "gl.jsonl", [tool("Glob", {})])) == "searching files"


def test_delegating(tmp_path):
    assert describe(write(tmp_path / "a.jsonl", [tool("Agent", {})])) == "delegating"
    assert describe(write(tmp_path / "t.jsonl", [tool("Task", {})])) == "delegating"


def test_ask_user_question(tmp_path):
    assert describe(write(tmp_path / "q.jsonl", [tool("AskUserQuestion", {})])) == "asks you a question"


def test_exit_plan_mode(tmp_path):
    assert describe(write(tmp_path / "e.jsonl", [tool("ExitPlanMode", {})])) == "plan to review"


def test_web(tmp_path):
    assert describe(write(tmp_path / "f.jsonl", [tool("WebFetch", {})])) == "browsing the web"
    assert describe(write(tmp_path / "s.jsonl", [tool("WebSearch", {})])) == "browsing the web"


def test_todo_write(tmp_path):
    assert describe(write(tmp_path / "td.jsonl", [tool("TodoWrite", {})])) == "planning"


def test_mcp_tool(tmp_path):
    assert describe(write(tmp_path / "m.jsonl",
                      [tool("mcp__filesystem__ReadFile", {})])) == "using filesystem"


def test_other_tool(tmp_path):
    assert describe(write(tmp_path / "o.jsonl", [tool("my_custom_tool", {})])) == "using my_custom_tool"


def test_text_block_without_tool_is_reply(tmp_path):
    p = write(tmp_path / "txt.jsonl",
              [assistant([{"type": "text", "text": "## Hello world\n- second"}])])
    assert describe(p) == "writing a reply"


def test_thinking_only_is_thinking(tmp_path):
    # thinking-only assistant records are the newest activity, no longer skipped
    p = write(tmp_path / "th.jsonl",
              [assistant([{"type": "thinking", "thinking": "hmm"}])])
    assert describe(p) == "thinking"


def test_sidechain_skipped(tmp_path):
    # a sidechain assistant record is skipped even if it is the newest
    p = write(tmp_path / "sc.jsonl", [
        {"type": "assistant", "isSidechain": True,
         "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
        assistant([{"type": "thinking", "thinking": "main"}]),
    ])
    assert describe(p) == "thinking"


def test_non_assistant_types_ignored(tmp_path):
    p = write(tmp_path / "u.jsonl", [
        {"type": "user", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
        assistant([{"type": "thinking", "thinking": "main"}]),
    ])
    assert describe(p) == "thinking"


def test_last_record_wins(tmp_path):
    p = write(tmp_path / "l.jsonl", [
        assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
        assistant([{"type": "thinking", "thinking": "newest"}]),
    ])
    assert describe(p) == "thinking"


def test_tail_with_partial_first_line(tmp_path):
    p = tmp_path / "big.jsonl"
    with open(p, "w") as f:
        f.write("x" * (300 * 1024) + "\n")            # huge first line, cut by the 256 KiB tail
        f.write(json.dumps({"type": "user"}) + "\n")
        f.write(json.dumps(tool("Bash")) + "\n")
    assert os.path.getsize(p) > 256 * 1024
    assert describe(p) == "running a command"


def test_malformed_lines_ignored(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text("{not json\n" + json.dumps(tool("Bash")) + "\n")
    assert describe(p) == "running a command"


def test_missing_file(tmp_path):
    assert describe(tmp_path / "nope.jsonl") is None


def test_truncation(tmp_path):
    p = write(tmp_path / "trunc.jsonl",
              [tool("Read", {"file_path": "/p/averyveryverylongfilename.txt"})])
    d = describe(p, max_len=20)
    assert len(d) == 20
    assert d.endswith("…")


def test_mcp_short_server_name(tmp_path):
    assert describe(write(tmp_path / "m2.jsonl", [tool("mcp__x__Tool", {})])) == "using x"


# --- model / effort (SPEC §16) ------------------------------------------------

@pytest.mark.parametrize("raw,short", [
    ("claude-opus-5-5", "opus 5.5"),
    ("claude-sonnet-5-5", "sonnet 5.5"),
    ("claude-haiku-4-5-20251001", "haiku 4.5"),
    ("claude-fable-5-1", "fable 5.1"),
    ("claude-opus-4-1-20250805", "opus 4.1"),
])
def test_model_short_family_version(raw, short):
    assert _model_short(raw) == short


def test_model_short_non_matching_keeps_stripped_id():
    assert _model_short("claude-opus4") == "opus4"
    assert _model_short("opus-5-5") == "opus 5.5"
    assert _model_short("plain-name") == "plain-name"


def test_model_short_missing_and_non_string():
    assert _model_short(None) is None
    assert _model_short("") is None
    assert _model_short(123) is None


def test_describe_config_model_and_effort(tmp_path):
    p = write(tmp_path / "c.jsonl", [
        {"type": "assistant",
         "message": {"model": "claude-opus-5-5", "content": []},
         "effort": "high"},
    ])
    assert describe_config(p) == ("opus 5.5", "high")
    # effort missing / not a string -> None
    p2 = write(tmp_path / "c2.jsonl", [
        {"type": "assistant", "message": {"model": "claude-sonnet-5-5", "content": []}},
    ])
    assert describe_config(p2) == ("sonnet 5.5", None)
    p3 = write(tmp_path / "c3.jsonl", [
        {"type": "assistant", "message": {"model": "claude-sonnet-5-5", "content": []},
         "effort": 5},
    ])
    assert describe_config(p3) == ("sonnet 5.5", None)


def test_scan_returns_activity_model_effort_newest(tmp_path):
    p = write(tmp_path / "s.jsonl", [
        tool("Bash"),
        {"type": "assistant",
         "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "reply"}]},
         "effort": "max"},
    ])
    assert scan(p) == Scan("writing a reply", "opus 5.5", "max", None, None, (), False)
    assert scan(p).activity == "writing a reply"
    assert scan(p).background == ()
    # model/effort come from the newest assistant record even if it has no concrete action
    assert describe_config(p) == ("opus 5.5", "max")


def test_scan_missing_file():
    from pathlib import Path
    assert scan(Path("/no/such/file")) == Scan(None, None, None, None, None, (), False)
    assert describe_config(Path("/no/such/file")) == (None, None)


def test_synthetic_api_error_records_are_skipped(tmp_path):
    """After a reboot without network Claude writes API errors as model "<synthetic>" messages."""
    p = write(tmp_path / "s.jsonl", [
        {"type": "assistant", "effort": "high",
         "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "done"}]}},
        {"type": "assistant", "isApiErrorMessage": True, "error": "server_error",
         "message": {"model": "<synthetic>",
                     "content": [{"type": "text", "text": "API Error: Can't reach the API server"}]}},
        {"type": "assistant",
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "No response requested."}]}},
    ])
    assert describe_config(p) == ("opus 5.5", "high")
    assert "API Error" not in (describe(p) or "")


# --- findings (SPEC §5, unread) ---------------------------------------------------------------
NARRATION = [
    "Let me check the config file to see how the cache is set up.",
    "Now I'll run the tests.",
    "The tests pass. Let me also look at the linter output before I commit, and then update the changelog entry "
    "so the release notes stay accurate.",                                                      # 173: still narration
    "Found it.\n\nLet me fix the import and rerun the failing test.",
    "Two things to check:\n- the cache key\n- the throttle",                                     # a list, but short
]
FINDINGS = [
    # a report: long enough on its own
    "The flaky test is a race, not a timeout. `wait_for` polls the status file every 50 ms, but the fake agent "
    "rewrites it with a plain open/write, so a poll can read it half-written and get invalid JSON, which the "
    "helper treats as no status. On a loaded CI runner that window is wide enough to hit about one run in thirty. "
    "Writing through a temp file and os.replace fixes it.",
    # shorter, but a list
    "The migration touches three tables and one of them is large:\n- `events` (40 M rows): needs a batched backfill\n"
    "- `users`: a new nullable column, instant\n- `sessions`: an index, built concurrently",
    # a heading
    "## Root cause\nThe registry lock is taken twice when a rename and a move land in the same daemon tick, so the "
    "second one waits for itself.",
    # a table
    "| test | before | after |\n|---|---|---|\n| startup | 1.9 s | 0.6 s |\n| first paint | 2.4 s | 0.9 s |\n"
    "| reload | 0.8 s | 0.3 s |",
    # a code block
    "The crash comes from this line in the parser, which assumes a trailing newline:\n```python\nhead, tail = "
    "text.rsplit(\"\\n\", 1)\n```\nAn input without one raises ValueError.",
]


@pytest.mark.parametrize("text", NARRATION)
def test_narration_is_not_a_finding(text):
    assert not is_finding(text)


@pytest.mark.parametrize("text", FINDINGS)
def test_reports_lists_headings_tables_code_are_findings(text):
    assert is_finding(text)


def _rec(kind, ts, **kw):
    """A transcript record: ("text", s), ("tool",), ("result",) or ("prompt", s), `ts` seconds after 10:00."""
    stamp = f"2026-11-11T10:00:{ts:02d}.000Z"
    if kind[0] == "text":
        return {"type": "assistant", "timestamp": stamp, "message": {"content": [{"type": "text", "text": kind[1]}]},
                **kw}
    if kind[0] == "tool":
        return {"type": "assistant", "timestamp": stamp,
                "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}, **kw}
    if kind[0] == "result":
        return {"type": "user", "timestamp": stamp,
                "message": {"content": [{"type": "tool_result", "content": "ok"}]}, **kw}
    return {"type": "user", "timestamp": stamp, "message": {"content": kind[1]}, **kw}


def _ms(ts):
    return 1794391200000 + ts * 1000           # 2026-11-11T10:00:00Z


def test_scan_reply_ms_is_the_newest_finding_mid_turn(tmp_path):
    """Narration between tool calls never counts; a finding counts once a tool call follows it in the turn."""
    p = write(tmp_path / "t.jsonl", [
        _rec(("prompt", "why does CI fail?"), 0),
        _rec(("text", NARRATION[0]), 1), _rec(("tool",), 2), _rec(("result",), 3),
        _rec(("text", FINDINGS[0]), 10), _rec(("tool",), 11), _rec(("result",), 12),
        _rec(("text", NARRATION[1]), 13), _rec(("tool",), 14),
    ])
    assert scan(p)[3] == _ms(10)
    narration_only = write(tmp_path / "n.jsonl", [
        _rec(("prompt", "go"), 0), *[_rec(("text", t), 1) for t in NARRATION], _rec(("tool",), 2),
    ])
    assert scan(narration_only)[3] is None


def test_scan_reply_ms_finding_and_tool_call_in_one_record(tmp_path):
    rec = _rec(("text", FINDINGS[1]), 5)
    rec["message"]["content"].append({"type": "tool_use", "name": "Edit", "input": {}})
    assert scan(write(tmp_path / "t.jsonl", [_rec(("prompt", "go"), 0), rec]))[3] == _ms(5)


def test_scan_reply_ms_not_at_the_end_of_a_turn(tmp_path):
    """A finding that ends its turn is the turn's answer (your turn), even once the next turn calls tools."""
    end = [_rec(("prompt", "go"), 0), _rec(("tool",), 1), _rec(("result",), 2), _rec(("text", FINDINGS[0]), 3)]
    assert scan(write(tmp_path / "a.jsonl", end))[3] is None
    nxt = end + [_rec(("prompt", "now fix it"), 4), _rec(("text", NARRATION[0]), 5), _rec(("tool",), 6)]
    assert scan(write(tmp_path / "b.jsonl", nxt))[3] is None


def test_scan_reply_ms_ignores_sidechains_and_api_errors(tmp_path):
    p = write(tmp_path / "t.jsonl", [
        _rec(("prompt", "go"), 0),
        _rec(("text", FINDINGS[0]), 1, isSidechain=True), _rec(("tool",), 2, isSidechain=True),
        _rec(("text", FINDINGS[2]), 3, isApiErrorMessage=True), _rec(("tool",), 4),
    ])
    assert scan(p)[3] is None


def test_scan_reply_ms_without_a_timestamp(tmp_path):
    """The newest finding without a usable timestamp: None, not an older finding's time."""
    newest = _rec(("text", FINDINGS[1]), 5)
    del newest["timestamp"]
    p = write(tmp_path / "t.jsonl", [_rec(("prompt", "go"), 0), _rec(("text", FINDINGS[0]), 1), _rec(("tool",), 2),
                                     newest, _rec(("tool",), 6)])
    assert scan(p)[3] is None


def test_scan_end_reply_ms_is_the_turn_ending_finding(tmp_path):
    """SPEC §5: the reply that ends a turn counts for end_reply_ms whether or not a tool call follows it."""
    p = write(tmp_path / "t.jsonl", [
        _rec(("prompt", "go"), 0),
        _rec(("text", FINDINGS[0]), 5),
    ])
    assert scan(p)[4] == _ms(5)
    assert scan(p)[3] is None, "reply_ms (mid-turn) is unchanged: no tool call follows"


def test_scan_end_reply_ms_none_for_short_narration(tmp_path):
    """The last turn's final text is short narration: nothing to show."""
    p = write(tmp_path / "t.jsonl", [
        _rec(("prompt", "go"), 0),
        _rec(("text", NARRATION[0]), 5),
    ])
    assert scan(p)[4] is None


def test_scan_end_reply_ms_ignores_older_turns(tmp_path):
    """A finding in an older turn (before the last prompt) does not count for end_reply_ms."""
    p = write(tmp_path / "t.jsonl", [
        _rec(("prompt", "go"), 0),
        _rec(("text", FINDINGS[0]), 5), _rec(("tool",), 6), _rec(("result",), 7),
        _rec(("prompt", "again"), 10),
        _rec(("text", NARRATION[0]), 11),
    ])
    assert scan(p)[4] is None, "the older-turn finding is before the last prompt"


def test_scan_end_reply_ms_newest_of_the_turn(tmp_path):
    p = write(tmp_path / "t.jsonl", [
        _rec(("prompt", "go"), 0),
        _rec(("text", FINDINGS[0]), 5), _rec(("tool",), 6),
        _rec(("text", FINDINGS[1]), 8),
    ])
    assert scan(p)[4] == _ms(8), "newest finding in the newest turn"
    assert scan(p)[3] == _ms(5), "reply_ms is the mid-turn finding, unchanged"


# --- background tasks (SPEC §5) -------------------------------------------------

def _bg(task_id, tool="Bash", desc="run the checks", command="", started=True):
    return Background(tool_use_id=task_id, tool=tool, description=desc, command=command,
                   started_ms=_ms(started) if started else None)


def test_scan_collects_launched_background_tasks(tmp_path):
    """Bash with run_in_background, Agent with run_in_background, and Monitor all count as launched."""
    p = write(tmp_path / "t.jsonl", [
        {"type": "assistant", "timestamp": "2026-11-11T10:00:01.000Z",
         "message": {"content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {
             "command": "pytest -q", "description": "run the tests",
             "run_in_background": True}}]}},
        {"type": "assistant", "timestamp": "2026-11-11T10:00:02.000Z",
         "message": {"content": [{"type": "tool_use", "id": "a1", "name": "Agent", "input": {
             "description": "plan the refactor", "run_in_background": True}}]}},
        {"type": "assistant", "timestamp": "2026-11-11T10:00:03.000Z",
         "message": {"content": [{"type": "tool_use", "id": "m1", "name": "Monitor", "input": {
             "description": "watch the deploy"}}]}},
    ])
    s = scan(p)
    assert [t.tool for t in s.background] == ["Bash", "Agent", "Monitor"]    # oldest first
    bash = s.background[0]
    assert bash.command == "pytest -q" and bash.tool_use_id == "b1"
    assert s.launched_any is True


def test_scan_background_ended_by_notice(tmp_path):
    """A notice with a final <status> ends the task (Monitors' event notices do not)."""
    def launch(tool, tid, ts, desc):
        return {"type": "assistant", "timestamp": f"2026-11-11T10:00:{ts:02d}.000Z",
               "message": {"content": [{"type": "tool_use", "id": tid, "name": tool, "input": {
                   "description": desc, "run_in_background": True}}]}}
    def notice(tid, status, ts):
        return {"type": "user", "timestamp": f"2026-11-11T10:00:{ts:02d}.000Z",
               "message": {"content": f"<task-notification><tool-use-id>{tid}</tool-use-id>"
                                     f"<status>{status}</status></task-notification>"}}
    p = write(tmp_path / "t.jsonl", [
        launch("Bash", "b1", 1, "run pytest"),
        notice("b1", "completed", 2),
        launch("Bash", "b2", 3, "run more"),
        notice("b2", "event", 4),       # a Monitor-style event notice: not a final status
        launch("Monitor", "m1", 5, "monitor prod"),
        notice("m1", "running", 6),    # an event notice: not ended
    ])
    s = scan(p)
    assert [t.tool_use_id for t in s.background] == ["b2", "m1"]   # b1 completed -> dropped


def test_scan_notice_with_two_blocks_ends_both(tmp_path):
    p = write(tmp_path / "t.jsonl", [
        {"type": "assistant", "timestamp": "2026-11-11T10:00:01.000Z",
         "message": {"content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {
             "description": "a", "run_in_background": True}}]}},
        {"type": "assistant", "timestamp": "2026-11-11T10:00:02.000Z",
         "message": {"content": [{"type": "tool_use", "id": "a1", "name": "Agent", "input": {
             "description": "b", "run_in_background": True}}]}},
        {"type": "user", "message": {"content": [{"type": "text",
             "text": ("<task-notification><tool-use-id>b1</tool-use-id><status>stopped</status></task-notification>"
                      "<task-notification><tool-use-id>a1</tool-use-id><status>failed</status></task-notification>")}]}},
    ])
    assert scan(p).background == ()


def test_scan_background_sidechain_ignored(tmp_path):
    p = write(tmp_path / "t.jsonl", [
        {"type": "assistant", "timestamp": "2026-11-11T10:00:01.000Z", "isSidechain": True,
         "message": {"content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {
             "description": "bg", "run_in_background": True}}]}},
    ])
    s = scan(p)
    assert s.background == () and s.launched_any is False


def test_is_endless_patterns():
    # SPEC §5 "Endless commands": servers/followers never make a session watching.
    assert is_endless("gh pr checks 31 --watch") is False
    assert is_endless("tsc --watch") is True
    assert is_endless("npm run build") is False
    assert is_endless("npm run dev") is True
    assert is_endless("docker compose up -d") is False
    assert is_endless("docker compose up") is True
    assert is_endless("docker-compose up --detach") is False
    assert is_endless("tail -f x.log") is True
    assert is_endless("pytest -q") is False
    assert is_endless("while :; do run; sleep 120; done") is False
    assert is_endless("vite") is True
    assert is_endless("vite build") is False
    assert is_endless("cargo watch") is True
    assert is_endless("jest --watch") is True
    assert is_endless("python -m http.server 8000") is True


def test_waiting_tasks_cutoffs():
    now = _ms(0)
    bg = [_bg("b1", started=1), _bg("a1", tool="Agent", started=2), _bg("m1", tool="Monitor", started=3)]
    # Bash counts only with status shell
    assert [t.tool_use_id for t in waiting_tasks("idle", bg, None, now)] == ["a1", "m1"]
    assert [t.tool_use_id for t in waiting_tasks("shell", bg, None, now)] == ["b1", "a1", "m1"]
    assert [t.tool_use_id for t in waiting_tasks("shell", [_bg("b1", started=1), _bg("b2", command="npm run dev", started=2)],
                                                 None, now)] == ["b1"]
    # older than the process start -> dropped (when both known)
    assert [t.tool_use_id for t in waiting_tasks("shell", [_bg("b1", started=1)], _ms(10), now)] == []
    # older than 24 h -> dropped; a task with no started_ms is kept
    assert [t.tool_use_id for t in waiting_tasks("shell", [_bg("b1", started=1)], None, _ms(90000))] == []
    assert [t.tool_use_id for t in waiting_tasks("shell", [_bg("b1", started=None)], None, now)] == ["b1"]
