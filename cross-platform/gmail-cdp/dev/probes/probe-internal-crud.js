(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 1) Create via UI, capture token + full i/s body
  const beforeIs = ((window.__agmail && window.__agmail.actions) || []).length;
  const proto = _m.V1k.prototype;
  const orig = proto.E0b;
  proto.E0b = function (a) {
    window.__lastV1k = this;
    window.__lastE0bArg = a;
    window.__lastE0bJson = JSON.stringify(a);
    return orig.apply(this, arguments);
  };

  const c = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-21',
    removeLabels: ['INBOX'],
  });
  await sleep(900);
  proto.E0b = orig;

  const isActs = (window.__agmail.actions || []).slice(beforeIs);
  const createIs = isActs.find((a) => /\[\[\[44,/.test(a.body || ''));
  const createBody = createIs && createIs.body;

  // 2) Delete via forged i/s op 45 (NOT UI)
  let forgeDel = null;
  if (c && c.id) {
    // Capture a real delete body shape first? Or forge from create's delete we know:
    // [null,[[[45,null,null,null,null,null,[id,[null,[]]]]]],[1,...],[ts...],2]
    // Safer: use UI delete for cleanup of 21, then create 22 and forge-delete that.
  }

  // Clean UI delete for 21
  if (c && c.id) await __agmail.deleteFilter(c.id);
  await sleep(600);

  // 3) Create 22 via UI, then forge-delete with i/s 45 by replaying/adapting
  const c2 = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-22',
    removeLabels: ['INBOX'],
  });
  await sleep(900);

  // Spy one real delete i/s to get envelope, then we'll also try manual
  const beforeDel = ((window.__agmail.actions) || []).length;
  // Don't UI delete yet — try forge via XHR using last delete template from SEAL-20

  // Build forge from known shape
  const id = c2 && c2.id;
  let forgeResult = null;
  if (id) {
    // Find at/u from a recent i/s request — use page fetch to same origin
    const recent = (window.__agmail.actions || []).filter((a) =>
      /\/i\/s/.test(a.u || a.url || '') || /\[\[\[4[45],/.test(a.body || '')
    );
    // Hook: fire through Gmail by calling a low-level if we can; else raw fetch
    // Parse last i/s URL from performance or agmail
    const lastIs = (window.__agmail.actions || []).slice(-5).find((a) =>
      /\[\[\[44,|\[\[\[45,/.test(a.body || '')
    );

    // Use XMLHttpRequest like Gmail — need full URL + headers from last request.
    // Simpler path: wrap and call the same transport. For now capture real delete body:
    const delBefore = (window.__agmail.actions || []).length;
    await __agmail.deleteFilter(id);
    await sleep(700);
    const delActs = (window.__agmail.actions || []).slice(delBefore);
    const delBody = (delActs.find((a) => /\[\[\[45,/.test(a.body || '')) || {})
      .body;

    forgeResult = {
      created2: id,
      delBodyHead: delBody && delBody.slice(0, 250),
      createBodyHead: createBody && createBody.slice(0, 350),
      e0bArg: window.__lastE0bJson && window.__lastE0bJson.slice(0, 120),
      e0bLen: window.__lastE0bArg &&
        (Array.isArray(window.__lastE0bArg)
          ? window.__lastE0bArg.length
          : Object.keys(window.__lastE0bArg || {}).length),
    };
  }

  // 4) Retry E0b with 4-field array on fresh token
  const c3 = await __agmail.createFilter({
    subject: 'AOS-FILTER-SEAL-23',
    removeLabels: ['INBOX'],
  });
  await sleep(800);
  const tokenArg = window.__lastE0bArg;
  const v1k = window.__lastV1k;
  if (c3 && c3.id) await __agmail.deleteFilter(c3.id);
  await sleep(500);

  let e0bRetry = null;
  if (v1k && tokenArg) {
    const newId =
      'z000000' +
      String(Date.now()) +
      '*' +
      String(Math.floor(Math.random() * 1e18));
    let arr;
    if (Array.isArray(tokenArg)) {
      arr = tokenArg.slice();
      while (arr.length < 4) arr.push('');
      arr[2] = newId;
    } else {
      // protobuf object — clone via JSON array form
      arr = JSON.parse(window.__lastE0bJson);
      while (arr.length < 4) arr.push('');
      arr[2] = newId;
    }
    try {
      v1k.E0b(arr);
      e0bRetry = { ok: true, newId, arrLen: arr.length };
    } catch (e) {
      e0bRetry = { ok: false, err: String(e), arrLen: arr.length };
    }
    await sleep(1000);
  }

  const list = await __agmail.listFilters();
  // cleanup
  for (const f of list.filter((f) => /AOS-FILTER/.test(f.criteria || ''))) {
    try {
      await __agmail.deleteFilter(f.id);
    } catch (e) {}
  }
  await sleep(500);
  const final = await __agmail.listFilters();

  return {
    forgeResult,
    e0bRetry,
    aosLeft: final.filter((f) => /AOS-FILTER/.test(f.criteria || '')).length,
    sacred: final.filter((f) => /modernist/.test(f.criteria || '')).length,
    createIsHead: createBody && createBody.slice(0, 500),
  };
})()
