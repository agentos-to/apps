const cTr = window.__cTr || [];
function hasTid(c) {
  const seen = new Set();
  let found = false;
  const walk = (o, depth) => {
    if (found || !o || depth > 4 || seen.has(o)) return;
    try {
      seen.add(o);
    } catch (e) {
      return;
    }
    try {
      for (const k of Object.keys(o).slice(0, 50)) {
        let v;
        try {
          v = o[k];
        } catch (e) {
          continue;
        }
        if (typeof v === "string" && (v.includes("r-585664302285623793") || v.includes("19f48a0360c4a115"))) {
          found = true;
          return;
        }
        if (v && typeof v === "object" && !(v instanceof Node) && depth < 4) walk(v, depth + 1);
      }
    } catch (e) {}
  };
  walk(c, 0);
  return found;
}
function methodsOf(c) {
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
  return methods;
}

// leave conversation: back to search list first
location.hash = "#search/subject%3AREPLYVERIFY-J1";
await new Promise((r) => setTimeout(r, 1500));

const candidates = [];
for (let i = 0; i < cTr.length; i++) {
  if (!hasTid(cTr[i])) continue;
  const methods = methodsOf(cTr[i]);
  candidates.push({
    i,
    methods,
    hasActivate: methods.includes("activate"),
    hasQrb: methods.includes("Qrb"),
    hasSelect: methods.some((m) => /select|open|activate|click|enter|show/i.test(m)),
  });
}

// Try activate on each that has it, then check hash/title
const tries = [];
for (const cand of candidates.filter((c) => c.hasActivate || c.hasQrb).slice(0, 8)) {
  const before = { hash: location.hash, title: document.title.slice(0, 40) };
  try {
    if (cand.hasActivate) {
      cTr[cand.i].activate();
    } else if (cand.hasQrb) {
      cTr[cand.i].Qrb();
    }
    await new Promise((r) => setTimeout(r, 1200));
    tries.push({
      i: cand.i,
      method: cand.hasActivate ? "activate" : "Qrb",
      before,
      after: { hash: location.hash, title: document.title.slice(0, 50) },
      opened: /REPLYVERIFY/i.test(document.title) && !/Search results/i.test(document.title),
    });
    if (tries[tries.length - 1].opened) break;
    // reset list
    location.hash = "#search/subject%3AREPLYVERIFY-J1";
    await new Promise((r) => setTimeout(r, 800));
  } catch (e) {
    tries.push({ i: cand.i, err: String(e).slice(0, 120) });
  }
}

// Also try activate on ALL with activate regardless of tid
const actAll = [];
if (!tries.some((t) => t.opened)) {
  for (let i = 0; i < Math.min(cTr.length, 40); i++) {
    const methods = methodsOf(cTr[i]);
    if (!methods.includes("activate")) continue;
    try {
      cTr[i].activate();
      await new Promise((r) => setTimeout(r, 600));
      actAll.push({
        i,
        hash: location.hash.slice(0, 80),
        title: document.title.slice(0, 40),
        opened: /REPLYVERIFY/i.test(document.title) && !/Search results/i.test(document.title),
      });
      if (actAll[actAll.length - 1].opened) break;
    } catch (e) {
      actAll.push({ i, err: String(e).slice(0, 80) });
    }
  }
}

return {
  nTr: cTr.length,
  nCand: candidates.length,
  candidates: candidates.slice(0, 15),
  tries,
  actAll: actAll.slice(0, 15),
  finalHash: location.hash,
  finalTitle: document.title,
};
