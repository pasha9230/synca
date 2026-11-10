// Уведомления менеджеру точки (решение владельца 2026-10-23: точки работают в панели, ВК — запасной). Пока открыта любая страница панели,
// раз в полминуты спрашивает /my-point/feed (заказы точки, записи «Для точки», строки её колокольчика — один источник с ним) и на новое
// даёт короткий звук и уведомление браузера. Разрешение на уведомления спрашивается только по кнопке «Включить уведомления» на «Рабочем месте»;
// звук браузер даёт каждой странице после нажатия на ней (кроме сайтов, которым он доверяет): пока звука нет, «Рабочее место» просит нажать;
// браузер без WebAudio не просит (нажатие звук не даст), а говорит, что звука не будет.
// Что уже показано, помнится отдельно для каждого входа (общий ПК точки). Без внешних сервисов: работает, пока страница открыта.
// «Рабочее место» (/my-point) на новое обновляется само (решение владельца 2026-11-10, вместо «Есть новое. Обновить»), но только когда точка
// ничего не делает: ~10 с без нажатий, клавиш и прокрутки, не открыто окно (подтверждение, «Передать клиента»), «ОТКАЗ» или меню, фокус не
// в поле ввода, нет уходящей формы. Занята — ждёт простоя. Не чаще раза в 30 с. Вкладка и поиск остаются (они в адресе), прокрутка — тоже.
(function () {
  "use strict";
  var KEY = "tires.notify.seen:", EVERY = 30000, MAX_SHOWN = 3, NOTHING = "новое видно только на странице и в колокольчике.";
  var button = document.querySelector("[data-notify-enable]"), state = document.querySelector("[data-notify-state]");
  var supported = "Notification" in window, audio = null, Ctx = window.AudioContext || window.webkitAudioContext;

  function showState() {
    if (!state) return;
    if (!supported) {
      state.textContent = Ctx ? "Этот браузер не показывает уведомления: будет только звук." : "Этот браузер не показывает уведомления и не даёт звук: " + NOTHING;
      return;
    }
    if (Notification.permission === "granted") { state.textContent = "Уведомления включены."; }
    else if (Notification.permission === "denied") { state.textContent = "Уведомления запрещены в настройках браузера: будет только звук."; }
    else { state.textContent = ""; }
    if (button) button.hidden = Notification.permission !== "default";
    if (!Ctx) {                                           // no WebAudio: a press gives no sound, so it is not asked for
      state.textContent = Notification.permission === "denied" ? "Уведомления запрещены в настройках браузера, а звук этот браузер не даёт: " + NOTHING
        : (state.textContent ? state.textContent + " " : "") + "Этот браузер не даёт звук: будут только уведомления.";
      return;
    }
    if (!muted()) return;
    state.textContent = (state.textContent ? state.textContent + " " : "") + "Нажмите на страницу, чтобы включить звук.";
  }
  function muted() { return !audio || audio.state !== "running"; }

  function unlock() {                                     // the browser lets a page sound only after a press on it
    try {
      if (Ctx) audio = audio || new Ctx();
      if (audio && audio.state === "suspended") { var on = audio.resume(); if (on && on.then) on.then(showState, function () {}); }
    } catch (e) { if (!audio) Ctx = null; }               // WebAudio that cannot be made is none: no endless «Нажмите…»
    showState();
  }
  ["click", "keydown", "touchstart"].forEach(function (name) { document.addEventListener(name, unlock, {passive: true}); });

  function ring() {
    if (audio && audio.state === "suspended") {           // paused by the browser after it was allowed (Safari, a background tab)
      try { var on = audio.resume(); if (on && on.then) on.then(function () { if (!muted()) play(); }, function () {}); } catch (e) { /* muted */ }
      return;
    }
    if (!muted()) play();
  }

  function play() {
    try {
      [[880, 0], [660, 0.18]].forEach(function (tone) {
        var osc = audio.createOscillator(), gain = audio.createGain(), at = audio.currentTime + tone[1];
        osc.frequency.value = tone[0];
        gain.gain.setValueAtTime(0.0001, at);
        gain.gain.exponentialRampToValueAtTime(0.25, at + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.16);
        osc.connect(gain); gain.connect(audio.destination);
        osc.start(at); osc.stop(at + 0.17);
      });
    } catch (e) { /* no sound: the notification and the bell remain */ }
  }

  function tell(item) {
    if (!supported || Notification.permission !== "granted") return;
    try {
      var n = new Notification(item.title, {body: item.text || "", tag: item.key, icon: "/static/favicon.svg"});
      n.onclick = function () { window.focus(); if (item.href) window.location.href = item.href; n.close(); };
    } catch (e) { /* some browsers allow notifications only from a service worker: the sound and the bell remain */ }
  }

  function readSeen(who) {
    try { var raw = window.localStorage.getItem(KEY + who); return raw === null ? null : JSON.parse(raw); } catch (e) { return undefined; }
  }
  function writeSeen(who, keys) {
    try { window.localStorage.setItem(KEY + who, JSON.stringify(keys)); } catch (e) { /* private window: this page remembers it itself */ }
  }

  // The question «Клиент забрал?» comes twice: as itself (question-<order>-<stay>) and as its row of the bell (alert-question-<order>-<stay>-0):
  // one key, so one sound and one notification. Its stay is in the key: the order back at the point is asked anew and rings again.
  function keyOf(key) { var m = /^alert-(question-\d+-\d*)-\d+$/.exec(String(key)); return m ? m[1] : String(key); }
  function once(items) {
    var had = {};
    return items.filter(function (it) { var k = keyOf(it.key); if (had[k]) return false; had[k] = true; it.key = k; return true; });
  }

  var memory = null;                                      // what this page has seen when the browser keeps nothing
  function check() {
    fetch("/my-point/feed", {credentials: "same-origin", headers: {"Accept": "application/json"}}).then(function (r) {
      if (!r.ok || r.redirected) throw new Error(r.status);                    // an expired login answers with the login page
      return r.json();
    }).then(function (data) {
      var items = once(data.items || []), keys = items.map(function (it) { return it.key; });
      var who = String(data.who || ""), stored = readSeen(who), seen = stored === undefined ? memory : stored;
      memory = keys;
      writeSeen(who, keys);
      if (!seen) return;                                  // the first look: what is there now is not new
      var known = {};
      seen.forEach(function (k) { known[k] = true; });
      var added = items.filter(function (it) { return !known[it.key]; });
      if (!added.length) return;
      ring();
      refreshSoon();
      added.slice(0, MAX_SHOWN).forEach(tell);
      if (added.length > MAX_SHOWN) tell({key: "more", title: "Ещё " + (added.length - MAX_SHOWN), text: "Новое на «Рабочем месте»", href: "/my-point"});
    }).catch(function () { /* no connection: the bell shows it; the next look tries again */ });
  }

  // --- «Рабочее место» обновляется само, пока точка ничего не делает --------------------------------------------------------------------
  var PAGE = !!(window.location && window.location.pathname === "/my-point"), IDLE = 10000, WAIT = 2000;
  var SCROLL = "tires.mypoint.scroll", LAST = "tires.mypoint.reloaded";
  var active = Date.now(), sending = false, waiting = null, reloaded = 0;
  var BUSY = ".modal-backdrop.is-open, dialog[open], [aria-modal='true'].is-open, details.mp-refuse[open], details.mp-handover[open], " +
             "details.pt-menu[open], details.mp-more[open], #bell-pop:not([hidden]), [aria-busy='true']";

  function session(name, value) {
    try {
      if (value === undefined) return window.sessionStorage.getItem(name);
      if (value === null) window.sessionStorage.removeItem(name); else window.sessionStorage.setItem(name, value);
    } catch (e) { /* private window: no memory between the loads */ }
    return null;
  }
  function typing() {
    var el = document.activeElement;
    if (!el || !el.matches) return false;
    return el.matches("textarea, select, [contenteditable=''], [contenteditable='true'], input:not([type=button]):not([type=submit]):not([type=reset]):not([type=hidden])");
  }
  function idle() {                                       // the point does nothing: no press, key or scroll lately, no window, no field, no form going
    if (Date.now() - active < IDLE || sending || typing()) return false;
    return !document.querySelector(BUSY);
  }
  function soon() {                                       // not more often than once in 30 s, this load and the one before
    var last = Math.max(reloaded, Number(session(LAST)) || 0);
    return Date.now() - last < EVERY;
  }
  function reload() {
    reloaded = Date.now();
    session(LAST, String(reloaded));
    var url = String(window.location.href).split("#")[0];
    session(SCROLL, JSON.stringify({url: url, y: window.scrollY || window.pageYOffset || 0}));
    window.location.replace(url);                         // a GET of the same address (the tab, the search): never a form sent again
  }
  function tryRefresh() {
    waiting = null;
    if (idle() && !soon()) { reload(); return; }
    waiting = setTimeout(tryRefresh, WAIT);               // busy: look again a little later, until the point is idle
  }
  function refreshSoon() {
    if (PAGE && !waiting) tryRefresh();
  }
  function moved() { active = Date.now(); }

  if (PAGE) {
    ["click", "keydown", "touchstart", "pointerdown", "wheel", "input", "scroll"].forEach(function (name) {
      document.addEventListener(name, moved, {passive: true, capture: true});
    });
    window.addEventListener("scroll", moved, {passive: true});
    window.addEventListener("submit", function (e) { if (!e.defaultPrevented) sending = true; });     // asked by a window: not yet sent
    window.addEventListener("pageshow", function (e) { if (e.persisted) { sending = false; moved(); } });
    try {                                                 // back where the point was before the refresh
      var was = JSON.parse(session(SCROLL) || "null");
      session(SCROLL, null);
      if (was && was.url === String(window.location.href).split("#")[0] && was.y > 0) {
        var put = function () { window.scrollTo(0, was.y); active = 0; };
        if (document.readyState === "complete") put(); else window.addEventListener("load", put);
      }
    } catch (e) { /* nothing kept */ }
  }

  if (button && supported) {
    button.addEventListener("click", function () {
      unlock();
      var asked = Notification.requestPermission(showState);
      if (asked && asked.then) asked.then(showState);
    });
  }
  unlock();                                               // a site the browser trusts may sound at once; otherwise after a press
  check();
  setInterval(check, EVERY);
})();
