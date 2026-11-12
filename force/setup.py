#!/usr/bin/env python3
# force.setup: idempotent first-run / update installer for a released Force tree.
#
#   python -m force.setup --release DIR --python PY [--no-fonts] [--no-services] [--self-test]
#
# run by share/get-force.sh (`curl -fsSL <base>/install.sh | sh`) and by `force update`. Every step
# prints one short `force: …` line (orange when stdout is a tty, plain otherwise); the whole thing is
# idempotent, so re-running on an already set-up machine changes nothing and exits 0.
#
# Each step is a small function taking explicit paths / an env dict / a `run` callable, so the tests can
# drive them with a fake HOME and a recording fake `run`.
from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from force import services
from force.config import WSL_ICONS_HINT, get_paths, is_wsl

# The one-line prefix used by every message ("force", orange like share/fonts.sh's `say`).
COLOR = "\033[1;38;2;217;119;87mforce\033[0m"


class SetupError(Exception):
    """A step failed; print the message and stop without switching anything."""


def say(msg: str) -> None:
    if sys.stdout.isatty():
        print(f"{COLOR} {msg}")
    else:
        print(f"force: {msg}")


def _make_run():
    def run(argv, **kw):
        """A command's result; a program that is not installed (no system service tools, fc-list, no bash in a
        container) is exit status 127, like a shell would report it, never an exception."""
        try:
            return subprocess.run(argv, capture_output=True, text=True, **kw)
        except FileNotFoundError as e:
            return subprocess.CompletedProcess(argv, 127, "", str(e))
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess(argv, 124, "", f"timed out after {e.timeout} s")
    return run


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# --- step 1: the release is a real Force release ----------------------------------------------
def check_release(rel) -> str:
    rel = Path(rel)
    version = (rel / "VERSION").read_text().strip() if (rel / "VERSION").is_file() else ""
    if not version:
        # a development release (deploy.sh, pushed with force install-to): named after its directory
        version = f"dev {rel.name}"
    elif not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SetupError(f"{rel}/VERSION is not X.Y.Z ({version!r})")
    for required in ("force/cli.py", "share/tmux.conf", "share/bin/force-sidebar"):
        if not (rel / required).is_file():
            raise SetupError(f"{rel} is not a Force release: missing {required}")
    return version


# --- step 2: tmux and its configs parse, on a throwaway socket --------------------------------
def _tmux_bin(rel: Path, env) -> str | None:
    bundled = rel / "share" / "tmux-bin" / "tmux"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return shutil.which("tmux", path=env.get("PATH", ""))


def _unlink_socket(name: str) -> None:
    try:
        os.unlink(f"/tmp/tmux-{os.getuid()}/{name}")
    except OSError:
        pass


def check_tmux(rel, run, env) -> None:
    tmux = _tmux_bin(Path(rel), env)
    if not tmux:
        raise SetupError("Force needs tmux; this release should include it")
    sock = f"force-setupcheck-{os.getpid()}"
    try:
        r = run([tmux, "-L", sock, "-f", "/dev/null", "new-session", "-d", "-s", "chk", "sleep", "30"])
        if r.returncode != 0:
            raise SetupError("tmux would not start a check session")
        for conf in ("share/tmux.conf", "share/tmux-ui.conf"):
            r = run([tmux, "-L", sock, "source-file", "-n", str(Path(rel) / conf)])
            if r.returncode != 0:
                raise SetupError(f"tmux rejected {conf}: this release's tmux config does not parse")
    finally:
        try:
            run([tmux, "-L", sock, "kill-server"])
        except Exception:
            pass
        _unlink_socket(sock)


# --- step 3: point <data>/app at the new release --------------------------------------------
def switch_app(data, rel, version) -> None:
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    app = data / "app"
    tmp = data / f"app.tmp.{os.getpid()}"
    try:
        os.symlink(os.path.abspath(rel), tmp)
        os.replace(tmp, app)
    finally:
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    say(f"Force {version} installed")


