# 2026-08-04—2026-08-05 Brent 日报 Spec

- 文档状态：已定稿
- 日报截止：2026-08-05 08:00（Asia/Shanghai）
- 统计窗口：`2026-08-04 08:00:00 <= t < 2026-08-05 08:00:00`
- 交付结果：见同目录 [HANDOFF.md](HANDOFF.md)

## 1. 目标

在既有金十新闻与 Brent 价格滚动日报基础上，生成一份可快速阅读、可追溯、可审计的 24 小时报告。本窗口新增并固化三项能力：

1. 报告顶部提供 2–5 条有正文证据的“今日重点”。
2. 完整展示窗口内符合主题条件的金十特朗普专栏直接表态。
3. 完整、按原顺序、一对一翻译并展示 ISW Iran Update 的 `Key Takeaways`。

新增内容只能补充背景与后续监测，不得改变原有 Brent 价格异动、正式新闻归因或核心驱动判断。

## 2. 时间与事实边界

- 全部窗口判断使用 `Asia/Shanghai`。
- `--date 2026-08-05` 表示前一日 08:00（含）至当日 08:00（不含），不是 8 月 5 日自然日。
- 金十原始快讯与 Yahoo Finance `BZ=F` 5 分钟 K 线是价格扫描、`selected_news`、核心驱动和正式归因的唯一事实源。
- 价格异动扫描沿用既有规则：30 分钟绝对波动达到 `0.50%` 的强窗口，以及达到 `0.25%` 的补充窗口。
- 只有直接涉及原油/能源供应、运输、关键航道、制裁、库存或产量变化，且时间关系与传导方向成立的金十新闻，才能进入正式归因。
- 时间接近不等于因果。没有合格新闻的价格异动必须明确标记为“新闻解释不足”。

## 3. 外部补充源

### 3.1 金十特朗普专栏

- 唯一来源为金十 Trump topic `418`：`https://xnews.jin10.com/topic/418`。
- 只保留统计窗口内特朗普本人直接引语、转述表态、命令、威胁、谈判立场或政策宣布。
- 主题至少命中一项：伊朗/美伊谈判、霍尔木兹、战争或军事行动、原油/油价/能源供应、制裁/豁免/二级关税。
- 仅讨论特朗普但不含其本人当日相关表态的评论、回顾和人物分析不得纳入。
- 合格表态不设数量上限。重复转述可合并，但每个独立表态必须保留代表 `statement_id` 和全部 `source_items`，供 Audit 追溯。
- 该板块不得进入 `selected_news`、价格异动匹配、核心驱动或正式归因。

### 3.2 ISW Iran Update

- 使用截止时间前发布的最新 ISW Iran Update；优先从官网获取，官网定位失败时可通过 ISW 官方 Bluesky 找到官网原文。
- 只提取原文 `Key Takeaways` 区段，不得混入后续 Toplines、国家章节、军事行动正文或站点通用内容。
- 英文原文与中文翻译必须保持原顺序和一对一条目边界，不得截断、合并或只展示前若干条。
- 必须满足：

  `source_key_takeaways_count == translated_key_takeaways_count == rendered_key_takeaways_count`

- 任一计数不相等时，整个 ISW 板块标记失败，不得把部分翻译呈现为成功内容。
- ISW 只用于后续监测，不得参与 Brent 价格归因。

## 4. 报告内容与顺序

Markdown 与 HTML 必须采用相同的主阅读顺序：

1. 报告标题。
2. `今日重点（Executive Summary）`，共 2–5 条。
3. 统计窗口、数据来源与必要的降级警告。
4. 标的信息和数据覆盖范围。
5. Brent 关键指标、30 分钟 K 线与新闻标记。
6. Brent 价格异动复盘。
7. 价格异动但新闻解释不足。
8. 未充分定价的重大风险。
9. 市场主要驱动与下一时段判断。
10. 特朗普相关言论。
11. ISW Iran Update — `Key Takeaways`。
12. 今日关注什么。
13. Audit 入口。

