# Website

The site at https://useforce.cc: the landing page, the documentation page and the privacy page. Plain HTML, CSS and
a little JavaScript; no framework, no third-party requests (fonts are self-hosted). GitHub Actions builds it with
`tools/build.py` and deploys it to GitHub Pages (`.github/workflows/pages.yml`).

```sh
python3 website/tools/build.py /tmp/site      # build into /tmp/site
python3 -m http.server -d /tmp/site 8080      # and look at it
```

| Path | What it is |
|---|---|
| `index.html`, `style.css`, `site.js` | the landing page. `site.js` plays the film over the cockpit window while it is in view, then leaves the live window to click around in (clicks are read as terminal cells), switches to the phone view below 640 px, and copies the install line |
| `docs.html`, `docs.css`, `docs.js` | the documentation page; `tools/md.py` turns it into `docs.md` for agents |
| `privacy.html` | the privacy page (also published as `privacy.md`) |
| `index.md` | the landing page as Markdown for agents (written by hand) |
| `agent-tools.js` | read-only WebMCP tools: the install command, a docs section, the agent skill |
| `cockpit-*.html`, `menu-*.html`, `phone-*.html` | real Force screens as coloured text, inlined into the page by `build.py` (`tools/screen.py` draws block and box characters as boxes and lines, so they stay sharp at any zoom) |
| `media/` | the film in two cuts (desktop 1920×1080, phone 1080×1378), WebM and MP4, with posters |
| `fonts/` | Geist and Unbounded (OFL), a DejaVu Sans Mono subset (Bitstream Vera licence) and the Nerd Font symbols, licences beside them |
| `repo/` | files copied to the site root as they are: `CNAME`, `robots.txt`, `auth.md` |
| `worker/markdown.js` | the Cloudflare Worker on useforce.cc that answers `Accept: text/markdown` with the Markdown versions |
| `tools/build.py OUT` | builds the site into OUT: inlines the screens, versions the assets, writes the Markdown versions, `llms.txt`, `sitemap.xml`, `security.txt`, the agent skill index, and `LICENSE.txt` |
| docs versions | `build.py` reads the release tags (`vX.Y.Z`): `docs.html` is the newest release line, and each older line gets `docs-X.Y.html` (and `.md`), its `website/docs.html` as that line's newest release had it, with the current stylesheet and script. A version menu at the top of each docs page lists them; the older pages carry a note pointing at the newest docs, `noindex` and a canonical link to `docs.html`, and stay out of the sitemap. Without git or tags (a source tarball) there is no menu. CI checks out the full history for this (`fetch-depth: 0`) |

## Remaking the screens

The screens are captures of real Force sessions, not mock-ups. `tools/demo_snapshot.py` feeds the real sidebar and
header invented sessions; each agent pane is a real Claude Code session in a small demo project, captured with
`tmux capture-pane -e` and converted with `tools/ansi2html.py`. The capture machine's name becomes `devbox`
(`FORCE_CAPTURE_HOST`, default this machine's name), and paths under `FORCE_DEMO_DIR` (default `~/demo-projects`)
become `~/`.

The phone screens are the same at 46×28: the sidebar alone (`phone-list.html`) and each session as header plus agent.

## The film

`tools/brag/index.html` (desktop) and `tools/brag/phone.html` (phone) are the film's compositions; `render(t)` draws
frame t, and the frames are captured headless at 30 fps and encoded with ffmpeg. Each cut ends on exactly what the
page shows next (the desktop window at its 1200 px layout, the phone's session list), so the live view takes over
in register. Change one side and the other must follow: the stage's shape in `style.css` and the composition's size.
