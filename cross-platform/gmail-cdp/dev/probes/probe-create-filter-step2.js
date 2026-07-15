(async () => {
  /* Fill criteria + advance to actions step; map checkboxes/buttons */
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let dlg = [...document.querySelectorAll('[role=dialog]')].find(
    (d) => d.offsetParent !== null || d.getClientRects().length
  );
  if (!dlg || !/From/.test(dlg.innerText || '')) {
    const link = [...document.querySelectorAll('a,button,[role=link]')].find((el) =>
      /Create a new filter/i.test((el.textContent || '').trim())
    );
    if (link) {
      link.click();
      await sleep(1200);
    }
    dlg = [...document.querySelectorAll('[role=dialog]')].find(
      (d) => d.offsetParent !== null || d.getClientRects().length
    );
  }
  if (!dlg) return { err: 'no_dialog' };

  const byClass = (cls) => dlg.querySelector('input.' + cls);
  const from = byClass('aQa');
  const to = byClass('aQf');
  const subj = byClass('aQd');
  const hasWords = byClass('aQb');
  const doesnt = byClass('aP9');
  if (subj) {
    subj.focus();
    subj.value = 'AOS-FILTER-PROBE';
    subj.dispatchEvent(new Event('input', { bubbles: true }));
    subj.dispatchEvent(new Event('change', { bubbles: true }));
  }
  await sleep(300);
  const createBtn = [...dlg.querySelectorAll('button,[role=button]')].find((el) =>
    /^Create filter$/i.test((el.textContent || '').trim())
  );
  if (!createBtn) {
    return {
      err: 'no_create_btn',
      text: (dlg.innerText || '').replace(/\s+/g, ' ').slice(0, 400),
    };
  }
  createBtn.click();
  await sleep(1500);

  dlg = [...document.querySelectorAll('[role=dialog]')].find(
    (d) => d.offsetParent !== null || d.getClientRects().length
  );
  if (!dlg) return { err: 'no_step2' };

  const checks = [...dlg.querySelectorAll('input[type=checkbox],input[type=radio]')].map((el) => {
    const lab =
      (el.closest('label') && el.closest('label').innerText) ||
      (el.parentElement && el.parentElement.innerText) ||
      '';
    return {
      id: el.id || null,
      name: el.name || null,
      className: (el.className || '').toString().slice(0, 40),
      checked: !!el.checked,
      label: (lab || '').replace(/\s+/g, ' ').trim().slice(0, 80),
    };
  });
  const inputs = [...dlg.querySelectorAll('input[type=text],input:not([type]),textarea,select')].map(
    (el) => ({
      type: el.type || el.tagName,
      id: el.id || null,
      name: el.name || null,
      aria: el.getAttribute('aria-label'),
      className: (el.className || '').toString().slice(0, 50),
      value: (el.value || '').slice(0, 60),
      options: el.tagName === 'SELECT'
        ? [...el.options].slice(0, 12).map((o) => o.textContent.trim().slice(0, 40))
        : null,
    })
  );
  const buttons = [...dlg.querySelectorAll('button,[role=button],input[type=submit]')]
    .map((el) => ({
      text: (el.textContent || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 80),
      className: (el.className || '').toString().slice(0, 50),
      disabled: !!el.disabled,
    }))
    .filter((b) => b.text);
  const text = (dlg.innerText || '').replace(/\s+/g, ' ').slice(0, 1200);
  return {
    step1: {
      from: !!from,
      to: !!to,
      subj: !!subj,
      hasWords: !!hasWords,
      doesnt: !!doesnt,
    },
    checks,
    inputs,
    buttons,
    text,
  };
})()
