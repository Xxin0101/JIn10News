# 2026-11-20 金十 / Brent 滚动日报

窗口：2026-11-19 08:00（含）至2026-11-20 08:00（不含），北京时间。

- [日报 Markdown](Jin10_Brent_Rolling_Reaction_20261120_0800.md)
- [日报 HTML](Jin10_Brent_Rolling_Reaction_20261120_0800.html)（下载本目录后打开）
- [审计 Markdown](Jin10_Brent_Rolling_Audit_20261120_0800.md)
- [审计 HTML](Jin10_Brent_Rolling_Audit_20261120_0800.html)

## 运行与验收

本期重新抓取金十78页、1547条原始快讯，回补范围取得279根Brent五分钟K线，统计窗口内269根。抓取成功后复用入库数据重建，工作流AUTOMATED_CHECKS_PASSED，issues为空。技能校验器、独立check、200项回归测试通过；1440像素桌面及390像素手机页面可读，无页面横向溢出或脚本异常。完成11段异动时间线审阅，未将同期消息升级为可靠正式归因。

## 内容与限制

- Yahoo BZ=F窗口末价89.77美元，窗口涨幅1.17%，区间88.51–90.70美元。
- 08:10–08:25存在15分钟有效交易时段K线缺口；5/124条分析事件的价格观察不完整。保留披露，不将缺失值补成0。
- ARIMA预测价90.02美元，相对上一完整交易日收盘90.00美元上涨0.02%；方向准确率47.70%、平衡准确率31.95%，283个OOS样本。信号弱，不作高置信度交易结论。
- ISW官网403、自动翻译429后，使用截止前官方Bluesky四条原文并逐条翻译核对：[官方线程](https://bsky.app/profile/thestudyofwar.bsky.social/post/3n2n2ntbzfh2c)。标题November 18，发布时间北京时间November 19 10:47。未纳入截止后发布内容。

## 本地运行环境记录

六模型目录已更名为 `Brent数据量化回测（Price Layer）`，通过BRENT_PRICE_LAYER_ROOT指定。其虚拟环境Python链接仍指向旧目录；本次通过BRENT_PRICE_LAYER_PYTHON临时包装器调用项目内真实的 `.python/cpython-3.12.14-macos-aarch64-none/bin/python3.12`，并加载既有 `.venv/lib/python3.12/site-packages`。未修改模型、回测产物或源代码。临时包装器位于 `/private/tmp/jin10-price-layer-python`，未来运行应重新核验，不能假定临时文件永久存在。
