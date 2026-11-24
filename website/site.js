// Force landing page: the cockpit (click anything in it), the film (plays muted while it is
// on screen; with reduced motion only when asked), and the copy buttons.
(function () {
  // the top bar: clear on the coral at the top of the page, a floating dark bar once the page scrolls
  var root = document.documentElement;
  // .floating: the bar jumps out as a black tab as soon as the page scrolls (style.css animates it)
  var bar = function () { root.classList.toggle("floating", window.scrollY > 4); };
  window.addEventListener("scroll", bar, { passive: true });
  bar();

  var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // --- the cockpit: seven real screens; the screen is a grid of cells, so a click is read as (row, column) and
  // answered the way Force answers it: a session opens, a count opens the session it counts, the rest explains itself
  var screen = document.querySelector(".screen");
  var wrap = document.querySelector(".term-wrap");
  if (screen && wrap) {
    var ORDER = ["shell", "api", "billing-fix", "docs-site", "infra", "mobile", "migration-2024"];
    var SAY = {
      "shell": "A plain shell sits in the list too, with the agents.",
      "api": "api is idle: the agent finished and you have read what it said.",
      "billing-fix": "billing-fix is waiting for your permission. You answer it right here.",
      "docs-site": "docs-site finished while you were elsewhere, so it is your turn.",
      "infra": "infra has been idle for 42 minutes.",
      "mobile": "mobile is running its test suite. It keeps going with the window closed.",
      "migration-2024": "Nobody has touched migration-2024 for 30 hours, so it is marked stale.",
      "new": "+ new starts another session. From a shell, force new NAME does the same.",
      "archived": "Archived sessions wait here with their conversations, out of the list.",
      "host": "devbox is this machine. force host add NAME puts another one here, one click away.",
      "limits": "Your five-hour and weekly limits: how much is used, and when each resets.",
      "menu": "Force's menu. Ctrl+Space opens it anywhere in Force; every item has its key on the right.",
      "quit": "Quit closes the window. The sessions keep running; type force to come back.",
      "sidebar": "Hides the sidebar when you want the whole width for the agent."
    };
    var PILL = { "rgb(240, 100, 94)": "billing-fix", "rgb(167, 212, 173)": "docs-site", "rgb(215, 175, 95)": "mobile" };
    var ICON = { "": "sidebar", "": "menu", "": "quit" };
    var hint = document.querySelector(".hint");
    var ITEM = {
      "Restart": "Restart starts the agent again, and it picks up its conversation.",
      "Archive": "Archive takes the session out of the list; its conversation stays.",
      "Sidebar on / off": "Hides the sidebar when you want the whole width for the agent.",
      "Quit (sessions keep running)": "Quit closes the window. The sessions keep running; type force to come back.",
      "Help": "Help lists every key; each menu item shows its own on the right.",
      "Upgrade now": "Upgrade now installs the newest release. The sessions keep running.",
      "Restart all sessions": "Restart all sessions starts every agent again, each with its conversation."
    };
    var menuOpen = false;
    var previous = "api";
    var call = screen.querySelector(".call");
    var current = "api";

    var shown = function () { return screen.querySelector('.term[data-view="' + current + '"]'); };
    var metrics = function () {
      var r = shown().getBoundingClientRect();
      return { x: r.left, y: r.top, cw: r.width / 150, lh: r.height / 40 };
    };
    var cellChar = function (row, col) {
      var line = shown().textContent.split("\n")[row] || "";
      return Array.from(line)[col] || " ";
    };
    var colorAt = function (m, row, col) {
      var el = document.elementFromPoint(m.x + (col + .5) * m.cw, m.y + (row + .5) * m.lh);
      return el && shown().contains(el) ? getComputedStyle(el).color : "";
    };
    // what the cell at (row, col) does, and the cells it covers: {go|say, r, c, w, h}
    var target = function (m, row, col) {
      if (col >= 1 && col < 29 && row >= 3 && row <= 16) {
        var k = (row - 3) >> 1;
        return { go: ORDER[k], r: 3 + 2 * k, c: 1, w: 28, h: 2 };
      }
      if (row === 18 && col >= 1 && col < 29) return col < 15 ? { say: "new", r: 18, c: 1, w: 13, h: 1 } : { say: "archived", r: 18, c: 15, w: 14, h: 1 };
      if (row === 35 && col >= 1 && col < 29) return { say: "host", r: 35, c: 1, w: 28, h: 1 };
      if ((row === 37 || row === 38) && col >= 1 && col < 29) return { say: "limits", r: 37, c: 1, w: 28, h: 2 };
      if (row === 1 && col >= 30) {
        var who = PILL[colorAt(m, row, col)];
        if (who) {   // the pill: the run of cells in its colour around the click
          var c0 = col, c1 = col, col0 = colorAt(m, row, col);
          while (c0 > 30 && colorAt(m, row, c0 - 1) === col0) c0--;
          while (c1 < 149 && colorAt(m, row, c1 + 1) === col0) c1++;
          return { go: who, r: 1, c: c0, w: c1 - c0 + 1, h: 1 };
        }
        for (var d = -1; d <= 1; d++) {
          var what = ICON[cellChar(row, col + d)];
          if (what) return { say: what, r: 1, c: col + d - 1, w: 3, h: 1 };
        }
      }
      return null;
    };
    var menu = function (open) {
      menuOpen = open;
      screen.querySelectorAll(".term.menu").forEach(function (t) { t.hidden = !open || t.dataset.menu !== current; });
    };
    var show = function (name) {
      if (name !== current) previous = current;
      current = name;
      screen.dataset.screen = name;
      screen.querySelectorAll(".term[data-view]").forEach(function (t) { t.hidden = t.dataset.view !== name; });
      if (name === "billing-fix") call.classList.add("done");
    };
    var act = function (t) {
      if (!t) return;
      if (t.go) { show(t.go); hint.textContent = SAY[t.go]; }
      else if (t.say === "menu") { menu(true); hint.textContent = SAY.menu; }
      else hint.textContent = SAY[t.say];
    };
    // a menu item: "│ Label   (key) │" on that row of the open menu
    var item = function (row, col) {
      var open = screen.querySelector(".term.menu:not([hidden])");
      var line = Array.from(open.textContent.split("\n")[row] || "");
      var l = line.indexOf("│"), r = line.lastIndexOf("│");
      if (l < 0 || r <= l || col < l || col > r) return { outside: true };
      var m = /^ (.+?)\s+\((\S+)\) $/.exec(line.slice(l + 1, r).join(""));
      return m ? { label: m[1], key: m[2] } : {};
    };
    var choose = function (it) {
      menu(false);
      if (!it || !it.label) return;
      if (it.label === "Previous session") { var p = previous; show(p); hint.textContent = SAY[p]; return; }
      var name = it.label.replace("…", "");
      hint.textContent = ITEM[name] || (it.label.slice(-1) === "…" ? name + " opens a small dialog in Force." : name + ".");
    };
    var cellOf = function (e) {
      var m = metrics();
      return { m: m, col: Math.floor((e.clientX - m.x) / m.cw), row: Math.floor((e.clientY - m.y) / m.lh) };
    };
    var at = function (e) {
      var c = cellOf(e);
      return c.row >= 0 && c.row < 40 && c.col >= 0 && c.col < 150 ? target(c.m, c.row, c.col) : null;
    };
    screen.addEventListener("mousemove", function (e) {
      if (menuOpen) { var c = cellOf(e); screen.classList.toggle("pointing", !!item(c.row, c.col).label); return; }
      screen.classList.toggle("pointing", !!at(e));
    });
    screen.addEventListener("mouseleave", function () { screen.classList.remove("pointing"); });
    screen.addEventListener("click", function (e) {
      if (menuOpen) { var c = cellOf(e); choose(item(c.row, c.col)); return; }
      act(at(e));
    });
    wrap.addEventListener("keydown", function (e) {
      if (e.key === " " && e.ctrlKey) { e.preventDefault(); if (menuOpen) menu(false); else act({ say: "menu" }); return; }
      if (menuOpen) {
        e.preventDefault();
        if (e.key === "Escape") { menu(false); return; }
        var open = screen.querySelector(".term.menu:not([hidden])");
        var lines = open.textContent.split("\n");
        for (var r = 0; r < lines.length; r++) {
          var it = item(r, Array.from(lines[r]).indexOf("│") + 1);
          if (it.key && (it.key === e.key || (it.key === "Tab" && e.key === "Tab"))) { choose(it); return; }
        }
        return;
      }
      var k = ORDER.indexOf(current), n = e.key === "ArrowDown" ? k + 1 : e.key === "ArrowUp" ? k - 1 : -1;
      if (n < 0 || n >= ORDER.length || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
      e.preventDefault();
      act({ go: ORDER[n] });
    });
  }

  // --- what moves in Force: a working session's pie fills in 8 steps of 125 ms (sidebar and header count, every
  // screen); in mobile's pane the agent's spinner turns, its verb shimmers, the running tool's dot blinks and the
  // timers and token count go up. The captures are still, so the page plays these on top of them.
  var live = document.querySelectorAll(".screen .term[data-view]");
  if (live.length) {
    var PIE = 0xF0A9E, STAR = "·✢✳✶✻✽", STARS = STAR + STAR.split("").reverse().join("");
    var pies = [], stars = [], dots = [], clocks = [], shimmer = [];
    // a cell's text changed length: keep the line 150 cells by taking spaces from (or giving them to) its end
    var fit = function (el, delta) {
      for (var n = el.nextSibling; n; n = n.nextSibling) {
        if (n.nodeType === 3 && n.nodeValue.indexOf("\n") >= 0) {
          var v = n.nodeValue, i = v.indexOf("\n");
          n.nodeValue = delta > 0 ? v.slice(0, Math.max(0, i - delta)) + v.slice(i)
                                  : v.slice(0, i) + new Array(1 - delta).join(" ") + v.slice(i);
          return;
        }
      }
    };
    var dur = function (s) {
      var h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
      return h ? h + "h " + m + "m " + x + "s" : m ? m + "m " + x + "s" : x + "s";
    };
    live.forEach(function (pre) {
      pre.querySelectorAll(".nf").forEach(function (el) {
        var c = el.textContent.codePointAt(0);
        if (c >= PIE && c < PIE + 8) pies.push(el);
      });
      pre.querySelectorAll("span").forEach(function (el) {
        var t = el.textContent, next = el.nextElementSibling;
        if (t.length === 1 && STAR.indexOf(t) >= 0 && next && /^\S+…$/.test(next.textContent)) {
          stars.push(el);
          var verb = next.textContent;   // the verb, one span per letter for the shimmer
          next.textContent = "";
          var letters = Array.from(verb).map(function (ch) {
            var s = document.createElement("span"); s.textContent = ch; next.appendChild(s); return s;
          });
          shimmer.push(letters);
        }
        if (t === "●" && el.nextSibling && /^ Running/.test(el.nextSibling.nodeValue || "")) dots.push(el);
        if (!el.children.length && /\b(\d+h )?(\d+m )?\d+s\b/.test(t) && pre.dataset.view === "mobile") {
          var m = /\b(?:(\d+)h )?(?:(\d+)m )?(\d+)s\b/.exec(t);
          clocks.push({ el: el, at: (+m[1] || 0) * 3600 + (+m[2] || 0) * 60 + (+m[3]) });
        }
        if (/^\d+ tokens\)$/.test(t)) clocks.push({ el: el, tokens: parseInt(t, 10) });
      });
    });
    var t0 = Date.now(), tick = 0;
    var step = function () {
      var ms = Date.now() - t0, s = Math.floor(ms / 1000);
      if (!still) {
        tick++;
        var pie = String.fromCodePoint(PIE + tick % 8);
        pies.forEach(function (el) { el.textContent = pie; });
        var star = STARS[tick % STARS.length];
        stars.forEach(function (el) { el.textContent = star; });
        shimmer.forEach(function (letters) {   // a lighter band sweeps across the verb and off its end
          var at = tick % (letters.length + 6) - 1;
          letters.forEach(function (l, i) { l.style.color = Math.abs(i - at) <= 1 ? "#ffd7af" : ""; });
        });
        dots.forEach(function (el) { el.style.visibility = Math.floor(ms / 600) % 2 ? "hidden" : ""; });
      }
      clocks.forEach(function (c) {
        var old = c.el.textContent, now = c.tokens != null
          ? (c.tokens + Math.floor(ms / 1000 * 2.7)) + " tokens)"
          : old.replace(/\b(?:\d+h )?(?:\d+m )?\d+s\b/, dur(c.at + s));
        if (now !== old) { c.el.textContent = now; fit(c.el, now.length - old.length); }
      });
    };
    setInterval(function () { if (!document.hidden) step(); }, 125);
  }

  // --- the film: it plays over the window and ends on it (its last frame is this window, drawn at 1200 px), then
  // fades and leaves the window live; Replay runs it again. With reduced motion, or when the browser will not play
  // it, the window is live from the start.
  var stage = document.getElementById("film");
  var film = document.getElementById("film-video");
  var btn = document.querySelector(".play");
  var replay = document.querySelector(".replay");
  var win = document.getElementById("cockpit");
  if (stage && film && btn && replay && win) {
    var phone = document.getElementById("phone");
    var phoneSize = window.matchMedia("(max-width: 640px)");
    if (phoneSize.matches) film.poster = film.dataset.posterPhone;   // the phone cut's first frame (the sources pick by media)
    var NOTE = { film: "When the film ends, the window stays: click around in it.",
                 live: "Click around. Every session in the sidebar opens, so do the counts at the top, and the keyboard icon opens the menu.",
                 phone: "Force at phone width, as in a phone's terminal. Tap a session to open it.",
                 phoneFilm: "When the film ends, Force stays: tap a session to open it." };
    var fitStage = function () {   // the window's 1200 px layout scaled to the stage; too small to read: .narrow
      var k = stage.clientWidth / 1200;
      stage.style.setProperty("--k", k);
      stage.classList.toggle("narrow", 13.2647 * k < 9.5);
    };
    if ("ResizeObserver" in window) new ResizeObserver(fitStage).observe(stage); else window.addEventListener("resize", fitStage);
    fitStage();
    var cue = hint && hint.textContent;
    var live = function (on) {
      stage.classList.toggle("live", on);
      win.inert = !on;
      if (phone) phone.inert = !on;
      film.setAttribute("aria-hidden", on ? "true" : "false");
      btn.hidden = on;
      replay.hidden = !on;
      if (hint) hint.textContent = phoneSize.matches ? (on ? NOTE.phone : NOTE.phoneFilm) : (on ? NOTE.live : NOTE.film);
      if (on && call) { call.style.animation = "none"; void call.offsetWidth; call.style.animation = ""; }   // the call starts afresh
    };
    var wanted = !still;           // the visitor's choice; the observer only plays while it is in view
    var sync = function () {
      var paused = film.paused;
      btn.classList.toggle("paused", paused);
      btn.setAttribute("aria-label", paused ? "Play the film" : "Pause the film");
    };
    var play = function () { var p = film.play(); if (p && p.catch) p.catch(function () { wanted = false; sync(); live(true); }); };
    film.addEventListener("play", sync);
    film.addEventListener("pause", sync);
    film.addEventListener("ended", function () { wanted = false; live(true); });
    // the captions: on the wallpaper under the window, never over it; timed to the film (seconds)
    var capEl = document.querySelector(".film-cap");
    var CAPS = [[.5, 2.6, "Seven agents. Seven terminals."], [2.8, 4.95, "Which one needs you?"],
                [6.3, 8.2, "Force: every agent in one terminal."], [8.25, 11.8, "It tells you who needs you."],
                [13.0, 15.6, "Close the terminal. They keep working."], [16.6, 18.75, "Open Force again. Nothing stopped."],
                [20.0, 22.4, "Stuck? Ask Chuck."]];
    var capNow = -1;
    var caption = function () {
      if (!capEl) return;
      var t = film.currentTime, i = -1;
      if (!stage.classList.contains("live")) CAPS.forEach(function (c, k) { if (t >= c[0] && t < c[1] - .3) i = k; });
      if (i === capNow) return;
      capNow = i;
      capEl.classList.remove("on");
      if (i >= 0) { capEl.textContent = CAPS[i][2]; void capEl.offsetWidth; capEl.classList.add("on"); }
    };
    var capLoop = function () { caption(); if (!film.paused && !film.ended) requestAnimationFrame(capLoop); };
    film.addEventListener("play", function () { requestAnimationFrame(capLoop); });
    film.addEventListener("seeked", caption);
    film.addEventListener("ended", caption);
    btn.addEventListener("click", function () {
      wanted = film.paused;
      if (wanted) play(); else film.pause();
    });
    film.addEventListener("click", function () { btn.click(); });
    replay.addEventListener("click", function () {
      live(false);
      film.currentTime = 0;
      wanted = true;
      play();
    });
    if (still) { live(true); replay.lastChild.textContent = " Play the film"; }
    else {
      live(false);
      if ("IntersectionObserver" in window) {
        new IntersectionObserver(function (es) {
          es.forEach(function (e) {
            if (e.isIntersecting && wanted && !stage.classList.contains("live")) play();
            else if (!e.isIntersecting && !film.paused) film.pause();
          });
        }, { threshold: 0.35 }).observe(stage);
      } else play();
    }
    sync();
  }

  // --- copy buttons
  // (the closing line's button copies the same line; where the clipboard is out of reach it shows the line instead)
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    var label = b.textContent;
    b.addEventListener("click", function () {
      var el = document.getElementById(b.getAttribute("data-copy"));
      var text = el.textContent.replace(/^\$\s*/, "");
      var show = function () { el.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "center" }); };
      if (!navigator.clipboard) return b.classList.contains("copy") ? undefined : show();
      navigator.clipboard.writeText(text).then(function () {
        b.textContent = "Copied";
        setTimeout(function () { b.textContent = label; }, 1600);
      }, function () { if (!b.classList.contains("copy")) show(); });
    });
  });
})();

