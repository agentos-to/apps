(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const proto = _m.V1k.prototype;
  const orig = proto.E0b;
  proto.E0b = function (a) {
    window.__lastV1k = this;
    window.__lastE0bArg = a;
    return orig.apply(this, arguments);
  };

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-24',
    removeLabels: ['INBOX'],
  });
  await sleep(800);
  proto.E0b = orig;

  const a = window.__lastE0bArg;
  const v1k = window.__lastV1k;
  const reads = {
    ctor: a && a.constructor && a.constructor.name,
    keys: a && Object.keys(a),
    F1: (function () {
      try {
        return _m.eu(a, 1);
      } catch (e) {
        return String(e);
      }
    })(),
    F2head: (function () {
      try {
        return String(_m.F(a, 2)).slice(0, 40);
      } catch (e) {
        return String(e);
      }
    })(),
    F3: (function () {
      try {
        return _m.F(a, 3);
      } catch (e) {
        return String(e);
      }
    })(),
    F4: (function () {
      try {
        return _m.F(a, 4);
      } catch (e) {
        return String(e);
      }
    })(),
    json: (function () {
      try {
        return JSON.stringify(a).slice(0, 100);
      } catch (e) {
        return String(e);
      }
    })(),
  };

  // Delete UI filter
  if (c && c.id) await __agmail.deleteFilter(c.id);
  await sleep(500);

  // Mutate live protobuf filter-id field and re-call E0b
  const newId =
    'z000000' + String(Date.now()) + '*' + String(Math.floor(Math.random() * 1e18));
  let mutate = null;
  try {
    const before3 = _m.F(a, 3);
    _m.I(a, 3, newId);
    const after3 = _m.F(a, 3);
    v1k.E0b(a);
    mutate = { ok: true, before3, after3, newId };
  } catch (e) {
    mutate = { ok: false, err: String(e), newId };
  }
  await sleep(1200);

  // Also try constructing fresh snd
  let fresh = null;
  try {
    const s = new _m.snd();
    // copy fields from captured via getters/setters
    const t = _m.F(a, 2);
    const id2 =
      'z000000' +
      String(Date.now() + 1) +
      '*' +
      String(Math.floor(Math.random() * 1e18));
    // Match E0b's own construction path in reverse for input
    // Input field1=21 (eu), field2=token (F), field3=id, field4=""
    let msg = a;
    // try new message of same type
    const Ctor = a.constructor;
    const m2 = new Ctor();
    _m.I(m2, 1, 21);
    // field 1 might need different setter — try wt path used inside E0b for snd out
    // For input, eu read 21 — might be int field
    try {
      _m.wt(m2, 1, 21);
    } catch (e) {}
    try {
      _m.I(m2, 2, t);
    } catch (e) {}
    try {
      _m.I(m2, 3, id2);
    } catch (e) {}
    try {
      _m.I(m2, 4, '');
    } catch (e) {}
    v1k.E0b(m2);
    fresh = {
      ok: true,
      id2,
      reads: {
        f1: _m.eu(m2, 1),
        f2: String(_m.F(m2, 2)).slice(0, 20),
        f3: _m.F(m2, 3),
      },
    };
  } catch (e) {
    fresh = { ok: false, err: String(e) };
  }
  await sleep(1000);

  const list = await __agmail.listFilters();
  const aos = list.filter((f) => /AOS-FILTER|z00000017838/.test(f.criteria || f.id || ''));
  // cleanup by id match on newId
  for (const f of list) {
    if (/AOS-FILTER/.test(f.criteria || '') || f.id === newId || (fresh && f.id === fresh.id2)) {
      try {
        await __agmail.deleteFilter(f.id);
      } catch (e) {}
    }
  }
  await sleep(500);
  const final = await __agmail.listFilters();

  return {
    reads,
    mutate,
    fresh,
    aosAfter: aos,
    aosLeft: final.filter((f) => /AOS-FILTER/.test(f.criteria || '')).length,
    sacred: final.filter((f) => /modernist/.test(f.criteria || '')).length,
  };
})()
