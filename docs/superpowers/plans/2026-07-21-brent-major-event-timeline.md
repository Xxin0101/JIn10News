# Brent Major Event Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add every qualifying major news item to the formal HTML timeline without changing price-attribution selection, and display concise Chinese ISW Key Takeaways in the Iran update card.

**Architecture:** build_markdown_report will return a small presentation bundle alongside its existing Markdown path. The bundle contains already-filtered major-event timeline items and ISW card items, so the formal HTML renderer never re-fetches sources, re-runs classifications, or changes formal Markdown/Audit text. The renderer will combine the bundle with the existing Markdown-derived price-review event data, then expose both through the existing blue detail panel.

**Tech Stack:** Python 3 standard library, existing SQLite TrackerStore, static HTML/CSS/vanilla JavaScript, unittest.

## Global Constraints

- Include only raw news that hits a major topic, passes direct_energy_supply_reason, is not a summary, and is not marked duplicate.
- Sort the timeline in ascending Beijing publication time and retain events already selected for price review with an explicit "已进入重点复盘" state.
- K-line blue markers and all selected_raw_ids price-attribution logic remain unchanged.
- Formal Markdown and Audit content/generation remain unchanged; presentation data is passed in memory from report generation to HTML rendering.
- ISW HTML card shows 3-5 Chinese Key Takeaways plus at most two relevance-qualified body supplements; it does not use ISW to change Brent attribution.
- Keep the four existing right-side "接下来关注什么" strings and their order byte-for-byte unchanged.
- Do not add dependencies, database fields, report filenames, or sidecar output files.

---

## File Structure

- Modify jin10_market_report/tracking_report.py: define the formal-report presentation bundle, filter major-event timeline rows, render the compact timeline, and extend the existing detail-panel client script.
- Modify jin10_market_report/external_update.py: preserve Key Takeaways separately from relevance-qualified body supplements in the existing cached ISW payload while retaining summary_zh for Markdown compatibility.
- Modify jin10_market_report/tracker.py: pass the returned presentation bundle to the formal HTML renderer for normal generation, backfill, and partial-report recovery.
- Modify run_jin10_tracker.py: pass the returned presentation bundle in the report command.
- Modify tests/test_tracking_chart.py: cover candidate filtering/order and formal HTML timeline/detail rendering.
- Modify tests/test_daily_requirements.py: cover ISW Key Takeaways and supplemental extraction/cache compatibility.

### Task 1: Build Formal Presentation Data

**Files:**
- Modify: jin10_market_report/tracking_report.py:488-570, 3380-3530, 4086-4155
- Modify: jin10_market_report/tracker.py:428-437, 665-678, 891-902
- Modify: run_jin10_tracker.py:205-216
- Test: tests/test_tracking_chart.py

**Interfaces:**
- Produces FormalReportPresentation(major_timeline_items, iran_update_items) and ReportBuildResult(markdown_path, presentation).
- Produces _major_event_timeline_items(audit, reaction_rows, selected_raw_ids, timezone_name) -> list[dict[str, str]] with id, time, short_title, title, topic, url, description, and review_status keys.
- Consumes the current audit["major_candidates"], reaction rows, and movement_review["selected_raw_ids"].

- [ ] **Step 1: Write the failing test**

~~~python
def test_major_timeline_keeps_only_qualified_nonduplicate_raw_news_in_time_order(self) -> None:
    audit = {
        "major_candidates": [
            (raw("late", "2026-07-20T12:10:00Z", "霍尔木兹油轮绕航", is_summary=0), "直接涉及原油运输", "命中重大主题：霍尔木兹通行/护航/封锁/油轮运行风险"),
            (raw("early", "2026-07-20T11:00:00Z", "原油出口港口装船受阻", is_summary=0), "直接涉及原油供应", "命中重大主题：原油出口、港口装船、制裁或OPEC/OPEC+产量政策"),
            (raw("summary", "2026-07-20T11:30:00Z", "中东局势跟踪", is_summary=1), "直接涉及原油运输", "命中重大主题：霍尔木兹通行/护航/封锁/油轮运行风险"),
            (raw("duplicate", "2026-07-20T11:40:00Z", "油轮遭袭", is_summary=0), "直接涉及原油运输", "命中重大主题：霍尔木兹通行/护航/封锁/油轮运行风险"),
            (raw("indirect", "2026-07-20T11:50:00Z", "地区外交会谈", is_summary=0), "缺少明确能源传导", "命中重大主题：地缘冲突影响能源运输或供应安全"),
        ]
    }
    reactions = [
        {"news_id": "late", "is_duplicate": 0},
        {"news_id": "early", "is_duplicate": 0},
        {"news_id": "summary", "is_duplicate": 0},
        {"news_id": "duplicate", "is_duplicate": 1},
        {"news_id": "indirect", "is_duplicate": 0},
    ]

    items = _major_event_timeline_items(audit, reactions, {"late"}, "Asia/Shanghai")

    self.assertEqual([item["id"] for item in items], ["early", "late"])
    self.assertEqual(items[0]["review_status"], "")
    self.assertEqual(items[1]["review_status"], "已进入重点复盘")
