// «Изменить цену просмотра» (ads_bid.html): «было → стало» while choosing, and the checks of the server before the press: the bounds of
// Avito, «Подтверждаю» for a jump of the price or a daily limit above the threshold. The server checks the same again.
(function () {
  var box = document.getElementById("ads-bid");
  var form = box && box.querySelector("form[data-bid-form]");
  if (!form) return;
  function num(text) {
    var t = String(text || "").replace(/[\s ₽]/g, "").replace(",", ".");
    return t === "" || isNaN(Number(t)) ? null : Number(t);
  }
  function rub(v) { return v.toFixed(2).replace(".", ",") + " ₽"; }
  var min = num(box.dataset.min), max = num(box.dataset.max), was = num(box.dataset.wasBid), drr = num(box.dataset.wasDrr);
  var warn = num(box.dataset.warn), jump = num(box.dataset.jump);
  var after = form.querySelector("[data-bid-after]"), note = form.querySelector("[data-bid-warn]");
  function chosen() {
    var pick = form.querySelector("input[name=pick]:checked");
    if (!pick) return null;
    return pick.value === "own" ? num(form.elements.own.value) : num(pick.value);
  }
  function problems() {
    var bid = chosen(), limit = num(form.elements.limit.value), out = [], need = [];
    if (bid === null || bid <= 0) out.push("выберите цену или впишите своё значение");
    else if ((min !== null && bid < min) || (max !== null && bid > max)) out.push("цена вне границ Авито: от " + rub(min) + " до " + rub(max));
    if (limit === null || limit <= 0) out.push("впишите, сколько тратить в день");
    if (bid && was && Math.abs(bid - was) / was > jump) need.push("цена меняется больше чем на " + Math.round(jump * 100) + " %");
    if (limit && limit > warn) need.push("лимит выше " + rub(warn));
    return {bid: bid, limit: limit, out: out, need: need};
  }
  function show() {
    var p = problems();
    var text = p.bid ? rub(p.bid) : "—";
    if (p.limit) text += ", лимит " + rub(p.limit);
    if (p.bid && was && drr !== null) text += ", ДРР ≈ " + (drr * p.bid / was).toFixed(1).replace(".", ",") + " % (если просмотров будет столько же)";
    after.textContent = text;
    var lines = p.out.slice();
    if (p.need.length && !form.elements.confirm.checked) lines.push("нужна галочка «Подтверждаю»: " + p.need.join(", "));
    note.textContent = lines.join("; ");
    note.hidden = !lines.length;
    return lines.length === 0;
  }
  form.addEventListener("input", show);
  form.addEventListener("change", show);
  form.addEventListener("submit", function (e) {
    if (!show()) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
  show();
})();
