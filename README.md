> 当前公开源码基线、配置边界与团队开发流程见 [PUBLICATION.md](PUBLICATION.md)。以下历史运行说明中的版本和部署状态，使用前须核对。

# 多平台处方采集与自动交付

仓库主线状态：`main`，HEAD `42756f4`（基于 `v1.7.0` 继续开发，HEAD 暂无同名 release tag）。生产服务器核验日期：`2026-09-29`；服务器仍部署在 `2b415fc`，与本地仓库不同。请阅读 [SYSTEM_TECHNICAL_DETAILS_CURRENT.md](SYSTEM_TECHNICAL_DETAILS_CURRENT.md) 的交接状态后再发布或排障。

本项目通过已授权、已登录的持久 Chrome profile 采集美团和京东商家处方订单。浏览器进程按店铺在采集前启动、采集后停止，登录态仍保存在各店独立 profile 中。网页或平台接口提供订单、患者、药品和金额等结构化字段；服务器本地 PaddleOCR 补充用法用量、出生日期、科室和开方医院信息；处理完成后可上传图片到腾讯 COS，并按店铺配置提交处方 JSON。天猫探索接入因平台风控已停用，不属于当前代码和部署范围。

系统不自动登录、不处理验证码、不切换账号，也不绕过平台风控。一般由人工运行统一启动器；截至 2026-09-29，和安美团另有一个每半小时触发的定时器。当前定时范围必须以服务器 `systemctl list-timers --all` 为准。

## 当前部署范围

服务器注册表在 2026-09-29 核验时包含 8 个已启用店铺：

| 店铺名称 | 店铺键 | 平台 | 浏览器端口 | 数据目录 |
| --- | --- | --- | ---: | --- |
| 保民 | `mt-32581900` | 美团 | 9362 | `/var/lib/prescription-collector/stores/mt-32581900` |
| 南无 | `mt-32187777` | 美团 | 9364 | `/var/lib/prescription-collector/stores/mt-32187777` |
| 和安 | `mt-34024037` | 美团 | 9350 | `/var/lib/prescription-collector/stores/mt-34024037` |
| 百恩堂 | `mt-34288562` | 美团 | 9361 | `/var/lib/prescription-collector/stores/mt-34288562` |
| 保民 | `jd-136186144` | 京东 | 9462 | `/var/lib/prescription-collector/stores/jd-136186144` |
| 南无 | `jd-19924616` | 京东 | 9464 | `/var/lib/prescription-collector/stores/jd-19924616` |
| 百恩堂 | `jd-67337414` | 京东 | 9463 | `/var/lib/prescription-collector/stores/jd-67337414` |
| 和安 | `jd-228009442` | 京东 | 9465 | `/var/lib/prescription-collector/stores/jd-228009442` |

每个店铺使用独立浏览器配置、登录态、端口、输出目录和 SQLite 状态库。统一调度器按照注册表顺序串行执行，OCR 不并行，避免多个模型推理同时抬高 CPU 和内存占用。

## 处理流程

1. `run_collect.sh` 读取 `/etc/prescription-collector/stores.json`，选择全部店铺或单个店铺。
2. `collect_store_fleet.py` 根据 `platform` 分发到美团或京东采集器。
3. 调度器按需启动对应 Chrome，采集器校验当前店铺身份后获取订单及处方详情，结束时无论成功失败都停止该浏览器。
4. 一个订单出现两张及以上处方时，视为平台异常：跳过 OCR、COS 和接口交付，并去重写入 `/var/lib/prescription-collector/multi_prescription_orders.jsonl`。
5. 正常单处方订单先合并同药；真正多药时按 OCR 药名位置逐药匹配。
6. 本地 OCR 识别用法用量、出生日期、科室和医院抬头。
7. 解析剂量、频次、包装总量并计算 `duration_days`；无法可靠计算的记录进入复核状态。
8. 美团先从当前登录店铺全量刷新商品 SKU 索引，再匹配 `drcode`；京东直接使用详情中的平台 SKU ID。订单字段提供 `total_fee`。
9. 图片按统一 COS 目录上传；需要复核的处方写入当次 `need_review.json`，仅正常处方进入 `prescription_upload.json`。
10. 运营启动器不再询问接口选项；是否提交由各店 `api_submit_enabled` 决定。
11. SQLite 记录增量状态，防止已成功处理的数据重复上传。

