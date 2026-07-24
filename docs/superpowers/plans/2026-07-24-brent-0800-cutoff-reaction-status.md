# Brent 08:00 Cutoff and Reaction Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the scheduled Beijing 08:00 cutoff authoritative for all daily Brent conclusions, move the 60-minute observation requirement to per-news status, and permanently retain fixed report modules and qualified content.

**Architecture:** Keep the existing fixed day-bound calculation, Yahoo `BZ=F` ingestion, SQLite schema, price-first movement scan, Jin10 filters, and homepage layout. Restrict report queries and metrics to `[day_start, day_end)`, derive window completeness only from in-window source and K-line quality, and annotate each candidate with a cutoff-aware `reaction_status` used by Markdown and HTML presentation.

**Tech Stack:** Python 3 standard library, SQLite-backed `TrackerStore`, `unittest`, Markdown and embedded HTML/JavaScript report renderer.

## Global Constraints

- The only instrument is Yahoo Finance `BZ=F` with `contract_month` empty and `contract_type` equal to `Yahoo Finance BZ=F`.
- The report window is Beijing previous-day 08:00 inclusive to report-day 08:00 exclusive.
- The scheduled 08:00 cutoff never moves with actual script completion time.
- A move above 5% never triggers `INVALID`, deletion, quarantine, or hidden output.
- Existing movement thresholds, price-first ordering, Jin10 core filters, homepage layout, retry logic, and file naming remain unchanged.
- Qualified news, movement windows, ISW Iran Update, and 今日关注什么 remain visible during `PARTIAL`.

---

### Task 1: Lock Completeness to the Scheduled Window

**Files:**
- Modify: `jin10_market_report/tracking_report.py`
- Test: `tests/test_daily_requirements.py`
- Test: `tests/test_tracking_chart.py`

**Interfaces:**
- Consumes: `_build_window_audit(store, brent, start_utc, end_utc, timezone_name)`.
- Produces: audit fields whose required price range equals `start_utc` through `end_utc`, plus explicit in-window gap diagnostics.

- [ ] **Step 1: Add failing coverage tests**

Add tests that build a store containing K-lines from report start through 07:55 and an extra post-cutoff row. Assert the audit passes the end boundary without requiring 09:05, excludes the extra row from `price_rows`, and never emits a latest-news-plus-60 coverage gap.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python3 -m unittest tests.test_daily_requirements.DailyRequirementTests.test_window_audit_stops_at_scheduled_0800 -v`

Expected: FAIL because the current audit still requests the guard interval through 09:05 and gates completeness on latest news plus 60 minutes.

- [ ] **Step 3: Implement window-only completeness**

Change `_build_window_audit` so `price_rows` use `start_utc, end_utc`; `price_start_required` and `price_end_required` equal those scheduled bounds; end coverage is satisfied by a last in-window row within the existing 10-minute tolerance; and latest-news-plus-60 is removed from `price_covered`, `coverage_gaps`, warnings, and Audit status text.

Add a material-gap diagnostic for gaps over 10 minutes during tradable time. Recognize the normal daily Brent maintenance break around Beijing 05:00–06:00 as expected, disclose it separately, and do not downgrade on that break alone. Duplicate or unordered timestamps and material in-session gaps make `price_covered` false.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python3 -m unittest tests.test_daily_requirements -v`

Expected: all daily requirement tests pass.

### Task 2: Bound Movement Metrics and Add Per-News reaction_status

**Files:**
- Modify: `jin10_market_report/tracking_report.py`
- Test: `tests/test_daily_requirements.py`

**Interfaces:**
- Consumes: candidates from `_build_price_movement_review` and stored reaction rows.
- Produces: candidate keys `reaction_status` and `reaction_status_note`; group attribution that only treats `CONFIRMED` news as confirmed while preserving all qualified news.

- [ ] **Step 1: Add failing status and cutoff tests**

