location.hash = "#search/subject%3AREPLYVERIFY-J1";
await new Promise((r) => setTimeout(r, 2000));

const hex = "19f48a0360c4a115";
const el = document.querySelector(`[data-legacy-thread-id="${hex}"]`);
if (!el) return { err: "no_el", n: document.querySelectorAll("[data-legacy-thread-id]").length };

// find best clickable: row, or link inside
const link = el.closest("tr")?.querySelector("a") || el.querySelector("a") || el;
const rect = link.getBoundingClientRect();

// try 1: .click()
link.click();
await new Promise((r) => setTimeout(r, 1500));
const afterClick = { hash: location.hash, title: document.title.slice(0, 60), open: /REPLYVERIFY/i.test(document.title) && !/Search results/i.test(document.title) };

if (afterClick.open) return { method: "element.click", ...afterClick, rect };

// reset
location.hash = "#search/subject%3AREPLYVERIFY-J1";
await new Promise((r) => setTimeout(r, 1200));

// try 2: MouseEvent bubble sequence
const el2 = document.querySelector(`[data-legacy-thread-id="${hex}"]`);
const link2 = el2.closest("tr")?.querySelector("a") || el2.querySelector("a") || el2;
for (const type of ["mousedown", "mouseup", "click"]) {
  link2.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window, clientX: rect.x + 5, clientY: rect.y + 5 }));
}
await new Promise((r) => setTimeout(r, 1500));
const afterMe = { hash: location.hash, title: document.title.slice(0, 60), open: /REPLYVERIFY/i.test(document.title) && !/Search results/i.test(document.title) };
if (afterMe.open) return { method: "MouseEvent", ...afterMe };

// try 3: jump via href on any anchor that could work
const hrefs = [...document.querySelectorAll("a[href]")].filter((a) => (a.innerText || "").includes("REPLYVERIFY")).map((a) => a.getAttribute("href")).slice(0, 5);

// try 4: go backed to known good and extract if something in controller changed closed
return {
  method: "none",
  afterClick,
  afterMe,
  hrefs,
  rect,
  elTag: el.tagName,
  elRole: el.getAttribute("role"),
  closestTr: !!el.closest("tr"),
  linkTag: link.tagName,
  linkHref: link.getAttribute && link.getAttribute("href"),
};
