const forms = [
  "#all/thread-a:r-585664302285623793",
  "#inbox/thread-a:r-585664302285623793",
  "#all/#thread-a:r-585664302285623793",
  "#search/rfc822msgid:bogus",
  "#inbox/19f48a0360c4a115",
  "#all/19f48a0360c4a115",
];
const results = [];
for (const h of forms) {
  location.hash = h;
  await new Promise((r) => setTimeout(r, 1200));
  results.push({
    tried: h,
    landed: location.hash,
    title: document.title.slice(0, 60),
    open: /REPLYVERIFY/i.test(document.title) && !/Search results/i.test(document.title),
  });
}
// known good control
location.hash = "#inbox/KtbxLrjRhsRPVcMPHPJcNmghLBjdwlCSNq";
await new Promise((r) => setTimeout(r, 1500));
results.push({
  tried: "known-perm",
  landed: location.hash,
  title: document.title.slice(0, 60),
  open: /REPLYVERIFY/i.test(document.title),
});

// Analyze row-like controller i from __cTr that has our thread id - dump method sources snippet for open-ish names
const cTr = window.__cTr || [];
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
        if (typeof v === "string" && (v.includes("r-585664302285623793") || v.includes("19f48a0360c4a115")))
          hits.push(path + "." + k + "=" + v.slice(0, 60));
        else if (v && typeof v === "object" && !(v instanceof Node) && depth < 3) walk(v, path + "." + k, depth + 1);
      }
    } catch (e) {}
  };
  walk(c, "c", 0);
  return hits;
}
const rows = [];
for (let i = 0; i < cTr.length; i++) {
  const h = hitsOf(cTr[i]);
  if (!h.length) continue;
  const methods = [];
  let p = cTr[i];
  for (let d = 0; d < 5 && p; d++) {
    for (const k of Object.getOwnPropertyNames(p)) {
      try {
        if (typeof p[k] === "function" && k !== "constructor") methods.push(k);
      } catch (e) {}
    }
    p = Object.getPrototypeOf(p);
  }
  // methods whose source mentions open / navigate / select / click / activate / show / perm
  const interesting = [];
  for (const m of methods) {
    try {
      const s = cTr[i][m].toString();
      if (/open|nav|select|click|activate|show|perm|thread|hash|location|goto|enter/i.test(s) && s.length < 2000)
        interesting.push({ m, snip: s.slice(0, 180) });
    } catch (e) {}
  }
  rows.push({ i, hits: h.slice(0, 6), nMethods: methods.length, methods: methods.slice(0, 35), interesting: interesting.slice(0, 12) });
}
return { results, nRowCandidates: rows.length, rows: rows.slice(0, 6) };
