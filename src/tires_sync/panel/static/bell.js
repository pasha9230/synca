(function () {
  "use strict";
  var btn = document.getElementById("bell-btn"), pop = document.getElementById("bell-pop"),
      list = document.getElementById("bell-list"), count = document.getElementById("bell-count");
  if (!btn || !pop || !list || !count) return;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function load() {
    return fetch("/alerts/feed", {credentials: "same-origin", headers: {"Accept": "application/json"}}).then(function (r) {
      if (!r.ok || r.redirected) throw new Error(r.status);                    // an expired login answers with the login page
      return r.json();
    });
  }
  function offline(on) {
    btn.classList.toggle("is-offline", on);
    btn.title = on ? "Нет связи с панелью: уведомления не обновляются" : "";
  }
  function setCount(n) {
    count.textContent = n > 99 ? "99+" : String(n);
    count.hidden = !n;
    btn.setAttribute("aria-label", "Уведомления" + (n ? ": " + n : ""));
  }
  function fill(data) {
    list.textContent = "";
    if (!data.items.length) { list.appendChild(el("div", "bell-empty", "Нет новых")); return; }
    data.items.forEach(function (it) {
      var a = el("a", "bell-row");
      a.href = it.href || "/orders/" + encodeURIComponent(it.order_id);
      a.appendChild(el("span", "bell-dot bell-dot--" + (["red", "yellow"].indexOf(it.level) >= 0 ? it.level : "green")));
      var body = el("div", "bell-body");
      body.appendChild(el("div", "bell-title", (it.title || "Заказ №" + it.order_id) + (it.client ? " · " + it.client : "")));
      body.appendChild(el("div", "bell-text", (it.text || "") + (it.more ? " · ещё " + it.more : "")));
      a.appendChild(body);
      a.appendChild(el("span", "bell-time", it.at));
      list.appendChild(a);
    });
  }
  function refresh(withList) {
    return load().then(function (data) {
      offline(false);
      setCount(data.count);
      if (withList) fill(data);
    }).catch(function () {
      offline(true);
      if (withList) { list.textContent = ""; list.appendChild(el("div", "bell-empty", "Нет связи с панелью: обновите страницу")); }
    });
  }
  function open() {
    pop.hidden = false; btn.setAttribute("aria-expanded", "true");
    list.textContent = ""; list.appendChild(el("div", "bell-empty", "Загружаю…"));
    refresh(true);
  }
  function close() { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); }

  btn.addEventListener("click", function () { if (pop.hidden) open(); else close(); });
  document.getElementById("bell-close").addEventListener("click", close);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !pop.hidden) { close(); btn.focus(); } });
  document.addEventListener("click", function (e) { if (!pop.hidden && !document.getElementById("bell").contains(e.target)) close(); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(!pop.hidden); });
  setInterval(function () { if (!document.hidden) refresh(!pop.hidden); }, 60000);
})();
