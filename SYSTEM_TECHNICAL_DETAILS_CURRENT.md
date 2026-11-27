# 多平台处方自动采集系统技术交接与技术细节

交接核对日期：`2026-09-29`。本文件同时作为系统技术概览和交接入口；服务器上的店铺、定时器、配置及代码状态会变化，操作前应按本文件中的只读核验命令重新确认。

## 0. 当前交接状态

### 代码与部署版本

- 本地仓库分支：`main`，HEAD：`42756f4`（`Add catalog duration per purchase unit`）。该提交在 `v1.7.0` 之后，当前没有对应的新版本标签。
- 服务器 `/opt/prescription-collector` 的 Git HEAD：`2b415fc`。它落后于本地 `main`；服务器工作区还有未提交和未跟踪文件。因此本地代码、服务器运行代码和 Git 提交不能视为完全一致。
- 本地工作区也有用户未提交或未跟踪的代码、备份和资料。交接时不要用 `git reset`、`git clean` 或批量覆盖方式清理；提交、发布或部署前应逐项盘点差异。
- 仓库中的若干说明仍记录着 2026-07 的状态。若与本节实时核对结果冲突，以本节核验结果和服务器配置为准，并在后续维护时继续同步文档。

### 当前服务器

- 主机：`172.21.0.2`（内网地址）；程序 `/opt/prescription-collector`，配置 `/etc/prescription-collector`，运行数据 `/var/lib/prescription-collector`。
- 采集器由 `prescription-collector-fleet.service` 触发；服务是 oneshot，完成后显示 `inactive` 是正常现象。
- 按店铺启用的浏览器模板：`meituan-store-chrome@.service`、`jingdong-store-chrome@.service`。浏览器有独立 profile、登录态和端口；登录维护使用对应登录服务和 VNC。
- 核验时 `prescription-collector-fleet.service` 未运行。
- 核验时唯一处于 active 的处方定时器是和安美团 `prescription-collector-mt-34024037.timer`，规则为每半小时触发（`OnCalendar=*:0/30`，`Persistent=true`）。它触发的 service 当时为 inactive、上次结果为 success。其他定时器不要由此推断为启用。

定时器状态核验：

```bash
systemctl list-timers --all | grep -Ei 'prescription|collector'
systemctl status prescription-collector-mt-34024037.timer --no-pager
systemctl status prescription-collector-mt-34024037.service --no-pager
```

暂停和恢复和安定时器：

```bash
systemctl stop prescription-collector-mt-34024037.timer
systemctl start prescription-collector-mt-34024037.timer
```

若编辑了 unit 文件，先执行 `systemctl daemon-reload`，再核对 `systemctl cat` 和 `systemctl is-active`。

### 已启用店铺清单

下表来自服务器 `/etc/prescription-collector/stores.json` 及对应店铺配置，核验日期为 2026-09-29。平台 ID、端口、配置和数据目录必须保持一店一份：

| 店铺 | 平台 | 注册键 | 平台店铺 ID | Chrome 端口 | 浏览器服务实例 | COS/API 提交 | 数据目录 |
| --- | --- | --- | --- | ---: | --- | --- | --- |
| 保民 | 美团 | `mt-32581900` | POI `32581900` | 9362 | `meituan-store-chrome@baomin-prescription` | 开/开 | `/var/lib/prescription-collector/stores/mt-32581900` |
| 南无 | 美团 | `mt-32187777` | POI `32187777` | 9364 | `meituan-store-chrome@nanwu-prescription` | 开/开 | `/var/lib/prescription-collector/stores/mt-32187777` |
| 和安 | 美团 | `mt-34024037` | POI `34024037` | 9350 | `meituan-store-chrome@hean-prescription` | 开/开 | `/var/lib/prescription-collector/stores/mt-34024037` |
| 百恩堂 | 美团 | `mt-34288562` | POI `34288562` | 9361 | `meituan-store-chrome@baientang-prescription` | 开/开 | `/var/lib/prescription-collector/stores/mt-34288562` |
| 保民 | 京东 | `jd-136186144` | vender `136186144` | 9462 | `jingdong-store-chrome@baomin-prescription` | 开/开 | `/var/lib/prescription-collector/stores/jd-136186144` |
| 南无 | 京东 | `jd-19924616` | vender `19924616` | 9464 | `jingdong-store-chrome@nanwu-prescription` | 开/关 | `/var/lib/prescription-collector/stores/jd-19924616` |
| 百恩堂 | 京东 | `jd-67337414` | vender `67337414` | 9463 | `jingdong-store-chrome@baientang-prescription` | 开/开 | `/var/lib/prescription-collector/stores/jd-67337414` |
| 和安 | 京东 | `jd-228009442` | vender `228009442` | 9465 | `jingdong-store-chrome@hean-prescription` | 开/开 | `/var/lib/prescription-collector/stores/jd-228009442` |

