/* Блок подбора каталога офиса как в каталоге MIM (office2/catalog.html, _catalog_filters.html mim_rows; lane office-catalog-mim,
   владелец 2026-11-11; образец — «Вид 2» клиента, shop_v2.js). Ширина, профиль, диаметр, производитель: клик открывает список, набор
   сужает его (matches), значение берётся кликом, Enter или Tab (Tab принимает набранное и переходит к следующему полю), выбранные —
   плашки с крестиком. Кнопки «Показать» нет: через секунду после изменения выдача запрашивается заново (страница сервера, её части
   [data-oc-live] и #items ставятся на место), адрес страницы обновляется. Только наш скрипт из /static (CSP панели). */
(function () {
  "use strict";

  // Какие значения списка видны при наборе. Как у MIM: числа — начинающиеся с набранного, по порядку списка; бренды — содержащие
  // набранное. Поменять поведение подбора — здесь, в одном месте.
  function matches(value, typed, isText) {
    if (!typed) return true;
    return isText ? value.indexOf(typed) >= 0 : value.indexOf(typed) === 0;
  }
  window.tiresOfficeCatalog = {matches: matches};

  var form = document.querySelector("form[data-oc]");
  if (!form) return;
  var DELAY = 1000, timer = null, seq = 0;

  function found(text) {
    var el = document.querySelector("#items [data-found]");
    if (el) el.textContent = text;
  }
  function target() {
    var pairs = [];
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.disabled || el.type === "submit" || el.type === "button") return;
      if ((el.type === "radio" || el.type === "checkbox") && !el.checked) return;
      var v = String(el.value || "").trim();
      if (v) pairs.push(encodeURIComponent(el.name) + "=" + encodeURIComponent(v));
    });
    return (form.getAttribute("action") || location.pathname) + (pairs.length ? "?" + pairs.join("&") : "");
  }
  function put(doc) {                                     // the parts of the fresh page put in place: the rows, the tabs, «все по фильтру»
    var items = doc.getElementById("items"), old = document.getElementById("items");
    if (!items || !old) throw new Error("no rows");
    old.parentNode.replaceChild(document.importNode(items, true), old);
    Array.prototype.forEach.call(document.querySelectorAll("[data-oc-live][id]"), function (el) {
      var fresh = doc.getElementById(el.id);
      if (fresh) el.parentNode.replaceChild(document.importNode(fresh, true), el);
    });
    Array.prototype.forEach.call(document.querySelectorAll("form[id] input[name=next]"), function (input) {
      var twin = doc.querySelector("form[id='" + input.form.id + "'] input[name=next]");
      if (twin) input.value = twin.value;                // where the actions of the rows come back to
    });
    var bar = document.getElementById("bulk");
    if (bar) { bar.hidden = true; var n = bar.querySelector("[data-count]"); if (n) n.textContent = "0"; }
    document.dispatchEvent(new Event("tires:table-loaded"));
  }
  function load() {
    var url = target(), mine = ++seq;
    clearTimeout(timer);
    fetch(url, {credentials: "same-origin"}).then(function (r) {
      if (!r.ok || r.redirected) throw new Error("status " + r.status);
      return r.text();
    }).then(function (html) {
      if (mine !== seq) return;
      put(new DOMParser().parseFromString(html, "text/html"));
      history.replaceState(null, "", url);
    }).catch(function () { if (mine === seq) location.href = url; });
  }
  function later() {
    found("Подбираем…");
    clearTimeout(timer);
    timer = setTimeout(load, DELAY);
  }
  function now() { clearTimeout(timer); ++seq; location.href = target(); }

  Array.prototype.forEach.call(form.querySelectorAll("[data-oc-ms]"), function (box) {
    var input = box.querySelector(".oc-ms-in"), list = box.querySelector(".oc-ms-list");
    var chips = box.querySelector("[data-oc-chips]"), counter = box.querySelector("[data-oc-n]");
    var one = box.hasAttribute("data-one"), text = box.hasAttribute("data-text");
    var prefix = box.getAttribute("data-prefix") || "", active = -1;
    var options = Array.prototype.slice.call(list.querySelectorAll("[role=option]"));
    if (!one) input.removeAttribute("name");             // the typed text is never sent: only the chips (their hidden inputs)
    options.forEach(function (o, i) { o.id = list.id + "-" + i; });

    function name() { return box.getAttribute("data-name"); }
    function valueOf(o) { return o.getAttribute("data-v"); }
    function textOf(o) { return (text ? o.getAttribute("data-label") || o.textContent : valueOf(o)).toLowerCase(); }
    function chosen() {
      if (one) return input.value.trim() ? [input.value.trim()] : [];
      return Array.prototype.map.call(chips.querySelectorAll("input[type=hidden]"), function (h) { return h.value; });
    }
    function shown() { return options.filter(function (o) { return !o.hidden; }); }
    function typed() { return input.value.trim().toLowerCase().replace(text ? /^$/ : /^[rр]/, ""); }
    function mark() {
      var on = chosen();
      options.forEach(function (o) {
        if (on.indexOf(valueOf(o)) >= 0) o.setAttribute("aria-selected", "true"); else o.removeAttribute("aria-selected");
      });
      if (counter) { counter.textContent = String(on.length); counter.hidden = !on.length; }
      if (!one) input.placeholder = on.length ? "" : (text ? "Все бренды" : "Все");
    }
    function narrow() {
      var t = typed();
      if (one && options.some(function (o) { return valueOf(o) === t; }) && input.value.trim() === input.defaultValue) t = "";   // the value itself: the whole list
      options.forEach(function (o) { o.hidden = !matches(textOf(o), t, text); });
      highlight(shown().length && t ? 0 : -1);
    }
    function highlight(i) {
      var vis = shown();
      options.forEach(function (o) { o.classList.remove("is-active"); });
      active = vis.length ? Math.max(-1, Math.min(i, vis.length - 1)) : -1;
      if (active >= 0) {
        vis[active].classList.add("is-active");
        input.setAttribute("aria-activedescendant", vis[active].id);
        if (vis[active].scrollIntoView) vis[active].scrollIntoView({block: "nearest"});
      } else input.removeAttribute("aria-activedescendant");
    }
    function open() {
      if (!list.hidden) return;
      list.hidden = false; input.setAttribute("aria-expanded", "true"); box.classList.add("is-open"); narrow();
    }
    function close() { list.hidden = true; input.setAttribute("aria-expanded", "false"); box.classList.remove("is-open"); }
    function label(value) {
      var o = options.filter(function (x) { return valueOf(x) === value; })[0];
      return o ? (o.getAttribute("data-label") || o.textContent) : prefix + value;
    }
    function chip(value) {
      var li = document.createElement("li"), hidden = document.createElement("input"), span = document.createElement("span");
      var x = document.createElement("button");
      li.className = "oc-ms-chip" + (name() === "xb" ? " oc-ms-chip--x" : "");
      hidden.type = "hidden"; hidden.name = name(); hidden.value = value;
      span.textContent = label(value);
      x.type = "button"; x.className = "oc-ms-x"; x.tabIndex = -1; x.setAttribute("data-oc-x", ""); x.title = "Убрать";
      x.setAttribute("aria-label", "Убрать " + label(value));
      x.innerHTML = '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
      li.appendChild(hidden); li.appendChild(span); li.appendChild(x);
      chips.appendChild(li);
    }
    function take(value) {                                 // a value chosen: a chip (several) or the field's value (one)
      value = String(value || "").trim();
      if (!value) return false;
      if (one) {
        var changed = input.defaultValue !== value;
        input.value = value; input.defaultValue = value; close(); mark();
        if (changed) later();
        return true;
      }
      input.value = "";
      if (chosen().indexOf(value) < 0) { chip(value); mark(); later(); }
      narrow();
      return true;
    }
    function typedValue() {                                // what Enter or Tab takes: the value typed in full, else the option lit, else the digits
      var vis = shown(), t = typed();
      if (!t && active < 0) return "";
      var exact = vis.filter(function (o) { return text ? textOf(o) === t : valueOf(o) === t; })[0];
      if (exact) return valueOf(exact);
      if (active >= 0 && vis[active]) return valueOf(vis[active]);
      if (text) return "";
      return /^\d{1,3}$/.test(t) ? String(parseInt(t, 10)) : "";
    }

    input.addEventListener("focus", open);
    input.addEventListener("click", open);
    input.addEventListener("input", function () { open(); narrow(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault(); open(); highlight(active + (e.key === "ArrowDown" ? 1 : -1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        var v = input.value.trim() || active >= 0 ? typedValue() : "";
        if (v) take(v);
        else if (one && !input.value.trim() && input.defaultValue) { input.defaultValue = ""; close(); mark(); later(); }
        else if (!input.value.trim()) { close(); load(); }        // Enter in an empty field: the rows at once
      } else if (e.key === "Tab") {                           // Tab: the typed value taken, the focus goes on (ширина → профиль → диаметр)
        var t = input.value.trim() ? typedValue() : "";
        if (t) take(t);
        if (!one) input.value = "";
        close();
      } else if (e.key === "Escape") {
        close();
      } else if (e.key === "Backspace" && !one && !input.value) {
        var last = chips.lastElementChild;
        if (last) { last.remove(); mark(); later(); }
      }
    });
    input.addEventListener("blur", function () {
      setTimeout(function () {
        if (box.contains(document.activeElement)) return;
        close();
        if (one) {                                         // a typed value of one field is taken when leaving it (empty: any)
          var raw = input.value.trim().replace(/^[rRрР]/, "");
          var clean = /^\d{1,3}$/.test(raw) ? String(parseInt(raw, 10)) : "";
          input.value = clean;
          if (clean !== input.defaultValue) { input.defaultValue = clean; mark(); later(); }
        } else input.value = "";
      }, 150);
    });
    list.addEventListener("mousedown", function (e) { e.preventDefault(); });   // the field keeps the focus: the next value at once
    list.addEventListener("click", function (e) {
      var o = e.target.closest("[role=option]");
      if (o) take(valueOf(o));
    });
    if (chips) chips.addEventListener("click", function (e) {
      var x = e.target.closest("[data-oc-x]");
      if (!x) return;
      e.preventDefault();
      x.closest(".oc-ms-chip").remove(); mark(); later();
    });
    box.querySelector(".oc-ms-box").addEventListener("click", function (e) {
      if (!e.target.closest("[data-oc-x]") && !e.target.closest("[role=option]")) input.focus();
    });
    Array.prototype.forEach.call(box.querySelectorAll("[data-oc-bm]"), function (radio) {   // «Только / Кроме»: the chips change sides
      radio.addEventListener("change", function () {
        var side = radio.value === "x" ? "xb" : "b";
        box.setAttribute("data-name", side);
        Array.prototype.forEach.call(chips.querySelectorAll(".oc-ms-chip"), function (li) {
          li.querySelector("input[type=hidden]").name = side;
          li.classList.toggle("oc-ms-chip--x", side === "xb");
        });
        if (chosen().length) later();
      });
    });
    mark();
  });

  form.addEventListener("change", function (e) {
    var t = e.target;
    if (t.closest(".oc-ms-in, [data-oc-ms]") || t.hasAttribute("data-oc-num") || t.hasAttribute("data-oc-bm")) return;
    if (t.hasAttribute("data-oc-mixed")) { now(); return; }    // «Разноширокие»: other fields, the page anew at once
    later();
  });
  Array.prototype.forEach.call(form.querySelectorAll("[data-oc-num]"), function (n) {
    n.addEventListener("input", function () { if (/^\d*$/.test(n.value.trim())) later(); });
  });
  form.addEventListener("submit", function (e) { e.preventDefault(); found("Подбираем…"); load(); });
})();
