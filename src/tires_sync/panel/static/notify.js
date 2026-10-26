// Уведомления менеджеру точки (решение владельца 2026-10-23: точки работают в панели, ВК — запасной). Пока открыта любая страница панели,
// раз в полминуты спрашивает /my-point/feed (заказы точки, записи «Для точки», строки её колокольчика — один источник с ним) и на новое
// даёт короткий звук и уведомление браузера. Разрешение на уведомления спрашивается только по кнопке «Включить уведомления» на «Моей точке»;
// звук браузер даёт каждой странице после нажатия на ней (кроме сайтов, которым он доверяет): пока звука нет, «Моя точка» просит нажать;
// браузер без WebAudio не просит (нажатие звук не даст), а говорит, что звука не будет.
// Что уже показано, помнится отдельно для каждого входа (общий ПК точки). Без внешних сервисов: работает, пока страница открыта.
(function () {
  "use strict";
  var KEY = "tires.notify.seen:", EVERY = 30000, MAX_SHOWN = 3, NOTHING = "новое видно только на странице и в колокольчике.";
  var button = document.querySelector("[data-notify-enable]"), state = document.querySelector("[data-notify-state]"),
      fresh = document.querySelector("[data-notify-fresh]");
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
      if (fresh) fresh.hidden = false;
      added.slice(0, MAX_SHOWN).forEach(tell);
      if (added.length > MAX_SHOWN) tell({key: "more", title: "Ещё " + (added.length - MAX_SHOWN), text: "Новое на «Моей точке»", href: "/my-point"});
    }).catch(function () { /* no connection: the bell shows it; the next look tries again */ });
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
