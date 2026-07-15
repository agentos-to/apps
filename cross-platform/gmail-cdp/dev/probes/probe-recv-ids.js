(async () => {
  location.hash = "#search/subject:Reservation+confirmed+for+Thursday";
  await new Promise((r) => setTimeout(r, 2000));
  const row = document.querySelector('[data-legacy-thread-id="19f48238253ee251"]');
  if (row) row.click();
  await new Promise((r) => setTimeout(r, 3500));
  const msgs = [...document.querySelectorAll("[data-legacy-message-id]")].map((el) => ({
    legM: el.getAttribute("data-legacy-message-id"),
    mid: el.getAttribute("data-message-id"),
  }));
  return { hash: location.hash.slice(0, 80), msgs: msgs.slice(0, 5) };
})()
