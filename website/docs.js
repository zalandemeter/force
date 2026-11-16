// Force docs: the copy button on the install line, and the contents list marking the section you are reading.
(function () {
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var el = document.getElementById(b.getAttribute("data-copy"));
      var text = el.textContent.replace(/^\s*\$\s*/, "").trim();
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(text).then(function () {
        b.textContent = "Copied";
        setTimeout(function () { b.textContent = "Copy"; }, 1600);
      });
    });
  });

  // a code block that scrolls sideways fades at its right edge until scrolled to the end
  var codes = Array.prototype.slice.call(document.querySelectorAll(".code"));
  function edges() {
    codes.forEach(function (c) {
      var over = c.scrollWidth > c.clientWidth + 1;
      c.classList.toggle("overflows", over);
      c.classList.toggle("at-end", !over || c.scrollLeft + c.clientWidth >= c.scrollWidth - 2);
    });
  }
  codes.forEach(function (c) { c.addEventListener("scroll", edges, { passive: true }); });
  window.addEventListener("resize", edges);
  edges();

  // the version menu closes on Escape and on a click elsewhere (it is a <details>: it opens without JavaScript)
  var ver = document.querySelector(".ver");
  if (ver) {
    document.addEventListener("click", function (e) { if (ver.open && !ver.contains(e.target)) ver.open = false; });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && ver.open) { ver.open = false; ver.querySelector("summary").focus(); }
    });
  }

  var links = Array.prototype.slice.call(document.querySelectorAll(".toc a"));
  var sections = links.map(function (a) { return document.querySelector(a.getAttribute("href")); });
  function mark() {
    var y = window.scrollY + 120, cur = 0;
    sections.forEach(function (s, i) { if (s && s.offsetTop <= y) cur = i; });
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 4) cur = sections.length - 1;
    links.forEach(function (a, i) {
      if (i !== cur) { a.removeAttribute("aria-current"); return; }
      if (a.hasAttribute("aria-current")) return;
      a.setAttribute("aria-current", "true");
      var toc = a.parentNode;   // on a phone the list is one scrolling row: keep the current one in it
      if (toc.scrollWidth > toc.clientWidth) toc.scrollLeft = a.offsetLeft - toc.clientWidth / 3;
    });
  }
  window.addEventListener("scroll", mark, { passive: true });
  mark();
})();
