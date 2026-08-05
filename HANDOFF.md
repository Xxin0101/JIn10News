# Brent 日报项目交接

最后整理：2026-08-05（Task 7 验证后的 20260805 FORMAL 交付状态）

## 当前运行合同

- 日报截止日是北京时间 08:00；`--date 2026-08-05` 严格表示 `2026-08-04 08:00 <= 时间 < 2026-08-05 08:00`，不是自然日。
- 金十快讯与 Yahoo Finance `BZ=F` 5 分钟 K 线构成唯一价格归因边界。先扫价格异动，再用合格金十原始新闻解释；HTML 展示模型和任何外部补充都不能修改 `selected_news`、核心驱动或 Markdown 归因。
- 新增模块：`jin10_market_report/trump_update.py`（金十 Trump topic 418 表态补充）和 `jin10_market_report/external_update.py`（ISW Iran Update）。两者都不参加价格归因。
- ISW 仅可输出完整、顺序不变的一对一 Key Takeaways 中文翻译；原文、翻译、实际渲染数不相等时不渲染未校验内容。特朗普模块仅保留命中规定主题的直接表态，并按相似表述可追溯分组；两者都没有展示项目截断上限。
- 主报告最上方 `今日重点（Executive Summary）` 必须是 2–5 条带证据映射的重点。HTML 与 Audit 必须完整保留合格条目和外部来源/分组追溯，不能以“另有 N 条”替代。

详细规则见 [SPEC.md](SPEC.md)。

## 运行与恢复命令

```bash
# 使用本地 SQLite 重算指定 08:00 窗口（不补主数据）
python3 run_jin10_tracker.py report --date 2026-08-05

# 仅在需要刷新失败的外部缓存时使用；成功缓存仍复用
python3 run_jin10_tracker.py report --date 2026-08-05 --retry-failed-isw --retry-failed-trump

# 回补历史窗口的金十和 Brent 后出报
python3 run_jin10_tracker.py backfill-day --date 2026-08-05 --retry-failed-isw --retry-failed-trump

# 持续抓取；这是前台进程，不是系统服务
python3 run_jin10_tracker.py auto --interval-seconds 300
```

缓存保存在 `data/external_updates/`：`isw_iran_update_20260805_0800.json` 与 `trump_statements_20260805_0800.json`。失败缓存默认复用；只能通过 `--retry-failed-isw` 或 `--retry-failed-trump` 选择性刷新对应失败缓存。

ISW、特朗普专题任一抓取失败、缓存异常或 ISW 计数校验失败，都要在日志、日报和 Audit 显示原因，但不能阻断金十 + Brent 主日报，更不能进入价格归因。判断主报告状态时核对金十/Brent 数据完整性、主报告状态和 Audit；不要把外部补充失败误报为主链路失败。

## 报告顺序与 20260805 交付检查

生成顺序为：更新/读取金十与 Brent → 计算反应与价格异动复盘 → 读取外部缓存或抓取 ISW、特朗普补充 → 在 `build_markdown_report` 内渲染 SVG → 写 Audit Markdown/HTML → 写主报告 Markdown → CLI 写主报告 HTML。20260805 已验证的 `FORMAL` 交付产生：

- `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800.md`
- `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800.html`
- `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800_brent_24h.svg`
- `outputs/tracker/Jin10_Brent_Rolling_Audit_20260805_0800.md`
- `outputs/tracker/Jin10_Brent_Rolling_Audit_20260805_0800.html`

在交付前，逐项核对：窗口边界、外部内容未进入归因、特朗普 `source_items` 完整、ISW Key Takeaways 三个计数一致、顶部重点为 2–5 条且可映射证据、HTML/Audit 未截断，以及各失败原因清晰但未污染主链路状态。

**20260805 08:00 实时报告已验证交付，实际状态为 `FORMAL`。** 统计窗口严格采用半开区间：北京时间 `2026-08-04 08:00:00 <= t < 2026-08-05 08:00:00`。当前交付结果如下：

- 正式五件套即上列两个主报告、SVG 和两个 Audit 文件；正文状态与 Audit 均确认 `FORMAL`，不能用同日遗留 `_FAILED` 文件名推断当前状态。
- ISW 严格完整性为原文/翻译/Markdown/HTML/Audit `6/6/6/6/6`，与官网 `Key Takeaways` 列表逐项一致且没有后续 Toplines、国家或军事行动正文泄漏。错误的旧成功缓存已可恢复地保存在 `data/external_updates/isw_iran_update_20260805_0800.json.pre-fix-invalid`。
- 特朗普专题为相关来源/独立表态/Markdown/HTML `3/3/3/3`，所有来源链接仅指向金十。
- 顶部“今日重点”为 Markdown/HTML `5/5`，符合 2–5 条要求。
- 数据库、Audit 和最终日志一致：原始新闻 `1139` 条，分段 `407/501/231`；过滤新闻 `197 = 189` 条非重复分析记录 `+ 8` 条重复记录；Brent 保护抓取区间/正式统计窗口分别为 `284/267` 根 5 分钟 K 线；最新分析新闻的 `+60` 分钟价格保护由数据库时间戳推导为已覆盖。
- 已披露限制：有效交易时段仍有 `2` 处大于 10 分钟的 K 线缺口，逐事件价格不完整 `14/189`。这些缺口在当前 SPEC 下不降低 `FORMAL` 状态，但要求使用者谨慎解释对应时段。
- SVG XML 解析通过；HTML 未发现旧重大事件时间轴标记或 `javascript:` URL；最终完整测试为 `97/97`。
- 本地文件浏览器的桌面/窄屏视觉验收未完成：`file://` 安全策略阻止加载，未尝试绕过。不得将视觉 QA 描述为已通过；此限制不改变当前 SPEC 下由数据、内容和程序化验收确定的 `FORMAL` 状态。

## 历史说明

20260721 08:00 是过去的正式样本和事件合并参考，不能证明当前连续抓取或 20260805 交付已完成。历史 `_PARTIAL`/`_FAILED` 文件可与正式文件共存；尤其 `_PARTIAL` 主报告的 Audit 可能沿用 `_FAILED` 文件名，需读取正文状态和日志，不能只看文件名。

## 回归门槛

```bash
git diff --check
rg -n "T[B]D|T[O]DO|implement [l]ater|fill in [d]etails" jin10_market_report run_jin10_tracker.py tests SPEC.md HANDOFF.md
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -v
```

没有新的占位符、`git diff --check` 成功且完整测试套件为绿，才可以进入任何实时回补步骤。
