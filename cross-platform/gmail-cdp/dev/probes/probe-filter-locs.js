(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const parseFrames = (stack) =>
    (stack || '')
      .split('\n')
      .slice(1)
      .map((line) => {
        const t = line.trim();
        const m =
          t.match(/^at ([^\s(]+) \((.+):(\d+):(\d+)\)$/) ||
          t.match(/^at (.+):(\d+):(\d+)$/);
        if (!m) return { raw: t.slice(0, 120) };
        if (m.length === 5) {
          return { fn: m[1], url: m[2], line: +m[3], col: +m[4] };
        }
        return { fn: null, url: m[1], line: +m[2], col: +m[3] };
      });

  const oStr = JSON.stringify;
  window.__filtLocs = [];
  JSON.stringify = function (v, ...rest) {
    try {
      const s = oStr.call(JSON, v);
      if (
        typeof s === 'string' &&
        (/\[\[\[1,/.test(s) || /\[\[\[2,/.test(s) || /522465311/.test(s)) &&
        /z000000\d+\*\d+/.test(s)
      ) {
        window.__filtLocs.push({
          kind: 'json',
          body: s.slice(0, 400),
          frames: parseFrames(new Error('loc').stack),
        });
      }
    } catch (e) {}
    return oStr.call(JSON, v, ...rest);
  };

  const oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (b) {
    try {
      const u = String(this.__u || '');
      const bs = typeof b === 'string' ? b : '';
      if (/\/(st\/s|i\/s)/.test(u) && /z000000|522465311/.test(u + bs)) {
        window.__filtLocs.push({
          kind: 'xhr',
          u: u.slice(0, 120),
          body: bs.slice(0, 300),
          frames: parseFrames(new Error('xhr').stack),
        });
      }
    } catch (e) {}
    return oSend.apply(this, arguments);
  };

  const created = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-1',
    removeLabels: ['INBOX'],
  });
  await sleep(600);
  if (created && created.id) {
    await __agmail.deleteFilter(created.id);
    await sleep(400);
  }
  JSON.stringify = oStr;

  const want = /^(uvm|Vqi|Tqi|Lqi|Qvm|xvm|Rih|xRb|oEb|C\.ld|x\.O7a|ovm\.Fy|Ewm|mwm)$/;
  const hits = [];
  for (const cap of window.__filtLocs || []) {
    for (const f of cap.frames || []) {
      if (!f.fn) continue;
      const short = f.fn.replace(/^_\./, '');
      if (want.test(short) || want.test(f.fn) || /uvm|Vqi|Tqi|Lqi|oEb/.test(f.fn)) {
        hits.push({
          via: cap.kind,
          fn: f.fn,
          line: f.line,
          col: f.col,
          urlTail: (f.url || '').slice(-100),
          url: f.url,
        });
      }
    }
  }

  const list = await __agmail.listFilters();
  return {
    created,
    aosLeft: list.filter((f) => /AOS-FILTER/.test(f.criteria || '')).length,
    nCaps: (window.__filtLocs || []).length,
    hits,
    sampleFrames: ((window.__filtLocs || [])[0] || {}).frames,
  };
})()
