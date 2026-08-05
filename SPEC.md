# Brent 日报运行规范

最后整理：2026-08-05
适用范围：`run_jin10_tracker.py`、`jin10_market_report/` 及其生成的日报与 Audit。

## 1. 日报边界与主链路

- 日报以 `Asia/Shanghai` 的固定 08:00 为截止：`--date YYYY-MM-DD` 覆盖前一日 `08:00`（含）至该日 `08:00`（不含）。例如 `--date 2026-08-05` 的窗口为 `2026-08-04 08:00 <= 时间 < 2026-08-05 08:00`。
- 金十公开快讯与 Yahoo Finance `BZ=F` 5 分钟 K 线是价格扫描、`selected_news`、核心驱动和 Markdown 正式归因的唯一事实源。先扫描 30 分钟价格异动，再在对应时间窗寻找满足直接供给/运输传导要求的金十新闻；外部补充源不得进入此链路。
- `selected_news`、Markdown 归因、核心/次要驱动不因 HTML 展示层的候选、事件合并或外部内容而改变。无合格新闻的价格异动必须明确写为“新闻解释不足”，不能以时间接近替代因果。
- HTML 首页与 Audit 必须完整呈现其应呈现的记录：不得用固定数量上限、摘要截断或“另有 N 条”隐藏合格的首页新闻、特朗普表态、ISW 已通过完整性校验的要点，或 Audit 所需的来源/分组追溯信息。短说明可以缩写，但不得替代完整条目及其可追溯来源。

## 2. 外部补充源

### 2.1 金十数据特朗普专栏

`jin10_market_report.trump_update.fetch_trump_statements` 抓取金十 Trump topic `418`（专题页：`https://xnews.jin10.com/topic/418`），作为报告中的外部补充栏目。

- 只读取上述半开日报窗口内的专题条目；窗口外条目不进入表态集合。
- 相关主题为：伊朗/美伊/德黑兰/核谈判、霍尔木兹/海峡、战争与军事行动、原油与能源、制裁/豁免/二级关税。
- 条目必须同时命中相关主题，并含直接表态标记（例如“特朗普：”“特朗普称/表示/说/宣布/要求/警告/威胁/发文/下令/公开批评/坦言”或“美国总统特朗普”）。主题词本身或评论转述不足以构成合格表态。
- 合格表态没有项目数上限；不得为了版面而截断。相同主题且规范化文本相似度达到分组阈值的滚动/重复条目可合并，但每个表态必须保留最早 `statement_id`、代表正文和全部 `source_items`（原始 ID、时间、原文 URL），以便 Audit 逐条追溯。
- 该栏目绝不参与 Brent 价格归因、`selected_news`、核心驱动、价格异动匹配或 HTML 新闻面板候选；它只在“特朗普相关言论”、顶部重点和独立 HTML 卡片中作为上下文出现。

### 2.2 ISW Iran Update

`jin10_market_report.external_update.fetch_latest_isw_update` 仅用于“接下来关注什么/后续监测”，不参与价格归因、`selected_news`、核心驱动或首页新闻候选。

- 优先抓取 ISW 官网，失败后可使用其官方 Bluesky 线程定位官网原文。
- 只能提取 ISW 原文的 **Key Takeaways**；不得混入正文、其他小节或旧版 `summary_zh`。
- 必须按原文完整顺序逐条一对一翻译。原文条数、翻译条数和正文/HTML 实际渲染条数都必须为正且完全相等；任一不等时，不得渲染未校验翻译，并在主报告与 Audit 明确记录完整性校验失败。

## 3. 顶部重点与审计要求

- 每份主报告最顶部必须有 `今日重点（Executive Summary）`，包含 2–5 条。每条都必须能映射至本报告的 Brent 表现、价格异动/归因、未匹配重大事件、特朗普补充表态或已校验 ISW 监测来源；不得写成无来源的泛化判断。
- Audit 必须分别记录 ISW 原文/翻译/渲染计数及校验结果，并记录特朗普专题总抓取数、窗口内数、相关来源条目数、独立表态数、每个表态的 ID 和全部来源 ID。HTML 与 Audit 不得截断这些合格条目或来源追溯。

## 4. 缓存、重试与失败边界

外部缓存位于数据库同级目录 `data/external_updates/`（默认数据库下为该路径）：

- ISW：`isw_iran_update_YYYYMMDD_0800.json`
- 特朗普专栏：`trump_statements_YYYYMMDD_0800.json`

成功缓存始终复用；失败缓存默认也复用，避免未经授权的反复外网访问。仅下列显式命令可忽略对应失败缓存并刷新，成功缓存仍复用：

```bash
python3 run_jin10_tracker.py report --date 2026-08-05 --retry-failed-isw --retry-failed-trump
python3 run_jin10_tracker.py backfill-day --date 2026-08-05 --retry-failed-isw --retry-failed-trump
```

任一外部补充源抓取失败、缓存读取异常或 ISW 完整性校验失败，都必须在日志、主报告和 Audit 中保留原因；不得阻断金十 + Brent 主链路，也不得把失败内容转成价格归因。主报告仍按金十与 Brent 的数据完整性状态生成；外部失败只使对应补充栏目显示明确失败/校验状态。

## 5. 运行入口与正式文件

| 场景 | 命令 | 说明 |
| --- | --- | --- |
| 初始化 | `python3 run_jin10_tracker.py init-db` | 创建/升级 SQLite 状态库。 |
| 单轮更新 | `python3 run_jin10_tracker.py run-once` | 更新金十、Brent、反应并生成日报（可加 `--no-report`）。 |
| 常驻轮询 | `python3 run_jin10_tracker.py auto --interval-seconds 300` | 前台进程；需要守护运行时由 launchd/服务器托管。 |
| 本地重算 | `python3 run_jin10_tracker.py report --date YYYY-MM-DD` | 只用现有 SQLite 重算，不补新闻或价格。 |
| 历史回补 | `python3 run_jin10_tracker.py backfill-day --date YYYY-MM-DD` | 回补金十和 Brent 后出报。 |

截止 `YYYYMMDD 08:00` 的正式 `FORMAL` 产物为：

- `outputs/tracker/Jin10_Brent_Rolling_Reaction_YYYYMMDD_0800.md`
- `outputs/tracker/Jin10_Brent_Rolling_Reaction_YYYYMMDD_0800.html`
- `outputs/tracker/Jin10_Brent_Rolling_Reaction_YYYYMMDD_0800_brent_24h.svg`
- `outputs/tracker/Jin10_Brent_Rolling_Audit_YYYYMMDD_0800.md`
- `outputs/tracker/Jin10_Brent_Rolling_Audit_YYYYMMDD_0800.html`

`PARTIAL`/`FAILED` 主报告会带相应后缀；历史兼容行为下，`_PARTIAL` 主报告对应 Audit 文件名可能仍为 `_FAILED`，故状态必须同时核对主报告、Audit 状态与日志。

## 6. 验收与历史记录

交付前至少执行：

```bash
git diff --check
rg -n "T[B]D|T[O]DO|implement [l]ater|fill in [d]etails" jin10_market_report run_jin10_tracker.py tests SPEC.md HANDOFF.md
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -v
```

验收还必须人工核对：08:00 半开窗口、外部源与价格归因边界、ISW 一对一计数、特朗普来源分组可追溯、2–5 条顶部重点、无截断的 HTML/Audit，以及失败原因未污染主链路结论。

历史记录：20260721 08:00 曾作为事件合并规则的正式样本；它不是当前运行状态或未来交付的替代证明。20260805 08:00 的实时回补/交付尚未在本规范更新时执行，必须由后续运行与产物验收确认。
