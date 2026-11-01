# Brent 周一方向预测日报交接

最后核对：2026-09-10 16:29 CST
工作目录：`/Users/xiaoxin/Documents/抓取数据`
当前状态：`COMPLETE`，完整回归、静态门槛、冻结快照核对、正式重渲染、内容验收和幂等复用均已通过。

## 0. 2026-09-10 常规滚动日报增量记录

- 已完成北京时间 `2026-09-09 08:00 <= t < 2026-09-10 08:00` 的常规 Brent 滚动日报；原新闻筛选、异动识别、归因和 Price Layer 规则未修改。
- 正式产物：
  - `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260910_0800.html`
  - `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260910_0800.md`
  - `outputs/tracker/Jin10_Brent_Rolling_Audit_20260910_0800.html`
  - `outputs/tracker/Jin10_Brent_Rolling_Audit_20260910_0800.md`
- 窗口校验：原始新闻 `1290` 条、过滤后 `218` 条、Brent 5分钟K线 `275` 根；原始新闻和价格边界覆盖均为 `yes`，价格连续性为 `yes`，正式状态为 `FORMAL`。
- Brent 指标：最新价 `101.91`，24小时涨跌幅 `+2.37%`，区间最高 `102.57`，区间最低 `98.83`。
- Price Layer：预测日期 `2026-09-10`，选择 `ARIMA`，当前收盘价 `101.63`，预测价 `101.46`，预测涨跌幅 `-0.17%`，方向为下跌。
- ISW：严格按 `2026-09-10 08:00` 北京时间截止。`Iran Update, September 9, 2026` 于北京时间 `08:16` 发布，因晚于截止点而排除；本期使用截止前最新的 `Iran Update, September 8, 2026`，北京时间 `2026-09-09 08:54` 发布，3条 Key Takeaways 原文与人工逐条中文翻译一一对应。
- 金十历史抓取做了两项不改变业务逻辑的最小容错修复：响应体 `socket.timeout` 统一封装为既有 `Jin10FetchError`，历史分页从请求窗口结束时点直接开始，避免先扫描截止后的实时流。对应测试先失败后通过。
- 最终验证：`python3 -m unittest discover -s tests -v` 退出码 `0`，`Ran 162 tests`，`OK`；成品断言和 `git diff --check` 均通过。

## 1. 新任务读取边界

新 Codex 任务按以下顺序开始：

1. 完整阅读根目录 `SPEC.md`。
2. 完整阅读本文件。
3. 只打开本文件“允许检查的文件”列出的代码、测试和产物。
4. 从“唯一下一步”继续，不重复已经完成的抓取、训练或历史探索。

不得读取旧任务聊天、旧 rollout、完整历史终端输出、`.codex/sessions`、`docs/superpowers/` 内旧计划或与本次日期无关的报告。只有当本文件列出的事实与本地文件直接矛盾、且通过允许检查的文件无法定位时，才可读取一段最小相关日志，并在本文件记录原因。

允许检查的文件：

- `SPEC.md`
- `HANDOFF.md`
- `run_jin10_tracker.py`
- `jin10_market_report/forecast_pipeline.py`
- `jin10_market_report/forecast.py`
- `jin10_market_report/tracking_report.py`
- `tests/test_forecast.py`
- 本文件第 4 节列出的五个正式产物

工作树原本就有大量用户修改和未跟踪文件。不得执行 `git reset`、`git checkout --`、批量删除、清理未跟踪文件或覆盖无关改动；本任务只修改解决本交接所述缺口所必需的文件。

## 2. 固定目标

生成覆盖 `2026-08-20` 至 `2026-08-21`、预测 `2026-08-24` Brent 方向的正式日报。周末生成阶段只读取冻结的周五本地快照：不得联网、抓取、回补、重训、重新分类新闻或运行实时 freshness。

新闻截止为北京时间 `2026-08-22 00:00:00`，边界为左闭右开。周六凌晨仍属于周五交易会话的 Brent 5 分钟价格可以使用，但周末新闻不得进入预测。

## 3. 已完成且无需重复的工作