配置开关顺序为 COS/API。运营运行前可再次读取：

```bash
python3 - <<'PY'
import json
p='/etc/prescription-collector/stores.json'
for s in json.load(open(p, encoding='utf-8'))['stores']:
    if not s.get('enabled'):
        continue
    c=json.load(open(s['config'], encoding='utf-8'))
    print(s['key'], s['name'], s['platform'],
          'cos=', c.get('cos_upload_enabled'),
          'api=', c.get('api_submit_enabled'),
          'config=', s['config'])
PY
```

京东南无当前配置为 COS 开、API 关；因此可生成含图片地址的上传 JSON，但不会自动提交接口。实际执行前仍以当时配置为准。

### 日常入口和运行观察

人工采集入口：

```bash
prescription-collect
# 若全局入口不可用：
/opt/prescription-collector/run_collect.sh
```

采集进程、服务与日志：

```bash
pgrep -af 'collect_store_fleet|server_(meituan|jd)_prescription_collect'
systemctl list-units --type=service | grep -E 'prescription|jingdong|meituan'
journalctl -u prescription-collector-fleet.service -f
```

运行汇总以终端显示的 `SUMMARY_PATH` 和店铺目录下的 `last_run.json` 为准。批次通常位于 `runs/YYYYMMDD/YYYYMMDD_HHMMSS/`。`summary.json` 看总结果，`api_response.json` 看接口实际业务回执，`prescription_upload.json` 是拟提交文件，按日期共享的 `need_review.json` 是待人工处理记录。部分运行可能按配置关闭保存图片或接口提交。

### 关键状态存储

- 每店 SQLite：`/var/lib/prescription-collector/stores/<store_key>/state.sqlite3`。记录处方去重键、图片 COS 状态、接口状态和上传时间。它是生产去重状态库，不要为了重跑而直接删除。
- 每店商品索引：美团 `catalog/medicine_sku_index.json`；药品用法兜底统一使用 `/opt/prescription-collector/drug_catalog_index.json`。
- COS 秘钥：`/etc/prescription-collector/cos.env`。只通过 root 管理的环境文件读取；不可放进 Git、普通配置 JSON、日志或交接文档。
- 统一单订单多处方队列：`/var/lib/prescription-collector/multi_prescription_orders.jsonl`。
- 和安最近一批历史复核修复交付（2026-09-24）保存在 `/var/lib/prescription-collector/stores/mt-34024037/reprocessing/20260924_29_review_repair/`，内含上传文件、接口回执和完成摘要；对应日期复核文件已清空，SQLite 状态为 success。

## 1. 系统目标

系统面向已授权的美团和京东商家后台，完成以下闭环：

```text
按需Chrome进程和持久登录态
  -> 平台订单与处方详情
  -> 网页结构化字段清洗
  -> 本地PaddleOCR字段补充
  -> 药品合并与逐药匹配
  -> 用药天数和drcode
  -> COS图片
  -> 接口JSON
  -> SQLite增量状态
```

系统采用网页字段优先、图片 OCR 补充的路线。OCR 是采集辅助，不替代医生或药师的专业判断。

## 2. 部署结构

```text
/opt/prescription-collector
  collect_store_fleet.py
  server_meituan_prescription_collect.py
  server_jd_prescription_collect.py
  prescription_usage_ocr.py
  multi_prescription_queue.py
  drug_text_normalization.py
  drug_catalog.py
  medicine_sku.py
  meituan_product_catalog.py
  cos_image_upload.py
  prescription_delivery.py
  run_collect.sh
  .venv/

/etc/prescription-collector
  stores.json
  stores/*.json
  cos.env

/var/lib/prescription-collector
  stores/<store_key>/
  multi_prescription_orders.jsonl
```

应用、配置、密钥和运行数据分离。采集器使用 `prescription-collector` 低权限账号，systemd 配合 `UMask=0077` 和限定写目录。

## 3. 多店铺和平台分发

`collect_store_fleet.py` 读取店铺注册表，根据 `platform` 分发：

```text
MEITUAN   -> server_meituan_prescription_collect.py
JINGDONG  -> server_jd_prescription_collect.py
```

