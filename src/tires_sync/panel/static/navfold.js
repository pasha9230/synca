// Сворачивание разделов меню по заголовку (решение владельца 2026-11-02).
// Подключён в <head>: свёрнутые разделы прячутся классами на <html> до отрисовки, без мигания.
// Выбор помнит браузер (localStorage); без хранилища все разделы открыты. Раздел текущей страницы открыт всегда,
// но сохранённый выбор при этом не переписывается.
(function () {
  "use strict";
  var KEY = "ts-nav-closed", SECS = ["sales", "goods", "analytics", "setup"];
  var root = document.documentElement;
  var read = function () {
    try { return (localStorage.getItem(KEY) || "").split(" ").filter(function (k) { return SECS.indexOf(k) >= 0; }); } catch (e) { return []; }
  };
  read().forEach(function (k) { root.classList.add("navc-" + k); });
  document.addEventListener("DOMContentLoaded", function () {
    var closed = read();
    document.querySelectorAll(".sb-group[data-sec]").forEach(function (g) {
      var k = g.getAttribute("data-sec"), btn = g.querySelector(".sb-fold");
      if (!btn) return;
      var set = function (open) { g.classList.toggle("is-closed", !open); btn.setAttribute("aria-expanded", String(open)); };
      set(closed.indexOf(k) < 0 || g.hasAttribute("data-current"));
      btn.addEventListener("click", function () {
        var open = g.classList.contains("is-closed");
        set(open);
        var now = read().filter(function (x) { return x !== k; });
        if (!open) now.push(k);
        try { localStorage.setItem(KEY, now.join(" ")); } catch (e) { /* private window: only this page */ }
      });
    });
    SECS.forEach(function (k) { root.classList.remove("navc-" + k); });
  });
})();
