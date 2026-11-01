# Brent Blue Panel Complete News Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the formal HTML blue detail panel the complete, price-first reading surface for all display-qualified Jin10 news and unmatched major events.

**Architecture:** Keep the existing price-movement review and its strict `selected_news` attribution result unchanged. Add a separate in-memory homepage presentation model built from every movement group's already-scored candidates plus unmatched qualified major candidates, then let the HTML renderer use that model for the default all-day view and K-line focus state. Audit consumes the same destination model only to explain where a major candidate appeared or why it was legitimately excluded.

**Tech Stack:** Python 3 standard library, SQLite-backed `TrackerStore`, static HTML/CSS/JavaScript generation, `unittest`.

## Global Constraints

- Do not change `_scan_30m_price_windows`, the 0.50% strong threshold, the 0.25% fallback threshold, or `_merge_price_windows`.
- Keep each movement group's existing `search_start_dt` / `search_end_dt` matching window.
- Preserve `selected_news`, core-driver selection, Markdown price-attribution text, ISW content, and the right-side tracking card.
- Use display eligibility only for HTML presentation; do not alter the SQLite schema or write display status into the existing attribution fields.
- A homepage item must be a direct-supply-path, `新增事实`, valid Jin10-link, nonduplicate, non-noise item. Priority may sort only identical publication timestamps; it must never exclude an eligible item.
- Unmatched qualified major candidates belong in the blue panel's `⚠ 重大事件` section. Remove the formal HTML's standalone major-event timeline.
- Do not stage or commit application source: this repository contains pre-existing untracked source files. Stage only files explicitly created for documentation when a commit is requested.

---

### Task 1: Build Display Eligibility, Group Presentation, and Audit Destinations

**Files:**
- Modify: `jin10_market_report/tracking_report.py:41-50, 594-626, 852-906, 1878-1999, 3577-3583`
- Modify: `tests/test_tracking_chart.py:1-180`
- Modify: `tests/test_daily_requirements.py:1-115`

**Interfaces:**
- Consumes: scored group candidates from `_build_price_movement_review`, `audit["major_candidates"]`, `all_reaction_rows`, `_valid_jin10_display_url`, `_freshness_label`, existing noise predicates, and `_formal_event_key`.
- Produces: `FormalReportPresentation(panel_groups, unmatched_major_items, iran_update_items)` where `panel_groups` is a tuple of `dict[str, object]` keyed by the exact formal event key used by the K-line marker and `unmatched_major_items` is a tuple of display-ready major-event dictionaries.
- Produces: `_homepage_candidate_is_eligible(candidate: dict[str, object]) -> bool`, `_build_blue_panel_groups(review, timezone_name) -> list[dict[str, object]]`, and `_build_unmatched_major_panel_items(audit, reaction_rows, displayed_raw_ids, timezone_name) -> list[dict[str, str]]`.

- [ ] **Step 1: Write failing group-eligibility tests**

Add a test to `tests/test_daily_requirements.py` that constructs a movement group with five scored candidates:

```python
group = {
    "start_dt": start,
    "end_dt": start + timedelta(minutes=30),
    "group_id": "move_01",
    "direction": "下跌",
    "cumulative_pct": -0.0051,
    "max_30m_pct": -0.0051,
    "pct_5m": -0.0012,
    "pct_15m": -0.0015,
    "pct_30m": -0.0051,
    "pct_60m": -0.0021,
    "attribution_judgment": "主要驱动",
    "attribution_confidence": "高",
    "pricing_status": "已部分定价",
    "attribution_reason": "核心事件已满足归因要求。",
    "candidates": [core, lower_priority, invalid_link, stale, duplicate],
}
```

Make `core` and `lower_priority` direct, new, valid and nonduplicate, with different publication times and only `core` marked `主要驱动`. Make the other three fail respectively on URL, freshness and duplicate status. Assert the blue-panel group contains `core` and `lower_priority` in publication-time order, while `selected_news` remains only `[core]`.

Add a second failing test that supplies the 2026-07-20 20:10 Hu Sai item as a major candidate and as a lower-priority direct/new/valid candidate in its movement group. Assert it is present in the group output despite `attribution_judgment == "背景信息"` or low confidence.

