# 金十中东能源新闻与 Brent 反应日报

从金十数据公开快讯里筛选伊朗、中东局势、能源供应、航运和贸易相关内容，对照 Brent 原油价格，生成 Word 日报或 HTML/Markdown 滚动日报。

本工具不是金十数据或 Yahoo Finance 的官方产品。使用前请阅读[免责声明](#免责声明)。

## 功能

- 抓取金十公开实时快讯，按关键词和相关性筛选，合并高度相似消息，并生成 Word 日报。
- 把筛选后的新闻写入 SQLite，缓存 Yahoo Finance `BZ=F` 的 Brent 5 分钟 K 线，计算发布后 5/15/30/60 分钟以及前 30 分钟到后 30 分钟的价格变化。
- 按北京时间 08:00 滚动窗口输出 HTML/Markdown 正式日报和审计报告。正式复盘先看价格异动，再匹配供给端相关新闻。
- 跟踪任务只做规则计算，不调用大模型。方向预测在未安装 LightGBM 时使用内置逻辑回归。
- 日报末尾可以追加“明日 Brent 价格预测”。该板块读取仓库之外的六模型结果，项目位置用 `BRENT_PRICE_LAYER_ROOT` 指定；不可用时其余日报仍会生成。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

`pip install -r requirements.txt` 安装同一组运行依赖。`python-docx` 供 `run_jin10_report.py` 生成 Word；`init-db` 和 `run-once` 本身只使用标准库。

初始化数据库，并手动跑一轮。`run-once` 会请求金十和 Yahoo Finance 的公开接口，需要网络：

```bash
python3 run_jin10_tracker.py init-db
python3 run_jin10_tracker.py run-once
```

默认数据库是 `data/jin10_tracker.sqlite`。日报写到 `outputs/tracker/`，日志写到 `logs/`。这三个目录已在 `.gitignore` 中。

只生成 Word 日报：

```bash
python3 run_jin10_report.py
```

离线测试：

```bash
python3 -m unittest discover -s tests
```

当前测试使用模拟响应，不访问外网，也不依赖本机私有路径。持续集成跑的是这套完整测试。

需要 LightGBM 预测而不是内置逻辑回归回退时：

```bash
pip install -e ".[forecast]"
```

## 免责声明

- 本项目为非官方工具，与金十数据、Yahoo Finance 没有隶属关系。
- 程序请求的是这些站点的公开接口。接口、访问策略和频率限制都可能变化；请遵守对方的服务条款，并保留请求间隔。
- 输出只用于研究与信息整理，不构成投资建议。
- 新闻发布时间附近的价格变化只描述同期市场表现，不能据此认定因果关系。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `BRENT_PRICE_LAYER_ROOT` | 外部六模型 Brent 回测项目的根目录。公开使用时请设置它。目录或模型产物不可用时，Price Layer 会说明原因，其余日报照常生成。 |
| `BRENT_PRICE_LAYER_HOLIDAYS` | 逗号分隔的额外全日休市日期，格式 `YYYY-MM-DD`。 |

Price Layer 在该外部项目自己的虚拟环境里运行选中模型。`pandas` 以及 ARIMA、LSTM、XGBoost 等依赖属于那个项目，不属于本仓库。

参与修改请看 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请看 [SECURITY.md](SECURITY.md)。

## 原 Word 日报运行方式

```bash
python3 run_jin10_report.py
```

默认输出：

- Word 日报：`outputs/Jin10_Iran_MiddleEast_Market_News_YYYYMMDD.docx`
- 运行日志：`logs/jin10_report_YYYYMMDD_HHMMSS.log`
- 结构化摘要：`logs/jin10_report_summary_YYYYMMDD_HHMMSS.json`

可选参数：

```bash
python3 run_jin10_report.py --hours 24 --max-pages 120 --min-score 4 --pause 0.5
python3 run_jin10_report.py --dry-run
```

## 数据源与抓取方式

- 仅使用金十数据网站公开实时资讯接口：`https://flash-api.jin10.com/get_flash_list`
- 工具按官网当前前端请求头和默认市场快讯通道请求数据。
- 分页使用 `max_time` 向前回溯，直到覆盖过去 24 小时或达到最大页数。
- 如果接口返回异常、结构变化、内容为空或疑似访问限制，日志会明确记录失败原因。

## 处理规则

- 先抓取过去 24 小时内公开可获取实时消息。
- 再按关键词组和相关性得分筛选。
- 去除普通财经噪音，保留与中东局势、能源、贸易、航运或市场风险相关内容。
- 对完全重复或高度相似消息进行合并，并在日志中记录合并数量。
- 对重大突发事件标记 `High Priority`。
- 对无法判断的市场影响写入“待人工判断”，不强行推断。

## 后续扩展

代码按模块拆分：

- `jin10_market_report/fetcher.py`：抓取和分页。
- `jin10_market_report/processor.py`：筛选、分类、去重、影响分析。
- `jin10_market_report/report.py`：Word 日报生成。
- `jin10_market_report/logging_utils.py`：运行日志和结构化摘要。

后续可在此基础上增加自动定时任务、历史日报归档、每日新增重大事件对比或人工复核清单。

## Brent 日内跟踪模式

### 功能概览

- 新闻来源仍仅使用金十数据公开实时资讯接口。
- 每 5 分钟任务只执行规则化抓取、入库、去重、Brent K 线更新、公式计算和报告更新，不调用任何 AI 或大模型接口。
- Brent 第一版默认使用 Yahoo Finance `BZ=F` 的 5 分钟 K 线。价格异动归因的新闻源仍仅限金十；ISW Iran Update 只作为日报末尾“接下来关注什么”的补充，不参与前文归因。
- 金十新闻按 `Asia/Shanghai` 解析；Brent K 线入库为 UTC，但匹配时按北京时间计算目标点后再转换为 UTC 查询，避免 8 小时时区错位。
- HTML/Markdown 日报固定使用 08:00 滚动窗口：前一日 08:00 <= 新闻发布时间 < 当日 08:00，不再按自然日 00:00 至 23:59 截取。
- Brent 回补下载窗口至少覆盖前一日 07:30 至当日 09:05，以保护最早新闻的前30分钟价格和最晚新闻的后60分钟观察窗口。
- 用于比较和匹配的 datetime 必须带时区；程序会拒绝 naive datetime。
- 新闻先进入 `Pending Brent Reaction Analysis`；发布满 60 分钟且价格窗口完整后，统一计算事件后 5/15/30/60 分钟和前30至后30累计变化。价格数据缺失时明确标注“价格数据不足”，不填 0、不静默跳过。
- 正式日报严格先扫描 Brent 30 分钟价格异动，再反查对应时段内的金十新闻；30 分钟绝对波动达到 0.50% 才进入主复盘，全天没有强窗口时才使用达到 0.25% 的补充窗口。
- 未达到阈值的时段不得因新闻重要而进入价格异动复盘；达到阈值但缺少合格新闻时，单列为“价格异动但新闻解释不足”。
- 归因新闻须通过供给端硬门槛，并直接涉及原油、能源供应、运输、港口、油轮、炼厂、制裁、OPEC、库存、产量、霍尔木兹或地缘冲突影响能源运输；股票、汇率、宏观情绪、机构研报及油价自身涨跌快讯不作为驱动新闻。
- 正式日报重点复盘由本窗口实际出现的独立 Brent 价格异动数量决定，不固定输出 3 至 5 条新闻，也不为凑数量加入低相关性事件。
- 集合类新闻会先解析具体油市条目；未提取到原油、LNG、航运、供应、出口、制裁、产量、OSP 或地缘升级事实的汇总不会进入重点事件。
- 同期多事件只在发布时间相差不超过 5 分钟、且主题高度相似或属于同一事件进展时合并。
- 价格反应解读使用谨慎措辞，不把同期价格波动绝对归因于单条新闻。
- 运行日志会输出抓取总数、价格匹配数量、价格缺失数量、重大主题命中数量、价格阈值命中数量、最终输出重点事件数量，以及未进入重点事件的具体原因。

### 初始化数据库

```bash
python3 run_jin10_tracker.py init-db
```

默认数据库：

```text
data/jin10_tracker.sqlite
```

### 手动运行一次

```bash
python3 run_jin10_tracker.py run-once
```

常用参数：

```bash
python3 run_jin10_tracker.py run-once --lookback-hours 6 --max-pages 20 --pause 0.2
python3 run_jin10_tracker.py run-once --db-path data/jin10_tracker.sqlite --output-dir outputs/tracker
python3 run_jin10_tracker.py run-once --no-report
```

默认输出：

- Markdown 滚动日报：`outputs/tracker/Jin10_Brent_Rolling_Reaction_YYYYMMDD_0800.md`
- HTML 滚动日报：`outputs/tracker/Jin10_Brent_Rolling_Reaction_YYYYMMDD_0800.html`
- 日报内嵌图表素材：`outputs/tracker/Jin10_Brent_Rolling_Reaction_YYYYMMDD_0800_brent_24h.svg`
- Markdown 审计报告：`outputs/tracker/Jin10_Brent_Rolling_Audit_YYYYMMDD_0800.md`
- HTML 审计报告：`outputs/tracker/Jin10_Brent_Rolling_Audit_YYYYMMDD_0800.html`
- SQLite 状态库：`data/jin10_tracker.sqlite`
- 运行日志：`logs/jin10_report_tracker_YYYYMMDD_HHMMSS.log`

正式日报会把现有5分钟Brent价格聚合为北京时间30分钟K线，并仅对既有匹配结果显示可点击的真实新闻时间点；同一根K线内的多条新闻会在图下按时间展开，未解释异动仅用浅灰区域提示。该展示层不改变新闻筛选、价格匹配、事件判断和独立Audit内容。

### 每 5 分钟自动运行

```bash
python3 run_jin10_tracker.py auto
```

自定义间隔：

```bash
python3 run_jin10_tracker.py auto --interval-seconds 300
```

自动模式会常驻进程。每轮失败会写入日志，并在下一轮继续运行。即使 Brent 行情更新失败，金十新闻抓取和入库也会继续。

### 仅重新生成日报

```bash
python3 run_jin10_tracker.py report
```

这个命令不抓取金十新闻、不更新 Brent 价格，只根据本地 SQLite 数据重新生成最近一个 08:00 截止窗口的正式日报和独立审计报告；同时按该窗口截止时间抓取并缓存一次最新 ISW Iran Update，抓取失败不会阻断主日报。

若该截止时间已有 ISW 失败缓存，人工重试时使用 `--retry-failed-isw`；成功缓存仍会复用：

```bash
python3 run_jin10_tracker.py report --date 2026-07-17 --retry-failed-isw
```

指定滚动日报截止日期：

```bash
python3 run_jin10_tracker.py report --date 2026-07-02
```

上述命令覆盖 `2026-07-01 08:00 <= 新闻 < 2026-07-02 08:00`。

### 回补历史日期

```bash
python3 run_jin10_tracker.py backfill-day --date 2026-07-02
```

历史回补仍只使用金十公开快讯接口，会从当前公开快讯流逐页向前回溯。如果公开接口在目标窗口前停止返回数据，日志会明确记录“目标日期可能需要登录/VIP历史查询或其他金十公开页面”。Brent 5 分钟K线会按 08:00 滚动窗口前后扩展范围单独补取。

## SQLite 字段说明

### `news`

- `news_id`：金十新闻唯一 ID。
- `source`：固定为 `金十数据`。
- `url`：金十原文链接。
- `title`：新闻标题或快讯主文本。
- `content`：原始快讯内容。
- `published_at_utc` / `published_at_bj`：新闻发布时间，分别为 UTC 和北京时间。
- `fetched_at_utc`：本地抓取时间。
- `keywords_json`：命中的关键词。
- `categories_json`：规则分类结果。
- `is_duplicate` / `duplicate_of`：是否重复及重复来源。
- `reaction_status`：`Pending Brent Reaction Analysis`、`analyzed` 等。
- `price_status`：`pending`、`ok`、`price_data_unavailable` 等。
- `is_in_daily`：是否进入最终重点日报。
- `is_event_update`：是否为同一事件后续进展。
- `event_type`：`首次事件`、`事件更新`、`重大升级`。
- `event_id` / `event_topic`：事件关联 ID 和主题。
- `created_at_utc` / `updated_at_utc`：本地记录创建和更新时间。

### `brent_klines`

- `symbol`：默认 `BZ=F`。
- `interval`：固定 `5m`。
- `ts_utc`：K 线时间，UTC。
- `open` / `high` / `low` / `close`：5 分钟 OHLC。
- `volume`：成交量，如数据源提供则保存。
- `source`：默认 `yahoo`。
- `fetched_at_utc`：行情抓取时间。

### `brent_reactions`

- `news_id`：关联新闻 ID。
- `baseline_time_utc` / `baseline_close`：兼容旧字段；当前等同于事件基准 K 线。
- `event_base_time_utc` / `event_base_close`：新闻发布后最近一根可用 5 分钟 K 线及其收盘价。
- `window_start_utc` / `window_end_utc`：发布后 30 分钟观察窗口。
- `window_high` / `window_low` / `final_close`：窗口内最高价、最低价和最后一根可用 K 线收盘价。
- `max_up_pct` / `max_down_pct` / `final_pct`：最大上涨、最大下跌、30 分钟最终涨跌幅。
- `price_time_5m_utc` / `price_5m` / `pct_5m`：事件后约 5 分钟匹配到的 K 线时间、价格和涨跌幅。
- `price_time_15m_utc` / `price_15m` / `pct_15m`：事件后约 15 分钟匹配到的 K 线时间、价格和涨跌幅。
- `price_time_30m_utc` / `price_30m` / `pct_30m`：事件后约 30 分钟匹配到的 K 线时间、价格和涨跌幅。
- `price_time_60m_utc` / `price_60m` / `pct_60m`：事件后约 60 分钟匹配到的 K 线时间、价格和涨跌幅。
- `pre30_time_utc` / `pre30_close` / `pre30_to_post30_pct`：事件前 30 分钟参考价及前30至后30累计变化。
- `reaction_label` / `price_reaction_level`：`强市场反应`、`中等市场反应`、`重要但暂未充分定价`、`价格数据不足`、`市场反应有限` 等。
- `trigger_window`：触发判断的核心窗口，如事件后60分钟或前30分钟至后30分钟。
- `oil_relevance_reason`：油市直接相关性依据，集合类新闻会记录被提取出的具体油市条目。
- `market_pricing_status`：已部分定价、尚未充分定价、与新闻逻辑背离或无法判断。
- `price_missing_reason`：价格数据缺失原因。
- `importance_reason`：进入重点事件的重大主题命中依据。
- `market_bias`：规则化油价方向判断，偏多、偏空或中性。
- `exclusion_reason`：未进入重点事件的具体原因。
- `market_interpretation`：规则化谨慎解读。
- `price_status`：价格分析状态。
- `multi_event_group_id`：同时间窗口多新闻事件组。
- `note`：异常或补充说明。

## 滚动日报重算示例

下面是一次只根据本地数据库重算 2026-07-02 08:00 滚动窗口的输出摘要：

```bash
python3 run_jin10_tracker.py report --date 2026-07-02
```

运行结果摘要：

```text
08:00滚动窗口校验:
report_start_bj=2026-07-01 08:00:00
report_end_bj=2026-07-02 08:00:00
first_news_bj=2026-07-01 08:00:00
last_news_bj=2026-07-01 10:27:33
first_brent_bj=2026-07-01 07:30:00
last_brent_bj=2026-07-01 10:34:09
latest_news_plus60_covered=no
news_total=8 price_klines=32

本地日报重算完成:
analyzed=8 pending=0 price_matched=8 price_missing=3
price_5m_missing=0 price_15m_missing=1 price_30m_missing=1 price_60m_missing=3
effective_oil_news=5 major_topic_hits=3 threshold_hits=5
final_daily=3 final_output_focus=3 excluded=5 multi_event_groups=0

日报已重新生成:
outputs/tracker/Jin10_Brent_Rolling_Reaction_20260702_0800.html
outputs/tracker/Jin10_Brent_Rolling_Reaction_20260702_0800.md
```

报告上半部分已输出霍尔木兹、原油贸易流向、OPEC/OPEC+供应决策等重点事件，并按 08:00 滚动窗口截取新闻。

## 已知限制

- 金十页面或接口结构变化、访问限制、登录要求、频率限制会导致抓取失败；日志会记录具体原因。
- Brent 默认价格源可能存在延迟、休市、缺失或接口异常；程序会保留新闻为 pending，后续自动补算。
- Yahoo Finance `BZ=F` 只是第一版 Brent 5 分钟行情来源，后续可替换为更稳定的付费或交易所数据接口。
- 同一 5 分钟窗口内多条高度相似新闻出现时，报告会标注同期多消息；超过 5 分钟或主题不同的新闻会分别分析。
- 价格反应只说明新闻发布时间附近的市场表现，不构成单一因果确认。
- 自动模式是本地常驻进程；如需要无人值守，可后续接入 APScheduler、launchd、Windows Task Scheduler 或服务器守护进程。

## 日报 Price Layer

常规 Brent 滚动日报会在末尾追加“明日 Brent 价格预测”。该板块只读取已经完成的六模型 Walk-Forward/OOS 结果，按日报截止时点截断历史记录后动态选择模型，并只运行当期选中的模型预测下一交易日；新闻、ISW、特朗普专栏、事件和基本面数据均不会进入 Price Layer。

六模型项目不在本仓库内。把 `BRENT_PRICE_LAYER_ROOT` 设为该项目根目录。未设置时，程序还会查找一个仅适用于原开发机的默认位置；其他环境请显式设置该变量，不要依赖这个默认位置。交易所临时新增全日休市时，用逗号分隔的 `BRENT_PRICE_LAYER_HOLIDAYS=YYYY-MM-DD,...` 补充日期。

模型产物、完整交易日价格或依赖不可用时，原日报仍照常生成，新增板块会写明不可用原因。选中模型由外部项目自己的虚拟环境执行。原有 `forecast-report` 冻结快照流程保持只读，不会触发这套新增模型运行。

## 许可证

本项目使用 [MIT License](LICENSE)。Copyright (c) 2026 Xxin0101 / Xincheng Huang。
