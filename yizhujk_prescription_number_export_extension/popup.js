const STATE_KEY = "yizhujkPrescriptionNumberExportState";

const elements = {
  pages: document.querySelector("#pages"),
  apiTotal: document.querySelector("#api-total"),
  files: document.querySelector("#files"),
  prescriptions: document.querySelector("#prescriptions"),
  uploaded: document.querySelector("#uploaded"),
  failed: document.querySelector("#failed"),
  filter: document.querySelector("#filter"),
  message: document.querySelector("#message"),
  exportTxt: document.querySelector("#export-txt"),
  exportCsv: document.querySelector("#export-csv"),
  copy: document.querySelector("#copy"),
  clear: document.querySelector("#clear"),
};

let currentState = { records: [], apiTotal: null, capturedPages: 0 };

function filteredRecords() {
  const filter = elements.filter.value;
  return (currentState.records || []).filter((record) => filter === "all" || record.status === filter);
}

function uniquePrescriptionNumbers(records = filteredRecords()) {
  return [...new Set(records.map((record) => record.prescriptionNumber).filter(Boolean))];
}

function render(state = {}) {
  currentState = state;
  const records = state.records || [];
  elements.pages.textContent = String(state.capturedPages || 0);
  elements.apiTotal.textContent = state.apiTotal == null ? "-" : String(state.apiTotal);
  elements.files.textContent = String(records.length);
  elements.prescriptions.textContent = String(uniquePrescriptionNumbers(records).length);
  elements.uploaded.textContent = String(records.filter((record) => record.status === "uploaded").length);
  elements.failed.textContent = String(records.filter((record) => record.status === "failed").length);
}

async function backgroundMessage(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) throw new Error(response?.error || "扩展后台执行失败");
  return response;
}

async function assertPageReady() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !String(tab.url || "").startsWith("https://o.yizhujk.com/")) {
    throw new Error("请先打开处方上传管理页面");
  }
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "PRESCRIPTION_EXPORT_PING" });
  } catch (_error) {
    throw new Error("扩展尚未加载到页面，请刷新页面后重新查询");
  }
}

function timestamp() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

function download(text, filename, mimeType) {
  const blob = new Blob(["\ufeff", text], { type: `${mimeType};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csvCell(value) {
  return `"${String(value ?? "").replaceAll('"', '""')}"`;
}

elements.exportTxt.addEventListener("click", async () => {
  elements.message.textContent = "";
  try {
    await assertPageReady();
    const numbers = uniquePrescriptionNumbers();
    if (!numbers.length) throw new Error("当前筛选范围没有已捕获的处方号");
    download(`${numbers.join("\r\n")}\r\n`, `处方号_${elements.filter.value}_${timestamp()}.txt`, "text/plain");
    elements.message.textContent = `已导出 ${numbers.length} 个唯一处方号。`;
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

elements.exportCsv.addEventListener("click", async () => {
  elements.message.textContent = "";
  try {
    await assertPageReady();
    const records = filteredRecords();
    if (!records.length) throw new Error("当前筛选范围没有已捕获的明细");
    const statusLabels = { uploaded: "已上传", failed: "上传失败", unknown: "未知" };
    const rows = [
      ["prescription_number", "file_name", "status"],
      ...records.map((record) => [record.prescriptionNumber, record.fileName, statusLabels[record.status] || "未知"]),
    ];
    download(rows.map((row) => row.map(csvCell).join(",")).join("\r\n"), `处方号明细_${elements.filter.value}_${timestamp()}.csv`, "text/csv");
    elements.message.textContent = `已导出 ${records.length} 条文件明细。`;
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

elements.copy.addEventListener("click", async () => {
  elements.message.textContent = "";
  try {
    const numbers = uniquePrescriptionNumbers();
    if (!numbers.length) throw new Error("当前筛选范围没有已捕获的处方号");
    await navigator.clipboard.writeText(numbers.join("\n"));
    elements.message.textContent = `已复制 ${numbers.length} 个唯一处方号。`;
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

elements.clear.addEventListener("click", async () => {
  elements.message.textContent = "";
  if (!window.confirm("确定清空本次浏览器会话内已捕获的数据吗？")) return;
  try {
    const response = await backgroundMessage({ type: "PRESCRIPTION_EXPORT_CLEAR" });
    render(response.state);
    elements.message.textContent = "本次缓存已清空。";
  } catch (error) {
    elements.message.textContent = error.message;
  }
});

elements.filter.addEventListener("change", () => {
  elements.message.textContent = `当前范围：${uniquePrescriptionNumbers().length} 个唯一处方号。`;
});

backgroundMessage({ type: "PRESCRIPTION_EXPORT_GET_STATE" }).then((response) => render(response.state)).catch((error) => {
  elements.message.textContent = error.message;
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "session" && changes[STATE_KEY]) render(changes[STATE_KEY].newValue || {});
});
