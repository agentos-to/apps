/**
 * page/eats.js — page-local SDK for Uber Eats (window.__ueats).
 *
 * Injected into the ubereats.com tab by uber.py before every Eats op.
 * Same-origin fetch to /_p/api/* rides the live session; Python stays
 * orchestration + AgentOS shaping.
 *
 * NOT the reverse-engineering toolkit. RE (commons/re/toolkit.js) is for
 * authoring only — see dev/probes/.
 *
 * Idempotent: bump __v to force-reload helpers after a deploy.
 */
(function () {
  if (globalThis.__ueats && __ueats.__v >= 5) return;
  const U = (globalThis.__ueats = { __v: 5 });

  const API = '/_p/api';
  const CSRF = { 'x-csrf-token': 'x', 'Content-Type': 'application/json' };
  const HOME = 'https://www.ubereats.com/orders';

  const onEats = () =>
    location.hostname.indexOf('ubereats.com') !== -1 &&
    location.hostname.indexOf('auth.uber.com') === -1;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const orderIdOf = (o) => {
    if (!o || typeof o !== 'object') return '';
    const info = o.orderInfo || {};
    const overview = o.activeOrderOverview || {};
    return (
      info.orderUuid ||
      o.orderUUID ||
      o.uuid ||
      overview.orderUuid ||
      ''
    );
  };

  const normTitle = (s) =>
    String(s || '')
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

  /** One RPC POST. Returns { ok, status, data, error, raw }. */
  U.api = async (op, body) => {
    if (!onEats()) {
      return { ok: false, status: 0, data: null, error: 'session_expired', raw: null };
    }
    const r = await fetch(`${API}/${op}`, {
      method: 'POST',
      credentials: 'include',
      cache: 'no-store',
      headers: CSRF,
      body: JSON.stringify(body || {}),
    });
    const text = await r.text();
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch (_) {}
    if (r.status !== 200) {
      const bounced =
        (text && text.indexOf('auth.uber.com') !== -1) ||
        (r.url && r.url.indexOf('auth.uber.com') !== -1);
      return {
        ok: false,
        status: r.status,
        data: null,
        error: bounced ? 'session_expired' : `http_${r.status}`,
        raw: parsed || text.slice(0, 500),
      };
    }
    if (!parsed || typeof parsed !== 'object') {
      return { ok: false, status: r.status, data: null, error: 'non_json', raw: text.slice(0, 500) };
    }
    if (parsed.status === 'failure') {
      const err = parsed.data && typeof parsed.data === 'object' ? parsed.data : {};
      const meta = (err.meta && err.meta.info) || {};
      const bodyErr = meta.body && typeof meta.body === 'object' ? meta.body : {};
      const msg =
        err.message || bodyErr.message || meta.message || 'api_failure';
      const code = err.code || meta.statusCode || bodyErr.code || '';
      return {
        ok: false,
        status: r.status,
        data: null,
        error: `api:${msg}`,
        code,
        raw: parsed,
      };
    }
    return { ok: true, status: r.status, data: parsed.data, error: null, raw: null };
  };

  /** Are we on Eats with a usable session? Probe drafts (getUserV1 often 403s). */
  U.session = async () => {
    if (!onEats()) return { ok: false, authenticated: false, reason: 'wrong_host' };
    const r = await U.api('getDraftOrdersByEaterUuidV1', { removeAdapters: true });
    if (!r.ok) {
      if (r.error === 'session_expired') {
        return { ok: false, authenticated: false, reason: 'session_expired' };
      }
      return {
        ok: true,
        authenticated: true,
        draftCount: null,
        soft: true,
        error: r.error,
      };
    }
    const drafts = (r.data && r.data.draftOrders) || [];
    return {
      ok: true,
      authenticated: true,
      draftCount: Array.isArray(drafts) ? drafts.length : 0,
    };
  };

  U.activeOrders = async () => {
    const r = await U.api('getActiveOrdersV1', {
      orderUuid: null,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'America/Chicago',
      showAppUpsellIllustration: true,
      isDirectTracking: false,
    });
    if (!r.ok) return r;
    return { ok: true, orders: (r.data && r.data.orders) || [] };
  };

  U.pastOrders = async (cursor) => {
    const r = await U.api('getPastOrdersV1', { lastWorkflowUUID: cursor || '' });
    if (!r.ok) return r;
    return {
      ok: true,
      orderUuids: (r.data && r.data.orderUuids) || [],
      ordersMap: (r.data && r.data.ordersMap) || {},
      paginationData: (r.data && r.data.paginationData) || null,
    };
  };

  /**
   * One-shot history feed for Shopping list: past pages (+ active cards)
   * until `limit` or the cursor runs out. Dropping active UUIDs that past
   * already marks completed.
   */
  U.listOrders = async (opts) => {
    const want = Math.max(1, Math.min(Number((opts && opts.limit) || 50), 100));
    let cursor = (opts && opts.cursor) || '';
    const ordersMap = {};
    const orderUuids = [];
    let paginationData = null;
    let pastOk = false;
    let pastErr = null;

    while (orderUuids.length < want) {
      const past = await U.pastOrders(cursor);
      if (!past.ok) {
        pastErr = past.error || 'past_failed';
        if (pastErr === 'session_expired' && orderUuids.length === 0) {
          return { ok: false, error: 'session_expired' };
        }
        break;
      }
      pastOk = true;
      paginationData = past.paginationData || null;
      const pageIds = past.orderUuids || [];
      const pageMap = past.ordersMap || {};
      if (pageIds.length === 0) break;
      pageIds.forEach((id) => {
        if (!ordersMap[id]) {
          ordersMap[id] = pageMap[id];
          orderUuids.push(id);
        }
      });
      const next =
        (paginationData && (paginationData.nextCursor || paginationData.nextPageCursor)) ||
        pageIds[pageIds.length - 1] ||
        '';
      if (!next || next === cursor) break;
      cursor = next;
      if (pageIds.length < 5) break; // thin page → end of history
    }

    const act = await U.activeOrders();
    if (!act.ok && act.error === 'session_expired' && !pastOk) {
      return { ok: false, error: 'session_expired' };
    }

    const completedIds = {};
    Object.keys(ordersMap).forEach((id) => {
      const base = (ordersMap[id] && ordersMap[id].baseEaterOrder) || {};
      if (base.isCompleted) completedIds[id] = true;
    });

    const active = [];
    if (act.ok) {
      (act.orders || []).forEach((o) => {
        const id = orderIdOf(o);
        if (id && !completedIds[id]) active.push(o);
      });
    }

    const ok = pastOk || active.length > 0;
    return {
      ok,
      error: ok ? null : pastErr || act.error || 'list_failed',
      orderUuids: orderUuids.slice(0, want),
      ordersMap,
      active,
      paginationData,
      pastOk,
      activeOk: !!act.ok,
      hasMore: !!(
        paginationData &&
        (paginationData.nextCursor || paginationData.nextPageCursor)
      ),
    };
  };

  U.drafts = async () => {
    const r = await U.api('getDraftOrdersByEaterUuidV1', { removeAdapters: true });
    if (!r.ok) return r;
    const all = (r.data && r.data.draftOrders) || [];
    const withItems = all.filter(
      (d) => ((d.shoppingCart || {}).items || []).length > 0,
    );
    return { ok: true, drafts: all, withItems };
  };

  /**
   * Session + API health report. Optional soft repair (navigate home).
   * Python should escalate login; this only does cheap in-page fixes.
   */
  U.health = async (opts) => {
    const repair = !!(opts && opts.repair);
    const repairs = [];
    const checks = {
      host: onEats(),
      href: location.href,
      v: U.__v,
    };

    if (!checks.host && repair) {
      try {
        location.assign(HOME);
        repairs.push('navigated_orders');
        await sleep(1200);
        checks.host = onEats();
        checks.href = location.href;
      } catch (e) {
        repairs.push('nav_failed:' + String(e && e.message ? e.message : e));
      }
    }

    checks.session = await U.session();
    checks.pastOrders = { ok: false };
    checks.receiptJson = { ok: false, probed: false };
    checks.activeOrders = { ok: false };

    if (checks.session && checks.session.authenticated) {
      const past = await U.pastOrders('');
      checks.pastOrders = {
        ok: !!past.ok,
        count: past.ok ? (past.orderUuids || []).length : 0,
        error: past.ok ? null : past.error,
      };

      const act = await U.activeOrders();
      checks.activeOrders = {
        ok: !!act.ok,
        count: act.ok ? (act.orders || []).length : 0,
        error: act.ok ? null : act.error,
      };

      const probeUuid =
        (past.ok && past.orderUuids && past.orderUuids[0]) ||
        (act.ok && act.orders && act.orders[0] && orderIdOf(act.orders[0])) ||
        '';
      if (probeUuid) {
        checks.receiptJson.probed = true;
        checks.receiptJson.orderUuid = probeUuid;
        const rec = await U.api('getReceiptByWorkflowUuidV1', {
          contentType: 'JSON',
          workflowUuid: probeUuid,
          timestamp: null,
        });
        if (rec.ok && rec.data && typeof rec.data.receiptData === 'string') {
          const parsed = U._parseReceiptJson(rec.data.receiptData);
          const n = parsed && parsed.items ? parsed.items.length : 0;
          checks.receiptJson = {
            ok: n > 0,
            probed: true,
            orderUuid: probeUuid,
            itemCount: n,
            error: n > 0 ? null : 'empty_cart',
          };
        } else {
          checks.receiptJson = {
            ok: false,
            probed: true,
            orderUuid: probeUuid,
            error: rec.error || 'receipt_json_failed',
          };
        }
      }
    }

    const ok =
      !!checks.host &&
      !!(checks.session && checks.session.authenticated) &&
      (checks.pastOrders.ok || checks.activeOrders.ok);

    return {
      ok,
      checks,
      repairs,
      tip: ok
        ? 'eats sdk healthy'
        : !checks.host
          ? 'not on ubereats.com — navigate or login_eats'
          : !(checks.session && checks.session.authenticated)
            ? 'session dead — login_eats'
            : 'APIs flaking — retry or repair:true',
    };
  };

  U.logout = async () => {
    if (!onEats()) return { ok: true, already: 'wrong_host' };
    try {
      await fetch('/logout', { method: 'POST', credentials: 'include' });
    } catch (_) {}
    return { ok: true };
  };

  /** Fill missing item images/prices from getStoreV1 catalog (title match). */
  U.enrichItems = async (storeUuid, items) => {
    if (!storeUuid || !Array.isArray(items) || !items.length) {
      return { ok: true, items: items || [] };
    }
    const need = items.some(
      (it) => !(it.image || it.imageUrl) || !it.price,
    );
    if (!need) return { ok: true, items };

    const store = await U.store(storeUuid);
    if (!store.ok || !store.store) {
      return { ok: false, items, error: store.error || 'store_failed' };
    }

    const catalog = {};
    const sectionsMap = store.store.catalogSectionsMap || {};
    Object.keys(sectionsMap).forEach((k) => {
      const secItems = sectionsMap[k];
      if (!Array.isArray(secItems)) return;
      secItems.forEach((item) => {
        if (item.type !== 'HORIZONTAL_GRID' && item.type !== 'VERTICAL_GRID') return;
        const std = ((item.payload || {}).standardItemsPayload) || {};
        (std.catalogItems || []).forEach((ci) => {
          const key = normTitle(ci.title || '');
          if (!key || catalog[key]) return;
          const cents = ci.price || 0;
          const amount = cents ? cents / 100 : null;
          catalog[key] = {
            id: ci.uuid,
            image: ci.imageUrl,
            priceAmount: amount,
            price: amount != null ? `$${amount.toFixed(2)}` : null,
          };
        });
      });
    });

    const out = items.map((it) => {
      const row = Object.assign({}, it);
      const key = normTitle(row.title || row.name || '');
      const hit = key ? catalog[key] : null;
      if (!hit) return row;
      if (!(row.image || row.imageUrl) && hit.image) {
        row.image = hit.image;
        row.imageUrl = hit.image;
      }
      if (!row.price && hit.price) {
        row.price = hit.price;
        row.priceAmount = hit.priceAmount;
      }
      if (hit.id && !row.asin) row.catalogId = row.catalogId || hit.id;
      return row;
    });
    return { ok: true, items: out };
  };

  /**
   * Structured order detail — prefer JSON receipt; HTML parsed in-page.
   * opts.enrich !== false → catalog-fill images/prices when store known.
   */
  U.getOrder = async (orderUuid, opts) => {
    const uuid = String(orderUuid || '').trim();
    if (!uuid) return { ok: false, error: 'order_uuid_required' };
    const enrich = !(opts && opts.enrich === false);

    const entity = await U.api('getOrderEntityByUuidV1', { orderUuid: uuid });
    if (entity.ok && entity.data && entity.data.orderEntity) {
      return {
        ok: true,
        source: 'orderEntity',
        orderUuid: uuid,
        entity: entity.data,
        past: null,
        receipt: null,
      };
    }

    let past = null;
    const pastRes = await U.pastOrders('');
    if (pastRes.ok && pastRes.ordersMap && pastRes.ordersMap[uuid]) {
      past = pastRes.ordersMap[uuid];
    }

    let receipt = null;
    let receiptSource = null;
    for (const contentType of ['JSON', 'WEB_HTML']) {
      const rec = await U.api('getReceiptByWorkflowUuidV1', {
        contentType,
        workflowUuid: uuid,
        timestamp: null,
      });
      if (!rec.ok) continue;
      const d = rec.data || {};
      if (!d || typeof d.receiptData !== 'string' || !d.receiptData.length) continue;

      if (contentType === 'JSON' || d.receiptData.trimStart().startsWith('{')) {
        const parsed = U._parseReceiptJson(d.receiptData);
        if (parsed && (parsed.items || []).length) {
          receipt = {
            timestamp: d.timestamp,
            receiptsForJob: d.receiptsForJob,
            ...parsed,
          };
          receiptSource = 'JSON';
          break;
        }
      }
      if (contentType === 'WEB_HTML' || d.receiptData.trimStart().startsWith('<')) {
        receipt = {
          timestamp: d.timestamp,
          receiptsForJob: d.receiptsForJob,
          items: U._parseReceiptHtml(d.receiptData),
          fare: U._parseReceiptFare(d.receiptData),
        };
        receiptSource = 'WEB_HTML';
        break;
      }
    }

    let active = null;
    const act = await U.activeOrders();
    if (act.ok) {
      active =
        (act.orders || []).find((o) => orderIdOf(o) === uuid) || null;
    }

    if (!past && !receipt && !active) {
      return {
        ok: false,
        error: entity.error || 'not_found',
        orderUuid: uuid,
      };
    }

    if (enrich && receipt && receipt.items && receipt.items.length) {
      const storeUuid =
        (past && past.storeInfo && past.storeInfo.uuid) || null;
      if (storeUuid) {
        const en = await U.enrichItems(storeUuid, receipt.items);
        if (en.ok) receipt.items = en.items;
      }
    }

    return {
      ok: true,
      source: receiptSource
        ? `receipt:${receiptSource}`
        : past
          ? 'past'
          : 'active',
      orderUuid: uuid,
      entity: null,
      past,
      receipt,
      active,
    };
  };

  /** Structured receipt (contentType JSON) → items + fare. */
  U._parseReceiptJson = (raw) => {
    let data;
    try {
      data = typeof raw === 'string' ? JSON.parse(raw) : raw;
    } catch (_) {
      return null;
    }
    if (!data || typeof data !== 'object') return null;

    const money = (v) => {
      if (v == null) return null;
      if (typeof v === 'string') return v.startsWith('$') ? v : `$${v}`;
      if (typeof v === 'number') {
        const n = v > 1000 ? v / 100 : v;
        return `$${n.toFixed(2)}`;
      }
      if (typeof v === 'object') {
        if (v.AmountE5 != null || v.amountE5 != null) {
          const e5 = Number(v.AmountE5 != null ? v.AmountE5 : v.amountE5);
          if (Number.isFinite(e5)) return `$${(e5 / 1e5).toFixed(2)}`;
        }
        if (v.amount != null) return money(Number(v.amount));
        if (v.value != null) return money(Number(v.value));
      }
      return null;
    };

    const items = [];
    for (const cart of data.cart || []) {
      for (const it of cart.Items || cart.items || []) {
        const title = it.Title || it.title || '';
        if (!title) continue;
        let qty = 1;
        if (typeof it.Quantity === 'number') qty = it.Quantity;
        else if (it.ItemQuantity && typeof it.ItemQuantity === 'object') {
          const corr = it.ItemQuantity.Corrected || it.ItemQuantity.Original || {};
          if (typeof corr.Value === 'number') qty = corr.Value;
        }
        const unit = it.UnitPrice;
        const total = it.TotalPrice || it.TotalPriceWithTaxes;
        let price = null;
        if (typeof total === 'string') price = total.startsWith('$') ? total : `$${total}`;
        else if (typeof unit === 'string') price = unit.startsWith('$') ? unit : `$${unit}`;
        else price = money(total) || money(unit);

        const customs = (it.Customizations || [])
          .map((c) => c.Title || c.title || c.Name || '')
          .filter(Boolean)
          .join(', ');
        const row = {
          id: it.Uuid || it.uuid,
          name: title,
          title,
          quantity: qty,
        };
        if (customs) row.customizations = customs;
        if (price) row.price = price;
        items.push(row);
      }
    }

    const fare = {};
    const f = data.fare || {};
    if (f.amount_charged) {
      fare.total =
        typeof f.amount_charged === 'string' ? f.amount_charged : money(f.amount_charged);
    }
    const details = Array.isArray(f.details)
      ? f.details
      : f.details && typeof f.details === 'object'
        ? Object.values(f.details)
        : [];
    const lines = [];
    for (const d of details) {
      if (!d || typeof d !== 'object') continue;
      const key = (d.key || d.type || d.label || '').toString();
      const label = d.label || d.title || key;
      const display =
        typeof d.amount === 'string' ? d.amount : money(d.amount) || money(d.value);
      if (!display) continue;
      lines.push({ key, label, display });
      const lk = `${key} ${label}`.toLowerCase();
      if (lk.includes('subtotal')) fare.item_subtotal = display;
      else if (lk.includes('delivery') && !lk.includes('discount')) fare.delivery_fee = display;
      else if (lk.includes('tax')) fare.tax = display;
      else if (lk.includes('tip')) fare.tip = display;
      else if (lk.includes('discount') || lk.includes('promo')) fare.delivery_discount = display;
    }
    if (lines.length) fare.lines = lines;
    return { items, fare, rawFare: f, misc: data.misc || null };
  };

  U._parseReceiptHtml = (html) => {
    if (!html || typeof html !== 'string') return [];
    let doc;
    try {
      doc = new DOMParser().parseFromString(html, 'text/html');
    } catch (_) {
      return [];
    }
    const items = [];
    const titles = doc.querySelectorAll('[data-testid^="shoppingCart_item_title_"]');
    titles.forEach((el) => {
      const tid = el.getAttribute('data-testid') || '';
      const uid = tid.replace('shoppingCart_item_title_', '');
      const qtyEl = doc.querySelector(`[data-testid="shoppingCart_item_quantity_${uid}"]`);
      const amtEl = doc.querySelector(`[data-testid="shoppingCart_item_amount_${uid}"]`);
      const custEls = doc.querySelectorAll(
        `[data-testid="shoppingCart_item_customization_${uid}"]`,
      );
      const qty = qtyEl ? parseInt(qtyEl.textContent.trim(), 10) || 1 : 1;
      const amountText = amtEl ? amtEl.textContent.trim() : '';
      const customizations = Array.from(custEls)
        .map((e) => e.textContent.trim())
        .filter(Boolean)
        .join(', ');
      let image = null;
      const imgEl =
        doc.querySelector(`[data-testid="shoppingCart_item_image_${uid}"] img`) ||
        doc.querySelector(`[data-testid="shoppingCart_item_image_${uid}"]`);
      if (imgEl && imgEl.getAttribute) {
        image = imgEl.getAttribute('src') || imgEl.getAttribute('href');
      }
      if (!image) {
        let node = el;
        for (let i = 0; i < 6 && node; i++) {
          const img = node.querySelector && node.querySelector('img[src]');
          if (img && (img.src || '').indexOf('http') === 0) {
            image = img.src;
            break;
          }
          node = node.parentElement;
        }
      }
      const item = {
        id: uid,
        name: (el.textContent || '').trim(),
        title: (el.textContent || '').trim(),
        quantity: qty,
      };
      if (customizations) item.customizations = customizations;
      if (amountText) item.price = amountText;
      if (image) item.image = image;
      items.push(item);
    });
    return items;
  };

  U._parseReceiptFare = (html) => {
    if (!html || typeof html !== 'string') return {};
    let doc;
    try {
      doc = new DOMParser().parseFromString(html, 'text/html');
    } catch (_) {
      return {};
    }
    const fare = {};
    const totalEl = doc.querySelector('[data-testid="total_fare_amount"]');
    if (totalEl) fare.total = totalEl.textContent.trim();
    const keys = [
      'item_subtotal',
      'delivery_fee',
      'service_fee',
      'tip',
      'delivery_discount',
      'tax',
    ];
    const lines = [];
    keys.forEach((key) => {
      const el = doc.querySelector(`[data-testid="fare_line_item_amount_${key}"]`);
      const labelEl = doc.querySelector(`[data-testid="fare_line_item_label_${key}"]`);
      if (!el) return;
      const display = el.textContent.trim();
      const label = labelEl
        ? labelEl.textContent.trim()
        : key.replace(/_/g, ' ');
      fare[key] = display;
      lines.push({ key, label, display });
    });
    if (lines.length) fare.lines = lines;
    return fare;
  };

  U.store = async (storeUuid) => {
    const r = await U.api('getStoreV1', { storeUuid });
    if (!r.ok) return r;
    return { ok: true, store: r.data };
  };

  U.help = () => ({
    v: U.__v,
    methods: [
      'api(op, body)',
      'session()',
      'health({repair?})',
      'listOrders({cursor?,limit?})',
      'activeOrders()',
      'pastOrders(cursor?)',
      'drafts()',
      'getOrder(orderUuid, {enrich?})',
      'enrichItems(storeUuid, items)',
      'store(storeUuid)',
      'logout()',
      'help()',
    ],
  });
})();
