# Brent 每日 08:00 日报自动化设计

## 目标

更新并启用现有的“每日08:00金十Brent滚动日报”计划任务。任务从下一个北京时间 08:00 开始，每天在本地项目中生成金十新闻事件与 Brent 市场反应滚动日报，并通过 ChatGPT Scheduled 和桌面通知发送结果摘要。

## 调度与统计窗口

- 运行时间：每天北京时间 08:00，包括周末和节假日。
- 统计窗口：前一日 08:00:00（含）至当日 08:00:00（不含），时区为 `Asia/Shanghai`。
- 任务范围：更新现有暂停任务，不创建重复任务。
- 执行位置：`/Users/xiaoxin/Documents/抓取数据` 本地项目。

## 执行流程

计划任务运行现有单次更新入口：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 run_jin10_tracker.py run-once --db-path data/jin10_tracker.sqlite --output-dir outputs/tracker --log-dir logs --timezone Asia/Shanghai
```

任务沿用项目中已经验证的新闻抓取、Brent 行情更新、SQLite 状态更新、异动识别、归因筛选、历史缺口补算以及日报和 Audit 生成逻辑。

## 输出与模板约束

- 正式 HTML 沿用当前最新渲染模板：K 线图位于蓝色详情框上方，空状态提示为“点击上方K线图中的蓝色新闻时间点，查看该时段的相关新闻与市场解读。”
- 其他 HTML 内容和交互保持不变。
- 正式 Markdown 与独立 Audit 的内容和生成逻辑保持不变。
- 输出继续使用 `outputs/tracker/Jin10_Brent_Rolling_Reaction_YYYYMMDD_0800.*` 和 `outputs/tracker/Jin10_Brent_Rolling_Audit_YYYYMMDD_0800.*` 命名。

## 周末、失败与安全边界

- 周末或节假日休市、行情不足时，生成并明确标注休市或缓存参考版，不将旧数据描述成当天完整行情。
- 数据源失败时沿用现有重试和降级机制；若重试后仍失败，报告失败原因、受影响窗口和人工处理建议。
- 自动化不得修改项目源代码、筛选规则、报告结构或文件命名。
- 任务在无人值守模式下运行；本机需保持开机、ChatGPT 桌面端需运行，项目目录与所需网络访问需可用。

## 送达方式

- 每次运行结果进入 ChatGPT 的 Scheduled 收件箱。
- 每次完成后发送桌面通知。
- 通知正文只包含简短摘要、当天正式 HTML 链接、正式 Markdown 路径、Audit Markdown/HTML 路径和日志路径。
- 通知不得展开完整日报或完整 Audit。

## 验收标准

- 现有任务处于启用状态，下一次运行时间对应北京时间 08:00。
- 没有创建第二条重复的 Brent 日报任务。
- 任务提示词包含统计窗口、最新 HTML 模板、降级规则、禁止修改源代码和摘要送达要求。
- 自动化配置可被重新查看，且项目、执行环境和通知策略与本设计一致。
