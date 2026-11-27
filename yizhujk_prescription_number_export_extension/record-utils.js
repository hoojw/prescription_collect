(function initPrescriptionExportUtils(root, factory) {
  const api = factory();
  root.YizhujkPrescriptionExportUtils = api;
  if (typeof module === "object" && module.exports) module.exports = api;
}(typeof globalThis === "object" ? globalThis : this, () => {
  "use strict";

  const PAGE_API_PATH = "/manage/prescriptionUpload/page";
  const ALLOWED_API_HOSTS = new Set(["api.yizhujk.com", "o.yizhujk.com"]);

  function cleanText(value, maxLength = 500) {
    return String(value ?? "").trim().slice(0, maxLength);
  }

  function basename(value) {
    const text = cleanText(value);
    return text.split(/[\\/]/).pop().split(/[?#]/, 1)[0];
  }

  function prescriptionNumberFromFileName(value) {
    const fileName = basename(value);
    const stem = fileName.replace(/\.[^.]+$/, "");
    return stem.replace(/_\d+$/, "").trim();
  }

  function statusFromRow(row) {
    const raw = row?.superviseStatus ?? row?.uploadStatus ?? row?.status;
    const normalized = cleanText(raw, 50).toLowerCase();
    if (["1", "success", "succeeded", "uploaded", "已上传", "上传成功", "成功"].includes(normalized)) {
      return "uploaded";
    }
    if (["2", "failed", "failure", "上传失败", "失败"].includes(normalized)) {
      return "failed";
    }
    return "unknown";
  }

  function isTargetPageApi(value, baseUrl = "https://o.yizhujk.com/") {
    try {
      const url = new URL(String(value || ""), baseUrl);
      return ALLOWED_API_HOSTS.has(url.hostname) && url.pathname === PAGE_API_PATH;
    } catch (_error) {
      return false;
    }
  }

  function sanitizedPagePayload(payload) {
    const data = payload?.data;
    if (!data || !Array.isArray(data.rows)) return null;
    const records = [];
    for (const row of data.rows) {
      const fileName = basename(row?.fileName);
      const prescriptionNumber = prescriptionNumberFromFileName(fileName);
      if (!fileName || !prescriptionNumber) continue;
      records.push({
        fileName,
        prescriptionNumber,
        status: statusFromRow(row),
      });
    }
    return {
      records,
      total: Number.isFinite(Number(data.total)) ? Number(data.total) : null,
      capturedAt: new Date().toISOString(),
    };
  }

  return {
    PAGE_API_PATH,
    basename,
    prescriptionNumberFromFileName,
    statusFromRow,
    isTargetPageApi,
    sanitizedPagePayload,
  };
}));
