# BZ=F Extreme Move Disclosure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every valid Yahoo Finance `BZ=F` price and qualified Jin10 news item visible while making extreme moves diagnostic-only, preserving `PARTIAL` solely for genuine coverage failures, and permanently rendering ISW, “今日关注什么”, instrument identity, and coverage details.

**Architecture:** Keep the existing Yahoo fetch, SQLite schema, price-first movement scan, Jin10 filters, and homepage layout. Change report construction so continuity jumps are annotations rather than state or filtering inputs; preserve movement/news collections during `PARTIAL`; add fixed reader-facing instrument, coverage, ISW, and tracking sections through the existing Markdown-to-HTML presentation path.

**Tech Stack:** Python 3.12 standard library, SQLite, `unittest`, existing Markdown/SVG/HTML renderers.

## Global Constraints

- The only price instrument is Yahoo Finance `BZ=F`.
- `instrument_name` is `Brent Crude Oil Last Day Financ`.
- `contract_month` is empty and must never be inferred.
- `contract_type` is `Yahoo Finance BZ=F`.
- Do not add checks for `2609`, `2026年9月合约`, `BRNU26`, or `BZU6`.
- Do not change the existing 0.25%/0.50% movement thresholds.
- Do not change the price-first, news-second analysis order.
- Do not broaden or weaken the core Jin10 news filter.
- Do not move the K-line chart, blue detail panel, or other homepage layout regions.
- Do not add `INVALID`; extreme moves never change report status.
- Only coverage failures may continue to produce `PARTIAL`.
- Existing source and test files are untracked in Git; do not stage or commit them without a separate baseline decision. Commit only this plan document.

---

### Task 1: Make Continuity Jumps Diagnostic-Only

**Files:**
- Modify: `tests/test_daily_requirements.py`
- Modify: `jin10_market_report/tracking_report.py`

**Interfaces:**
- Consumes: `_build_window_audit(store, brent, start_utc, end_utc, timezone_name)`
- Produces: audit fields `price_continuity_issues`, `price_continuity_ok`, `price_covered`, `price_freshness`, and `data_complete` where continuity issues are disclosed but do not affect completeness.

- [ ] **Step 1: Write the failing status test**

Add a test that supplies complete news/price boundaries and one 15-minute +15.11% continuity issue, then asserts:

```python
audit = _build_window_audit(Store(), BrentConfig(), start, end, "Asia/Shanghai")
status, _ = classify_report_status(audit)

self.assertFalse(audit["price_continuity_ok"])
self.assertTrue(audit["price_covered"])
self.assertEqual(audit["price_freshness"], "passed")
self.assertEqual(status, "FORMAL")
```

The store fixture must return four news rows covering all three news segments, price rows covering `start - 30m` through `end + 65m`, and consecutive closes `87.17` and `100.34` inside the window.

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_daily_requirements.DailyRequirementTests.test_extreme_jump_is_diagnostic_not_status_gate -v
```

Expected: FAIL because `price_covered` and `price_freshness` currently include `price_continuity_ok`.

- [ ] **Step 3: Remove continuity from completeness calculations**

Change the window audit calculation to:

```python
price_covered = price_boundary_covered and latest_news_plus60_covered
data_complete = raw_news_window_covered and price_covered
price_freshness = "passed" if price_covered else "failed"
```

Keep:

```python
price_continuity_issues = _price_continuity_issues(price_rows)
price_continuity_ok = not price_continuity_issues
```

so the Audit retains diagnostic evidence.

- [ ] **Step 4: Remove continuity from top-level failure wording**

In `_top_level_warning`, do not add “行情连续性异常待复核” to reasons that explain why the report missed the formal threshold. Keep a separate extreme-move notice in the report body.

- [ ] **Step 5: Run the focused and existing status tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_daily_requirements.DailyRequirementTests.test_extreme_jump_is_diagnostic_not_status_gate \
  tests.test_daily_requirements.DailyRequirementTests.test_price_continuity_flags_short_extreme_jump_but_not_long_gap \
  tests.test_daily_requirements.DailyRequirementTests.test_window_audit_requires_both_price_guard_boundaries -v
```

Expected: all PASS.

---

### Task 2: Preserve Extreme Movements and PARTIAL News

**Files:**
- Modify: `tests/test_daily_requirements.py`
- Modify: `tests/test_tracking_chart.py`
- Modify: `jin10_market_report/tracking_report.py`

