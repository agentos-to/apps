(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const proto = _m.V1k.prototype;
  const orig = proto.E0b;
  let callerFrame = null;
  proto.E0b = function (a) {
    try {
      const lines = (new Error('E0b').stack || '').split('\n');
      for (const l of lines) {
        const m = l.match(/\((https[^)]+\.js[^)]*):(\d+):(\d+)\)/);
        if (m && !/E0b/.test(l)) {
          callerFrame = { url: m[1], line: +m[2], col: +m[3], raw: l.trim().slice(0, 200) };
          break;
        }
      }
      window.__lastV1k = this;
      window.__lastE0bArg = a;
    } catch (e) {}
    return orig.apply(this, arguments);
  };

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-18',
    removeLabels: ['INBOX'],
  });
  await sleep(800);
  proto.E0b = orig;

  let extracted = null;
  if (callerFrame && callerFrame.url) {
    const t = await (await fetch(callerFrame.url)).text();
    const lines = t.split('\n');
    const L = lines[callerFrame.line - 1] || t;
    const col = callerFrame.col || 1;
    // If single-line file, use col on whole text
    const src = L.length > 10 ? L : t;
    const start = Math.max(0, (L.length > 10 ? col : col) - 300);
    extracted = {
      frame: callerFrame,
      around: src.slice(start, start + 900),
      // search for E0b( call sites in this file
      callSites: (function () {
        const out = [];
        let p = 0;
        while ((p = t.indexOf('.E0b(', p + 1)) !== -1 && out.length < 8) {
          out.push(t.slice(Math.max(0, p - 120), p + 200));
        }
        return out;
      })(),
    };
  }

  if (c && c.id) {
    await __agmail.deleteFilter(c.id);
    await sleep(400);
  }

  return {
    created: c && c.id,
    hasV1k: !!window.__lastV1k,
    extracted,
  };
})()
