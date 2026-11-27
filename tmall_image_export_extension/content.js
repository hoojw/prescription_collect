(() => {
  "use strict";

  const EXPORTER_KEY = "__tmallPrescriptionCollectorV3";
  const PAGE_PATH = "/b2c_sellercenter/citybuy/prescriptionlist";
  const PAGE_SIZE = 100;
  const MAX_PAGES = 1000;
  const USED_PRESCRIPTION_STATUS = "3";
  const RISK_TEXT_PATTERNS = [
    { reason: "verification_failed", pattern: /验证失败|验证未通过|点击重试|error:guqcx3/i },
    { reason: "slider_or_captcha", pattern: /滑块|拖动.*验证|请完成验证|验证码/i },
    { reason: "rate_limited", pattern: /访问过于频繁|操作过于频繁|请求过于频繁|流量异常|稍后再试/i },
    { reason: "security_challenge", pattern: /安全验证|异常访问|存在风险|风控|账号异常/i },
  ];

  if (window[EXPORTER_KEY]) return;
  const runtime = { running: false, stopRequested: false, currentState: null };
  window[EXPORTER_KEY] = runtime;

  const isoNow = () => new Date().toISOString();
  const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

  function batchId() {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  }

  async function send(message) {
    const response = await chrome.runtime.sendMessage(message);
    if (!response?.ok) throw new Error(response?.error || "扩展后台执行失败");
    return response;
  }

  async function publishState(state) {
    runtime.currentState = state;
    await send({ type: "TMALL_EXPORT_STATE", state });
  }

  function reportRisk(reason, source = "prescription_collection") {
    chrome.runtime.sendMessage({
      type: "TMALL_RISK_EVENT",
      event: {
        reason,
        source,
        page_url: location.href,
        batch_id: runtime.currentState?.batchId || "",
        page_number: runtime.currentState?.currentPage || null,
      },
    }).catch(() => {});
  }

  function assertSafePage() {
    if (/_____tmd_____/i.test(location.href) || /\/punish(?:[/?#]|$)/i.test(location.href)) {
      reportRisk("tmd_punish_redirect");
      throw new Error("页面进入风控状态，采集已停止");
    }
    if (!location.pathname.startsWith(PAGE_PATH)) {
      throw new Error("请先打开天猫健康处方列表页");
    }
    const text = document.body?.innerText || "";
    for (const item of RISK_TEXT_PATTERNS) {
      if (item.pattern.test(text)) {
        reportRisk(item.reason);
        throw new Error("页面出现验证或访问限制，采集已停止，请人工处理");
      }
    }
  }

  function validateDate(value, label) {
    const text = String(value || "");
    if (!/^20\d{2}-\d{2}-\d{2}$/.test(text)) throw new Error(`${label}格式必须是 YYYY-MM-DD`);
    const date = new Date(`${text}T00:00:00`);
    if (!Number.isFinite(date.getTime())) throw new Error(`${label}不是有效日期`);
    return text;
  }

  function orderIds(value) {
    if (Array.isArray(value)) return [...new Set(value.map(String).filter(Boolean))];
    return [...new Set((String(value || "").match(/\d{15,20}/g) || []))];
  }

  function firstValue(object, keys) {
    for (const key of keys) {
      const value = object?.[key];
      if (value !== undefined && value !== null && value !== "") return value;
    }
    return null;
  }

  function booleanValue(value) {
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value !== 0;
    const text = String(value ?? "").trim().toLowerCase();
    if (["true", "1", "yes", "是"].includes(text)) return true;
    if (["false", "0", "no", "否", ""].includes(text)) return false;
    return null;
  }

  function deepValue(object, wantedKeys) {
    const queue = [object];
    const seen = new Set();
    while (queue.length) {
      const value = queue.shift();
      if (!value || typeof value !== "object" || seen.has(value)) continue;
      seen.add(value);
      for (const key of wantedKeys) {
        if (value[key] !== undefined && value[key] !== null && value[key] !== "") return value[key];
      }
      for (const child of Object.values(value)) {
        if (child && typeof child === "object") queue.push(child);
      }
    }
    return null;
  }

  function normalizePrescription(raw) {
    const prescriptionId = String(firstValue(raw, ["prescriptionId", "rxNo", "rxId"]) || "").trim();
    const ids = orderIds(firstValue(raw, ["bizOrderId", "orderId", "orderIds"]));
    return {
      platform: "TIANMAO",
      prescription_id: prescriptionId,
      rx_no: prescriptionId,
      order_id: ids[0] || "",
      order_ids: ids,
      patient_name: String(firstValue(raw, ["patientName", "medicalUserName"]) || "").trim(),
      patient_gender: String(firstValue(raw, ["patientSex", "gender"]) || "").trim(),
      prescription_time: String(firstValue(raw, ["sendPrescriptionTime", "prescriptionTime"]) || "").trim(),
      hospital_name: String(firstValue(raw, ["hospitalName"]) || "").trim(),
      doctor: String(firstValue(raw, ["doctorName", "doctor"]) || "").trim(),
      prescription_status: String(firstValue(raw, ["statusName", "status"]) || "").trim(),
      phone_consultation: booleanValue(firstValue(raw, ["isDoctorCall", "phoneConsultation"])),
      audit: "",
      image_url: String(firstValue(raw, ["picUrl", "imageUrl"]) || "").trim(),
      collected_at: isoNow(),
    };
  }

  function isUsedPrescription(raw, record) {
    const statusCode = String(firstValue(raw, ["status"]) || "").trim();
    const statusName = String(firstValue(raw, ["statusName"]) || record.prescription_status || "").trim();
    return statusCode === USED_PRESCRIPTION_STATUS || statusName === "已使用";
  }

  function prescriptionResponse(result) {
    const payload = result?.payload;
    if (!payload || payload.success !== true || !Array.isArray(payload.model)) {
      throw new Error(payload?.errorMsg || "处方列表接口返回结构异常");
    }
    return { records: payload.model, total: Number(payload.totalCount || payload.model.length) };
  }

  function detailModel(result) {
    const payload = result?.payload;
    if (!payload || payload.retType === -1) throw new Error("处方详情接口返回失败");
    return payload?.data?.model || payload?.data || payload;
  }

  async function enrichDetail(record) {
    const response = await send({
      type: "TMALL_PRESCRIPTION_DETAIL_FETCH",
      data: { patientId: -1, rxId: record.prescription_id, hospitalName: record.hospital_name },
    });
    const model = detailModel(response.result);
    record.image_url = String(deepValue(model, ["picUrl", "imageUrl", "rxPicUrl"]) || record.image_url || "").trim();
    const auditInfo = model && typeof model === "object" ? model.auditInfo : null;
    record.audit = String(firstValue(auditInfo, ["auditDoctorName"]) || "").trim();
    record.audit_source = record.audit ? "auditInfo.auditDoctorName" : "missing";
    return record;
  }

  async function runCollection(startDateValue, endDateValue) {
    if (runtime.running) throw new Error("已有采集任务正在运行");
    assertSafePage();
    const startDate = validateDate(startDateValue, "开始日期");
    const endDate = validateDate(endDateValue, "结束日期");
    if (startDate > endDate) throw new Error("开始日期不能晚于结束日期");

    runtime.running = true;
    runtime.stopRequested = false;
    const id = batchId();
    const records = [];
    const skipped = [];
    const errors = [];
    const seen = new Set();
    const state = {
      status: "running", mode: "prescription_collection", batchId: id,
      startDate, endDate, currentStage: "处方列表", currentPage: 0,
      totalPages: null, total: 0, processed: 0, success: 0, failed: 0,
      skippedNoOrder: 0, skippedNotUsed: 0, auditMissing: 0, imageMissing: 0,
      message: "正在读取处方列表",
    };
    await publishState(state);

    try {
      for (let pageNum = 1; pageNum <= MAX_PAGES && !runtime.stopRequested; pageNum += 1) {
        assertSafePage();
        state.currentPage = pageNum;
        state.message = `正在读取处方列表第 ${pageNum}${state.totalPages ? `/${state.totalPages}` : ""} 页`;
        await publishState(state);
        const query = {
          pageNum: String(pageNum), pageSize: String(PAGE_SIZE),
          startTime: `${startDate} 00:00:00`, endTime: `${endDate} 23:59:59`,
          status: USED_PRESCRIPTION_STATUS,
        };
        const response = await send({ type: "TMALL_PRESCRIPTION_PAGE_FETCH", query });
        const page = prescriptionResponse(response.result);
        state.total = page.total;
        state.totalPages = Math.max(1, Math.ceil(page.total / PAGE_SIZE));
        if (!page.records.length) break;

        for (const raw of page.records) {
          if (runtime.stopRequested) break;
          const record = normalizePrescription(raw);
          if (!record.prescription_id || seen.has(record.prescription_id)) continue;
          seen.add(record.prescription_id);
          if (!isUsedPrescription(raw, record)) {
            skipped.push({ prescription_id: record.prescription_id, reason: "prescription_status_not_used" });
            state.skippedNotUsed += 1;
            state.processed += 1;
            continue;
          }
          if (!record.order_id) {
            skipped.push({ prescription_id: record.prescription_id, reason: "order_id_missing" });
            state.skippedNoOrder += 1;
            state.processed += 1;
            continue;
          }
          state.currentStage = "处方详情";
          state.message = `正在获取处方详情 ${state.processed + 1}/${state.total}`;
          await publishState(state);
          try {
            await enrichDetail(record);
            if (!record.audit) state.auditMissing += 1;
            if (!record.image_url) state.imageMissing += 1;
            records.push(record);
            state.success += 1;
          } catch (error) {
            errors.push({ prescription_id: record.prescription_id, stage: "prescription_detail", error: String(error?.message || error) });
            state.failed += 1;
          }
          state.processed += 1;
          await sleep(150);
        }
        if (pageNum >= state.totalPages) break;
      }

      const batch = {
        schema_version: 2,
        platform: "TIANMAO",
        batch_id: id,
        date_range: `${startDate}..${endDate}`,
        generated_at: isoNow(),
        source: "tmall_prescription_list_and_queryRxInfo",
        summary: {
          platform_total: state.total,
          records: records.length,
          skipped_no_order: state.skippedNoOrder,
          skipped_not_used: state.skippedNotUsed,
          detail_errors: errors.length,
          audit_missing: state.auditMissing,
          image_missing: state.imageMissing,
        },
        records,
        skipped,
        errors,
      };
      await send({ type: "TMALL_RAW_BATCH_SAVE", batch });
      if (runtime.stopRequested) {
        await publishState({ ...state, status: "stopped", message: `采集已停止，已保留 ${records.length} 条内存批次数据` });
      } else {
        state.currentStage = "已完成订单";
        state.message = "处方阶段完成，正在启动已完成订单分页关联";
        await publishState(state);
        await send({ type: "TMALL_ORDER_STAGE_START" });
      }
    } catch (error) {
      await publishState({ ...state, status: "failed", message: String(error?.message || error) });
      throw error;
    } finally {
      runtime.running = false;
      runtime.stopRequested = false;
      runtime.currentState = null;
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "TMALL_EXPORT_START") {
      if (runtime.running) {
        sendResponse({ ok: false, error: "已有采集任务正在运行" });
        return false;
      }
      runCollection(message.startDate, message.endDate).catch(() => {});
      sendResponse({ ok: true });
      return false;
    }
    if (message?.type === "TMALL_EXPORT_STOP") {
      runtime.stopRequested = true;
      sendResponse({ ok: true });
      return false;
    }
    return false;
  });
})();