# --- step 4: the ~/.local/bin/force launcher ------------------------------------------------
_LAUNCHER = """#!{py} -I
# Force launcher (written by `force setup`); it runs the live release, {data}/app.
import sys
sys.path.insert(0, "{data}/app")
from force.cli import main
sys.exit(main())
"""


def write_launcher(py, data, home) -> Path:
    data = os.path.abspath(data)
    launcher = home / ".local" / "bin" / "force"
    _atomic_write(launcher, _LAUNCHER.format(py=py, data=data), 0o755)
    return launcher


# --- step 5: put ~/.local/bin on PATH for new terminals and `ssh -t HOST force` ---------------
# The old install.sh block we must replace (its exact marker), so we never leave two blocks behind.
_OLD_BASHRC_MARK = "# force: ~/.local/bin also for non-interactive ssh commands (ssh -t <host> force)"
# The new marked block: at the very TOP of ~/.bashrc, before Debian's non-interactive early return.
_BASHRC_BLOCK = (
    "# force: ~/.local/bin on PATH (also for ssh -t HOST force)\n"
    'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac\n'
    "# force end\n"
)
_FISH_BLOCK = (
    "# force: ~/.local/bin on PATH (also for ssh -t HOST force)\n"
    "fish_add_path -g $HOME/.local/bin\n"
)


def _strip_old_bashcr_block(content: str) -> str:
    lines = content.splitlines(keepends=True)
    out = []
    i = 0
    while i < len(lines):
        if lines[i].rstrip("\n") == _OLD_BASHRC_MARK:
            i += 1
            if i < len(lines) and lines[i].lstrip().startswith('case ":$PATH:"'):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def _ensure_bash(home) -> None:
    bashrc = home / ".bashrc"
    content = ""
    if bashrc.exists():
        old = bashrc.read_text()
        if _BASHRC_BLOCK.splitlines()[0] in old:          # new block already present
            return
        content = _strip_old_bashcr_block(old)
    _atomic_write(bashrc, _BASHRC_BLOCK + content)


def _append_zshenv(home) -> None:
    zshenv = home / ".zshenv"
    content = ""
    if zshenv.exists():
        content = zshenv.read_text()
        if _BASHRC_BLOCK.splitlines()[0] in content:
            return
        if content and not content.endswith("\n"):
            content += "\n"
    _atomic_write(zshenv, content + _BASHRC_BLOCK)


def _ensure_fish(home) -> None:
    path = home / ".config" / "fish" / "conf.d" / "force.fish"
    _atomic_write(path, _FISH_BLOCK)


def update_paths(home, env, run) -> bool:
    """Add ~/.local/bin to the shell configs the machine uses; return whether it is already on THIS process's
    PATH (the caller reminds the user when not)."""
    shell = (env.get("SHELL") or "").replace("\\", "/").rsplit("/", 1)[-1]
    bin_dir = str(home / ".local" / "bin")
    on_path = bin_dir in [p for p in env.get("PATH", "").split(":") if p]
    if (home / ".bashrc").exists() or shell.endswith("bash"):
        _ensure_bash(home)
    if (home / ".zshrc").exists() or shell.endswith("zsh"):
        _append_zshenv(home)
    if (home / ".config" / "fish").exists() or shell.endswith("fish"):
        _ensure_fish(home)
    return on_path


# --- step 6: the user's services (systemd on Linux, launchd on macOS, SPEC §51) --------------
def _linger(run, user) -> None:
    """Thin wrapper: linger lives in force/services (SPEC §51)."""
    services._linger(run, user)


def setup_services(rel, home, run, no_services, self_test, user) -> None:
    if no_services:
        return
    services.install(rel, home, run, self_test, user)


