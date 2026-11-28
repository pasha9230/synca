(function () {
  'use strict';
  var form = document.querySelector('[data-wheel-filters]');
  if (!form) return;
  var details = form.querySelector('[data-wheel-details]');
  var more = form.querySelector('[data-wheel-more]');
  var car = form.querySelector('[data-wheel-car-panel]');
  var carToggle = form.querySelector('[data-wheel-car-toggle]');
  carToggle.addEventListener('click', function () {
    car.hidden = !car.hidden;
    carToggle.setAttribute('aria-expanded', String(!car.hidden));
  });
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-wheel-edit]');
    if (!button) return;
    var cell = document.getElementById('wheel-price-' + button.dataset.wheelEdit);
    if (cell) { cell.open = true; cell.scrollIntoView({block: 'nearest'}); var input = cell.querySelector('[name=value]'); if (input) input.focus(); }
  });
  details.addEventListener('toggle', function () { more.value = details.open ? '1' : '0'; });
  form.addEventListener('change', function (event) {
    var names = ['car_make', 'car_year', 'car_model', 'car_variant'];
    var index = names.indexOf(event.target.name);
    if (index >= 0) names.slice(index + 1).forEach(function (name) {
      var field = form.querySelector('[name="' + name + '"]');
      if (field) field.value = '';
    });
    if (event.target.matches('select, input[type="number"]')) form.requestSubmit();
  });
}());
