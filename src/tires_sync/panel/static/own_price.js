// «Своя цена» (the card of a position, the cell «Наша цена» of the catalog): the price that goes into the feed, as the feed rounds it (up to
// tens), shown while typing. The card picks % or ₽ with a switch; the cell reads it from the text: «+5 %» a percent, «6990» rubles. The
// catalog's table is loaded anew after a change: the forms are found by the event, not once.
(function () {
  var rub = function (n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " ₽"; };
  function show(form) {
    var purchase = parseFloat(form.getAttribute("data-purchase")) || 0;
    var out = form.querySelector("[data-own-result]");
    var field = form.querySelector("[name=value]");
    if (!out || !field) return;
    out.classList.remove("text-err");
    var each = form.querySelector("[data-own-each]");
    if (each) each.textContent = "";
    var text = (field.value || "").replace(/[\s  ₽]/g, "").replace(",", ".").replace(/[−–]/, "-");
    var radio = form.querySelector("[name=kind]:checked");
    var kind = radio ? radio.value : (/%$/.test(text) ? "pct" : "price");
    if (!radio) text = text.replace(/%$/, "");
    var n = parseFloat(text);
    if (!/^[+\-]?\d+(\.\d+)?$/.test(text) || isNaN(n)) { out.textContent = ""; return; }
    if (kind !== "pct" && (n !== Math.floor(n) || (!radio && /^[+\-]/.test(text)))) { out.textContent = "цена — целое число рублей"; return; }
    var value = kind === "pct" ? purchase * (1 + n / 100) : n;
    if (kind === "pct" && purchase <= 0) { out.textContent = "закупка неизвестна"; return; }
    var final = Math.ceil(Math.round(value * 1e6) / 1e7) * 10;      // up to tens, as the server (float dust below a millionth dropped)
    var cost = parseFloat(form.getAttribute("data-cost")) || purchase;   // «ниже закупки» as the server asks it: today's supplier price
    var below = cost > 0 && final < cost;
    out.textContent = (kind === "pct" ? rub(Math.round(value)) : rub(n)) + " → " + rub(final) + " в фиде" + (below ? " — ниже закупки" : "");
    out.classList.toggle("text-err", below);
    if (each) {                                                        // the cell: what goes into each account's feed (its correction on top)
      var shifts = [];
      try { shifts = JSON.parse(form.getAttribute("data-shifts") || "[]"); } catch (err) { shifts = []; }
      each.textContent = shifts.map(function (s) {
        var v = kind === "pct" ? purchase * (1 + (n + s[1]) / 100) : Math.floor(n + purchase * s[1] / 100 + 0.5);
        return s[0] + " " + rub(Math.ceil(Math.round(v * 1e6) / 1e7) * 10).replace(/ /g, "\u00a0");   // a price never broken in two
      }).join(" · ");
    }
  }
  function onEvent(e) {
    var form = e.target.closest && e.target.closest("[data-own-price]");
    if (form) show(form);
  }
  document.addEventListener("input", onEvent);
  document.addEventListener("change", onEvent);
  document.addEventListener("toggle", function (e) {                  // the cell opened: its price at once, the field ready to type
    var cell = e.target;
    if (!cell.matches || !cell.matches("[data-own-cell]") || !cell.open) return;
    var form = cell.querySelector("[data-own-price]");
    if (form) { show(form); var f = form.querySelector("[name=value]"); if (f) f.focus(); }
  }, true);
  document.querySelectorAll("[data-own-price]").forEach(show);
})();
