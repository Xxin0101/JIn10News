# Brent 周一方向预测只读 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `forecast-report` 收口为基于周五本地计算快照的零网络、零回补、无周末 freshness、幂等报告命令。

**Architecture:** `forecast_pipeline.py` 负责校验冻结快照、重建预测展示数据和结果清单复用；`tracking_report.py` 增加冻结快照审计模式，跳过实时边界 freshness 与状态降级；CLI 删除可选回补语义，使只读成为不可绕过的默认行为。

**Tech Stack:** Python 3、SQLite 只读查询、JSON 快照、unittest、Markdown/HTML 报告生成。

**Spec:** `docs/superpowers/specs/2026-08-23-brent-monday-forecast-read-only-design.md`

## Global Constraints

- 周六、周日不得调用任何价格、新闻或外部补充抓取函数。
- 不得执行历史回补、LightGBM/Logistic 重拟合或新闻预测重算。
- 不得以周末无新 K 线为由运行实时 freshness 或降级为 PARTIAL。
- 快照缺失或无效时立即失败，不允许联网兜底。
- 同一快照只允许一次报告渲染，后续执行复用现有产物。

---

### Task 1: 冻结快照契约与硬只读预测流水线

**Files:**
- Modify: `tests/test_forecast.py`
- Modify: `jin10_market_report/forecast_pipeline.py`

**Interfaces:**
- Consumes: `<output_stem>_forecast_audit.json`，包含 `target_date`、`daily_price_last_date`、`future_data_used`、`quant`、`news`、`final`。
- Produces: `_load_forecast_snapshot(path, end_day, target_day)` 与只读 `run_forecast_report(...)`。

- [ ] **Step 1: Write the failing tests**

  添加集成测试，以完整本地快照和最小只读 Store 调用 `run_forecast_report`；将 `run_backfill_day`、`fetch_yahoo_daily_bars`、`quantitative_forecast`、`build_news_forecast_from_store`、`fetch_latest_isw_update`、`fetch_trump_statements` patch 为抛错，断言报告仍生成且 `backfills == []`。

- [ ] **Step 2: Run tests to verify RED**

  Run: `python3 -m unittest tests.test_forecast.ForecastReadOnlyPipelineTests.test_weekend_pipeline_uses_only_saved_friday_snapshot -v`  
  Expected: FAIL，因为当前实现仍调用 ISW，并只从快照读取 `quant`、仍重算 `news`。

- [ ] **Step 3: Implement the minimal read-only path**

  在 `forecast_pipeline.py` 删除抓取、回补与训练导入和分支；验证周五到周一日期关系，完整加载 `quant/news/final`，只从这些字段构造预测 payload，并给外部补充传入固定的 `not_applicable` 本地占位对象。

- [ ] **Step 4: Run tests to verify GREEN**

  Run: `python3 -m unittest tests.test_forecast.ForecastReadOnlyPipelineTests.test_weekend_pipeline_uses_only_saved_friday_snapshot -v`  
  Expected: PASS。

### Task 2: 冻结快照审计，不执行周末 freshness

**Files:**
- Modify: `tests/test_forecast.py`
- Modify: `jin10_market_report/tracking_report.py`

**Interfaces:**
- Consumes: `build_markdown_report(..., frozen_snapshot=True)`。
- Produces: 正式无后缀 Markdown，审计标记 freshness 为 `not_applicable`，不调用实时 `_build_window_audit`。

- [ ] **Step 1: Write the failing test**

  在测试中 patch `_build_window_audit` 为抛错，使用 `frozen_snapshot=True` 生成报告；断言产物名不含 `_PARTIAL`/`_FAILED`，展示状态为周五冻结快照。

- [ ] **Step 2: Run test to verify RED**

  Run: `python3 -m unittest tests.test_forecast.ForecastReadOnlyPipelineTests.test_frozen_report_skips_realtime_freshness_audit -v`  
  Expected: FAIL，因为当前 builder 总会调用 `_build_window_audit`。

- [ ] **Step 3: Implement minimal frozen audit**

  新增纯本地 `_build_frozen_snapshot_audit`，只做报告渲染需要的本地统计，不做新闻/价格结束边界 freshness；`build_markdown_report` 在冻结模式下使用该审计并强制正式快照状态，同时继续禁止 `refresh_multi_event_groups`。

- [ ] **Step 4: Run test to verify GREEN**

  Run: `python3 -m unittest tests.test_forecast.ForecastReadOnlyPipelineTests.test_frozen_report_skips_realtime_freshness_audit -v`  
  Expected: PASS。

### Task 3: 指纹幂等与 CLI 收口

**Files:**
- Modify: `tests/test_forecast.py`
- Modify: `jin10_market_report/forecast_pipeline.py`
- Modify: `run_jin10_tracker.py`

**Interfaces:**
- Consumes: 快照 SHA-256 与 `<output_stem>_forecast_result.json`。
- Produces: `reused: bool`、稳定产物路径；CLI 不再暴露或传递 `--skip-backfill`。

- [ ] **Step 1: Write the failing tests**

  第一次调用写入结果清单；第二次 patch `build_markdown_report`/`build_html_report` 为抛错并断言仍返回，且 `reused=True`。另断言快照缺失会抛 `FileNotFoundError` 且 CLI 默认没有 `skip_backfill` 属性。

- [ ] **Step 2: Run tests to verify RED**

  Run: `python3 -m unittest tests.test_forecast.ForecastReadOnlyPipelineTests.test_same_snapshot_reuses_existing_report tests.test_forecast.ForecastReadOnlyPipelineTests.test_missing_snapshot_fails_without_fallback tests.test_forecast.IswRetryTests.test_cli_exposes_one_explicit_forecast_report_command -v`  
  Expected: 至少幂等和 CLI 断言 FAIL。

- [ ] **Step 3: Implement result manifest and CLI change**

  计算快照内容 SHA-256；匹配清单与存在的正式产物时直接返回；首次完成后原子写结果清单。删除 `--skip-backfill` 参数及调用参数。

- [ ] **Step 4: Run focused and full verification**

  Run: `python3 -m unittest tests.test_forecast -v`  
  Expected: 所有 forecast 测试 PASS。

- [ ] **Step 5: Run repository regression verification**

  Run: `python3 -m unittest discover -s tests -v`  
  Expected: 全部测试 PASS，且测试期间没有网络访问。

