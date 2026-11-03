// Journal entry editor: add/remove line rows, live totals, and a Submit button
// that stays disabled until the entry balances with at least two filled lines.
// Progressive enhancement only: the server re-validates everything.
(function () {
  "use strict";
  var form = document.getElementById("je-form");
  if (!form) return;
  var body = document.getElementById("je-rows");
  var template = document.getElementById("je-row-template");
  var submit = document.getElementById("je-submit");
  var AMOUNT = /^\$?\s*(\d{1,3}(,\d{3})+|\d+)(\.\d{1,2})?$/;

  // Amounts as integer cents (no floating point): null when not a valid amount.
  function cents(text) {
    text = text.trim();
    if (text === "") return 0;
    if (!AMOUNT.test(text)) return null;
    var parts = text.replace(/[$,\s]/g, "").split(".");
    var frac = ((parts[1] || "") + "00").slice(0, 2);
    return parseInt(parts[0], 10) * 100 + parseInt(frac, 10);
  }

  function money(value) {
    var sign = value < 0 ? "-" : "";
    value = Math.abs(value);
    var whole = String(Math.floor(value / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return sign + whole + "." + ("0" + (value % 100)).slice(-2);
  }

  function update() {
    var debit = 0, credit = 0, filled = 0, valid = true;
    body.querySelectorAll("[data-je-row]").forEach(function (row) {
      var account = row.querySelector('[name="line_account"]').value;
      var d = cents(row.querySelector('[name="line_debit"]').value);
      var c = cents(row.querySelector('[name="line_credit"]').value);
      if (d === null || c === null) { valid = false; return; }
      if (!account && !d && !c) return;
      if (!account || (d && c) || (!d && !c)) { valid = false; return; }
      filled += 1;
      debit += d;
      credit += c;
    });
    document.getElementById("je-total-debit").textContent = money(debit);
    document.getElementById("je-total-credit").textContent = money(credit);
    document.getElementById("je-difference").textContent = money(debit - credit);
    submit.disabled = !(valid && filled >= 2 && debit === credit && debit > 0);
  }

  document.getElementById("je-add").addEventListener("click", function () {
    body.appendChild(template.content.firstElementChild.cloneNode(true));
    update();
  });
  body.addEventListener("click", function (event) {
    var button = event.target.closest("[data-je-remove]");
    if (!button) return;
    button.closest("[data-je-row]").remove();
    update();
  });
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
})();
