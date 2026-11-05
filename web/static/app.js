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
