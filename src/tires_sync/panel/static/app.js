// Tires Sync: минимальное поведение без фреймворков.
(function () {
  "use strict";
  // Крестик из набора иконок (_ui.html, icon("x", "i-sm")): кнопки «закрыть / убрать», которые строит скрипт
  var X_ICON = '<svg class="i i-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  // 1) Фильтры применяются сразу при выборе
  document.addEventListener("change", function (e) {
    var form = e.target.closest && e.target.closest("form[data-autosubmit]");
    if (form && !e.target.closest("[data-no-autosubmit]")) form.submit();
  });

  // 2) Выбор позиций: «выбрать все» в таблице бренда и нижняя панель массовых действий
  function syncSelection() {
    var rows = document.querySelectorAll("[data-select-row]");
    var checked = 0;
    rows.forEach(function (box) {
      if (box.checked) checked += 1;
      var tr = box.closest("tr");
      if (tr) tr.classList.toggle("is-selected", box.checked);
    });
    document.querySelectorAll("[data-select-all]").forEach(function (all) {
      var table = all.closest("table");
      if (!table) return;
      var boxes = table.querySelectorAll("[data-select-row]");
      var n = Array.prototype.filter.call(boxes, function (b) { return b.checked; }).length;
      all.checked = n > 0 && n === boxes.length;
      all.indeterminate = n > 0 && n < boxes.length;
    });
    var bar = document.getElementById("bulk");
    if (bar) {
      bar.hidden = checked === 0;
      var counter = bar.querySelector("[data-count]");
      if (counter) counter.textContent = checked;
    }
  }
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.matches && t.matches("[data-select-all]")) {
      var table = t.closest("table");
      if (table) table.querySelectorAll("[data-select-row]").forEach(function (b) { b.checked = t.checked; });
      syncSelection();
    } else if (t.matches && t.matches("[data-select-row]")) {
      syncSelection();
    }
  });
  var clearBtn = document.getElementById("bulk-clear");
  if (clearBtn) clearBtn.addEventListener("click", function () {
    document.querySelectorAll("[data-select-row], [data-select-all]").forEach(function (b) { b.checked = false; });
    syncSelection();
  });
  syncSelection();

  // 3) Кнопка доступна только после галочки «Я понимаю»
  document.querySelectorAll("[data-requires]").forEach(function (btn) {
    var ids = btn.dataset.requires.split(",").map(function (s) { return document.getElementById(s.trim()); });
    var sync = function () { btn.disabled = !ids.every(function (c) { return c && c.checked; }); };
    ids.forEach(function (c) { if (c) c.addEventListener("change", sync); });
    btn.addEventListener("resync", sync);
    sync();
  });

  // 4) После отправки формы кнопки блокируются, на нажатой появляется «Применяю…»
  document.addEventListener("submit", function (e) {
    var buttons = e.target.querySelectorAll("button[data-busy]");
    if (!buttons.length) return;
    var clicked = e.submitter;
    setTimeout(function () {
      buttons.forEach(function (b) {
        b.disabled = true;
        if (b === clicked) { b.classList.add("is-busy"); b.textContent = b.dataset.busy; }
      });
    }, 0);
  });

  // После «Назад» браузер возвращает страницу из кеша с заблокированными кнопками
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    var card = document.querySelector("form[data-owin]");                // the card of an order from the cache of «Назад»: read it again, so that
    if (card && !card.querySelector("[data-item-new]")) {                // the positions are fresh (З6); the answer to a POST (…/save with an error)
      var posted = String(location.pathname || "").match(/^\/orders\/(\d+)\/./);    // is opened by its address, never sent again
      if (posted && typeof location.replace === "function") location.replace("/orders/" + posted[1]);
      else if (typeof location.reload === "function") location.reload();
    }
    document.querySelectorAll("button.is-busy").forEach(function (b) { b.classList.remove("is-busy"); });
    document.querySelectorAll("button[data-busy]").forEach(function (b) { b.disabled = false; });
    document.querySelectorAll("[data-requires]").forEach(function (b) { b.dispatchEvent(new Event("resync")); });
    document.querySelectorAll("form[data-owin]").forEach(function (form) {    // the card of an order, sent before «Назад» (data-once): it works again
      if (!form.dataset.sent || form.querySelector("[data-item-new]")) return;  // «Новый заказ» stays blocked: one more press would be one more order
      delete form.dataset.sent;
      form.querySelectorAll("button").forEach(function (b) { b.removeAttribute("aria-busy"); });
      form.querySelectorAll("[data-find-code], [data-find-text], [data-find-price], [data-find-auto]").forEach(function (f) { f.value = ""; });   // the row went with it: not twice
    });
  });

  // 5) Закрытие сообщений
  document.addEventListener("click", function (e) {
    var c = e.target.closest && e.target.closest(".flash .close");
    if (c) c.closest(".flash").remove();
  });

  // 6) Окно предпросмотра объявления
  function closeModals() {
    document.querySelectorAll(".modal-backdrop.is-open").forEach(function (m) { m.classList.remove("is-open"); });
  }
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModals(); });
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t.classList && t.classList.contains("modal-backdrop")) { closeModals(); return; }
    if (t.closest && t.closest("[data-close-modal]")) { closeModals(); return; }
    var rb = t.closest && t.closest("[data-rollback]");
    if (rb) {
      ["rb1", "rb2"].forEach(function (id) { var c = document.getElementById(id); if (c) { c.checked = false; c.dispatchEvent(new Event("change")); } });
      document.getElementById("rb-form").action = "/history/rollback/" + rb.getAttribute("data-rollback");
      document.getElementById("rb-version").textContent = rb.getAttribute("data-rollback");
      document.getElementById("rollback").classList.add("is-open");
      return;
    }
    var opener = t.closest && t.closest("[data-preview]");
    if (!opener) return;
    var body = document.getElementById("pvbody");
    var box = document.getElementById("pv");
    if (!body || !box) return;
    body.innerHTML = '<div class="loading"><span class="spinner"></span>Загружаю…</div>';
    box.classList.add("is-open");
    fetch("/catalog/preview/" + encodeURIComponent(opener.getAttribute("data-preview")), {credentials: "same-origin"})
      .then(function (r) { if (!r.ok || r.redirected) throw new Error("load"); return r.text(); })
      .then(function (html) { body.innerHTML = html; })
      .catch(function () { body.innerHTML = '<p class="muted" style="padding:24px">Не удалось загрузить предпросмотр.</p>'; });
  });

  // 10) Действия на месте: страница не перезагружается, результат виден сразу
  (function () {
    var box = document.getElementById("toasts");
    function toast(text, kind, link) {
      if (!box) return;
      var t = document.createElement("div");
      t.className = "toast toast--" + (kind || "ok");
      var span = document.createElement("span");
      span.textContent = text;
      t.appendChild(span);
      if (link) {
        var a = document.createElement("a");
        a.href = link; a.textContent = "Посмотреть и применить";
        t.appendChild(a);
      }
      var x = document.createElement("button");
      x.type = "button"; x.className = "toast-x"; x.setAttribute("aria-label", "Закрыть"); x.innerHTML = X_ICON;
      x.addEventListener("click", function () { t.remove(); });
      t.appendChild(x);
      box.appendChild(t);
      setTimeout(function () { t.remove(); }, kind === "err" ? 12000 : 7000);
    }
    function setPending(n) {
      document.querySelectorAll("[data-pend-count]").forEach(function (el) {
        el.textContent = n;
        if (el.id === "navpend") el.hidden = !n;
      });
      document.querySelectorAll("[data-pend-title]").forEach(function (el) { el.textContent = "Неподтверждённых изменений: " + n; });
      var bar = document.getElementById("pendbar");
      if (bar) bar.hidden = !n;
      var apply = document.querySelector("[data-pending-apply]");
      if (apply && !n) apply.disabled = true;                         // nothing to apply: the draft of the form is gone
      if (n && window.tiresPending) window.tiresPending();             // the bar of the settings pages: the line and the draft anew
    }
    function swap(old, url) {
      return fetch(url, {credentials: "same-origin"}).then(function (r) {
        if (!r.ok || r.redirected) throw new Error("load");
        return r.text();
      }).then(function (html) {
        old.innerHTML = html.trim();
        syncSelection();
        document.dispatchEvent(new Event("tires:table-loaded"));
      });
    }
    function targets() {
      var items = document.getElementById("items");
      return items ? [items] : [];
    }
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (!form.hasAttribute || !form.hasAttribute("data-ajax")) return;
      if (e.submitter && e.submitter.hasAttribute("formaction")) return;   // a button that goes to its own page (not an in-place action)
      e.preventDefault();
      var submitter = e.submitter;
      var data = new FormData(form);
      if (submitter && submitter.name) data.append(submitter.name, submitter.value);
      var affected = targets();
      var wasBulk = form.dataset.scope === "bulk";
      fetch(form.action, {method: "POST", body: data, credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
        .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); })
        .then(function (res) {
          if (res.staged !== undefined) setPending(res.staged);
          toast(res.message, res.ok ? "ok" : "err", res.ok && res.staged ? res.review_url : "");
          if (!res.ok) return;
          if (wasBulk) {
            document.querySelectorAll("[data-select-row], [data-select-all]").forEach(function (b) { b.checked = false; });
            syncSelection();
          }
          return Promise.all(affected.map(function (el) {
            return swap(el, el.dataset.url);
          })).catch(function () { toast("Изменение сохранено, но строку не удалось обновить: обновите страницу.", "err"); });
        })
        .catch(function () { toast("Не удалось выполнить действие: проверьте соединение и повторите.", "err"); });
    });
  })();

  // 7) Кнопка «Скопировать»: берёт значение поля, на которое указывает data-copy
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-copy]");
    if (!btn) return;
    var field = document.querySelector(btn.getAttribute("data-copy"));
    if (!field) return;
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    var done = function () {
      btn.textContent = "Скопировано";
      clearTimeout(btn._t);
      btn._t = setTimeout(function () { btn.textContent = btn.dataset.label; }, 2000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(field.value).then(done, function () { field.select(); });
    } else {
      field.select();
    }
  });

  // 8) Сворачивание бокового меню (состояние запоминается)
  (function () {
    var app = document.querySelector(".app");
    var logo = app && app.querySelector(".sb-logo");
    if (!logo) return;
    app.querySelectorAll(".sb-link").forEach(function (a) { if (!a.title) a.title = a.textContent.replace(/\d+/g, "").trim(); });
    var btn = document.createElement("button");
    btn.type = "button"; btn.className = "sb-toggle";
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M15 10l-2 2 2 2"/></svg>';
    logo.appendChild(btn);
    var store = function (key, value) { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (err) { return null; } };
    var set = function (c) {
      app.classList.toggle("is-collapsed", c);
      btn.title = c ? "Развернуть меню" : "Свернуть меню";
      btn.setAttribute("aria-label", btn.title);
      btn.setAttribute("aria-expanded", String(!c));
    };
    set(store("ts-sb-collapsed") === "1");
    btn.addEventListener("click", function () {
      var c = !app.classList.contains("is-collapsed");
      set(c); store("ts-sb-collapsed", c ? "1" : "0");
    });
  })();

  // 9) Всплывающая расшифровка: [data-tip-title] + [data-tip]
  (function () {
    var pop = document.createElement("div");
    pop.className = "tip-pop"; pop.setAttribute("role", "tooltip"); document.body.appendChild(pop);
    var show = function (el) {
      pop.innerHTML = "";
      var b = document.createElement("b"); b.textContent = el.dataset.tipTitle || "";
      pop.append(b, el.dataset.tip || "");
      var r = el.getBoundingClientRect(), w = pop.offsetWidth, ph = pop.offsetHeight;
      var x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8), y = r.top - ph - 8;
      if (y < 8) y = r.bottom + 8;
      pop.style.left = x + "px"; pop.style.top = y + "px"; pop.classList.add("is-on");
    };
    var hide = function () { pop.classList.remove("is-on"); };
    var find = function (e) { return e.target.closest && e.target.closest("[data-tip]"); };
    document.addEventListener("mouseover", function (e) { var el = find(e); if (el) show(el); else hide(); });
    document.addEventListener("focusin", function (e) { var el = find(e); if (el) show(el); });
    document.addEventListener("focusout", hide);
    document.addEventListener("scroll", hide, true);
  })();
  // 5) Список брендов исключений: поиск и счётчик отмеченных
  (function () {
    document.addEventListener("input", function (e) {
      var box = e.target;
      if (!box.matches || !box.matches("[data-brand-filter]")) return;
      var table = document.getElementById(box.getAttribute("data-brand-filter"));
      var q = box.value.trim().toLowerCase();
      if (table) table.querySelectorAll("tbody tr[data-name]").forEach(function (tr) { tr.hidden = q !== "" && tr.getAttribute("data-name").indexOf(q) === -1; });
    });
    document.addEventListener("change", function (e) {
      var box = e.target;
      if (!box.matches || !box.matches("input[name=excluded]")) return;
      var table = box.closest("table");
      var out = table && document.querySelector("[data-brand-count=" + table.id + "]");
      if (out) out.textContent = table.querySelectorAll("input[name=excluded]:checked").length;
    });
  })();
  // 5a) Ошибка формы под полем: сервер называет поле (flash с категорией «field:<имя>»), сообщение переезжает под это поле, поле подсвечено
  //     и в фокусе. Поля нет на странице — сообщение остаётся вверху, как было.
  document.querySelectorAll("[data-field-error]").forEach(function (note) {
    var parts = note.getAttribute("data-field-error").split("@"), name = parts[0], scope = parts[1], field = null;
    // «имя@N»: the field of the form whose hidden «id» is N (a page of several forms, as the points); «имя@new»: the form without an id
    var forms = scope ? Array.prototype.filter.call(document.querySelectorAll("form"), function (f) {
      var id = f.querySelector("input[name=id]");
      return scope === "new" ? !(id && id.value) : !!(id && id.value === scope);
    }) : [document];
    forms.forEach(function (root) {
      root.querySelectorAll("[name]").forEach(function (el) { if (!field && el.name === name && el.type !== "hidden") field = el; });
    });
    if (!field) return;
    var folded = field.closest("details");                   // a field in a folded block («Дополнительно»): open it, or the message would be lost
    if (folded) folded.open = true;
    var hint = document.createElement("p"), id = "err-" + name;
    hint.className = "hint hint--err"; hint.id = id; hint.setAttribute("role", "alert");
    hint.textContent = note.querySelector(".flash-text") ? note.querySelector(".flash-text").textContent : note.textContent;
    field.classList.add("is-err"); field.setAttribute("aria-invalid", "true"); field.setAttribute("aria-describedby", id);
    var box = field.closest(".field");                        // inside the field's own block: a grid of fields keeps its cells
    if (box) box.appendChild(hint); else field.parentNode.insertBefore(hint, field.nextSibling);
    note.remove();
    field.addEventListener("input", function () { field.classList.remove("is-err"); field.removeAttribute("aria-invalid"); }, {once: true});
    if (field.scrollIntoView) field.scrollIntoView({block: "center"});
    field.focus();
  });
  // 5b) Форма с data-confirm спрашивает подтверждение окном панели (не системным окном браузера): заголовок (data-confirm-title), что
  //     произойдёт (data-confirm), кнопка действия (data-confirm-ok; data-confirm-danger — красная). «Отмена», Esc, щелчок мимо — ничего не уходит.
  //     Слушатель на перехвате: срабатывает раньше data-once, иначе кнопка погасла бы и после отказа; после «Да» форма уходит обычным путём.
  var askWin = null;
  function askConfirm(form, submitter) {
    if (!askWin) {
      askWin = document.createElement("div");
      askWin.className = "modal-backdrop confirm-window"; askWin.id = "confirm-window";
      askWin.setAttribute("role", "dialog"); askWin.setAttribute("aria-modal", "true"); askWin.setAttribute("aria-labelledby", "confirm-window-h");
      askWin.innerHTML = '<div class="modal confirm-modal"><div class="modal-head"><h2 id="confirm-window-h"></h2></div>' +
        '<div class="modal-body"><p class="confirm-text"></p></div><div class="modal-foot"><button type="button" class="btn btn--secondary" data-confirm-cancel>Отмена</button>' +
        '<button type="button" class="btn" data-confirm-go></button></div></div>';
      document.body.appendChild(askWin);
    }
    var go = askWin.querySelector("[data-confirm-go]"), cancel = askWin.querySelector("[data-confirm-cancel]"), danger = form.hasAttribute("data-confirm-danger");
    var back = submitter || form.querySelector("button");
    cancel.onclick = function () { askWin.classList.remove("is-open"); if (back) back.focus(); };   // this window only, the focus back where it was
    askWin.querySelector("h2").textContent = form.getAttribute("data-confirm-title") || "Подтвердите действие";
    askWin.querySelector(".confirm-text").textContent = form.getAttribute("data-confirm");
    go.textContent = form.getAttribute("data-confirm-ok") || "Да, выполнить";
    go.className = "btn " + (danger ? "btn--danger" : "btn--primary");
    go.onclick = function () {
      askWin.classList.remove("is-open");
      form.dataset.confirmed = "1";
      if (form.requestSubmit) form.requestSubmit(submitter && submitter.form === form ? submitter : undefined); else form.submit();
      setTimeout(function () { delete form.dataset.confirmed; }, 0);       // a send stopped on the way (a check of the browser) asks again next time
    };
    askWin.classList.add("is-open");
    (danger ? cancel : go).focus();                                         // what cannot be undone: the keyboard starts on «Отмена»
  }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches || !form.matches("form[data-confirm]")) return;
    if (form.dataset.confirmed) { delete form.dataset.confirmed; return; }       // the press after «Да»: goes on to data-once and the server
    e.preventDefault(); e.stopPropagation();
    askConfirm(form, e.submitter);
  }, true);

  // 5e) «Заказ поставщику» (_order_supplier.html): строки сверки появляются по очереди — «проверяется…», затем ✓ / ! / ✗ (итог посчитан
  //     сервером, без JS он виден сразу; при «меньше движения» — тоже сразу). «Подтвердить» показывает «Оформляем…» до ответа страницы.
  document.querySelectorAll("[data-supplier-check]").forEach(function (list) {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var lines = list.querySelectorAll(".supplier-line"), actions = document.querySelector("[data-supplier-actions]");
    lines.forEach(function (li) { li.classList.add("is-checking"); });
    if (actions) actions.classList.add("is-checking");
    lines.forEach(function (li, n) {
      setTimeout(function () {
        li.classList.remove("is-checking");
        if (n === lines.length - 1 && actions) actions.classList.remove("is-checking");
      }, 250 * (n + 1));
    });
  });
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches || !form.matches("form[data-supplier-go]") || form.dataset.sent) return;
    var busy = form.parentNode.querySelector("[data-supplier-busy]");
    if (busy) { busy.hidden = false; form.hidden = true; }
  });

  // 6) Форма с data-once отправляется один раз: кнопка гаснет после нажатия
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches || !form.matches("form[data-once]")) return;
    if (form.dataset.sent) { e.preventDefault(); return; }
    form.dataset.sent = "1";
    form.querySelectorAll("button").forEach(function (b) { b.setAttribute("aria-busy", "true"); });
  });

  // 7) Фото в чат: браузер уменьшает снимок до 1600 px (JPEG) до отправки, иначе фото с телефона не пройдёт по размеру запроса (1 МБ).
  //    Форма ответа одна: текст уходит вместе с фото тем же запросом (сервер шлёт сначала фото, потом текст); без фото форма уходит как обычно.
  //    Если браузер фото не открыл (например, HEIC не в Safari), небольшой файл уходит как есть, большой не отправляется.
  var PHOTO_SIDE = 1600, PHOTO_RAW_MAX = 900 * 1024;
  var photoNote = function (form, text) {
    var note = form.querySelector("[data-photo-note]");
    if (note) { note.textContent = text; note.classList.add("text-err"); }
    clearTimeout(form.sendWatch); form.sendWatch = null;                 // the send is over: the watch of part 7d must not overwrite this note
    delete form.dataset.sent;
    form.querySelectorAll("button").forEach(function (b) { b.removeAttribute("aria-busy"); });
  };
  var photoShrink = function (file) {
    return new Promise(function (resolve, reject) {
      // the photo is read as a data: address: the policy of the panel allows images from data: but not from blob:, so a blob: picture never opened
      var reader = new FileReader(), img = new Image();
      img.onload = function () {
        var scale = Math.min(1, PHOTO_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
        var canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
        canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
        var ctx = canvas.getContext("2d");
        ctx.fillStyle = "#fff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        var encode = function (quality) {                                // a very detailed photo is encoded again, coarser, until it fits the request
          canvas.toBlob(function (blob) {
            if (!blob) { reject(new Error("toBlob")); return; }
            if (blob.size > PHOTO_RAW_MAX && quality > 0.5) { encode(quality - 0.15); return; }
            resolve(blob);
          }, "image/jpeg", quality);
        };
        encode(0.85);
      };
      img.onerror = function () { reject(new Error("decode")); };
      reader.onload = function () { img.src = reader.result; };
      reader.onerror = function () { reject(new Error("read")); };
      reader.readAsDataURL(file);
    });
  };
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form.matches || !form.matches("form[data-photo-form]") || !window.fetch || !window.FormData) return;     // a second press (part 6 stopped it)
    var input = form.querySelector("input[type=file]");
    var file = input && input.files && input.files[0];
    if (!file) return;
    e.preventDefault();
    var send = function (blob, name) {
      var data = new FormData(form);
      data.set("photo", blob, name);
      // The answer is a redirect to the chat: it is not followed here, or that hidden page would take the message about the send
      // (also «не отправляйте повторно») and the manager would see a page without it.
      return fetch(form.action, { method: "POST", body: data, credentials: "same-origin", redirect: "manual" }).then(function (r) {
        if (r.status === 413) { photoNote(form, "Файл слишком большой для отправки: выберите другое фото."); return; }
        if (r.type !== "opaqueredirect") { photoNote(form, "Панель не приняла фото (код " + r.status + "): обновите страницу."); return; }
        var query = new URLSearchParams();
        ["f", "acc"].forEach(function (k) { var v = form.elements[k] && form.elements[k].value; if (v) query.set(k, v); });
        var qs = query.toString();
        window.location.href = form.action.replace(/\/send(-image)?$/, "") + (qs ? "?" + qs : "");       // the chat page shows the result (or asks to log in)
      });
    };
    photoShrink(file).then(function (blob) { return send(blob, "photo.jpg"); }, function () {
      if (file.size > PHOTO_RAW_MAX) { photoNote(form, "Браузер не смог открыть это фото (например, HEIC). Сохраните его как JPEG или отправьте из Safari."); return; }
      return send(file, file.name || "photo");
    }).catch(function () { photoNote(form, "Нет связи с панелью: фото не отправлено, попробуйте ещё раз."); });
  });

  // 7b) Фото в чат: после выбора видно имя файла (уйдёт по кнопке «Отправить» вместе с текстом), «×» убирает выбранное фото, текст не трогает
  var photoChosen = function (form, file) {
    var note = form.querySelector("[data-photo-note]"), clear = form.querySelector("[data-photo-clear]");
    if (note) {
      if (note.dataset.idle === undefined) note.dataset.idle = note.textContent;
      note.textContent = file ? "Фото: " + file.name + " (уйдёт первым, до текста)." : note.dataset.idle;
      note.classList.remove("text-err");
    }
    if (clear) clear.hidden = !file;
  };
  document.addEventListener("change", function (e) {
    var input = e.target, form = input.closest && input.closest("form[data-photo-form]");
    if (!form || input.type !== "file") return;
    photoChosen(form, input.files && input.files[0]);
  });
  document.addEventListener("click", function (e) {
    var button = e.target.closest && e.target.closest("[data-photo-clear]"), form = button && button.closest("form[data-photo-form]");
    if (!form || form.dataset.sent) return;                              // a send has begun: the photo is already on its way, «×» would only hide it
    var input = form.querySelector("input[type=file]");
    if (input) input.value = "";
    photoChosen(form, null);
    if (input) input.focus();                                            // «×» hides itself: the keyboard stays on the photo button
  });

  // 7d) Ответ в чат: «Отправить» не зависает. После «Назад» браузер возвращает страницу из кеша с формой «уже отправлена» (часть 6),
  //     и кнопка молча не нажималась; если панель не ответила за 45 с — тоже. Кнопка снова доступна, подсказка просит проверить чат.
  //     Повторное нажатие безопасно: метка отправки та же, сервер не отправит сообщение второй раз.
  var CHAT_SEND_WAIT = 45000;
  var chatSendAgain = function (form, text) {
    clearTimeout(form.sendWatch); form.sendWatch = null;
    delete form.dataset.sent;
    form.querySelectorAll("button").forEach(function (b) { b.removeAttribute("aria-busy"); b.disabled = false; });
    var note = form.querySelector("[data-photo-note]");
    if (note) { note.textContent = text; note.classList.add("text-err"); }
  };
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches || !form.matches("form[data-photo-form]") || !form.dataset.sent || form.sendWatch) return;    // a blocked second press keeps the first watch
    form.sendWatch = setTimeout(function () {
      if (form.dataset.sent) chatSendAgain(form, "Панель долго не отвечает: обновите страницу и проверьте чат, прежде чем отправлять ещё раз.");
    }, CHAT_SEND_WAIT);
  });
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-photo-form]").forEach(function (form) {
      if (form.dataset.sent) chatSendAgain(form, "Страница открыта из истории браузера: обновите её, чтобы увидеть, ушло ли сообщение.");
    });
  });

  // 7c) Меню «Ещё» (details.menu): закрывается щелчком мимо и клавишей Esc
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.menu[open]").forEach(function (m) { if (!m.contains(e.target)) m.removeAttribute("open"); });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll("details.menu[open]").forEach(function (m) { m.removeAttribute("open"); });
  });

  // 8) Каталог: скрыть цену поставщика на экране (например, при показе клиенту). Только вид, данные не меняются.
  var SP_KEY = "tires.hideSupplierPrice";
  var spStore = function (value) {
    try { if (value === undefined) return localStorage.getItem(SP_KEY); localStorage.setItem(SP_KEY, value); } catch (err) { return null; }
  };
  var spApply = function () {
    var hidden = spStore() === "1";
    document.body.classList.toggle("hide-sp", hidden);
    document.querySelectorAll("[data-toggle-sp]").forEach(function (b) {
      b.setAttribute("aria-pressed", hidden ? "true" : "false");
      var label = b.querySelector("[data-sp-label]");
      if (label) label.textContent = hidden ? "Показать цену поставщика" : "Скрыть цену поставщика";
    });
  };
  document.addEventListener("click", function (e) {
    if (!e.target.closest || !e.target.closest("[data-toggle-sp]")) return;
    spStore(spStore() === "1" ? "0" : "1");
    spApply();
  });
  spApply();
  document.addEventListener("tires:table-loaded", spApply);
  // 9) Заказы: перетаскивание карточек между колонками доски
  (function () {
    var board = document.getElementById("board");
    if (!board) return;
    var dragged = null;
    var say = function (text) {
      var box = document.getElementById("board-msg");
      if (!box) return;
      box.querySelector(".flash-text").textContent = text;
      box.hidden = false;
    };
    // a closed stage is folded into a narrow column: its head unfolds it and folds it back
    board.addEventListener("click", function (e) {
      var head = e.target.closest && e.target.closest("[data-fold-toggle]");
      if (!head) return;
      var col = head.closest(".col"), open = col.classList.toggle("is-folded") === false;
      head.setAttribute("aria-expanded", open ? "true" : "false");
    });
    board.addEventListener("dragstart", function (e) {
      var card = e.target.closest && e.target.closest('.ocard[draggable="true"]');     // false: no right to move orders
      if (!card) return;
      dragged = card; card.classList.add("is-drag");
      e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", card.dataset.card);
    });
    board.addEventListener("dragend", function () {
      if (dragged) dragged.classList.remove("is-drag");
      dragged = null;
      board.querySelectorAll(".col.is-over").forEach(function (c) { c.classList.remove("is-over"); });
    });
    board.addEventListener("dragover", function (e) {
      var col = e.target.closest && e.target.closest(".col");
      if (!col || !dragged) return;
      e.preventDefault();
      board.querySelectorAll(".col.is-over").forEach(function (c) { if (c !== col) c.classList.remove("is-over"); });
      col.classList.add("is-over");
    });
    board.addEventListener("drop", function (e) {
      var col = e.target.closest && e.target.closest(".col");
      if (!col || !dragged) return;
      e.preventDefault();
      var card = dragged;
      if (card.closest(".col") === col) return;
      var data = new FormData();
      data.append("csrf", board.dataset.csrf); data.append("stage_id", col.dataset.stage);
      fetch("/orders/" + card.dataset.card + "/move", {method: "POST", body: data, credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (!res.ok && res.go) { window.location.href = res.go; return; }     // «Шины остаются на точке?»: the card asks (2026-10-29)
          if (!res.ok) { say(res.message); return; }
          if (res.send && window.tiresSendWindow) { window.tiresSendWindow(res.send, board.dataset.csrf); return; }   // the board reloads when it closes
          window.location.reload();
        })
        .catch(function () { say("Не удалось перенести заказ: проверьте соединение."); });
    });
  })();
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-copy]");
    if (!btn) return;
    var el = document.getElementById(btn.dataset.copy);
    if (!el) return;
    el.select();
    if (navigator.clipboard) { navigator.clipboard.writeText(el.value).then(function () { btn.textContent = "Скопировано"; }); }
    else { document.execCommand("copy"); btn.textContent = "Скопировано"; }
  });
  document.querySelectorAll("form.rule-form").forEach(function (form) {
    var trigger = form.querySelector("[name=trigger]"), action = form.querySelector("[name=action]");
    var labels = {remind: "Текст напоминания", tag_add: "Тег (одно слово или фраза)", tag_remove: "Тег (одно слово или фраза)"};
    function sync() {
      form.querySelectorAll("[data-trigger]").forEach(function (el) { el.hidden = el.dataset.trigger !== trigger.value; });
      form.querySelectorAll("[data-action]").forEach(function (el) { el.hidden = el.dataset.action.split(" ").indexOf(action.value) < 0; });
      var label = form.querySelector("[data-text-label]");
      if (label && labels[action.value]) { label.textContent = labels[action.value]; }
    }
    trigger.addEventListener("change", sync); action.addEventListener("change", sync); sync();
  });
  (function () {                                    // «Чаты»: the list keeps its place (owner's word 2026-10-24: a chat picked low in the list, a reload, and the list was at the top again)
    var list = document.querySelector(".chats-list"), live = document.getElementById("chats-live");
    if (!list || !live) return;
    var KEY = "chats-list-place", place = live.dataset.place || "";    // the list as the server reads it: state, account, search (not the address as typed)
    function active() { return list.querySelector(".chat-row.is-active"); }
    function reveal() {                              // the open chat in sight, moving the list only (never the page)
      var row = active();
      if (!row) return;
      var box = list.getBoundingClientRect(), at = row.getBoundingClientRect();
      if (at.top < box.top || at.bottom > box.bottom) list.scrollTop += at.top - box.top - Math.max(0, (list.clientHeight - row.offsetHeight) / 2);
    }
    function places() {
      try { var all = JSON.parse(sessionStorage.getItem(KEY) || "{}"); return all && typeof all === "object" ? all : {}; } catch (e) { return {}; }
    }
    var mark = places()[place];                     // a click on a chat, a form of the chat, F5: the page is loaded again, the list comes back where it was
    if (typeof mark === "number") list.scrollTop = mark;
    reveal();
    window.addEventListener("pagehide", function () {    // each list (state, account, search) keeps its own place in this tab
      var all = places();
      if (!(place in all) && Object.keys(all).length >= 20) all = {};
      all[place] = list.scrollTop;
      try { sessionStorage.setItem(KEY, JSON.stringify(all)); } catch (e) { /* private mode: the list starts at the top */ }
    });
    function after(bar) {                            // what follows the filter row: the rows of the chats or the empty note
      var nodes = [];
      for (var n = bar ? bar.nextSibling : null; n; n = n.nextSibling) nodes.push(n);
      return nodes;
    }
    document.addEventListener("click", function (e) {    // «Обновить список»: the rows are drawn again in place, the open chat stays where it is on the screen
      var link = e.target.closest && e.target.closest("[data-chats-relist]");
      if (!link || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
      e.preventDefault();
      var row = active(), offset = row ? row.getBoundingClientRect().top - list.getBoundingClientRect().top : null, top = list.scrollTop;
      fetch(window.location.href, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
        .then(function (r) { if (!r.ok || r.redirected) throw new Error("page"); return r.text(); })
        .then(function (html) {
          var fresh = new DOMParser().parseFromString(html, "text/html").querySelector(".chats-list");
          var bar = list.querySelector(".chats-filter"), freshBar = fresh && fresh.querySelector(".chats-filter");
          if (!bar || !freshBar) throw new Error("page");
          after(bar).forEach(function (n) { list.removeChild(n); });     // the search typed and an open filter menu stay as they are
          after(freshBar).forEach(function (n) { list.appendChild(document.adoptNode(n)); });
          var count = bar.querySelector(".ml-auto"), freshCount = freshBar.querySelector(".ml-auto");
          if (count && freshCount) count.innerHTML = freshCount.innerHTML;
          list.scrollTop = top;
          var now = active();
          if (now && offset !== null) list.scrollTop += now.getBoundingClientRect().top - list.getBoundingClientRect().top - offset;
          var note = document.getElementById("chats-order");
          if (note) note.hidden = true;
          var off = document.getElementById("chats-offline");   // the list is fresh again: no «Нет связи» left from an earlier miss
          if (off) { off.hidden = true; delete off.dataset.stale; }
        })
        .catch(function () {                         // the page never reloads itself: «Нет связи» says what to do
          var bar = document.getElementById("chats-offline");
          if (bar) { bar.hidden = false; bar.dataset.stale = "1"; }   // the list is old: the next good poll reads the page before it hides the bar
        });
    });
  })();
  (function () {                                    // «Чаты»: the order column folds away and comes back; the choice is remembered on this computer
    var layout = document.querySelector(".chats-layout");
    if (!layout) return;
    var folded = false;
    try { folded = !!localStorage.getItem("chats-deal-folded"); } catch (e) { folded = false; }
    function show() {                                 // the buttons are found again each time: the live update below redraws them
      var fold = document.querySelector("[data-deal-fold]"), unfold = document.querySelector("[data-deal-unfold]");
      layout.classList.toggle("is-deal-folded", folded);
      if (fold) fold.setAttribute("aria-expanded", folded ? "false" : "true");
      if (unfold) unfold.hidden = !folded;
    }
    function set(value) {
      folded = value;
      try { localStorage.setItem("chats-deal-folded", folded ? "1" : ""); } catch (e) { /* private mode: only this page */ }
      show();
    }
    show();
    document.addEventListener("click", function (e) {
      var fold = e.target.closest && e.target.closest("[data-deal-fold]"), unfold = e.target.closest && e.target.closest("[data-deal-unfold]");
      if (fold) { set(true); var u = document.querySelector("[data-deal-unfold]"); if (u) u.focus(); }
      if (unfold) { set(false); var f = document.querySelector("[data-deal-fold]"); if (f) f.focus(); }
    });
    document.addEventListener("chats:refilled", show);
  })();
  (function () {                                    // «Чаты»: an open page asks for fresh messages and updates itself in place (no reload, no jumping)
    var box = document.getElementById("chats-live");
    if (!box) return;
    var sig = box.dataset.sig, busy = false, misses = 0;
    function offline(on) {                           // two misses in a row (a cut connection, an expired login): say so, the list is not live
      var bar = document.getElementById("chats-offline");
      if (!bar) return;
      bar.hidden = !on;
      if (!on) delete bar.dataset.stale;
    }
    function stale() { var bar = document.getElementById("chats-offline"); return !!(bar && bar.dataset.stale); }
    function hrefs(root) { return Array.prototype.map.call(root.querySelectorAll(".chats-list .chat-row"), function (a) { return a.getAttribute("href"); }); }
    function refill(doc) {
      // rows that are on the page get their text, time and dot refreshed where they are; the order of the list and its scroll stay as the person left them
      var rows = doc.querySelectorAll(".chats-list .chat-row");
      Array.prototype.forEach.call(rows, function (fresh) {
        var want = fresh.getAttribute("href"), mine = null;
        Array.prototype.forEach.call(document.querySelectorAll(".chats-list .chat-row"), function (row) { if (row.getAttribute("href") === want) mine = row; });
        if (mine) { mine.innerHTML = fresh.innerHTML; mine.className = fresh.className; }
      });
      var note = document.getElementById("chats-order");
      if (note) note.hidden = hrefs(document).join("|") === hrefs(doc).join("|");
      var thread = document.querySelector(".chat-thread"), fresh = doc.querySelector(".chat-thread");
      if (thread && fresh && thread.innerHTML !== fresh.innerHTML) {
        var atEnd = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 40, top = thread.scrollTop;
        thread.innerHTML = fresh.innerHTML;
        thread.scrollTop = atEnd ? thread.scrollHeight : top;
      }
      [".chats-main .card-head", ".chats-deal"].forEach(function (sel) {      // the head and the deal block follow too, unless a hand is in them
        var mine = document.querySelector(sel), fresh = doc.querySelector(sel);
        if (!(mine && fresh && mine.innerHTML !== fresh.innerHTML && !mine.contains(document.activeElement))) return;
        var picker = mine.querySelector(".chat-pick");             // «Подобрать позицию» stays as it is (what is typed, its list of suggestions)
        mine.innerHTML = fresh.innerHTML;
        var slot = mine.querySelector(".chat-pick");
        if (picker && slot) {
          var order = slot.querySelector("[name=order_id]"), kept = picker.querySelector("[name=order_id]");
          if (order && kept) kept.value = order.value;                    // the order of the chat as it is now: the server checks it
          slot.parentNode.replaceChild(picker, slot);
        }
      });
      document.dispatchEvent(new Event("chats:refilled"));
      var live = doc.getElementById("chats-live");
      if (live) sig = box.dataset.sig = live.dataset.sig;
    }
    function ask() {
      if (busy || document.hidden) return;
      busy = true;
      var data = new FormData();
      data.append("csrf", box.dataset.csrf); data.append("chat", box.dataset.chat || "");
      fetch("/chats/refresh", {method: "POST", body: data, credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
        .then(function (r) { if (!r.ok || r.redirected) throw new Error("refresh"); return r.json(); })
        .then(function (res) {                      // the bar hides only when the page is as fresh as the server: nothing new, or read and refilled
          if (!(res.sig && res.sig !== sig) && !stale()) { misses = 0; offline(false); return null; }
          return fetch(window.location.href, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
            .then(function (r) { if (!r.ok || r.redirected) throw new Error("page"); return r.text(); })
            .then(function (html) {
              misses = 0;                            // the connection is there: a page it cannot read is no «Нет связи»
              try { refill(new DOMParser().parseFromString(html, "text/html")); }
              catch (err) { console.error("chats refill", err); return; }    // the bar (and its «stale») stays: the next poll tries again
              offline(false);
            });
        })
        .catch(function () { misses += 1; if (misses >= 2) offline(true); })
        .then(function () { busy = false; });
    }
    window.setInterval(ask, 15000);
  })();
  // 11) Заказ: поиск позиции по коду или названию. Подсказки с остатком и ценой объявления; выбор подставляет код и цену (её можно поменять).
  document.querySelectorAll("[data-item-find]").forEach(function (wrap) {
    var input = wrap.querySelector("[data-find-text]"), code = wrap.querySelector("[data-find-code]"), list = wrap.querySelector(".suggest"), row = wrap.closest("[data-item-row]");
    var price = row && row.querySelector("[data-find-price]"), timer = null, asked = "", items = [], active = -1, autoPrice = "";   // the price of this row, not of the page
    var auto = row && row.querySelector("[data-find-auto]");      // the price the list put in the field goes with it: kept as it is, the server prices it again
    function remember() { if (auto) auto.value = autoPrice; }      // by the account of the window when it is saved
    if (auto && price && auto.value && price.value === auto.value) autoPrice = auto.value;      // a window back with an error, or after «Назад»
    function close() { list.hidden = true; active = -1; }
    function mark() {
      Array.prototype.forEach.call(list.children, function (li, i) { li.setAttribute("aria-selected", i === active ? "true" : "false"); });
    }
    function deal() {                                               // the deal of the same window: a deal of the point's stock is offered «шина с точки» too
      var form = wrap.closest("form"), kind = form && form.querySelector('select[name="deal_kind"]');
      return kind && kind.value === "stock" ? "stock" : "";
    }
    function pick(i) {
      var it = items[i];
      if (!it) return;
      if (it.point) {                                               // the price is the manager's: none in the panel, and not the one an earlier pick brought
        input.value = asked = it.name; code.value = it.goods_id;
        if (price && autoPrice && price.value === autoPrice) price.value = "";
        autoPrice = ""; remember(); close(); if (price) price.focus(); return;
      }
      input.value = asked = it.name + " · " + it.size; code.value = it.goods_id;
      if (price && it.price != null && (price.value === "" || price.value === autoPrice)) { price.value = autoPrice = String(Math.round(it.price)); }
      remember(); close();
      if (price) price.focus();
    }
    function show(found) {
      items = found; active = -1; list.innerHTML = "";
      if (!found.length) {
        var none = document.createElement("li"); none.className = "s-empty"; none.textContent = "Нет позиций с остатком по этому запросу";
        list.appendChild(none);
      }
      found.forEach(function (it, i) {
        var li = document.createElement("li"), name = document.createElement("span"), meta = document.createElement("span"), cost = document.createElement("span");
        li.setAttribute("role", "option");
        name.className = "s-name"; name.textContent = it.point ? "«" + it.name + "»" : it.name;
        meta.className = "s-meta"; meta.textContent = it.point ? "шина с точки, нет в выгрузке WebMIM" : it.size + " · остаток " + it.stock + " · код " + it.goods_id + (it.oth ? " · " + it.oth : "");
        cost.className = "s-price"; cost.textContent = it.point ? "впишите цену" : (it.price != null ? Math.round(it.price).toLocaleString("ru-RU") + " ₽" : "нет цены");
        li.appendChild(name); li.appendChild(meta); li.appendChild(cost);
        li.addEventListener("mousedown", function (e) { e.preventDefault(); pick(i); });
        list.appendChild(li);
      });
      list.hidden = false;
    }
    function address(q, kind) {                                     // the account of the window prices the list (the order, the chat; a new
      var url = wrap.getAttribute("data-item-find"), form = wrap.closest("form"), more = "";     // window: its «Чат Авито» or its point)
      if (url.indexOf("?") < 0 && form) {
        ["avito_chat", "point_id"].forEach(function (name) {
          var f = form.querySelector('[name="' + name + '"]');
          if (f && f.value.trim()) more += "&" + name + "=" + encodeURIComponent(f.value.trim());
        });
      }
      return url + (url.indexOf("?") < 0 ? "?" : "&") + "q=" + encodeURIComponent(q) + (kind ? "&deal=" + kind : "") + more;
    }
    function ask() {
      var q = input.value.replace(/·/g, " ").trim(), kind = deal(), key = address(q, kind);     // another point or chat asks again
      if (q.length < 2) { close(); return; }
      if (key === asked) { list.hidden = false; return; }
      asked = key;
      fetch(key, {credentials: "same-origin"})
        .then(function (r) { if (!r.ok || r.redirected) throw new Error("find"); return r.json(); })
        .then(function (res) { if (asked === key && input.value.replace(/·/g, " ").trim() === q) show(res.items || []); })
        .catch(function () {                         // say it instead of an empty list: the manager would think there is nothing
          if (input.value.replace(/·/g, " ").trim() !== q) { asked = ""; return; }      // an old request: the field asks something else now
          asked = ""; items = []; list.innerHTML = "";
          var miss = document.createElement("li"); miss.className = "s-empty"; miss.textContent = "Нет связи с панелью: подсказки не загрузились, попробуйте ещё раз";
          list.appendChild(miss); list.hidden = false;
        });
    }
    input.addEventListener("input", function () {
      code.value = "";                                              // the text changed: the chosen position and the price it brought are no longer meant
      if (price && autoPrice && price.value === autoPrice) price.value = "";
      autoPrice = ""; remember();
      clearTimeout(timer); timer = setTimeout(ask, 250); });
    input.addEventListener("focus", function () { if (input.value.trim().length >= 2) ask(); });
    input.addEventListener("blur", function () { setTimeout(close, 150); });
    input.addEventListener("keydown", function (e) {
      if (list.hidden || !items.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active = (active + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
        mark();
      } else if (e.key === "Enter" && active >= 0) {
        e.preventDefault(); pick(active);
      } else if (e.key === "Escape") {
        close();
      }
    });
  });
  // 13) Окно заказа: теги плашками (Enter, запятая или уход с поля добавляют, ✕ убирает; уходят строкой через запятую вместе с окном)
  document.querySelectorAll("[data-tags]").forEach(function (box) {
    var field = box.querySelector("[data-tag-in]"), value = box.querySelector("[data-tag-value]");
    function tags() { return Array.prototype.map.call(box.querySelectorAll("[data-tag]"), function (t) { return t.getAttribute("data-tag"); }); }
    function sync() { value.value = tags().join(", "); }
    function add() {
      var text = field.value.replace(/,/g, " ").replace(/\s+/g, " ").trim().slice(0, 30);
      field.value = "";
      if (!text || tags().indexOf(text) >= 0) return;
      var chip = document.createElement("span"), x = document.createElement("button");
      chip.className = "owin-tag"; chip.setAttribute("data-tag", text); chip.textContent = text;
      x.type = "button"; x.className = "owin-tag-x"; x.setAttribute("data-tag-x", ""); x.setAttribute("aria-label", "Убрать тег «" + text + "»"); x.innerHTML = X_ICON;
      chip.appendChild(x); box.insertBefore(chip, field); sync();
    }
    field.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); }        // Enter here adds a tag, it does not send the window
      else if (e.key === "Backspace" && !field.value) {
        var all = box.querySelectorAll("[data-tag]");
        if (all.length) { all[all.length - 1].remove(); sync(); }
      }
    });
    field.addEventListener("input", function (e) {                  // picked in the list of known tags; a phone's autocorrect is not a pick
      if (e.inputType && e.inputType !== "insertReplacementText") return;
      var known = field.list ? Array.prototype.map.call(field.list.options, function (o) { return o.value; }) : [];
      if (known.indexOf(field.value) >= 0) add();
    });
    field.addEventListener("blur", add);
    box.addEventListener("click", function (e) {
      var x = e.target.closest && e.target.closest("[data-tag-x]");
      if (x) { x.closest("[data-tag]").remove(); sync(); field.focus(); }
    });
    if (value.form) value.form.addEventListener("submit", add);                // a tag typed but not yet added goes too
  });

  // 13a) Заказ: ссылка на часть страницы (#wants после правки строки «Что нужно клиенту») раскрывает её, если она свёрнута
  (function () {
    var part = location.hash && document.getElementById(location.hash.slice(1));
    if (part && part.tagName === "DETAILS") part.open = true;
  })();

  // 13b) Новый заказ: «+» переносит позицию из полей в список (код, текст, шт., цена уходят скрытыми полями), ✕ убирает, итог пересчитывается.
  //      Без «+» заполненные поля уходят как одна позиция: окно работает и без этого блока.
  function typedAmount(text) {                                       // the same reading as amounts.parse_amount: «1.500» is 1500, «1234,50» is 1234.5
    var s = String(text).replace(/[\s  ₽]/g, "");
    if (!/^[\d.,]+$/.test(s)) return NaN;
    if (s.indexOf(".") >= 0 && s.indexOf(",") >= 0) {
      var last = Math.max(s.lastIndexOf("."), s.lastIndexOf(","));
      s = s.slice(0, last).replace(/[.,]/g, "") + "." + s.slice(last + 1);
    } else if (/^\d{1,3}([.,]\d{3})+$/.test(s) && s.charAt(0) !== "0") {
      s = s.replace(/[.,]/g, "");
    } else if ((s.match(/[.,]/g) || []).length > 1) {
      return NaN;
    }
    return parseFloat(s.replace(",", "."));
  }
  function rub(n) { return n.toLocaleString("ru-RU", {maximumFractionDigits: 2}) + " ₽"; }
  document.querySelectorAll("[data-item-new]").forEach(function (row) {
    var form = row.closest("form"), list = form.querySelector("[data-item-list]"), total = form.querySelector("[data-item-total]");
    var text = row.querySelector("[data-find-text]"), code = row.querySelector("[data-find-code]"), qty = row.querySelector("[data-find-qty]"), price = row.querySelector("[data-find-price]");
    var auto = row.querySelector("[data-find-auto]");
    function recount() {
      var lines = list.querySelectorAll("[data-item-line]"), sum = 0, open = 0;
      Array.prototype.forEach.call(lines, function (line) {
        var v = line.querySelector("[data-sum]").getAttribute("data-sum");
        if (v === "") open += 1; else sum += parseFloat(v);
      });
      total.textContent = "";
      if (!lines.length) {
        var none = document.createElement("span"); none.className = "muted"; none.textContent = "Позиций пока нет.";
        total.appendChild(none); return;
      }
      var b = document.createElement("b"); b.textContent = rub(Math.round(sum * 100) / 100);
      total.appendChild(document.createTextNode("Итого: ")); total.appendChild(b);
      if (open) { var note = document.createElement("span"); note.className = "muted"; note.textContent = " без позиций по цене панели"; total.appendChild(document.createTextNode(" ")); total.appendChild(note); }
    }
    function cell(tr, cls) { var td = document.createElement("td"); td.className = cls; tr.appendChild(td); return td; }
    function hidden(td, name, v) { var i = document.createElement("input"); i.type = "hidden"; i.name = name; i.value = v; td.appendChild(i); }
    function add() {
      var t = text.value.trim(), c = code.value.trim(), n = qty.value.trim(), p = price.value.trim();
      fine();
      if (!t && !c) return false;
      if (!/^\d+$/.test(n) || +n < 1 || +n > 100) { qty.setCustomValidity("Количество: целое число от 1 до 100."); qty.reportValidity(); return false; }
      var unit = p ? typedAmount(p) : null;
      if (p && !(unit > 0 && unit <= 1000000)) { price.setCustomValidity("Цена за шт.: число больше нуля."); price.reportValidity(); return false; }
      if (c === "point" && !p) { price.setCustomValidity("Шина с точки: впишите цену за шт., в панели её цены нет."); price.reportValidity(); return false; }
      var tr = document.createElement("tr"), name = cell(tr, "owin-iname"), many = cell(tr, "owin-iqty"), sum = cell(tr, "num owin-isum"), xs = cell(tr, "owin-ix");
      tr.setAttribute("data-item-line", "");
      name.textContent = t || c;
      if (c === "point") { var tag = document.createElement("div"), badge = document.createElement("span"); badge.className = "badge badge--neutral"; badge.textContent = "с точки · нет в выгрузке"; tag.appendChild(badge); name.appendChild(tag); }
      else if (c) { var mono = document.createElement("div"); mono.className = "mono owin-code"; mono.textContent = c; name.appendChild(mono); }
      hidden(name, "item_goods_id", c); hidden(name, "item_query", t); hidden(name, "item_qty", n); hidden(name, "item_sale_unit", p); hidden(name, "item_sale_auto", auto ? auto.value : "");
      many.textContent = n + " шт × " + (p ? rub(unit) : "цена из панели");
      sum.setAttribute("data-sum", p ? String(Math.round(unit * +n * 100) / 100) : "");
      sum.textContent = p ? rub(Math.round(unit * +n * 100) / 100) : "—";
      var x = document.createElement("button");
      x.type = "button"; x.className = "icon-btn"; x.setAttribute("data-item-x", ""); x.title = "Убрать из списка"; x.setAttribute("aria-label", "Убрать " + (t || c)); x.innerHTML = X_ICON;
      xs.appendChild(x);
      list.appendChild(tr);
      text.value = ""; price.value = ""; qty.value = "4";
      text.dispatchEvent(new Event("input"));                        // the finder forgets the chosen code and the price it brought
      recount();
      return true;
    }
    function fine() { qty.setCustomValidity(""); price.setCustomValidity(""); }   // a refusal is about this position only: any change takes it away
    [text, qty, price].forEach(function (f) { f.addEventListener("input", fine); });
    row.querySelector("[data-item-plus]").addEventListener("click", function () { if (add()) text.focus(); else if (!text.value.trim()) text.focus(); });
    row.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || e.defaultPrevented || !e.target.matches("input")) return;
      e.preventDefault();                                            // Enter in the fields of a position puts it in the list, it does not make the order yet
      if (add()) text.focus();
    });
    list.addEventListener("click", function (e) {
      var x = e.target.closest && e.target.closest("[data-item-x]");
      if (x) { x.closest("[data-item-line]").remove(); recount(); }
    });
    form.addEventListener("submit", function (e) {
      if ((text.value.trim() || code.value.trim()) && !add()) { e.preventDefault(); e.stopImmediatePropagation(); }   // a wrong position stops the send, it is shown at its field
    });
  });
  // 13c) Позиция заказа: «Закупка за N шт» = закупка за 1 шт × шт. строки, пересчёт при вводе (только показ; сохраняет кнопка строки)
  document.querySelectorAll("[data-buy]").forEach(function (buy) {
    var unit = buy.querySelector("[data-buy-unit]"), qty = document.querySelector('input[name="qty"][form="' + unit.getAttribute("form") + '"]');
    function recount() {
      var n = qty ? parseInt(qty.value, 10) : NaN, price = typedAmount(unit.value);
      buy.querySelector("[data-buy-qty]").textContent = isFinite(n) && n > 0 ? n : "?";
      buy.querySelector("[data-buy-sum]").textContent = isFinite(n) && n > 0 && isFinite(price) && price > 0 ? Math.round(n * price).toLocaleString("ru-RU") + " ₽" : "—";
    }
    unit.addEventListener("input", recount);
    if (qty) qty.addEventListener("input", recount);
  });
  // 13d) Возврат: пустая «Сумма возврата» по типу (поставщику — закупка, клиенту — продажа; суммы в data-suggest-*). Окно «Отмена или
  //      возврат» подставляет её в поле, смена типа меняет подставленную, набранную руками не трогает. Окно заказа только показывает
  //      подсказку серым (data-return-amount="hint"): любая его кнопка шлёт всю форму, подставленное сохранилось бы без «Сохранить»
  document.querySelectorAll("[data-return-amount]").forEach(function (amount) {
    var kind = amount.form && amount.form.elements.namedItem("return_kind"), hint = amount.getAttribute("data-return-amount") === "hint";
    if (!kind) return;
    amount.addEventListener("input", function () {
      if (amount.value.trim()) amount.removeAttribute("data-auto"); else amount.setAttribute("data-auto", "");   // стёрта — снова подсказка
    });
    kind.addEventListener("change", function () {
      var suggest = amount.getAttribute("data-suggest-" + kind.value) || "";
      if (hint) amount.placeholder = suggest;
      else if (amount.hasAttribute("data-auto")) amount.value = suggest;
    });
  });
  // 12) «Отправить клиенту?»: готовый текст задачи уходит в чат Авито только по нажатию «Отправить»; при сбое окно остаётся с текстом и ошибкой
  function focusSend(win) {
    var area = win.querySelector("textarea:not([readonly])");
    if (area) { area.focus(); area.setSelectionRange(area.value.length, area.value.length); }   // a correction is typed at the end, not before the text
  }
  document.querySelectorAll(".send-window.is-open").forEach(focusSend);
  // the board has no page of the order: the same window is drawn from the answer of the move (text only, never markup); closed, the board reloads
  // s.more: other messages of the order wait too (to the point): a link to the window «Сообщения по заказу» of the card; without s.url only that
  window.tiresSendWindow = function (s, csrf) {
    function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
    function hidden(name, value) { var i = el("input"); i.type = "hidden"; i.name = name; i.value = value; return i; }
    function moreLink() {
      var p = el("p", "small"), a = el("a", "btn btn--secondary btn--sm", "Открыть сообщения заказа");
      a.href = s.more; a.setAttribute("data-messages-link", "");
      p.appendChild(el("span", null, "Ждут ещё: " + (s.others || []).join("; ") + ". ")); p.appendChild(a);
      return p;
    }
    if (!s.url) {
      var box = el("div", "modal-backdrop send-window is-open"), dlg = el("div", "modal"), top = el("div", "modal-head"), hh = el("h3", null, "Сообщения по заказу №" + s.card);
      var close = el("button", "icon-btn ml-auto"), inner = el("div", "modal-body stack"), bottom = el("div", "modal-foot"), wait = el("button", "btn btn--secondary", "Позже");
      box.id = "order-messages-" + s.card; box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true"); box.setAttribute("aria-labelledby", box.id + "-h");
      hh.id = box.id + "-h"; close.type = "button"; close.innerHTML = X_ICON; close.setAttribute("data-close-modal", ""); close.setAttribute("aria-label", "Закрыть");
      wait.type = "button"; wait.setAttribute("data-close-modal", "");
      top.appendChild(hh); top.appendChild(close); inner.appendChild(moreLink()); bottom.appendChild(wait);
      dlg.appendChild(top); dlg.appendChild(inner); dlg.appendChild(bottom); box.appendChild(dlg); document.body.appendChild(box);
      new MutationObserver(function () { if (!box.classList.contains("is-open")) window.location.reload(); }).observe(box, {attributes: true, attributeFilter: ["class"]});
      return;
    }
    var win = el("div", "modal-backdrop send-window is-open"), modal = el("div", "modal"), form = el("form");
    win.id = "send-task-" + s.task; win.setAttribute("role", "dialog"); win.setAttribute("aria-modal", "true"); win.setAttribute("aria-labelledby", win.id + "-h");
    form.method = "post"; form.action = s.url; form.setAttribute("data-send-task", "");
    form.appendChild(hidden("csrf", csrf)); form.appendChild(hidden("chat", s.chat)); form.appendChild(hidden("back", "/orders"));
    var head = el("div", "modal-head"), h = el("h3", null, "Отправить клиенту?"), x = el("button", "icon-btn ml-auto");
    h.id = win.id + "-h"; x.type = "button"; x.innerHTML = X_ICON; x.setAttribute("data-close-modal", ""); x.setAttribute("aria-label", "Закрыть");
    head.appendChild(h); head.appendChild(x);
    var body = el("div", "modal-body stack"), label = el("label", "field"), area = el("textarea", "textarea");
    body.appendChild(el("p", "small", (s.client || "Клиент без имени") + " · чат " + s.ref + " · " + s.title));
    area.name = "text"; area.rows = 10; area.maxLength = 2000; area.required = true; area.value = s.body;
    label.appendChild(el("span", "label", "Текст сообщения (можно поправить)")); label.appendChild(area); body.appendChild(label);
    var err = el("p", "small text-err"); err.setAttribute("data-send-error", ""); err.setAttribute("role", "alert"); err.hidden = true; body.appendChild(err);
    if (s.more) body.appendChild(moreLink());
    body.appendChild(el("p", "small muted", "«Не сейчас»: сообщение подождёт в заказе №" + s.card + ", в «Сообщениях по заказу»."));
    var foot = el("div", "modal-foot"), later = el("button", "btn btn--secondary", "Не сейчас"), go = el("button", "btn btn--primary", "Отправить");
    later.type = "button"; later.setAttribute("data-close-modal", ""); go.setAttribute("data-send-button", "");
    foot.appendChild(later); foot.appendChild(go);
    form.appendChild(head); form.appendChild(body); form.appendChild(foot); modal.appendChild(form); win.appendChild(modal);
    document.body.appendChild(win);
    new MutationObserver(function () { if (!win.classList.contains("is-open")) window.location.reload(); }).observe(win, {attributes: true, attributeFilter: ["class"]});
    focusSend(win);
  };
  document.addEventListener("click", function (e) {
    var opener = e.target.closest && e.target.closest("[data-open-modal]");
    if (!opener || opener.disabled) return;
    var win = document.getElementById(opener.getAttribute("data-open-modal"));
    if (!win) return;
    if (opener.tagName === "A") e.preventDefault();                 // a link of a menu («Отменить заказ…»): the window, not a jump down the page
    var menu = opener.closest("details.menu");
    if (menu) menu.removeAttribute("open");
    win.classList.add("is-open"); focusSend(win);
  });
  // «Подключения → Google-таблица»: «Придумать» кладёт в поле случайный секрет и показывает его, чтобы скопировать в свойства скрипта
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-sheet-secret-new]");
    var field = b && document.querySelector("[data-sheet-secret]");
    if (!field || !window.crypto) return;
    var bytes = new Uint8Array(24);
    window.crypto.getRandomValues(bytes);
    field.value = Array.prototype.map.call(bytes, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
    field.type = "text"; field.select();
  });
  if (window.location.hash === "#finish") {                         // /orders/<id>#finish: the window «Отмена или возврат» of the card
    var finishWin = document.getElementById("finish-window");
    if (finishWin) finishWin.classList.add("is-open");
  }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.hasAttribute || !form.hasAttribute("data-send-task")) return;
    e.preventDefault();
    var button = form.querySelector("[data-send-button]"), error = form.querySelector("[data-send-error]");
    if (button.disabled) return;                                    // one press, one request: the server refuses a second send of the task anyway
    button.disabled = true; button.textContent = "Отправляю…"; error.hidden = true;
    fetch(form.action, {method: "POST", body: new FormData(form), credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
      .then(function (r) {
        if (r.redirected || r.status === 400) return {ok: false, message: "Сообщение не отправлено: страница устарела или вход истёк. Обновите страницу."};
        if (!r.ok) throw new Error("http " + r.status);
        return r.json();
      })
      .then(function (res) {
        if (res.ok) { window.location.reload(); return; }
        error.textContent = res.message; error.hidden = false;
        button.disabled = false; button.textContent = "Отправить";
      })
      .catch(function () {
        error.textContent = "Нет ответа от панели: неизвестно, ушло ли сообщение. Обновите страницу и проверьте чат, прежде чем отправлять ещё раз.";
        error.hidden = false; button.textContent = "Отправить";
        button.disabled = false;
      });
  });
  // «Сообщения по заказу»: кнопка сообщения или «Отправить оба» уходит одним запросом; ответ по каждому сообщению пишется под ним.
  // «Отправлено вручную» и «Пропустить» — обычная форма. Окно закрыли после отправки — страница перечитывается.
  document.addEventListener("submit", function (e) {
    var form = e.target, by = e.submitter;
    if (!form.hasAttribute || !form.hasAttribute("data-messages-form")) return;
    if (!by || by.hasAttribute("data-plain")) return;
    e.preventDefault();
    if (form.hasAttribute("data-busy")) return;                    // one press, one request: the server sends each message once anyway
    var buttons = [].slice.call(form.querySelectorAll("[data-send-one],[data-send-all]")), was = buttons.map(function (b) { return b.disabled; });
    var label = by.textContent, data = new FormData(form), all = form.querySelector("[data-send-error-all]");
    data.append(by.name, by.value);
    form.setAttribute("data-busy", ""); buttons.forEach(function (b) { b.disabled = true; }); by.textContent = "Отправляю…";
    form.querySelectorAll("[data-send-error]").forEach(function (p) { p.hidden = true; }); all.hidden = true;
    function done() {
      form.removeAttribute("data-busy"); by.textContent = label;
      buttons.forEach(function (b, i) { b.disabled = was[i] || !!b.closest("[data-sent]"); });
    }
    fetch(form.action, {method: "POST", body: data, credentials: "same-origin", headers: {"X-Requested-With": "fetch"}})
      .then(function (r) {
        if (r.redirected || r.status === 400 || r.status === 403) return {ok: false, results: [{task: null, ok: false, message: "Ничего не отправлено: страница устарела, вход истёк или нет права. Обновите страницу."}]};
        if (!r.ok) throw new Error("http " + r.status);
        return r.json();
      })
      .then(function (res) {
        if (res.ok) { window.location.reload(); return; }
        res.results.forEach(function (r) {
          var item = r.task != null && form.querySelector('[data-message="' + r.task + '"]'), p = item ? item.querySelector("[data-send-error]") : all;
          p.textContent = r.message; p.hidden = false; p.className = "small " + (r.ok ? "text-ok" : "text-err");
          if (r.ok && item) {
            item.setAttribute("data-sent", "");
            var ready = form.querySelector('input[name="ready"][value="' + r.task + '"]');
            if (ready) ready.remove();
            form.setAttribute("data-changed", "");
          }
        });
        done();
      })
      .catch(function () {
        all.textContent = "Нет ответа от панели: неизвестно, ушли ли сообщения. Обновите страницу и проверьте чат клиента и ВК точки, прежде чем отправлять ещё раз.";
        all.hidden = false; form.setAttribute("data-changed", ""); done();
      });
  });
  document.querySelectorAll(".messages-window").forEach(function (win) {
    new MutationObserver(function () {
      var form = win.querySelector("[data-messages-form]");
      if (!win.classList.contains("is-open") && form && form.hasAttribute("data-changed")) window.location.reload();
    }).observe(win, {attributes: true, attributeFilter: ["class"]});
  });

  // 14) Каталог: фото позиции не загрузилось — та же заглушка «нет фото», что и без фото, а не значок битой картинки
  function noPhoto(img) {
    var box = document.createElement("div");
    box.className = "thumb"; box.textContent = "нет фото";
    img.replaceWith(box);
  }
  document.addEventListener("error", function (e) {
    if (e.target.matches && e.target.matches("img.thumb")) noPhoto(e.target);
  }, true);                                                              // error does not bubble: caught on the way down
  document.querySelectorAll("img.thumb").forEach(function (img) {
    if (img.complete && !img.naturalWidth && img.getAttribute("src")) noPhoto(img);   // failed before this script ran
  });

  // 15) Шапка таблицы липнет к верху страницы. .table-wrap листает широкую таблицу вбок и потому сам становится
  // контейнером прокрутки: th{position:sticky} липнет к нему, а не к странице. Поэтому шапку внутри обёртки
  // сдвигаем вниз сами, пока таблица проходит под верхним краем (ниже закреплённой панели .stickybar, если она видна).
  // Без скрипта шапка просто не липнет; таблицы без обёртки и .table--plain не трогаем.
  var main = document.querySelector(".main");
  var pageScrolled = new WeakMap();            // обёртка → листает ли её именно .main (а не своя прокрутка внутри)
  function byPage(wrap) {
    if (!pageScrolled.has(wrap)) {
      var ok = getComputedStyle(wrap).maxHeight === "none";          // a wrap with its own height scrolls itself: not ours
      for (var el = wrap.parentElement; el && el !== main; el = el.parentElement) {
        if (/(auto|scroll)/.test(getComputedStyle(el).overflowY)) { ok = false; break; }
      }
      pageScrolled.set(wrap, ok && el === main);
    }
    return pageScrolled.get(wrap);
  }
  var headFrame = 0;
  function placeHeads() {
    headFrame = 0;
    var wraps = main.querySelectorAll(".table-wrap");
    if (!wraps.length) return;                                         // no wrapped table on the page: nothing to read
    var line = Math.max(0, main.getBoundingClientRect().top);
    document.querySelectorAll(".stickybar").forEach(function (bar) {
      if (bar.getClientRects().length) line = Math.max(line, bar.getBoundingClientRect().bottom);
    });
    wraps.forEach(function (wrap) {
      var head = wrap.querySelector(":scope > .table:not(.table--plain) > thead");
      if (!head || !byPage(wrap)) return;
      var box = head.parentElement.getBoundingClientRect();              // the table, not the wrap: its scrollbar is not room
      var shift = Math.floor(Math.max(0, Math.min(line - box.top, box.height - head.getBoundingClientRect().height)));
      head.style.transform = shift ? "translateY(" + shift + "px)" : "";
    });
  }
  function headsLater() { if (!headFrame) headFrame = requestAnimationFrame(placeHeads); }
  if (main) {
    main.addEventListener("scroll", headsLater, {passive: true});
    window.addEventListener("resize", function () { pageScrolled = new WeakMap(); headsLater(); });
    document.addEventListener("tires:table-loaded", headsLater);        // the catalog table is swapped in place (may appear only then)
    var barWatch = new MutationObserver(headsLater);                    // a draft bar shown or hidden moves the line and the table
    document.querySelectorAll(".stickybar").forEach(function (bar) { barWatch.observe(bar, {attributes: true, attributeFilter: ["hidden"]}); });
    headsLater();
  }
})();

