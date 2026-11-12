// «Аналитика рекламы»: строки групп сворачиваются и раскрываются по стрелке (владелец 2026-11-12, «как в Roistat»).
// Без скрипта видно всё дерево. Со скриптом открыт верхний уровень: видны его группы, глубже — по клику.
(function () {
  "use strict";
  var table = document.getElementById("ads-tree");
  if (!table) return;
  var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr[data-node]"));
  var byId = {}, open = {};
  rows.forEach(function (r) { byId[r.getAttribute("data-node")] = r; });
  var shown = function (id) {                       // the row of `id` is visible: every group above it is open
    while (id) {
      if (!open[id]) return false;
      var row = byId[id];
      id = row ? row.getAttribute("data-parent") : "";
    }
    return true;
  };
  var apply = function () {
    rows.forEach(function (r) {
      var parent = r.getAttribute("data-parent");
      r.hidden = !!parent && !shown(parent);
      var btn = r.querySelector(".ads-tog");
      if (btn) {
        var id = r.getAttribute("data-node");
        btn.setAttribute("aria-expanded", String(!!open[id]));
        btn.textContent = open[id] ? "▾" : "▸";
      }
    });
  };
  rows.forEach(function (r) {
    var btn = r.querySelector(".ads-tog");
    if (!btn) return;
    var id = r.getAttribute("data-node");
    if (r.getAttribute("data-depth") === "0") open[id] = true;
    btn.hidden = false;
    btn.setAttribute("aria-label", "Раскрыть или свернуть");
    btn.addEventListener("click", function () { open[id] = !open[id]; apply(); });
  });
  apply();
})();
