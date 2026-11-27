const STATE_KEY = "tmallPrescriptionImageExportState";
const RISK_EVENTS_KEY = "tmallPrescriptionRiskEvents";
const RAW_BATCH_KEY = "tmallPrescriptionRawBatch";
const ENRICHED_BATCH_KEY = "tmallPrescriptionEnrichedBatch";
const SERVER_CONFIG_KEY = "tmallPrescriptionServerConfig";
const RISK_DEDUPLICATION_MS = 5 * 60 * 1000;
const RISK_EVENT_LIMIT = 1000;
let riskWriteQueue = Promise.resolve();

function respondAsync(sendResponse, promise) {
  promise.then(
    (value) => sendResponse({ ok: true, ...value }),
    (error) => sendResponse({ ok: false, error: String(error?.message || error) }),
  );
  return true;
}

async function downloadUrl(url, filename) {
  const downloadId = await chrome.downloads.download({
    url,
    filename,
    conflictAction: "uniquify",
    saveAs: false,
  });
  return { downloadId };
}

async function fetchSoldOrdersInPage(tabId, form) {
  if (!Number.isInteger(tabId)) {
    throw new Error("无法确定天猫订单页标签");
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [form],
    func: async (requestForm) => {
      const endpoint = "https://trade.taobao.com/trade/itemlist/asyncSold.htm?event_submit_do_query=1&_input_charset=utf8";
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" },
        body: new URLSearchParams(requestForm).toString(),
      });
      const contentType = response.headers.get("content-type") || "";
      const declared = contentType.match(/charset\s*=\s*["']?([^;"'\s]+)/i)?.[1]?.toLowerCase() || "";
      const encoding = /^(?:gbk|gb2312|gb18030)$/.test(declared)
        ? "gb18030"
        : declared === "utf-8" || declared === "utf8"
          ? "utf-8"
          : "gb18030";
      const bytes = await response.arrayBuffer();
      let text;
      try {
        text = new TextDecoder(encoding).decode(bytes);
      } catch (_error) {
        text = new TextDecoder("gb18030").decode(bytes);
      }
      return {
        ok: response.ok,
        status: response.status,
        final_url: response.url,
        response_encoding: encoding,
        text,
      };
    },
  });
  const result = results?.[0]?.result;
  if (!result) {
    throw new Error("订单页没有返回接口结果");
  }
  return result;
}

async function fetchPrescriptionPageInPage(tabId, query) {
  if (!Number.isInteger(tabId)) throw new Error("无法确定天猫处方页标签");
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [query],
    func: async (requestQuery) => {
      const endpoint = new URL("https://alihealthyao.taobao.com/trademanager/searchPrescriptionOrder.htm");
      Object.entries(requestQuery || {}).forEach(([key, value]) => endpoint.searchParams.set(key, String(value)));
      const response = await fetch(endpoint, { credentials: "include", mode: "cors" });
      const text = await response.text();
      if (!response.ok) return { ok: false, status: response.status, final_url: response.url, error: `HTTP ${response.status}` };
      try {
        const normalized = text.replace(/"bizOrderId":(\d+)/g, '"bizOrderId":"$1"');
        return { ok: true, status: response.status, final_url: response.url, payload: JSON.parse(normalized) };
      } catch (_error) {
        return { ok: false, status: response.status, final_url: response.url, error: "处方列表未返回JSON" };
      }
    },
  });
  const result = results?.[0]?.result;
  if (!result?.ok) throw new Error(result?.error || "处方列表接口失败");
  return result;
}

async function fetchPrescriptionDetailInPage(tabId, data) {
  if (!Number.isInteger(tabId)) throw new Error("无法确定天猫处方页标签");
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [data],
    func: async (requestData) => {
      const deadline = Date.now() + 10_000;
      while (!globalThis.lib?.mtop?.request && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      const mtop = globalThis.lib?.mtop;
      if (!mtop?.request) return { ok: false, error: "页面MTop运行时不可用" };
      const request = {
        api: "mtop.alihealth.rxms.sellerCenter.queryRxInfo",
        data: requestData,
        v: "1.0",
        dataType: "json",
      };
      return await new Promise((resolve) => {
        let settled = false;
        const finish = (ok, value) => {
          if (settled) return;
          settled = true;
          let payload = value;
          try { payload = JSON.parse(JSON.stringify(value)); } catch (_error) {}
          resolve(ok ? { ok: true, payload } : { ok: false, error: JSON.stringify(payload) });
        };
        const timeout = setTimeout(() => finish(false, "处方详情请求超时"), 20_000);
        const success = (value) => { clearTimeout(timeout); finish(true, value); };
        const failure = (value) => { clearTimeout(timeout); finish(false, value); };
        try {
          const returned = mtop.request(request, success, failure);
          if (returned?.then) returned.then(success, failure);
        } catch (error) {
          failure(String(error?.message || error));
        }
      });
    },
  });
  const result = results?.[0]?.result;
  if (!result?.ok) throw new Error(result?.error || "处方详情接口失败");
  return result;
}

