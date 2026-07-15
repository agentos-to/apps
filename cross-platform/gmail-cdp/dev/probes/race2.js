const t0 = Date.now();
let wrappedAt = null, sawJlnAt = null, firstSrc = null;
for (let i = 0; i < 200; i++) {
  if (window._m && typeof _m.jLn === "function") {
    if (sawJlnAt == null) {
      sawJlnAt = Date.now() - t0;
      firstSrc = String(_m.jLn).slice(0, 80);
    }
    if (!_m.jLn.__agmCap) {
      const O = _m.jLn;
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
      wrappedAt = Date.now() - t0;
      break;
    }
  }
  await new Promise((r) => setTimeout(r, 25));
}
// wait boot
const gdl = Date.now() + 20000;
while (!(window.gmonkey || (window.GM_APP_NAME && document.querySelector("[data-legacy-thread-id]"))) && Date.now() < gdl) {
  await new Promise((r) => setTimeout(r, 100));
}
await new Promise((r) => setTimeout(r, 3000));
const caps = window.__c || [];
// self-test wrap
let selfOk = false;
try {
  _m.jLn.call({__self: 1});
  selfOk = caps.some((c) => c && c.__self);
} catch (e) {}
return {
  sawJlnAt,
  wrappedAt,
  firstSrc,
  total: caps.length,
  selfOk,
  jLnCap: !!(window._m && _m.jLn && _m.jLn.__agmCap),
  jLnSrc: window._m && String(_m.jLn).slice(0, 100),
  hash: location.hash,
  title: document.title,
  nRows: document.querySelectorAll("[data-legacy-thread-id]").length,
  gmonkey: !!window.gmonkey,
};
