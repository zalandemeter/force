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
| `index.md` | the landing page as Markdown for agents (written by hand; `{{source}}` becomes the link to the source code) |
| `agent-tools.js` | read-only WebMCP tools: the install command, a docs section, the agent skill |
| `cockpit-*.html`, `menu-*.html`, `phone-*.html`, `card-chuck.html` | real Force screens as coloured text, inlined into the page by `build.py` (`tools/screen.py` draws block and box characters as boxes and lines, so they stay sharp at any zoom) |
| `media/` | the film in two cuts (desktop 1920×1080, phone 1080×1378), WebM and MP4, with posters |
| `fonts/` | Geist and Unbounded (OFL), a DejaVu Sans Mono subset (Bitstream Vera licence) and the Nerd Font symbols, licences beside them |
| `repo/` | files copied to the site root as they are: `CNAME`, `robots.txt`, `auth.md` |
| `worker/markdown.js` | the Cloudflare Worker on useforce.cc that answers `Accept: text/markdown` with the Markdown versions |
| `tools/build.py OUT` | builds the site into OUT: inlines the screens, versions the assets, adds structured data (JSON-LD: what Force is, its source code) to `index.html`, writes the Markdown versions, `llms.txt`, `sitemap.xml`, `security.txt`, the agent skill index, and `LICENSE.txt` |
| docs versions | `build.py` reads the release tags (`vX.Y.Z`): `docs.html` is the newest release line, and each older line gets `docs-X.Y.html` (and `.md`), its `website/docs.html` as that line's newest release had it, with the current stylesheet and script. A version menu at the top of each docs page lists them; the older pages carry a note pointing at the newest docs, `noindex` and a canonical link to `docs.html`, and stay out of the sitemap. Without git or tags (a source tarball) there is no menu. CI checks out the full history for this (`fetch-depth: 0`) |

## Remaking the screens

The screens are captures of real Force sessions, not mock-ups. Each agent pane is a real Claude Code session in a
small demo project, captured once with `tmux capture-pane -e` (paths under the demo folder shown as `~/`). The
sidebar, the header, the menu and Chuck's card come from this checkout, so they follow Force's look:

```sh
(cd crates/sidebar && cargo build --release)
python3 website/tools/respin.py crates/sidebar/target/release
```

`tools/respin.py` feeds the real sidebar and header the invented sessions of `tools/demo_snapshot.py` in a tmux
server of its own (`FORCE_CAPTURE_SOCKET`, never Force's), captures them at the page's sizes and splices them into
`cockpit-NAME.html` (150×40) and `phone-NAME.html` (46 wide), and writes `phone-list.html` (the sidebar alone,
46×27). The menu (`menu-NAME.html`) and Chuck's card (`card-chuck.html`, `phone-chuck.html`) are drawn by tmux for
a client, so they are captured through a second tmux server attached to the first. The capture machine's name
becomes `devbox` (`FORCE_CAPTURE_HOST`, default this machine's name).

`fonts/nerd-symbols.woff2` holds only the icons the screens and the film use, cut from Symbols Nerd Font Mono 3.5.1
with `pyftsubset`; a screen that shows a new icon needs it added there.

## The film

`tools/brag/index.html` (desktop, 23.3 s) and `tools/brag/phone.html` (phone, 19.3 s) are the film's compositions;
`render(t)` draws frame t. `tools/brag/film.py WORK` copies them with the screens they load (drawn by `screen.py`),
captures the frames headless at 30 fps (`capture.js`, playwright-core on `NODE_PATH`) and encodes `media/` with
ffmpeg: both cuts in MP4 and WebM, their posters (the first frame) and `brag.jpg`, the share image. The captions are
the page's (`site.js`, `CAPS`), on the wallpaper under the window, timed to the film. Each cut ends on exactly what the
page shows next (the desktop window at its 1200 px layout, the phone's session list), so the live view takes over
in register. Change one side and the other must follow: the stage's shape in `style.css` and the composition's size.
