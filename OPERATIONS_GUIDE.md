# 处方采集运营使用与维护说明书

适用环境：当前服务器四店铺美团、京东处方采集系统。  
适用版本：`v1.6.1` 及服务器 2026-07-31 运营版 `run_collect.sh`。

## 1. 运营人员可以做什么

运营人员负责：

- 选择店铺和日期并启动采集；
- 查看本次执行是否完成、接口是否成功；
- 查看 `need_review.json`，按处方图片核对药名、规格和用法；
- 登录失效时通过 VNC 恢复对应店铺登录态；
- 将不能确认的单位换算、药品匹配或医院来源问题交给技术人员。

运营人员不要：

- 删除 `state.sqlite3`、生产运行目录或已经上传成功的数据；
- 手工把 `need_review.json` 中的记录复制到 `prescription_upload.json` 后上传；
- 修改 `status_code` 或 `duration_days` 来绕过复核；
- 混用不同店铺的 Chrome profile、端口、配置或 SQLite；
- 在截图、群聊或工单中公开患者姓名、电话、密钥、Cookie 或带签名的原始图片地址。

## 2. 店铺对应关系

| 运营名称 | 平台 | 店铺键 | 浏览器登录实例 | 端口 | 配置文件 |
| --- | --- | --- | --- | ---: | --- |
| 保民 | 美团 | `mt-32581900` | `store-1` | 9333 | `/etc/prescription-collector/stores/mt-32581900.json` |
| 南无 | 美团 | `mt-32187777` | `store-2` | 9341 | `/etc/prescription-collector/stores/mt-32187777.json` |
| 保民 | 京东 | `jd-136186144` | `jd-136186144` | 9433 | `/etc/prescription-collector/stores/jd-136186144.json` |
| 南无 | 京东 | `jd-19924616` | `jd-19924616` | 9441 | `/etc/prescription-collector/stores/jd-19924616.json` |

浏览器进程按需启动，未采集时显示 `inactive` 是正常现象。每个店铺的登录态保存在独立 profile 中，停止浏览器不会清除登录态。

## 3. 日常采集操作

在服务器 root 终端执行：

```bash
prescription-collect
```

如果全局命令不可用：

```bash
/opt/prescription-collector/run_collect.sh
```

### 3.1 交互步骤

1. 确认“配置规则预检通过”。
2. 选择“全部已启用店铺”或某一个店铺。
3. 输入开始日期和结束日期，格式为 `MM-DD`，例如 `07-31`。
4. 直接回车表示使用店铺配置中的默认日期范围。
5. 检查屏幕显示的完整日期范围，再确认执行。

当前运营版的规则：

- 年份自动采用服务器当前年份；跨年历史日期不要直接使用，应联系技术人员；
- 最大新增数量固定为 `0`，即不限制新增数量；
- 不再询问是否提交接口，是否提交完全由各店铺配置中的 `api_submit_enabled` 决定；
- COS 是否上传也按各店铺配置执行；
- 店铺串行执行，不会同时运行多个 OCR。

截至 2026-08-03，生产配置为：保民美团、南无美团、保民京东自动提交；南无京东 `api_submit_enabled=false`，只生成待提交 JSON，不自动提交。运营人员每次仍应以服务器配置为准：

```bash
grep -H 'api_submit_enabled' /etc/prescription-collector/stores/*.json
```

### 3.2 运行期间

不要关闭当前终端。重点观察：

- `STORE_START`：开始某店；
- `BROWSER_READY`：该店浏览器已就绪；
- `completed`：该店采集完成摘要；
- `progress ... processed=N/T percent=P`：该店逐处方处理进度；`saved` 为已保存数，`review` 为当前复核数，`errors` 为技术错误累计数；
- `STORE_END ... exit_code=0`：该店正常结束；
- 最后 `failed=0`：本批所有店铺程序执行成功。

需要正常停止时在原终端按 `Ctrl+C`。调度器会尝试停止当前按需浏览器。不要直接执行 `kill -9`；强制杀死可能留下浏览器进程。

## 4. 采集完成后检查

