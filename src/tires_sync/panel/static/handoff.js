// «Передать под заказ» on a list (the calls): one window for every row; the row's button gives the window its address and the phone
document.addEventListener("click", function (e) {
  var btn = e.target.closest && e.target.closest("[data-handoff-action]");
  if (!btn) return;
  var win = document.getElementById("handoff");
  var form = win && win.querySelector("form");
  if (!form) return;
  form.setAttribute("action", btn.getAttribute("data-handoff-action"));
  var phone = form.querySelector("input[name=phone]");
  if (phone) phone.value = btn.getAttribute("data-handoff-phone") || "";
});
