// Invoice editor: add/remove line rows, picking an item fills its description and
// price, and live line amounts and total in integer cents (BigInt, no floating
// point). Progressive enhancement only: "Save draft" is always enabled and the
// server re-validates everything.
(function () {
  "use strict";
  var form = document.getElementById("inv-form");
  if (!form) return;
  var body = document.getElementById("inv-rows");
  var template = document.getElementById("inv-row-template");
  var total = document.getElementById("inv-total");
  var PRICE = /^\$?\s*(\d{1,3}(,\d{3})+|\d+)(\.\d{1,2})?$/;
  var QTY = /^\d+(\.\d{1,3})?$/;

  // Price as integer cents: null when not a valid amount.
  function priceCents(text) {
    text = text.trim();
    if (!PRICE.test(text)) return null;
    var parts = text.replace(/[$,\s]/g, "").split(".");
    return BigInt(parts[0]) * 100n + BigInt(((parts[1] || "") + "00").slice(0, 2));
  }

  // Quantity as integer thousandths: null when not a valid quantity.
  function qtyMilli(text) {
    text = text.trim();
    if (!QTY.test(text)) return null;
    var parts = text.split(".");
    return BigInt(parts[0]) * 1000n + BigInt(((parts[1] || "") + "000").slice(0, 3));
  }

  // Cents of qty x price, rounded half up (both operands are non-negative).
  function amountCents(qty, price) {
    return (qty * price + 500n) / 1000n;
  }

  function money(cents) {
    var whole = String(cents / 100n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return whole + "." + ("0" + String(cents % 100n)).slice(-2);
  }

  function update() {
    var sum = 0n, valid = true;
    body.querySelectorAll("[data-inv-row]").forEach(function (row) {
      var cell = row.querySelector("[data-inv-amount]");
      var qty = qtyMilli(row.querySelector('[name="line_qty"]').value);
      var price = priceCents(row.querySelector('[name="line_unit_price"]').value);
      if (qty === null || price === null) {
        cell.textContent = "";
        if (row.querySelector('[name="line_item"]').value) valid = false;
        return;
      }
      var amount = amountCents(qty, price);
      cell.textContent = money(amount);
      sum += amount;
    });
    total.textContent = valid ? money(sum) : "";
  }

  function pick(select) {
    var option = select.options[select.selectedIndex];
    var row = select.closest("[data-inv-row]");
    if (!option || !option.value) return;
    row.querySelector('[name="line_description"]').value = option.getAttribute("data-description") || "";
    row.querySelector('[name="line_unit_price"]').value = option.getAttribute("data-price") || "";
    var qty = row.querySelector('[name="line_qty"]');
    if (qty.value.trim() === "") qty.value = "1";
  }

  document.getElementById("inv-add").addEventListener("click", function () {
    body.appendChild(template.content.firstElementChild.cloneNode(true));
    update();
  });
  body.addEventListener("click", function (event) {
    var button = event.target.closest("[data-inv-remove]");
    if (!button) return;
    button.closest("[data-inv-row]").remove();
    update();
  });
  form.addEventListener("change", function (event) {
    if (event.target.name === "line_item") pick(event.target);
    update();
  });
  form.addEventListener("input", update);
  update();
})();
