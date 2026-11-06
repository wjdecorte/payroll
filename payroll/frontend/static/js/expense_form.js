// Expense form: add and remove split-line rows, a live total of the line amounts in integer
// cents (BigInt, no floating point), and Save kept enabled (the server re-validates everything).
// Progressive enhancement only.
(function () {
  "use strict";
  var form = document.getElementById("exp-form");
  if (!form) return;
  var body = document.getElementById("exp-rows");
  var template = document.getElementById("exp-row-template");
  var total = document.getElementById("exp-total");
  var add = document.getElementById("exp-add");
  if (!body || !template || !total || !add) return;
  var AMOUNT = /^\$?\s*(\d{1,3}(,\d{3})+|\d+)(\.\d{1,2})?$/;
  var MAX_ROWS = parseInt(add.getAttribute("data-max") || "100", 10);

  // Amount as integer cents: null when not a valid amount.
  function cents(text) {
    text = text.trim();
    if (!AMOUNT.test(text)) return null;
    var parts = text.replace(/[$,\s]/g, "").split(".");
    return BigInt(parts[0]) * 100n + BigInt(((parts[1] || "") + "00").slice(0, 2));
  }

  function money(value) {
    var whole = String(value / 100n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return "$" + whole + "." + ("0" + String(value % 100n)).slice(-2);
  }

  function rows() {
    return body.querySelectorAll("[data-exp-row]");
  }

  function update() {
    var sum = 0n, bad = false;
    rows().forEach(function (row) {
      var text = row.querySelector('[name="line_amount"]').value;
      if (text.trim() === "") return;
      var value = cents(text);
      if (value === null) bad = true;
      else sum += value;
    });
    total.textContent = bad ? "Check the amounts" : money(sum);
    add.disabled = rows().length >= MAX_ROWS;
  }

  add.addEventListener("click", function () {
    if (rows().length >= MAX_ROWS) return;
    body.appendChild(template.content.firstElementChild.cloneNode(true));
    update();
  });
  body.addEventListener("click", function (event) {
    var button = event.target.closest("[data-exp-remove]");
    if (!button) return;
    var row = button.closest("[data-exp-row]");
    if (rows().length > 1) {
      row.remove();
    } else {
      row.querySelectorAll("input").forEach(function (input) { input.value = ""; });
      row.querySelectorAll("select").forEach(function (select) { select.selectedIndex = 0; });
    }
    update();
  });
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
})();
