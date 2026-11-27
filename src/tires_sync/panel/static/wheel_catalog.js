(function () {
  'use strict';
  var form = document.querySelector('[data-wheel-filters]');
  if (!form) return;
  var details = form.querySelector('[data-wheel-details]');
  var more = form.querySelector('[data-wheel-more]');
  details.addEventListener('toggle', function () { more.value = details.open ? '1' : '0'; });
  form.addEventListener('change', function (event) {
    if (event.target.matches('select, input[type="number"]')) form.requestSubmit();
  });
}());
