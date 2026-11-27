/* Job operation page: fill registration form from a planned step and
   show the strict interior range for the selected piece/orientation. */
(function () {
  "use strict";

  var pieceSel = document.getElementById("cut-piece");
  var orientInputs = document.querySelectorAll('input[name="orient"]');
  var posInput = document.getElementById("cut-pos");
  var suggestedInput = document.getElementById("cut-suggested");
  var posLabel = document.getElementById("pos-label");
  var rangeHint = document.getElementById("range-hint");

  function currentOrient() {
    var checked = document.querySelector('input[name="orient"]:checked');
    return checked ? checked.value : "V";
  }

  function updateHint() {
    if (!pieceSel || !pieceSel.selectedOptions.length) return;
    var opt = pieceSel.selectedOptions[0];
    var o = currentOrient();
    if (o === "V") {
      posLabel.textContent = "竖切位置 x (mm，整数)";
      rangeHint.textContent =
        "纸片 #" + pieceSel.value + " 的竖切有效整数刀位：" +
        opt.dataset.x0 + " < x < " + opt.dataset.x1;
      if (posInput) posInput.placeholder = "例如 " + (Number(opt.dataset.x0) + 1);
    } else {
      posLabel.textContent = "横切位置 y (mm，整数)";
      rangeHint.textContent =
        "纸片 #" + pieceSel.value + " 的横切有效整数刀位：" +
        opt.dataset.y0 + " < y < " + opt.dataset.y1;
      if (posInput) posInput.placeholder = "例如 " + (Number(opt.dataset.y0) + 1);
    }
  }

  if (pieceSel) pieceSel.addEventListener("change", function () {
    // Changing the piece invalidates the recommended baseline.
    if (suggestedInput) suggestedInput.value = "";
    updateHint();
  });
  orientInputs.forEach(function (r) {
    r.addEventListener("change", updateHint);
  });
  updateHint();

  document.querySelectorAll(".fill-step").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var piece = btn.dataset.piece;
      var orient = btn.dataset.orient;
      var pos = btn.dataset.pos;

      if (pieceSel) {
        pieceSel.value = piece;
        // The root planned piece must be one of the active options.
        if (pieceSel.value !== piece) {
          alert("推荐纸片 #" + piece + " 已不是活纸片，页面将刷新最新切序。");
          window.location.reload();
          return;
        }
      }
      orientInputs.forEach(function (r) { r.checked = (r.value === orient); });
      updateHint();
      if (posInput) { posInput.value = pos; posInput.focus(); posInput.select(); }
      if (suggestedInput) suggestedInput.value = pos;
      // Smooth-scroll the form into view on small screens.
      var form = document.getElementById("cut-form");
      if (form && form.scrollIntoView) form.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });
})();
