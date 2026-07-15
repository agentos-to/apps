(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function safe(e) {
    try {
      return (e && e.message) || String(e && e.Oa) || String(e);
    } catch (x) {
      return 'e';
    }
  }

  const proto = _m.V1k.prototype;
  const orig = proto.E0b;
  proto.E0b = function (a) {
    window.__lastV1k = this;
    window.__lastE0bArg = a;
    return orig.apply(this, arguments);
  };

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-TOKEN',
    removeLabels: ['INBOX'],
  });
  await sleep(800);
  proto.E0b = orig;

  const token = _m.F(window.__lastE0bArg, 2);
  const v1k = window.__lastV1k;
  if (c && c.id) await __agmail.deleteFilter(c.id);
  await sleep(500);

  const newId =
    'z000000' + String(Date.now()) + '*' + String(Math.floor(Math.random() * 1e18));
  const msg = new _m.Vae();
  _m.wt(msg, 1, 21);
  _m.I(msg, 2, token);
  _m.I(msg, 3, newId);
  _m.I(msg, 4, '');

  let e0b = null;
  try {
    v1k.E0b(msg);
    e0b = { ok: true, json: JSON.stringify(msg).slice(0, 100) };
  } catch (e) {
    e0b = { ok: false, err: safe(e) };
  }
  await sleep(1200);

  // Capture i/s 44 template via another UI create, adapt, send through Gmail XHR hook
  const before = ((window.__agmail && window.__agmail.actions) || []).length;
  const c2 = await __agmail.createFilter({
    subject: 'AOS-FILTER-TMPL',
    removeLabels: ['INBOX'],
  });
  await sleep(800);
  const tmplAct = (window.__agmail.actions || [])
    .slice(before)
    .find((a) => /\[\[\[44,/.test(a.body || ''));
  const tmpl = tmplAct && tmplAct.body;

  // Find how Gmail posts i/s — wrap ovm.Fy briefly to steal a live sender, OR
  // use the request builder from last sync. Simpler: mutate template id/criteria
  // and post using XMLHttpRequest open to last URL pattern with cookies.
  let isSend = null;
  if (tmpl && c2 && c2.id) {
    const body = tmpl
      .split(c2.id)
      .join(newId)
      .replace(/AOS-FILTER-TMPL/g, 'AOS-FILTER-INTERNAL');
    // Steal URL from actions if present; else construct
    let url = (tmplAct.u || tmplAct.url || '').trim();
    if (!url) {
      url =
        location.origin +
        '/sync/u/0/i/s?hl=en&c=' +
        Math.floor(Math.random() * 1000) +
        '&rt=r&pt=ji';
    }
    try {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url, false);
      xhr.withCredentials = true;
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.setRequestHeader('X-Same-Domain', '1');
      xhr.send(body);
      isSend = {
        status: xhr.status,
        resp: String(xhr.responseText || '').slice(0, 250),
        url: url.slice(0, 100),
        bodyHead: body.slice(0, 220),
      };
    } catch (e) {
      isSend = { err: safe(e) };
    }
  }

  if (c2 && c2.id) await __agmail.deleteFilter(c2.id);
  await sleep(800);

  const list = await __agmail.listFilters();
  const aos = list.filter((f) => /AOS-FILTER|INTERNAL/.test(f.criteria || ''));
  const byId = list.filter((f) => f.id === newId);

  for (const f of [...aos, ...byId]) {
    try {
      await __agmail.deleteFilter(f.id);
    } catch (e) {}
  }
  await sleep(500);
  const final = await __agmail.listFilters();

  return {
    newId,
    e0b,
    isSend,
    aos,
    byId,
    aosLeft: final.filter((f) => /AOS-FILTER/.test(f.criteria || '')).length,
    sacred: final.filter((f) => /modernist/.test(f.criteria || '')).length,
  };
})()
