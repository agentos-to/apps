const t0 = Date.now();
let wrappedAt = null;
for (let i = 0; i < 120; i++) {
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
    window.__c = window.__c || [];
    wrappedAt = Date.now() - t0;
    break;
  }
  if (window._m && _m.jLn && _m.jLn.__agmCap) {
    wrappedAt = Date.now() - t0;
    break;
  }
  await new Promise((r) => setTimeout(r, 40));
}
// wait for gmonkey + settling
const gdl = Date.now() + 15000;
while (!(window.gmonkey || window.GM_APP_NAME) && Date.now() < gdl) {
  await new Promise((r) => setTimeout(r, 100));
}
await new Promise((r) => setTimeout(r, 2500));
// force search list too
location.hash = "#search/subject%3AREPLYVERIFY-J1";
await new Promise((r) => setTimeout(r, 2000));

const caps = window.__c || [];
const shapes = caps.map((c, i) => {
  const methods = [];
  let p = c;
  for (let d = 0; d < 4 && p; d++) {
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
        if (typeof v === "string" && (v.startsWith("thread-") || /^[0-9a-f]{16}$/.test(v) || v.includes("REPLYVERIFY") || v.startsWith("Ktbx")))
          hits.push(path + "." + k + "=" + v.slice(0, 60));
        else if (v && typeof v === "object" && !(v instanceof Node) && depth < 3) walk(v, path + "." + k, depth + 1);
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
    methods: methods.slice(0, 30),
    hits: hits.slice(0, 12),
  };
});

return {
  wrappedAt,
  readyMs: Date.now() - t0,
  total: caps.length,
  nMsgView: shapes.filter((s) => s.hasZa && s.hasCa && s.hasHa).length,
  nWithHits: shapes.filter((s) => s.hits.length).length,
  withHits: shapes.filter((s) => s.hits.length).slice(0, 8),
  samples: shapes.slice(0, 6),
  sigs: (() => {
    const m = {};
    for (const s of shapes) {
      const sig = s.methods.slice(0, 10).join(",") + "|za=" + s.hasZa;
      m[sig] = (m[sig] || 0) + 1;
    }
    return Object.entries(m)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 15);
  })(),
  hash: location.hash,
  title: document.title,
};
