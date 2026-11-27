(function () {
  'use strict';
  var form = document.querySelector('[data-wheel-filters]');
  if (!form) return;
  var details = form.querySelector('[data-wheel-details]');
  var more = form.querySelector('[data-wheel-more]');
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-wheel-edit]');
    if (!button) return;
    var cell = document.getElementById('wheel-price-' + button.dataset.wheelEdit);
    if (cell) { cell.open = true; cell.scrollIntoView({block: 'nearest'}); var input = cell.querySelector('[name=value]'); if (input) input.focus(); }
  });
  details.addEventListener('toggle', function () { more.value = details.open ? '1' : '0'; });
  form.addEventListener('change', function (event) {
    if (event.target.matches('select, input[type="number"]')) form.requestSubmit();
  });
}());
