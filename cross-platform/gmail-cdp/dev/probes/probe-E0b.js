(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const proto = _m.V1k.prototype;
  const orig = proto.E0b;
  window.__E0bHits = [];
  proto.E0b = function (a) {
    try {
      window.__E0bHits.push({
        thisKeys: Object.keys(this).slice(0, 20),
        hasHa: !!this.ha,
        aType: a && a.constructor && a.constructor.name,
        fields: {
          f1: (function () {
            try {
              return _m.eu(a, 1);
            } catch (e) {
              return String(e);
            }
          })(),
          f2: (function () {
            try {
              return _m.F(a, 2);
            } catch (e) {
              return String(e);
            }
          })(),
          f3: (function () {
            try {
              return _m.F(a, 3);
            } catch (e) {
              return String(e);
            }
          })(),
          f4: (function () {
            try {
              return _m.F(a, 4);
            } catch (e) {
              return String(e);
            }
          })(),
        },
        json: (function () {
          try {
            return JSON.stringify(a).slice(0, 500);
          } catch (e) {
            return String(e);
          }
        })(),
      });
      window.__lastV1k = this;
      window.__lastE0bArg = a;
    } catch (e) {
      window.__E0bHits.push({ err: String(e) });
    }
    return orig.apply(this, arguments);
  };

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-16',
    removeLabels: ['INBOX'],
  });
  await sleep(1000);
  const hitsC = window.__E0bHits.slice();
  window.__E0bHits = [];
  let d = null;
  if (c && c.id) {
    d = await __agmail.deleteFilter(c.id);
    await sleep(800);
  }
  const hitsD = window.__E0bHits.slice();
  proto.E0b = orig;
  const list = await __agmail.listFilters();
  return {
    c: c && { id: c.id, status: c.status, action: c.action },
    d,
    hitsC,
    hitsD,
    aosLeft: list.filter((f) => /AOS-FILTER/.test(f.criteria || '')).length,
    hasV1k: !!window.__lastV1k,
    jtdType: typeof _m.jtd,
  };
})()