每个店铺最新摘要：

```bash
python3 -m json.tool \
  /var/lib/prescription-collector/stores/mt-32581900/last_run.json
```

把路径中的店铺键替换为目标店铺。重点字段：

| 字段 | 运营含义 |
| --- | --- |
| `status=success` | 本次无待复核记录且流程完成 |
| `status=needs_review` | 流程完成，但有记录被隔离到复核文件；不代表整批失败 |
| `status=failed` | 采集流程发生系统错误，需要看 `error` 和日志 |
| `new_prescriptions` | 本次新增处方数 |
| `duplicates` | SQLite 已处理或本批重复数 |

`duplicates` 只按 SQLite 中是否已经存在处方键或订单键判断，与该行的 `api_status`、`cos_status` 是否成功无关。遇到接口或 COS 失败时不要通过整批重跑来补传，应交技术人员定向恢复。
| `duration_success` | 用药天数计算成功数 |
| `duration_needs_review` | 用药天数需复核数 |
| `need_review_records` | `need_review.json` 中处方数 |
| `upload_json_records` | 本次进入接口 JSON 的处方数 |
| `api_status=success` | 接口业务返回成功 |
| `api_status=ready_not_submitted` | JSON 已生成，但店铺配置未启用自动提交 |
| `api_status=uncertain_response` | HTTP 已返回但业务结果不明确，禁止直接重传整批，先查 `api_response.json` |
| `api_status=failed` | 接口请求或业务返回失败，交技术人员处理 |
| `cos_errors` | 图片上传失败数，非零时需要技术处理 |

本次运行目录可从终端输出的 `SUMMARY_PATH` 找到。常用文件：

```text
runs/<run_id>/summary.json
runs/<run_id>/need_review.json
runs/<run_id>/prescription_upload.json
runs/<run_id>/api_response.json
runs/<run_id>/images/
```

`need_review.json` 即使没有记录也会存在，内容为 `[]` 属于正常。

## 5. need_review 处理流程

1. 打开本次 `need_review.json`，记录 `prescription_id`、`need_review_reasons` 和 `processing`。
2. 在同一运行目录的 `images/` 中找到对应图片。
3. 对照图片核对药品名称、规格、购买盒数、用法、每次剂量和频次。
4. 只记录核对结论，不直接改生产 JSON 或 SQLite。

如果 `need_review_reasons` 为 `platform_required_missing`，说明平台接口返回的上传必需字段为空。查看 `platform_missing_fields` 和 `platform_missing_sources`，这类记录没有图片处理和接口提交，不要手工复制到 `prescription_upload.json`；平台数据恢复后由下一次采集重新获取。
5. 将“处方编号、状态码、图片实际文字、系统识别文字、建议处理”交给技术人员。

多处方平台异常不放入 `need_review.json`，统一记录在：

```text
/var/lib/prescription-collector/multi_prescription_orders.jsonl
```

该类订单不做 OCR、不传 COS、不进接口 JSON，但不影响后续订单。

## 6. 用药时长状态码

`processing[].status_code` 的版本当前为 2。只有 `100`、`110`、`120` 可以进入接口；其余代码会把整张处方放入 `need_review.json`。

### 6.1 成功码

| 代码 | 含义 |
| ---: | --- |
| 100 | OCR 用法解析及天数计算成功 |
| 110 | OCR 失败后，业务药品索引严格匹配并计算成功 |
| 120 | 处方和严格匹配的业务索引都只有“适量/必要时”，按已确认规则固定 30 天 |

### 6.2 OCR、包装和换算复核码