- [ ] **Step 2: Run the focused eligibility tests to verify they fail**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_daily_requirements.DailyRequirementTests.test_blue_panel_keeps_all_display_eligible_candidates \
  tests.test_daily_requirements.DailyRequirementTests.test_blue_panel_keeps_houthi_ban_without_core_driver_status -v
```

Expected: FAIL because the presentation builder and display-eligibility functions do not exist.

- [ ] **Step 3: Add display-only presentation builders**

Extend the existing `typing` import with `Mapping` and `Sequence`, then replace the timeline-only presentation fields with these dataclass fields:

```python
@dataclass(frozen=True)
class FormalReportPresentation:
    panel_groups: tuple[dict[str, object], ...]
    unmatched_major_items: tuple[dict[str, str], ...]
    iran_update_items: tuple[dict[str, str], ...]
```

Implement the display predicate without consulting `attribution_judgment`, `attribution_confidence`, `tier`, or score threshold:

```python
def _homepage_candidate_is_eligible(candidate: dict[str, object]) -> bool:
    row = candidate["row"]
    title = str(_val(row, "title") or "")
    body = str(_val(row, "body") or _val(row, "detail") or "")
    return (
        bool(candidate.get("direct_supply_path"))
        and str(candidate.get("freshness") or "") == "新增事实"
        and bool(_valid_jin10_display_url(_val(row, "url")))
        and not bool(candidate.get("is_duplicate"))
        and not _is_institution_forecast_noise(row)
        and not _is_formal_report_noise(row)
        and not bool(candidate.get("price_quote_only"))
        and not bool(candidate.get("is_commentary"))
        and bool(title)
        and bool(direct_energy_supply_reason(title, body))
    )
```

In `_build_price_movement_review`, fetch the window's reaction rows once, build a news-id keyed map, and set each candidate's duplicate status from that map before building groups. Do not change the existing `selected_news` list. Add `group["homepage_news"]` as every eligible candidate, sorted by `(published_dt, -attribution_priority, raw_id)`.

Inside the presentation builder, calculate each group key by converting `group["start_dt"]` and `group["end_dt"]` to Beijing clocks and passing those two clocks plus the Beijing report-window start to `_formal_event_key(start_clock, end_clock, window_start_local)`. This must produce the identical key emitted by `_parse_formal_events` and `prepare_interactive_chart_markers`; do not derive a parallel key format.

Create per-item dictionaries with `id`, Beijing `time`, `title`, `summary`, `url`, `impact`, `price_status`, `relation`, `judgment`, and `confidence`. Use “同期相关信息” for non-core news unless relation is `价格先于新闻`, in which case use “价格先于新闻，不作该段归因”.

Build unmatched major items only when a major candidate passes the equivalent raw-item display predicate and its raw id is absent from every `homepage_news`; set `price_status` to `未匹配显著异动` and `impact` exactly to `暂未发现可直接匹配的Brent显著异动，需持续关注后续影响。`.

- [ ] **Step 4: Update Audit destination tests and implementation**

Add a focused test in `tests/test_tracking_chart.py` for `_forced_candidate_lines` with one eligible Hu Sai major candidate included in `homepage_destinations={"houthi": ["20:10–20:40"]}`. Assert the rendered audit line contains `首页展示：价格异动组 20:10–20:40` and does not contain `被更高优先级事件替代`.

Change `_forced_candidate_lines` to accept a `homepage_destinations: Mapping[str, Sequence[str]]` argument. Resolve statuses in this order:

```python
if raw_id in homepage_destinations:
    status = f"首页展示：{'；'.join(homepage_destinations[raw_id])}"
elif is_duplicate:
    status = f"未进入首页：重复报道，已并入 {duplicate_of or '同事件'}"
elif freshness != "新增事实":
    status = f"未进入首页：非新增事实（{freshness}）"
elif not valid_url:
    status = "未进入首页：原文链接无效"
elif not direct_supply:
    status = "未进入首页：无直接油市传导路径"
else:
    status = "未进入首页：明确噪音新闻"
```

Pass destinations derived from panel groups and unmatched major items through `_build_audit_report_lines`. In the rendered audit line, calculate `进入正式日报` from `raw_id in homepage_destinations` rather than from `selected_raw_ids` / `selected_rows`; this keeps a blue-panel-only item from being reported as absent. Do not use `is_in_daily` to describe homepage inclusion.

- [ ] **Step 5: Run focused tests to verify they pass**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_daily_requirements.DailyRequirementTests.test_blue_panel_keeps_all_display_eligible_candidates \
  tests.test_daily_requirements.DailyRequirementTests.test_blue_panel_keeps_houthi_ban_without_core_driver_status \
  tests.test_tracking_chart.TrackingChartTests.test_forced_candidate_audit_records_homepage_destination -v
```

