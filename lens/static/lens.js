/* GroundWork Lens — Minimal vanilla client behavior (zero CDN) */
(function () {
  "use strict";

  // Copy buttons in Setup wizard & Not-set-up page
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-copy-target], [data-copy-text]");
    if (!btn) return;
    var text = btn.getAttribute("data-copy-text");
    if (!text) {
      var targetSel = btn.getAttribute("data-copy-target");
      var el = targetSel ? document.querySelector(targetSel) : null;
      text = el ? (el.value !== undefined ? el.value : el.textContent) : "";
    }
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text.trim()).then(function () {
        var orig = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = orig; }, 1400);
      });
    }
  });

  // Setup wizard: "Let other GitHub accounts install this app" toggles the
  // manifest's `public` flag. Without JS the app is created private.
  document.addEventListener("change", function (ev) {
    var box = ev.target;
    if (!box.matches || !box.matches("[data-manifest-public]")) return;
    var field = document.querySelector(box.getAttribute("data-manifest-public"));
    if (!field) return;
    try {
      var manifest = JSON.parse(field.value);
      manifest["public"] = !!box.checked;
      field.value = JSON.stringify(manifest);
    } catch (e) {
      /* leave the manifest unchanged */
    }
  });

  // Confirmation prompt before approving a document
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var confirmMsg = form.getAttribute("data-confirm");
    if (confirmMsg && !window.confirm(confirmMsg)) {
      ev.preventDefault();
    }
  });

  // Live markdown preview on Document editor
  var previewBtn = document.getElementById("btn-live-preview");
  if (previewBtn) {
    previewBtn.addEventListener("click", function () {
      var editor = document.getElementById("doc-body-editor");
      var out = document.getElementById("doc-live-preview-pane");
      var csrf = document.querySelector('input[name="_csrf"]');
      if (!editor || !out || !csrf) return;
      var body = new URLSearchParams();
      body.set("_csrf", csrf.value);
      body.set("markdown", editor.value);
      fetch("/api/preview-markdown", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          out.innerHTML = data.html || "";
          out.classList.remove("hidden");
        })
        .catch(function () {});
    });
  }

  // Screenshot count & size guard before submit
  var imgInput = document.getElementById("request-images-input");
  var imgPreview = document.getElementById("request-images-info");
  if (imgInput && imgPreview) {
    imgInput.addEventListener("change", function () {
      var files = imgInput.files || [];
      if (files.length > 5) {
        imgPreview.textContent = "You can attach up to 5 images per request.";
        return;
      }
      var names = [];
      for (var i = 0; i < files.length; i++) {
        if (files[i].size > 5 * 1024 * 1024) {
          imgPreview.textContent = files[i].name + " is larger than 5 MB.";
          return;
        }
        names.push(files[i].name);
      }
      imgPreview.textContent = names.length ? "Selected: " + names.join(", ") : "";
    });
  }

  // Project picker: fill a repo's branch suggestions the first time its field is used
  document.addEventListener("focusin", function (ev) {
    var input = ev.target;
    if (!input.matches || !input.matches("input[data-branches-repo]")) return;
    if (input.getAttribute("data-branches-loaded")) return;
    input.setAttribute("data-branches-loaded", "1");
    var list = document.getElementById(input.getAttribute("list"));
    if (!list) return;
    var qs = new URLSearchParams();
    qs.set("host_id", input.getAttribute("data-branches-host"));
    qs.set("repo", input.getAttribute("data-branches-repo"));
    fetch("/api/branches?" + qs.toString(), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        (data.branches || []).forEach(function (name) {
          var opt = document.createElement("option");
          opt.value = name;
          list.appendChild(opt);
        });
      })
      .catch(function () {});
  });

  // Format ISO timestamps in viewer's local timezone
  var timeEls = document.querySelectorAll("time[data-iso]");
  for (var i = 0; i < timeEls.length; i++) {
    var iso = timeEls[i].getAttribute("data-iso");
    if (!iso) continue;
    var d = new Date(iso);
    if (!isNaN(d.getTime())) {
      timeEls[i].title = d.toLocaleString();
    }
  }
})();