交接核对时注册表有 4 家美团店铺和 4 家京东店铺（详见第 0 节服务器清单）。每个店铺独立：

- Chrome profile；
- DevTools 端口；
- 平台登录态；
- 输出目录；
- SQLite 状态库；
- 平台商品 SKU 索引。

店铺串行执行。一个店铺失败不会阻止后续店铺尝试，但批次最终状态会包含失败结果。

## 4. 浏览器会话模型

每个店铺保留独立、持久的 Chrome profile 和登录态，但浏览器进程不常驻。根调度器仅负责校验并启动注册表指定的 systemd 浏览器单元、等待本机 DevTools 端口就绪，以及在该店采集结束后停止浏览器；平台采集器仍以 `prescription-collector` 低权限账号运行。VNC 只用于首次登录或登录失效续期。

浏览器单元名称按平台限定为 `meituan-store-chrome@*.service` 或 `jingdong-store-chrome@*.service`，端口、配置路径和服务名均不得在已启用店铺间重复，避免调度器以 root 操作任意服务或发生串店。

连接后必须校验店铺：

- 美团校验 `expected_poi_id`、浏览器上下文及订单返回 `poiId`；
- 京东校验 `expected_vender_id` 和 `expected_shop_name`。

任何身份不一致都会停止该店铺，防止数据串店。

## 5. 平台采集器

### 美团

美团采集器在已登录页面上下文中调用同源订单和处方详情接口。主要取得：

- 订单号、状态、时间和 `total_fee`；
- 患者网页字段；
- 药品名称、规格、数量、UPC等；
- 处方编号、医生、药师、审核字段；
- 美团处方图片地址；
- 当前 `poiId`。

### 京东

京东采集器将浏览器传输、列表/详情请求和 HTML 解析拆分为独立层。通过已授权浏览器执行同源请求，列表使用 `rxStatusType=2` 且不使用 `diagnosisStatus` 过滤；详情仍保持订单级对象，进入后续流程后再判断单处方、多处方和药品列表。主要取得：

- 京东订单号、`rxId` 和处方编号；
- 商家与店铺身份；
- 患者、医院、医生和药师字段；
- `rxSkuList` 药品及平台 SKU；
- 处方图片地址和订单金额。

京东详情字段优先级固定为：

- 金额：`skuSum` → `orderSum`；
- 处方时间：`storeTime` → `created` → `orderTime`；
- 药品列表：完整 `rxSkuList` → 完整 `originRxSkuList`，不逐项混合两个列表。

两个平台保留独立采集逻辑，共享 OCR、药品解析、COS 和接口交付模块。

## 6. 药品标题清洗

`drug_text_normalization.py` 在平台药品进入 SKU 匹配、同药合并和名称规格拆分前删除标题末尾的效期提示，例如：

```text
坎地沙坦酯片 8mg*7片/板/盒【27年4月到期】
-> 坎地沙坦酯片 8mg*7片/板/盒
```

支持末尾方括号或中文方括号中的“到期、有效期、效期、过期、临期”。不删除标题中部信息。需要审计时保留 `source_drug_name`。

## 7. 单订单多处方队列

同一订单出现两张及以上处方被视为平台异常。美团和京东统一执行：

1. 不下载处方图片。
2. 不执行 OCR。
3. 不上传 COS。
4. 不进入接口 JSON。
5. 不写入各店 `review.jsonl`。
6. 继续处理后续订单。
7. 去重写入统一文件 `/var/lib/prescription-collector/multi_prescription_orders.jsonl`。

唯一键：

```text
<platform>:<store_id>:<order_id>
```

记录字段：

```json
{
  "schema_version": 1,
  "queue_key": "MEITUAN:32187777:<order_id>",
  "status": "pending",
  "reason": "multiple_prescriptions_in_order",
  "platform": "MEITUAN",
  "store_id": "32187777",
  "store_name": "<store_name>",
  "order_id": "<order_id>",
  "prescription_count": 2,
  "prescription_ids": ["<id1>", "<id2>"],
  "first_seen_at": "<iso_datetime>"
}
```

文件采用追加写和 Linux 文件锁；进程重启后仍会读取既有 `queue_key` 去重。权限为 600，不保存患者字段和图片地址。

## 8. 同药合并与单张处方多药

单张处方包含多种药是正常业务，与单订单多处方隔离无关。

处理顺序：

