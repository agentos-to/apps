const ik = window.GM_ID_KEY;
const tokens = [
  "19d8843eaa6e03ca",
  "16712789d475e7fb",
  "15f5868033169419",
  "13bc96ba2430e5a7",
  "199e3a19f179f4c1",
  "18a6613cbeef76af",
  "16a7e78385cbfa19",
  "1587b3ecb011e3a5",
];
const out = [];
for (const th of tokens) {
  const r = await fetch(
    "/mail/u/0/?ik=" + encodeURIComponent(ik) + "&view=om&th=" + th,
    { credentials: "include" }
  );
  const html = await r.text();
  const pre = html.match(/<pre[^>]*id="raw_message_text"[^>]*>([\s\S]*?)<\/pre>/i);
  if (!pre) {
    out.push({ th, err: "no_pre", title: ((html.match(/<title>([^<]+)/) || [])[1] || "").slice(0, 80) });
    continue;
  }
  const raw = pre[1]
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"');
  const headers = raw.split(/\r?\n\r?\n/, 1)[0];
  const grab = (name) => {
    const re = new RegExp("^" + name + ":\\s*(.*(?:\\r?\\n[ \\t].*)*)", "im");
    const hm = headers.match(re);
    return hm ? hm[1].replace(/\r?\n[ \t]+/g, " ").trim().slice(0, 220) : null;
  };
  out.push({
    th,
    from: grab("From"),
    subj: (grab("Subject") || "").slice(0, 70),
    listUnsub: grab("List-Unsubscribe"),
    listUnsubPost: grab("List-Unsubscribe-Post"),
    listId: grab("List-Id"),
  });
}
return out;
