(async () => {
  const g = window.__agmail;
  location.hash = "#search/subject:AOS-ATT-1783690638";
  await new Promise((r) => setTimeout(r, 2500));
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
  const msg = hit[4][0];
  const hexRe = /^[0-9a-f]{14,18}$/i;
  const hexIdx = [];
  for (let i = 0; i < msg.length; i++) {
    if (typeof msg[i] === "string" && hexRe.test(msg[i])) hexIdx.push({ i, v: msg[i] });
  }
  /* also walk for att arrays */
  const atts = [];
  const walk = (n, path) => {
    if (!Array.isArray(n) || path.length > 6) return;
    if (
      typeof n[0] === "string" &&
      n[0].indexOf("/") !== -1 &&
      typeof n[1] === "string" &&
      typeof n[5] === "string" &&
      n[5].indexOf("view=att") !== -1
    ) {
      atts.push({ path: path.join("."), mime: n[0], name: n[1], size: n[2], part: n[3], url: n[5] });
    }
    n.forEach((e, i) => walk(e, path.concat(i)));
  };
  walk(msg, []);
  /* compare received stub */
  location.hash = "#search/subject:Reservation+confirmed+for+Thursday";
  await new Promise((r) => setTimeout(r, 2500));
  const p2 = JSON.parse(g.bv[g.bv.length - 1]);
  const stubs2 = [];
  const find2 = (n, d) => {
    if (d > 12 || !Array.isArray(n)) return;
    if (typeof n[0] === "string" && typeof n[3] === "string" && /^thread-[af]:/.test(n[3])) {
      stubs2.push(n);
      return;
    }
    for (const e of n) find2(e, d + 1);
  };
  find2(p2, 0);
  const hit2 = stubs2.find((t) => /Reservation/.test(t[0] || "")) || stubs2[0];
  const msg2 = hit2 && hit2[4] && hit2[4][0];
  const hexIdx2 = [];
  if (msg2)
    for (let i = 0; i < msg2.length; i++) {
      if (typeof msg2[i] === "string" && hexRe.test(msg2[i])) hexIdx2.push({ i, v: msg2[i] });
    }
  return {
    self: { threadId: hit[3], t19: hit[19], msgHexes: hexIdx, atts, msgLen: msg.length },
    recv: hit2
      ? { threadId: hit2[3], t19: hit2[19], msgHexes: hexIdx2, msg0: msg2 && msg2[0] }
      : null,
  };
})()
