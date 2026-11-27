/* Frame editor: dynamic rows, live SVG preview, JSON submit with fallback. */
(function () {
  "use strict";

  var MAX = 12;
  var tbody = document.getElementById("frame-rows");
  var addBtn = document.getElementById("add-frame");
  var countSpan = document.getElementById("frame-count");
  var form = document.getElementById("job-form");
  var errBox = document.getElementById("form-errors");
  var svg = document.getElementById("editor-preview");
  var claimContext = document.getElementById("claim-context");
  if (!tbody || !form) return;

  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "className") n.className = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    if (text != null) n.textContent = text;
    return n;
  }

  function addRow(vals) {
    vals = vals || {};
    if (tbody.children.length >= MAX) return;
    var tr = el("tr");

    var idxTd = el("td", { className: "row-idx muted" });
    tr.appendChild(idxTd);

    function cell(name, placeholder, value, inputType) {
      var td = el("td");
      var inp = el("input", {
        type: inputType || (name === "fname" ? "text" : "number"),
        name: name,
        min: name === "fname" ? null : "0",
        step: "1",
        placeholder: placeholder || ""
      });
      if (value !== undefined && value !== null) inp.value = value;
      inp.addEventListener("input", refresh);
      td.appendChild(inp);
      tr.appendChild(td);
      return inp;
    }

    cell("fname", "名称(可空)", vals.name);
    cell("x", "0", vals.x);
    cell("y", "0", vals.y);
    cell("w", "宽", vals.w);
    cell("h", "高", vals.h);

    var actTd = el("td");
    var del = el("button", { type: "button", className: "btn danger small" }, "删");
    del.addEventListener("click", function () { tr.remove(); refresh(); });
    actTd.appendChild(del);
    tr.appendChild(actTd);

    tbody.appendChild(tr);
    refresh();
  }

  function renumber() {
    var rows = tbody.querySelectorAll("tr");
    rows.forEach(function (tr, i) {
      tr.querySelector(".row-idx").textContent = i + 1;
    });
    countSpan.textContent = "（" + rows.length + " / " + MAX + "）";
    addBtn.disabled = rows.length >= MAX;
  }

  function readRows() {
    return Array.prototype.map.call(tbody.querySelectorAll("tr"), function (tr) {
      var q = function (n) { var i = tr.querySelector('input[name="' + n + '"]'); return i ? i.value.trim() : ""; };
      return { name: q("fname"), x: q("x"), y: q("y"), w: q("w"), h: q("h") };
    });
  }

  function num(v) { var n = parseInt(v, 10); return isNaN(n) ? null : n; }

  var SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }

  function refreshPreview() {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var W = num(document.getElementById("f-w").value);
    var H = num(document.getElementById("f-h").value);
    var VW = 760, VH = 420, M = 28;
    if (!W || !H || W <= 0 || H <= 0) {
      var t = svgEl("text", { x: VW / 2, y: VH / 2, "text-anchor": "middle", fill: "#9ca3af", "font-size": 15 });
      t.textContent = "填入正整数纸张尺寸后显示预览";
      svg.appendChild(t);
      return;
    }
    var sc = Math.min((VW - 2 * M) / W, (VH - 2 * M) / H);
    var pw = W * sc, ph = H * sc;
    var ox = (VW - pw) / 2, oy = (VH - ph) / 2;
    function X(x) { return ox + x * sc; }
    function Y(y) { return oy + y * sc; }

    svg.appendChild(svgEl("rect", {
      x: ox, y: oy, width: pw, height: ph,
      fill: "#fffdf5", stroke: "#8a7a55", "stroke-width": 1.5
    }));

    var palette = ["#2563eb", "#0891b2", "#7c3aed", "#db2777", "#ea580c",
      "#16a34a", "#ca8a04", "#dc2626", "#4f46e5", "#0d9488", "#9333ea", "#65a30d"];

    readRows().forEach(function (r, i) {
      var x = num(r.x), y = num(r.y), w = num(r.w), h = num(r.h);
      if (x === null || y === null || w === null || h === null || w <= 0 || h <= 0) return;
      var out = x < 0 || y < 0 || x + w > W || y + h > H;
      var fill = out ? "#fee2e2" : palette[i % palette.length];
      var rect = svgEl("rect", {
        x: X(x), y: Y(y), width: Math.max(1.5, w * sc), height: Math.max(1.5, h * sc),
        fill: out ? fill : fill, "fill-opacity": out ? 0.9 : 0.16,
        stroke: out ? "#b91c1c" : fill, "stroke-width": 1.6
      });
      var title = svgEl("title", {});
      title.textContent = (r.name || ("画" + (i + 1))) +
        "  (" + x + "," + y + ") " + w + "×" + h + (out ? " 超出纸张!" : "");
      rect.appendChild(title);
      svg.appendChild(rect);

      if (w * sc > 34 && h * sc > 16) {
        var txt = svgEl("text", {
          x: X(x + w / 2), y: Y(y + h / 2) + 4,
          "text-anchor": "middle", "font-size": 12, "font-weight": 700,
          fill: out ? "#b91c1c" : fill
        });
        txt.textContent = r.name || ("画" + (i + 1));
        svg.appendChild(txt);
      }
    });
  }

  function refresh() { renumber(); refreshPreview(); }

  addBtn.addEventListener("click", function () { addRow(); });
  document.getElementById("f-w").addEventListener("input", refreshPreview);
  document.getElementById("f-h").addEventListener("input", refreshPreview);

  // Restore after a 422 server render, otherwise start with blank rows.
  var oldNode = document.getElementById("old-payload");
  var restored = null;
  if (oldNode) {
    try { restored = JSON.parse(oldNode.textContent); } catch (e) { restored = null; }
  }
  if (restored) {
    document.getElementById("f-name").value = restored.name || "";
    document.getElementById("f-w").value = restored.paper_w || "";
    document.getElementById("f-h").value = restored.paper_h || "";
    (restored.frames || []).forEach(addRow);
    if (!restored.frames || !restored.frames.length) { addRow(); addRow(); addRow(); }
  } else if (claimContext) {
    // Claim flow: dimensions are server-rendered and locked; start with a
    // few blank frame rows instead of overwriting the leftover dimensions.
    addRow(); addRow(); addRow();
  } else {
    // Convenient starting point.
    document.getElementById("f-w").value = 100;
    document.getElementById("f-h").value = 100;
    for (var i = 0; i < 5; i++) addRow();
  }
  refresh();

  form.addEventListener("submit", function (ev) {
    // Let normal multipart/form-urlencoded submission proceed if fetch/JSON
    // is unavailable; here we enhance with JSON for inline errors.
    ev.preventDefault();
    errBox.style.display = "none";
    errBox.innerHTML = "";

    var payload = {
      name: document.getElementById("f-name").value,
      paper_w: document.getElementById("f-w").value,
      paper_h: document.getElementById("f-h").value,
      frames: readRows().filter(function (r) {
        return r.x || r.y || r.w || r.h;
      })
    };

    if (claimContext) {
      payload.claim_source_job_id = claimContext.dataset.sourceJob;
      payload.claim_piece_id = claimContext.dataset.sourcePiece;
    }

    fetch(form.action, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (resp) {
      return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
    }).then(function (r) {
      if (r.ok && r.data.ok) { window.location.href = r.data.redirect; return; }
      var errs = (r.data && r.data.errors) || ["提交失败，请检查输入。"];
      errBox.innerHTML = "<strong>录入有误，未创建：</strong><ul><li>" +
        errs.map(function (e) { return String(e).replace(/[<>&]/g, ""); }).join("</li><li>") +
        "</li></ul>";
      errBox.style.display = "block";
      window.scrollTo({ top: 0, behavior: "smooth" });
    }).catch(function () {
      // Network/JS failure: programmatic submit bypasses this handler and
      // performs a native form-urlencoded POST.
      form.submit();
    });
  });
})();
