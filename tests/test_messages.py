"""Messages between hosts (SPEC §52): targets, routing, --from, the message log, peers and the outbox."""
import dataclasses
import json
import os
import time

import pytest

from force import messages, remote
from force.config import Config


def test_parse_target():
    assert messages.parse_target("portal") == (None, "portal")
    assert messages.parse_target("Old_Name") == (None, "Old_Name")      # plain names go on unchecked (no_session)
    assert messages.parse_target("lap:portal") == ("lap", "portal")
    assert messages.parse_target("build.dev:portal") == ("build.dev", "portal")
    with pytest.raises(messages.automation.UsageError):
        messages.parse_target("bad host:portal")          # bad host (a space)
    with pytest.raises(messages.automation.UsageError):
        messages.parse_target("lap:BadName")             # bad name (upper case: not a force name)
    with pytest.raises(messages.automation.UsageError):
        messages.parse_target("a:b:c")                  # two colons
    with pytest.raises(messages.automation.UsageError):
        messages.parse_target("lap:/etc")               # a name with a path separator


def test_parse_target_host_regex_length(env):
    assert messages.parse_target("h" * 64 + ":portal") == ("h" * 64, "portal")
    with pytest.raises(messages.automation.UsageError):
        messages.parse_target("h" * 65 + ":portal")   # host too long


def test_resolve_local(env):
    assert messages.resolve(env.paths, messages.short_host()) == ("local", None)


def test_resolve_remote_by_name(env):
    remote.save(env.paths, [remote.Remote("build", "build")])
    assert messages.resolve(env.paths, "build") == ("remote", remote.Remote("build", "build"))


def test_resolve_remote_by_hello_short_host(env):
    remote.save(env.paths, [remote.Remote("b1", "b1")])
    remote.write_status(env.paths, "b1", "connected", "build.example.com")
    kind, target = messages.resolve(env.paths, "build")
    assert kind == "remote" and target.name == "b1"


def test_resolve_live_peer(env):
    messages.mark_peer(env.paths, "lap", os.getpid())
    assert messages.resolve(env.paths, "lap") == ("peer", "lap")


def test_resolve_dead_peer_no_route(env):
    messages.mark_peer(env.paths, "lap", 2 ** 22)      # a pid that is not alive
    with pytest.raises(messages.automation.CmdError) as ei:
        messages.resolve(env.paths, "lap")
    assert ei.value.code == "no_route"


def test_resolve_unknown_no_route(env):
    with pytest.raises(messages.automation.CmdError) as ei:
        messages.resolve(env.paths, "ghost")
    assert ei.value.code == "no_route"


def test_parse_from():
    assert messages.parse_from("abc@lap") == ("abc", "lap")
    assert messages.parse_from("@lap") == ("", "lap")
    assert messages.parse_from("ab-c@build.example.com") == ("ab-c", "build.example.com")
    for bad in ("no-at", "A@lap", "a@b@c", "a@bad host", "@"):
        with pytest.raises(messages.automation.UsageError):
            messages.parse_from(bad)


def test_accepts():
    cfg = Config()
    assert not messages.accepts(cfg, "lap")                       # empty (off)
    cfg = dataclasses.replace(cfg, accept_prompts_from="lap other")
    assert messages.accepts(cfg, "lap") and messages.accepts(cfg, "other")
    assert not messages.accepts(cfg, "ghost")
    cfg = dataclasses.replace(cfg, accept_prompts_from="lap,other")
    assert messages.accepts(cfg, "other") and not messages.accepts(cfg, "ghost")
    cfg = dataclasses.replace(cfg, accept_prompts_from="*")
    assert messages.accepts(cfg, "anyhost")


def test_header():
    assert messages.header("abc", "lap") == '[from abc on lap · reply: force prompt lap:abc "…"]'
    assert messages.header("", "lap") == "[from lap]"


def test_log_writes_one_line_without_text(env):
    env.paths.state.mkdir(parents=True, exist_ok=True)
    messages.log(env.paths, dir="out", cmd="prompt", from_="a@h", to="s", result="sent", length=3)
    lines = (env.paths.state / "messages.log").read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["dir"] == "out" and data["cmd"] == "prompt" and data["result"] == "sent"
    assert data["from_"] == "a@h" and data["to"] == "s" and data["length"] == 3 and "at" in data
    assert "secret text" not in lines[0]


def test_queue_drain_order_and_delete(env):
    messages.queue(env.paths, "lap", {"to": "a", "text": "one", "from": "s@me", "at": 100.0})
    time.sleep(0.001)
    messages.queue(env.paths, "lap", {"to": "b", "text": "two", "from": "s@me", "at": 101.0})
    items = messages.drain(env.paths, "lap", max_age_s=1000, now=200.0)
    assert [i["text"] for i in items] == ["one", "two"]
    assert not list((env.paths.state / "outbox" / "lap").iterdir())     # deleted on read


def test_drain_expired_dropped_and_logged(env):
    messages.queue(env.paths, "lap", {"to": "a", "text": "old", "from": "s@me", "at": 100.0})
    assert messages.drain(env.paths, "lap", max_age_s=10, now=200.0) == []
    assert not list((env.paths.state / "outbox" / "lap").iterdir())
    assert any(json.loads(l).get("result") == "expired"
               for l in (env.paths.state / "messages.log").read_text().splitlines())


def test_mark_unmark_peer_alive(env):
    assert not messages.peer_alive(env.paths, "lap")
    messages.mark_peer(env.paths, "lap", os.getpid())
    assert messages.peer_alive(env.paths, "lap")
    messages.unmark_peer(env.paths, "lap", os.getpid() + 1)     # a different pid: no change
    assert messages.peer_alive(env.paths, "lap")
    messages.unmark_peer(env.paths, "lap", os.getpid())
    assert not messages.peer_alive(env.paths, "lap")
