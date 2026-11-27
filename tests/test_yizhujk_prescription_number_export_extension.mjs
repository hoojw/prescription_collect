import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const utils = require("../yizhujk_prescription_number_export_extension/record-utils.js");

assert.equal(
  utils.prescriptionNumberFromFileName("2084536519174914_0.png"),
  "2084536519174914",
);
assert.equal(
  utils.prescriptionNumberFromFileName("133390539704831.png"),
  "133390539704831",
);
assert.equal(
  utils.prescriptionNumberFromFileName("runs/abc/2084536519174914_12.jpeg?sign=hidden"),
  "2084536519174914",
);

assert.equal(
  utils.isTargetPageApi("https://api.yizhujk.com/manage/prescriptionUpload/page"),
  true,
);
assert.equal(
  utils.isTargetPageApi("https://api.yizhujk.com/manage/prescriptionUpload/detail"),
  false,
);
assert.equal(
  utils.isTargetPageApi("https://evil.example/manage/prescriptionUpload/page"),
  false,
);

const sanitized = utils.sanitizedPagePayload({
  errCode: 0,
  data: {
    total: 3,
    rows: [
      { fileName: "2084536519174914_0.png", superviseStatus: 1, patientName: "不应保留" },
      { fileName: "2084536514317910_1.jpg", superviseStatus: 2, token: "不应保留" },
      { fileName: "", superviseStatus: 1 },
    ],
  },
});

assert.equal(sanitized.total, 3);
assert.deepEqual(sanitized.records, [
  {
    fileName: "2084536519174914_0.png",
    prescriptionNumber: "2084536519174914",
    status: "uploaded",
  },
  {
    fileName: "2084536514317910_1.jpg",
    prescriptionNumber: "2084536514317910",
    status: "failed",
  },
]);
assert.equal(JSON.stringify(sanitized).includes("不应保留"), false);

console.log("yizhujk prescription number export extension tests passed");
