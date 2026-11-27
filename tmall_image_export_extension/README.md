# 天猫处方采集扩展

扩展相当于天猫平台的 `run_collect` 操作入口。运营人员只输入开始日期和结束日期，扩展复用当前 Chrome 的人工登录态完成处方和订单数据采集。

扩展不执行 OCR、不计算用药天数、不生成最终上传 JSON，也不读取或导出 Cookie。OCR、业务规则、复核分流和 `prescription_upload.json` 由内部服务器处理。

## 当前流程

1. 在天猫健康处方列表页输入采集日期并点击“开始采集”。
2. 扩展调用 `searchPrescriptionOrder.htm`，通过 `startTime/endTime/pageNum/pageSize` 分页读取处方，并固定传入 `status=3`（已使用）。接口若异常返回其他状态，采集端会再次校验并跳过。
3. 没有订单号的处方直接跳过，不进入复核。
4. 对每张处方调用页面自身的 `mtop.alihealth.rxms.sellerCenter.queryRxInfo`，取得图片地址；审核药师严格读取详情对象中的 `auditInfo.auditDoctorName` 并写入 `audit`，不搜索或回退到其他同名字段。字段不存在时标记缺失，不启用审核药师 OCR。
5. 扩展自动打开或复用“已卖出宝贝”后台标签，通过 `trade/itemlist/asyncSold.htm` 分页读取 `TRADE_FINISHED` 订单。
6. 订单查询日期在处方日期范围前后各扩展一天；订单响应按其 GBK/GB18030 编码显式解码，再按主订单号和子订单号关联金额、商品、数量和商家编码。
7. 扩展只采集、关联并汇总平台原始字段，不因金额、商品、数量或商家编码缺失而判定复核。一个处方关联多个订单号时保留所有已完成订单候选，由服务器统一校验并分流 `need_review`。
8. 汇总批次暂存在浏览器会话存储中，订单汇总完成后自动发送内部服务器。界面不提供手动发送入口，服务器以批次号和内容哈希防止重复入队。

## 安装

1. Chrome 打开 `chrome://extensions`。
2. 开启“开发者模式”。
3. 选择“加载已解压的扩展程序”。
4. 选择 `tmall_image_export_extension` 目录。
5. 更新扩展后刷新天猫处方列表页和“已卖出宝贝”页面。

## 使用

1. 打开 `https://alihealth.taobao.com/b2c_sellercenter/citybuy/prescriptionlist`。
2. 点击扩展图标。
3. 选择开始日期和结束日期。日期口径为处方开具时间，默认昨天。
4. 点击“开始采集”。
5. 需要中止时点击“停止”；停止信号会同时发送给处方和订单阶段。

扩展不要求运营人员填写页数、最大新增数量、是否下载图片或是否提交接口。

## 服务器接收协议

扩展将一次采集形成的汇总批次提交给内部接收接口。每批至少包含：

```text
batch_id
platform=TIANMAO
date_range
prescription_id
order_id
患者、医院、医生和处方状态字段
audit
image_url 或图片二进制
total_fee
商品名称、数量、drcode
```

接收层以 `batch_id + 内容哈希` 实现批次幂等，返回 `accepted` 或 `duplicate`；批次号相同但内容不同返回冲突。后续处理层以 `platform + prescription_id` 实现记录幂等，在接收后检查必填字段、多订单关系和图片处理结果，再生成 `prescription_upload.json` 或 `need_review.json`。

扩展“服务器设置”中填写至少 32 位令牌；内部接收地址默认为 `http://172.21.0.2:8095/internal/tmall/prescription-batches`，不需要 SSH 隧道。HTTP 仅放行该内部服务器和本机回环地址，令牌不写入采集 JSON 或日志。

## 风控边界

- 检测 `_tmd_/punish`、验证码、滑块、安全验证和访问频率提示后立即停止。
- 不自动登录、不处理验证码、不绕过平台风控。
- 风控事件只保存于扩展本地存储；URL 仅保留域名和路径。
- 同一标签页和原因在五分钟内合并为同一次事件。

## 当前状态

- 接收层和串行 OCR 队列处理器已部署到内部处方服务器。
- 服务器自动执行图片获取、OCR、用药时长计算、COS 上传及上传/复核分流。
- 最终业务接口保持停用，生成结果中的 `api_status` 为 `not_submitted`。
- 下一项验证是用一批真实小样本核对平台图片地址、字段映射和最终 JSON。
