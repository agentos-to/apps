(async () => {
  const ik = window.GM_ID_KEY;
  const r = await fetch(
    "/mail/u/0/?ik=" + encodeURIComponent(ik) + "&view=om&th=19f4c3f4dacb9969",
    { credentials: "include" }
  );
  const t = await r.text();
  const pre = (t.match(/id="raw_message_text"[^>]*>([\s\S]*?)<\/pre>/i) || [])[1];
  if (!pre) return { err: "no_pre", len: t.length };
  const raw = pre
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"');
  return {
    subj: (raw.match(/^Subject:.*$/mi) || [])[0],
    hasAtt: /Content-Disposition:\s*attachment/i.test(raw),
    fname: (raw.match(/filename="?([^"\r\n;]+)/i) || [])[1],
    rawLen: raw.length,
    head: raw.slice(0, 500),
  };
})()
