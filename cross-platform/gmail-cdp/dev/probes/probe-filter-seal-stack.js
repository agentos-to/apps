(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const oStr = JSON.stringify;
  const hits = [];
  JSON.stringify = function (v, ...rest) {
    try {
      const s = oStr.call(JSON, v);
      if (
        typeof s === 'string' &&
        (/\[\[\[1,/.test(s) || /\[\[\[2,/.test(s) || /522465311/.test(s)) &&
        /z000000\d+\*\d+/.test(s)
      ) {
        const err = new Error('filt');
        const frames = (err.stack || '').split('\n').slice(1, 25).map((l) => {
          const t = l.trim();
          const m = t.match(/^at ([^\s(]+)?\s*\(?(https?:[^:\s]+):(\d+):(\d+)/);
          if (m) return { fn: m[1] || '(anon)', url: m[2], line: +m[3], col: +m[4] };
          const m2 = t.match(/^at (https?:[^:\s]+):(\d+):(\d+)/);
          if (m2) return { fn: '(anon)', url: m2[1], line: +m2[2], col: +m2[3] };
          return { raw: t.slice(0, 120) };
        });
        hits.push({ body: s.slice(0, 400), frames });
      }
    } catch (e) {}
    return oStr.call(JSON, v, ...rest);
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
  const list = await __agmail.listFilters();
  return {
    created,
    hits: hits.slice(0, 4),
    aosLeft: list.filter((f) => /AOS-FILTER/.test(f.criteria || '')).length,
    sacred: list.some((f) => /modernist/.test(f.criteria || '')),
  };
})()