1. 读取平台药品列表。
2. 清洗标题末尾效期信息。
3. 按规范化药名和规格合并同药，忽略价格差异。
4. 合并购买数量。
5. 合并后单药走快速路径。
6. 真正多药按 OCR 药名相似度和纵坐标匹配。
7. 药品数与用法数一致但名称不足时，才允许按纵向顺序兜底。

药品上传字段只保留接口需要的名称、规格、数量、用法、天数和 `drcode` 等信息。

## 9. OCR 模型与线程

两平台共用 `PaddleUsageOCR` 实现和同一套服务器模型缓存。每个店铺进程加载一次模型；当前批次仍是店铺级加载，不是跨六店共享模型进程。

OCR 线程受 `run_collect.sh` 和运行环境变量控制；当前部署约定为 OCR 推理线程 3，BLAS/OMP 等底层线程单独限制。部署和基准测试前，以服务器实际启动脚本及进程环境为准，不要仅依据旧配置默认值。

历史默认线程记录（不一定等于当前部署）：

```text
PRESCRIPTION_OCR_THREADS=2
OMP_NUM_THREADS=2
MKL_NUM_THREADS=2
OPENBLAS_NUM_THREADS=2
```

汇总记录模型加载、首次推理、累计推理、预测调用数和平均每图耗时，供以后评估是否需要批次级共享 OCR。

## 10. 用法用量 OCR

### 美团

- 单药区：图片高度 35%–53%。
- 多药区：35%–70%。
- 候选不足回退：30%–70%。
- 用法候选使用 HSV 蓝色文字掩膜。
- 多药额外读取原始彩色图中的黑色药名，只作为位置锚点。
- 蓝色掩膜已解析出用法、但计算原因为 `dose_and_package_units_mismatch` 时，仅对同一用法区域执行一次原彩图回退。只有原图候选置信度达标且能够成功完成单位换算时才替换主结果；否则继续复核。原始候选、回退候选和最终选择来源均保留在 OCR 追溯字段中。

### 京东

- 使用独立配置的裁剪比例。
- 当前用法预处理为灰度阈值 210。

解析结果包括：

- `usage_text` 原始 OCR 文本；
- `usage_method` 详细用法；
- `administration_route` 规范化给药途径；
- `dose_per_time` 和 `dose_unit`；
- `times_per_day` 或周期频次；
- 图片明确疗程。

OCR 把剂量单位误识别为“掀”时，在剂量语境中修正为“揿”。

首次核心区域无法完成匹配时，系统仍保留完整宽区域回退。两次识别结果先换算到统一的全图坐标，再合并同一物理行并重新执行多药匹配；仅当合并结果覆盖更多药品时才采用，避免回退结果覆盖已经正确的首次结果。运行记录保留候选文字、置信度、坐标和来源轮次，正式上传 JSON 不包含这些诊断字段。

对于 OCR 丢失标点形成的连续文本（例如“口服一日一次一次2.0片”），解析器先锁定频次片段，再从剩余文本提取每次剂量，防止把频次中的“一次”误判成剂量 `1次`。

## 11. 出生日期、科室和医院标题

美团处方当前使用：

- 出生日期和科室：横向 48%–96%，纵向 20%–35%，灰度阈值 225；
- 医院标题：横向 10%–90%，纵向 2%–20%，灰度阈值 140。

出生日期以年龄或出生标签作为上下文，排除开具日期。科室允许合并相邻文字框，最长按已确认的 16 个汉字处理。

医院标题与店铺配置的内部医院名称及别名比较：

- 内部医院：输出标准医院名称，保留内部医生；
- 外部医院代开：`hospital_name` 输出“外部医院”，`doctor` 为空；
- 无法可靠判断：进入医院来源复核，不自动提交对应记录。

## 12. 用药天数计算

优先级：

1. 图片存在明确疗程时使用明确疗程。
2. 否则解析包装总量和购买数量。
3. 解析每次剂量和频次。
4. 统一可换算单位。
5. 计算并向下取整。

公式：

```text
总药量 = 每盒可用量 × 购买盒数
每日药量 = 每次剂量 × 每日次数
duration_days = floor(总药量 ÷ 每日药量)
```

支持：

- mg、g、μg质量换算；
- ml、L容量换算；
- IU和万单位；
- 片、粒、袋、支、瓶等离散单位；
- 规格含量与片粒数量换算；
- 容器与容量换算；
- `N日M次` 和 `N周M次` 周期频次；
- 范围剂量或频次采用已确认的保守端点规则；
- 起始剂量按固定剂量计算全部药量。

