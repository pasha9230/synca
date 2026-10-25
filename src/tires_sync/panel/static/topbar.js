(function () {
  "use strict";
  // The one search of the top bar: the list under the field as one types (search.json), arrows / Enter / Esc; Enter with nothing chosen
  // opens the page of the search. On a phone the field is an icon that opens it across the bar.
  var form = document.getElementById("topsearch"), input = document.getElementById("topsearch-input"),
      pop = document.getElementById("topsearch-list"), opener = document.getElementById("topsearch-open");
  if (!form || !input || !pop) return;
  var DELAY = 250, timer = null, pending = null, shown = "", active = -1;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function links() { return pop.querySelectorAll(".topsearch-item"); }
  function open(on) {
    pop.hidden = !on;
    input.setAttribute("aria-expanded", on ? "true" : "false");
    if (!on) { active = -1; input.removeAttribute("aria-activedescendant"); }
  }
  function mark(n) {
    var items = links();
    if (!items.length) return;
    active = (n + items.length) % items.length;
    for (var i = 0; i < items.length; i++) {
      items[i].classList.toggle("is-active", i === active);
      items[i].setAttribute("aria-selected", i === active ? "true" : "false");
    }
    input.setAttribute("aria-activedescendant", items[active].id);
    items[active].scrollIntoView({block: "nearest"});
  }
  function fill(data) {
    pop.textContent = "";
    active = -1;
    if (!data.groups.length) {
      pop.appendChild(el("div", "topsearch-empty", "Ничего не нашли"));
    }
    var n = 0;
    data.groups.forEach(function (g) {
      var head = el("div", "topsearch-head", g.title);
      if (g.more) {
        var all = el("a", "", "все");
        all.href = g.all_url;
        head.appendChild(all);
      }
      pop.appendChild(head);
      g.items.forEach(function (it) {
        var a = el("a", "topsearch-item");
        a.href = it.url;
        a.id = "topsearch-item-" + (n++);
        a.setAttribute("role", "option");
        a.setAttribute("aria-selected", "false");
        a.tabIndex = -1;
        a.appendChild(el("span", "topsearch-title", it.title));
        if (it.subtitle) a.appendChild(el("span", "topsearch-sub", it.subtitle));
        pop.appendChild(a);
      });
    });
    if (data.groups.length) {
      var page = el("a", "topsearch-all", "Все результаты");
      page.href = "/search?q=" + encodeURIComponent(data.q);
      pop.appendChild(page);
    }
    open(true);
  }
  function ask(q) {
    if (pending) pending.abort();                                     // the answer to an older query must not overwrite a newer one
    pending = typeof AbortController === "function" ? new AbortController() : null;
    fetch("/search.json?q=" + encodeURIComponent(q), {credentials: "same-origin", headers: {"Accept": "application/json"},
                                                       signal: pending ? pending.signal : undefined}).then(function (r) {
      if (!r.ok || r.redirected) throw new Error(r.status);           // an expired login answers with the login page
      return r.json();
    }).then(function (data) {
      if (input.value.trim() !== q) return;
      shown = q;
      fill(data);
    }).catch(function (e) {
      if ((e && e.name === "AbortError") || input.value.trim() !== q) return;
      pop.textContent = "";
      pop.appendChild(el("div", "topsearch-empty", "Поиск не ответил. Нажмите Enter, чтобы открыть страницу поиска."));
      open(true);
    });
  }
  input.addEventListener("input", function () {
    clearTimeout(timer);
    var q = input.value.trim();
    if (q.length < 2 && !/\d/.test(q)) {
      if (pending) pending.abort();
      shown = "";
      open(false);
      return;
    }
    timer = setTimeout(function () { ask(q); }, DELAY);
  });
  input.addEventListener("focus", function () {
    if (shown && shown === input.value.trim() && pop.childNodes.length) open(true);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (pop.hidden) { if (shown && pop.childNodes.length) open(true); else return; }
      e.preventDefault();
      mark(active < 0 ? (e.key === "ArrowDown" ? 0 : -1) : active + (e.key === "ArrowDown" ? 1 : -1));
    } else if (e.key === "Enter") {
      var items = links();
      if (!pop.hidden && active >= 0 && items[active]) {
        e.preventDefault();
        window.location.href = items[active].href;
      } else if (!input.value.trim()) {
        e.preventDefault();
      }
    } else if (e.key === "Escape") {
      if (!pop.hidden) { open(false); e.preventDefault(); }
      else if (form.classList.contains("is-open")) { form.classList.remove("is-open"); input.blur(); }
    }
  });
  document.addEventListener("click", function (e) {
    if (!form.contains(e.target)) {
      open(false);
      if (!input.value.trim()) form.classList.remove("is-open");
    }
  });
  if (opener) {
    opener.addEventListener("click", function () {
      form.classList.add("is-open");
      input.focus();
    });
  }
})();
