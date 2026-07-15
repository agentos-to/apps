(async () => {
  location.hash = "#search/subject:AOS-ATT-1783690638";
  await new Promise((r) => setTimeout(r, 2500));
  const rows = [...document.querySelectorAll("tr.zA, div[role=row]")].slice(0, 8).map((el) => {
    const a = {};
    for (const at of el.attributes || []) a[at.name] = (at.value || "").slice(0, 80);
    const kids = [...el.querySelectorAll("[data-legacy-thread-id],[data-legacy-message-id],[data-thread-id]")].slice(0, 4).map((k) => ({
      legT: k.getAttribute("data-legacy-thread-id"),
      legM: k.getAttribute("data-legacy-message-id"),
      tid: k.getAttribute("data-thread-id"),
      mid: k.getAttribute("data-message-id"),
    }));
    return {
      text: (el.innerText || "").replace(/\s+/g, " ").slice(0, 90),
      hasPaperclip: !!(el.querySelector('img[alt*="ttachment"], span[title*="ttachment"], .brd, .aZo, [aria-label*="ttachment"]')),
      paperclipSel: !!el.querySelector(".brd") || !!el.querySelector('[aria-label*="Attachment"]') || !!el.querySelector("img.yE"),
      attrs: a,
      kids,
    };
  });
  /* also dump bv stub shape for one AOS thread if __agmail captured */
  let stubSample = null;
  const g = window.__agmail;
  if (g && g.bv && g.bv.length) {
    try {
      const p = JSON.parse(g.bv[g.bv.length - 1]);
      const stubs = [];
      const find = (n, d) => {
        if (d > 10 || !Array.isArray(n)) return;
        if (typeof n[0] === "string" && typeof n[3] === "string" && /^thread-[af]:/.test(n[3])) {
          stubs.push({ subj: n[0], id: n[3], msg0: (n[4] && n[4][0] && n[4][0][0]) || null, msgKeys: n[4] && n[4][0] ? Object.keys(n[4][0]).slice(0, 8) : null, msg0head: n[4] && n[4][0] ? JSON.stringify(n[4][0]).slice(0, 400) : null });
          return;
        }
        for (const e of n) find(e, d + 1);
      };
      find(p, 0);
      stubSample = stubs.filter((s) => /AOS-ATT/.test(s.subj)).slice(0, 3);
    } catch (e) {
      stubSample = { err: String(e) };
    }
  }
  return { n: rows.length, rows, stubSample };
})()
