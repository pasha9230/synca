// The window of «Продать» of a record on «На ОТХ» (mypoint_oth_sale.html): the sum counts itself from the pieces; «Клиент отказался
// предоставлять имя и телефон» makes the name and the phone not needed. The server checks everything again.
(function () {
  var form = document.querySelector("[data-sale-form]");
  if (!form) return;
  var total = form.querySelector("[data-sale-total]");
  function rub(v) { return v === Math.round(v) ? String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, " ") : v.toFixed(2).replace(".", ","); }
  function count() {
    var sum = 0;
    form.querySelectorAll("[data-sale-line]").forEach(function (line) {
      var qty = parseInt(line.querySelector("[data-sale-qty]").value, 10);
      sum += (isNaN(qty) ? 0 : qty) * parseFloat(line.getAttribute("data-unit"));
    });
    total.value = rub(sum);
  }
  function none() {
    var off = form.querySelector("[data-sale-none]").checked;
    form.querySelectorAll("[data-sale-contact]").forEach(function (input) { input.required = !off; });
  }
  form.addEventListener("input", count);
  form.querySelector("[data-sale-none]").addEventListener("change", none);
  none();
})();
