/**
 * gmail_re.js — Gmail Web RE helpers for live browser_session eval.
 *
 * Prepend toolkit.js then this file (or only this if __re already on the page):
 *   js = toolkit + gmail_re + body
 * Idempotent: window.__gre = {…}  (__v bump forces reload of helpers)
 *
 * Production path for reply/send/attach/filters/labels/send-as/vacation lives
 * on window.__agmail (injected by every gmail-cdp op via gmail_cdp._LIB).
 * This file is for ad-hoc RE without waiting on an op. Thin delegates below
 * prefer __agmail when present.
 *
 * Account norms (bg profile): services.browser_session
 *   verb=eval|navigate  target=mail.google.com  mode=background
 *
 * See dev/gmail_re.md + requirements.md §§9–12 for Settings UI patterns + sacred
 * modernist.club filter warning.
 */
(function () {
  if (globalThis.__gre && __gre.__v >= 6) return;
  const G = (globalThis.__gre = { __v: 6 });

  const ready = async (ms = 15000) => {
    const dl = Date.now() + ms;
    while (Date.now() < dl) {
      if (window.gmonkey || window.GM_APP_NAME) return true;
      await new Promise((r) => setTimeout(r, 150));
    }
    return false;
  };

  const ag = (name) => (window.__agmail && typeof window.__agmail[name] === 'function'
    ? window.__agmail[name].bind(window.__agmail) : null);

  G.ready = ready;

  G.accounts = async () => {
    const out = [], seen = new Set();
    for (let n = 0; n < 8; n++) {
      try {
        const r = await fetch('/mail/u/' + n + '/feed/atom', { credentials: 'include' });
        if (!r.ok) break;
        const landed = (r.url.match(/\/u\/(\d+)\//) || [])[1];
        if (r.redirected || String(landed) !== String(n)) break;
        const txt = await r.text();
        const email = (txt.match(/for ([^<]+@[^<]+)</) || [])[1];
        if (!email || seen.has(email.toLowerCase())) break;
        seen.add(email.toLowerCase());
        out.push({ index: n, email });
      } catch (e) { break; }
    }
    return out;
  };

  // Base-class capture — prefer __re.captureBase when toolkit is loaded.
  G.wrapJln = () => {
    if (globalThis.__re && __re.captureBase) return __re.captureBase(_m, 'jLn', { bucket: '__agmCtrl' });
    if (!_m || typeof _m.jLn !== 'function') return { err: 'no_jln' };
    const base = _m.jLn.__orig || _m.jLn;
    const W = function (...a) {
      (window.__agmCtrl = window.__agmCtrl || []).push(this);
      return base.apply(this, a);
    };
    W.prototype = base.prototype;
    Object.setPrototypeOf(W, base);
    for (const k of Object.getOwnPropertyNames(base)) {
      if (!['length', 'name', 'prototype'].includes(k)) try { W[k] = base[k]; } catch (e) {}
    }
    W.__agmCap = true; W.__orig = base; _m.jLn = W;
    window.__agmCtrl = window.__agmCtrl || [];
    return { ok: true, bucket: '__agmCtrl' };
  };

  G.msgViews = () => (window.__agmCtrl || []).filter(
    (c) => typeof c.Za === 'function' && c.Ca !== undefined && c.ha !== undefined
  );

  // Wall 4 — open a conversation. hex|thread|perm.
  G.open = async (kind, token) => {
    G.wrapJln();
    window.__agmCtrl = [];
    if (kind === 'perm') {
      location.hash = '#inbox/' + token;
    } else if (kind === 'hex' || kind === 'thread') {
      location.hash = kind === 'hex' ? ('#search/' + token) : '#search/in:anywhere';
      let row = null, dl = Date.now() + 10000;
      while (Date.now() < dl) {
        row = kind === 'hex'
          ? document.querySelector('[data-legacy-thread-id="' + token + '"]')
          : (document.querySelector('[data-thread-id="#' + token + '"]')
            || document.querySelector('[data-thread-id="' + token + '"]'));
        if (row) break;
        await new Promise((r) => setTimeout(r, 200));
      }
      if (!row) return { status: 'no_row', token };
      window.__agmCtrl = [];
      row.click(); // Gmail accepts this; CDP click not required
    } else return { status: 'bad_kind', kind };
    let ctrl = null, dl = Date.now() + 9000;
    while (Date.now() < dl) {
      await new Promise((r) => setTimeout(r, 200));
      const m = G.msgViews();
      if (m.length) { ctrl = m[m.length - 1]; break; }
    }
    return {
      status: ctrl ? 'open' : 'no_ctrl',
      hash: location.hash, title: document.title,
      nMsg: G.msgViews().length, nCap: (window.__agmCtrl || []).length,
      hasCtrl: !!ctrl,
    };
  };

  G.listRows = (needle) => [...document.querySelectorAll('[data-legacy-thread-id]')].map((el) => ({
    hex: el.getAttribute('data-legacy-thread-id'),
    tid: el.getAttribute('data-thread-id'),
    text: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 80),
  })).filter((r) => !needle || (r.text + r.hex + r.tid).includes(needle));

  G.fileInputs = () => [...document.querySelectorAll('input[type=file]')].map((el, i) => ({
    i, name: el.name, accept: el.accept, multi: el.multiple,
    files: el.files && el.files.length,
  }));

  G.attach = (files) => {
    if (globalThis.__re && __re.attachFiles) {
      return __re.attachFiles({ selector: 'input[type=file][name=Filedata]', files });
    }
    return { err: 'load toolkit.js for __re.attachFiles' };
  };

  // view=om — needs MESSAGE legacy hex (msg[55] / data-legacy-message-id),
  // NOT thread hex. Self-sents diverge; received often match.
  G.om = async (hex, idx = 0) => {
    const ik = window.GM_ID_KEY;
    if (!ik) return { err: 'no_ik' };
    const r = await fetch(
      '/mail/u/' + idx + '/?ik=' + encodeURIComponent(ik) + '&view=om&th=' + encodeURIComponent(hex),
      { credentials: 'include' }
    );
    const t = await r.text();
    const pre = (t.match(/id=["']raw_message_text["'][^>]*>([\s\S]*?)<\/pre>/i) || [])[1];
    return {
      ok: r.ok, status: r.status, len: t.length,
      hasPre: !!pre,
      rawLen: pre ? pre.length : 0,
      head: t.replace(/\s+/g, ' ').slice(0, 160),
    };
  };

  G.resolveOm = async (token) => {
    const fn = ag('resolveOmToken');
    if (fn) return fn(token);
    return { err: 'run a gmail-cdp op first (injects __agmail), or open thread and read data-legacy-message-id' };
  };

  // ── Settings: filters / labels / send-as / vacation ─────────────────
  // Prefer __agmail (full create/delete). Fallbacks are list-only scrapes.
  G.listFilters = async () => {
    const fn = ag('listFilters');
    if (fn) return fn();
    location.hash = '#settings/filters';
    await new Promise((r) => setTimeout(r, 2000));
    return [...document.querySelectorAll('table tr')]
      .filter((tr) => /Matches:/.test(tr.innerText || ''))
      .map((tr) => {
        const txt = (tr.innerText || '').replace(/\s+/g, ' ').trim();
        const m = txt.match(/Matches:\s*(.*?)\s*Do this:\s*(.*?)(?:\s*edit|\s*delete|$)/i);
        const del = [...tr.querySelectorAll('a,button,[role=link]')].find((el) =>
          /^delete$/i.test((el.textContent || '').trim())
        );
        const im = del && del.id && del.id.match(/#(z[\w*]+\*?[\w*]*)/);
        return { id: im ? im[1] : (del && del.id), criteria: m && m[1], action: m && m[2], text: txt.slice(0, 200) };
      });
  };
  G.createFilter = async (opts) => {
    const fn = ag('createFilter');
    if (fn) return fn(opts);
    return { err: 'need __agmail — run gmail-cdp.list_filters (or any op) once to inject _LIB' };
  };
  G.deleteFilter = async (id) => {
    const fn = ag('deleteFilter');
    if (fn) return fn(id);
    return { err: 'need __agmail — run gmail-cdp.list_filters once to inject _LIB' };
  };
  G.createLabel = async (opts) => {
    const fn = ag('createLabel');
    if (fn) return fn(opts);
    return { err: 'need __agmail' };
  };
  G.deleteLabel = async (id) => {
    const fn = ag('deleteLabel');
    if (fn) return fn(id);
    return { err: 'need __agmail' };
  };
  G.listSendAs = async () => {
    const fn = ag('listSendAs');
    if (fn) return fn();
    return { err: 'need __agmail' };
  };
  G.getVacation = async () => {
    const fn = ag('getVacation');
    if (fn) return fn();
    return { err: 'need __agmail' };
  };
  G.setVacation = async (opts) => {
    const fn = ag('setVacation');
    if (fn) return fn(opts);
    return { err: 'need __agmail' };
  };

  /** Flag Joe's real forward filter — never delete in tests. */
  G.sacredFilters = (list) => (list || []).filter((f) =>
    /modernist\.club/i.test((f.criteria || '') + (f.text || '')) &&
    /joe@contini\.co/i.test((f.action || '') + (f.text || ''))
  );

  /**
   * RE: snapshot recent filter-related sync captures from __agmail.
   * Create writes /sync/st/s (opaque settings token) + /sync/i/s action 2;
   * delete emits /sync/i/s action 1. Hand-forging i/s alone does NOT stick —
   * call Gmail's own UI, or drive _m.V1k.prototype.E0b on a live instance
   * (captureBase / spy) — see dev/gmail_re.md "Filter sync RE".
   */
  G.filterSyncCaps = () => {
    const ag = window.__agmail;
    if (!ag) return { err: 'no_agmail' };
    const last = (arr, n) => (arr || []).slice(-(n || 5)).map((a) => ({
      ts: a.ts, status: a.status, body: (a.body || '').slice(0, 500),
    }));
    const V1k = (typeof _m !== 'undefined' && _m.V1k) || null;
    return {
      i_s: last(ag.actions, 8),
      st_s: last(ag.settingsActions, 8),
      sealed: {
        writer: V1k && typeof V1k.prototype.E0b === 'function' ? '_m.V1k.prototype.E0b' : null,
        msgType: typeof (typeof _m !== 'undefined' && _m.snd),
        field: 522465311,
        next: "gbreak wait expr '_m.V1k.prototype.E0b' --trigger createFilter; inspect paused this/scopes; or captureBase(_m,'V1k').take(); do not forge i/s alone",
      },
      notes: {
        i_s_codes: 'filter delete≈[[[1,…,[id,[null,[]]]]] ; upsert≈[[[2,…,[id,[[filterRow]]]]]]',
        st_s: 'settings write — field 522465311 / _.snd via V1k.E0b; required to persist',
        stack: 'V1k.E0b → snd → … → JSON.stringify → _.C.ld → _.x.O7a → uvm (uvm sealed)',
        strings: '_m.GHl="getFiltersList" _m.sEm="Error creating filter" _m.WHa="create-filter/"',
      },
    };
  };

  G.moles = async () => {
    const gm = await new Promise((res) => { try { window.gmonkey.load('2', res); } catch (e) { res(null); } });
    if (!gm) return { err: 'no_gmonkey' };
    const ds = (gm.getMainWindow().getOpenDraftMessages && gm.getMainWindow().getOpenDraftMessages()) || [];
    return {
      count: ds.length,
      subjects: ds.map((d) => { try { return d.getSubject && d.getSubject(); } catch (e) { return null; } }),
    };
  };

  // Clean EQn (never double-wrap).
  G.eqnClean = () => {
    if (typeof _m.__agmEQnOrig === 'function' && _m.EQn !== _m.__agmEQnOrig) {
      _m.EQn = _m.__agmEQnOrig;
      try { delete _m.__agmEQnOrig; } catch (e) { _m.__agmEQnOrig = undefined; }
    }
    return { eqn: typeof _m.EQn, src: String(_m.EQn).slice(0, 80) };
  };

  G.help = () => ({
    methods: Object.keys(G).filter((k) => typeof G[k] === 'function'),
    agmail: 'Connector __agmail.* ships with every gmail-cdp op — prefer it. Seed with list_filters if missing.',
    open: "await __gre.open('hex','19f48a…') | open('thread','thread-a:r-…') | open('perm','Ktbx…')",
    attach: "await compose then __gre.attach([{filename,mimeType,content:b64}])",
    om: "await __gre.om(messageHex) — thread hex fails on many self-sents; use resolveOm / list.messageHex",
    filters: "await __gre.listFilters() | createFilter({subject:'AOS-FILTER-*',removeLabels:['INBOX']}) | deleteFilter(id) — NEVER touch modernist.club→joe@contini.co",
    filterSync: "after a create/delete: __gre.filterSyncCaps() — i/s + st/s bodies + RE notes",
    labels: "await __gre.createLabel({name:'AOS-LABEL-*'}) | deleteLabel(name)",
    vacation: "await __gre.getVacation() | setVacation({enableAutoReply,responseSubject,responseBodyPlainText}) — always restore",
    sendAs: "await __gre.listSendAs()",
    sacred: "await __gre.sacredFilters(await __gre.listFilters())",
    geval: "GRE=1 geval '…' also injects gmail_re.js; FRESH=1 reloads toolkit+gre",
  });
})();
