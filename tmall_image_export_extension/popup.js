const STATE_KEY = "tmallPrescriptionImageExportState";
const RISK_EVENTS_KEY = "tmallPrescriptionRiskEvents";

const elements = {
  status: document.querySelector("#status"),
  count: document.querySelector("#count"),
  page: document.querySelector("#page"),
  success: document.querySelector("#success"),
  failed: document.querySelector("#failed"),
  skippedNoOrder: document.querySelector("#skipped-no-order"),
  skippedNotUsed: document.querySelector("#skipped-not-used"),
  batch: document.querySelector("#batch"),
  message: document.querySelector("#message"),
  start: document.querySelector("#start"),
  stop: document.querySelector("#stop"),
  startDate: document.querySelector("#start-date"),
  endDate: document.querySelector("#end-date"),
  riskTotal: document.querySelector("#risk-total"),
  risk24h: document.querySelector("#risk-24h"),
  risk7d: document.querySelector("#risk-7d"),
  riskLatest: document.querySelector("#risk-latest"),
  riskAverage: document.querySelector("#risk-average"),
  exportRisk: document.querySelector("#export-risk"),
  clearRisk: document.querySelector("#clear-risk"),
  openOptions: document.querySelector("#open-options"),
};

function render(state = {}) {
  const running = state.status === "running" || state.status === "stopping";
  const orderMode = state.mode === "order_enrichment";
  const labels = {
    idle: "等待开始",
    running: orderMode ? "正在补全订单" : "正在导出",
    stopping: "正在停止",
    completed: "导出完成",
    stopped: "已停止",
    failed: "执行失败",
  };

  elements.status.textContent = labels[state.status] || "等待开始";
  elements.count.textContent = `${state.processed || 0} / ${state.total || 0}`;
  if (state.currentPage) {
    const completedPages = state.completedPages || 0;
    const targetPages = state.targetPages || "?";
    elements.page.textContent = `${state.currentPage}/${state.totalPages || "?"}（完成 ${completedPages}/${targetPages}）`;
  } else {
    elements.page.textContent = "-";
  }
  elements.success.textContent = String(state.success || 0);
  elements.failed.textContent = String(state.failed || 0);
  elements.skippedNoOrder.textContent = String(state.skippedNoOrder || 0);
  elements.skippedNotUsed.textContent = String(state.skippedNotUsed || 0);
  elements.batch.textContent = state.batchId || "-";
  elements.message.textContent = state.message || "";
  elements.start.disabled = running;
  elements.stop.disabled = !running;
  elements.startDate.disabled = running;
  elements.endDate.disabled = running;
}

function formatDuration(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "-";
  const minutes = Math.round(milliseconds / 60_000);
  if (minutes < 60) return `${Math.max(minutes, 1)} 分钟`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} 小时`;
  return `${Math.round(hours / 24)} 天`;
}

function formatLocalTime(value) {
  const milliseconds = Date.parse(value || "");
  if (!Number.isFinite(milliseconds)) return "-";
  return new Date(milliseconds).toLocaleString("zh-CN", { hour12: false });
}

function renderRiskStats(stats = {}) {
  elements.riskTotal.textContent = String(stats.total || 0);
  elements.risk24h.textContent = String(stats.last_24_hours || 0);
  elements.risk7d.textContent = String(stats.last_7_days || 0);
  elements.riskLatest.textContent = formatLocalTime(stats.latest_event?.occurred_at);
  elements.riskAverage.textContent = formatDuration(stats.average_interval_ms);
}

async function backgroundMessage(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) {
    throw new Error(response?.error || "扩展后台执行失败");
  }
  return response;
}

async function refreshRiskStats() {
  try {
    const response = await backgroundMessage({ type: "TMALL_RISK_STATS" });
    renderRiskStats(response.stats);
  } catch (error) {
    elements.message.textContent = `读取风控记录失败：${error.message}`;
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) {
    throw new Error("没有找到当前标签页");
  }
  if (!String(tab.url || "").startsWith("https://alihealth.taobao.com/b2c_sellercenter/citybuy/prescriptionlist")) {
    throw new Error("请先打开天猫健康处方列表页");
  }
  return tab;
}

async function sendToPage(message) {
  const tab = await activeTab();
  try {
    return await chrome.tabs.sendMessage(tab.id, message);
  } catch (error) {
    throw new Error("扩展尚未加载到页面，请刷新处方列表页后重试");
  }
}

elements.start.addEventListener("click", async () => {
  elements.message.textContent = "";
  try {
    const startDate = elements.startDate.value;
    const endDate = elements.endDate.value;
    if (!startDate || !endDate) throw new Error("请选择开始日期和结束日期");
    if (startDate > endDate) throw new Error("开始日期不能晚于结束日期");
    const response = await sendToPage({
      type: "TMALL_EXPORT_START",
      startDate,
      endDate,
    });
    if (!response?.ok) {
      throw new Error(response?.error || "无法开始导出");
    }
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

elements.stop.addEventListener("click", async () => {
  elements.message.textContent = "";
  try {
    await backgroundMessage({ type: "TMALL_FULL_COLLECT_STOP" });
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

elements.openOptions.addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

elements.exportRisk.addEventListener("click", async () => {
  elements.message.textContent = "";
  try {
    await backgroundMessage({ type: "TMALL_RISK_EXPORT" });
    elements.message.textContent = "风控记录已导出到下载目录。";
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

elements.clearRisk.addEventListener("click", async () => {
  if (!window.confirm("确定清空本机保存的全部风控记录吗？此操作无法撤销。")) {
    return;
  }
  elements.message.textContent = "";
  try {
    const response = await backgroundMessage({ type: "TMALL_RISK_CLEAR" });
    renderRiskStats(response.stats);
    elements.message.textContent = "风控记录已清空。";
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

chrome.storage.local.get(STATE_KEY).then((data) => render(data[STATE_KEY]));
if (!elements.startDate.value || !elements.endDate.value) {
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  const value = `${yesterday.getFullYear()}-${String(yesterday.getMonth() + 1).padStart(2, "0")}-${String(yesterday.getDate()).padStart(2, "0")}`;
  elements.startDate.value = value;
  elements.endDate.value = value;
}
refreshRiskStats();

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "local" && changes[STATE_KEY]) {
    render(changes[STATE_KEY].newValue);
  }
  if (areaName === "local" && changes[RISK_EVENTS_KEY]) {
    refreshRiskStats();
  }
});
