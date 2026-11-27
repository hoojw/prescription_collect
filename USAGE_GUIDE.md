# 多平台处方采集与自动交付使用说明

适用版本：`v1.6.1`。

## 1. 目录与账号

```text
/opt/prescription-collector                 程序和虚拟环境
/etc/prescription-collector                 注册表、店铺配置和COS环境文件
/var/lib/prescription-collector             运行数据、状态库和统一多处方队列
```

采集器实际以低权限账号 `prescription-collector` 运行。管理员使用 root 执行启动器；不要直接用 root 运行单个平台 Python 采集器，否则可能生成服务账号无法写入的目录和锁文件。

## 2. 日常运行前检查

检查四个按需浏览器：

```bash
systemctl status meituan-store-chrome@store-1.service --no-pager
systemctl status meituan-store-chrome@store-2.service --no-pager
systemctl status jingdong-store-chrome@jd-136186144.service --no-pager
systemctl status jingdong-store-chrome@jd-19924616.service --no-pager
```

`store-1` 对应保民美团（9333），`store-2` 对应南无美团（9341）。未采集时四个 Chrome 服务应为 `inactive`，且无需 `enable`；统一调度器会逐店启动，端口就绪后采集，并在该店成功或失败后停止。Chrome profile 与登录态不会因进程停止而删除。

检查版本和配置：

```bash
cd /opt/prescription-collector
./run_collect.sh --version
./run_collect.sh --check-config
```

预检会校验已启用店铺的平台和 OCR 模式，不采集、不上传。交互菜单只显示店铺名称、平台和配置文件。

## 3. 手动执行

```bash
cd /opt/prescription-collector
./run_collect.sh
```

启动器依次询问：

1. 采集全部已启用店铺或指定单店。
2. 开始日期和结束日期；留空使用各店 `lookback_days`。
3. 最终确认。

运营版固定 `limit=0`，不限制新增数量；不再询问是否提交接口，完全按每店 `api_submit_enabled` 执行。

选择“全部”时，调度器仍按注册表顺序串行执行，不会并行启动多个 OCR。

日期格式必须是 `MM-DD`，年份自动采用服务器当前年份，例如：

```text
07-21
```

`lookback_days=1` 表示执行当天 00:00:00 至 23:59:59，不是过去 24 小时。

跨年历史日期不要使用运营启动器，应交由技术人员显式传入完整日期。店铺配置关闭接口时，网页采集、OCR、COS 和 `prescription_upload.json` 生成仍会执行，`api_status` 通常为 `ready_not_submitted`。

如果只想检查登录，应使用各平台采集器的 `--check-login`，不要启动完整采集。

## 4. 当前店铺行为

| 店铺名称 | 店铺键 | 平台 | OCR模式 | COS | 最终接口 |
| --- | --- | --- | --- | --- | --- |
| 保民 | `mt-32581900` | 美团 | `blue_mask` | 启用 | 以配置为准 |
| 南无 | `mt-32187777` | 美团 | `blue_mask` | 启用 | 以配置为准 |
| 保民 | `jd-136186144` | 京东 | `grayscale` | 启用 | 以配置为准 |
| 南无 | `jd-19924616` | 京东 | `grayscale` | 启用 | 初始关闭 |

执行时以 `/etc/prescription-collector/stores/*.json` 为准。不要根据文档猜测接口开关。

## 5. 查看运行状态和工作量

交互式运行时，终端会持续显示当前店铺、日期范围、页数、订单数及完成摘要。

另一个终端可查看统一服务状态：

```bash
systemctl show prescription-collector-fleet.service \
  -p ActiveState -p SubState -p Result -p ExecMainStatus
```

查看服务日志：

```bash
journalctl -u prescription-collector-fleet.service -f
```

如果是直接执行 `./run_collect.sh`，主要进度在原终端，systemd 日志可能没有本次内容。

查看各店最后一次摘要：

```bash
python3 -m json.tool \
  /var/lib/prescription-collector/stores/mt-32581900/last_run.json
```

常用汇总字段：

