#!/usr/bin/env python3
"""Build the website (useforce.cc) into OUT: index.html with the cockpit screens inlined, the other pages, the
stylesheets, scripts, fonts and media, the Markdown versions for agents, and the files in website/root.

    build.py OUT
"""
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from screen import render   # noqa: E402  (a Force screen as the page draws it)
from md import html_to_md   # noqa: E402  (the docs page as Markdown for agents)

SITE = Path(__file__).resolve().parent.parent
FILES = ["style.css", "site.js", "docs.css", "docs.js", "agent-tools.js"]
PAGES = ["docs.html", "privacy.html"]   # pages copied as they are (index.html is built); both go into sitemap.xml
BASE = "https://useforce.cc"   # the site's address (website/root/CNAME)
REPO = "https://github.com/zalandemeter/force"   # the public repository; set to None to leave every data-repo link out
CONTACT = "hello@useforce.cc"   # the one public address (privacy page, footer, security.txt)
SKILL_DESC = ("Install and drive Force, the code cockpit: start Claude Code, OpenCode and shell sessions that keep "
              "running in tmux, hand them prompts and wait for the answer, read their screens, answer their dialogs, "
              "and report a script's status on its card.")
SKILL_FRONT = f"---\nname: force\ndescription: {json.dumps(SKILL_DESC)}\n---\n\n"   # quoted: the text has colons
SKILL_INSTALL = ("## Install Force (once per machine)\n\n"
                 "If `force` is not on PATH, install it as the user (Linux x86-64, no root):\n"
                 "`curl -fsSL https://useforce.cc/install.sh | sh`. Then `force skill` prints this guide for the\n"
                 "installed version.\n\n")
DIRS = ["fonts", "media"]   # (the cockpit-*.html screens are inlined, not copied)


def repo_links(html: str) -> str:
    """Links marked data-repo point at REPO, or are dropped while there is no public repository (never a dead link)."""
    if REPO:
        return html.replace("{{repo}}", REPO).replace(" data-repo", "")
    html, n = re.subn(r'\n?[ \t]*<a\b[^>]*\bdata-repo\b[^>]*>.*?</a>', "", html, flags=re.S)
    if "{{repo}}" in html:
        raise SystemExit("build: a {{repo}} link is not marked data-repo")
    return html