- `forecast-report` 已改为硬只读，读取独立的 `_friday_snapshot.json`。
- 新增本地命令 `prepare-forecast-snapshot`；该命令只从保存的 JSON 整理严格周五快照。
- 快照校验已覆盖日期、窗口、`future_data_used=false`、Model A 结果、新闻事件截止和最终方向。
- 同一快照已有 SHA-256 复用机制。
- 冻结快照模式下，周末 freshness 被跳过；ISW 和特朗普模块不进入模型或历史归因。
- 严格快照已于 2026-08-23 22:30 生成并通过本地读取校验：
  - `snapshot_kind=frozen_friday`
  - `target_date=2026-08-24`
  - `daily_price_last_date=2026-08-21`
  - `future_data_used=false`
  - 新闻窗口结束：`2026-08-21T16:00:00+00:00`
  - 价格窗口结束：`2026-08-21T21:00:00+00:00`
  - 周五事件 70 条；旧保存结果中的 5 条周六新闻已排除
  - Model A：LightGBM，上涨概率 `0.5400764804102713`
  - Model A walk-forward：504 个测试样本，Accuracy `0.5238095238`，Balanced Accuracy `0.5085470085`，AUC `0.5231560620`，Brier `0.2503531647`，F1 `0.6190476190`
  - Model B/C 因新闻历史只有 33 个会话，不具备数值概率修正资格
  - 最终方向：偏多；置信度：LOW；不展示主观拼接的最终概率
  - 快照 SHA-256：`fa1aabb1f0142cdc7ec5ecc9953ff1e221f158b844a08c2c6f9b13424ed01b42`

2026-08-23 22:36 的专项核验：

- `test_prepare_friday_snapshot_filters_legacy_weekend_news`：通过。
- `test_cli_exposes_local_friday_snapshot_preparation`：通过。
- `test_same_snapshot_reuses_existing_report`：失败，唯一错误为 `KeyError: 'report_logic_version'`。
- 较早的 138 项全量测试曾通过，但发生在最后一轮快照边界和清单断言修改之前，不能作为最终回归证据。

## 4. 当前正式路径和有效性

固定路径：

- `outputs/tracker/Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824_friday_snapshot.json`
- `outputs/tracker/Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824.md`
- `outputs/tracker/Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824.html`
- `outputs/tracker/Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824_brent_24h.svg`
- `outputs/tracker/Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824_forecast_result.json`

快照及现有 Markdown、HTML、SVG 和结果清单均为最终验收版本：

- 结果清单包含 `report_logic_version: 2` 和正确的快照 SHA、目标日期及正式路径。
- `_load_reusable_result` 同时校验快照 SHA、文件路径和报告逻辑版本，不复用旧渲染。
- Markdown 与 HTML 使用冻结快照文案，不出现提示重新联网的外部源失败文字。
- CLI 首次正式重渲染输出 `reused=false`，第二次原样运行输出 `reused=true`。

历史 `_PARTIAL`、`_FAILED` 和 `_forecast_audit.json` 文件保留为证据，不删除，也不得作为最终正式产物。

## 5. 唯一下一步

无。固定目标已完成；除非用户提出新的日期或需求，不再运行抓取、回补、快照准备、重训或重渲染。

## 6. 逐步执行流程

每一步成功后才进入下一步，并在本文件“执行记录”追加命令、退出码和关键结果。

### 步骤 1：确认起点

运行：

```bash
git status --short
python3 run_jin10_tracker.py prepare-forecast-snapshot --help
python3 run_jin10_tracker.py forecast-report --help
```

预期：两个子命令均存在；不要在此步骤运行抓取、回补或快照准备。

### 步骤 2：先复现单一失败

```bash
python3 -m unittest tests.test_forecast.ForecastReadOnlyPipelineTests.test_same_snapshot_reuses_existing_report -v
```

预期起点：只因缺少 `report_logic_version` 失败。若失败原因不同，立即按第 10 节停止，不要猜测修改。

### 步骤 3：做最小实现和回归测试

只修改第 5 节列出的版本写入、版本校验、CLI 状态输出及对应测试。先运行直接相关测试：

```bash
python3 -m unittest \
  tests.test_forecast.ForecastReadOnlyPipelineTests.test_same_snapshot_reuses_existing_report \
  tests.test_forecast.ForecastReadOnlyPipelineTests.test_friday_snapshot_rejects_weekend_news \
  tests.test_forecast.ForecastReadOnlyPipelineTests.test_prepare_friday_snapshot_filters_legacy_weekend_news \
  tests.test_forecast.ForecastReadOnlyPipelineTests.test_missing_snapshot_fails_without_fallback \
  tests.test_forecast.ForecastReadOnlyPipelineTests.test_weekend_pipeline_uses_only_saved_friday_snapshot \
  tests.test_forecast.ForecastReadOnlyPipelineTests.test_frozen_report_skips_realtime_freshness_audit \
  tests.test_forecast.IswRetryTests.test_cli_exposes_local_friday_snapshot_preparation \
  -v
```