## 关键业务规则

- 同一订单多处方：整单跳过业务处理，进入统一 JSONL 待以后专项处理；不阻塞后续订单。
- 单张处方多药：先合并同药，再逐药匹配用法和计算天数。
- 药品标题末尾的到期、效期、过期或临期标签在 SKU 匹配前清洗；原始标题按需保留用于审计。
- 美团用法用量采用蓝色文字掩膜；京东采用灰度预处理；多药药名位置仍使用原始彩色区域。
- 两平台的宽区域 OCR 回退都会复用首次识别结果，按全图坐标合并并去除同一物理行的重复检测。
- 药名含“吸入”，且包装和剂量单位都属于“泡、吸、揿”时，三者仅在该规则内按同一次离散吸入剂量互认。
- 没有明确依据时不使用固定 7 天；业务药品表只在剂量或频次解析失败时按严格 C 级匹配兜底。
- 唯一的定值例外：处方用法含“适量”或“必要时”，且业务索引精确匹配后仍只有“适量/必要时”而没有可计算用法时，固定 `duration_days=30`，状态码为 `120`；药名或规格不匹配时仍进入复核。
- 一张处方中任一药品需要复核，或 `drcode` 缺失/歧义时，整张处方不进入上传 JSON，并单独保存到当次 `need_review.json`。
- `duration_days` 按已确认规则向下取整。

## 主要入口

- `run_collect.sh`：交互式手动启动器；具体提示以服务器部署脚本为准。
- `collect_store_fleet.py`：多店铺串行调度和平台分发。
- `server_meituan_prescription_collect.py`：美团采集器。
- `server_jd_prescription_collect.py`：京东采集器。
- `prescription_usage_ocr.py`：共享 OCR、用法解析和用药天数计算。
- `multi_prescription_queue.py`：单订单多处方统一 JSONL 队列。
- `drug_text_normalization.py`：平台药品标题效期信息清洗。
- `drug_catalog.py`：业务药品用法严格匹配兜底。
- `medicine_sku.py`：平台商品编码匹配。
- `meituan_product_catalog.py`：美团商品列表全量分页、完整性校验、店铺缓存和 SKU 索引生成。
- `cos_image_upload.py`：COS 上传与统一对象路径。
- `prescription_delivery.py`：接口 JSON 生成和提交。

## 文档

- `OPERATIONS_GUIDE.md`：运营人员采集、VNC 登录维护、状态码和复核操作手册。
- `USAGE_GUIDE.md`：日常运行、状态检查、店铺扩展和故障处理。
- `CONFIGURATION_GUIDE_CURRENT.md`：注册表、店铺配置和环境变量说明。
- `SYSTEM_TECHNICAL_DETAILS_CURRENT.md`：当前架构、OCR、计算、交付、状态与安全细节。

历史日期阶段报告和重复说明已在 v1.3.1 删除，以上 5 份文档为唯一维护入口。

## 快速检查与运行

```bash
cd /opt/prescription-collector
./run_collect.sh --version
./run_collect.sh --check-config
./run_collect.sh
```

只查看统一多处方队列：

```bash
sed -n '1,20p' /var/lib/prescription-collector/multi_prescription_orders.jsonl
```

采集 service 为 `oneshot`，执行完成后显示 `inactive` 属于正常状态。定时器是独立 systemd unit；停止采集 service 不等于停用 timer，需分别检查。

## 数据和安全

- 配置目录：`/etc/prescription-collector`。
- 程序目录：`/opt/prescription-collector`。
- 运行数据：`/var/lib/prescription-collector`。
- COS 密钥只保存在 root 管理的环境文件中，不写入 Git 或普通 JSON。
- 运行目录权限为 700，处方数据和统一队列为 600，配置通常为 640。
- Git 已忽略 SQLite、运行目录、图片目录、日志、密钥和本地取回的处方图片。

详细操作见 `USAGE_GUIDE.md`。
