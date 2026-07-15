(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  let huxName = null;
  for (const k of Object.getOwnPropertyNames(_m)) {
    try {
      const v = _m[k];
      if (
        typeof v === 'function' &&
        v.prototype &&
        typeof v.prototype.YQe === 'function' &&
        typeof v.prototype.Jvd === 'function'
      ) {
        huxName = k;
        break;
      }
    } catch (e) {}
  }

  const H = huxName && _m[huxName];
  const info = {
    huxName,
    has: !!H,
    methods:
      H &&
      Object.getOwnPropertyNames(H.prototype)
        .filter((n) => typeof H.prototype[n] === 'function')
        .slice(0, 40),
  };
  if (!H) return { info };

  const hits = { YQe: [], Jvd: [], Rve: [] };
  const origs = {};
  for (const m of ['YQe', 'Jvd', 'Rve']) {
    origs[m] = H.prototype[m];
    H.prototype[m] = function () {
      try {
        hits[m].push({
          n: arguments.length,
          args: [].slice.call(arguments).map((a) => {
            try {
              if (a == null) return a;
              if (typeof a === 'string') return a.slice(0, 80);
              if (typeof a === 'object')
                return {
                  t: (a.constructor && a.constructor.name) || typeof a,
                  keys: Object.keys(a).slice(0, 15),
                  j: JSON.stringify(a).slice(0, 180),
                };
              return typeof a;
            } catch (e) {
              return '?';
            }
          }),
          thisKeys: Object.keys(this).slice(0, 15),
        });
        window.__lastHux = this;
      } catch (e) {}
      return origs[m].apply(this, arguments);
    };
  }

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-HUX',
    removeLabels: ['INBOX'],
  });
  await sleep(1000);
  const afterC = JSON.parse(JSON.stringify(hits));
  for (const m of ['YQe', 'Jvd', 'Rve']) hits[m] = [];

  if (c && c.id) await __agmail.deleteFilter(c.id);
  await sleep(1000);
  const afterD = JSON.parse(JSON.stringify(hits));

  for (const m of ['YQe', 'Jvd', 'Rve']) H.prototype[m] = origs[m];

  return {
    info,
    afterC,
    afterD,
    hasHux: !!window.__lastHux,
    c: c && { id: c.id, status: c.status },
  };
})()
