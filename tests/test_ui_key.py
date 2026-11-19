"""share/ui-key.sh in a window showing a remote (SPEC §40): the round trips each key makes. The remote is a test
tmux server (forcetest-*) reached through a fake link that logs every call; `force` is a fake that logs every call
and does what the remote's `force switch` / `force new` would. Never the live sockets."""
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_KEY = ROOT / "share" / "ui-key.sh"

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux")

FAKE_LINK = """#!/bin/sh
# @force_host_tmux = this script + `tmux -L force`: one round trip to the remote
echo "$*" >> "{log}"
shift 3
exec tmux -L "{host}" "$@"
"""

FAKE_FORCE = """#!/bin/sh
# the remote's force, reached with FORCE_HOST (a forwarded command: one round trip)
echo "force $*" >> "{log}"
[ -n "$FORCE_HOST" ] || {{ echo "no FORCE_HOST" >&2; exit 9; }}
cmd=$1; shift
case "$cmd" in
  switch)
    idx=""; client=""; print=""; dir=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --index) idx=$2; shift ;;
        --client) client=$2; shift ;;
        --next|--prev) dir=$1 ;;
        --print) [ -n "{old}" ] && {{ echo "force: error: unrecognized arguments: --print" >&2; exit 2; }}; print=1 ;;
      esac
      shift
    done
    [ -n "$dir" ] && idx=3
    tmux -L "{host}" switch-client -c "$client" -t "=s$idx" || exit 2
    [ -n "$print" ] && tmux -L "{host}" display -p -c "$client" '#{{client_session}}'
    exit 0 ;;
  new) tmux -L "{host}" new-session -d -s "$1" "sleep 600"; exit $? ;;
esac
exit 1
"""


@pytest.fixture
def remote_window(tmp_path):
    """A ui server window whose main pane is a client of the 'remote' (another test server), set up as a ui
    window showing remote devbox."""
    tag = uuid.uuid4().hex[:10]
    ui, host = f"forcetest-uikey-{tag}-ui", f"forcetest-uikey-{tag}"
    log = tmp_path / "calls.log"
    made = {}

    def make(old_force=False):
        link = tmp_path / "fakelink"
        link.write_text(FAKE_LINK.format(log=log, host=host))
        force = tmp_path / "force"
        force.write_text(FAKE_FORCE.format(log=log, host=host, old="1" if old_force else ""))
        for f in (link, force):
            f.chmod(0o755)
        for s in ("s1", "s2", "s3"):
            subprocess.run(["tmux", "-L", host, "-f", "/dev/null", "new-session", "-d", "-s", s, "sleep 600"],
                           check=True)
        subprocess.run(["tmux", "-L", ui, "-f", "/dev/null", "new-session", "-d", "-s", "ui", "-x", "100", "-y", "30",
                        f"TMUX= tmux -L {host} attach -t s1"], check=True)
        nested = ""
        for _ in range(100):
            nested = subprocess.run(["tmux", "-L", host, "list-clients", "-F", "#{client_name}"],
                                    capture_output=True, text=True).stdout.strip()
            if nested:
                break
            time.sleep(0.05)
        assert nested, "the nested client attached"
        win = subprocess.run(["tmux", "-L", ui, "display", "-p", "-t", "ui", "#{window_id}"],
                             capture_output=True, text=True).stdout.strip()
        sock = subprocess.run(["tmux", "-L", ui, "display", "-p", "-t", "ui", "#{socket_path}"],
                              capture_output=True, text=True).stdout.strip()
        for k, v in (("@force_host", "devbox"), ("@force_host_tmux", f"{link} tmux -L force"),
                     ("@force_nested", nested), ("@force_session", "s1")):
            subprocess.run(["tmux", "-L", ui, "set-option", "-w", "-t", win, k, v], check=True)
        made.update(ui=ui, host=host, win=win, sock=sock, nested=nested, force=force)
        return made

    def key(action, arg=None):
        log.write_text("")
        env = dict(os.environ, FORCE_BIN=str(made["force"]))
        env.pop("FORCE_HOST", None)
        subprocess.run(["sh", str(UI_KEY), made["sock"], made["win"], action, *([arg] if arg else [])],
                       env=env, timeout=30, check=True)
        calls = [line for line in log.read_text().splitlines() if line]
        shown = subprocess.run(["tmux", "-L", host, "display", "-p", "-c", made["nested"], "#{client_session}"],
                               capture_output=True, text=True).stdout.strip()
        published = subprocess.run(["tmux", "-L", ui, "show", "-wqv", "-t", made["win"], "@force_session"],
                                   capture_output=True, text=True).stdout.strip()
        return calls, shown, published

    try:
        yield make, key
    finally:
        for s in (ui, host):
            subprocess.run(["tmux", "-L", s, "kill-server"], capture_output=True)


def test_switch_by_index_is_one_round_trip(remote_window):
    make, key = remote_window
    make()
    calls, shown, published = key("idx", "2")
    assert calls == ["force switch --index 2 --client " + calls[0].split("--client ")[1]]
    assert calls[0].endswith(" --print")
    assert shown == "s2" and published == "s2"


def test_next_is_one_round_trip(remote_window):
    make, key = remote_window
    make()
    calls, shown, published = key("next")
    assert len(calls) == 1 and calls[0].startswith("force switch --next")
    assert shown == "s3" and published == "s3"


def test_last_is_one_round_trip(remote_window):
    make, key = remote_window
    make()
    key("idx", "2")
    calls, shown, published = key("last")
    assert len(calls) == 1 and calls[0].startswith("tmux -L force switch-client") and "display" in calls[0]
    assert shown == "s1" and published == "s1"


def test_a_failed_switch_still_asks_what_is_shown(remote_window):
    """No last session: the switch fails, and what the client shows is asked on its own, as before."""
    make, key = remote_window
    make()
    calls, shown, published = key("last")
    assert len(calls) == 2 and "display -p" in calls[1]
    assert shown == "s1" and published == "s1"


def test_new_shell_is_two_round_trips(remote_window):
    make, key = remote_window
    made = make()
    calls, shown, published = key("newshell", "sh1")
    assert [c.split()[0:2] for c in calls] == [["force", "new"], ["tmux", "-L"]]
    assert shown == "sh1" and published == "sh1"
    subprocess.run(["tmux", "-L", made["host"], "has-session", "-t", "=sh1"], check=True)


def test_a_remote_without_print_switches_as_before(remote_window):
    """A remote on an older Force refuses --print: it switches without it and is asked (two more calls)."""
    make, key = remote_window
    make(old_force=True)
    calls, shown, published = key("idx", "3")
    assert [c.split()[0] for c in calls] == ["force", "force", "tmux"]
    assert "--print" in calls[0] and "--print" not in calls[1]
    assert shown == "s3" and published == "s3"
