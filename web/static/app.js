/* Behaviour that htmx would otherwise evaluate from hx-on attributes, kept in
   a real file so the CSP can stay script-src 'self' (no unsafe-eval). */
document.body.addEventListener('htmx:beforeSwap', (event) => {
  const status = event.detail.xhr.status;
  if (status === 422 || status === 409) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});
document.body.addEventListener('htmx:afterRequest', (event) => {
  const target = event.detail.elt;
  const redirect = target && target.getAttribute('data-redirect-on-success');
  if (redirect && event.detail.successful) {
    window.location = redirect;
  }
});

/* Theme: dark-first, light override stored per browser. */
(() => {
  const root = document.documentElement;
  const key = 'voip-edge-theme';
  const meta = document.querySelector('meta[name="theme-color"]');
  try {
    const stored = localStorage.getItem(key);
    if (stored === 'light' || stored === 'dark') {
      root.dataset.theme = stored;
    }
  } catch {
    /* storage may be unavailable in private mode */
  }

  const apply = (theme) => {
    root.dataset.theme = theme;
    if (meta) {
      meta.setAttribute('content', theme === 'light' ? '#f5f7fb' : '#0b0f14');
    }
  };
  if (meta) {
    meta.setAttribute('content', root.dataset.theme === 'light' ? '#f5f7fb' : '#0b0f14');
  }

  const button = document.getElementById('theme-toggle');
  if (!button) {
    return;
  }
  button.addEventListener('click', () => {
    const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    apply(next);
    try {
      localStorage.setItem(key, next);
    } catch {
      /* ignore */
    }
  });
})();

/* Wizard domain mode: subdomain + base domain, or a custom full domain. */
(() => {
  const sync = (form) => {
    if (!form) return;
    const checked = form.querySelector('input[name="domain_mode"]:checked');
    const hidden = form.querySelector('input[type="hidden"][name="domain_mode"]');
    const mode = checked ? checked.value : (hidden ? hidden.value : 'custom');
    form.querySelectorAll('[data-domain-mode-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.domainModePanel !== mode;
    });
    const sub = form.querySelector('#field-domain_subdomain');
    const base = form.querySelector('#field-domain_base');
    const preview = form.querySelector('#domain-preview');
    const subValue = sub ? sub.value.trim().replace(/^\.+|\.+$/g, '') : '';
    const baseValue = base ? base.value.trim().replace(/^\.+|\.+$/g, '') : '';
    const full = subValue && baseValue ? `${subValue}.${baseValue}` : '—';
    if (preview) preview.textContent = full;
    if (mode === 'custom') {
      const custom = form.querySelector('#field-domain');
      if (custom && !custom.value.trim() && subValue && baseValue) {
        custom.value = `${subValue}.${baseValue}`;
      }
    }
  };

  document.addEventListener('change', (event) => {
    if (event.target.closest('input[name="domain_mode"], #field-domain_subdomain, #field-domain_base')) {
      sync(event.target.form);
    }
  });
  document.addEventListener('input', (event) => {
    if (event.target.closest('#field-domain_subdomain')) {
      sync(event.target.form);
    }
  });
})();