| 代码 | 含义 | 运营核对重点 |
| ---: | --- | --- |
| 201 | 未找到可靠用法 | 图片是否有用法行、裁剪是否覆盖、文字是否太浅 |
| 202 | 每次剂量缺失或无法解析 | 是否写有“一次 N 片/ml/mg”等 |
| 203 | 频次缺失或无法解析 | 是否写有“一日 N 次、每周 N 次”等 |
| 204 | 包装总量无法解析 | 商品规格是否缺数量或写法异常 |
| 205 | 包装单位信息不足，不能换算 | 是否缺浓度、单支容量或每单位含量 |
| 206 | 剂量单位和包装单位无法换算 | 比较图片剂量单位与规格单位，尤其“揿/瓶、滴/ml、丸/袋” |
| 207 | 药品剂型推断单位与 OCR 单位冲突 | 例如药名像片剂，但 OCR 识别成 ml |
| 208 | OCR 用法与业务药品索引用法冲突 | 两边剂量或频次不一致，不能自动选择 |
| 209 | OCR 置信度低于阈值 | 查看图片清晰度、水印和 OCR 错字 |

### 6.3 业务药品索引复核码

| 代码 | 含义 | 维护方向 |
| ---: | --- | --- |
| 301 | 业务索引没有匹配药名 | 检查业务药品索引是否缺药 |
| 302 | 药名命中但临床规格不一致 | 核对规格，不能按相似药名强行匹配 |
| 303 | 命中多个互相冲突的用法 | 清理业务表中的重复或冲突记录 |
| 304 | 业务索引剂量本身无法解析 | 修正索引用量写法 |
| 305 | 业务索引频次本身无法解析 | 修正索引频次写法 |
| 306 | 业务药品索引不可读取或未提供 | 检查索引文件和配置开关 |
| 307 | 业务药品索引结构版本不兼容 | 重新生成正确版本的 JSON 索引 |

## 7. drcode 和医院来源代码

### 7.1 drcode_status

| 代码 | 含义 | 是否可上传 |
| ---: | --- | --- |
| 1 | 直接使用平台 SKU | 是 |
| 2 | UPC 精确匹配 | 是 |
| 3 | 商品完整标题匹配 | 是 |
| 4 | 药名加规格严格匹配 | 是 |
| 11 | 未找到商品编码 | 否 |
| 12 | 匹配到多个编码，存在歧义 | 否 |
| 13 | 商品索引不可用 | 否 |
| 14 | drcode 匹配功能被关闭 | 否 |

美团每次运行会先刷新当前店铺商品索引；京东优先直接使用详情中的平台 SKU。不同店铺的商品索引不能混用。

### 7.2 hospital_source_code

| 代码 | 含义 |
| ---: | --- |
| 1 | 本店内部医院 |
| 2 | 外部医院；输出 `hospital_name=外部医院`，医生字段为空 |
| 3 | 医院抬头无法可靠判断，整张处方进入复核 |
| 0 | 未设置或该流程未使用医院 OCR；不是运营人员可手工改写的成功码 |

## 8. VNC 登录态维护

只有出现登录页、验证码、店铺身份不匹配时才使用 VNC。正常采集不需要提前打开浏览器。

### 8.1 启动可视登录浏览器

一次只维护一个店铺：

```bash
# 保民美团
systemctl start meituan-store-login@store-1.service

# 南无美团
systemctl start meituan-store-login@store-2.service

# 保民京东
systemctl start jingdong-store-login@jd-136186144.service

# 南无京东
systemctl start jingdong-store-login@jd-19924616.service
```

然后在现有 VNC 桌面中完成平台登录、短信或人工验证，并确认页面显示的店铺名称正确。不要在同一 profile 上登录另一家店。

### 8.2 登录完成后停止可视浏览器

使用与启动时相同的单元名：

```bash
systemctl stop meituan-store-login@store-1.service
```

其他店铺替换相应单元名。停止后等待几秒，让 Chrome 完整写入 Cookie 和 profile。

### 8.3 无采集验证四店登录态

```bash
/opt/prescription-collector/.venv/bin/python \
  /opt/prescription-collector/collect_store_fleet.py \
  --registry /etc/prescription-collector/stores.json \
  --run-user prescription-collector \
  --check-login
```

该命令只逐店启动浏览器并验证身份，不采集、不上传 COS、不提交接口。应看到四店 `logged_in=true`、`store_match=true`，最后 `failed=0`；完成后四个按需浏览器会回到 `inactive`。

## 9. 常见故障与处理

