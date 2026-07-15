/**
 * Uber-specific glue over toolkit `__re.sweep` — authoring only (dev/probes).
 * Not shipped. Inject toolkit.js first, then eval after setting __ORDER_UUID__.
 *
 * The generic discovery move lives in commons/re/toolkit.js:
 *   __re.sweep(url, { field, values, base, headers })
 */
(async () => {
  const uuid = __ORDER_UUID__;
  if (typeof __re === 'undefined' || typeof __re.sweep !== 'function') {
    return { error: 'inject toolkit.js (__v>=4 with __re.sweep) first' };
  }
  __re.net.on();
  __re.net.clear();

  const hdrs = { 'Content-Type': 'application/json', 'x-csrf-token': 'x' };

  // Soft warm of past-orders list (optional noise for net.urls).
  await __re.fetch('/_p/api/getPastOrdersV1', {
    method: 'POST',
    headers: hdrs,
    body: JSON.stringify({ lastWorkflowUUID: '' }),
  });

  const receipt = await __re.sweep('/_p/api/getReceiptByWorkflowUuidV1', {
    headers: hdrs,
    base: { workflowUuid: uuid, timestamp: null },
    field: 'contentType',
    values: ['APPLICATION_JSON', 'JSON', 'WEB_JSON', 'WEB_HTML'],
  });

  const entity = await __re.fetch('/_p/api/getOrderEntityByUuidV1', {
    method: 'POST',
    headers: hdrs,
    body: JSON.stringify({ orderUuid: uuid }),
  });

  return {
    href: location.href,
    receipt,
    entity: { status: entity.status, ok: entity.ok, keys: entity.json && Object.keys(entity.json).slice(0, 12) },
    urls: (__re.net.urls() || []).filter((u) => u.indexOf('/_p/api/') !== -1).slice(0, 40),
    detect: await __re.detect(),
    toolkit: { v: __re.__v },
  };
})()