“今日重点”必须在其余报告内容形成后，从已验证内容中提炼。每一条都要映射到 Brent 表现、正式归因、重大事件、特朗普表态或通过完整性校验的 ISW 研判；外部补充内容必须标明“表态”或“研判”，不能写成市场因果。

## 5. 正式交付物

截止 `20260805 08:00` 的 `FORMAL` 交付必须包含：

- `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800.md`
- `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800.html`
- `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800_brent_24h.svg`
- `outputs/tracker/Jin10_Brent_Rolling_Audit_20260805_0800.md`
- `outputs/tracker/Jin10_Brent_Rolling_Audit_20260805_0800.html`

主报告 Markdown 是完整、可审计的文本产物；HTML 必须展示同一组今日重点、特朗普表态和 ISW 条目，不得使用固定上限、摘要截断或“另有 N 条”隐藏合格内容。Audit 必须保留数据完整性、筛选、事件分组、来源映射和外部板块计数。

## 6. 缓存、重试与失败处理

本窗口的外部缓存为：

- `data/external_updates/isw_iran_update_20260805_0800.json`
- `data/external_updates/trump_statements_20260805_0800.json`

成功缓存始终复用；失败缓存默认也复用。只有显式提供对应参数时才刷新失败缓存：

```bash
python3 run_jin10_tracker.py report --date 2026-08-05 --retry-failed-isw --retry-failed-trump
python3 run_jin10_tracker.py backfill-day --date 2026-08-05 --retry-failed-isw --retry-failed-trump
```

- 金十主快讯或 Brent 数据不足时，按既有状态机生成 `PARTIAL`/`FAILED` 产物，不得补造数据。
- 特朗普专栏抓取失败时保留板块并显示失败原因，不得回退到其他来源。
- ISW 解析、翻译或计数校验失败时显示失败原因和原文入口，不得输出伪完整结果。
- 外部补充源失败不改变金十 + Brent 主链路状态，也不得污染正式归因；失败原因必须同时出现在日志、主报告和 Audit。

## 7. 验收标准

交付必须同时满足：

- 统计窗口严格为北京时间 `2026-08-04 08:00`（含）至 `2026-08-05 08:00`（不含）。
- 五个正式文件均生成，主报告与 Audit 的状态一致。
- 今日重点为 2–5 条，且每条能映射到正文证据。
- 特朗普板块只来自金十 topic `418`，覆盖全部合格独立表态并可追溯至全部来源条目。
- ISW 板块只包含完整 `Key Takeaways`；原文、翻译、Markdown、HTML 和 Audit 计数一致。
- 外部补充内容没有进入 `selected_news`、价格异动匹配或核心驱动。
- 数据缺口、价格观察窗口不完整、无法解释的异动和外部抓取失败均如实披露。
- SVG 可被 XML 解析；HTML 不含 `javascript:` URL，且不残留已废弃的重大事件时间轴标记。
- 完整测试套件通过，且文档、代码中没有新增占位符。

建议回归命令：

```bash
git diff --check
rg -n "T[B]D|T[O]DO|implement [l]ater|fill in [d]etails" jin10_market_report run_jin10_tracker.py tests SPEC.md HANDOFF.md
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -v
```

## 8. 明确不做

- 不抓取或翻译 ISW `Key Takeaways` 之后的全文正文。
- 不使用特朗普专栏或 ISW 内容反向解释已发生的 Brent 异动。
- 不扩大金十主快讯的正式归因口径。
- 不为满足数量目标加入弱相关内容。
- 不以历史 HTML、Markdown 或搜索摘要反向写入事实数据库。
- 不把程序化验证等同于桌面端和窄屏视觉验收。

## 9. 依据

- 日期专属设计：[2026-08-05-brent-daily-isw-trump-summary-design.md](../../superpowers/specs/2026-08-05-brent-daily-isw-trump-summary-design.md)
- 项目运行规范：[根目录 SPEC.md](../../../SPEC.md)
- 正式主报告：[Jin10_Brent_Rolling_Reaction_20260805_0800.md](../../../outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800.md)
- 正式 Audit：[Jin10_Brent_Rolling_Audit_20260805_0800.md](../../../outputs/tracker/Jin10_Brent_Rolling_Audit_20260805_0800.md)
