#!/usr/bin/env python3
"""Render the film: both cuts' frames captured headless, encoded into website/media.

    film.py WORK [desktop|phone ...]      (default: both)

WORK is a scratch folder (frames take a few GB). The compositions (index.html, phone.html) are copied there with the
fonts and the screens they load, drawn by screen.py as the page draws them. capture.js needs playwright-core
(NODE_PATH) and its Chromium; ffmpeg encodes. Nothing here touches Force itself.
"""
import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))
from screen import render   # noqa: E402

# cut: (composition, seconds, frame size, output name, poster name, screens it loads)
CUTS = {
    "desktop": ("index.html", 23.3, "brag", "poster", ["cockpit-api.html", "cockpit-billing-fix.html", "card-chuck.html"]),
    "phone": ("phone.html", 19.3, "brag-phone", "poster-phone", ["phone-list.html", "phone-billing-fix.html"]),
}
SHARE_AT = 7.4      # the share image (brag.jpg, og:image): the cockpit with billing-fix calling


def serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)   # any free port on loopback
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main(argv):
    work = Path(argv[0]).resolve()
    cuts = argv[1:] or list(CUTS)
    comp = work / "comp"
    comp.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SITE / "fonts", comp / "fonts", dirs_exist_ok=True)
    for name in ("index.html", "phone.html"):
        shutil.copy2(HERE / name, comp / name)
    for cut in cuts:
        for screen in CUTS[cut][4]:
            text = (SITE / screen).read_text(encoding="utf-8")
            (comp / screen).write_text(render(text, overlay=screen.startswith(("menu-", "card-"))), encoding="utf-8")
    srv, port = serve(comp)
    media = SITE / "media"
    try:
        for cut in cuts:
            page, secs, out, poster, _ = CUTS[cut]
            frames = work / f"frames-{cut}"
            shutil.rmtree(frames, ignore_errors=True)
            subprocess.run(["node", str(HERE / "capture.js"), f"http://127.0.0.1:{port}/{page}", str(frames),
                            "--fps", "30", str(secs)], check=True)
            seq = str(frames / "%05d.png")
            ff = ["ffmpeg", "-v", "error", "-y", "-framerate", "30", "-i", seq]
            subprocess.run(ff + ["-c:v", "libx264", "-preset", "slow", "-crf", "26", "-pix_fmt", "yuv420p",
                                 "-movflags", "+faststart", str(media / f"{out}.mp4")], check=True)
            subprocess.run(ff + ["-c:v", "libvpx-vp9", "-crf", "40", "-b:v", "0", "-row-mt", "1", "-pix_fmt", "yuv420p",
                                 str(media / f"{out}.webm")], check=True)
            # the poster is the first frame (what shows before the film plays); the share image a frame that says
            # what Force is
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(frames / "00000.png"), "-q:v", "6",
                            str(media / f"{poster}.jpg")], check=True)
            if cut == "desktop":
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(frames / f"{round(SHARE_AT * 30):05d}.png"),
                                "-q:v", "3", str(media / "brag.jpg")], check=True)
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main(sys.argv[1:])
