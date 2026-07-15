const t0 = Date.now();
let wrappedAt = null;
let target = null;
for (let i = 0; i < 250; i++) {
  const M = window._m;
  if (M && typeof M.Tr === "function" && !M.Tr.__agmCap) {
    const O = M.Tr;
    const W = function (...a) {
      (window.__cTr = window.__cTr || []).push(this);
      return O.apply(this, a);
    };
    W.prototype = O.prototype;
    Object.setPrototypeOf(W, O);
    for (const k of Object.getOwnPropertyNames(O)) {
      if (!["length", "name", "prototype"].includes(k)) try { W[k] = O[k]; } catch (e) {}
    }
    W.__agmCap = true;
    W.__orig = O;
    M.Tr = W;
    window.__cTr = [];
    wrappedAt = Date.now() - t0;
    target = "Tr";
    break;
  }
  await new Promise((r) => setTimeout(r, 20));
}
// also wrap jLn if present
if (window._m && typeof _m.jLn === "function" && !_m.jLn.__agmCap) {
  const O = _m.jLn;
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
  window.__c = [];
}

const gdl = Date.now() + 20000;
while (!(window.gmonkey || window.GM_APP_NAME) && Date.now() < gdl) await new Promise((r) => setTimeout(r, 80));
await new Promise((r) => setTimeout(r, 2500));
location.hash = "#search/subject%3AREPLYVERIFY-J1";
await new Promise((r) => setTimeout(r, 2000));

const cTr = window.__cTr || [];
const cJ = window.__c || [];

// Classify Tr captures that have thread ids
function hitsOf(c) {
  const hits = [];
  const seen = new Set();
  const walk = (o, path, depth) => {
    if (!o || depth > 3 || seen.has(o)) return;
    try {
      seen.add(o);
    } catch (e) {
      return;
    }
    try {
      for (const k of Object.keys(o).slice(0, 40)) {
        let v;
        try {
          v = o[k];
        } catch (e) {
          continue;
        }
        if (typeof v === "string" && (v.startsWith("thread-") || /^[0-9a-f]{16}$/.test(v) || v.includes("REPLYVERIFY")))
          hits.push(path + "." + k + "=" + v.slice(0, 50));
        else if (v && typeof v === "object" && !(v instanceof Node) && depth < 3) walk(v, path + "." + k, depth + 1);
      }
    } catch (e) {}
  };
  walk(c, "c", 0);
  return hits;
}

const withHits = [];
for (let i = 0; i < cTr.length && withHits.length < 10; i++) {
  const h = hitsOf(cTr[i]);
  if (h.length) {
    const methods = [];
    let p = cTr[i];
    for (let d = 0; d < 4 && p; d++) {
      for (const k of Object.getOwnPropertyNames(p)) {
        try {
          if (typeof p[k] === "function" && k !== "constructor") methods.push(k);
        } catch (e) {}
      }
      p = Object.getPrototypeOf(p);
    }
    withHits.push({ i, hits: h.slice(0, 10), methods: methods.slice(0, 25), nMethods: methods.length });
  }
}

return {
  wrappedAt,
  target,
  nTr: cTr.length,
  nJ: cJ.length,
  nWithHits: withHits.length,
  withHits,
  nRows: document.querySelectorAll("[data-legacy-thread-id]").length,
  hash: location.hash,
  title: document.title,
};
