const hex = "19f48a0360c4a115";
const tries = [
  "#search/" + hex,
  "#search/" + encodeURIComponent(hex),
  "#search/rfc822msgid:" + hex,
  "#search/" + encodeURIComponent("in:anywhere"),
  "#all/" + hex,
  "#search/subject:REPLYVERIFY-J1",
];
const out = [];
for (const h of tries) {
  location.hash = h;
  await new Promise((r) => setTimeout(r, 1500));
  const el = document.querySelector(`[data-legacy-thread-id="${hex}"]`);
  out.push({
    h,
    landed: location.hash.slice(0, 80),
    hasRow: !!el,
    nRows: document.querySelectorAll("[data-legacy-thread-id]").length,
    title: document.title.slice(0, 50),
  });
}
return out;