~~~

- [ ] **Step 2: Run test to verify it fails**

Run:

~~~bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_tracking_chart.TrackingChartTests.test_major_timeline_keeps_only_qualified_nonduplicate_raw_news_in_time_order -v
~~~

Expected: FAIL because _major_event_timeline_items and the presentation result types do not exist.

- [ ] **Step 3: Write minimal implementation**

~~~python
@dataclass(frozen=True)
class FormalReportPresentation:
    major_timeline_items: tuple[dict[str, str], ...]
    iran_update_items: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class ReportBuildResult:
    markdown_path: Path
    presentation: FormalReportPresentation


def _major_event_timeline_items(audit, reaction_rows, selected_raw_ids, timezone_name):
    reaction_by_id = {str(row["news_id"]): row for row in reaction_rows}
    selected = {str(value) for value in selected_raw_ids}
    items = []
    for raw_row, oil_reason, major_reason in audit.get("major_candidates") or []:
        raw_id = str(raw_row["raw_id"])
        reaction = reaction_by_id.get(raw_id)
        if int(raw_row["is_summary"] or 0) or (reaction and int(reaction["is_duplicate"] or 0)):
            continue
        title = str(raw_row["title"] or "")
        if not direct_energy_supply_reason(title, _raw_text(raw_row)):
            continue
        items.append(_major_timeline_item(raw_row, major_reason or oil_reason, raw_id in selected, timezone_name))
    return sorted(items, key=lambda item: (item["time"], item["id"]))
~~~

Have build_markdown_report create this list from its existing audit, all_reaction_rows, and selected_raw_ids, then return ReportBuildResult. Update each call site to use result.markdown_path and call build_html_report(result.markdown_path, output_dir, presentation=result.presentation). Preserve build_html_report(markdown_path, output_dir) by making presentation optional.

- [ ] **Step 4: Run test to verify it passes**

Run:

~~~bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_tracking_chart.TrackingChartTests.test_major_timeline_keeps_only_qualified_nonduplicate_raw_news_in_time_order -v
~~~

Expected: PASS; qualifying events are ordered, summaries/duplicates/non-direct rows are absent, and selected review items retain their status.

- [ ] **Step 5: Commit**

~~~bash
git add jin10_market_report/tracking_report.py jin10_market_report/tracker.py run_jin10_tracker.py tests/test_tracking_chart.py
git commit -m "feat: prepare major event timeline data"
~~~

### Task 2: Render and Interact With the Major Event Timeline

**Files:**
- Modify: jin10_market_report/tracking_report.py:3828-4155
- Test: tests/test_tracking_chart.py

**Interfaces:**
- Consumes FormalReportPresentation.major_timeline_items through _parse_formal_dashboard(text, presentation=None).
- Produces section#major-event-timeline, button.major-timeline-entry[data-major-event-key], and JSON in #brent-major-event-data.
- Reuses #related-news-panel without changing existing data-event-key K-line and unmatched-band behavior.

- [ ] **Step 1: Write the failing test**

~~~python
rendered = build_html_report(
    markdown,
    output_dir,
    presentation=FormalReportPresentation(
        major_timeline_items=(
            {"id": "raw-1", "time": "2026-07-20 20:10", "short_title": "胡塞宣布对沙特实施海上禁令", "title": "胡塞武装宣布对沙特实施海上禁令", "topic": "地缘政治/军事局势", "url": "https://flash.jin10.com/detail/1", "description": "命中重大主题，作为运输风险跟踪。", "review_status": ""},
            {"id": "raw-2", "time": "2026-07-20 20:23", "short_title": "伊朗称维护霍尔木兹利益", "title": "伊朗外交部称将坚定维护在霍尔木兹海峡的国家利益", "topic": "霍尔木兹航运风险", "url": "https://flash.jin10.com/detail/2", "description": "命中重大主题，已进入重点复盘。", "review_status": "已进入重点复盘"},
        ),
        iran_update_items=(),
    ),
).read_text(encoding="utf-8")

