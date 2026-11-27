(() => {
  "use strict";

  const MESSAGE_SOURCE = "yizhujk-prescription-number-export";

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== location.origin) return;
    if (event.data?.source !== MESSAGE_SOURCE || event.data?.type !== "PAGE_RESULT") return;
    chrome.runtime.sendMessage({
      type: "PRESCRIPTION_PAGE_CAPTURED",
      payload: event.data.payload,
    }).catch(() => {});
  });

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "PRESCRIPTION_EXPORT_PING") {
      sendResponse({ ok: true, pageUrl: location.href });
      return false;
    }
    return false;
  });
})();