async function serverConfig() {
  const data = await chrome.storage.local.get(SERVER_CONFIG_KEY);
  const value = data[SERVER_CONFIG_KEY];
  return value && typeof value === "object" ? value : {};
}

async function sendEnrichedBatch(batch) {
  if (!batch?.batch_id || !Array.isArray(batch.records)) {
    return { status: "failed", error: "汇总批次结构无效" };
  }
  const config = await serverConfig();
  const endpoint = String(config.endpoint || "").trim();
  const token = String(config.token || "").trim();
  if (!endpoint || !token) return { status: "not_configured" };

  let parsed;
  try {
    parsed = new URL(endpoint);
  } catch (_error) {
    return { status: "failed", error: "服务器接收地址无效" };
  }
  if (!/^https?:$/.test(parsed.protocol) || parsed.username || parsed.password) {
    return { status: "failed", error: "服务器接收地址必须是无内嵌凭据的 HTTP(S) 地址" };
  }

  const startedAt = new Date().toISOString();
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60_000);
    try {
      const response = await fetch(parsed.href, {
        method: "POST",
        headers: {
          "Accept": "application/json",
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json;charset=UTF-8",
        },
        body: JSON.stringify(batch),
        signal: controller.signal,
      });
      let payload = null;
      try { payload = await response.json(); } catch (_error) {}
      if (response.ok && ["accepted", "duplicate"].includes(payload?.status)) {
        return {
          status: payload.status,
          http_status: response.status,
          batch_id: String(payload.batch_id || batch.batch_id),
          records: Number(payload.records ?? batch.records.length),
          queue_status: String(payload.queue_status || "queued"),
          attempts: attempt,
          started_at: startedAt,
          finished_at: new Date().toISOString(),
        };
      }
      if (response.status < 500 || attempt === 3) {
        return {
          status: "failed",
          http_status: response.status,
          error: String(payload?.code || `服务器返回 HTTP ${response.status}`),
          attempts: attempt,
          started_at: startedAt,
          finished_at: new Date().toISOString(),
        };
      }
    } catch (error) {
      if (attempt === 3) {
        return {
          status: "failed",
          error: error?.name === "AbortError" ? "服务器接收超时" : "无法连接服务器接收地址",
          attempts: attempt,
          started_at: startedAt,
          finished_at: new Date().toISOString(),
        };
      }
    } finally {
      clearTimeout(timeout);
    }
  }
  return { status: "failed", error: "服务器接收失败" };
}

async function waitForTabComplete(tabId, timeoutMs = 30_000) {
  const current = await chrome.tabs.get(tabId);
  if (current.status === "complete") return;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("订单页加载超时"));
    }, timeoutMs);
    const listener = (updatedId, info) => {
      if (updatedId !== tabId || info.status !== "complete") return;
      clearTimeout(timeout);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function startOrderStage() {
  let [tab] = await chrome.tabs.query({ url: "https://myseller.taobao.com/home.htm/trade-platform/tp/sold*" });
  if (!tab) {
    tab = await chrome.tabs.create({
      url: "https://myseller.taobao.com/home.htm/trade-platform/tp/sold",
      active: false,
    });
  }
  await waitForTabComplete(tab.id);
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      const response = await chrome.tabs.sendMessage(tab.id, { type: "TMALL_ORDER_ENRICH_START" });
      if (!response?.ok) throw new Error(response?.error || "无法启动订单补全");
      return { tab_id: tab.id };
    } catch (error) {
      if (attempt === 19) throw new Error("订单页尚未加载插件，请确认订单登录态后刷新页面");
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  throw new Error("无法启动订单补全");
}

async function stopAllStages() {
  const tabs = await chrome.tabs.query({
    url: [
      "https://alihealth.taobao.com/b2c_sellercenter/citybuy/prescriptionlist*",
      "https://myseller.taobao.com/home.htm/trade-platform/tp/sold*",
    ],
  });
  await Promise.all(tabs.map((tab) => chrome.tabs.sendMessage(tab.id, { type: "TMALL_EXPORT_STOP" }).catch(() => null)));
  return { tabs_notified: tabs.length };
}

function isoNow() {
  return new Date().toISOString();
}

function compactTimestamp() {
  return isoNow().replace(/[-:]/g, "").replace("T", "_").replace(/\.\d{3}Z$/, "Z");
}

function safePageUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return `${url.origin}${url.pathname}`;
  } catch (_error) {
    return "";
  }
}