**Interfaces:**
- Consumes: `_build_price_movement_review(...)`
- Produces: a movement review whose `groups`, `windows`, `unexplained`, `selected_news`, `homepage_news`, `selected_raw_ids`, and chart markers remain present during extreme jumps and `PARTIAL`.

- [ ] **Step 1: Replace the quarantine regression test**

Replace the test that expects an extreme move to be removed with:

```python
result = _quarantine_continuity_movements(review, audit)

self.assertEqual(result["groups"], [clean, extreme_move])
self.assertEqual(result["windows"], [clean_window, extreme_window])
self.assertIn(extreme_move, result["unexplained"])
self.assertEqual(result["quarantined_groups"], [])
self.assertEqual(result["quarantined_windows"], [])
```

Also assert that the extreme move retains its `selected_news` and `homepage_news`.

- [ ] **Step 2: Add a failing PARTIAL preservation test**

Create a review containing one group with a core candidate, `selected_news`, `homepage_news`, and IDs. Call `_degrade_movement_review_for_incomplete_data` with `{"data_complete": False}` and assert:

```python
self.assertEqual(result["groups"], [group])
self.assertEqual(result["groups"][0]["selected_news"], [candidate])
self.assertEqual(result["groups"][0]["homepage_news"], [candidate])
self.assertEqual(result["selected_raw_ids"], {"news-1"})
self.assertEqual(result["core_raw_ids"], set())
self.assertEqual(result["groups"][0]["attribution_judgment"], "暂无法确认")
self.assertEqual(result["groups"][0]["attribution_confidence"], "低")
```

- [ ] **Step 3: Run both tests and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_daily_requirements.DailyRequirementTests.test_continuity_notice_preserves_extreme_movements \
  tests.test_daily_requirements.DailyRequirementTests.test_partial_preserves_qualified_news_and_windows -v
```

Expected: both FAIL under the current quarantine and clearing behavior.

- [ ] **Step 4: Make quarantine a no-op compatibility layer**

Keep the existing function name for callers/tests, but implement:

```python
def _quarantine_continuity_movements(review, audit):
    review["quarantined_groups"] = []
    review["quarantined_windows"] = []
    return review
```

The diagnostic issues remain in `audit`.

- [ ] **Step 5: Preserve collections while lowering certainty**

In `_degrade_movement_review_for_incomplete_data`:

- retain `groups`, `windows`, `selected_news`, `homepage_news`, `selected_raw_ids`, and each candidate;
- set group/candidate attribution judgment to `暂无法确认`;
- set attribution confidence to `低`;
- clear only `core_raw_ids`, because `PARTIAL` must not present an automated high-confidence core conclusion;
- keep associated news visible as context rather than deleting it;
- rebuild `unexplained` only for groups that truly have no qualified `selected_news`.

- [ ] **Step 6: Replace quarantine copy with the required notice**

Render continuity issues with:

```text
本时段出现极端价格波动，可能与重大突发事件有关，需结合相关新闻人工判断。
```

Remove “行情源连续性异常（不作市场异动）”, “确定错误”, and “不作为真实市场异动或新闻归因依据” from reader-facing output.

- [ ] **Step 7: Use the fixed no-news sentence**

For any group with no qualified news, render:

```text
暂未找到符合条件的金十新闻，原因暂无法确认，需人工继续核查。
```

- [ ] **Step 8: Run focused movement, panel, and chart tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_daily_requirements.DailyRequirementTests.test_continuity_notice_preserves_extreme_movements \
  tests.test_daily_requirements.DailyRequirementTests.test_partial_preserves_qualified_news_and_windows \
  tests.test_tracking_chart -v
```

Expected: all PASS; existing chart and panel interaction tests remain green.

---

### Task 3: Add Fixed BZ=F Identity and Coverage Sections

**Files:**
- Modify: `tests/test_daily_requirements.py`
- Modify: `tests/test_tracking_chart.py`
- Modify: `jin10_market_report/brent.py`
- Modify: `jin10_market_report/tracking_report.py`

**Interfaces:**
- Produces: `BRENT_INSTRUMENT_METADATA`
- Produces: audit diagnostics `duplicate_timestamp_count`, `timestamps_ordered`, `price_gap_count`, and `max_price_gap_minutes`
- Produces: reader-facing “标的信息” and “数据覆盖范围” sections.

- [ ] **Step 1: Write failing metadata and coverage tests**

