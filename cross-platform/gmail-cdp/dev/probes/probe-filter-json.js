(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const oStr = JSON.stringify;
  window.__filtJson = [];
  JSON.stringify = function (v, ...rest) {
    try {
      const s = oStr.call(JSON, v);
      if (
        typeof s === 'string' &&
        /z000000\d+\*\d+/.test(s) &&
        (/\[\[\[1,/.test(s) || /\[\[\[2,/.test(s) || /522465311/.test(s) || /88147817/.test(s))
      ) {
        window.__filtJson.push({
          t: Date.now(),
          len: s.length,
          body: s.slice(0, 1200),
          stack: new Error('json').stack,
        });
        if (window.__filtJson.length > 15) window.__filtJson.shift();
      }
    } catch (e) {}
    return oStr.call(JSON, v, ...rest);
  };

  const created = await __agmail.createFilter({
    subject: 'AOS-FILTER-RE-2',
    removeLabels: ['UNREAD'],
  });
  await sleep(800);
  const id = created && created.id;
  let deleted = null;
  if (id) {
    deleted = await __agmail.deleteFilter(id);
    await sleep(500);
  }

  const framesOf = (stack) =>
    (stack || '')
      .split('\n')
      .slice(1, 22)
      .map((l) => {
        const m = l.trim().match(/^at ([^\s(]+)/);
        return m ? m[1] : l.trim().slice(0, 80);
      });

  JSON.stringify = oStr;
  const list = await __agmail.listFilters();
  return {
    created,
    deleted,
    n: window.__filtJson.length,
    captures: window.__filtJson.map((c) => ({
      len: c.len,
      body: c.body,
      frames: framesOf(c.stack),
    })),
    list,
    sacredOnly:
      list.length === 1 &&
      list[0].id === 'z0000001680819884557*4072220868256049026',
  };
})()
