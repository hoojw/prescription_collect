# 多平台处方采集配置说明

适用版本：`v1.6.1`。

## 1. 配置层级

系统有三层配置：

1. `/etc/prescription-collector/stores.json`：店铺注册表和默认选择范围。
2. `/etc/prescription-collector/stores/<store>.json`：每个店铺的平台、浏览器、数据目录、OCR、COS和接口设置。
3. `/etc/prescription-collector/cos.env`：COS 凭据，仅 root 可读。

程序目录 `/opt/prescription-collector` 不存放店铺登录态和明文密钥。

## 2. 当前注册表

```json
{
  "schema_version": 1,
  "selection": {
    "mode": "all",
    "store_key": ""
  },
  "stores": [
    {
      "key": "mt-32581900",
      "name": "保民",
      "platform": "MEITUAN",
      "enabled": true,
      "browser_service": "meituan-store-chrome@store-1.service",
      "config": "/etc/prescription-collector/stores/mt-32581900.json"
    },
    {
      "key": "mt-32187777",
      "name": "南无",
      "platform": "MEITUAN",
      "enabled": true,
      "browser_service": "meituan-store-chrome@store-2.service",
      "config": "/etc/prescription-collector/stores/mt-32187777.json"
    },
    {
      "key": "jd-136186144",
      "name": "保民",
      "platform": "JINGDONG",
      "enabled": true,
      "browser_service": "jingdong-store-chrome@jd-136186144.service",
      "config": "/etc/prescription-collector/stores/jd-136186144.json"
    },
    {
      "key": "jd-19924616",
      "name": "南无",
      "platform": "JINGDONG",
      "enabled": true,
      "browser_service": "jingdong-store-chrome@jd-19924616.service",
      "config": "/etc/prescription-collector/stores/jd-19924616.json"
    }
  ]
}
```

`run_collect.sh` 允许在运行时选择全部或单店，不需要为每次手动选择永久修改 `selection`。

## 3. 注册表字段

| 字段 | 说明 |
| --- | --- |
| `key` | 调度器内唯一店铺键，只允许小写字母、数字、下划线和短横线 |
| `name` | 手动启动器显示的简短店铺名，例如“保民”“南无” |
| `platform` | `MEITUAN` 或 `JINGDONG` |
| `enabled` | 是否出现在手动启动器中 |
| `browser_service` | 该店按需启动的浏览器 systemd 单元；平台前缀必须与 `platform` 一致 |
| `config` | 店铺 JSON 的绝对路径 |

调度器按注册表顺序串行执行。每店执行前启动 `browser_service`，端口就绪后以低权限账号运行采集器，并在成功、失败或中断时停止浏览器。一个店铺失败时会继续后续店铺，批次最终退出码反映是否存在失败。

## 4. 通用店铺字段

| 字段 | 说明 |
| --- | --- |
| `chrome_port` | 该店独立 Chrome DevTools 端口 |
| `output_dir` | 独立运行目录和 SQLite 状态库目录 |
| `lookback_days` | 未输入日期时包含当天在内的自然日数 |
| `max_pages` | 最大扫描页数；`0` 不是无限，而是不扫描 |
| `page_size` | 每页数量，按平台已验证值配置 |
| `limit` | 最大新增处方数；`0` 为不限 |
| `request_retries` | 页面、详情和图片请求最大尝试次数 |
| `retention_days` | 按天清理运行目录；`0` 为关闭 |
| `retention_runs` | 每个店铺最多保留的运行目录数量；`0` 为关闭 |
| `ocr_enabled` | 是否执行本地 OCR |
| `ocr_min_confidence` | 自动采用 OCR 字段的最低置信度 |
| `usage_ocr_preprocessing_mode` | 用法区预处理模式 |
| `cos_upload_enabled` | 是否上传处方图片到 COS |
| `cos_folder_prefix` | COS 根前缀，当前统一为 `/files` |
| `api_submit_enabled` | 是否自动提交最终处方 JSON |
| `api_url` | 对应环境的处方导入地址 |
| `platform` | 对外接口平台值 |
| `app_code` | 对外接口应用代码，当前为 `pachong` |
| `drug_catalog_enabled` | 剂量或频次失败时是否启用业务药品用法兜底 |
| `drug_catalog_index` | 业务药品 JSON 索引路径 |
| `compact_processing_json` | 是否仅保留数字状态和必要复核信息 |

