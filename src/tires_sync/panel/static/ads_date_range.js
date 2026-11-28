(function () {
  'use strict';
  var form = document.querySelector('[data-range-form]');
  if (!form) return;
  var start = form.elements.start, end = form.elements.end;
  var calendar = form.querySelector('[data-calendar]');
  var label = form.querySelector('[data-month-label]');
  var today = end.max;
  function date(text) { var a = text.split('-').map(Number); return new Date(a[0], a[1] - 1, a[2]); }
  function iso(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  var month = date(end.value || today); month.setDate(1);
  var choosingEnd = false;
  function render() {
    calendar.textContent = '';
    label.textContent = month.toLocaleDateString('ru-RU', {month: 'long', year: 'numeric'});
    ['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС'].forEach(function (day, i) {
      var node = document.createElement('b'); node.textContent = day; if (i > 4) node.className = 'weekend'; calendar.appendChild(node);
    });
    var offset = (month.getDay() + 6) % 7;
    for (var i = 0; i < offset; i++) calendar.appendChild(document.createElement('span'));
    var count = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
    for (var n = 1; n <= count; n++) {
      var d = new Date(month.getFullYear(), month.getMonth(), n), value = iso(d);
      var b = document.createElement('button'); b.type = 'button'; b.textContent = n; b.dataset.date = value;
      b.disabled = value > today; if (d.getDay() === 0 || d.getDay() === 6) b.classList.add('weekend');
      if (value >= start.value && value <= end.value) b.classList.add('in-range');
      if (value === start.value || value === end.value) b.classList.add('selected');
      calendar.appendChild(b);
    }
  }
  calendar.addEventListener('click', function (e) {
    var value = e.target.dataset.date; if (!value) return;
    if (!choosingEnd || value < start.value) { start.value = value; end.value = value; choosingEnd = true; }
    else { end.value = value; choosingEnd = false; }
    render();
  });
  form.addEventListener('click', function (e) {
    var change = e.target.dataset.month;
    if (change) { month.setMonth(month.getMonth() + Number(change)); render(); }
    var preset = Number(e.target.dataset.preset);
    if (preset) { var d = date(today); end.value = iso(d); d.setDate(d.getDate() - preset + 1); start.value = iso(d); month = date(today); month.setDate(1); choosingEnd = false; render(); }
    if (e.target.hasAttribute('data-clear')) { start.value = ''; end.value = ''; choosingEnd = false; render(); }
  });
  form.addEventListener('change', function () { if (end.value) { month = date(end.value); month.setDate(1); } render(); });
  form.addEventListener('submit', function (e) { if (start.value > end.value) { e.preventDefault(); end.setCustomValidity('Конец периода должен быть не раньше начала'); end.reportValidity(); } else end.setCustomValidity(''); });
  end.addEventListener('input', function () { end.setCustomValidity(''); });
  render();
}());
