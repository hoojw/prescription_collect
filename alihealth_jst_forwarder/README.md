# 聚石塔阿里健康处方转发服务

本服务部署在聚石塔 ECS 内，通过奇门自定义场景接受塔外现有处方服务器的调用，固定转发以下两个 TOP API：

- `alibaba.alihealth.asyncprescribe.prescription.search`
- `alibaba.alihealth.asyncprescribe.prescription.detail`

它不做 OCR、药品匹配、金额计算、SQLite 去重、COS 上传或最终 JSON 提交。成功响应中的 `payload` 是未经业务转换的阿里健康 TOP JSON 字符串，业务处理继续由现有处方服务器完成。

## 1. 调用链

```text
现有处方服务器（塔外调用方 AppKey）
  -> 奇门分配的场景网关 /router/qm(test)
  -> 奇门验调用方签名、授权和路由
  -> 聚石塔公网 HTTPS 地址
  -> 本服务验奇门转发签名
  -> 阿里健康 TOP API（聚石塔内调用）
  -> 原始 JSON 放入 payload 返回
```

这不是可调用任意 `method` 的通用代理。奇门 API 名必须与 `config.json` 中的两个名称完全一致，内部 TOP 方法名不可由请求方覆盖。

## 2. 已落实的奇门规则

- 塔外系统需要单独调用方 AppKey，并与塔内目标应用建立奇门映射和授权。
- 自定义场景在奇门云网关创建，场景会获得随机二级网关域名。
- 奇门请求映射后端必须是公网可访问的 HTTP/HTTPS 地址；若域名解析到私网地址会被安全校验拦截。
- 后端最大超时为 30 秒，因此默认 TOP 读取超时为 20 秒，Nginx 读取超时为 27 秒。
- `targetAppkey`/`target_app_key` 必须透传，聚石塔后端使用目标应用 Secret 对奇门请求验签。
- 验签必须动态纳入除 `sign` 外的所有非空请求参数，不能写死参数列表；原始 JSON Body 也参与签名。
- 失败响应包含 `flag`、`sub_code`、`sub_message`，验签失败固定使用 `sign-check-failure` 和 `Illegal request`。
- 路由优先使用简单的场景路由，不要同时堆叠场景路由和 API 路由；多个规则同时命中时可能随机选择。

官方资料：

- 奇门聚石塔内外互通说明：https://developer.alibaba.com/docs/doc.htm?articleId=109580&docType=1
- 奇门自定义 API 场景：https://developer.alibaba.com/docs/doc.htm?articleId=106556&docType=1&treeId=285
- 奇门签名算法：https://developer.alibaba.com/docs/doc.htm?articleId=118394&docType=1&treeId=285
- 奇门路由配置：https://developer.alibaba.com/docs/doc.htm?articleId=107637&docType=1&treeId=285

## 3. 奇门控制台配置

### 3.1 应用和权限

1. 确认塔内目标应用获得“阿里健康异步开方”权限包 `scopeId=16437`。
2. 为当前塔外处方服务器准备单独的调用方 AppKey。
3. 登录奇门云网关，创建企业自定义场景。
4. 将塔内目标应用与场景关联。

### 3.2 定义两个自定义 API

API 名全部使用小写且不少于三个词。示例仅为占位，最终名称必须先在奇门控制台确定，再同步到配置：

```text
replacecompany.alihealth.prescriptions.search
replacecompany.alihealth.prescription.detail
```

查询接口 JSON Body：

```json
{
  "hospital_id": "586522974",
  "start_time": 1786377600000,
  "end_time": 1786463999000,
  "page_no": 1,
  "page_size": 100
}
```

详情接口 JSON Body：

```json
{
  "hospital_id": "586522974",
  "rx_no": "2608030897110097"
}
```

两个接口统一响应字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `flag` | String | `success` 或 `failure` |
| `sub_code` | String | 失败错误码 |
| `sub_message` | String | 失败说明 |
| `payload` | String | 成功时的 TOP 原始 JSON |

在奇门响应配置中，将失败判断条件设置为 `flag == failure`，并映射 `sub_code` 和 `sub_message`。

### 3.3 请求映射、发布和路由

1. 正式/测试后端填写聚石塔 Nginx 或 SLB 的公网 HTTPS 地址，建议只填写域名根地址。
2. 超时设置不超过 30 秒。
3. 开启并透传目标 AppKey，后端必须收到 `target_app_key` 或兼容形式。
4. 完成 API 自测和非法签名验证，再申请发布；官方说明发布审核通常需要 5–7 个工作日。
5. 发布后授权塔外调用方 AppKey。
6. 分别配置测试和正式环境，优先使用场景路由。
7. 塔外调用方使用奇门分配的随机域名：测试 `/router/qmtest`，正式 `/router/qm`。

## 4. 聚石塔服务器安装

以下命令以 CentOS/RHEL 系统为例。在代码仓库根目录执行复制，保持 Python 包目录结构：

```bash
mkdir -p /opt/alihealth-forwarder
cp -a alihealth_jst_forwarder /opt/alihealth-forwarder/
```

```text
/opt/alihealth-forwarder/
├── alihealth_jst_forwarder/
│   ├── deploy/
│   └── requirements.txt
└── .venv/
```

创建专用用户和目录：