// --- flair options (previews: ?fx=hero,panels,texture,beam put fx-NAME on <html>; the chosen ones get baked in)
(function () {
  var root = document.documentElement;
  var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var m = /[?&]fx=([^&]*)/.exec(location.search);
  if (m) decodeURIComponent(m[1]).split(",").forEach(function (n) { if (n) root.classList.add("fx-" + n.trim()); });
  var on = function (n) { return root.classList.contains("fx-" + n); };

  // hero: the window's tilt eases from 1 to 0 over the first part of the scroll towards it
  var stage = document.getElementById("film");
  if (on("hero") && stage && !still) {
    var tilt = function () {
      var r = stage.getBoundingClientRect(), vh = window.innerHeight;
      var t = Math.max(0, Math.min(1, (r.top - vh * .18) / (vh * .55)));
      stage.style.setProperty("--tilt", t.toFixed(3));
    };
    tilt();
    window.addEventListener("scroll", tilt, { passive: true });
    window.addEventListener("resize", tilt);
  }

  // panels: the light follows the cursor; the excerpts' bell rings, the waiting card breathes, the limit bars fill
  if (on("panels")) {
    document.querySelectorAll(".panel").forEach(function (p) {
      p.addEventListener("pointermove", function (e) {
        var r = p.getBoundingClientRect();
        p.style.setProperty("--mx", (e.clientX - r.left) + "px");
        p.style.setProperty("--my", (e.clientY - r.top) + "px");
      });
    });
    document.querySelectorAll(".shot .term span").forEach(function (s) {
      var st = s.getAttribute("style") || "";
      if (s.classList.contains("nf") && /#f0645e/i.test((s.parentNode.getAttribute && s.parentNode.getAttribute("style")) || "")) s.classList.add("bell");
      if (/(background|--bg):#2c2c2c/i.test(st)) s.classList.add("row-need");
      if (/[━╸]/.test(s.textContent) && !s.children.length) s.classList.add("bar");
    });
    var lim = document.querySelector(".p-limits");
    if (lim && "IntersectionObserver" in window) {
      new IntersectionObserver(function (es, o) {
        es.forEach(function (e) { if (e.isIntersecting) { lim.classList.add("seen"); o.disconnect(); } });
      }, { threshold: .6 }).observe(lim);
    } else if (lim) lim.classList.add("seen");
  }
  // --- the phone view (narrow screens): Force's session list; a tap opens a session, "Sessions" goes back
  var phoneEl = document.getElementById("phone");
  if (phoneEl) {
    var pscreen = phoneEl.querySelector(".phone-screen");
    var pback = phoneEl.querySelector(".phone-back");
    var ptitle = phoneEl.querySelector(".phone-title");
    var ppill = phoneEl.querySelector(".phone-pill");
    var plist = pscreen.querySelector('.term[data-view="list"]');
    var NEEDS = "billing-fix";
    var pshow = function (name) {
      pscreen.querySelectorAll(".term[data-view]").forEach(function (t) { t.hidden = t.dataset.view !== name; });
      pscreen.querySelectorAll(".phone-row").forEach(function (b) { b.hidden = name !== "list"; });
      var list = name === "list";
      pback.hidden = list;
      ptitle.textContent = list ? "Force" : name;
      ppill.hidden = name === NEEDS;
      ppill.textContent = list ? "Tap a session" : "\u25cf 1 needs you";
      ppill.classList.toggle("need", !list);
    };
    // one button over each session's two rows of the list (the rows are found by name, so a recapture can move them)
    var seen = {};
    plist.textContent.split("\n").forEach(function (line, row) {
      var name = (line.replace(/[^\x21-\x7e]+/g, " ").trim().split(" ")[0] || "");
      if (!pscreen.querySelector('.term[data-view="' + name + '"]') || name === "list" || seen[name]) return;
      seen[name] = true;
      var b = document.createElement("button");
      b.type = "button"; b.className = "phone-row"; b.style.top = "calc(" + row + " * var(--lh))";
      b.setAttribute("aria-label", "Open " + name);
      b.addEventListener("click", function () { pshow(name); });
      pscreen.appendChild(b);
    });
    pback.addEventListener("click", function () { pshow("list"); });
    ppill.addEventListener("click", function () { pshow(ppill.classList.contains("need") ? NEEDS : "list"); });
  }
})();
