(async () => {
  /* Open Create a new filter and map step-1 criteria fields */
  const link = [...document.querySelectorAll('a,button,[role=link]')].find((el) =>
    /Create a new filter/i.test((el.textContent || '').trim())
  );
  if (!link) {
    return {
      err: 'no_create_link',
      text: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 400),
    };
  }
  link.click();
  await new Promise((r) => setTimeout(r, 1500));
  const dialogs = [...document.querySelectorAll('[role=dialog], .Kj-JD, .aRp, .ZZ')];
  const visible = dialogs.filter((d) => d.offsetParent !== null || d.getClientRects().length);
  const root = visible[0] || document.body;
  const inputs = [...root.querySelectorAll('input,textarea,select')].map((el) => ({
    tag: el.tagName,
    type: el.type || null,
    name: el.name || null,
    id: el.id || null,
    aria: el.getAttribute('aria-label'),
    placeholder: el.placeholder || null,
    value: (el.value || '').slice(0, 80),
    className: (el.className || '').toString().slice(0, 60),
  }));
  const labels = [...root.querySelectorAll('label,td,th,span,div')]
    .map((el) => (el.childNodes.length <= 2 ? (el.textContent || '').trim() : ''))
    .filter((t) => t && t.length < 40 && /From|To|Subject|Has the words|Doesn|Size|Has attachment|Create filter|Search|Cancel|Don't include/i.test(t));
  const buttons = [...root.querySelectorAll('button,[role=button],input[type=submit],a')]
    .map((el) => ({
      text: (el.textContent || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 60),
      tag: el.tagName,
      className: (el.className || '').toString().slice(0, 40),
      name: el.name || null,
    }))
    .filter((b) => b.text && b.text.length < 50);
  const text = (root.innerText || '').replace(/\s+/g, ' ').slice(0, 800);
  return {
    dialogCount: visible.length,
    inputs,
    labels: [...new Set(labels)].slice(0, 30),
    buttons: buttons.slice(0, 20),
    text,
  };
})()
