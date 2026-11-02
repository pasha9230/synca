/* «Шаблоны сообщений»: a click on a variable puts it into the text of the same form, where the cursor is. */
(function () {
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-insert]");
    if (!btn) return;
    var form = btn.closest("form"), area = form && form.querySelector("textarea[name=body]");
    if (!area || area.disabled) return;
    e.preventDefault();
    var put = btn.getAttribute("data-insert"), at = area.selectionStart, end = area.selectionEnd;
    if (typeof at !== "number") { at = end = area.value.length; }
    area.value = area.value.slice(0, at) + put + area.value.slice(end);
    area.focus();
    area.selectionStart = area.selectionEnd = at + put.length;
  });
})();
