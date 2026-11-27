# 处方号导出助手

用于 `https://o.yizhujk.com/#/meituanJdPrescription/index`。扩展监听页面自身已经发出的
`/manage/prescriptionUpload/page` 列表请求，只提取响应中的 `fileName`、处方号和状态。

## 安装

1. 打开 `chrome://extensions/`。
2. 开启“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择本目录 `yizhujk_prescription_number_export_extension`。
5. 返回管理页面并刷新一次。

## 使用

1. 在处方上传管理页设置筛选条件并点击“查询”。
2. 如需多页数据，逐页翻页；每次接口响应都会自动累计并按 `fileName` 去重。
3. 点击浏览器工具栏中的“处方号导出助手”。
4. 可选择全部、已上传、上传失败或未知状态，然后导出 TXT、CSV，或复制处方号。

TXT 每行一个唯一处方号。CSV 保留 `prescription_number`、`file_name` 和 `status` 三列。

## 数据边界

- 不读取 Cookie、密码、请求头或 localStorage。
- 不主动调用列表或详情接口，不改变页面请求。
- 不采集患者、医生、药品等详情字段。
- 捕获结果只保存在 `chrome.storage.session`，关闭浏览器后自动清空。
- 安装或更新扩展后必须刷新管理页面，随后重新执行查询。
