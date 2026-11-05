// Deposit form: a live total of the ticked payments in integer cents (BigInt, no
// floating point) and Save disabled while nothing is ticked. Progressive enhancement
// only: the server re-validates everything.
(function () {
  "use strict";
  var form = document.getElementById("dep-form");
  if (!form) return;
  var rows = document.getElementById("dep-rows");
  var total = document.getElementById("dep-total");
  var save = document.getElementById("dep-save");
  if (!rows || !total || !save) return;

  function money(cents) {
    var whole = String(cents / 100n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return "$" + whole + "." + ("0" + String(cents % 100n)).slice(-2);
  }

  function update() {
    var sum = 0n;
    rows.querySelectorAll('input[name="payment_id"]').forEach(function (box) {
      if (box.checked && /^\d+$/.test(box.dataset.cents || "")) sum += BigInt(box.dataset.cents);
    });
    total.textContent = money(sum);
    save.disabled = sum === 0n;
  }

  rows.addEventListener("change", update);
  update();
})();