function riskReasonFromUrl(value) {
  const url = String(value || "");
  if (/_____tmd_____/i.test(url)) {
    return "tmd_punish_redirect";
  }
  if (/\/punish(?:[/?#]|$)/i.test(url)) {
    return "punish_redirect";
  }
  return "";
}

async function riskEvents() {
  const data = await chrome.storage.local.get(RISK_EVENTS_KEY);
  return Array.isArray(data[RISK_EVENTS_KEY]) ? data[RISK_EVENTS_KEY] : [];
}

function optionalPageNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

async function recordRiskEvent(input = {}) {
  const occurredAt = input.occurred_at || isoNow();
  const occurredMs = Date.parse(occurredAt);
  const reason = String(input.reason || "unknown_risk");
  const tabId = Number.isInteger(input.tab_id) ? input.tab_id : null;
  const events = await riskEvents();
  let matchingEvent = null;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const candidate = events[index];
    if (candidate.reason === reason && candidate.tab_id === tabId) {
      matchingEvent = candidate;
      break;
    }
  }
  const latestSeenMs = Date.parse(matchingEvent?.last_seen_at || matchingEvent?.occurred_at || "");
  const sameEpisode = matchingEvent
    && Number.isFinite(occurredMs)
    && Number.isFinite(latestSeenMs)
    && occurredMs - latestSeenMs >= 0
    && occurredMs - latestSeenMs <= RISK_DEDUPLICATION_MS;

  if (sameEpisode) {
    matchingEvent.last_seen_at = occurredAt;
    matchingEvent.observations = Number(matchingEvent.observations || 1) + 1;
    if (input.source) matchingEvent.source = String(input.source);
    if (input.page_url) matchingEvent.page_url = safePageUrl(input.page_url);
    if (input.batch_id) matchingEvent.batch_id = String(input.batch_id);
    const pageNumber = optionalPageNumber(input.page_number);
    if (pageNumber !== null) matchingEvent.page_number = pageNumber;
  } else {
    events.push({
      event_id: `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`,
      occurred_at: occurredAt,
      last_seen_at: occurredAt,
      observations: 1,
      reason,
      source: String(input.source || "unknown"),
      page_url: safePageUrl(input.page_url),
      tab_id: tabId,
      batch_id: input.batch_id ? String(input.batch_id) : "",
      page_number: optionalPageNumber(input.page_number),
    });
  }

  const retained = events.slice(-RISK_EVENT_LIMIT);
  await chrome.storage.local.set({ [RISK_EVENTS_KEY]: retained });
  return { deduplicated: Boolean(sameEpisode), stats: buildRiskStats(retained) };
}

function enqueueRiskEvent(input) {
  const operation = riskWriteQueue.then(() => recordRiskEvent(input));
  riskWriteQueue = operation.catch(() => {});
  return operation;
}

function buildRiskStats(events) {
  const ordered = [...events]
    .filter((event) => Number.isFinite(Date.parse(event.occurred_at)))
    .sort((left, right) => Date.parse(left.occurred_at) - Date.parse(right.occurred_at));
  const now = Date.now();
  const intervals = [];
  for (let index = 1; index < ordered.length; index += 1) {
    intervals.push(Date.parse(ordered[index].occurred_at) - Date.parse(ordered[index - 1].occurred_at));
  }
  return {
    total: ordered.length,
    observations: ordered.reduce((sum, event) => sum + Number(event.observations || 1), 0),
    last_24_hours: ordered.filter((event) => now - Date.parse(event.occurred_at) <= 24 * 60 * 60 * 1000).length,
    last_7_days: ordered.filter((event) => now - Date.parse(event.occurred_at) <= 7 * 24 * 60 * 60 * 1000).length,
    latest_event: ordered.at(-1) || null,
    last_interval_ms: intervals.at(-1) ?? null,
    average_interval_ms: intervals.length
      ? Math.round(intervals.reduce((sum, value) => sum + value, 0) / intervals.length)
      : null,
  };
}

async function riskLogSnapshot() {
  const events = await riskEvents();
  return {
    generated_at: isoNow(),
    definition: "同一标签页、同一原因在5分钟内连续出现，合并为一次风控事件",
    stats: buildRiskStats(events),
    events,
  };
}

async function exportRiskLog() {
  const snapshot = await riskLogSnapshot();
  const body = JSON.stringify(snapshot, null, 2);
  const url = `data:application/json;charset=utf-8,${encodeURIComponent(body)}`;
  return downloadUrl(url, `天猫风控记录/risk-control-log_${compactTimestamp()}.json`);
}

async function handleRiskNavigation(details) {
  if (details.frameId !== 0) {
    return;
  }
  const reason = riskReasonFromUrl(details.url);
  if (!reason) {
    return;
  }
  await enqueueRiskEvent({
    reason,
    source: "navigation",
    page_url: details.url,
    tab_id: details.tabId,
  });
}

chrome.webNavigation.onCommitted.addListener((details) => {
  handleRiskNavigation(details).catch(() => {});
});

chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  handleRiskNavigation(details).catch(() => {});
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "TMALL_EXPORT_STATE") {
    return respondAsync(
      sendResponse,
      chrome.storage.local.set({ [STATE_KEY]: message.state }).then(() => ({})),
    );
  }

  if (message?.type === "TMALL_RAW_BATCH_SAVE") {
    return respondAsync(
      sendResponse,
      chrome.storage.session.set({ [RAW_BATCH_KEY]: message.batch }).then(() => ({})),
    );
  }

  if (message?.type === "TMALL_RAW_BATCH_GET") {
    return respondAsync(
      sendResponse,
      chrome.storage.session.get(RAW_BATCH_KEY).then((data) => ({ batch: data[RAW_BATCH_KEY] || null })),
    );
  }

  if (message?.type === "TMALL_ENRICHED_BATCH_SAVE") {
    return respondAsync(
      sendResponse,
      chrome.storage.session.set({ [ENRICHED_BATCH_KEY]: message.batch }).then(() => ({})),
    );
  }

  if (message?.type === "TMALL_ENRICHED_BATCH_SEND") {
    return respondAsync(
      sendResponse,
      sendEnrichedBatch(message.batch).then((delivery) => ({ delivery })),
    );
  }

  if (message?.type === "TMALL_PRESCRIPTION_PAGE_FETCH") {
    return respondAsync(
      sendResponse,
      fetchPrescriptionPageInPage(_sender.tab?.id, message.query).then((result) => ({ result })),
    );
  }

  if (message?.type === "TMALL_PRESCRIPTION_DETAIL_FETCH") {
    return respondAsync(
      sendResponse,
      fetchPrescriptionDetailInPage(_sender.tab?.id, message.data).then((result) => ({ result })),
    );
  }

  if (message?.type === "TMALL_ORDER_STAGE_START") {
    return respondAsync(sendResponse, startOrderStage());
  }

  if (message?.type === "TMALL_FULL_COLLECT_STOP") {
    return respondAsync(sendResponse, stopAllStages());
  }

  if (message?.type === "TMALL_ORDER_PAGE_FETCH") {
    return respondAsync(
      sendResponse,
      fetchSoldOrdersInPage(_sender.tab?.id, message.form).then((result) => ({ result })),
    );
  }

  if (message?.type === "TMALL_RISK_EVENT") {
    const input = { ...(message.event || {}), tab_id: _sender.tab?.id ?? null };
    return respondAsync(sendResponse, enqueueRiskEvent(input));
  }

  if (message?.type === "TMALL_RISK_STATS") {
    return respondAsync(sendResponse, riskLogSnapshot());
  }

  if (message?.type === "TMALL_RISK_EXPORT") {
    return respondAsync(sendResponse, exportRiskLog());
  }

  if (message?.type === "TMALL_RISK_CLEAR") {
    return respondAsync(
      sendResponse,
      chrome.storage.local.set({ [RISK_EVENTS_KEY]: [] }).then(() => ({ stats: buildRiskStats([]) })),
    );
  }

  return false;
});