| 字段 | 含义 |
| --- | --- |
| `api_total` / `rx_ids` | 平台列表返回或京东扫描到的总量 |
| `rx_orders` / `details_ok` | 处方订单及详情成功数 |
| `new_prescriptions` | 本次新增正常处方数 |
| `duplicates` | SQLite 已处理或本次重复数 |
| `multi_prescription_orders_skipped` | 本次遇到的单订单多处方数 |
| `multi_prescription_orders_queued` | 本次新写入统一 JSONL 的数量 |
| `ocr_images` | 实际进入 OCR 的图片数 |
| `ocr_model_load_seconds` | OCR 模型加载耗时 |
| `ocr_inference_seconds` | OCR 推理累计耗时 |
| `duration_success` | 用药天数成功数 |
| `duration_needs_review` | 用药天数复核数 |
| `sku_refresh_status` | 美团商品索引刷新状态 |
| `sku_refresh_products` / `sku_refresh_entries` | 美团商品数和生成的 SKU 条目数 |
| `drcode_success` / `drcode_missing` | 平台药品编码成功和缺失数 |
| `need_review_records` | 从接口 JSON 隔离并写入复核文件的处方数 |
| `cos_success` / `cos_errors` | COS 成功和失败数 |
| `upload_json_records` | 最终接口 JSON 记录数 |
| `api_status` | 接口提交结果 |

SQLite 去重只判断处方键或订单键是否已经存在，不读取 `api_status`、`cos_status` 决定是否再次采集。因此接口或 COS 失败不能通过重跑整批自动重试，应交技术人员定向恢复。

### 仅补录平台处方编号

补录历史编号时先运行预览（不写 SQLite）：

```bash
/opt/prescription-collector/.venv/bin/python \
  /opt/prescription-collector/backfill_state_from_platform.py \
  --config /etc/prescription-collector/stores/jd-136186144.json \
  --start 2026-07-22 --end 2026-07-22
```

核对输出数量和 `prescription_ids.jsonl` 后，增加 `--commit` 才写入状态库。该命令不下载图片、不做 OCR、不传 COS、不提交处方接口；运行前仍需启动对应店铺浏览器，结束后关闭浏览器。

当平台接口返回的上传必需字段为空时，记录会进入 `need_review.json`，原因统一为 `platform_required_missing`，具体字段见 `platform_missing_fields`。这类记录不下载图片、不执行 OCR、不上传 COS、不进入 `prescription_upload.json`，也不写入 SQLite 已处理状态；平台恢复后可在下一次采集重新获取。

## 6. 结果文件

每个店铺的数据目录包含：

```text
state.sqlite3
last_run.json
runs/<run_id>/
  summary.json
  prescriptions.jsonl
  need_review.json               每次均生成，只保存需复核处方
  prescription_upload.json       仅存在正常可交付处方时生成
  api_response.json              仅发生提交时存在
  images/                        正常处方本地图片
  review.jsonl                   其他可复核错误存在时生成
```

`need_review.json` 与 `review.jsonl` 用途不同：前者保存已经取得完整处方、但用法天数、`drcode` 或医院来源需要人工复核的记录；后者保留详情请求、图片下载或平台选择等技术异常。复核处方不会进入 `prescription_upload.json`，但不影响同批其他正常处方交付。

单订单多处方不进入店铺 `review.jsonl`，而是进入统一文件：

```text
/var/lib/prescription-collector/multi_prescription_orders.jsonl
```

查看前 20 条：

```bash
sed -n '1,20p' /var/lib/prescription-collector/multi_prescription_orders.jsonl
```

队列以 `平台:店铺:订单号` 去重，状态初始为 `pending`。文件只保存平台、店铺、订单号、处方编号、数量和首次发现时间，不保存患者字段和图片地址。

## 7. 多处方与多药的区别

### 一个订单多张处方

这是当前认定的平台异常：

- 不下载图片；
- 不执行 OCR；
- 不上传 COS；
- 不进入接口 JSON；
- 汇总到统一 JSONL；
- 继续处理后续订单。

### 一张处方多种药

属于正常业务：先合并同名同规格药品，再按 OCR 药名位置匹配每种药的用法，逐药计算 `duration_days`。

## 8. OCR 与用药天数复核

美团用法区使用蓝色掩膜；京东使用配置的灰度阈值。医院标题、出生日期和科室使用独立固定区域，不与用法区混用。

常见需复核原因：

- OCR 没有解析出剂量或频次；
- 包装数量无法解析；
- 包装单位与剂量单位无法换算；
- 多药数量与用法行无法可靠对应；
- 业务药品严格匹配失败；
- OCR 置信度低于阈值。

