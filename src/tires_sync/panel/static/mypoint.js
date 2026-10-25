// «Моя точка»: «Надиктовать» в поле «Что нужно» — микрофон браузера (Web Speech API). Кнопка есть только там, где браузер умеет
// распознавать речь и подтверждает, что политика сайта разрешает микрофон. Сейчас панель его запрещает всем страницам (Permissions-Policy
// в panel/__init__.py), поэтому кнопки нет нигде: решение тимлида 2026-10-21, включение — вопрос владельцу (звук уходит Google/Apple).
(function () {
  "use strict";
  var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  var button = document.querySelector("[data-dictate]");
  var policy = document.permissionsPolicy || document.featurePolicy;
  if (!Recognition || !button) return;
  if (!(policy && policy.allowsFeature && policy.allowsFeature("microphone"))) return;    // only where the site's Permissions-Policy is known to allow it
  var form = button.closest("form"), field = form && form.querySelector("textarea[name=need]");
  if (!field) return;
  var MAX = 500;                                                                     // as maxlength of the field and the server's limit
  var idle = button.textContent, listening = null;
  var stop = function () {
    if (listening) { try { listening.stop(); } catch (e) { /* already stopped */ } }
    listening = null;
    button.textContent = idle;
    button.classList.remove("is-listening");
  };
  button.hidden = false;
  button.addEventListener("click", function () {
    if (listening) { stop(); return; }
    var rec = new Recognition();
    rec.lang = "ru-RU";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    rec.onresult = function (e) {
      var text = Array.prototype.map.call(e.results, function (r) { return r[0].transcript; }).join(" ").trim();
      if (text) field.value = ((field.value.trim() ? field.value.trim() + " " : "") + text).slice(0, MAX);
      field.dispatchEvent(new Event("input", {bubbles: true}));
    };
    rec.onerror = function (e) {
      stop();
      button.textContent = e.error === "not-allowed" ? "Микрофон не разрешён" : "Не расслышал — ещё раз";
    };
    rec.onend = function () { if (listening === rec) stop(); };
    listening = rec;
    button.textContent = "Слушаю… нажмите, чтобы остановить";
    button.classList.add("is-listening");
    try { rec.start(); } catch (e) { stop(); }
  });
})();
