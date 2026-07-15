(async () => {
  /* Probe filter surfaces: settings hash, net, gmonkey, known endpoints */
  __re.net.on();
  __re.net.clear();
  const before = location.href;
  /* Gmail filter settings live under #settings/filters */
  location.hash = "#settings/filters";
  await new Promise((r) => setTimeout(r, 4000));
  const urls = __re.net.urls().filter((u) =>
    /filter|setting|prefs|cfg|rule/i.test(u)
  );
  const detail = __re.net.detail("/sync/", { limit: 6, resp: 120 });
  const detail2 = __re.net
    .reqs
    .filter((e) => /filter|setting|prefs|cfg/i.test(e.url))
    .slice(-8)
    .map((e) => ({
      method: e.method,
      url: e.url.replace(location.origin, "").slice(0, 160),
      status: e.status,
      len: e.resp ? e.resp.length : 0,
      head: e.resp ? e.resp.slice(0, 200) : null,
    }));
  /* DOM: any filter rows? */
  const text = (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 500);
  const rows = [...document.querySelectorAll("table tr, [role=listitem], .rQ")]
    .slice(0, 20)
    .map((el) => (el.innerText || "").replace(/\s+/g, " ").slice(0, 120))
    .filter((t) => t.length > 10);
  return {
    hash: location.hash,
    before,
    filterUrls: urls.slice(0, 20),
    syncDetail: detail,
    filterReqs: detail2,
    textHead: text,
    rowSample: rows.slice(0, 12),
  };
})()
