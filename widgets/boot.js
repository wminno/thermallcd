/* thermallcd platform boot helper — injected into every widget iframe (D21).
   Gives widgets a single, auth-aware way to consume /api/metrics (PF1 snapshot)
   and enforces the stale contract (D23) platform-wide. */
(function () {
  "use strict";
  var TOKEN = window.TLCD_TOKEN || "";
  var callbacks = [];
  var timer = null;
  var lastTs = null;

  function render(state, data) {
    callbacks.forEach(function (cb) { cb(state, data); });
  }

  function markStale(root) {
    if (root.classList.contains("tlcd-stale")) return;
    root.classList.add("tlcd-stale");
  }

  function unmarkStale(root) {
    root.classList.remove("tlcd-stale");
  }

  function poll() {
    fetch("/api/metrics?token=" + encodeURIComponent(TOKEN))
      .then(function (r) { if (!r.ok) throw new Error("metrics " + r.status); return r.json(); })
      .then(function (snap) {
        var root = document.body;
        if (snap.ts === lastTs) {           // same tick: nothing moved
          if (Date.now() / 1000 - snap.ts > Math.max(5 * snap.refresh, 10)) markStale(root);
          return;
        }
        lastTs = snap.ts;
        unmarkStale(root);
        render("live", snap.data ? snap.data : snap);
        timer = setTimeout(poll, Math.max((snap.refresh || 1) * 1000, 250));
      })
      .catch(function () {
        markStale(document.body);
        render("stale", null);
        timer = setTimeout(poll, 5000);
      });
  }

  window.tlcd = {
    /** cb(state, data): state is "live" (first data / fresh ticks) or "stale". */
    onMetrics: function (cb) { callbacks.push(cb); },
    /** Fire cb once the DOM is parsed — widgets don't need DOMContentLoaded. */
    ready: function (cb) {
      if (document.readyState !== "loading") cb();
      else document.addEventListener("DOMContentLoaded", cb);
    },
  };

  window.tlcd.ready(function () { poll(); });
})();
