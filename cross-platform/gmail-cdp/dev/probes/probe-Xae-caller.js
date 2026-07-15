(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const oX = _m.Xae;
  let frame = null;
  _m.Xae = function (a, b) {
    try {
      const lines = (new Error('Xae').stack || '').split('\n');
      for (const l of lines) {
        const m = l.match(/\((https[^)]+):(\d+):(\d+)\)/);
        if (m) {
          frame = {
            url: m[1],
            line: +m[2],
            col: +m[3],
            raw: l.trim().slice(0, 180),
            tokenHead: String(b).slice(0, 40),
          };
          break;
        }
      }
    } catch (e) {}
    return oX.apply(this, arguments);
  };

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-XAE',
    removeLabels: ['INBOX'],
  });
  await sleep(800);
  _m.Xae = oX;

  let extracted = null;
  if (frame) {
    const t = await (await fetch(frame.url)).text();
    const lines = t.split('\n');
    const L = lines[frame.line - 1] || t;
    const col = frame.col;
    extracted = {
      frame,
      around: L.slice(Math.max(0, col - 400), col + 500),
      // search for Xae call sites in this file
      sites: (function () {
        const out = [];
        let p = 0;
        while ((p = t.indexOf('Xae(', p + 1)) !== -1 && out.length < 6) {
          out.push(t.slice(Math.max(0, p - 150), p + 200));
        }
        return out;
      })(),
    };
  }

  if (c && c.id) await __agmail.deleteFilter(c.id);
  await sleep(400);

  return { extracted, hasFrame: !!frame };
})()