“适量/必要时”只有在业务索引严格命中同一药名和临床规格，且索引也没有具体可计算用法时才固定为 30 天（`status_code=120`）。药名未命中、规格不一致或 OCR 没识别出这两个关键词时仍写入 `need_review.json`。

“泡、吸、揿”互认只在药名包含“吸入”且包装与剂量单位都在该集合时启用。其他药品不能使用该换算。

OCR CPU 线程默认限制为 3，OMP/MKL/OpenBLAS 各保持 1。临时调整：

```bash
PRESCRIPTION_OCR_THREADS=2 /opt/prescription-collector/run_collect.sh
```

## 9. 暂停和停止

直接运行时，在原终端按 `Ctrl+C`。已完成的单条记录仍保留，未完成店铺下次可继续。

停止 systemd 统一任务：

```bash
systemctl stop prescription-collector-fleet.service
```

调度器收到正常中断后会停止当前按需浏览器；如进程被强制杀死，可手工检查并停止对应浏览器单元。

## 10. 登录失效

登录失效时，通过 VNC 启动对应店铺浏览器并完成登录。不要更换 Chrome profile，也不要用另一个店铺的端口启动。登录完成后可停止浏览器，Cookie 和本地登录态仍会保留。

重新登录后先执行：

```bash
cd /opt/prescription-collector
./run_collect.sh --check-config
```

配置预检只校验配置结构；真正的登录态由采集器开始连接店铺时校验。

## 11. 新增店铺

每个新店铺必须独立配置：

1. Chrome profile 和 systemd 浏览器实例。
2. 唯一 DevTools 端口。
3. 店铺配置 JSON。
4. 独立输出目录和 SQLite 状态库。
5. 平台商品 SKU 索引。
6. `/etc/prescription-collector/stores.json` 注册项及唯一 `browser_service`。

新增后先执行 `./run_collect.sh --check-config`，再使用小样本并跳过接口提交验证。不要复制其他店铺的输出目录或状态库。

美团无需再通过 VNC 手工导出商品表。每次采集在校验店铺登录态后，都会从商品列表接口全量分页刷新本店 JSON 索引；刷新成功才原子替换，失败时仅使用 72 小时内的同店缓存。京东 `drcode` 直接使用处方详情中的平台 SKU ID。不同店铺的商品编码和缓存绝不能共用。

美团商品刷新结果可在 `summary.json` 查看：

- `sku_refresh_status`：`success`、`fallback_cache`、`unavailable` 或 `not_requested`；
- `sku_refresh_products`、`sku_refresh_entries`、`sku_refresh_pages`；
- `sku_refresh_seconds`、`sku_refresh_cache_age_seconds`；
- `sku_refresh_error`：失败类型和简要原因，不记录 Cookie 或令牌。

## 12. 数据保留与清理

`retention_runs` 只清理各店 `runs/<run_id>`，不会自动删除：

- `state.sqlite3`；
- `last_run.json`；
- 统一多处方 JSONL；
- OCR 模型；
- COS 对象。

不要手工删除成功生产上传运行或 SQLite 状态库。清理前应根据 `summary.json` 的 `api_status` 判断，生产成功记录应保留。

## 13. 常见问题

### Permission denied

通常是曾用 root 直接运行单个平台采集器，导致输出目录或锁文件归 root。检查目标店铺目录和 `.collector.lock` 的属主，应为 `prescription-collector`。

### ccache 或 oneDNN 提示

首次 OCR 出现 `No ccache found`、`ReduceMeanCheckIfOneDNNSupport` 等通常是提示，不等于采集失败。判断结果应看最终 `summary.json` 和退出码。

### 接口 HTTP 200 但状态不确定

接口业务响应可能与 HTTP 状态不同。检查 `api_response.json`、`api_status`、成功数、失败数和任务 ID；不要仅凭终端报错重复上传整批。

### COS 成功但接口未提交

检查店铺 `api_submit_enabled` 是否为 `false`。运营启动器不再临时询问接口选项；`ready_not_submitted` 表示 JSON 已准备但未提交。

### `duration_days` 失败

在 `prescriptions.jsonl` 中查找对应药品的处理状态、OCR 原文、剂量、频次、包装单位和复核原因。不要直接写固定天数绕过计算规则。

## 14. 密钥安全

COS 密钥位于 root 管理的环境文件，权限应为 600。不要把密钥写入店铺 JSON、Markdown、Git、终端截图或聊天记录。修改后无需重新生成店铺配置，但下一次采集进程必须能读取环境文件。
