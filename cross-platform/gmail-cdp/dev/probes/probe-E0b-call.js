(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Capture one real E0b invocation
  const proto = _m.V1k.prototype;
  const orig = proto.E0b;
  proto.E0b = function (a) {
    window.__lastV1k = this;
    window.__lastE0bArg = a;
    window.__lastE0bJson = JSON.stringify(a);
    return orig.apply(this, arguments);
  };

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-19',
    removeLabels: ['INBOX'],
  });
  await sleep(800);
  proto.E0b = orig;

  const v1k = window.__lastV1k;
  const argJson = window.__lastE0bJson;
  if (!v1k || !argJson) {
    return { err: 'no capture', c };
  }

  // Delete the UI-created one
  if (c && c.id) {
    await __agmail.deleteFilter(c.id);
    await sleep(600);
  }

  // Mint a new client filter id (same shape as Gmail's)
  const newId =
    'z' +
    String(Date.now()) +
    '*' +
    String(Math.floor(Math.random() * 1e19));

  // Reuse token from last create, swap filter id (index 2 in array form)
  const arg = JSON.parse(argJson);
  const oldId = arg[2];
  arg[2] = newId;

  let callErr = null;
  try {
    v1k.E0b(arg);
  } catch (e) {
    callErr = String(e);
  }

  await sleep(1500);
  const list = await __agmail.listFilters();
  const hit = list.filter(
    (f) => f.id === newId || /AOS-FILTER-SEAL-19/.test(f.criteria || '')
  );

  // cleanup any AOS leftovers
  for (const f of list.filter((f) => /AOS-FILTER/.test(f.criteria || ''))) {
    try {
      await __agmail.deleteFilter(f.id);
    } catch (e) {}
  }
  await sleep(500);
  const final = await __agmail.listFilters();

  return {
    uiCreated: c && c.id,
    oldId,
    newId,
    callErr,
    argLen: arg.length,
    tokenHead: String(arg[1]).slice(0, 40),
    hitAfterCall: hit,
    aosLeft: final.filter((f) => /AOS-FILTER/.test(f.criteria || '')).length,
    sacred: final.filter((f) => /modernist/.test(f.criteria || '')).length,
    total: final.length,
  };
})()
