(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const proto = _m.V1k.prototype;
  const methods = ['E0b', 'kHb', 'IOb', 'Scg', 'hz', 'Grf'];
  const hits = {};
  const origs = {};
  for (const m of methods) {
    if (typeof proto[m] !== 'function') continue;
    origs[m] = proto[m];
    hits[m] = [];
    proto[m] = function () {
      try {
        const args = [].slice.call(arguments);
        hits[m].push({
          n: args.length,
          stack: (new Error(m).stack || '')
            .split('\n')
            .slice(1, 8)
            .map((l) => l.trim().slice(0, 140)),
          a0: (function () {
            try {
              return JSON.stringify(args[0]).slice(0, 200);
            } catch (e) {
              return typeof args[0];
            }
          })(),
        });
        window.__lastV1k = this;
      } catch (e) {}
      return origs[m].apply(this, arguments);
    };
  }

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-17',
    removeLabels: ['INBOX'],
  });
  await sleep(900);
  const afterC = JSON.parse(JSON.stringify(hits));
  for (const m of methods) hits[m] = [];

  let d = null;
  if (c && c.id) {
    d = await __agmail.deleteFilter(c.id);
    await sleep(900);
  }
  const afterD = JSON.parse(JSON.stringify(hits));

  for (const m of methods) proto[m] = origs[m];

  // Extract E0b caller source from create stack
  let callerSrc = null;
  const st = (((afterC.E0b || [])[0] || {}).stack || [])[1] || '';
  const mm = st.match(/\((https[^)]+):(\d+):(\d+)\)/);
  if (mm) {
    const url = mm[1];
    const line = +mm[2];
    const col = +mm[3];
    const t = await (await fetch(url)).text();
    const lines = t.split('\n');
    const L = lines[line - 1] || '';
    callerSrc = {
      urlTail: url.slice(-80),
      line,
      col,
      around: L.slice(Math.max(0, col - 200), col + 400),
    };
  }

  return {
    c: c && { id: c.id, status: c.status },
    d,
    afterC: Object.fromEntries(
      Object.entries(afterC).map(([k, v]) => [k, { n: v.length, sample: v[0] }])
    ),
    afterD: Object.fromEntries(
      Object.entries(afterD).map(([k, v]) => [k, { n: v.length, sample: v[0] }])
    ),
    callerSrc,
    hasV1k: !!window.__lastV1k,
  };
})()
