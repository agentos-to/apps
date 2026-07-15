(async () => {
  /* Decode jslog base64 for thread id — already have data attrs.
     Look for attachment flag in bv stub deeper fields. */
  const g = window.__agmail;
  /* force fresh bv via search */
  location.hash = "#search/subject:AOS-ATT-1783690638";
  await new Promise((r) => setTimeout(r, 3000));
  /* install hook if missing */
  if (!g || !g.bv || !g.bv.length) {
    return { err: "no_bv", hasAgmail: !!g, bvLen: g && g.bv && g.bv.length };
  }
  const p = JSON.parse(g.bv[g.bv.length - 1]);
  const stubs = [];
  const find = (n, d) => {
    if (d > 12 || !Array.isArray(n)) return;
    if (typeof n[0] === "string" && typeof n[3] === "string" && /^thread-[af]:/.test(n[3])) {
      stubs.push(n);
      return;
    }
    for (const e of n) find(e, d + 1);
  };
  find(p, 0);
  const hit = stubs.find((t) => /AOS-ATT-1783690638/.test(t[0] || ""));
  if (!hit) return { err: "no_stub", n: stubs.length, subjs: stubs.map((s) => s[0]).slice(0, 8) };
  /* dump full stub structure (truncated) */
  const dump = (v, d) => {
    if (d > 4) return typeof v;
    if (v === null || typeof v !== "object") return v;
    if (Array.isArray(v)) return v.slice(0, 12).map((x) => dump(x, d + 1));
    return Object.fromEntries(Object.keys(v).slice(0, 20).map((k) => [k, dump(v[k], d + 1)]));
  };
  return {
    id: hit[3],
    subj: hit[0],
    len: hit.length,
    keys: hit.map((x, i) => ({ i, t: typeof x, s: Array.isArray(x) ? "arr" + x.length : String(x).slice(0, 60) })),
    msg0: dump(hit[4] && hit[4][0], 0),
    full: JSON.stringify(hit).slice(0, 2500),
  };
})()