| 现象 | 含义 | 运营处理 |
| --- | --- | --- |
| `logged_in=false` | 登录已失效 | 按第 8 节通过 VNC 登录 |
| `store_match=false` | 当前 profile 登录了错误店铺 | 不要采集；在对应 VNC 实例切回正确店铺 |
| `BROWSER_READY` 超时 | Chrome 服务或 DevTools 端口未就绪 | 记录店铺和服务名，交技术人员检查 systemd 日志 |
| `Permission denied` | 目录或锁文件属主错误 | 不要用 root 直接跑平台 Python；交技术修复权限 |
| `No ccache found` | Paddle 提示缺少编译缓存 | 通常不是失败，以最终摘要为准 |
| `ReduceMeanCheckIfOneDNNSupport` | Paddle/oneDNN 运行提示 | 通常不是失败，以最终摘要为准 |
| `status=needs_review` | 有业务复核记录 | 查看 `need_review.json`，正常记录仍可继续上传 |
| `api_status=ready_not_submitted` | 配置关闭自动提交 | 核对该店 `api_submit_enabled`，不要重复运行整批 |
| 接口 HTTP 200 但提示异常 | 可能已经接收，业务返回不明确 | 查看 `api_response.json`，先向接口方核对，禁止直接重传 |
| 终端断开 | 不能据此判断任务已停 | 重新连接后查进程、`last_run.json` 和浏览器服务状态 |

查看浏览器日志示例：

```bash
journalctl -u meituan-store-chrome@store-1.service -n 100 --no-pager
```

## 10. 2026-07-31 need_review 检查结果

周五共找到 5 次运行，其中保民美团运行两次，日期范围分别为 2026-07-30 和 2026-07-29，不是同一日期的重复结果。

| 店铺 | 运行号 | 采集日期 | 新增 | 待复核 | 可上传 | 接口状态 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 保民美团 | `20260731_161647` | 2026-07-30 | 193 | 8 | 185 | success |
| 南无美团 | `20260731_164503` | 2026-07-30 | 5 | 0 | 5 | success |
| 保民京东 | `20260731_164603` | 2026-07-30 | 47 | 0 | 47 | success |
| 南无京东 | `20260731_164834` | 2026-07-30 | 3 | 1 | 2 | ready_not_submitted |
| 保民美团 | `20260731_172906` | 2026-07-29 | 211 | 19 | 192 | ready_not_submitted |

总计 459 条新增处方，28 条进入复核，431 条进入上传 JSON。按本次运行文件计算，复核原因如下：

- `206` 共 27 条：剂量单位与包装单位不能换算。
  - 25 条硫酸沙丁胺醇吸入气雾剂：规格单位为“揿”，OCR 用量识别为“每次 2 瓶”；
  - 1 条氟米龙滴眼液：剂量为“滴”，包装总量为 ml；
  - 1 条摩罗丹：剂量为“丸”，包装解析总量停留在“袋”。
- `205` 共 1 条：替尔泊肽注射液，处方用量为每周一次、一次 2.5mg，规格为 `2.4ml:10mg/支*1支`，系统未取得可靠的每支强度换算。

这些记录均被正确隔离，没有进入对应运行的 `prescription_upload.json`。其中气雾剂“瓶”很可能是 OCR 单位错字，但运营人员仍应逐图确认，不能批量把所有“瓶”直接改为“揿”。

### 10.1 2026-08-03 后续处理

- 已上线仅针对 `dose_and_package_units_mismatch` 的原彩图 OCR 回退；
- 气雾剂原图识别为“掀”，按既有单位字典纠正为“揿”；
- 25 条气雾剂成功恢复用药天数；
- 摩罗丹按 `16丸×18袋=288丸`，结合每日 48 丸计算为 6 天；
- 替尔泊肽按每支 10mg、每次 2.5mg、每周一次计算为 28 天；
- 以上共 27 条已补传正式接口并明确返回“操作成功”；
- 氟米龙滴眼液 1 条仍保留复核：图片为每次 1 滴、业务表为每次 2 滴，且没有获批的 `滴↔ml` 换算规则，禁止强行上传。