(function () {
  // A box with data-check-all ticks or unticks the boxes named in it (inside its own form): «Выбрать все» of a table.
  document.addEventListener("change", function (e) {
    var box = e.target;
    if (!box.matches || !box.matches("input[data-check-all]")) return;
    var form = box.closest("form");
    if (!form) return;
    form.querySelectorAll('input[type=checkbox][name="' + box.getAttribute("data-check-all") + '"]').forEach(function (x) { x.checked = box.checked; });
  });
})();

(function () {
  // «Список» of «Заказы»: a click anywhere on a row opens the order (a link, a button or selected text keeps its own way);
  // «Колонки»: ↑ and ↓ move a column in the form (the order of its fields is the order saved).
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t.closest) return;
    var move = t.closest("[data-col-up],[data-col-down]");
    if (move) {
      var li = move.closest("[data-col]");
      var other = move.hasAttribute("data-col-up") ? li.previousElementSibling : li.nextElementSibling;
      if (other) li.parentNode.insertBefore(li, move.hasAttribute("data-col-up") ? other : other.nextElementSibling);
      move.focus();
      return;
    }
    var row = t.closest("tr[data-href]");
    if (!row || t.closest("a,button,input,label,select") || String(window.getSelection ? window.getSelection() : "")) return;
    if (e.ctrlKey || e.metaKey) window.open(row.getAttribute("data-href"), "_blank");
    else window.location.href = row.getAttribute("data-href");
  });

  // Полоса «Не применено: N» страниц настроек: что изменится в фиде (тот же расчёт, что у страницы черновика) и нужные подтверждения.
  // «Применить» отправляет ровно тот черновик, что описан строкой (rev); без скрипта кнопка работает, подтверждения спросит страница черновика.
  var bar = document.querySelector("[data-settings-bar]");
  function plural(n, one, few, many) {
    var t = Math.abs(n) % 100, u = t % 10;
    return t >= 11 && t <= 14 ? many : u === 1 ? one : u >= 2 && u <= 4 ? few : many;
  }
  function refreshPending() {
    fetch(bar.getAttribute("data-pending-url"), { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) {
        var apply = bar.querySelector("[data-pending-apply]");
        if (!d.open) { if (apply) apply.disabled = true; return; }
        var form = bar.querySelector("[data-pending-form]");
        if (form) form.setAttribute("action", "/draft/" + d.draft + "/confirm");
        if (apply) apply.disabled = false;
        bar.querySelector("[data-pending-n]").textContent = d.count;               // the draft the line describes: its own number
        bar.querySelector("[data-pending-title]").textContent = "Не применено: " + d.count + " " + plural(d.count, "правка", "правки", "правок");
        bar.querySelector("[data-pending-line]").textContent = d.line + " Пока вы не нажмёте «Применить», фид не меняется.";
        var rev = bar.querySelector("[data-pending-rev]");
        var changed = rev && rev.value !== d.rev;
        if (rev) rev.value = d.rev;
        if (changed) {                       // another draft than the one agreed to: a tick given for the old one is no consent to this one
          bar.querySelectorAll("[data-pending-checks] input[type=checkbox]").forEach(function (box) { box.checked = false; });
        }
        var checks = bar.querySelector("[data-pending-checks]");
        if (!checks) return;
        var understand = checks.querySelector("[data-check-understand]"), strong = checks.querySelector("[data-check-strong]");
        understand.hidden = !d.guard.over;
        strong.hidden = !d.strong;
        var agreed = bar.querySelector("[data-pending-gone]");
        if (agreed) agreed.value = d.guard.over ? d.guard.gone : "";     // the number the tick is given for: the server checks it is still so
        if (d.guard.over) {
          checks.querySelector("[data-understand-text]").textContent = "Я понимаю: исчезнет " + d.guard.gone + " объявлений (порог защиты " + d.guard.limit + ")";
        }
        checks.hidden = !(d.guard.over || d.strong);
      })
      .catch(function () { bar.querySelector("[data-pending-line]").textContent = "Не удалось посчитать, что изменится: откройте «Подробнее»."; });
  }
  if (bar) {
    window.tiresPending = refreshPending;
    if (!bar.hidden) refreshPending();
  }
})();
