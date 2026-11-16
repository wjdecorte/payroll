// General page behaviour. Loaded from base.html; the CSP (script-src 'self')
// forbids inline scripts and on* attributes, so everything binds here, either by
// event delegation on document (works for HTMX-swapped content too) or via
// htmx.onLoad for content that needs per-element listeners.
(function () {
  'use strict';

  // Default the pay period to the current month when the server left it empty.
  function setDefaultPayPeriod() {
    const input = document.querySelector('input[name="pay_period"]');
    if (input && !input.value) {
      const now = new Date();
      const yyyy = now.getFullYear();
      const mm = String(now.getMonth() + 1).padStart(2, '0');
      input.value = `${yyyy}-${mm}`;
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setDefaultPayPeriod);
  } else {
    setDefaultPayPeriod();
  }

  // <button data-action="print">
  document.addEventListener('click', function (event) {
    const button = event.target.closest('[data-action="print"]');
    if (button) {
      event.preventDefault();
      window.print();
    }
  });

  // Forms marked data-swap-errors (Approve and Void on the run panel) answer a refusal with
  // a 409 or 422 panel that carries the message and the typed values, and a run that is gone
  // with a 404 error partial; HTMX skips 4xx responses by default, so tell it to swap these
  // in. A 403 is not swapped: it comes from the auth dependency as JSON, not as a partial.
  document.addEventListener('htmx:beforeSwap', function (event) {
    const status = event.detail.xhr.status;
    const source = event.detail.requestConfig && event.detail.requestConfig.elt;
    if ((status === 404 || status === 409 || status === 422) && source && source.hasAttribute('data-swap-errors')) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  // [data-stop-propagation]: keep a click on e.g. a row's delete button from also
  // triggering the row's own hx-get. This must be a listener on the element itself
  // (a document-level listener runs after the row's), so attach it on every load.
  function stopPropagation(event) {
    event.stopPropagation();
  }

  htmx.onLoad(function (root) {
    const scope = root.querySelectorAll ? root : document;
    const elements = [];
    if (scope.matches && scope.matches('[data-stop-propagation]')) {
      elements.push(scope);
    }
    elements.push(...scope.querySelectorAll('[data-stop-propagation]'));
    for (const el of elements) {
      if (!el.dataset.stopPropagationBound) {
        el.addEventListener('click', stopPropagation);
        el.dataset.stopPropagationBound = '1';
      }
    }
  });
})();
