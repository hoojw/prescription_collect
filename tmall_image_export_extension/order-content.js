(() => {
  "use strict";

  const EXPORTER_KEY = "__tmallPrescriptionOrderEnricherV2";
  const ORDER_PAGE_PATH = "/home.htm/trade-platform/tp/sold";
  const REQUESTED_PAGE_SIZE = 100;
  const MAX_API_PAGES = 1000;

  if (window[EXPORTER_KEY]) return;
  const runtime = { running: false, stopRequested: false, currentState: null };
  window[EXPORTER_KEY] = runtime;
  const isoNow = () => new Date().toISOString();

  async function send(message) {
    const response = await chrome.runtime.sendMessage(message);
    if (!response?.ok) throw new Error(response?.error || "扩展后台执行失败");
    return response;
  }

  async function publishState(state) {
    runtime.currentState = state;
    await send({ type: "TMALL_EXPORT_STATE", state });
  }

  function reportRisk(reason) {
    chrome.runtime.sendMessage({
      type: "TMALL_RISK_EVENT",
      event: {
        reason,
        source: "order_batch_enrichment",
        page_url: location.href,
        batch_id: runtime.currentState?.batchId || "",
      },
    }).catch(() => {});
  }

  function assertSafeOrderPage() {
    if (/_____tmd_____/i.test(location.href) || /\/punish(?:[/?#]|$)/i.test(location.href)) {
      reportRisk("tmd_punish_redirect");
      throw new Error("订单页进入风控状态，已停止批量补全");
    }
    if (!location.pathname.startsWith(ORDER_PAGE_PATH)) {
      throw new Error("请先打开天猫『已卖出宝贝』页面");
    }
  }

  function parseLocalDate(value) {
    const match = String(value || "").match(/(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})/);
    if (!match) return null;
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    return Number.isFinite(date.getTime()) ? date : null;
  }

  function queryDateRange(records) {
    const dates = records.map((record) => parseLocalDate(record.prescription_time)).filter(Boolean);
    if (!dates.length) throw new Error("处方批次缺少可解析的开方日期");
    const minimum = new Date(Math.min(...dates.map((date) => date.getTime())));
    const maximum = new Date(Math.max(...dates.map((date) => date.getTime())));
    minimum.setDate(minimum.getDate() - 1);
    minimum.setHours(0, 0, 0, 0);
    maximum.setDate(maximum.getDate() + 1);
    maximum.setHours(23, 59, 59, 999);
    return { dateBegin: minimum.getTime(), dateEnd: maximum.getTime() };
  }

  function firstValue(object, paths) {
    for (const path of paths) {
      let value = object;
      for (const key of path.split(".")) value = value?.[key];
      if (value !== undefined && value !== null && value !== "") return value;
    }
    return null;
  }

  function arrayValue(object, paths) {
    const value = firstValue(object, paths);
    return Array.isArray(value) ? value : [];
  }

  function decimalText(value) {
    const match = String(value ?? "").replace(/,/g, "").match(/-?\d+(?:\.\d+)?/);
    return match ? Number(match[0]).toFixed(2) : null;
  }

  function integerValue(value) {
    const number = Number(String(value ?? "").match(/\d+/)?.[0]);
    return Number.isInteger(number) && number > 0 ? number : null;
  }

  function flattenNamedValues(value, output = []) {
    if (Array.isArray(value)) {
      value.forEach((item) => flattenNamedValues(item, output));
    } else if (value && typeof value === "object") {
      const name = value.name ?? value.key ?? value.label ?? value.title;
      const data = value.value ?? value.text ?? value.content;
      if (name !== undefined && data !== undefined) output.push([String(name), String(data)]);
      Object.values(value).forEach((item) => flattenNamedValues(item, output));
    }
    return output;
  }

  function merchantCode(subOrder) {
    const itemInfo = subOrder?.itemInfo || {};
    const direct = firstValue(subOrder, [
      "itemInfo.outerId", "itemInfo.outer_id", "itemInfo.skuOuterId", "itemInfo.outerSkuId",
      "itemInfo.skuInfo.outerId", "skuInfo.outerId", "outerId", "outer_id",
      "skuOuterId", "outerSkuId", "merchantCode",
    ]);
    if (direct) return String(direct).trim();
    const named = flattenNamedValues([itemInfo.skuText, itemInfo.extra, subOrder.extra]);
    const pair = named.find(([name]) => /商家编码|商品编码/.test(name));
    if (pair?.[1]) return pair[1].trim();
    const text = JSON.stringify([itemInfo.skuText, itemInfo.extra, subOrder.extra]);
    return text.match(/(?:商家编码|商品编码)[^\w\u4e00-\u9fff-]*([\w.-]+)/)?.[1] || "";
  }

  function orderIdOf(order) {
    const value = firstValue(order, ["id", "orderId", "bizOrderId", "mainOrderId", "orderInfo.id"]);
    return String(value || "").match(/\d{15,20}/)?.[0] || "";
  }

  function subOrderIdOf(subOrder) {
    const value = firstValue(subOrder, ["id", "orderId", "bizOrderId", "subOrderId", "orderInfo.id"]);
    return String(value || "").match(/\d{15,20}/)?.[0] || "";
  }

  function parseProduct(subOrder, index) {
    return {
      source_index: index,
      drug_name: String(firstValue(subOrder, ["itemInfo.title", "title", "itemTitle"]) || "").trim(),
      drcode: merchantCode(subOrder),
      unit_price: decimalText(firstValue(subOrder, [
        "priceInfo.realTotal", "priceInfo.price", "itemInfo.price", "unitPrice", "price",
      ])),
      quantity: integerValue(firstValue(subOrder, ["quantity", "num", "itemInfo.quantity"])),
    };
  }

  function parseOrder(order) {
    const subOrders = arrayValue(order, ["subOrders", "subOrderList", "orders"]);
    const orderId = orderIdOf(order);
    const aliasIds = [...new Set([orderId, ...subOrders.map(subOrderIdOf)].filter(Boolean))];
    const products = subOrders.map(parseProduct);
    return {
      order_id: orderId,
      order_alias_ids: aliasIds,
      order_status: String(firstValue(order, [
        "statusInfo.text", "statusInfo.statusText", "orderStatusName", "status", "tradeStatus",
      ]) || "已完成"),
      total_fee: decimalText(firstValue(order, [
        "payInfo.actualFee", "payInfo.actual_fee", "actualFee", "orderInfo.actualFee",
      ])),
      products,
    };
  }

  function responseData(payload) {
    for (const candidate of [payload, payload?.data, payload?.data?.data, payload?.result]) {
      if (candidate && Array.isArray(candidate.mainOrders)) return candidate;
    }
    return null;
  }

  function totalPagesOf(data) {
    const page = data?.page || data?.pagination || {};
    const direct = Number(page.totalPage ?? page.totalPages ?? data.totalPage ?? data.totalPages);
    if (Number.isFinite(direct) && direct > 0) return direct;
    const total = Number(page.total ?? page.totalNumber ?? data.total);
    const size = Number(page.pageSize ?? page.defaultPageSize ?? data.pageSize ?? REQUESTED_PAGE_SIZE);
    return Number.isFinite(total) && total >= 0 && Number.isFinite(size) && size > 0
      ? Math.ceil(total / size)
      : null;
  }

  function parseApiText(result) {
    if (/_____tmd_____|\/punish(?:[/?#]|$)/i.test(result.final_url || "")) {
      reportRisk("order_api_punish_redirect");
      throw new Error("订单接口被重定向到风控页");
    }
    if (!result.ok) throw new Error(`订单接口 HTTP ${result.status}`);
    const text = String(result.text || "").trim();
    if (/登录|login|验证码|滑块|安全验证|异常访问/i.test(text.slice(0, 2000)) && !text.startsWith("{")) {
      throw new Error("订单登录态失效或出现验证页面");
    }
    try {
      return JSON.parse(text);
    } catch (_error) {
      throw new Error("订单接口未返回 JSON，可能登录失效或页面结构已变化");
    }
  }

  async function runOrderEnrichment() {
    if (runtime.running) throw new Error("已有订单补全任务正在运行");
    assertSafeOrderPage();
    const response = await send({ type: "TMALL_RAW_BATCH_GET" });
    const batch = response.batch;
    if (!batch?.records?.length) throw new Error("没有处方字段批次，请先在处方列表页执行字段补采");

    const state = {
      status: "running", mode: "order_enrichment", batchId: batch.batch_id,
      total: batch.records.length, processed: 0, success: 0, failed: 0,
      skippedOrders: 0, currentPage: 0, totalPages: null,
      message: "正在批量读取已完成订单",
    };
    runtime.running = true;
    runtime.stopRequested = false;
    await publishState(state);

    const dateRange = queryDateRange(batch.records);
    const ordersById = new Map();
    const errors = [];
    try {
      for (let pageNum = 1; pageNum <= MAX_API_PAGES && !runtime.stopRequested; pageNum += 1) {
        state.currentPage = pageNum;
        state.message = `正在读取已完成订单第 ${pageNum}${state.totalPages ? `/${state.totalPages}` : ""} 页`;
        await publishState(state);
        const form = {
          action: "itemlist/SoldQueryAction",
          tabCode: "latest3Months",
          orderStatus: "TRADE_FINISHED",
          pageNum: String(pageNum),
          pageSize: String(REQUESTED_PAGE_SIZE),
          prePageNo: String(Math.max(1, pageNum - 1)),
          lastStartRow: "",
          sifg: "0",
          close: "0",
          isQnNew: "true",
          isHideNick: "true",
          queryMore: "false",
          dateBegin: String(dateRange.dateBegin),
          dateEnd: String(dateRange.dateEnd),
        };
        const fetched = await send({ type: "TMALL_ORDER_PAGE_FETCH", form });
        const payload = parseApiText(fetched.result);
        const data = responseData(payload);
        if (!data) throw new Error("订单接口 JSON 中没有 mainOrders");
        const pageOrders = data.mainOrders;
        const totalPages = totalPagesOf(data);
        if (totalPages) state.totalPages = totalPages;
        let newIds = 0;
        for (const rawOrder of pageOrders) {
          const order = parseOrder(rawOrder);
          if (order.order_id) {
            let added = false;
            for (const aliasId of order.order_alias_ids) {
              if (!ordersById.has(aliasId)) {
                ordersById.set(aliasId, order);
                added = true;
              }
            }
            if (added) newIds += 1;
          }
        }
        if (!pageOrders.length || !newIds || (state.totalPages && pageNum >= state.totalPages)) break;
      }
    } catch (error) {
      errors.push({ stage: "order_page_fetch", error: String(error?.message || error) });
    }

    const collected = [];
    const skipped = [];
    let multipleOrderIdRecords = 0;
    let multipleCompletedOrderRecords = 0;
    for (const record of batch.records) {
      const orderIds = Array.isArray(record.order_ids) ? record.order_ids.filter(Boolean) : [record.order_id].filter(Boolean);
      if (orderIds.length > 1) multipleOrderIdRecords += 1;
      const matchedOrders = [];
      const matchedMainOrderIds = new Set();
      for (const orderId of orderIds) {
        const order = ordersById.get(String(orderId));
        if (order && !matchedMainOrderIds.has(order.order_id)) {
          matchedMainOrderIds.add(order.order_id);
          matchedOrders.push(order);
        }
      }
      if (!matchedOrders.length) {
        skipped.push({
          prescription_id: record.prescription_id,
          order_id: record.order_id,
          order_ids: orderIds,
          reason: "order_not_completed_or_not_found",
        });
      } else {
        if (matchedOrders.length > 1) multipleCompletedOrderRecords += 1;
        collected.push({
          ...record,
          order_link_status: "completed_order_found",
          matched_order_ids: [...matchedMainOrderIds],
          order: matchedOrders.length === 1 ? matchedOrders[0] : null,
          orders: matchedOrders,
          total_fee: matchedOrders.length === 1 ? matchedOrders[0].total_fee : null,
        });
      }
      state.processed += 1;
    }
    state.success = collected.length;
    state.skippedOrders = skipped.length;
    state.failed = errors.length;

    const resultPackage = {
      schema_version: 2,
      platform: "TIANMAO",
      batch_id: batch.batch_id,
      generated_at: isoNow(),
      source: "tmall_sold_order_asyncSold_batch",
      validation_stage: "server_after_ingest",
      order_date_range: { begin_ms: dateRange.dateBegin, end_ms: dateRange.dateEnd },
      partial: runtime.stopRequested || errors.length > 0,
      summary: {
        prescription_records: batch.records.length,
        completed_records: collected.length,
        skipped_orders: skipped.length,
        browser_review_records: 0,
        multiple_order_id_records: multipleOrderIdRecords,
        multiple_completed_order_records: multipleCompletedOrderRecords,
        errors: errors.length,
        order_pages_scanned: state.currentPage,
        completed_order_ids_loaded: ordersById.size,
      },
      records: collected,
      skipped,
      errors,
    };
    await send({ type: "TMALL_ENRICHED_BATCH_SAVE", batch: resultPackage });
    let delivery;
    try {
      const response = await send({ type: "TMALL_ENRICHED_BATCH_SEND", batch: resultPackage });
      delivery = response.delivery || { status: "failed", error: "服务器没有返回接收状态" };
    } catch (error) {
      delivery = { status: "failed", error: String(error?.message || error) };
    }
    resultPackage.delivery = delivery;
    await send({ type: "TMALL_ENRICHED_BATCH_SAVE", batch: resultPackage });
    const deliveryMessage = ["accepted", "duplicate"].includes(delivery.status)
      ? `服务器已接收 ${delivery.records ?? collected.length} 条，状态 ${delivery.status}`
      : delivery.status === "not_configured"
        ? "等待配置服务器接收地址"
        : `服务器发送失败，批次已保留：${delivery.error || "未知错误"}`;
    await publishState({
      ...state,
      status: runtime.stopRequested ? "stopped" : errors.length ? "failed" : "completed",
      message: errors.length
        ? `订单分页读取失败：${errors[0].error}`
        : `订单汇总完成：已关联 ${collected.length}，未找到已完成订单 ${skipped.length}；字段完整性由服务器判断；${deliveryMessage}`,
    });
    runtime.running = false;
    runtime.stopRequested = false;
    runtime.currentState = null;
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "TMALL_ORDER_ENRICH_START") {
      if (runtime.running) {
        sendResponse({ ok: false, error: "已有订单补全任务正在运行" });
        return false;
      }
      runOrderEnrichment().catch(async (error) => {
        runtime.running = false;
        await publishState({ status: "failed", mode: "order_enrichment", message: String(error?.message || error) }).catch(() => {});
      });
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