新增的旧版本失效测试和 CLI `reused` 输出测试也必须单独运行并通过。

### 步骤 4：完整测试

使用已经验证可导入 LightGBM 4.6.0 的本地运行时：

```bash
env \
  PYTHONPYCACHEPREFIX=/private/tmp/brent_pycache \
  PYTHONPATH=/private/tmp/brent_forecast_deps \
  DYLD_LIBRARY_PATH=/private/tmp/brent_forecast_libomp/libomp/22.1.8/lib \
  /Users/xiaoxin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  -m unittest tests.test_forecast -v
```

随后运行全量回归：

```bash
env \
  PYTHONPYCACHEPREFIX=/private/tmp/brent_pycache \
  PYTHONPATH=/private/tmp/brent_forecast_deps \
  DYLD_LIBRARY_PATH=/private/tmp/brent_forecast_libomp/libomp/22.1.8/lib \
  /Users/xiaoxin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  -m unittest discover -s tests -v
```

两条命令都必须退出码为 0。记录实际测试总数，不能引用旧的 138 项结果代替。

### 步骤 5：静态和文档门槛

```bash
git diff --check -- SPEC.md HANDOFF.md run_jin10_tracker.py jin10_market_report/forecast_pipeline.py tests/test_forecast.py
rg -n "T[B]D|T[O]DO|implement [l]ater|fill in [d]etails" SPEC.md HANDOFF.md run_jin10_tracker.py jin10_market_report tests
rg -n "[[:blank:]]+$" SPEC.md HANDOFF.md run_jin10_tracker.py jin10_market_report/forecast_pipeline.py tests/test_forecast.py
```

预期：三条命令均无问题输出。第二、三条 `rg` 返回未命中是预期，不把未命中当作失败。

### 步骤 6：核对冻结快照，不重算

读取第 4 节快照并核对第 3 节列出的日期、窗口、事件数、概率和 SHA。快照当前有效，不运行 `prepare-forecast-snapshot`。只有代码修改意外使现有快照合同校验失败时才停止；不得自动从旧 audit 重建。

### 步骤 7：记录生成前时间并执行最终生成命令

先记录以下文件的修改时间：Markdown、HTML、SVG、结果清单。然后执行且只执行一次第 8 节的最终生成命令。预期标准输出包含 `reused=false`，因为旧清单缺少逻辑版本，必须重渲染。

### 步骤 8：验收并验证幂等

首次生成后逐项核对：

- 五个正式产物均存在，Markdown、HTML、SVG 和结果清单的修改时间晚于首次最终命令开始时间。
- 结果清单包含 `report_logic_version: 2`、正确快照 SHA、正确目标日期和正式路径。
- Markdown 与 HTML 顶部先为“今日总结”，其后为“2026年8月24日 Brent 预测”。
- 展示 LightGBM 上涨概率约 `54.01%`、下跌概率约 `45.99%`、回测指标、新闻方向、最终偏多和 LOW。
- 明确新闻截止到北京时间 2026-08-22 00:00，不把周末风险写成已发生事实。
- 不出现“ISW抓取失败”“特朗普专栏抓取失败”“待下次运行补充”或其他提示重新联网的冻结模式文案。
- ISW 和特朗普仍明确不参与模型及历史归因。

记录 Markdown 和 HTML 的修改时间，再执行第二次完全相同的最终命令。预期标准输出包含 `reused=true`，且 Markdown 和 HTML 修改时间与第二次运行前完全相同。

只有以上全部满足，才把本文件当前状态改为 `COMPLETE`，写入最终测试数、两次 `reused` 状态、产物修改时间和路径。

## 7. 本地快照准备命令

当前快照已经完成，不应重复运行。仅用于说明可恢复路径；若第 10 节允许人工决定重建，确切命令为：

```bash
python3 run_jin10_tracker.py prepare-forecast-snapshot \
  --source-path outputs/tracker/Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824_forecast_audit.json \
  --start-date 2026-08-20 \
  --end-date 2026-08-21 \
  --target-date 2026-08-24
```

该命令只允许读取本地保存结果；若观察到任何网络访问，立即停止。

