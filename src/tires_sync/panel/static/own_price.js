// «Своя цена» on the card of a position: the price that goes into the feed, as the feed rounds it (up to tens), shown while typing.
(function () {
  var form = document.querySelector("[data-own-price]");
  if (!form) return;
  var purchase = parseFloat(form.getAttribute("data-purchase")) || 0;
  var out = form.querySelector("[data-own-result]");
  var rub = function (n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " ₽"; };
  function show() {
    var kind = (form.querySelector("[name=kind]:checked") || {}).value;
    var text = (form.querySelector("[name=value]").value || "").replace(/[\s ]/g, "").replace(",", ".").replace("−", "-");
    var n = parseFloat(text);
    if (!/^[+\-]?\d+(\.\d+)?$/.test(text) || isNaN(n)) { out.textContent = ""; return; }
    var value = kind === "pct" ? purchase * (1 + n / 100) : n;
    if (kind === "pct" && purchase <= 0) { out.textContent = "закупка неизвестна"; return; }
    var final = Math.ceil(Math.round(value * 100) / 1000) * 10;
    out.textContent = (kind === "pct" ? rub(Math.round(value)) : rub(n)) + " → " + rub(final) + " в фиде" +
      (purchase > 0 && final < purchase ? " — ниже закупки" : "");
  }
  form.addEventListener("input", show);
  form.addEventListener("change", show);
  show();
})();
