"""Chuck, Force's support assistant (SPEC §50).

One reused agent session (`chuck`, registry extra role = "support") on the user's own agent (setting
supportAgent), in its own folder <home>/support with a fixed context pack (share/support/: CHUCK.md as CLAUDE.md
and AGENTS.md, the docs, the command reference, the FAQ, a code map, CONTRIBUTING). It reaches the install
through `force mcp` (force/mcp.py), registered for this session only.

Nothing goes to GitHub from here: issues, comments and pull requests are pre-filled links the user opens and
submits (`force support link | comment | pr`). `force support search` reads GitHub's public search API without
a login.

  force support                    the card, then Chuck's session (the menu key ?, the sidebar button)
  force support search WORDS       issues that match (open and closed)
  force support link bug|feature --title T [--field id=value …]   the pre-filled new-issue link, copied
  force support comment N [--file F]                               the comment text copied, the issue's link
  force support fix N --user GITHUB_NAME                           fix mode: the user's fork in <support>/src
  force support pr --title T [--body-file F]                       the pre-filled pull request link, copied
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, registry

REPO = "zalandemeter/force"
# testing only: another public repository's issues (owner/name), e.g. to try the duplicate path on real issues
_override = os.environ.get("FORCE_SUPPORT_REPO", "")
if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", _override):
    REPO = _override
GITHUB = "https://github.com"
API = "https://api.github.com"
NAME = "chuck"
ROLE = "support"
URL_LIMIT = 8000                # GitHub answers 414 above roughly 8 kB of URL
TEMPLATES = {"bug": "bug_report.yml", "feature": "feature_request.yml"}
FIELDS = {
    "bug": ("what", "expected", "steps", "version", "where", "environment", "doctor", "logs"),
    "feature": ("problem", "idea", "alternatives"),
}
# when the link is too long, these fields are shortened first, in this order
SHRINK_ORDER = ("logs", "doctor", "environment", "steps", "what", "alternatives", "idea", "problem", "expected")
PACK_FILES = ("docs.md", "commands.md", "faq.md", "architecture.md", "contributing.md")


class SupportError(Exception):
    pass


# --------------------------------------------------------------------------- where things are ----------
def support_dir(paths) -> Path:
    return paths.home / "support"


def pack_dir(paths) -> Path:
    """The context pack of the running release: <release>/share/support."""
    return Path(paths.tmux_conf).parent / "support"


def app_dir(paths) -> Path:
    return Path(os.path.abspath(Path(paths.tmux_conf).parent.parent))


def version() -> str:
    from .updates import current_version
    return current_version() or "development build"


# --------------------------------------------------------------------------- the folder ------------------
def prepare_dir(paths) -> Path:
    """Write Chuck's folder from the running release's pack (every open: an update brings the new pack).
    CHUCK.md becomes CLAUDE.md and AGENTS.md; opencode.json registers `force mcp` for OpenCode."""
    d = support_dir(paths)
    d.mkdir(parents=True, exist_ok=True)
    src = pack_dir(paths)
    brief = (src / "CHUCK.md").read_text()
    brief = brief.replace("{{version}}", version()).replace("{{app}}", str(app_dir(paths)))
    for name in ("CLAUDE.md", "AGENTS.md"):
        _write(d / name, brief)
    for name in PACK_FILES:
        if (src / name).exists():
            _write(d / name, (src / name).read_text())
    schema = Path(paths.tmux_conf).parent / "settings.schema.json"
    if schema.exists():
        _write(d / "settings.schema.json", schema.read_text())
    _write(d / "opencode.json", json.dumps(opencode_config(), indent=2) + "\n")
    return d


def _write(path: Path, text: str) -> None:
    try:
        if path.read_text() == text:
            return
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def mcp_command() -> list[str]:
    """How an agent starts Force's MCP server: this install's launcher (FORCE_BIN for an instance, SPEC §2.1)."""
    return [config.launcher_path(), "mcp"]


def opencode_config() -> dict:
    """Chuck's OpenCode: Force's MCP server, and approval for every change (OpenCode matches permission keys as
    patterns on tool names, `force_*`; the read tools after it are allowed)."""
    from .mcp import READ_TOOLS
    cmd = mcp_command()
    permission = {"edit": "ask", "bash": "ask", "webfetch": "ask", "force_*": "ask"}
    permission.update({f"force_{t}": "allow" for t in READ_TOOLS})
    return {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"force": {"type": "local", "command": cmd, "enabled": True}},
        "permission": permission,
    }


def claude_launch(paths) -> tuple[dict, list[str]]:
    """Extra settings and arguments for Chuck's Claude Code (SPEC §50): only his own instructions (the user's
    CLAUDE.md files and rules are skipped, no auto memory), only Force's MCP server, its read tools allowed."""
    from .mcp import READ_TOOLS
    d = support_dir(paths)
    excludes = [str(paths.claude_dir / "CLAUDE.md"), str(paths.claude_dir / "rules") + "/**"]
    for a in d.parents:
        for f in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
            excludes.append(str(a / f))
        excludes.append(str(a / ".claude" / "rules") + "/**")
    settings = {
        "claudeMdExcludes": excludes,
        "autoMemoryEnabled": False,
        # the read tools need no approval; the change tools ask every time (force/mcp.py)
        "permissions": {"allow": [f"mcp__force__{t}" for t in READ_TOOLS]},
    }
    mcp = {"mcpServers": {"force": {"type": "stdio", "command": mcp_command()[0], "args": mcp_command()[1:]}}}
    # the normal permission mode whatever the user's default (a bypass would let change tools run unasked)
    return settings, ["--mcp-config", json.dumps(mcp, separators=(",", ":")), "--strict-mcp-config",
                      "--permission-mode", "default"]


