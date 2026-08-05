# Brent 日报项目交接

最后整理：2026-08-05（Task 6 文档与回归前状态）

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

生成顺序为：更新/读取金十与 Brent → 计算反应与价格异动复盘 → 读取外部缓存或抓取 ISW、特朗普补充 → 写 Markdown 与 Audit → 写 HTML 与 SVG。正式 `FORMAL` 交付应产生：

- `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800.md`
- `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800.html`
- `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800_brent_24h.svg`
- `outputs/tracker/Jin10_Brent_Rolling_Audit_20260805_0800.md`
- `outputs/tracker/Jin10_Brent_Rolling_Audit_20260805_0800.html`

在交付前，逐项核对：窗口边界、外部内容未进入归因、特朗普 `source_items` 完整、ISW Key Takeaways 三个计数一致、顶部重点为 2–5 条且可映射证据、HTML/Audit 未截断，以及各失败原因清晰但未污染主链路状态。

**20260805 08:00 实时报告尚未在本次交接时运行或验收；不得标记为已交付。** 后续 Task 7 完成实际回补、文件检查和结果验证后，才可更新此状态。

## 历史说明

20260721 08:00 是过去的正式样本和事件合并参考，不能证明当前连续抓取或 20260805 交付已完成。历史 `_PARTIAL`/`_FAILED` 文件可与正式文件共存；尤其 `_PARTIAL` 主报告的 Audit 可能沿用 `_FAILED` 文件名，需读取正文状态和日志，不能只看文件名。

## 回归门槛

```bash
git diff --check
rg -n "T[B]D|T[O]DO|implement [l]ater|fill in [d]etails" jin10_market_report run_jin10_tracker.py tests SPEC.md HANDOFF.md
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -v
```

没有新的占位符、`git diff --check` 成功且完整测试套件为绿，才可以进入任何实时回补步骤。