Expected: PASS. The tests prove priority does not hide qualified group news and Audit no longer reports priority replacement as a homepage exclusion.

### Task 2: Render the Default All-Day Blue Panel and K-Line Focus State

**Files:**
- Modify: `jin10_market_report/tracking_report.py:3927-3985, 4115-4284`
- Modify: `tests/test_tracking_chart.py:431-570`

**Interfaces:**
- Consumes: `FormalReportPresentation.panel_groups` and `.unmatched_major_items` from Task 1, plus the parsed Markdown event metrics.
- Produces: `dashboard["panel_groups"]`, `dashboard["unmatched_major_items"]`, and a single `#related-news-panel` that renders both default and focused states.

- [ ] **Step 1: Write a failing HTML interaction test**

Replace `test_formal_html_renders_major_event_timeline_without_changing_price_review` with a test that passes this presentation fixture:

```python
FormalReportPresentation(
    panel_groups=(
        {
            "key": "event-20260720t2010-20260720t2040",
            "time_range": "20:10–20:40",
            "direction": "下跌",
            "cumulative": "-0.51%",
            "news": (houthi_item, hormuz_item),
        },
    ),
    unmatched_major_items=(insurance_item,),
    iran_update_items=(),
)
```

Assert that generated HTML contains all of the following:

```python
self.assertIn("全日重要新闻", rendered)
self.assertIn("胡塞武装宣布对沙特实施海上禁令", rendered)
self.assertIn("⚠ 重大事件", rendered)
self.assertIn("暂未发现可直接匹配的Brent显著异动，需持续关注后续影响。", rendered)
self.assertIn("查看全日重要新闻", rendered)
self.assertNotIn('id="major-event-timeline"', rendered)
self.assertNotIn("data-major-event-key", rendered)
self.assertNotIn("被更高优先级事件替代", rendered)
```

Parse the `brent-panel-data` JSON script and assert the first group contains both group news in chronological order, while unmatched events are stored only under `unmatched_major_items`.

- [ ] **Step 2: Run the focused HTML test to verify it fails**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_tracking_chart.TrackingChartTests.test_formal_html_blue_panel_defaults_to_all_day_news -v
```

Expected: FAIL because the formal renderer still emits `major-event-timeline` and only has marker-specific event data.

- [ ] **Step 3: Replace timeline rendering with one panel data source**

In `_parse_formal_dashboard`, carry the presentation values directly:

```python
"panel_groups": list(presentation.panel_groups) if presentation else [],
"unmatched_major_items": list(presentation.unmatched_major_items) if presentation else [],
```

Remove the `major_timeline_items` return field and remove the `#major-event-timeline` HTML, its CSS rules, and the `brent-major-event-data` script.

Serialize the new payload as:

```html
<script type="application/json" id="brent-panel-data">...</script>
```

Add JavaScript helpers with these behaviors:

```javascript
const renderAllDay = () => {
  clearActive();
  panel.replaceChildren();
  panel.appendChild(addText('h3', 'news-panel-title', '全日重要新闻'));
  panelGroups.forEach(renderGroup);
  renderUnmatchedMajorItems();
};

const showGroup = (key) => {
  const group = byKey.get(key);
  if (!group) return;
  clearActive();
  document.querySelectorAll(`[data-event-key="${key}"]`).forEach(node => node.classList.add('is-active'));
  panel.replaceChildren();
  panel.appendChild(buildAllDayButton());
  renderGroup(group);
};
```

`renderGroup` must render group price metrics and market interpretation, then every presentation news item with time, title, impact, price status and `查看金十原文 ↗`. It must use `textContent` for all news fields and only assign URLs that have already passed `_valid_jin10_display_url`.

Wire existing chart marker triggers to `showGroup(key)`. Unmatched major events have no significant-movement marker and therefore render only at the bottom of `renderAllDay`; do not create a separate timeline, band, or click target for them. When no presentation is supplied, preserve current Markdown-parsed event behavior as a fallback.

