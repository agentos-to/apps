(async () => {
  const oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (b) {
    try {
      const u = String(this.__u || '');
      const bs = typeof b === 'string' ? b : '';
      if (/\/(st\/s|i\/s)/.test(u) && /z000000|522465311|88147817|subject:\(/.test(u + bs)) {
        (window.__filtStacks = window.__filtStacks || []).push({
          u: u.slice(0, 160),
          body: bs.slice(0, 400),
          stack: new Error('filt').stack,
          t: Date.now(),
        });
        if (window.__filtStacks.length > 20) window.__filtStacks.shift();
      }
    } catch (e) {}
    return oSend.apply(this, arguments);
  };
  window.__filtStacks = [];

  const list = await __agmail.listFilters();
  const aos = list.filter((f) => /AOS-FILTER/.test(f.criteria || ''));
  const results = [];
  for (const f of aos) {
    results.push(await __agmail.deleteFilter(f.id));
    await new Promise((r) => setTimeout(r, 400));
  }

  const created = await __agmail.createFilter({
    subject: 'AOS-FILTER-CRUD-4',
    removeLabels: ['INBOX'],
  });
  await new Promise((r) => setTimeout(r, 1000));

  const stacks = window.__filtStacks || [];
  return {
    deleted: results,
    created,
    n: stacks.length,
    stacks: stacks.map((s) => ({
      u: s.u,
      body: s.body,
      frames: (s.stack || '')
        .split('\n')
        .slice(1, 30)
        .map((l) => l.trim()),
    })),
  };
})()
