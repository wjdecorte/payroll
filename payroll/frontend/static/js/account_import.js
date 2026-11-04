// QBO chart import preview: tick or untick accounts, keeping sub-accounts with their
// parents (unticking a parent unticks its children; ticking a child ticks its parents),
// with a live count on the Import button. Progressive enhancement only: the checkboxes
// submit without this script and the server re-validates the selection.
(function () {
  "use strict";
  var button = document.getElementById("import-button");
  var boxes = Array.prototype.slice.call(
    document.querySelectorAll('input[name="line"][data-key]')
  );
  if (!button || boxes.length === 0) return;

  function isUnder(child, parent) {
    return child.dataset.key.indexOf(parent.dataset.key + ":") === 0;
  }

  function refresh() {
    var ticked = 0;
    boxes.forEach(function (box) {
      if (box.checked) ticked += 1;
      box.closest("tr").classList.toggle("text-muted", !box.checked);
    });
    button.disabled = ticked === 0;
    button.textContent = "Import " + ticked + (ticked === 1 ? " account" : " accounts");
  }

  function setAll(checked) {
    boxes.forEach(function (box) {
      box.checked = checked;
    });
    refresh();
  }

  boxes.forEach(function (box) {
    box.addEventListener("change", function () {
      boxes.forEach(function (other) {
        if (!box.checked && isUnder(other, box)) other.checked = false;
        if (box.checked && isUnder(box, other)) other.checked = true;
      });
      refresh();
    });
  });

  var all = document.getElementById("select-all");
  var none = document.getElementById("select-none");
  if (all) all.addEventListener("click", function () { setAll(true); });
  if (none) none.addEventListener("click", function () { setAll(false); });
  refresh();
})();