self.assertIn('id="major-event-timeline"', rendered)
self.assertLess(rendered.index('id="related-news-panel"'), rendered.index('id="major-event-timeline"'))
self.assertIn('data-major-event-key="raw-1"', rendered)
self.assertIn("胡塞宣布对沙特实施海上禁令", rendered)
self.assertIn("地缘政治/军事局势", rendered)
self.assertIn("已进入重点复盘", rendered)
self.assertIn("重大事件追踪（非价格归因）", rendered)
self.assertIn("if(item.kind==='major')", rendered)
self.assertIn("Enter", rendered)
self.assertIn("event.key===' '", rendered)
~~~

Add assertions that the original K-line data-event-key, existing empty prompt, and all four tracking strings remain present and unchanged.

- [ ] **Step 2: Run test to verify it fails**

Run:

~~~bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_tracking_chart.TrackingChartTests.test_formal_html_renders_major_event_timeline_without_changing_price_review -v
~~~

Expected: FAIL because the formal dashboard has no major-event timeline markup or data payload.

- [ ] **Step 3: Write minimal implementation**

~~~python
lines.append('<section id="major-event-timeline" class="major-timeline" aria-label="重大事件时间轴"><h2>重大事件时间轴</h2>')
for item in dashboard.get("major_timeline_items") or []:
    lines.append(
        '<button type="button" class="major-timeline-entry" '
        f'data-major-event-key="{html.escape(item["id"], quote=True)}">'
        f'<time>{html.escape(item["time"])}</time>'
        f'<span class="major-timeline-title">{html.escape(item["short_title"])}</span>'
        f'<span class="major-timeline-tag">{html.escape(item["topic"])}</span>'
        f'<span class="major-timeline-status">{html.escape(item["review_status"])}</span>'
        '</button>'
    )
lines.append('</section>')
~~~

Add a major-event branch to the current client script. It must clear the K-line active state, mark the clicked timeline item active, replace the blue panel with the non-attribution heading, full title, time/topic, short description, optional status, and a target="_blank" source link. Do not create price metric cards in that branch. Add keyboard handling for the timeline buttons, and CSS for a single-column vertical rail with wrapping titles/tags on narrow screens.

- [ ] **Step 4: Run test to verify it passes**

Run:

~~~bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_tracking_chart.TrackingChartTests.test_formal_html_renders_major_event_timeline_without_changing_price_review -v
~~~

Expected: PASS; the compact list appears after the blue panel, uses the supplied order, and its interaction remains non-causal.

- [ ] **Step 5: Commit**

~~~bash
git add jin10_market_report/tracking_report.py tests/test_tracking_chart.py
git commit -m "feat: render major event timeline"
~~~

### Task 3: Structure ISW Key Takeaways for the HTML Card

**Files:**
- Modify: jin10_market_report/external_update.py:163-234, 395-436
- Modify: jin10_market_report/tracking_report.py:3767-3892
- Test: tests/test_daily_requirements.py
- Test: tests/test_tracking_chart.py

**Interfaces:**
- Produces key_takeaways_zh: list[str] and supplemental_zh: list[str] in successful ISW cache records while retaining existing summary_zh and energy_significance_zh unchanged.
- Produces _isw_dashboard_items(external_update) -> list[dict[str, str]] with 3-5 key-takeaway items followed by at most two supplemental items.
- Consumes the report generator's existing external_update object to populate FormalReportPresentation.iran_update_items.

- [ ] **Step 1: Write the failing test**

~~~python
source = {
    "fetch_method": "官网直连",
    "title": "Iran Update, July 21, 2026",
    "url": "https://understandingwar.org/research/middle-east/iran-update-july-21-2026/",
    "published_at_utc": "2026-07-21T04:00:00Z",
    "source_bullets": [
        "Iranian forces threatened commercial shipping near the Strait of Hormuz.",
        "Iranian officials discussed sanctions relief and nuclear negotiations.",
        "Regional military forces prepared additional air defense deployments.",
    ],
    "supplemental_source_bullets": [
        "A tanker operator reported new insurance constraints for Gulf voyages.",
        "An unrelated domestic political development occurred.",
    ],
}
with patch("jin10_market_report.external_update._translate_bullets", side_effect=[
    ["伊朗部队威胁霍尔木兹附近商船。", "伊朗官员讨论制裁缓解与核谈判。", "地区军事力量准备追加防空部署。"],
    ["一名油轮运营商报告海湾航线出现新的保险限制。"],
]):
    result = fetch_latest_isw_update(cutoff, cache_dir=cache_dir)

self.assertEqual(result["key_takeaways_zh"], result["summary_zh"])
self.assertEqual(result["supplemental_zh"], ["一名油轮运营商报告海湾航线出现新的保险限制。"])
self.assertEqual(len(_isw_dashboard_items(result)), 4)
~~~