# --------------------------------------------------------------------------- the session -----------------
def find_entry(reg) -> registry.Entry | None:
    live = [e for e in reg.entries if registry.is_support(e) and not e.archived]
    if live:
        return live[0]
    arch = [e for e in reg.entries if registry.is_support(e)]
    return arch[0] if arch else None


def agent_for(cockpit) -> str | None:
    """The kind Chuck runs on: the setting, else the first installed of Claude Code and OpenCode."""
    from .cli import available_kinds
    have = [k for k in available_kinds(cockpit) if k in ("claude", "opencode")]
    want = cockpit.cfg.support_agent
    if want in ("claude", "opencode"):
        if want not in have:
            raise SupportError(f"supportAgent is {want}, but {want} is not installed here "
                               "(force settings set supportAgent auto)")
        return want
    return have[0] if have else None


def ensure_session(cockpit) -> str:
    """Chuck's session, created or brought back from the archive; its name."""
    from . import cli
    prepare_dir(cockpit.paths)
    reg = registry.load(cockpit.paths.registry)
    e = find_entry(reg)
    if e is not None and not e.archived:
        return e.name
    if e is not None:
        if cli.unarchive_command(cockpit, e.name) != 0:
            raise SupportError(f"could not bring {e.name} back from the archive")
        return e.name
    kind = agent_for(cockpit)
    if kind is None:
        raise SupportError("Chuck needs Claude Code or OpenCode. Install Claude Code with: "
                           "curl -fsSL https://claude.ai/install.sh | bash")
    from .kinds import free_name
    name = NAME if not reg.name_taken(NAME) else free_name(NAME, reg.name_taken)
    if name is None:
        raise SupportError("no free name for Chuck's session")
    if cli.create_session(cockpit, name, str(support_dir(cockpit.paths)), no_switch=True, kind=kind,
                          extra={"role": ROLE}) != 0:
        raise SupportError("could not start Chuck's session")
    return name


# --------------------------------------------------------------------------- the card --------------------
CARD = """\
  {face}  Chuck, Force's assistant

  Ask me how something in Force works. I can also:
    · set things up for you, like a new remote host or a notification
    · check whether a problem is already reported, and write a bug
      report or an idea you can submit
    · help fix it: build the fix and prepare a pull request

  I run on your own {agent} and plan, so each answer uses your
  tokens. Nothing goes to GitHub without your click.

  Enter  talk to Chuck        Esc  close"""


def card_text(agent: str | None, rich: bool) -> str:
    label = {"claude": "Claude Code", "opencode": "OpenCode"}.get(agent or "", "agent")
    return CARD.format(face="" if rich else ":)", agent=label)


def open_command(cockpit, ui: bool = False) -> int:
    """`force support`: in a popup (ui=True, the menu key and the sidebar button) the card first, then Chuck's
    session; from a shell: the card printed, then the session (switched to when inside Force)."""
    try:
        agent = agent_for(cockpit)
    except SupportError as e:
        agent = None
        problem = str(e)
    else:
        problem = None if agent else ("Chuck needs Claude Code or OpenCode. Install Claude Code with: "
                                      "curl -fsSL https://claude.ai/install.sh | bash")
    rich = os.environ.get("FORCE_GLYPHS") == "rich"
    print("\n" + card_text(agent, rich) if ui else card_text(agent, rich).rsplit("\n\n", 1)[0])
    if problem:
        print("\n  " + problem)
        if ui:
            _wait_key()
        return 1
    if ui and _wait_key() not in ("\r", "\n"):
        return 0
    try:
        name = ensure_session(cockpit)
    except SupportError as e:
        print(f"force: {e}", file=sys.stderr)
        if ui:
            _wait_key()
        return 1
    if ui:
        return _switch(cockpit, name)
    from .cli import _inside_cockpit
    if _inside_cockpit(cockpit):
        return _switch(cockpit, name)
    cmd = Path(config.launcher_path()).name
    print(f"\n  Chuck's session is `{name}`: open it with `{cmd} attach {name}`, or press ? inside Force.")
    return 0