def doc_versions(repo: Path) -> list[tuple[str, list[str]]]:
    """The release lines of the docs, newest first: [("1.4", ["v1.4.0", "v1.4.1", "v1.4.2"]), ...] from the
    repository's vX.Y.Z tags. Empty without git or tags (a build from a source tarball): no version menu then."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "tag", "--list", "v*"], capture_output=True, text=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    lines: dict[tuple[int, int], list[tuple[int, str]]] = {}
    for tag in out.split():
        m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
        if m:
            lines.setdefault((int(m[1]), int(m[2])), []).append((int(m[3]), tag))
    return [(f"{x}.{y}", [t for _, t in sorted(lines[(x, y)])]) for x, y in sorted(lines, reverse=True)]


def version_menu(versions: list[tuple[str, list[str]]], shown: str) -> str:
    """The docs' version menu (a <details>: it works without JavaScript): the newest line is docs.html, each older
    one docs-X.Y.html, with the releases it covers."""
    def span(tags: list[str]) -> str:
        return tags[0][1:] if len(tags) == 1 else f"{tags[0][1:]} – {tags[-1][1:]}"
    items = []
    for i, (line, tags) in enumerate(versions):
        href = "docs.html" if i == 0 else f"docs-{line}.html"
        cur = ' aria-current="page"' if line == shown else ""
        note = "latest" if i == 0 else span(tags)
        items.append(f'<li><a href="{href}"{cur}><span class="ver-n">{line}</span>'
                     f'<span class="ver-r">{note}</span></a></li>')
    latest = " (latest)" if shown == versions[0][0] else ""
    return (f'<details class="ver"><summary aria-label="Documentation version: {shown}{latest}">'
            f'<span class="ver-k">Version</span> {shown}<svg viewBox="0 0 16 16" aria-hidden="true">'
            f'<path d="M4 6l4 4 4-4"/></svg></summary><ul>{"".join(items)}</ul></details>\n      ')


def with_versions(page: str, versions: list[tuple[str, list[str]]], shown: str) -> str:
    """`page` (docs.html of any release) with the version menu at the top of its head; an older line also gets a
    note pointing at the newest docs, and stays out of search results (canonical: docs.html)."""
    head = '<div class="doc-head">'
    if head not in page:
        raise SystemExit("build: a docs page without <div class=\"doc-head\">")
    page = page.replace(head, head + "\n      " + version_menu(versions, shown), 1)
    if shown != versions[0][0]:
        start = page.index(head)
        end = page.index("</div>", start)
        tags = dict(versions)[shown]
        note = (f'\n      <p class="ver-note">These are the docs for Force {shown} (up to {tags[-1][1:]}). '
                f'<a href="docs.html">Read the docs for {versions[0][0]}</a>, the newest release.</p>\n    ')
        page = page[:end] + note + page[end:]
        page = page.replace("</title>", f" · {shown}</title>", 1)
        page = page.replace("</head>", f'<meta name="robots" content="noindex">\n'
                                       f'<link rel="canonical" href="{BASE}/docs.html">\n</head>', 1)
        page = page.replace('href="docs.md"', f'href="docs-{shown}.md"')
    return page


def versioned(page: str) -> str:
    """Asset links with their content hash, so a browser never pairs a new page with an old cached file."""
    for f in FILES:
        v = hashlib.sha1((SITE / f).read_bytes()).hexdigest()[:10]
        page = page.replace(f'"{f}"', f'"{f}?v={v}"')
    return page


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    page = (SITE / "index.html").read_text(encoding="utf-8")
    # every <pre ... data-include="FILE"></pre> gets FILE (a Force screen or a fragment of one, as coloured text)
    page, n = re.subn(r'(<pre\b[^>]*?) data-include="([^"]+)"([^>]*)></pre>',
                      lambda m: f'{m.group(1)}{m.group(3)}>{render((SITE / m.group(2)).read_text(encoding="utf-8"), overlay=m.group(2).startswith("menu-"))}</pre>', page)
    if n == 0 or "data-include" in page:
        raise SystemExit("build: an include placeholder is missing or malformed in index.html")
    # versioned links, so a browser never pairs the new page with a stylesheet or script it cached earlier
    for f in FILES:
        v = hashlib.sha1((SITE / f).read_bytes()).hexdigest()[:10]
        page = page.replace(f'"{f}"', f'"{f}?v={v}"')
    # the film and its posters too: a new cut must not meet a cached old one (it has to match the window pixel for pixel)
    page = re.sub(r'"media/([^"?]+)"', lambda m: f'"media/{m.group(1)}?v={hashlib.sha1((SITE / "media" / m.group(1)).read_bytes()).hexdigest()[:10]}"', page)
    (out / "index.html").write_text(repo_links(page), encoding="utf-8")
    # the docs of every older release line, as its newest release had them; a line without a docs page is left out
    versions, older = doc_versions(SITE.parent), {}
    for line, tags in versions[1:]:
        try:
            older[line] = subprocess.run(["git", "-C", str(SITE.parent), "show", f"{tags[-1]}:website/docs.html"],
                                         capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            pass
    versions = versions[:1] + [v for v in versions[1:] if v[0] in older]
    for name in PAGES:
        other = (SITE / name).read_text(encoding="utf-8")
        if name == "docs.html" and versions:
            other = with_versions(other, versions, versions[0][0])
        (out / name).write_text(repo_links(versioned(other)), encoding="utf-8")
    for line, old in older.items():
        (out / f"docs-{line}.html").write_text(repo_links(versioned(with_versions(old, versions, line))),
                                               encoding="utf-8")
        (out / f"docs-{line}.md").write_text(html_to_md(repo_links(old), f"docs-{line}.html"), encoding="utf-8")
    for f in FILES:
        shutil.copy2(SITE / f, out / f)
    for d in DIRS:
        if (out / d).exists():
            shutil.rmtree(out / d)
        shutil.copytree(SITE / d, out / d)
    # Markdown for agents: index.md (written by hand), docs.md (made from docs.html), and llms.txt pointing at
    # both; a Cloudflare Worker (website/worker/markdown.js) serves them for `Accept: text/markdown`
    shutil.copy2(SITE / "index.md", out / "index.md")
    for name in ("docs", "privacy"):
        (out / f"{name}.md").write_text(html_to_md(repo_links((SITE / f"{name}.html").read_text(encoding="utf-8")), f"{name}.html"),
                                        encoding="utf-8")
    (out / "llms.txt").write_text(
        "# Force\n\n"
        "> Force is the code cockpit: it keeps your Claude Code, OpenCode and shell sessions running in tmux on your\n"
        "> machine or a server, through closed terminals and reboots, and shows them all in one terminal with who\n"
        "> needs you. Linux, one-line install: `curl -fsSL https://useforce.cc/install.sh | sh`.\n\n"
        "## Docs\n\n"
        f"- [Overview]({BASE}/index.md): what Force does, the install line, the main commands\n"
        f"- [Documentation]({BASE}/docs.md): install, the cockpit and its states, keys, sessions, other machines,"
        " notifications, scripts and agents (`force prompt`, `wait`, `read`, `keys`, `status`), settings, every command\n"
        f"- [Privacy]({BASE}/privacy.md): what the site and the app process (no cookies, analytics or telemetry)\n"
        "\n## Agent skill\n\n"
        f"- [force]({BASE}/.well-known/agent-skills/force/SKILL.md): install Force and drive its sessions from an agent\n",
        encoding="utf-8")
    # the agent guide (`force skill`, share/skill.md) as an Agent Skill, with a discovery index (Agent Skills
    # Discovery v0.2.0): an agent that finds the site can install Force and drive it
    skill_dir = out / ".well-known" / "agent-skills"
    (skill_dir / "force").mkdir(parents=True, exist_ok=True)
    guide = (SITE.parent / "share" / "skill.md").read_text(encoding="utf-8")
    cut = guide.index("\n## ") + 1                      # the install note goes after the guide's title and intro
    skill = (SKILL_FRONT + guide[:cut] + SKILL_INSTALL + guide[cut:]).encode("utf-8")
    (skill_dir / "force" / "SKILL.md").write_bytes(skill)
    (skill_dir / "index.json").write_text(json.dumps({
        "$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
        "skills": [{"name": "force", "type": "skill-md", "description": SKILL_DESC,
                    "url": "/.well-known/agent-skills/force/SKILL.md",
                    "digest": "sha256:" + hashlib.sha256(skill).hexdigest()}]}, indent=2) + "\n", encoding="utf-8")
    # security.txt (RFC 9116): where to report a vulnerability; Expires is renewed by every build (a year ahead)
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT00:00:00Z")
    (out / ".well-known" / "security.txt").write_text(
        f"Contact: mailto:{CONTACT}\nExpires: {expires}\nPreferred-Languages: en\n"
        f"Canonical: {BASE}/.well-known/security.txt\n", encoding="utf-8")
    # the sitemap lists the built pages, so a page added to PAGES is in it without another step
    urls = [f"{BASE}/"] + [f"{BASE}/{name}" for name in PAGES]
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                     + "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls) + "</urlset>\n",
                                     encoding="utf-8")
    for f in (SITE / "root").iterdir():
        shutil.copy2(f, out / f.name)
    shutil.copy2(SITE.parent / "LICENSE", out / "LICENSE.txt")   # the licence and its warranty disclaimer, linked from every footer
    (out / ".nojekyll").write_text("")
    print(f"build: {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