每个店铺必须使用独立 `output_dir`。禁止让多个店铺共用 SQLite 状态库。

## 5. 美团专用字段

| 字段 | 当前口径 |
| --- | --- |
| `expected_poi_id` | 必填；浏览器和订单返回的 `poiId` 必须一致 |
| `statuses` | 订单状态白名单；当前为已完成、全部发货、待发货 |
| `page_size` | 当前验证值 40 |
| `usage_ocr_preprocessing_mode` | 必须为 `blue_mask` |
| `hospital_name` | 当前店铺对应的内部医院输出名称 |
| `medicine_sku_enabled` | 是否匹配 `drcode` |
| `medicine_sku_index` | 当前店铺独立 SKU 索引 |
| `medicine_sku_refresh_enabled` | 美团采集前是否从当前登录店铺刷新 SKU 索引 |
| `medicine_sku_refresh_page_size` | 商品列表每页请求数量，当前为 `100` |
| `medicine_sku_refresh_max_pages` | 商品列表安全分页上限 |
| `medicine_sku_cache_ttl_hours` | 刷新失败时允许使用同店旧缓存的最长小时数，当前为 `72` |
| `medicine_sku_refresh_retries` | 商品列表全量刷新重试次数 |

两个美团店铺当前都启用 COS 和生产接口提交。执行时仍应以服务器配置为准。

美团 `medicine_sku_index` 应位于本店数据目录，例如：

```text
/var/lib/prescription-collector/stores/mt-32581900/catalog/medicine_sku_index.json
```

采集器先校验浏览器中的 `poiId`，再请求该店全部商品。只有页数、`totalCount`、商品数量和店铺标识全部通过时才原子替换索引。刷新失败时只允许使用 TTL 内、且 `poi_id` 与当前店铺完全一致的缓存；没有合格缓存时继续采集，但无法取得 `drcode` 的处方进入复核，不提交接口。

## 6. 京东专用字段

| 字段 | 当前口径 |
| --- | --- |
| `expected_vender_id` | 必填；校验京东商家身份 |
| `expected_shop_name` | 必填；校验当前店铺名称 |
| `internal_hospital_name` | 判断处方是否由内部医院开具的源名称 |
| `internal_output_hospital_name` | 内部处方输出的标准医院名称 |
| `rx_status_type` | 固定为 `2`：保留审核通过处方，排除取消和审核未通过 |
| `page_size` | 当前验证值 10 |
| `usage_ocr_preprocessing_mode` | 当前为 `grayscale` |
| `usage_ocr_grayscale_threshold` | 当前为 210 |
| `default_fee_type` | 当前默认“自费” |

京东店铺各自配置接口开关。新录入的南无京东初始设置为 `api_submit_enabled=false`，完成小样本验证前不自动提交最终接口。

## 7. OCR 区域

美团当前参数：

| 区域 | 横向 | 纵向 | 预处理 |
| --- | --- | --- | --- |
| 单药用法 | 全宽 | 35%–53% | HSV 蓝色掩膜 |
| 多药用法 | 全宽 | 35%–70% | 蓝色用法 + 原图药名锚点 |
| 宽区域回退 | 全宽 | 30%–70% | 同当前用法模式 |
| 出生日期和科室 | 48%–96% | 20%–35% | 灰度阈值 225 |
| 医院标题 | 10%–90% | 2%–20% | 灰度阈值 140 |

京东用法区比例和灰度阈值按其店铺配置执行。两平台共用同一个 PaddleOCR 实现，但每个店铺进程只加载一次模型。核心区域无法完成用法匹配时会执行宽区域回退；回退结果与首次结果按全图坐标合并，已识别内容不会被直接丢弃。

## 8. 单订单多处方

