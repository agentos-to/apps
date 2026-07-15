(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const beforeSt = ((window.__agmail && window.__agmail.settingsActions) || []).length;
  const beforeIs = ((window.__agmail && window.__agmail.actions) || []).length;

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-20',
    removeLabels: ['INBOX'],
  });
  await sleep(900);
  const midSt = ((window.__agmail.settingsActions) || []).length;
  const midIs = ((window.__agmail.actions) || []).length;

  let d = null;
  if (c && c.id) {
    d = await __agmail.deleteFilter(c.id);
    await sleep(900);
  }
  const actsSt = (window.__agmail.settingsActions || []).slice(beforeSt);
  const actsIs = (window.__agmail.actions || []).slice(beforeIs);

  function briefSt(a) {
    try {
      const j = JSON.parse(a.body);
      const slot = j[0][0][0];
      const f = slot[1][1]['88147817'][1]['522465311'];
      return {
        kind: 'st/s',
        action: slot[2],
        extra: slot[3],
        id: f[2],
        f0: f[0],
        f3: f[3],
        tokenHead: String(f[1]).slice(0, 24),
      };
    } catch (e) {
      return { kind: 'st/s', err: String(e).slice(0, 80) };
    }
  }
  function briefIs(a) {
    try {
      const body = a.body || a.b || '';
      const head = body.slice(0, 180);
      const hasId = c && c.id && body.includes(c.id);
      const m = body.match(/\[\[\[(\d+)/);
      return {
        kind: 'i/s',
        op: m && m[1],
        hasId,
        head,
        u: (a.u || a.url || '').slice(0, 60),
      };
    } catch (e) {
      return { kind: 'i/s', err: String(e).slice(0, 80) };
    }
  }

  const list = await __agmail.listFilters();
  return {
    c: c && { id: c.id, status: c.status },
    d,
    nSt: actsSt.length,
    nIs: actsIs.length,
    st: actsSt.map(briefSt),
    is: actsIs.map(briefIs),
    aosLeft: list.filter((f) => /AOS-FILTER/.test(f.criteria || '')).length,
  };
})()
