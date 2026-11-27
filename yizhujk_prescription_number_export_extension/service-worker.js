importScripts("record-utils.js");

const STATE_KEY = "yizhujkPrescriptionNumberExportState";
const EMPTY_STATE = {
  records: [],
  apiTotal: null,
  capturedPages: 0,
  updatedAt: null,
};

let writeQueue = Promise.resolve();

async function state() {
  const stored = await chrome.storage.session.get(STATE_KEY);
  return stored[STATE_KEY] || { ...EMPTY_STATE };
}

async function mergeCapture(payload) {
  const current = await state();
  const recordsByFileName = new Map(
    (current.records || []).map((record) => [record.fileName, record]),
  );

  for (const candidate of payload?.records || []) {
    const fileName = YizhujkPrescriptionExportUtils.basename(candidate?.fileName);
    const prescriptionNumber = YizhujkPrescriptionExportUtils.prescriptionNumberFromFileName(fileName);
    const status = ["uploaded", "failed", "unknown"].includes(candidate?.status)
      ? candidate.status
      : "unknown";
    if (!fileName || !prescriptionNumber) continue;
    recordsByFileName.set(fileName, { fileName, prescriptionNumber, status });
  }

  const next = {
    records: [...recordsByFileName.values()],
    apiTotal: Number.isFinite(Number(payload?.total)) ? Number(payload.total) : current.apiTotal,
    capturedPages: Number(current.capturedPages || 0) + 1,
    updatedAt: new Date().toISOString(),
  };
  await chrome.storage.session.set({ [STATE_KEY]: next });
  return next;
}

function respond(sendResponse, operation) {
  operation.then(
    (result) => sendResponse({ ok: true, ...result }),
    (error) => sendResponse({ ok: false, error: String(error?.message || error) }),
  );
  return true;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "PRESCRIPTION_PAGE_CAPTURED") {
    writeQueue = writeQueue.then(() => mergeCapture(message.payload));
    return respond(sendResponse, writeQueue.then((next) => ({ state: next })));
  }

  if (message?.type === "PRESCRIPTION_EXPORT_GET_STATE") {
    return respond(sendResponse, writeQueue.then(() => state()).then((value) => ({ state: value })));
  }

  if (message?.type === "PRESCRIPTION_EXPORT_CLEAR") {
    writeQueue = writeQueue.then(async () => {
      await chrome.storage.session.set({ [STATE_KEY]: { ...EMPTY_STATE } });
      return { ...EMPTY_STATE };
    });
    return respond(sendResponse, writeQueue.then((next) => ({ state: next })));
  }

  return false;
});
