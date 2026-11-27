# 天猫浏览器扩展接收层

该服务接收 `tmall_image_export_extension` 生成的处方与已完成订单汇总批次。它只负责鉴权、协议校验、批次幂等和私有落盘，不执行 OCR，也不根据业务字段生成 `need_review.json`。

## 数据流

```text
Chrome 扩展
  -> POST 汇总批次
  -> Bearer Token 鉴权
  -> batch_id + 内容哈希幂等
  -> /var/lib/prescription-collector/stores/tmall-extension/inbox/<batch_id>.json
  -> systemd path 自动唤醒串行处理器
  -> 原色 PaddleOCR（年龄 + 诊断 + 用法用量；年龄与诊断共用一次推理）
  -> 用药天数计算 + 药品目录兜底 + COS
  -> prescription_upload.json / need_review.json
```

缺失 `total_fee`、`drcode`、商品数量等业务字段不会被接收层拒绝。接收层只要求：

- `platform` 为 `TIANMAO`；
- `batch_id` 合法；
- `validation_stage` 为 `server_after_ingest`；
- `records` 为非空数组；
- 每条记录有 `prescription_id`。

## 服务器部署

以下命令中的服务器、端口和密钥路径必须替换为经过确认的目标环境。

1. 将 `tmall_extension_receiver` 随项目代码部署到 `/opt/prescription-collector`。
2. 复制配置：

```bash
install -m 640 -o root -g prescription-collector \
  /opt/prescription-collector/tmall_extension_receiver/config.example.json \
  /etc/prescription-collector/tmall-extension-receiver.json
```

3. 创建数据目录：

```bash
install -d -m 700 -o prescription-collector -g prescription-collector \
  /var/lib/prescription-collector/stores/tmall-extension
```

4. 生成接收令牌并保存到仅 root 可读的环境文件：

```bash
umask 077
printf 'TMALL_INGEST_TOKEN=%s\n' "$(openssl rand -hex 32)" \
  > /etc/prescription-collector/tmall-extension-receiver.env
chown root:root /etc/prescription-collector/tmall-extension-receiver.env
chmod 600 /etc/prescription-collector/tmall-extension-receiver.env
```

5. 安装并启动服务：

```bash
install -m 644 -o root -g root \
  /opt/prescription-collector/tmall_extension_receiver/deploy/tmall-extension-receiver.service \
  /etc/systemd/system/tmall-extension-receiver.service
systemctl daemon-reload
systemctl enable --now tmall-extension-receiver.service
systemctl status tmall-extension-receiver.service
```

服务默认只监听 `127.0.0.1:8095`。

## SSH 隧道测试

在运行 Chrome 扩展的 Windows 电脑保持以下命令运行：

```powershell
ssh -N -L 8095:127.0.0.1:8095 -p <SSH端口> -i "<私钥路径>" root@<服务器地址>
```

扩展“服务器设置”填写：

```text
http://127.0.0.1:8095/internal/tmall/prescription-batches
```

接收令牌填写服务器环境文件中的 `TMALL_INGEST_TOKEN` 值。真实处方首次发送属于敏感数据传输，应在确认目标服务器、批次范围和接收目录后执行。

## 查看状态

```bash
systemctl status tmall-extension-receiver.service
journalctl -u tmall-extension-receiver.service -n 100 --no-pager
ls -l /var/lib/prescription-collector/stores/tmall-extension/inbox
```

日志只显示服务状态，不输出患者字段、令牌或完整批次内容。

## 自动串行 OCR

处理器和美团、京东共用 `/run/prescription-collector/prescription-automation.lock`。
因此同一时间只会有一个采集/OCR任务使用 PaddleOCR；新天猫批次先进入
`inbox`，当前任务结束后再按文件进入时间依次处理。一次唤醒会加载一次模型，
然后连续清空当时队列中的全部批次。

```bash
install -m 640 -o root -g prescription-collector \
  /opt/prescription-collector/tmall_extension_receiver/processor.example.json \
  /etc/prescription-collector/tmall-extension-processor.json

install -m 644 -o root -g root \
  /opt/prescription-collector/tmall_extension_receiver/deploy/tmall-extension-processor.service \
  /etc/systemd/system/tmall-extension-processor.service
install -m 644 -o root -g root \
  /opt/prescription-collector/tmall_extension_receiver/deploy/tmall-extension-processor.path \
  /etc/systemd/system/tmall-extension-processor.path

systemctl daemon-reload
systemctl enable --now tmall-extension-processor.path
```

每个批次输出在：

```text
/var/lib/prescription-collector/stores/tmall-extension/runs/<batch_id>/
  images/
  prescriptions.jsonl
  prescription_upload.json
  need_review.json
  summary.json
```

当处理器配置 `api_submit_enabled=true` 时，可上传记录在 COS 成功和复核分流完成后，以 `platform=TIANMAO`、`appCode=pachong` 提交正式业务接口。接口响应保存为同一运行目录下的 `api_response.json`，状态写入 `summary.json`；`need_review.json` 不提交。关闭开关时 `api_status=not_submitted`。

服务器在下载图片和启动 OCR 之前再次校验 `prescription_status`，仅 `已使用`（或平台状态码 `3`）进入处理；`有效`、`已过期`和空状态直接跳过，并计入 `summary.json` 的 `skipped_not_used`。