吸入制剂特殊规则：药名包含“吸入”，且包装与用量单位都属于“泡、吸、揿”时，三者按同一次离散吸入剂量 1:1 互认。该规则不适用于其他药品或 `ml/支` 等单位。

“必要时”“适量”不再固定写 7 天。通常没有可靠计算依据时进入复核；仅当业务药品索引也通过严格 C 级匹配确认同一药品、同一临床规格，并且索引用法仍为“适量/必要时”时，执行已确认的 30 天规则。

## 13. 业务药品用法兜底

`drug_catalog_enabled=true` 时，仅在 OCR 剂量或频次失败后查询预生成 JSON 索引。严格 C 级匹配要求：

- 规范药名一致；
- 临床规格一致；
- 给药途径按已确认规则一致；
- 同规格候选用法一致。

“外用”不等于“涂抹”。业务表不处理包装解析失败，也不提供固定 7 天。

定性用法的特殊分支：

1. OCR 原文或解析字段必须明确包含“适量”或“必要时”；
2. 业务索引必须通过药名和临床规格的严格 C 级匹配；
3. 命中的同规格候选必须一致地保留“适量/必要时”，不存在可计算的具体剂量与频次；
4. 满足以上条件后输出 `duration_days=30`、`duration_calculation_reason=qualitative_usage_catalog_default_30_days`、`status_code=120`；
5. 索引未命中、规格不匹配、候选冲突或 OCR 未识别出定性关键词时，仍进入复核。

状态码定义版本为 `2`。`100`、`110`、`120` 均属于可进入上传 JSON 的成功处理状态。

## 14. total_fee 和 drcode

`total_fee` 来自平台订单金额，作为 JSON 数值输出。京东优先采用 `skuSum`，缺失时使用 `orderSum`。数据库 `decimal(10,2)` 可把 `29.0` 接收为 `29.00`；JSON 本身不保证显示末尾零。

`drcode` 来自平台商品列表：

- 美团在每次店铺采集前，通过已登录浏览器请求
  `/reuse/health/product/retail/r/searchListPageV2`。请求按 `pageNum/pageSize`
  全量分页，`wmPoiId` 必须等于当前配置店铺；
- 返回的 `wmProductSkus[].id` 是页面显示的 `SKUID`，作为 `drcode`；
  `wmProductSkus[].upcCode`、商品 `name` 和 SKU `spec` 分别用于 UPC、
  标题和名称规格严格匹配；
- 只有每页 `totalCount` 一致且最终商品数等于 `totalCount` 时，才将索引
  原子写入本店 `catalog/medicine_sku_index.json`；
- 刷新失败只回退到 72 小时内、`poi_id` 完全一致的缓存，不会拿另一店
  索引兜底；
- 京东优先使用平台详情中的 SKU ID；
- 匹配失败、索引不可用或冲突进入复核，不用价格推断编码。

## 15. COS

COS 根前缀统一为 `/files`，对象路径：

```text
files/<platform>/<store_id>/<business_date>/<prescription_id>.png
```

接口 JSON 使用 COS 短地址；平台原始长图片地址只在正常处方原始记录中保留用于审计。单订单多处方不上传 COS。

COS 凭据由 root 管理的环境文件加载，不出现在代码、店铺 JSON 和 Git 中。

## 16. 接口交付

`prescription_delivery.py` 生成接口 JSON，并以 multipart 文件提交。平台值分别为 `MEITUAN` 和 `JINGDONG`，应用代码为 `pachong`。

是否提交取决于：

- 店铺 `api_submit_enabled`；
- 是否存在有效 COS 图片；
- 医院来源是否允许自动交付。

运营版启动器不再询问接口选项；技术人员直接运行采集器时仍可使用 `--skip-api-submit`。

提交前按处方整体隔离复核记录：全部药品处理状态必须为 `100` 或 `110`，美团全部药品的 `drcode_status` 必须为 `1` 至 `4`，医院来源也必须明确。任一项需要复核时，整张处方写入当次 `need_review.json`，不进入 `prescription_upload.json`；同批其他正常处方继续交付。

各店是否启用生产接口以服务器店铺配置中的 `api_submit_enabled` 为准。HTTP 成功与业务成功分开记录，不能只看 HTTP 200。

## 17. SQLite 增量状态

每个店铺独立 `state.sqlite3`，记录处方键、订单号、COS状态、接口状态、消息、上传时间和尝试次数。