Add assertions for:

```python
self.assertEqual(BRENT_INSTRUMENT_METADATA, {
    "instrument_name": "Brent Crude Oil Last Day Financ",
    "source": "Yahoo Finance",
    "ticker": "BZ=F",
    "contract_month": "",
    "contract_type": "Yahoo Finance BZ=F",
})
```

Build an audit from ordered rows with one 15-minute gap and assert:

```python
self.assertEqual(audit["duplicate_timestamp_count"], 0)
self.assertTrue(audit["timestamps_ordered"])
self.assertEqual(audit["price_gap_count"], 1)
self.assertEqual(audit["max_price_gap_minutes"], 15)
```

Build Markdown and HTML and assert both contain “标的信息”, “数据覆盖范围”, the five instrument fields, valid K-line count, gap count, and `PARTIAL` reason when applicable.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_daily_requirements.DailyRequirementTests.test_bz_f_instrument_metadata_is_fixed \
  tests.test_daily_requirements.DailyRequirementTests.test_window_audit_reports_timestamp_quality \
  tests.test_tracking_chart.TrackingChartTests.test_report_always_shows_instrument_and_coverage -v
```

Expected: FAIL because the metadata constant and coverage fields do not yet exist.

- [ ] **Step 3: Add the fixed metadata constant**

In `brent.py` add:

```python
BRENT_INSTRUMENT_METADATA = {
    "instrument_name": "Brent Crude Oil Last Day Financ",
    "source": "Yahoo Finance",
    "ticker": "BZ=F",
    "contract_month": "",
    "contract_type": "Yahoo Finance BZ=F",
}
```

Do not add contract-month parsing.

- [ ] **Step 4: Calculate timestamp diagnostics**

From the audit’s ordered `price_rows`, calculate:

```python
timestamps = [_price_ts(row) for row in price_rows]
timestamps = [item for item in timestamps if item is not None]
duplicate_timestamp_count = len(timestamps) - len(set(timestamps))
timestamps_ordered = timestamps == sorted(timestamps)
gaps = [
    int((current - previous).total_seconds() // 60)
    for previous, current in zip(timestamps, timestamps[1:])
    if current - previous > timedelta(minutes=5)
]
```

Store the count and maximum in the audit. These fields are diagnostic and do not hide data or independently change status.

- [ ] **Step 5: Render fixed sections**

Add Markdown sections before “本窗口Brent表现”:

```markdown
## 标的信息

- instrument_name: Brent Crude Oil Last Day Financ
- source: Yahoo Finance
- ticker: BZ=F
- contract_month:
- contract_type: Yahoo Finance BZ=F

## 数据覆盖范围
```

The coverage section uses the audit’s exact news/K-line start/end, counts, coverage booleans, missing reaction counts, timestamp diagnostics, cache flag, and `PARTIAL` reasons. The HTML renderer consumes the same Markdown without moving the chart or blue panel.

- [ ] **Step 6: Run metadata, coverage, and layout tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_daily_requirements.DailyRequirementTests.test_bz_f_instrument_metadata_is_fixed \
  tests.test_daily_requirements.DailyRequirementTests.test_window_audit_reports_timestamp_quality \
  tests.test_tracking_chart.TrackingChartTests.test_report_always_shows_instrument_and_coverage \
  tests.test_tracking_chart.TrackingChartTests.test_formal_html_keeps_chart_before_news_panel -v
```

Expected: all PASS.

---

### Task 4: Make ISW and 今日关注什么 Permanent

**Files:**
- Modify: `tests/test_tracking_chart.py`
- Modify: `jin10_market_report/tracking_report.py`

**Interfaces:**
- Consumes: existing `external_update` dictionaries.
- Produces: fixed Markdown/HTML modules `ISW Iran Update` and `今日关注什么`.

- [ ] **Step 1: Write failing success and failure tests**

For successful ISW data, assert Markdown and HTML contain:

- `ISW Iran Update`;
- 1–2 sentence Key Takeaways;
- an important-facts list;
- energy/Strait/port/shipping implications;
- the official URL;
- `今日关注什么`;
- all seven tracking categories.

For failed ISW data, assert both outputs contain:

```text
ISW Iran Update：本次抓取失败，待下次运行补充。
```

and:

```text
指定网站本次抓取失败，以下内容根据已获取的相关新闻和ISW信息整理。
```

as well as the ISW listing URL and the fixed module headings.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_tracking_chart.TrackingChartTests.test_isw_and_today_watch_always_render_on_success \
  tests.test_tracking_chart.TrackingChartTests.test_isw_and_today_watch_always_render_on_failure -v
```

Expected: FAIL because the current Markdown nests ISW under “接下来关注什么” and the HTML headings/copy differ.

- [ ] **Step 3: Split the Markdown modules**

Refactor `_tracking_outlook_lines` into fixed output that always emits:

```markdown
## ISW Iran Update
```

followed by:

```markdown
## 今日关注什么
```

On success, render `key_takeaways_zh` first, then important facts, `energy_significance_zh`, publication metadata, and the official URL. On failure, render the exact failure sentence and listing URL.

- [ ] **Step 4: Build the seven tracking items**

Always include:

1. Strait of Hormuz passage;
2. tanker/port/AIS changes;
3. sanctions, waivers, and export policy;
4. OPEC+ and producer policy;
5. conflict escalation or de-escalation;
6. EIA/API and supply-demand data;
7. the most useful successful ISW takeaway, or an explicit pending-ISW item on failure.

On failure, prepend the exact specified-site failure note. Use existing Jin10 evidence and fixed verification topics; do not fabricate ISW conclusions.

- [ ] **Step 5: Update HTML card headings without changing layout**

Keep the existing summary-grid card positions and IDs, but change visible headings to:

```html
<h2>ISW Iran Update</h2>
<h2>今日关注什么</h2>
```

Update fallback parsers to recognize the new Markdown headings.

- [ ] **Step 6: Run ISW, dashboard, and HTML tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_tracking_chart.TrackingChartTests.test_isw_and_today_watch_always_render_on_success \
  tests.test_tracking_chart.TrackingChartTests.test_isw_and_today_watch_always_render_on_failure \
  tests.test_tracking_chart -v
```

Expected: all PASS.

---

### Task 5: Full Verification and 2026-07-24 Rebuild

**Files:**
- Regenerate: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260724_0800_PARTIAL.md`
- Regenerate: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260724_0800_PARTIAL.html`
- Regenerate: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260724_0800_FAILED.md`
- Regenerate: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260724_0800_FAILED.html`
- Generate: a new report log under `logs/`

**Interfaces:**
- Consumes: current SQLite data only.
- Produces: rebuilt reader and audit artifacts demonstrating the corrected behavior.

- [ ] **Step 1: Run the complete test suite**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -v
```

Expected: every test passes with zero failures and zero errors.

- [ ] **Step 2: Rebuild the report without fetching**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 run_jin10_tracker.py report \
  --date 2026-07-24 \
  --db-path data/jin10_tracker.sqlite \
  --output-dir outputs/tracker \
  --log-dir logs \
  --timezone Asia/Shanghai
```

Expected: report remains `PARTIAL` only if current stored K-lines still fail the existing boundary or latest-news +60 coverage gates. Extreme jumps alone must not cause `PARTIAL`.

- [ ] **Step 3: Verify required content**

Check that Markdown, HTML, Audit, and SVG:

- identify only Yahoo Finance `BZ=F`;
- show `contract_month` empty;
- retain 86.06, 100.34, 102.01 in price metrics and chart data where applicable;
- include every price movement group, including groups overlapping the four >5% jumps;
- contain the human-review extreme-move notice;
- do not contain “不作市场异动”, “价格一定错误”, `INVALID`, `2609`, `BRNU26`, or `BZU6`;
- preserve every qualified Jin10 item and no-news groups;
- always contain `ISW Iran Update` and `今日关注什么`;
- display data coverage and `PARTIAL` reasons;
- keep the chart before the blue news panel and retain the exact empty-state prompt.

- [ ] **Step 4: Compare status facts**

Confirm the final log, Audit, and filename agree on:

- news status;
- price coverage status;
- `FORMAL` or `PARTIAL`;
- manual rerun requirement;
- K-line boundaries;
- missing reaction counts;
- continuity jumps as diagnostic-only.

- [ ] **Step 5: Report the completed changes**

Return:

- modified file list;
- confirmation that no >5% `INVALID` rule exists;
- confirmation that extreme values remain in high/low/change calculations;
- confirmation that ISW is always rendered;
- confirmation that “今日关注什么” is always rendered;
- confirmation that Yahoo Finance `BZ=F` remains the only instrument;
- paths to rebuilt HTML, Markdown, Audit, and log.