def _wait_key() -> str:
    """One key from the terminal (the popup's), raw; '' when there is no terminal."""
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except (OSError, ValueError, ImportError, AttributeError):
        return ""
    try:
        tty.setraw(fd)
        return os.read(fd, 1).decode(errors="replace")
    except OSError:
        return ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _switch(cockpit, name: str) -> int:
    """Show `name` in this terminal: split mode switches the host's nested client (FORCE_CLIENT, as the dialogs
    do), classic mode the client the popup belongs to. A ui window showing a remote host (FORCE_HOST) comes back
    to this machine, on Chuck's session: he runs here."""
    window = os.environ.get("FORCE_WINDOW")
    if os.environ.get("FORCE_HOST") and window:
        from .cli import ui_host_command
        return ui_host_command(cockpit, "local", window, target_name=name)
    argv = ["tmux", "-L", cockpit.paths.tmux_socket, "switch-client"]
    client = os.environ.get("FORCE_CLIENT")
    if client:
        argv += ["-c", client]
    argv += ["-t", f"={name}"]
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"force: cannot switch to {name}: {r.stderr.strip()}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- GitHub (no login) -----------
def _ua() -> str:
    from .updates import current_version
    return f"force/{current_version() or 'dev'}"


def search(words: str, limit: int = 10, opener=urllib.request.urlopen) -> list[dict]:
    """Open and closed issues of the repo matching `words` (GitHub's public search API, no login: 10
    searches a minute)."""
    q = f"{words} repo:{REPO} is:issue"
    url = f"{API}/search/issues?" + urllib.parse.urlencode({"q": q, "per_page": str(limit)})
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": _ua()})
    try:
        with opener(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise SupportError("GitHub's search limit for this network is reached (10 a minute): try again in a "
                               "minute") from None
        raise SupportError(f"GitHub search failed: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SupportError(f"GitHub search failed: {getattr(e, 'reason', e)}") from None
    out = []
    for it in data.get("items") or []:
        out.append({"number": it.get("number"), "title": it.get("title") or "", "state": it.get("state") or "",
                    "url": it.get("html_url") or "", "comments": it.get("comments") or 0,
                    "labels": [lb.get("name") for lb in it.get("labels") or [] if isinstance(lb, dict)]})
    return out


def format_results(items: list[dict]) -> str:
    if not items:
        return "no matching issues"
    lines = []
    for it in items:
        labels = f" [{', '.join(it['labels'])}]" if it["labels"] else ""
        lines.append(f"#{it['number']} {it['state']}{labels}: {it['title']}\n    {it['url']}")
    return "\n".join(lines)


def issue_link(kind: str, title: str, fields: dict[str, str]) -> tuple[str, list[str]]:
    """The pre-filled new-issue link (GitHub issue forms fill a field from the query parameter named after its
    id) and the fields that had to be shortened to stay under GitHub's URL limit."""
    if kind not in TEMPLATES:
        raise SupportError("kind must be bug or feature")
    unknown = [k for k in fields if k not in FIELDS[kind]]
    if unknown:
        raise SupportError(f"unknown field(s) for a {kind}: {', '.join(unknown)} "
                           f"(the form has: {', '.join(FIELDS[kind])})")
    if not title.strip():
        raise SupportError("the issue needs a title")
    fields = {k: v for k, v in fields.items() if v is not None and str(v).strip()}
    shortened: list[str] = []

    def build() -> str:
        params = {"template": TEMPLATES[kind], "title": title.strip(), **fields}
        return f"{GITHUB}/{REPO}/issues/new?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    url = build()
    for key in SHRINK_ORDER:
        if len(url) <= URL_LIMIT:
            break
        if key not in fields:
            continue
        text = fields[key]
        note = "\n… (shortened: paste the rest after opening)"
        # the longest beginning of the text that still fits (characters cost 1 to 9 URL characters each)
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            fields[key] = text[:mid].rstrip() + note
            if len(build()) <= URL_LIMIT:
                lo = mid
            else:
                hi = mid - 1
        fields[key] = (text[:lo].rstrip() + note) if lo else "(too long for the link: paste it after opening)"
        url = build()
        shortened.append(key)
    if len(url) > URL_LIMIT:
        raise SupportError("the title is too long for a link")
    return url, shortened


def comment_link(number: int) -> str:
    return f"{GITHUB}/{REPO}/issues/{int(number)}#issuecomment-new"


def pr_link(user: str, branch: str, title: str, body: str) -> str:
    """The pre-filled pull request from the user's fork (`compare/main...USER:force:BRANCH?quick_pull=1`)."""
    q = urllib.parse.urlencode({"quick_pull": "1", "title": title, "body": body}, quote_via=urllib.parse.quote)
    return f"{GITHUB}/{REPO}/compare/main...{urllib.parse.quote(user)}:force:{urllib.parse.quote(branch)}?{q}"


CONVENTIONAL = re.compile(r"^(feat|fix|docs|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9._/-]+\))?!?: \S.{0,98}$")


# --------------------------------------------------------------------------- the clipboard ---------------
def copy(text: str, paths) -> bool:
    """Put `text` on the user's clipboard: tmux's buffer with -w (OSC 52 to the terminal the user looks at, also
    over ssh), and this machine's clipboard tool (share/clip-set.sh). True when one of them took it."""
    ok = False
    for sock in (paths.ui_socket, paths.tmux_socket):
        try:
            r = subprocess.run(["tmux", "-L", sock, "load-buffer", "-w", "-"], input=text.encode(),
                               capture_output=True, timeout=5)
            ok = ok or r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    clip = Path(paths.tmux_conf).parent / "clip-set.sh"
    if clip.exists():
        try:
            subprocess.run(["sh", str(clip)], input=text.encode(), capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ok


# --------------------------------------------------------------------------- fix mode ---------------------
def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "fix"


def issue_title(number: int, opener=urllib.request.urlopen) -> str | None:
    req = urllib.request.Request(f"{API}/repos/{REPO}/issues/{int(number)}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": _ua()})
    try:
        with opener(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("title")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def fix_setup(paths, number: int, user: str, title: str | None = None, run=subprocess.run,
              upstream: str | None = None, fork: str | None = None) -> tuple[Path, str]:
    """Clone the user's fork into <support>/src (or reuse it), add the upstream remote, and create the branch
    fix/N-slug from upstream main. Returns (the clone, the branch)."""
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", user or ""):
        raise SupportError("give your GitHub user name: force support fix N --user NAME")
    src = support_dir(paths) / "src"
    fork = fork or f"{GITHUB}/{user}/force.git"
    upstream = upstream or f"{GITHUB}/{REPO}.git"

    def git(*args, cwd=src):
        r = run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
        if r.returncode != 0:
            raise SupportError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip().splitlines()[-1:] or ''}")
        return r.stdout

    if not (src / ".git").exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        r = run(["git", "clone", "--quiet", fork, str(src)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SupportError(f"cannot clone your fork {fork}: fork the repository first "
                               f"({GITHUB}/{REPO}/fork)")
    remotes = git("remote").split()
    if "upstream" not in remotes:
        git("remote", "add", "upstream", upstream)
    git("fetch", "--quiet", "upstream", "main")
    branch = f"fix/{int(number)}-{slug(title or issue_title(number) or 'fix')}"
    existing = git("branch", "--list", branch).strip()
    if existing:
        git("checkout", "--quiet", branch)
    else:
        git("checkout", "--quiet", "-b", branch, "upstream/main")
    return src, branch


def pr_body(number: int | None, extra: str = "") -> str:
    parts = []
    if extra.strip():
        parts.append(extra.strip())
    if number:
        parts.append(f"Fixes #{int(number)}")
    parts.append("Made with Chuck, Force's assistant.")
    return "\n\n".join(parts)


def current_branch(src: Path, run=subprocess.run) -> tuple[str, str | None]:
    """The clone's branch and the GitHub user its origin belongs to."""
    b = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(src), capture_output=True, text=True)
    o = run(["git", "remote", "get-url", "origin"], cwd=str(src), capture_output=True, text=True)
    m = re.search(r"github\.com[:/]([A-Za-z0-9-]+)/", o.stdout or "")
    return b.stdout.strip(), (m.group(1) if m else None)


# --------------------------------------------------------------------------- the command -----------------
def command(cockpit, args) -> int:
    action = getattr(args, "action", None)
    try:
        if action in (None, "open"):
            return open_command(cockpit, ui=getattr(args, "ui", False))
        if action == "search":
            words = " ".join(args.words).strip()
            if not words:
                raise SupportError("give some words to search for")
            items = search(words)
            print(json.dumps(items, indent=2) if args.json else format_results(items))
            return 0
        if action == "link":
            fields = {}
            for f in args.field or []:
                if "=" not in f:
                    raise SupportError(f"--field takes id=value, got {f!r}")
                k, v = f.split("=", 1)
                fields[k.strip()] = v
            for f in args.field_file or []:
                if "=" not in f:
                    raise SupportError(f"--field-file takes id=FILE, got {f!r}")
                k, p = f.split("=", 1)
                fields[k.strip()] = Path(p).expanduser().read_text()
            url, short = issue_link(args.kind, args.title, fields)
            print(url)
            if short:
                print(f"(shortened to fit a link: {', '.join(short)}; paste the full text after opening)",
                      file=sys.stderr)
            if copy(url, cockpit.paths):
                print("(copied)", file=sys.stderr)
            return 0
        if action == "comment":
            text = Path(args.file).expanduser().read_text() if args.file else sys.stdin.read()
            if not text.strip():
                raise SupportError("the comment is empty")
            copied = copy(text, cockpit.paths)
            print(comment_link(args.number))
            print("(the comment is copied: open the link, paste it and press Comment)" if copied
                  else "(paste the comment there and press Comment)", file=sys.stderr)
            return 0
        if action == "fix":
            src, branch = fix_setup(cockpit.paths, args.number, args.user, args.title)
            print(f"{src}\n{branch}")
            return 0
        if action == "pr":
            src = support_dir(cockpit.paths) / "src"
            if not (src / ".git").exists():
                raise SupportError("no fix workspace yet: force support fix N --user NAME")
            if not CONVENTIONAL.match(args.title or ""):
                raise SupportError("the title must be a Conventional Commit, like 'fix(sidebar): stop the flicker'")
            branch, user = current_branch(src)
            user = args.user or user
            if not user:
                raise SupportError("cannot tell your GitHub user from the clone: add --user NAME")
            m = re.match(r"fix/(\d+)-", branch)
            body = pr_body(int(m.group(1)) if m else None,
                           Path(args.body_file).expanduser().read_text() if args.body_file else "")
            url = pr_link(user, branch, args.title, body)
            print(url)
            if copy(url, cockpit.paths):
                print("(copied)", file=sys.stderr)
            return 0
    except SupportError as e:
        print(f"force: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"force: {e}", file=sys.stderr)
        return 1
    print(f"force: unknown support action {action!r}", file=sys.stderr)
    return 2


def add_parser(sub) -> None:
    p = sub.add_parser("support", help="Chuck, Force's assistant: the card and his session; search | link | "
                                       "comment | fix | pr (SPEC §50)")
    ss = p.add_subparsers(dest="action")
    p.add_argument("--ui", action="store_true", help=argparse_suppress())
    o = ss.add_parser("open", help="the card, then Chuck's session (the default)")
    o.add_argument("--ui", action="store_true", help=argparse_suppress())
    s = ss.add_parser("search", help="find issues on GitHub (open and closed) that match WORDS")
    s.add_argument("words", nargs="+")
    s.add_argument("--json", action="store_true")
    lk = ss.add_parser("link", help="the pre-filled new-issue link for a bug or a feature request (copied)")
    lk.add_argument("kind", choices=sorted(TEMPLATES))
    lk.add_argument("--title", required=True)
    lk.add_argument("--field", action="append", metavar="ID=VALUE", help="a form field (repeatable)")
    lk.add_argument("--field-file", action="append", metavar="ID=FILE", help="a form field from a file")
    c = ss.add_parser("comment", help="copy a comment for issue N and print its link (text from --file or stdin)")
    c.add_argument("number", type=int)
    c.add_argument("--file")
    f = ss.add_parser("fix", help="fix mode: clone your fork and branch fix/N-slug from upstream main")
    f.add_argument("number", type=int)
    f.add_argument("--user", required=True, help="your GitHub user name (your fork: github.com/USER/force)")
    f.add_argument("--title", help="the issue title for the branch name (default: read from GitHub)")
    pr = ss.add_parser("pr", help="the pre-filled pull request link for the fix branch (copied)")
    pr.add_argument("--title", required=True, help="a Conventional Commit, like 'fix(sidebar): …'")
    pr.add_argument("--body-file", help="more text for the pull request description")
    pr.add_argument("--user", help="your GitHub user name (default: from the clone's origin)")


def argparse_suppress():
    import argparse
    return argparse.SUPPRESS