# --- step 7: the icon font on a desktop ------------------------------------------------------
def setup_fonts(rel, env, run, wsl=None) -> None:
    if (is_wsl(env) if wsl is None else wsl):
        # the terminal is a Windows program: fonts.sh puts the symbols font into Windows and adds a "Force"
        # profile to Windows Terminal (a Linux font would never reach it)
        r = run(["bash", str(Path(rel) / "share" / "fonts.sh")], timeout=180)
        if r.returncode == 0:
            say(f'Windows Terminal: restart it once, then open "Force ({env.get("WSL_DISTRO_NAME") or "WSL"})" '
                "from its new-tab menu for Force with icons")
        else:
            say(WSL_ICONS_HINT)
        return
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return                                                        # headless: nothing to do
    fc = shutil.which("fc-list", path=env.get("PATH", ""))
    if not fc:
        return
    r = run([fc, ":", "family"])
    if r.returncode != 0 or "symbols nerd font" in r.stdout.lower():
        return                                                        # font present (or no fontconfig)
    r = run(["bash", str(Path(rel) / "share" / "fonts.sh")], timeout=120)
    if r.returncode != 0:
        say("note: icon font not installed (force fonts tries again)")


# --- step 8: is Claude Code around for agent sessions -------------------------------------------
def check_claude(paths) -> None:
    if not os.access(paths.claude_bin, os.X_OK):
        say("Claude Code is not installed: Force runs shell sessions; for agent sessions install it with:")
        say("  curl -fsSL https://claude.ai/install.sh | bash")


# --- step 9: prune old releases --------------------------------------------------------------
def prune(data, live_rel) -> None:
    releases = Path(data) / "releases"
    if not releases.is_dir():
        return
    live = str(Path(live_rel).resolve())
    others = [d for d in releases.iterdir() if d.is_dir() and str(d.resolve()) != live]
    others.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for old in others[5:]:
        shutil.rmtree(old, ignore_errors=True)


# --- driver ----------------------------------------------------------------------------------
def run_setup(rel, py, no_fonts=False, no_services=False, self_test=False, env=None, run=None) -> int:
    env = dict(os.environ) if env is None else env
    run = _make_run() if run is None else run
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    data = Path(os.path.abspath(env.get("FORCE_HOME") or str(home / ".local" / "share" / "force")))

    version = check_release(rel)
    check_tmux(rel, run, env)
    switch_app(data, rel, version)
    write_launcher(py, data, home)
    on_path = update_paths(home, env, run)
    user = env.get("USER") or getpass.getuser()
    setup_services(rel, home, run, no_services, self_test, user)
    if not no_fonts:
        setup_fonts(rel, env, run)
    check_claude(get_paths(env))
    prune(data, rel)
    say("ready. Type: force")
    if not on_path:
        say('open a new terminal first (or run: export PATH="$HOME/.local/bin:$PATH")')
    return 0


def main(argv=None) -> int:
    # a development instance (SPEC §2.1) never writes the live install's launcher, units, PATH blocks or fonts
    inst = os.environ.get("FORCE_INSTANCE")
    if inst:
        print(f"force: this is the {inst} instance (FORCE_INSTANCE={inst}); set it up with ./dev.sh", file=sys.stderr)
        return 2
    try:
        return _main(argv)
    except SetupError as e:
        print(f"force: {e}", file=sys.stderr)
        return 1


def _main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = list(argv)
    rel = py = None
    no_fonts = no_services = self_test = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--release":
            i += 1
            if i >= len(args):
                raise SetupError("--release needs an argument")
            rel = args[i]
        elif a == "--python":
            i += 1
            if i >= len(args):
                raise SetupError("--python needs an argument")
            py = args[i]
        elif a == "--no-fonts":
            no_fonts = True
        elif a == "--no-services":
            no_services = True
        elif a == "--self-test":
            self_test = True
        else:
            raise SetupError(f"unknown argument: {a}")
        i += 1
    if not rel or not py:
        raise SetupError("usage: force.setup --release DIR --python PY [--no-fonts] [--no-services] [--self-test]")
    try:
        return run_setup(rel, py, no_fonts=no_fonts, no_services=no_services, self_test=self_test)
    except SetupError as e:
        print(f"force: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
