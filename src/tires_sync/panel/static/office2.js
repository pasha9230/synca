// «Новый вид» of the office's «Заказы» (lane port-office-orders): the selection of «Список» with its side panel, and the columns of the
// order's card (variant C) — fold, widen by the border, back to the default; the layout is the person's own (POST /orders/card/layout).
// Without this script the pages still work: the forms of the panel need the ids it puts in, the card shows its three columns as saved.
(function () {
  "use strict";

  // 1) «Список»: a checkbox per row (every row of an order is the order), «все на странице», the side panel with what is selected
  var wrap = document.querySelector("[data-bulk-wrap]");
  if (wrap) {
    var panel = wrap.querySelector("[data-bulk]"), all = wrap.querySelector("[data-sel-all]"), hint = wrap.querySelector("[data-bulk-hint]");
    var boxes = Array.prototype.slice.call(wrap.querySelectorAll("[data-sel]"));
    var money = function (n) { return n.toLocaleString("ru-RU", {maximumFractionDigits: 0}) + " ₽"; };
    var chosen = function () {
      var seen = {}, out = [];
      boxes.forEach(function (b) { if (b.checked && !seen[b.dataset.sel]) { seen[b.dataset.sel] = 1; out.push(b); } });
      return out;
    };
    var draw = function () {
      var picked = chosen(), ids = picked.map(function (b) { return b.dataset.sel; });
      boxes.forEach(function (b) { var tr = b.closest("tr"); if (tr) tr.classList.toggle("is-sel", b.checked); });
      if (all) { all.checked = picked.length > 0 && ids.length === new Set(boxes.map(function (b) { return b.dataset.sel; })).size; all.indeterminate = picked.length > 0 && !all.checked; }
      if (!panel) return;
      panel.hidden = !picked.length;
      if (hint) hint.hidden = !!picked.length;
      panel.querySelectorAll("[data-bulk-n]").forEach(function (n) { n.textContent = String(picked.length); });
      panel.querySelectorAll("[data-bulk-ids]").forEach(function (box) {
        box.textContent = "";
        ids.forEach(function (id) { var i = document.createElement("input"); i.type = "hidden"; i.name = "id"; i.value = id; box.appendChild(i); });
      });
      var xls = panel.querySelector("[data-bulk-xls]");
      if (xls) xls.href = "/orders/export.xlsx?ids=" + ids.join(",");
      var sum = panel.querySelector("[data-bulk-sum]");
      if (sum) sum.textContent = money(picked.reduce(function (a, b) { return a + (parseFloat(b.dataset.sum) || 0); }, 0));
      var list = panel.querySelector("[data-bulk-list]");
      list.textContent = "";
      picked.forEach(function (b) {
        var li = document.createElement("li"), main = document.createElement("div"), top = document.createElement("div"), cli = document.createElement("span");
        var stage = document.createElement("span"), x = document.createElement("button");
        main.className = "o2-bl-main";
        top.innerHTML = "<b></b> <span class=\"muted small\"></span>";
        top.querySelector("b").textContent = b.dataset.wm ? "№ " + b.dataset.wm : "№ WebMIM ещё нет";
        top.querySelector("span").textContent = "внутр. " + b.dataset.sel;
        cli.className = "o2-bl-cli"; cli.textContent = b.dataset.client || "";
        stage.className = "o2-stage"; stage.style.setProperty("--c", b.dataset.stageColor || "#999"); stage.textContent = b.dataset.stageName || "";
        x.type = "button"; x.className = "icon-btn"; x.title = "Убрать из выбора"; x.setAttribute("aria-label", "Убрать из выбора"); x.textContent = "×";
        x.addEventListener("click", function () { mark(b.dataset.sel, false); draw(); });
        var second = document.createElement("div");
        second.className = "o2-bl-row"; second.appendChild(cli); second.appendChild(stage);
        main.appendChild(top); main.appendChild(second); li.appendChild(main); li.appendChild(x); list.appendChild(li);
      });
    };
    var mark = function (id, on) { boxes.forEach(function (b) { if (b.dataset.sel === id) b.checked = on; }); };
    boxes.forEach(function (b) { b.addEventListener("change", function () { mark(b.dataset.sel, b.checked); draw(); }); });
    if (all) all.addEventListener("change", function () { boxes.forEach(function (b) { b.checked = all.checked; }); draw(); });
    var none = panel && panel.querySelector("[data-bulk-none]");
    if (none) none.addEventListener("click", function () { boxes.forEach(function (b) { b.checked = false; }); draw(); });
    wrap.addEventListener("click", function (e) {                 // beside a checkbox in a tall row: never the row's «open the order» (app.js)
      if (e.target.closest && e.target.closest(".o2-chk")) e.stopPropagation();
    });
    draw();                                                       // after «Назад» the browser may keep the ticks
  }

  // 2) The card: the three columns («Заказ», «Лента», «Чат Авито»)
  var body = document.querySelector("[data-ccs]");
  if (body) {
    var DEFAULT = {w: [1.15, 1, 0.85], off: [false, false, false]};
    var layout;
    try { layout = JSON.parse(body.dataset.layout); } catch (e) { layout = JSON.parse(JSON.stringify(DEFAULT)); }
    var cols = Array.prototype.slice.call(body.querySelectorAll("[data-cc]"));
    var splits = Array.prototype.slice.call(body.querySelectorAll("[data-split]"));
    var timer = null;
    var save = function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var data = new FormData();
        data.append("csrf", body.dataset.csrf); data.append("layout", JSON.stringify(layout));
        fetch("/orders/card/layout", {method: "POST", body: data, credentials: "same-origin", headers: {"X-Requested-With": "fetch"}}).catch(function () {});
      }, 400);
    };
    var near = function (i) {                                  // the border i moves the nearest open columns on its two sides
      var a = i, b = i + 1;
      while (a >= 0 && layout.off[a]) a--;
      while (b < 3 && layout.off[b]) b++;
      return a >= 0 && b < 3 ? [a, b] : null;
    };
    var apply = function () {
      cols.forEach(function (col, i) {
        col.classList.toggle("is-off", !!layout.off[i]);
        col.style.setProperty("--w", String(layout.w[i]));
        col.querySelectorAll("[data-cc-toggle]").forEach(function (b) { b.setAttribute("aria-expanded", layout.off[i] ? "false" : "true"); });
        var strip = col.querySelector(".o2c-strip");               // the strip shows while the column is folded: it opens it (expanded «false»)
        if (strip) strip.setAttribute("aria-expanded", "false");
      });
      splits.forEach(function (s, i) { s.classList.toggle("is-idle", !near(i)); });
    };
    body.addEventListener("click", function (e) {
      var t = e.target.closest && e.target.closest("[data-cc-toggle]");
      if (!t) return;
      var i = +t.dataset.ccToggle, off = !layout.off[i];
      if (off && layout.off.filter(function (x) { return !x; }).length === 1) return;    // one column stays open
      layout.off[i] = off; apply(); save();
    });
    var reset = document.querySelector("[data-cc-reset]");
    if (reset) reset.addEventListener("click", function () { layout = JSON.parse(JSON.stringify(DEFAULT)); apply(); save(); });
    splits.forEach(function (s, i) {
      s.addEventListener("dblclick", function () { var p = near(i); if (!p) return; layout.w[p[0]] = DEFAULT.w[p[0]]; layout.w[p[1]] = DEFAULT.w[p[1]]; apply(); save(); });
      s.addEventListener("keydown", function (e) {                // ← → widen by the keyboard
        var p = near(i); if (!p || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
        e.preventDefault();
        var d = e.key === "ArrowLeft" ? -0.05 : 0.05, a = layout.w[p[0]] + d, b = layout.w[p[1]] - d;
        if (a < 0.2 || b < 0.2) return;
        layout.w[p[0]] = +a.toFixed(3); layout.w[p[1]] = +b.toFixed(3); apply(); save();
      });
      s.addEventListener("pointerdown", function (e) {
        var p = near(i); if (!p) return;
        e.preventDefault();
        var A = cols[p[0]], B = cols[p[1]], startX = e.clientX, wa = A.getBoundingClientRect().width, wb = B.getBoundingClientRect().width;
        var share = layout.w[p[0]] + layout.w[p[1]];
        s.classList.add("is-drag"); body.classList.add("is-dragging");
        if (s.setPointerCapture) s.setPointerCapture(e.pointerId);
        var move = function (ev) {
          var dx = ev.clientX - startX, na = Math.max(wa + dx, 120), nb = Math.max(wb - dx, 120);
          var a = share * na / (na + nb), b = share - a;
          if (a < 0.2 || b < 0.2 || a > 5 || b > 5) return;
          layout.w[p[0]] = +a.toFixed(3); layout.w[p[1]] = +b.toFixed(3); apply();
        };
        var up = function () {
          s.classList.remove("is-drag"); body.classList.remove("is-dragging");
          s.removeEventListener("pointermove", move); s.removeEventListener("pointerup", up); s.removeEventListener("pointercancel", up);
          save();
        };
        s.addEventListener("pointermove", move); s.addEventListener("pointerup", up); s.addEventListener("pointercancel", up);
      });
    });
    apply();
  }

  // 3) The card's «Лента»: Все / Заметки / Этапы / Чат / Система
  var tabs = document.querySelector("[data-feed-tabs]"), feed = document.querySelector("[data-feed]");
  if (tabs && feed) {
    tabs.addEventListener("click", function (e) {
      var t = e.target.closest && e.target.closest("[data-feed-tab]");
      if (!t) return;
      var kind = t.dataset.feedTab;
      tabs.querySelectorAll("[data-feed-tab]").forEach(function (b) { var on = b === t; b.classList.toggle("is-on", on); b.setAttribute("aria-selected", on ? "true" : "false"); });
      feed.querySelectorAll("li[data-kind]").forEach(function (li) { li.hidden = !!kind && li.dataset.kind !== kind; });
    });
  }
})();
