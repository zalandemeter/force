import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from force.config import Config, get_paths, load_config
from force.lifecycle import Cockpit
from force.tmux import Tmux

sys.path.insert(0, str(Path(__file__).parent))

# a development instance's variables (SPEC §2.1) from the developer's shell must not leak into the tests
os.environ.pop("FORCE_INSTANCE", None)
os.environ.pop("FORCE_BIN", None)

# The service managers that start and stop the developer's own Force. A test that reached one for real once ran
# `systemctl --user disable --now force.service force-tmux.service` and stopped every live session.
_SERVICE_MANAGERS = frozenset({"systemctl", "launchctl", "loginctl"})


class _NoServiceManagerPopen(subprocess.Popen):
    """subprocess.Popen (under run, call, check_output) that refuses the service managers, as on a machine without
    one: FileNotFoundError, so force/services.backend() is None. Tests that need a manager inject a fake `run`."""

    def __init__(self, args, *a, **kw):
        if isinstance(args, (str, bytes)):
            words = os.fsdecode(args).split()
            program = words[0] if words else ""
        elif isinstance(args, os.PathLike):
            program = os.fsdecode(args)
        else:
            args = list(args)
            program = os.fsdecode(args[0]) if args else ""
        if os.path.basename(program) in _SERVICE_MANAGERS:
            raise FileNotFoundError(2, "the live service manager is off limits in tests", program)
        super().__init__(args, *a, **kw)


@pytest.fixture(autouse=True)
def _no_live_service_manager(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _NoServiceManagerPopen)


def make_fakecockpit(tmp: Path, paths) -> str:
    """A tmp script that stands in for the force CLI (which is written elsewhere). Implements `run NAME`
    (execs fake claude via build_argv) and `sidebar` (a long sleep)."""
    repo = str(Path(__file__).parent.parent)
    py = sys.executable
    lines = [
        f"#!{py}",
        "import os, sys",
        f"sys.path.insert(0, {repo!r})",
        "from force.registry import locked",
        "from force.claude import build_argv",
        "from force.config import get_paths",
        "from force.registry import Entry",
        "def main():",
        "    argv = sys.argv[1:]",
        "    if not argv or argv[0] in ('sidebar', 'header', 'nested'):",
        "        os.execvp('sleep', ['sleep', '1000000'])",
        "    if argv[0] == 'run':",
        "        name = argv[1]",
        "        paths = get_paths()",
        "        with locked(paths.home) as reg:",
        "            e = reg.get(name)",
        "            if e is None or e.archived:",
        "                print('no such session: %s' % name, file=sys.stderr); sys.exit(2)",
        "            nm, sid, cwd = e.name, e.session_id, e.cwd",
        "            memory_dir = e.extra.get('memory_dir')",
        "            shell = e.extra.get('kind') == 'shell'",
        "        if shell:",
        "            os.environ['FORCE_SESSION'] = nm",
        "            os.chdir(cwd)",
        "            os.execvp('sleep', ['sleep', '1000000'])",
        "        argv2 = build_argv(paths.claude_bin, paths.claude_dir, nm, sid, memory_dir=memory_dir)",
        "        os.environ['FORCE_SESSION'] = nm",
        "        os.chdir(cwd)",
        "        os.execvp(argv2[0], argv2)",
        "    sys.exit(2)",
        "main()",
    ]
    script = tmp / "fakecockpit"
    script.write_text("\n".join(lines))
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    state = tmp_path / "state"
    claude_dir = tmp_path / "claude"
    bin_dir = tmp_path / "bin"
    for d in (home, state, claude_dir):
        d.mkdir(parents=True, exist_ok=True)

    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g exit-empty off\nset -g remain-on-exit on\n")
    (tmp_path / "tmux-ui.conf").write_text("set -g exit-empty off\nset -g remain-on-exit on\n")

    versions = tmp_path / "versions"
    versions.mkdir(exist_ok=True)
    fake_src = Path(__file__).parent / "fake_claude.py"
    target = versions / "9.9.9"
    shutil.copy2(fake_src, target)
    target.chmod(0o755)
    bin_dir.mkdir(exist_ok=True)
    claude_bin = bin_dir / "claude"
    if claude_bin.exists() or claude_bin.is_symlink():
        claude_bin.unlink()
    claude_bin.symlink_to(target)

    socket = f"forcetest-{uuid.uuid4().hex}"
    sconf = tmp_path / "settings.json"
    # the fixture's default layout is classic (new installs get split); tests that want split set it themselves
    sconf.write_text(json.dumps({"layout": "classic"}))
    os.environ["FORCE_HOME"] = str(home)
    os.environ["FORCE_STATE"] = str(state)
    os.environ["FORCE_CLAUDE_DIR"] = str(claude_dir)
    os.environ["FORCE_CONFIG"] = str(sconf)
    os.environ["FORCE_TMUX_SOCKET"] = socket
    os.environ["FORCE_UI_SOCKET"] = socket + "-ui"      # never the live ui server (split mode)
    os.environ["FORCE_TMUX_CONF"] = str(conf)
    os.environ["FORCE_CLAUDE_BIN"] = str(claude_bin)

    paths = get_paths()
    cfg = load_config(paths.config_file)
    tmux = Tmux(paths.tmux_socket, paths.tmux_conf)

    ns = SimpleNamespace(
        tmp=tmp_path, paths=paths, cfg=cfg, tmux=tmux, socket=socket,
        claude_bin=claude_bin, home=home, state=state, claude_dir=claude_dir,
        cockpit=None, cockpit_bin=None,
        make_cockpit=lambda: _build_cockpit(ns),
    )
    try:
        yield ns
    finally:
        # teardown: kill this test socket, even if the test failed.
        for sock in (socket, socket + "-ui"):
            try:
                subprocess.run(["tmux", "-L", sock, "kill-server"],
                             capture_output=True, timeout=15)
            except Exception:
                pass
        # remove the socket file so it cannot linger.
        for sock in (socket, socket + "-ui"):
            try:
                os.unlink(f"/tmp/tmux-{os.getuid()}/{sock}")
            except (OSError, ValueError):
                pass


def _build_cockpit(env):
    env.cockpit_bin = make_fakecockpit(env.tmp, env.paths)
    env.cockpit = Cockpit(env.paths, env.cfg, env.tmux, env.cockpit_bin)
    return env.cockpit


@pytest.fixture
def cockpit(env):
    return env.make_cockpit()


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self):
        return f"NS({self.__dict__})"


@pytest.fixture(scope="session", autouse=True)
def _remove_test_sockets():
    """tmux 3.4 leaves the socket file behind after kill-server. Tests that name their own servers
    `forcetest-…-<pid>` kill them; this removes the files they leave once the run is over."""
    yield
    sock_dir = Path(f"/tmp/tmux-{os.getuid()}")
    for sock in sock_dir.glob(f"forcetest-*-{os.getpid()}"):
        if subprocess.run(["tmux", "-L", sock.name, "list-sessions"], capture_output=True).returncode == 0:
            continue                                  # still has a live server: leave it
        try:
            sock.unlink()
        except OSError:
            pass