## 8. 最终生成命令

以下命令是本次唯一正式生成命令，首次用于重渲染，第二次原样重复用于验证幂等：

```bash
env \
  PYTHONPYCACHEPREFIX=/private/tmp/brent_pycache \
  PYTHONPATH=/private/tmp/brent_forecast_deps \
  DYLD_LIBRARY_PATH=/private/tmp/brent_forecast_libomp/libomp/22.1.8/lib \
  /Users/xiaoxin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  run_jin10_tracker.py forecast-report \
  --start-date 2026-08-20 \
  --end-date 2026-08-21 \
  --target-date 2026-08-24
```

禁止给该命令增加回补、抓取、重训、外部源重试或实时 freshness 参数。

## 9. 重试上限

- 每条完全相同的命令最多执行 2 次：首次执行加 1 次重试；不得第三次执行。
- 数据合同、快照校验、日期、截止时间、未来数据、断言、代码语法、依赖缺失和确定性测试失败不重试，直接停止。
- 只有明确的瞬时本地故障才允许重试一次，例如进程被系统偶发中断、临时文件占用或一次性资源抖动。
- 允许重试前只能形成一个可验证假设；一次只做一个最小修正，不连续堆叠多种猜测。
- 完整测试失败时，只定位并修正一个直接根因，然后重跑失败测试和一次全量回归；若同一失败再次出现，停止。
- 最终生成命令若因确定性合同或内容校验失败，重试次数为 0；若为明确瞬时本地故障，只允许重试 1 次。
- 幂等验证的第二次命令是验收步骤，不计作失败重试；如果它未返回 `reused=true`，这是确定性失败，停止而不是继续运行。

## 10. 停止条件

出现任一条件立即停止，不进入后续阶段：

- 发现 `forecast-report` 调用了网络、金十/Yahoo 抓取、回补、ISW/特朗普刷新、模型重新拟合、新闻重新分类或实时 freshness。
- 缺少快照、快照 SHA 改变、日期或窗口不符、含周末新闻、`future_data_used` 不为 false，或 Model A 已保存结果缺失。
- 同一失败在首次执行和一次允许的重试后仍出现。
- 任一命令连续 10 分钟没有新输出：只允许查看一次进程状态和最后输出；不得继续盲等。若无法证明仍在取得进展，终止该命令并停止。
- 本任务累计执行时间达到 45 分钟仍未完成。
- 出现上下文即将压缩、已压缩、反复重新阅读或无法确定已经完成哪一步的迹象。
- 发现另一个任务或进程正在修改 `run_jin10_tracker.py`、`jin10_market_report/forecast_pipeline.py`、`tests/test_forecast.py`、`SPEC.md`、`HANDOFF.md` 或正式产物。
- 工作树出现本任务范围外的新改动，或需要删除、重置、覆盖用户文件才能继续。
- 完整测试非零退出，且一次最小根因修正后仍不能通过。
- 正式产物仍含旧冻结模式失败文案、文件时间未更新、第二次运行未复用，或任一 SPEC 验收项不满足。

停止前必须完成以下动作：

1. 把本文件当前状态改为 `BLOCKED`。
2. 在“执行记录”写入最后命令、退出码、最后一段错误、已经使用的重试次数、已排除原因和唯一下一步。
3. 不运行新的诊断分支，不回读旧聊天，不让任务继续挂起。
4. 向用户简短报告具体阻塞条件和所需决定。

## 11. 执行记录

