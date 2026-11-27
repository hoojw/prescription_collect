import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const files = ["service-worker.js", "content.js", "order-content.js", "popup.js", "options.js"];

for (const file of files) {
  const result = spawnSync(process.execPath, ["--check", join(root, file)], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(`${file} syntax check failed\n${result.stderr}`);
}

const manifest = JSON.parse(await readFile(join(root, "manifest.json"), "utf8"));
for (const permission of ["storage", "scripting", "tabs"]) {
  if (!manifest.permissions.includes(permission)) throw new Error(`missing permission: ${permission}`);
}
if (!manifest.options_page || !manifest.optional_host_permissions?.includes("https://*/*")) {
  throw new Error("server endpoint configuration is incomplete");
}

const popup = await readFile(join(root, "popup.html"), "utf8");
for (const id of ["start-date", "end-date", "start", "stop", "open-options"]) {
  if (!popup.includes(`id="${id}"`)) throw new Error(`missing popup element: ${id}`);
}
for (const removedId of ["max-pages", "download-images", "enrich-orders", "send-current"]) {
  if (popup.includes(`id="${removedId}"`)) throw new Error(`obsolete popup element remains: ${removedId}`);
}

const content = await readFile(join(root, "content.js"), "utf8");
for (const marker of ["startTime", "endTime", "TMALL_PRESCRIPTION_PAGE_FETCH", "USED_PRESCRIPTION_STATUS", 'status: USED_PRESCRIPTION_STATUS', 'model.auditInfo', 'firstValue(auditInfo, ["auditDoctorName"])', 'auditInfo.auditDoctorName']) {
  if (!content.includes(marker)) throw new Error(`missing prescription marker: ${marker}`);
}
for (const forbidden of ["hospitalInnerAuditDoctorName"]) {
  if (content.includes(forbidden)) throw new Error(`obsolete prescription field remains: ${forbidden}`);
}

const orderContent = await readFile(join(root, "order-content.js"), "utf8");
for (const marker of ["TRADE_FINISHED", "TMALL_ORDER_PAGE_FETCH", "TMALL_ENRICHED_BATCH_SAVE"]) {
  if (!orderContent.includes(marker)) throw new Error(`missing order marker: ${marker}`);
}
for (const forbidden of ["order_required_missing", "order_review_reason", "needs_review: needsReview"]) {
  if (orderContent.includes(forbidden)) throw new Error(`browser-side review remains: ${forbidden}`);
}
for (const marker of ["order_alias_ids", "matched_order_ids", 'validation_stage: "server_after_ingest"', "字段完整性由服务器判断"]) {
  if (!orderContent.includes(marker)) throw new Error(`missing server-review handoff marker: ${marker}`);
}

const worker = await readFile(join(root, "service-worker.js"), "utf8");
for (const marker of ["TMALL_ENRICHED_BATCH_SEND", "Authorization", "not_configured"]) {
  if (!worker.includes(marker)) throw new Error(`missing server delivery marker: ${marker}`);
}
if (worker.includes("TMALL_ENRICHED_BATCH_SEND_CURRENT")) {
  throw new Error("manual batch delivery handler remains");
}

console.log("extension verification passed");
