// Owner page: a live total of the ticked expenses in integer cents (BigInt, no floating
// point) and Reimburse disabled while nothing is ticked. Progressive enhancement only: the
// server re-validates everything.
(function () {
  "use strict";
  var form = document.getElementById("reimburse-form");
  if (!form) return;
  var rows = document.getElementById("owner-rows");
  var total = document.getElementById("ticked-total");
  var save = document.getElementById("reimburse-save");
  if (!rows || !total || !save) return;

  function money(cents) {
    var whole = String(cents / 100n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return "$" + whole + "." + ("0" + String(cents % 100n)).slice(-2);
  }

  function update() {
    var sum = 0n;
    rows.querySelectorAll('input[name="expense_id"]').forEach(function (box) {
      if (box.checked && /^\d+$/.test(box.dataset.cents || "")) sum += BigInt(box.dataset.cents);
    });
    total.textContent = money(sum);
    save.disabled = sum === 0n;
  }

  rows.addEventListener("change", update);
  update();
})();
