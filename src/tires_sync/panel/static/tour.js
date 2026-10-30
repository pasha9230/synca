/* «Обзор» (_tour.html): the step's element scrolled into view and the window put next to it; Enter or Space on «Дальше»/«Назад».
   A resize (a phone's keyboard, its address bar) or a scroll only moves the window: no scroll, no focus taken from a field.
   Without this script the tour still works (radio buttons and CSS). */
(function () {
  var root = document.querySelector("[data-tour-root]");
  if (!root) return;
  var radios = root.querySelectorAll(".tour-radio");

  function show(moved) {
    var radio = root.querySelector(".tour-radio:checked");
    if (!radio) return;
    var pop = radio.nextElementSibling, target = document.querySelector(pop.getAttribute("data-tour-sel"));
    pop.classList.remove("is-placed");
    pop.style.left = pop.style.top = "";
    if (!target || !target.getClientRects().length) return;          // not on the page now (no return yet): the window at the corner
    if (!moved) target.scrollIntoView({block: "center", behavior: "auto"});
    var r = target.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight, gap = 16;
    var vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    var left = r.right + gap + w <= vw ? r.right + gap : Math.max(gap, Math.min(r.left, vw - w - gap));
    var top = left === r.right + gap ? Math.max(gap, Math.min(r.top, vh - h - gap))
      : (r.bottom + gap + h <= vh ? r.bottom + gap : Math.max(gap, r.top - h - gap));
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    pop.classList.add("is-placed");
    var focus = pop.querySelector(".btn--primary");
    if (focus && !moved) focus.focus({preventScroll: true});
  }

  root.querySelectorAll("[data-tour-go]").forEach(function (label) {
    label.tabIndex = 0;
    label.setAttribute("role", "button");
    label.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); label.click(); }
    });
  });
  radios.forEach(function (radio) { radio.addEventListener("change", function () { show(false); }); });
  var queued = false;
  function moved() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function () { queued = false; show(true); });
  }
  window.addEventListener("resize", moved);
  window.addEventListener("scroll", moved, {passive: true});
  var menu = document.querySelector(".sidebar");                       // the steps of the menu: it scrolls on its own
  if (menu) menu.addEventListener("scroll", moved, {passive: true});
  show(false);
})();
