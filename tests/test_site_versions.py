"""The docs version menu (website/tools/build.py): release lines from the tags, the menu, the older pages."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "website" / "tools"))
import build  # noqa: E402

PAGE = """<html><head><title>Force documentation</title>
<link rel="alternate" type="text/markdown" href="docs.md"></head><body>
<main class="doc">
    <div class="doc-head">
      <h1>Force documentation</h1>
      <p class="lead">Lead.</p>
    </div>
</main></body></html>"""

VERSIONS = [("1.5", ["v1.5.0", "v1.5.1"]), ("1.4", ["v1.4.0", "v1.4.1", "v1.4.2"]), ("1.3", ["v1.3.0"])]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_release_lines_from_tags(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "x")
    for t in ("v1.0.0", "v1.0.10", "v1.0.2", "v1.1.0", "v2.0.0", "v1.1.0-rc1", "nightly"):
        _git(tmp_path, "tag", t)
    assert build.doc_versions(tmp_path) == [("2.0", ["v2.0.0"]), ("1.1", ["v1.1.0"]),
                                            ("1.0", ["v1.0.0", "v1.0.2", "v1.0.10"])]


def test_no_git_no_versions(tmp_path):
    assert build.doc_versions(tmp_path / "nothing") == []


def test_menu_on_the_newest_page():
    page = build.with_versions(PAGE, VERSIONS, "1.5")
    assert '<details class="ver">' in page and "Version</span> 1.5" in page
    assert '<a href="docs.html" aria-current="page"><span class="ver-n">1.5</span><span class="ver-r">latest' in page
    assert '<a href="docs-1.4.html"><span class="ver-n">1.4</span><span class="ver-r">1.4.0 – 1.4.2' in page
    assert '<span class="ver-r">1.3.0</span>' in page, "one release: no range"
    assert "ver-note" not in page and "noindex" not in page and "canonical" not in page


def test_an_older_page_points_at_the_newest():
    page = build.with_versions(PAGE, VERSIONS, "1.4")
    assert '<a href="docs-1.4.html" aria-current="page">' in page
    assert "These are the docs for Force 1.4 (up to 1.4.2)." in page
    assert page.index("ver-note") < page.index("</div>"), "the note is inside the head"
    assert '<meta name="robots" content="noindex">' in page
    assert f'<link rel="canonical" href="{build.BASE}/docs.html">' in page
    assert "<title>Force documentation · 1.4</title>" in page
    assert 'href="docs-1.4.md"' in page


def test_a_page_without_a_head_is_refused():
    with pytest.raises(SystemExit):
        build.with_versions("<html></html>", VERSIONS, "1.5")