- [ ] **Step 4: Run focused HTML tests to verify they pass**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_tracking_chart.TrackingChartTests.test_formal_html_blue_panel_defaults_to_all_day_news \
  tests.test_tracking_chart.TrackingChartTests.test_formal_html_renders_compact_chart_dashboard \
  tests.test_tracking_chart.TrackingChartTests.test_formal_html_excludes_news_without_valid_original_link -v
```

Expected: PASS. The blue panel renders the complete default reading view, filters safely to a clicked price group, and retains link filtering.

### Task 3: Wire Report Generation, Run the Regression, and Verify Artifacts

**Files:**
- Modify: `jin10_market_report/tracking_report.py:3324-3583`
- Modify: `tests/test_tracking_chart.py:95-150`
- Modify: `tests/test_daily_requirements.py:1-180`
- Generated: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260721_0800.html`
- Generated: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260721_0800.md`
- Generated: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260721_0800.html`
- Generated: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260721_0800.md`

**Interfaces:**
- Consumes: Task 1 presentation builders and Task 2 renderer contract.
- Produces: `ReportBuildResult.presentation` that every `run-once`, `report`, and backfill call site already passes to `build_html_report`.

- [ ] **Step 1: Write the failing report-builder regression test**

Update `test_markdown_report_returns_major_event_presentation_for_html_renderer` to patch a review containing the Hu Sai 20:10 candidate and a group spanning 20:10–20:40. Assert:

```python
group = result.presentation.panel_groups[0]
self.assertEqual(group["news"][0]["title"], "胡塞武装宣布对沙特实施海上禁令")
self.assertEqual(result.presentation.unmatched_major_items, ())
self.assertNotIn("major_timeline_items", result.presentation.__dataclass_fields__)
```

- [ ] **Step 2: Run the report-builder regression to verify it fails**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_tracking_chart.TrackingChartTests.test_markdown_report_returns_complete_blue_panel_presentation -v
```

Expected: FAIL because `build_markdown_report` still constructs `major_timeline_items` instead of the new complete panel presentation.

- [ ] **Step 3: Wire the presentation into the report builder**

After `_build_price_movement_review` and before `_build_audit_report_lines`, build the panel presentation once. Pass its computed homepage destinations to Audit and assign the same `panel_groups` and `unmatched_major_items` to `FormalReportPresentation` after Markdown/Audit writing. Keep every existing `build_html_report(..., presentation=report_result.presentation)` call site unchanged.

Use a route helper with this output shape:

```python
def _homepage_destinations(
    panel_groups: Iterable[dict[str, object]],
    unmatched_major_items: Iterable[dict[str, str]],
) -> dict[str, tuple[str, ...]]:
    # raw id -> ("价格异动组 20:10–20:40",) or ("⚠ 重大事件",)
```

This keeps Audit and homepage on a single source of truth without persisting display flags.

- [ ] **Step 4: Run all unit tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -v
```

Expected: PASS with all legacy and new blue-panel tests green.

- [ ] **Step 5: Regenerate the 2026-07-21 report from the local database**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 run_jin10_tracker.py report \
  --date 2026-07-21 \
  --db-path data/jin10_tracker.sqlite \
  --output-dir outputs/tracker \
  --log-dir logs \
  --timezone Asia/Shanghai
```

Expected: exit code 0 and refreshed formal HTML/Markdown plus Audit Markdown/HTML for the 2026-07-21 08:00 cutoff.

- [ ] **Step 6: Validate the regenerated artifacts and the Hu Sai regression**

Run:

```bash
python3 -c 'from pathlib import Path; import json, re; p=Path("outputs/tracker/Jin10_Brent_Rolling_Reaction_20260721_0800.html"); t=p.read_text(encoding="utf-8"); data=json.loads(re.search(r"id=\"brent-panel-data\">(.*?)</script>", t, re.S).group(1)); assert "胡塞武装宣布对沙特实施海上禁令" in t; assert "全日重要新闻" in t; assert "查看全日重要新闻" in t; assert "⚠ 重大事件" in t; assert "id=\"major-event-timeline\"" not in t; assert "被更高优先级事件替代" not in t; assert any(item["title"] == "胡塞武装宣布对沙特实施海上禁令" for group in data["panel_groups"] for item in group["news"]); print(len(data["panel_groups"]))'
```

Expected: a positive group count, no standalone timeline, and the 20:10 Hu Sai article inside a blue-panel price group with its valid Jin10 URL.
