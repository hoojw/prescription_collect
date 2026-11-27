(() => {
  "use strict";

  const INSTALLED_KEY = "__yizhujkPrescriptionNumberExportHookV1";
  const MESSAGE_SOURCE = "yizhujk-prescription-number-export";
  const utils = globalThis.YizhujkPrescriptionExportUtils;
  if (globalThis[INSTALLED_KEY] || !utils) return;
  globalThis[INSTALLED_KEY] = true;

  function publish(payload) {
    const sanitized = utils.sanitizedPagePayload(payload);
    if (!sanitized || !sanitized.records.length) return;
    window.postMessage({
      source: MESSAGE_SOURCE,
      type: "PAGE_RESULT",
      payload: sanitized,
    }, location.origin);
  }

  async function inspectFetchResponse(response, request) {
    const requestUrl = typeof request === "string" ? request : request?.url;
    const responseUrl = response?.url || requestUrl;
    if (!utils.isTargetPageApi(responseUrl, location.href) || !response?.ok) return;
    try {
      publish(await response.clone().json());
    } catch (_error) {
      // The observer must never change or block the page's own request flow.
    }
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = function observedFetch(...args) {
      const result = originalFetch.apply(this, args);
      result.then((response) => inspectFetchResponse(response, args[0])).catch(() => {});
      return result;
    };
  }

  const xhrUrls = new WeakMap();
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function observedOpen(method, url, ...args) {
    xhrUrls.set(this, String(url || ""));
    return originalOpen.call(this, method, url, ...args);
  };

  XMLHttpRequest.prototype.send = function observedSend(...args) {
    const requestUrl = xhrUrls.get(this);
    if (utils.isTargetPageApi(requestUrl, location.href)) {
      this.addEventListener("load", () => {
        if (this.status < 200 || this.status >= 300) return;
        try {
          const payload = this.responseType === "json"
            ? this.response
            : JSON.parse(this.responseText);
          publish(payload);
        } catch (_error) {
          // Ignore malformed or non-JSON responses without affecting the page.
        }
      }, { once: true });
    }
    return originalSend.apply(this, args);
  };
})();
