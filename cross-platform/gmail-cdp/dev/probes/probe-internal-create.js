(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function safe(e) {
    try {
      return (e && e.message) || String(e && e.Oa) || String(e);
    } catch (x) {
      return 'err';
    }
  }

  const proto = _m.V1k.prototype;
  const orig = proto.E0b;
  proto.E0b = function (a) {
    window.__lastV1k = this;
    window.__lastE0bArg = a;
    return orig.apply(this, arguments);
  };

  // Capture token from a real create
  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-27',
    removeLabels: ['INBOX'],
  });
  await sleep(800);
  proto.E0b = orig;

  const src = window.__lastE0bArg;
  const v1k = window.__lastV1k;
  const token = _m.F(src, 2);
  const uiId = c && c.id;

  // Delete UI filter
  if (uiId) await __agmail.deleteFilter(uiId);
  await sleep(600);

  // Build new Vae with same token, new id
  const newId =
    'z000000' + String(Date.now()) + '*' + String(Math.floor(Math.random() * 1e18));
  const msg = new _m.Vae(src.La);
  const steps = [];
  try {
    steps.push({ f3before: _m.F(msg, 3) });
    _m.I(msg, 3, newId);
    steps.push({ f3after: _m.F(msg, 3), ok: true });
  } catch (e) {
    steps.push({ set3: false, err: safe(e) });
  }

  let e0b = null;
  try {
    v1k.E0b(msg);
    e0b = { ok: true };
  } catch (e) {
    e0b = { ok: false, err: safe(e) };
  }
  await sleep(1000);

  // Now forge i/s op 44 with criteria — capture a template from another create first
  // Simpler: create SEAL-28 via UI just to steal i/s envelope structure, then...
  // Actually try list to see if E0b alone registered an empty filter
  let list1 = await __agmail.listFilters();

  // Capture full create i/s template
  const before = ((window.__agmail && window.__agmail.actions) || []).length;
  const c2 = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-28',
    removeLabels: ['INBOX'],
  });
  await sleep(800);
  const isCreate = (window.__agmail.actions || [])
    .slice(before)
    .find((a) => /\[\[\[44,/.test(a.body || ''));
  const template = isCreate && isCreate.body;

  // Replace id in template with newId and also craft criteria for SEAL-27-INTERNAL
  let forged = null;
  if (template && e0b && e0b.ok) {
    const body2 = template
      .split(c2.id)
      .join(newId)
      .replace(/AOS-FILTER-SEAL-28/g, 'AOS-FILTER-SEAL-27-INTERNAL');
    // Fire via fetch to same i/s endpoint — need URL. Use performance or hook.
    // Prefer: replay through ovm if we can build request. For now raw XHR to relative path.
    try {
      const u =
        location.origin +
        '/sync/u/0/i/s?hl=en&c=' +
        Date.now() +
        '&rt=r&pt=ji';
      const r = await fetch(u, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: body2,
      });
      forged = {
        status: r.status,
        text: (await r.text()).slice(0, 200),
        bodyHead: body2.slice(0, 200),
      };
    } catch (e) {
      forged = { err: safe(e) };
    }
    await sleep(1000);
  }

  if (c2 && c2.id) await __agmail.deleteFilter(c2.id);
  await sleep(500);

  const list2 = await __agmail.listFilters();
  const aos = list2.filter((f) => /AOS-FILTER/.test(f.criteria || ''));

  // cleanup
  for (const f of aos) {
    try {
      await __agmail.deleteFilter(f.id);
    } catch (e) {}
  }
  // also try delete newId if present with empty criteria
  for (const f of list2) {
    if (f.id === newId) {
      try {
        await __agmail.deleteFilter(f.id);
      } catch (e) {}
    }
  }
  await sleep(500);
  const final = await __agmail.listFilters();

  return {
    uiId,
    newId,
    tokenHead: String(token).slice(0, 40),
    steps,
    e0b,
    forged,
    listAfterE0b: list1.map((f) => ({
      id: f.id,
      criteria: f.criteria,
    })),
    aosAfterForge: aos,
    aosLeft: final.filter((f) => /AOS-FILTER/.test(f.criteria || '')).length,
    sacred: final.filter((f) => /modernist/.test(f.criteria || '')).length,
    Vae: typeof _m.Vae,
  };
})()