去重只判断处方键（美团同时支持订单键）是否已经存在于状态库，不再把 `api_status` 或 `cos_status` 作为重复判断条件。两个状态字段仅用于追溯当时是否请求及是否成功；接口或 COS 失败后的定向重试应使用恢复工具，不能依赖重新采集整批。不要让两个店铺共用状态库，也不要在未确认后果时删除生产状态库。

`backfill_state_from_platform.py` 用于历史编号补录。它仅请求平台列表和详情以取得处方编号、订单号并生成稳定键，不下载图片、不执行 OCR、不上传 COS、不提交处方接口；默认只输出预览，明确增加 `--commit` 后才写入店铺 SQLite。补录行的 `api_status`、`cos_status` 都是 `not_requested`。

## 平台必需字段缺失复核

列表和详情接口完成标准化后，采集器在图片下载和 OCR 前校验平台来源的上传必需字段。美团和京东统一使用 `platform_required_missing`，并把具体路径写入 `platform_missing_fields` 与 `platform_missing_sources`。

该规则检查订单号、处方编号、处方时间、图片地址、患者姓名/性别/年龄、诊断名称、药品列表及药品名称/规格/数量、订单金额；京东额外检查平台 SKU。生日、科室、诊断编码、外部医院医生等允许由 OCR、默认值或业务规则处理的字段不触发本规则。

命中后只写入当次 `need_review.json`，不下载图片、不执行 OCR、不上传 COS、不进入 `prescription_upload.json`，也不写入 SQLite 已处理状态，确保平台恢复后可以重新采集。网络错误仍保持 `detail_errors`/`review.jsonl` 的技术错误路径。

## 18. 运行目录和保留

```text
<output_dir>/
  state.sqlite3
  last_run.json
  runs/YYYYMMDD/
    need_review.json
    YYYYMMDD_HHMMSS/
      summary.json
      prescriptions.jsonl
      prescription_upload.json
      api_response.json
      images/
      review.jsonl
```

`need_review.json` 按日期合并，同一处方重复扫描且没有更新时不会重复添加。`retention_runs` 和 `retention_days` 只处理批次目录；图片保留策略可单独删除较早批次图片，其他 JSON 和 SQLite 不随图片清理。不会由此删除状态库、最后摘要、统一多处方队列、OCR模型和 COS 对象。

## 19. 汇总与 schema

美团当前运行汇总 schema 为 11，京东为 6。重要字段包括：

- 列表、详情、重复和新增数量；
- 多处方跳过和新增队列数；
- OCR 图片数及各阶段耗时；
- OCR 无文字、低置信度、用法未找到、用法解析失败、多药匹配失败和天数计算失败的分类计数；
- 用药天数、SKU和医院来源结果；
- 复核隔离数量和 `need_review.json` 相对路径；
- COS成功和失败数；
- 接口状态、业务成功数和错误信息；
- 整店耗时与批次耗时。

统一多处方 JSONL 使用独立 schema 1。

## 20. 验证记录与边界

过去的测试数量和批次回归结果属于对应提交的历史证据，不代表当前服务器逐项重新验证。2026-09-29 交接核对确认：

- 本地 Git `main` HEAD 为 `42756f4`，服务器 `/opt/prescription-collector` HEAD 为 `2b415fc`，两者提交不同；服务器工作区还有本地改动。
- 服务器统一采集服务当时未运行；和安美团的半小时定时器 active，其余定时器状态应逐个查询。
- 服务器注册表含 8 家启用店铺，详见第 0 节。
- 2026-09-24 和安 29 条历史复核修复返回接口 `totalCount=29, successCount=29, failCount=0`；SQLite 对应键为 success，日期复核文件已清空。该记录不等同于当前代码版本的整体验收。

每次交接或上线都应记录 commit、配置快照、店铺选择、接口回执及 SQLite 状态核验结果。不要用旧测试数替代对当前版本的验证。

## 21. 已知边界

- OCR 仍可能出现高置信度错字，置信度不是医学正确性的保证。
- 不同医院模板位置可能有小幅变化；固定区域需要用真实样本持续回归。
- 一个订单多处方当前不自动处理，统一队列只是以后专项处理的入口。
- 一张处方多药依赖药名位置和数量一致性，无法可靠对应时必须复核。
- `total_fee` 是订单金额，不拆分到药品。
- 当前每个店铺会独立加载 OCR 模型；店铺扩展到六个后应继续观察批次总耗时，再决定是否实现批次级共享模型进程。
- 系统只处理用户授权的店铺和内部 COS、接口目的地。
