(async () => {
  /* Can we get message hex from gmonkey without DOM? */
  const api = await new Promise((res) => {
    try {
      gmonkey.load("2", res);
    } catch (e) {
      res(null);
    }
  });
  if (!api) return { err: "no_gmonkey" };
  const mw = api.getMainWindow && api.getMainWindow();
  const am = mw && mw.getActiveMessage && mw.getActiveMessage();
  const ct = api.getCurrentThread && api.getCurrentThread();
  const out = {
    hash: location.hash.slice(0, 80),
    activeMsg: am
      ? {
          id: am.getMessageId && am.getMessageId(),
          from: am.getFromAddress && am.getFromAddress(),
          methods: Object.getOwnPropertyNames(Object.getPrototypeOf(am)).slice(0, 30),
        }
      : null,
    thread: ct
      ? {
          id: ct.getThreadId && ct.getThreadId(),
          methods: Object.getOwnPropertyNames(Object.getPrototypeOf(ct)).slice(0, 30),
        }
      : null,
  };
  /* open AOS thread if not open */
  if (!am) {
    location.hash = "#search/subject:AOS-ATT-1783690638";
    await new Promise((r) => setTimeout(r, 2000));
    const row = document.querySelector('[data-legacy-thread-id="19f4c3f47ee6d85d"]');
    if (row) row.click();
    await new Promise((r) => setTimeout(r, 3500));
    const am2 = mw && mw.getActiveMessage && mw.getActiveMessage();
    out.afterOpen = am2
      ? { id: am2.getMessageId && am2.getMessageId(), from: am2.getFromAddress && am2.getFromAddress() }
      : null;
    out.domMsg = [...document.querySelectorAll("[data-legacy-message-id]")].map((el) =>
      el.getAttribute("data-legacy-message-id")
    );
  }
  return out;
})()
