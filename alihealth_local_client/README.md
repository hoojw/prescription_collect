# 现有服务器奇门调用、处方拉取与商品匹配

本组件运行在现有 `/opt/prescription-collector` 服务器，通过奇门网关调用聚石塔内的阿里健康转发服务。当前职责是：

1. 按日期分页获取处方号并去重；
2. 逐处方获取详情，单条失败不阻塞后续记录；
3. 保存未经业务转换的 TOP 原始响应；
4. 用天猫商品导出生成的单盒 SKU 索引匹配药品；
5. `drcode` 使用单盒 SKU 的商家编码 `sku.skuOuterId`；
6. `total_fee` 按各药品“单盒 SKU 价格 × 总盒数”求和；
7. 未匹配、匹配歧义或盒数异常的记录只写入 `need_review.json`。

当前仍不执行 OCR、SQLite 去重、COS 上传或最终处方 JSON 提交。

## 1. 商品索引规则

每个商品必须恰好找到一个单盒 SKU：

- 优先读取销售属性中的 `套餐类型:N盒装`；
- 同时用 `sku.skuOuterId` 末尾的 `*N` 交叉校验；
- 无 `*N` 后缀视为 1 盒；
- 两种盒数表达冲突，或同一商品不能唯一确定单盒 SKU 时，索引构建直接失败；
- 单盒价格只使用 `sku.skuPrice`，不使用商品层 `price`；
- 索引保存源文件 SHA-256、源行号、商品数、SKU 数和匹配键冲突，便于审计。

生成索引示例：

```bash
/opt/prescription-collector/.venv/bin/python \
  -m alihealth_local_client.product_index \
  --output /var/lib/prescription-collector/stores/tm-STORE/catalog/tmall_product_index.json \
  --expect-products 102 \
  /path/to/export-1.xlsx /path/to/export-2.xlsx
```

配置文件中的 `product_index_path` 必须指向生成后的索引。

## 2. 匹配和复核边界

匹配顺序为：

1. 详情已提供商家编码或平台 SKU 时直接匹配；
2. 药品标题与规格标准化后精确匹配；
3. 去品牌后的药名和规格匹配。

同一个匹配键命中多个商品时返回 `catalog_ambiguous`，不猜测 `drcode`；未命中返回 `catalog_not_found`；盒数不是正整数返回 `box_quantity_invalid`。上述记录均不写入 `catalog_processed.jsonl`。

## 3. 密钥边界

现有服务器只保存：

```text
QIMEN_CALLER_APP_KEY
QIMEN_CALLER_APP_SECRET
```

配置 JSON 保存非敏感的 `target_app_key`，用于将请求路由到聚石塔目标应用。目标应用的 AppSecret 只保留在聚石塔。原 `/opt/prescription-collector/tests/alihealth_test/` 直调 TOP 脚本，应在奇门真实联调成功后再删除。

## 4. 安装配置

```bash
cp /opt/prescription-collector/alihealth_local_client/config.example.json \
  /etc/prescription-collector/stores/tm-STORE.json
cp /opt/prescription-collector/alihealth_local_client/qimen-caller.env.example \
  /etc/prescription-collector/qimen-caller.env
```

需要替换奇门网关、两个自定义 API 名、目标 AppKey、医院 ID、输出目录、商品索引路径和调用方 AppKey/AppSecret。

```bash
chown root:prescription-collector \
  /etc/prescription-collector/stores/tm-STORE.json \
  /etc/prescription-collector/qimen-caller.env
chmod 640 \
  /etc/prescription-collector/stores/tm-STORE.json \
  /etc/prescription-collector/qimen-caller.env
```

商品索引由运行账户读取：

```bash
chown -R prescription-collector:prescription-collector \
  /var/lib/prescription-collector/stores/tm-STORE/catalog
chmod 700 /var/lib/prescription-collector/stores/tm-STORE/catalog
chmod 600 /var/lib/prescription-collector/stores/tm-STORE/catalog/tmall_product_index.json
```

## 5. 手动小批量测试

```bash
set -a
source /etc/prescription-collector/qimen-caller.env
set +a

runuser --preserve-environment -u prescription-collector -- \
  /opt/prescription-collector/.venv/bin/python \
  -m alihealth_local_client.fetch \
  --config /etc/prescription-collector/stores/tm-STORE.json \
  --start 2026-08-10 \
  --end 2026-08-10 \
  --limit 20
```

每次运行输出：

```text
<output_dir>/runs/<run_id>/
├── search_pages.jsonl
├── details.jsonl
├── detail_errors.jsonl
├── catalog_processed.jsonl
├── need_review.json
└── summary.json
```

`details.jsonl`、`catalog_processed.jsonl` 和 `need_review.json` 可能包含患者信息，只允许 `prescription-collector` 运行账户读取，不得复制到普通日志或聊天工具。

## 6. systemd 手动任务

```bash
cp /opt/prescription-collector/alihealth_local_client/deploy/alihealth-qimen-fetch@.service \
  /etc/systemd/system/
systemctl daemon-reload
systemctl start alihealth-qimen-fetch@tm-STORE.service
journalctl -u alihealth-qimen-fetch@tm-STORE.service -f
```

该服务没有定时器，也不会调用最终上传接口。

## 7. 联调顺序

1. 用奇门测试网关拉取 1 页 search；
2. 确认响应可解析出 `service_result.data.rx_nos`；
3. 用其中 1 个处方号拉取 detail；
4. 核对真实详情中的 `drug_list.drug_dto`、药品名、规格和总盒数字段；
5. 执行 `--limit 20`，核对商品匹配、`drcode`、`total_fee` 和复核分流；
6. 完成生产奇门授权后再切换 `/router/qm`；
7. 最后才接入 OCR、处方 JSON 生成、COS 和生产上传。

在真实网关、API 名和调用方密钥未配置前，只能进行本地模拟测试，不能宣称已经取得阿里健康真实数据。