```bash
useradd --system --home-dir /var/lib/alihealth-forwarder --shell /sbin/nologin alihealth-forwarder
mkdir -p /opt/alihealth-forwarder /etc/alihealth-forwarder /var/lib/alihealth-forwarder
chown -R root:root /opt/alihealth-forwarder
chown alihealth-forwarder:alihealth-forwarder /var/lib/alihealth-forwarder
```

创建虚拟环境：

```bash
python3 -m venv /opt/alihealth-forwarder/.venv
/opt/alihealth-forwarder/.venv/bin/pip install -r \
  /opt/alihealth-forwarder/alihealth_jst_forwarder/requirements.txt
```

安装配置：

```bash
cp /opt/alihealth-forwarder/alihealth_jst_forwarder/config.example.json \
  /etc/alihealth-forwarder/config.json
cp /opt/alihealth-forwarder/alihealth_jst_forwarder/alihealth-forwarder.env.example \
  /etc/alihealth-forwarder/forwarder.env
chown root:alihealth-forwarder /etc/alihealth-forwarder/config.json \
  /etc/alihealth-forwarder/forwarder.env
chmod 640 /etc/alihealth-forwarder/config.json \
  /etc/alihealth-forwarder/forwarder.env
```

必须替换：

- 两个奇门自定义 API 名；
- 塔外调用方 AppKey 白名单；
- 塔内 TOP AppKey/AppSecret；
- 奇门目标 AppKey/AppSecret。

安装 systemd：

```bash
cp /opt/alihealth-forwarder/alihealth_jst_forwarder/deploy/alihealth-forwarder.service \
  /etc/systemd/system/alihealth-forwarder.service
systemctl daemon-reload
systemctl enable --now alihealth-forwarder.service
systemctl status alihealth-forwarder.service
```

应用进程默认只监听 `127.0.0.1:8088`，不要直接暴露该端口。

## 5. Nginx/SLB

使用 `deploy/nginx-alihealth-forwarder.conf` 作为模板，替换域名和证书后启用。健康检查：

```bash
curl -fsS http://127.0.0.1:8088/healthz
curl -fsS https://alihealth-jst.example.com/healthz
```

预期：

```json
{"status":"ok"}
```

Nginx、WAF、SLB 或其他代理不得改变 JSON Body 字节，否则奇门签名校验会失败。

## 6. 配置说明

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `listen_host` | `127.0.0.1` | 本机监听地址 |
| `listen_port` | `8088` | 本机端口 |
| `api_paths` | `/`, `/qimen` | 接收奇门转发的路径 |
| `qimen_methods` | 无 | 控制台创建的两个 API 名 |
| `allowed_caller_app_keys` | 无 | 塔外调用方 AppKey 白名单，不能为空 |
| `clock_skew_seconds` | `600` | 请求时间允许偏差，与 TOP 十分钟窗口一致 |
| `max_body_bytes` | `1048576` | 最大 JSON Body |
| `max_page_size` | `100` | 查询最大页大小 |
| `max_search_span_milliseconds` | `2678400000` | 单次最大查询跨度，默认 31 天；阿里健康接口使用 13 位毫秒时间戳 |
| `top_gateway_url` | `https://eco.taobao.com/router/rest` | TOP HTTPS 正式网关 |
| `top_sign_method` | `hmac` | 出站 TOP 签名方法，与接口官方示例一致 |
| `top_timeout_seconds` | `20` | 必须小于奇门 30 秒限制 |
| `top_retries` | `0` | 默认不重试，最多允许配置为 1 |

敏感值只写入 `/etc/alihealth-forwarder/forwarder.env`，不要写进 Git、配置 JSON 或日志。

## 7. 日志和排错

```bash
journalctl -u alihealth-forwarder.service -f
```

日志只包含转发请求 ID、API 名、调用方 AppKey、结果、错误码和耗时，不记录患者信息、处方正文或 TOP 原始响应。

常见错误码：

| 错误码 | 含义 |
|---|---|
| `sign-check-failure` | 奇门签名不合法，或代理改变了 Body |
| `forbidden-caller` | 调用方 AppKey 不在本地白名单 |
| `target-appkey-mismatch` | 奇门目标 AppKey 配置错误 |
| `expired-request` | 请求时间超过允许窗口 |
| `unsupported-api` | API 名不在固定白名单 |
| `invalid-request` | 字段、分页、时间范围不合法 |
| `upstream-timeout` | 阿里健康 TOP 调用超时 |
| `upstream-network-error` | 聚石塔到 TOP 网关网络失败 |
| `upstream-invalid-json` | TOP 返回的内容不是 JSON |

TOP 返回的业务错误（例如权限尚未审批）仍会作为有效 `payload` 原样返回，由塔外现有服务器解析和记录。

## 8. 上线前验证顺序

1. 本地单元测试通过。
2. 聚石塔健康检查通过。
3. 奇门测试环境验签自测通过，包括平台注入额外参数的动态验签测试。
4. 奇门测试网关调用 search，确认 payload 是 TOP 原始 JSON。
5. 权限审批后用一个真实处方号调用 detail。
6. 发布并授权塔外 AppKey。
7. 切换正式 `/router/qm`，先执行小时间范围，不提交最终业务接口。

当前阿里健康权限未审批时，只能完成奇门验签、路由和 TOP 权限错误透传验证，不能宣称真实处方接口已经可用。
