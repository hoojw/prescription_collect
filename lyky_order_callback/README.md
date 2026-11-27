# 邻医快药原始订单回调接收服务

本服务接收邻医云开放平台“推送原始订单”消息。它是一个独立服务，不与美团、京东处方采集器共用运行目录、SQLite 状态库或密钥。

接口依据：

- [推送原始订单](https://b2c-open-doc.lyky.cn/#/zh-cn/apidoc/order/order-origin)
- [签名规则](https://b2c-open-doc.lyky.cn/#/zh-cn/guide/sign-rule)

## 已按文档落实的协议

- 回调地址由对接方在邻医云开放后台创建应用后配置；本服务建议使用：`https://<回调域名>/callbacks/lyky/orders/original`。
- 请求方式：`POST`。
- 请求编码：UTF-8。
- 请求体：`application/x-www-form-urlencoded`。
- 系统参数：`app_id`、`timestamp`、`signature`、`version`；业务订单 JSON 位于 `body`。
- 成功响应严格为：`{"data":"ok"}`。

验签与邻医云“签名规则”一致：排除 `signature` 和应用级 `body` 后，将所有系统参数与本方 `app_secret` 按参数名排序、按顺序拼接参数值，计算 MD5 十六进制字符串，再对该字符串进行 Base64 编码。`app_secret` 只从受限环境文件读取。

## 接收和落库规则

```text
邻医云 POST 表单
  -> Content-Type、大小、UTF-8、重复参数检查
  -> app_id、签名、10位时间戳检查
  -> body JSON 最小结构检查
  -> SQLite 事务持久化
  -> {"data":"ok"}
```

- 成功响应只在 SQLite 已提交后返回；写库失败返回 500，不会伪造成功。
- 同一 `app_id + body.id + 完整载荷摘要` 重复送达只保留一条，但仍返回成功确认。
- 同一订单记录 ID 的内容发生变化时保留新事件，避免覆盖可能的订单更新。
- 订单含姓名、电话和收货地址。服务日志不记录这些字段，SQLite 运行目录由专用系统账号持有，权限由 systemd `UMask=0077` 限制。
- 文档中列出的业务字段若缺失会作为 `schema_warnings_json` 保存，方便排查；只有订单记录 ID、原始订单号、店铺 ID 或 `body` 本身无效时才拒绝回调。

## 部署前需要准备

1. 可公网访问的 HTTPS 域名和有效证书。
2. 邻医云开放后台分配的真实 `app_id` 与 `app_secret`。
3. 该域名 443 端口放行；应用自身仅监听本机 `127.0.0.1:8092`。
4. 在邻医云开放后台把完整 HTTPS 回调地址配置为：

   ```text
   https://<回调域名>/callbacks/lyky/orders/original
   ```

不要把应用直接暴露在公网端口 8092，也不要将 `app_secret` 放进 Git、JSON 配置、终端截图或普通日志。

## 服务器安装

以下以 CentOS/RHEL 为例；在部署服务器上执行。

```bash
useradd --system --home-dir /var/lib/lyky-order-callback --shell /sbin/nologin lyky-order-callback
mkdir -p /opt/lyky-order-callback /etc/lyky-order-callback /var/lib/lyky-order-callback
cp -a lyky_order_callback /opt/lyky-order-callback/
python3 -m venv /opt/lyky-order-callback/.venv
chown -R root:root /opt/lyky-order-callback
chown lyky-order-callback:lyky-order-callback /var/lib/lyky-order-callback
```

安装配置。`config.json` 不放密钥；密钥仅放 `callback.env`。

```bash
cp /opt/lyky-order-callback/lyky_order_callback/config.example.json \
  /etc/lyky-order-callback/config.json
cp /opt/lyky-order-callback/lyky_order_callback/callback.env.example \
  /etc/lyky-order-callback/callback.env
chown root:lyky-order-callback /etc/lyky-order-callback/config.json /etc/lyky-order-callback/callback.env
chmod 640 /etc/lyky-order-callback/config.json /etc/lyky-order-callback/callback.env
```

替换 `app_id` 与 `LYKY_APP_SECRET` 后安装服务：

```bash
cp /opt/lyky-order-callback/lyky_order_callback/deploy/lyky-order-callback.service \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lyky-order-callback.service
curl -fsS http://127.0.0.1:8092/healthz
```

再基于 `deploy/nginx-lyky-order-callback.conf` 配置 Nginx HTTPS 反向代理、证书和域名；配置通过后执行：

```bash
nginx -t
systemctl reload nginx
```

## 运行检查

```bash
systemctl status lyky-order-callback.service --no-pager
journalctl -u lyky-order-callback.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8092/healthz
```

不要直接打印数据库的 `payload_json`，其中可能含个人信息。只查条数可使用：

```bash
sqlite3 /var/lib/lyky-order-callback/orders.sqlite3 \
  'select count(*) as received_order_events from original_order_events;'
```

## 失败处理

| HTTP状态 | 返回代码 | 含义 |
| ---: | --- | --- |
| 401 | `unknown_app` / `invalid_signature` / `expired_request` | 应用ID、密钥、签名算法或服务器时间不一致。 |
| 400 | `invalid_form` / `invalid_body` / `invalid_timestamp` | 回调格式或业务 JSON 无效。 |
| 413 | `request_too_large` | 消息超过配置上限。 |
| 415 | `unsupported_content_type` | 未使用表单编码。 |
| 500 | `storage_error` | 本地数据库无法写入；平台不会收到成功确认。 |

任何 `{"data":"ok"}` 都表示该事件已经安全持久化；它不表示后续 ERP、财务或处方业务已经处理。

## 测试

在代码目录运行：

```bash
python -m unittest tests.test_lyky_order_callback
```

单元测试使用临时 SQLite 和演示密钥，不会连接邻医云，也不会发送真实订单。