该规则不再配置为 `review_only`，而是采集器固定行为：

- 两张及以上处方时整单跳过业务处理；
- 不下载图片、不做 OCR、不传 COS、不进接口 JSON；
- 以 `平台:店铺:订单号` 去重写入 `/var/lib/prescription-collector/multi_prescription_orders.jsonl`；
- 后续订单继续处理。

统一文件不在各店 `retention_runs` 清理范围内。

## 9. COS 目录

所有店铺的 `cos_folder_prefix` 保持 `/files`。程序自动生成：

```text
files/<platform>/<store_id>/<business_date>/<prescription_id>.png
```

示例平台目录使用小写 `meituan` 或 `jingdong`。不要在不同店铺配置中手工拼接店铺子目录，避免重复层级。

COS 凭据来自 `/etc/prescription-collector/cos.env`，不属于店铺 JSON。

## 10. 接口提交

运营版 `run_collect.sh` 不再询问是否提交接口，是否提交由店铺 `api_submit_enabled` 决定。技术人员直接调用采集器时仍可用 `--skip-api-submit` 临时阻止提交。图片必须先有有效 COS 地址，接口 JSON 不使用美团或京东的长签名图片地址。

接口提交前还会执行固定复核隔离：任一药品处理状态不是 `100`（OCR成功）、`110`（业务表成功）或 `120`（定性用法与业务表一致，按 30 天处理），美团 `drcode_status` 不是 `1` 至 `4`，或者医院来源状态为待复核时，整张处方写入当次 `need_review.json`，不进入 `prescription_upload.json`。该规则不是配置开关。

`total_fee` 是订单金额；一个订单对应多张处方时当前整单已被隔离，因此不会发生金额拆分。

## 11. 用药天数相关开关

`drug_catalog_enabled=true` 仅在 OCR 剂量或频次失败时启用业务药品表严格 C 级匹配。它不会：

- 覆盖已成功解析的 OCR 用法；
- 处理包装规格解析失败；
- 提供固定 7 天；
- 在药名、规格或候选用法不一致时强行命中。

存在一个经过业务确认的 30 天例外：处方识别文本含“适量”或“必要时”，严格 C 级精确匹配到的业务索引记录也只有“适量/必要时”而没有可计算的具体用法时，输出 `duration_days=30` 和 `status_code=120`。索引未命中、规格不一致、OCR 未识别出上述关键词时均不启用该规则。

吸入制剂“泡、吸、揿”换算是共享计算规则，不是配置开关。

## 12. 环境变量

| 变量 | 作用 |
| --- | --- |
| `PRESCRIPTION_OCR_THREADS` | OCR CPU 线程上限，默认 2 |
| `OMP_NUM_THREADS` | 由启动器同步设置 |
| `MKL_NUM_THREADS` | 由启动器同步设置 |
| `OPENBLAS_NUM_THREADS` | 由启动器同步设置 |
| `PADDLE_PDX_MODEL_SOURCE` | Paddle 模型来源，服务器通常为 BOS |
| `COS_BASE_URL` | COS 公开基础地址 |
| `COS_ACCESS_KEY` | COS AccessKey，不写入 Git |
| `COS_SECRET_KEY` | COS SecretKey，不写入 Git |
| `COS_REGION` | COS 区域 |
| `COS_BUCKET` | COS Bucket |
| `COS_FOLDER_PREFIX` | 环境层默认前缀 |

## 13. 安全修改流程

1. 复制要修改的配置到安全备份位置。
2. 只修改目标店铺，不批量覆盖其他店铺。
3. 验证 JSON：

```bash
python3 -m json.tool /etc/prescription-collector/stores/mt-32581900.json >/dev/null
```

4. 检查权限：

```bash
stat -c '%U:%G %a %n' /etc/prescription-collector/stores/*.json
```

5. 执行：

```bash
cd /opt/prescription-collector
./run_collect.sh --check-config
```

6. 新店铺或重要规则变更先跑小样本并跳过接口提交。

不要把 COS 密钥、Cookie、患者数据或签名图片地址写入配置模板、Markdown 或 Git。
