/* «Вид 2» of the client's block of the filters (/shop, _shop_filters2.html; the owner 2026-11-09…10). The lists of the width, the profile,
   the radius and the brands: a click opens the list, the digits typed narrow it, a value is taken by a click or Enter (Tab takes the
   typed one and goes to the next field); the chosen ones are chips with a cross. No «Показать»: a second after the last change the rows
   are asked anew (the page of the server, its part .shop-results put in place), «Подбираем…» meanwhile; never on each key. Only our
   own script of /static (the CSP of «Вид 2»). */
(function () {
  "use strict";
  var form = document.querySelector("form[data-fv2]");
  if (!form) return;
  var DELAY = 1000, timer = null, seq = 0;

  function found(text) {
    var el = document.querySelector(".shop-results [data-found]");
    if (el) el.textContent = text;
  }
  function target() {
    var pairs = [];
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.disabled || el.type === "submit") return;
      if ((el.type === "radio" || el.type === "checkbox") && !el.checked) return;
      var v = String(el.value || "").trim();
      if (v) pairs.push(encodeURIComponent(el.name) + "=" + encodeURIComponent(v));
    });
    return (form.getAttribute("action") || location.pathname) + (pairs.length ? "?" + pairs.join("&") : "");
  }
  function load() {
    var url = target(), mine = ++seq;
    fetch(url, {credentials: "same-origin"}).then(function (r) {
      if (!r.ok) throw new Error("status " + r.status);
      return r.text();
    }).then(function (html) {
      if (mine !== seq) return;
      var doc = new DOMParser().parseFromString(html, "text/html");
      var fresh = doc.querySelector(".shop-results"), old = document.querySelector(".shop-results");
      if (!fresh || !old) throw new Error("no rows");
      old.parentNode.replaceChild(document.importNode(fresh, true), old);
      history.replaceState(null, "", url);
    }).catch(function () { if (mine === seq) location.href = url; });
  }
  function later() {
    found("Подбираем…");
    clearTimeout(timer);
    timer = setTimeout(load, DELAY);
  }
  function now() { clearTimeout(timer); ++seq; location.href = target(); }

  Array.prototype.forEach.call(form.querySelectorAll("[data-ms]"), function (box) {
    var input = box.querySelector(".shop-ms-in"), list = box.querySelector(".shop-ms-list");
    var chips = box.querySelector("[data-ms-chips]"), counter = box.querySelector("[data-ms-n]");
    var name = box.getAttribute("data-name"), one = box.hasAttribute("data-one"), text = box.hasAttribute("data-text");
    var prefix = box.getAttribute("data-prefix") || "", active = -1;
    var options = Array.prototype.slice.call(list.querySelectorAll("[role=option]"));
    if (!one) input.removeAttribute("name");          // the typed text is never sent: only the chips (their hidden inputs)
    options.forEach(function (o, i) { o.id = list.id + "-" + i; });

    function chosen() {
      if (one) return input.value.trim() ? [input.value.trim()] : [];
      return Array.prototype.map.call(chips.querySelectorAll("input[type=hidden]"), function (h) { return h.value; });
    }
    function shown() { return options.filter(function (o) { return !o.hidden; }); }
    function mark() {
      var on = chosen();
      options.forEach(function (o) {
        if (on.indexOf(o.getAttribute("data-v")) >= 0) o.setAttribute("aria-selected", "true"); else o.removeAttribute("aria-selected");
      });
      if (counter) { counter.textContent = String(on.length); counter.hidden = !on.length; }
      if (!one) input.placeholder = on.length ? "" : (text ? "Все бренды" : "Все");
    }
    function narrow() {
      var typed = input.value.trim().toLowerCase().replace(/^[rр]/, "");
      if (one && options.some(function (o) { return o.getAttribute("data-v") === typed; })) typed = "";   // the value itself: the whole list
      options.forEach(function (o) {
        var v = (text ? o.textContent : o.getAttribute("data-v")).toLowerCase();
        o.hidden = !!typed && (text ? v.indexOf(typed) < 0 : v.indexOf(typed) !== 0);
      });
      highlight(shown().length && typed ? 0 : -1);
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
      var o = options.filter(function (x) { return x.getAttribute("data-v") === value; })[0];
      return o ? o.textContent : prefix + value;
    }
    function chip(value) {
      var li = document.createElement("li"), hidden = document.createElement("input"), span = document.createElement("span");
      var x = document.createElement("button");
      li.className = "shop-ms-chip";
      hidden.type = "hidden"; hidden.name = name; hidden.value = value;
      span.textContent = label(value);
      x.type = "button"; x.className = "shop-ms-x"; x.setAttribute("data-ms-x", ""); x.title = "Убрать";
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
    function typedValue() {                                // what Enter or Tab takes: the option lit, else the typed size itself
      var vis = shown(), raw = input.value.trim();
      if (active >= 0 && vis[active]) return vis[active].getAttribute("data-v");
      if (!raw) return "";
      if (text) return vis.length ? vis[0].getAttribute("data-v") : "";
      raw = raw.replace(/^[rRрР]/, "");
      return /^\d{1,3}$/.test(raw) ? String(parseInt(raw, 10)) : "";
    }

    input.addEventListener("focus", open);
    input.addEventListener("click", open);
    input.addEventListener("input", function () { open(); narrow(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault(); open(); highlight(active + (e.key === "ArrowDown" ? 1 : -1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        var v = typedValue();
        if (v) take(v); else if (one && !input.value.trim() && input.defaultValue) { input.defaultValue = ""; close(); later(); }
      } else if (e.key === "Tab") {
        var t = input.value.trim() && typedValue();
        if (t) take(t);
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
        if (one) {                                       // a typed value of one field is taken when leaving it (empty: any)
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
      if (o) take(o.getAttribute("data-v"));
    });
    if (chips) chips.addEventListener("click", function (e) {
      var x = e.target.closest("[data-ms-x]");
      if (!x) return;
      e.preventDefault();
      x.closest(".shop-ms-chip").remove(); mark(); later();
    });
    box.querySelector(".shop-ms-box").addEventListener("click", function (e) {
      if (!e.target.closest("[data-ms-x]") && !e.target.closest("[role=option]")) input.focus();
    });
    mark();
  });

  Array.prototype.forEach.call(form.querySelectorAll("input[type=radio]"), function (r) { r.addEventListener("change", later); });
  var mixed = form.querySelector("[data-ms-mixed]");
  if (mixed) mixed.addEventListener("change", now);       // other fields: the page anew at once
  form.addEventListener("submit", function (e) { e.preventDefault(); clearTimeout(timer); found("Подбираем…"); load(); });
})();
