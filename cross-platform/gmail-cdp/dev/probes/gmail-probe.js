// gmail-probe.js — session RE helpers for Wall 4 / threaded-reply. Prepended via geval with toolkit.
// Adds window.__gprobe after toolkit.
(function () {
  if (globalThis.__gprobe && __gprobe.__v === 3) return;
  const gp = (globalThis.__gprobe = { __v: 3 });

  gp.jLnInstall = function () {
    const O = (_m.jLn && _m.jLn.__orig) || _m.jLn;
    if (!O) return { ok: false, err: "no_jLn" };
    const W = function (...a) {
      (window.__c = window.__c || []).push(this);
      return O.apply(this, a);
    };
    W.prototype = O.prototype;
    Object.setPrototypeOf(W, O);
    for (const k of Object.getOwnPropertyNames(O)) {
      if (!["length", "name", "prototype"].includes(k)) try { W[k] = O[k]; } catch (e) {}
    }
    W.__agmCap = true;
    W.__orig = O;
    _m.jLn = W;
    window.__c = window.__c || [];
    return { ok: true, prevCap: window.__c.length };
  };

  gp.jLnClear = () => { window.__c = []; return 0; };
  gp.jLnCount = () => (window.__c || []).length;

  gp.msgViews = function () {
    return (window.__c || []).filter(
      (c) => typeof c.Za === "function" && c.Ca !== undefined && c.ha !== undefined
    );
  };

  gp.classifyCaps = function () {
    const caps = window.__c || [];
    return caps.map((c, i) => {
      const methods = [];
      let p = c;
      for (let d = 0; d < 5 && p; d++) {
        for (const k of Object.getOwnPropertyNames(p)) {
          try {
            if (typeof p[k] === "function" && k !== "constructor") methods.push(k);
          } catch (e) {}
        }
        p = Object.getPrototypeOf(p);
      }
      const hits = [];
      const seen = new Set();
      const walk = (o, path, depth) => {
        if (!o || depth > 3 || seen.has(o)) return;
        try { seen.add(o); } catch (e) { return; }
        try {
          for (const k of Object.keys(o).slice(0, 50)) {
            let v; try { v = o[k]; } catch (e) { continue; }
            if (typeof v === "string") {
              if (
                v.startsWith("thread-") ||
                v.startsWith("msg-") ||
                /^[0-9a-f]{16}$/.test(v) ||
                /^[A-Za-z0-9]{20,}$/.test(v) && /perm|Ktbx/i.test(v) ||
                v.includes("REPLYVERIFY")
              )
                hits.push(path + "." + k + "=" + v.slice(0, 60));
            } else if (v && typeof v === "object" && !(v instanceof Node) && depth < 3)
              walk(v, path + "." + k, depth + 1);
          }
        } catch (e) {}
      };
      walk(c, "c", 0);
      return {
        i,
        hasZa: typeof c.Za === "function",
        hasCa: c.Ca !== undefined,
        hasHa: c.ha !== undefined,
        nMethods: methods.length,
        methods: methods.slice(0, 40),
        hits: hits.slice(0, 20),
      };
    });
  };

  gp.listRows = function () {
    return [...document.querySelectorAll("[data-legacy-thread-id]")].map((el) => ({
      leg: el.getAttribute("data-legacy-thread-id"),
      tid: el.getAttribute("data-thread-id"),
      text: (el.innerText || "").replace(/\s+/g, " ").slice(0, 80),
      href: (el.closest("a") || el.querySelector("a") || {}).href || null,
      jslog: (el.getAttribute("jslog") || "").slice(0, 160),
    }));
  };

  // Heuristic: after a UI open, read permId from hash `#inbox/<permId>` or `#all/<permId>`
  gp.hashPerm = function () {
    const m = (location.hash || "").match(/#(inbox|all|label\/[^/]+|search\/[^/]+)\/([A-Za-z0-9]+)/);
    if (!m) return null;
    // ignore hex-only (doesn't open)
    if (/^[0-9a-f]{14,18}$/i.test(m[2])) return { kind: "hex_ignored", token: m[2], view: m[1] };
    return { kind: "perm", token: m[2], view: m[1], hash: location.hash };
  };

  // Intercept history / pushState / hashchange to log navigation targets
  gp.watchNav = function () {
    if (gp._navWatch) return { already: true };
    gp._navLog = [];
    const log = (why, u) => {
      gp._navLog.push({ t: Date.now(), why, u: String(u).slice(0, 200), hash: location.hash });
    };
    const _ps = history.pushState.bind(history);
    const _rs = history.replaceState.bind(history);
    history.pushState = function (s, t, u) { log("pushState", u); return _ps(s, t, u); };
    history.replaceState = function (s, t, u) { log("replaceState", u); return _rs(s, t, u); };
    window.addEventListener("hashchange", () => log("hashchange", location.hash));
    gp._navWatch = true;
    return { ok: true };
  };
  gp.navLog = () => (gp._navLog || []).slice(-30);

  // Wrap location.hash setter if configurable — often not; nav watch covers pushState.

  // Open conversation by known-good permId (re-derive after ROT via click once)
  gp.openByPerm = function (permId) {
    location.hash = "#inbox/" + permId;
    return { hash: location.hash };
  };

  // RE: click a list row by legacy hex and capture resulting perm hash
  gp.findRowEl = function (hexOrSubj) {
    const rows = [...document.querySelectorAll("[data-legacy-thread-id]")];
    const hit = rows.find(
      (el) =>
        el.getAttribute("data-legacy-thread-id") === hexOrSubj ||
        (el.innerText || "").includes(hexOrSubj)
    );
    return hit || null;
  };

  gp.permTokens = {
    nHl: () => _m.nHl, // getItemServerPermIdByLegacyThreadStorageId
    mHl: () => _m.mHl,
    P8: () => _m.P8, // server_perm_id
  };

  // Scan Closure string table for related tokens
  gp.permStrings = function () {
    const out = [];
    for (const k of Object.keys(_m)) {
      try {
        const v = _m[k];
        if (typeof v === "string" && /perm|LegacyThread|StorageId|server_perm/i.test(v))
          out.push({ k, v: v.slice(0, 120) });
      } catch (e) {}
    }
    return out;
  };

  // Find functions referencing a marker string
  gp.fnRefs = function (marker, limit = 20) {
    const out = [];
    for (const k of Object.keys(_m)) {
      try {
        const v = _m[k];
        if (typeof v !== "function") continue;
        const s = v.toString();
        if (s.includes(marker)) out.push({ k, len: s.length, snip: s.slice(0, 240) });
      } catch (e) {}
      if (out.length >= limit) break;
    }
    return out;
  };
})();