Create a synthetic 07:49 news candidate whose stored reaction row may contain later values. Assert `reaction_status == "PENDING"`, the candidate remains in `selected_news` and `homepage_news`, and the group does not expose it as a confirmed core driver. Add a store-call assertion that metric K-lines are requested only through `end_utc`.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python3 -m unittest tests.test_daily_requirements.DailyRequirementTests.test_0749_news_is_pending_and_preserved -v`

Expected: FAIL because candidates currently have no per-news reaction status and metric rows extend 60 minutes beyond cutoff.

- [ ] **Step 3: Implement cutoff-aware news status**

Add a helper that returns:

```python
if published_at + timedelta(minutes=60) > scheduled_end:
    return "PENDING", "新闻已纳入，但截至08:00后续60分钟价格观察尚未完成，暂不做最终归因。"
if the stored event baseline and 5/15/30/60-minute values are complete and price_status is usable:
    return "CONFIRMED", ""
return "UNCONFIRMED", "已有部分价格数据，但暂时无法确认价格反应。"
```

Use `end_utc` rather than `end_utc + 60 minutes` for movement metrics. Preserve qualified `PENDING` and `UNCONFIRMED` candidates, but exclude them from confirmed `core_driver`, `secondary_drivers`, and high-confidence attribution.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python3 -m unittest tests.test_daily_requirements -v`

Expected: all daily requirement tests pass.

### Task 3: Render Status and Permanent Modules

**Files:**
- Modify: `jin10_market_report/tracking_report.py`
- Test: `tests/test_tracking_chart.py`

**Interfaces:**
- Consumes: candidate `reaction_status` and `reaction_status_note`, plus the existing external update dictionary.
- Produces: matching Markdown and HTML status disclosure, fixed ISW and 今日关注什么 modules, and unchanged blue-panel news cardinality.

- [ ] **Step 1: Add failing presentation tests**

Assert a PENDING item includes the exact pending sentence in Markdown and the blue panel payload; ISW failure emits `本次ISW抓取失败，以下内容待下次运行补充。`; successful ISW dashboard data includes the original publication date; and rendered blue-panel news count equals the eligible backend list.

- [ ] **Step 2: Run presentation tests and confirm RED**

Run: `python3 -m unittest tests.test_tracking_chart -v`

Expected: failures on the new exact copy, publication-date item, and reaction-status fields.

- [ ] **Step 3: Implement presentation changes**

Add reaction status and notes to `_panel_news_item`, movement Markdown, and the embedded JavaScript entry renderer. Update failure copy in both Markdown and dashboard fallbacks. Add the successful ISW publication date as a reader-visible dashboard item while retaining the original URL.

- [ ] **Step 4: Run presentation tests and confirm GREEN**

Run: `python3 -m unittest tests.test_tracking_chart -v`

Expected: all tracking chart tests pass.

### Task 4: Full Verification and Simulated Report

**Files:**
- Regenerate: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260724_0800*.md`
- Regenerate: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260724_0800*.html`
- Regenerate: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260724_0800*.md`
- Regenerate: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260724_0800*.html`

**Interfaces:**
- Consumes: existing SQLite cache and the `report --date` entry.
- Produces: a reproducible local simulated report with a strict 08:00 cutoff and reader-visible fixed modules.

- [ ] **Step 1: Run the complete suite**

Run: `PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -p 'test_*.py' -v`

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Regenerate the cached report**

Run: `PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 run_jin10_tracker.py report --date 2026-07-24 --db-path data/jin10_tracker.sqlite --output-dir outputs/tracker --timezone Asia/Shanghai`

Expected: output status depends only on actual in-window source and K-line completeness, never on 09:05 or latest-news-plus-60 availability.

- [ ] **Step 3: Verify the generated artifacts**

Confirm:

- the statistical window ends at 2026-07-24 08:00 Beijing;
- no 08:00-after price is included in metrics or movement groups;
- PARTIAL reasons, if present, name actual in-window gaps rather than future data;
- the 07:49 news is visible as PENDING when it passes existing news eligibility;
- extreme prices remain in headline metrics;
- ISW Iran Update and 今日关注什么 are present in Markdown and HTML;
- the chart precedes the blue details panel;
- the empty-state sentence remains exact;
- report Markdown, HTML, Audit Markdown, Audit HTML, SVG, and log/output evidence exist and are non-empty.
