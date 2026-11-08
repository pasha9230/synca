(function () {
  "use strict";
  // «Работать как точка»: the list closes on a click anywhere else and on Esc (the owner 2026-11-08)
  var box = document.querySelector("details.as-point");
  if (!box) return;
  document.addEventListener("click", function (e) { if (box.open && !box.contains(e.target)) box.open = false; });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && box.open) { box.open = false; box.querySelector("summary").focus(); } });
})();
