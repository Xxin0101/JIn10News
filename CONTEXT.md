# 上下文

公开仓库 `Xxin0101/JIn10News` 的短入口。只写本仓库 `README.md`、`SPEC.md`、`HANDOFF.md`、`SPEC_20260904.md`、`HANDOFF_20260904.md`、`SECURITY.md`、`CONTRIBUTING.md` 和公开代码里对得上的事实。对不上的不写。本文件与 `DECISIONS.md` 不改采集、频率、预测或部署；未进入 `main` 前，不表示 `main` 已更新。

## 目标

从金十公开快讯筛选伊朗、中东局势、能源供应、航运和贸易内容，对照 Brent，生成 Word 日报或 HTML/Markdown 滚动日报。它不是金十数据或 Yahoo Finance 的官方产品。输出用于研究整理，不构成投资建议；新闻附近的价格变化只描述同期表现。

本仓库做新闻与反应日报。六模型价格层在仓外，只由 `BRENT_PRICE_LAYER_ROOT` 指向。不要把本仓库称作 5+1 价格模型主仓。

## 架构

包 `jin10-market-report`（导入名 `jin10_market_report`，`0.1.0`，Python 3.10+）。`run_jin10_report.py` 生成 Word。`run_jin10_tracker.py` 提供 `init-db`、`run-once`、`auto`、`report`、`backfill-day`，以及 `prepare-forecast-snapshot` 和只读 `forecast-report`。

不另建采集器，不改目录：抓取与 Word 在 `fetcher.py`、`processor.py`、`report.py`；行情与反应在 `brent.py`、`reaction.py`、`storage.py`、`tracker.py`；滚动日报在 `tracking_report.py`；周一预测在 `forecast.py`、`forecast_pipeline.py`；外部六模型适配在 `price_layer.py`；ISW 与特朗普背景在 `external_update.py`、`trump_update.py`。

新闻只请求 `https://flash-api.jin10.com/get_flash_list`。Brent 第一版是 Yahoo Finance `BZ=F` 的 5 分钟 K 线。时区 `Asia/Shanghai`，窗口为前一日 08:00 ≤ 发布时间 < 当日 08:00。`auto` 默认 300 秒。`data/`、`outputs/`、`logs/` 已忽略，不要提交。

## 现有决定

- 5 分钟跟踪只做规则计算，不调用大模型。未安装 LightGBM 时，跟踪方向用内置逻辑回归。`SPEC.md` 的周一正式预测必须使用真实 LightGBM，缺失则失败，不得静默替代。
- 正式复盘先看价格异动，再匹配供给端新闻。30 分钟绝对波动 0.50% 进入主复盘；没有强窗口时才用 0.25% 补充窗口。
- ISW Iran Update 与金十特朗普专栏不进入归因、`selected_news` 或 Price Layer。
- 价格预测板块只读取外部六模型已完成的 Walk-Forward/OOS，按截止时点选择并只运行当期模型。名字见 `price_layer.py` 的 `EXPECTED_MODELS`：`ARIMA_1_1_1`、`Linear_Regression_MA3_MA9`、`LSTM`、`XGBoost`、`LightGBM`、`Ensemble_Average`。训练依赖在外部项目自己的虚拟环境中。不可用时其余日报仍生成。`forecast-report` 不启动这套模型。
- 公开使用请设置 `BRENT_PRICE_LAYER_ROOT`。未设置时还有仅适用于原开发机的默认位置；不要依赖它，本文不记录该路径。额外休市用 `BRENT_PRICE_LAYER_HOLIDAYS`。
- 周末 `forecast-report` 只读周五冻结快照。`_PARTIAL` 与 `_FAILED` 不是正式交付。安全问题见 `SECURITY.md`，使用私人安全通告。

## 命名

滚动日报：`Jin10_Brent_Rolling_Reaction_YYYYMMDD_0800`（Markdown、HTML、SVG、Audit）。周一预测前缀：`Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824`。外部价格层根目录变量名是 `BRENT_PRICE_LAYER_ROOT`。

## 进度与公开测试证据

正式日报在被忽略的 `outputs/`，不能只看 git 树。新会话按表阅读，不做全库扫描。`docs/superpowers/` 与 `docs/daily-reports/` 不是起点。长记录留在 HANDOFF。会话规则见 `DECISIONS.md`。

| 任务 | 先读 | 文件自述 |
| --- | --- | --- |
| 运行方式 | `README.md`、`CONTRIBUTING.md` | 公开说明 |
| 周一预测：2026-08-20 至 2026-08-21，目标 2026-08-24 | `SPEC.md`、`HANDOFF.md` 及其点名的代码和测试 | `COMPLETE`（2026-08-24；文首另有 2026-09-10 滚动日报） |
| 滚动日报，参考样本 2026-09-04 | `SPEC_20260904.md`、`HANDOFF_20260904.md` | `COMPLETE`（最后核对 2026-09-10） |
| 安全 | `SECURITY.md` | 私人安全通告 |

本次没有重跑测试。公开入口是 `python3 -m unittest discover -s tests`：`README.md` 写明使用模拟响应，不访问外网，不读本机私有路径。`.github/workflows/ci.yml` 在 Python 3.10 与 3.12 上执行该命令，只 `pip install -e .`，不安装 LightGBM。`HANDOFF.md` 记录 2026-08-24：`tests.test_forecast` 46 项、全量 143 项通过；2026-09-10：全量 162 项通过，退出码 0，并记录 `git diff --check` 通过。