Add a dashboard test asserting all displayed items are Chinese, link to the official article, remain at most seven total, and the existing failed-ISW card still links to ISW_LISTING_URL.

- [ ] **Step 2: Run tests to verify they fail**

Run:

~~~bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_daily_requirements.DailyRequirementTests.test_isw_cache_keeps_key_takeaways_and_relevant_supplements tests.test_tracking_chart.TrackingChartTests.test_formal_html_renders_chinese_isw_key_takeaways -v
~~~

Expected: FAIL because the cache has no separated Key Takeaways/supplemental fields and the dashboard uses only the first Markdown-parsed summary.

- [ ] **Step 3: Write minimal implementation**

~~~python
def _isw_relevance_score(text: str) -> int:
    lowered = text.lower()
    groups = (
        ("iran", "hormuz", "shipping", "tanker", "vessel", "port"),
        ("sanction", "oil export", "production", "energy", "refinery"),
        ("strike", "attack", "missile", "drone", "military", "escalat"),
    )
    return sum(1 for group in groups if any(term in lowered for term in group))


def _select_isw_supplements(source_bullets: list[str]) -> list[str]:
    return [item for item in source_bullets if _isw_relevance_score(item) >= 2][:2]
~~~

Extend the official-site and Bluesky source structures with supplemental_source_bullets. Translate Key Takeaways and selected supplements separately, store both lists in the existing JSON cache, and retain summary_zh as the original Key Takeaways translation for Markdown compatibility. Build the HTML card from key_takeaways_zh[:5] + supplemental_zh[:2], preserving the current failure fallback. Do not change _tracking_outlook_lines or its four fixed tracking items.

- [ ] **Step 4: Run tests to verify they pass**

Run:

~~~bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_daily_requirements.DailyRequirementTests.test_isw_cache_keeps_key_takeaways_and_relevant_supplements tests.test_tracking_chart.TrackingChartTests.test_formal_html_renders_chinese_isw_key_takeaways tests.test_tracking_chart.TrackingChartTests.test_formal_html_links_isw_listing_when_external_update_failed -v
~~~

Expected: PASS; successful cards show concise Chinese source-backed items, and failures retain the ISW listing link without invented summaries.

- [ ] **Step 5: Commit**

~~~bash
git add jin10_market_report/external_update.py jin10_market_report/tracking_report.py tests/test_daily_requirements.py tests/test_tracking_chart.py
git commit -m "feat: summarize ISW key takeaways"
~~~

### Task 4: Run Regression and Regenerate the Formal HTML

**Files:**
- Modify: generated current formal report and HTML only through the existing report command
- Test: tests/

**Interfaces:**
- Consumes the final CLI report path and existing data/jin10_tracker.sqlite cache.
- Produces the current day's formal HTML, Markdown, Audit Markdown/HTML, and one new report log without altering output names.

- [ ] **Step 1: Run the full unit suite**

~~~bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -v
~~~

Expected: PASS with all existing and new tests green.

- [ ] **Step 2: Regenerate the current report from the existing cache**

~~~bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 run_jin10_tracker.py report --date 2026-07-21 --db-path data/jin10_tracker.sqlite --output-dir outputs/tracker --log-dir logs --timezone Asia/Shanghai
~~~

Expected: the standard four report artifacts regenerate; no source-code/configuration changes occur during this command.

- [ ] **Step 3: Inspect the generated formal HTML**

~~~bash
rg -n 'major-event-timeline|重大事件追踪（非价格归因）|伊朗局势更新|AIS船位|EIA/API库存' outputs/tracker/Jin10_Brent_Rolling_Reaction_20260721_0800.html
~~~

Expected: the timeline and non-attribution panel text exist; ISW card exists; each unchanged tracking item remains present.

- [ ] **Step 4: Commit**

~~~bash
git add jin10_market_report/external_update.py jin10_market_report/tracking_report.py jin10_market_report/tracker.py run_jin10_tracker.py tests/test_daily_requirements.py tests/test_tracking_chart.py
git commit -m "feat: add Brent major event timeline"
~~~

## Plan Self-Review

- Spec coverage: Task 1 implements the confirmed eligibility policy and preserves Markdown/Audit; Task 2 implements the separate homepage timeline and its non-causal detail state; Task 3 implements Chinese ISW Key Takeaways plus capped supplements and failure fallback; Task 4 verifies regression and regenerated output.
- Placeholder scan: completed; all tasks have concrete files, test commands, expected outcomes, and implementation boundaries.
- Type consistency: all renderer call sites consume ReportBuildResult.markdown_path and ReportBuildResult.presentation; direct HTML tests continue to use the optional presentation parameter.
