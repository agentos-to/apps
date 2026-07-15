const hex = "19f48a0360c4a115";
const mark = "REPLYWIRE-1783636273";
const mode = "r";

// ensure action hook for gates
const __g = (window.__agmail = window.__agmail || {});
if (!__g.hooked) {
  __g.hooked = true; __g.bv = []; __g.fd = []; __g.actions = []; __g.hdrs = null;
  const oOpen = XMLHttpRequest.prototype.open, oSend = XMLHttpRequest.prototype.send, oSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (m, u, ...r) { this.__u = u; this.__h = {}; return oOpen.call(this, m, u, ...r); };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) { try { this.__h[k] = v; } catch (e) {} return oSet.call(this, k, v); };
  XMLHttpRequest.prototype.send = function (b) {
    const x = this, u = String(this.__u || "");
    if (u.indexOf("/i/bv") !== -1 || u.indexOf("/i/fd") !== -1) {
      const bucket = u.indexOf("/i/bv") !== -1 ? __g.bv : __g.fd;
      this.addEventListener("load", function () { try { bucket.push(x.responseText); } catch (e) {} });
    }
    if (/\/i\/s(\?|$)/.test(u)) {
      const rec = { body: typeof b === "string" ? b : "", ts: Date.now(), status: null };
      __g.actions.push(rec);
      this.addEventListener("load", function () { try { rec.status = x.status; } catch (e) {} });
    }
    if (u.indexOf("/sync/") !== -1 && this.__h && this.__h["X-Framework-Xsrf-Token"]) __g.hdrs = this.__h;
    return oSend.call(this, b);
  };
}

// 1. jLn wrap (clean base)
const O = (_m.jLn && _m.jLn.__orig) || _m.jLn;
const W = function (...a) {
  (window.__c = window.__c || []).push(this);
  return O.apply(this, a);
};
W.prototype = O.prototype;
Object.setPrototypeOf(W, O);
for (const k of Object.getOwnPropertyNames(O)) {
  if (!["length", "name", "prototype"].includes(k)) try { W[k] = O[k]; } catch (e) {}
}
W.__agmCap = true;
W.__orig = O;
_m.jLn = W;
window.__c = [];

// 2. surface list row
location.hash = "#search/" + hex;
await new Promise((r) => setTimeout(r, 1800));
const row = document.querySelector('[data-legacy-thread-id="' + hex + '"]');
if (!row) return { status: "no_row" };

// 3. open conversation via row click (page-js, accepted by Gmail)
window.__c = [];
row.click();
// wait for open + message-view capture
let ctrl = null;
const odl = Date.now() + 8000;
while (Date.now() < odl) {
  await new Promise((r) => setTimeout(r, 200));
  const mvs = (window.__c || []).filter((c) => typeof c.Za === "function" && c.Ca !== undefined && c.ha !== undefined);
  if (mvs.length) { ctrl = mvs[mvs.length - 1]; break; }
  // open signal
  if (/REPLYVERIFY/i.test(document.title) && !/Search results/i.test(document.title)) {
    // still may need msg view
  }
}
if (!ctrl) {
  // if open by title but no capture, re-hash to re-render
  if (/REPLYVERIFY/i.test(document.title)) {
    const h = location.hash;
    location.hash = "#inbox";
    await new Promise((r) => setTimeout(r, 600));
    location.hash = h;
    const rdl = Date.now() + 6000;
    while (Date.now() < rdl) {
      await new Promise((r) => setTimeout(r, 200));
      const mvs = (window.__c || []).filter((c) => typeof c.Za === "function" && c.Ca !== undefined && c.ha !== undefined);
      if (mvs.length) { ctrl = mvs[mvs.length - 1]; break; }
    }
  }
}
if (!ctrl) return { status: "no_ctrl", nCap: (window.__c || []).length, title: document.title, hash: location.hash };

// 4. clean EQn (never wrap)
const EQn = (_m.__agmEQnOrig && typeof _m.__agmEQnOrig === "function") ? _m.__agmEQnOrig : _m.EQn;
// if EQn is tangled, try expand
try { EQn(ctrl, mode); } catch (e) { return { status: "eqn_throw", what: String(e).slice(0, 120) }; }

// 5. gmonkey mole
const __gm = window.__agmGm || (window.__agmGm = await new Promise((res) => { try { window.gmonkey.load("2", res); } catch (e) { res(null); } }));
if (!__gm) return { status: "no_gmonkey" };
const __mw = __gm.getMainWindow();
let draft = null;
const mdl = Date.now() + 8000;
while (Date.now() < mdl) {
  await new Promise((r) => setTimeout(r, 200));
  const ds = (__mw.getOpenDraftMessages && __mw.getOpenDraftMessages()) || [];
  if (ds.length) { draft = ds[ds.length - 1]; break; }
}
if (!draft) return { status: "no_mole", title: document.title };

// 6. setBody prepend to existing quote
const prev = (draft.getBody && draft.getBody()) || "";
const html = "<div>" + mark + " — wired reply_email end-to-end.</div>" + (prev || "");
draft.setBody(html);

// 7. autosave gate
const needle = mark;
const saveT0 = Date.now();
let saveBody = null;
const adl = Date.now() + 13000;
while (Date.now() < adl) {
  await new Promise((r) => setTimeout(r, 300));
  const news = (__g.actions || []).filter((a) => a.ts >= saveT0);
  const hit = news.find((a) => a.body.indexOf(needle) !== -1);
  if (hit) { saveBody = hit.body; break; }
  if (!needle && news.length) { saveBody = news[0].body; break; }
}
if (saveBody === null) {
  const any = (__g.actions || []).filter((a) => a.ts >= saveT0);
  if (any.length) saveBody = any[any.length - 1].body;
}
if (saveBody === null) return { status: "no_autosave", subject: draft.getSubject && draft.getSubject() };

// 8. send + send gate
const sendT0 = Date.now();
try { draft.send(); } catch (e) { return { status: "send_failed", what: String(e).slice(0, 80) }; }
let confirmed = false, fired = false;
const sdl = Date.now() + 10000;
while (Date.now() < sdl) {
  await new Promise((r) => setTimeout(r, 300));
  const sn = (__g.actions || []).filter((a) => a.ts >= sendT0);
  if (sn.length) fired = true;
  if (sn.some((a) => a.status === 200)) { confirmed = true; break; }
}
return {
  status: confirmed ? "sent" : (fired ? "sent_unconfirmed" : "send_no_confirm"),
  mark,
  subject: draft.getSubject && draft.getSubject(),
  to: draft.getToEmails && draft.getToEmails(),
  hash: location.hash,
  title: document.title.slice(0, 60),
};