- 2026-08-23 22:36：旧任务已停止，当前没有已知并发写入者。
- 2026-08-23 22:36：专项测试复现唯一已知缺口：`report_logic_version` 未写入结果清单。
- 2026-08-23 22:36：严格周五快照存在，SHA 和窗口核对通过；正式报告仍是旧文案版本，等待逻辑版本修复后重渲染。
- 唯一下一步：执行第 6 节步骤 1，然后按步骤 2 复现，完成第 5 节的最小实现。
- 2026-08-23 22:52 CST，步骤 1：`git status --short`，退出码 `0`；工作树仍为交接已声明的多修改/多未跟踪状态，本任务范围内可见 `HANDOFF.md`、`SPEC.md`、`run_jin10_tracker.py` 及未跟踪的 `forecast_pipeline.py`、`test_forecast.py`，未发现与交接事实直接矛盾的新证据。
- 2026-08-23 22:52 CST，步骤 1：`python3 run_jin10_tracker.py prepare-forecast-snapshot --help`，退出码 `0`；本地快照准备子命令存在，本步骤未运行快照准备。
- 2026-08-23 22:52 CST，步骤 1：`python3 run_jin10_tracker.py forecast-report --help`，退出码 `0`；只读正式报告子命令存在，本步骤未运行报告生成、抓取、回补或训练。
- 2026-08-23 22:52 CST，步骤 1 结论：通过。唯一下一步更新为执行步骤 2 的单一失败复现命令；相同命令尚未使用重试。
- 2026-08-23 22:53 CST，步骤 2：`python3 -m unittest tests.test_forecast.ForecastReadOnlyPipelineTests.test_same_snapshot_reuses_existing_report -v`，退出码 `1`；稳定复现 `tests/test_forecast.py:928` 的 `KeyError: 'report_logic_version'`，1 项测试仅此错误，未重试。
- 2026-08-23 22:53 CST，步骤 2 根因核对：`forecast_pipeline.py` 的新清单写入体没有 `report_logic_version`，`_load_reusable_result` 也未校验该版本；`run_jin10_tracker.py` 的成功输出未打印 `reused`。与交接所述单一根因完全一致，没有出现不同失败。
- 2026-08-23 22:53 CST，步骤 2 结论：预期失败已确认。唯一下一步更新为步骤 3：先补“旧/缺失版本必须重渲染”和 CLI 小写 `reused` 输出测试并观察预期失败，再做版本常量、写入、校验和输出的最小实现。
- 2026-08-24 00:43 CST，步骤 3 RED：`python3 -m unittest tests.test_forecast.ForecastReadOnlyPipelineTests.test_old_or_missing_report_logic_version_forces_rerender -v`，退出码 `1`；缺失版本和版本 `1` 两个子场景均因错误返回 `reused=True` 失败，符合预期。
- 2026-08-24 00:43 CST，步骤 3 RED：`python3 -m unittest tests.test_forecast.IswRetryTests.test_forecast_cli_prints_lowercase_reused_status -v` 首次退出码 `1`，测试夹具缺少 `log_dir`；只补齐该参数后第二次同命令退出码 `1`，两个子场景分别因缺少 `reused=false` 和 `reused=true` 失败，确认目标行为尚未实现。该完全相同命令已执行 2 次，不得再次原样执行。
- 2026-08-24 00:43 CST，步骤 3 最小实现：`forecast_pipeline.py` 新增单一 `REPORT_LOGIC_VERSION = 2`，复用清单要求版本匹配，新清单写入该版本；`run_jin10_tracker.py` 成功输出新增小写 `reused=true|false`。测试仅新增旧/缺失版本重渲染与本地模拟 CLI 输出场景，未触发网络、回补或训练。
- 2026-08-24 00:43 CST，步骤 3 GREEN：原相同快照复用测试、旧/缺失版本重渲染测试和 CLI 小写复用状态测试分别单独运行，退出码均为 `0`，各 `Ran 1 test`、`OK`；CLI 通过命令使用 `python3 -m unittest -v ...`，未第三次原样执行 RED 命令。
- 2026-08-24 00:43 CST，步骤 3 专项测试：按第 6 节步骤 3 的 7 项固定命令运行，退出码 `0`，`Ran 7 tests`，`OK`。本步骤没有瞬时故障重试，也没有遗留失败。
- 2026-08-24 00:43 CST，停止记录：时间命令 `date '+%Y-%m-%d %H:%M:%S %Z'` 退出码 `0`，返回 `2026-08-24 00:43:35 CST`；相对本任务 2026-08-23 22:52 的步骤 1 起点已超过 45 分钟，命中第 10 节累计执行时间停止条件。最后一条工作命令为 7 项专项测试，退出码 `0`，无错误尾段；所有允许的瞬时故障重试次数均为 `0`，没有合同/快照/网络类错误证据。本任务未进入步骤 4，也未运行正式生成命令。
- 2026-08-24 00:43 CST，唯一下一步：新任务完整读取 `SPEC.md` 与本文件后，直接执行第 6 节步骤 4 的 `tests.test_forecast` 完整测试命令；成功后继续同一步全量 `tests/` 回归，不重复步骤 1—3。
- 2026-08-24 08:33 CST，步骤 4：按第 6 节固定本地 LightGBM 运行时执行 `python3 -m unittest tests.test_forecast -v`，退出码 `0`，`Ran 46 tests`，`OK`；未触发网络、回补、重训或实时 freshness。
- 2026-08-24 08:33 CST，步骤 4：随后执行 `python3 -m unittest discover -s tests -v`，退出码 `0`，`Ran 143 tests`，`OK`；本轮完整回归通过，无失败和重试。
- 2026-08-24 08:33 CST，步骤 5：`git diff --check -- SPEC.md HANDOFF.md run_jin10_tracker.py jin10_market_report/forecast_pipeline.py tests/test_forecast.py` 退出码 `0`、无问题输出；占位内容和行尾空白两条固定 `rg` 命令均退出码 `1` 且无输出，表示预期未命中。唯一下一步更新为步骤 6：只读核对现有冻结快照，不重算。
- 2026-08-24 08:35 CST，步骤 6：只读核对现有 `_friday_snapshot.json`，SHA-256 为 `fa1aabb1f0142cdc7ec5ecc9953ff1e221f158b844a08c2c6f9b13424ed01b42`；`snapshot_kind=frozen_friday`、`target_date=2026-08-24`、`daily_price_last_date=2026-08-21`、`future_data_used=false`。新闻窗口为 `2026-08-20T00:00:00+00:00` 至 `2026-08-21T16:00:00+00:00`，价格窗口截至 `2026-08-21T21:00:00+00:00`，事件 70 条且最晚事件为北京时间 `2026-08-21T23:29:58+08:00`。
- 2026-08-24 08:35 CST，步骤 6：Model A 仍为 LightGBM，上涨概率 `0.5400764804102713`、下跌概率 `0.45992351958972866`；walk-forward 504 个测试样本，Accuracy `0.5238095238`、Balanced Accuracy `0.5085470085`、AUC `0.5231560620`、Brier `0.2503531647`、F1 `0.6190476190`。新闻历史 33 个会话，Model B/C 无数值修正资格；最终偏多、`LOW`，不展示最终概率。快照全部符合交接记录，本步骤未运行准备、抓取、回补、重训或重算。唯一下一步更新为步骤 7：记录正式产物时间后首次执行最终生成命令。
- 2026-08-24 08:34 CST，步骤 7：生成前 Markdown/HTML 修改时间均为 `2026-08-23 22:30:35 CST`，SVG 为 `2026-08-23 22:30:34 CST`，结果清单为 `2026-08-23 22:30:35 CST`；最终命令开始时间为 `2026-08-24 08:34:44 CST`。
- 2026-08-24 08:34 CST，步骤 7：首次执行第 8 节唯一正式生成命令，退出码 `0`，标准输出明确包含 `reused=false`。正式 Markdown、HTML 和结果清单修改时间更新为 `2026-08-24 08:34:54 CST`，SVG 更新为 `2026-08-24 08:34:53 CST`，均晚于命令开始时间。
- 2026-08-24 08:37 CST，步骤 8 内容验收：五个正式产物均存在；清单包含 `report_logic_version: 2`、正确快照 SHA、`target_date=2026-08-24` 和正式路径。Markdown/HTML 均先显示“今日总结”，再显示“2026年8月24日 Brent 预测”；页面展示 LightGBM 上涨 `54.0%`、下跌 `46.0%`（底层值分别为 `54.007648%`、`45.992352%`）、504 个 walk-forward 样本及完整指标、新闻偏多、最终偏多和 `LOW`。新闻截止明确为北京时间 `2026-08-22 00:00` 且不含周末新闻；周末事项仅以 `Future Risk Event` 表述；冻结模式不出现 ISW/特朗普抓取失败或待补跑文字，并明确不触发周末抓取或补跑。SVG 通过 XML 校验并完成可视检查。
- 2026-08-24 08:37 CST，步骤 8 幂等验收：第二次原样执行最终命令，退出码 `0`，标准输出明确包含 `reused=true`；运行前后 Markdown 与 HTML 修改时间均保持 `2026-08-24 08:34:54 CST`，未重渲染。
- 2026-08-24 08:38 CST，最终状态更新为 `COMPLETE`。完整测试记录为 `tests.test_forecast` 46 项、全量 `tests/` 143 项，均为 `OK`；首次/第二次正式命令分别为 `reused=false`/`reused=true`。五个正式产物路径仍为第 4 节固定路径，无 `_PARTIAL` 或 `_FAILED` 后缀。
