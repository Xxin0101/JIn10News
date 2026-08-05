# Brent Daily ISW, Trump Statements, and Top Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing 08:00 Brent rolling report so it renders every translated ISW `Key Takeaways` item, all relevant Trump statements from Jin10 topic 418, and a 2–5 item evidence-backed summary at the top, then generate and verify the 2026-08-05 report.

**Architecture:** Keep the existing Jin10 flash/Brent attribution pipeline unchanged. Make ISW and Trump topic 418 independent cached external inputs, pass both into the Markdown/Audit builder, and render the same complete collections in the formal HTML presentation. Build the top summary deterministically from the already-validated report model so it cannot introduce a new attribution claim.

**Tech Stack:** Python 3.12 standard library (`urllib`, `html.parser`, `json`, `datetime`, `difflib`, `pathlib`), SQLite, `unittest`, Markdown, generated HTML/SVG.

## Global Constraints

- The report window is `2026-08-04 08:00 <= time < 2026-08-05 08:00` in `Asia/Shanghai`.
- Brent attribution remains price-first and continues to use the 0.50% strong-window rule and 0.25% fallback rule from `SPEC.md`.
- Trump topic and ISW content must never change `selected_news`, movement groups, core drivers, Brent reaction labels, or formal attribution database fields.
- ISW collects only the official article's `Key Takeaways`; it does not collect or translate the later country sections.
- Every ISW Key Takeaway is preserved in source order and translated one-to-one; source, translated, Markdown, and HTML counts must match.
- Trump statements come only from Jin10 topic 418 and preserve every independent in-window statement about Iran, Hormuz, war/military action, oil/energy, or sanctions.
- Top summary length is 2–5 items, and every item maps to evidence in the report body.
- External-source failures remain visible but do not alter the Jin10 + Brent report status; failed inputs cannot be cited in the top summary.
- Existing Markdown strict-attribution and HTML full-display collections remain separate.
- No AI or LLM call is added to the five-minute fetch, database, rules, or price-calculation path.
- Do not use existing HTML, Markdown, SVG, or search snippets to backfill the SQLite fact tables.

---

## File Map

- Modify `jin10_market_report/external_update.py`: parse and translate the complete ISW Key Takeaways collection without supplemental body content or item limits.
- Create `jin10_market_report/trump_update.py`: fetch, filter, group, cache, and audit Jin10 topic 418 statements.
- Modify `jin10_market_report/tracking_report.py`: build the top summary, Trump section, complete ISW section, audit fields, and full HTML presentation.
- Modify `jin10_market_report/tracker.py`: fetch/log/pass the Trump cache in `run_once` and `backfill-day` flows.
- Modify `run_jin10_tracker.py`: expose `--retry-failed-trump` beside `--retry-failed-isw` for `report` and `backfill-day`.
- Modify `tests/test_daily_requirements.py`: ISW completeness and report-source audit regression coverage.
- Create `tests/test_trump_update.py`: Jin10 topic API pagination, time filtering, topic filtering, direct-statement filtering, grouping, and cache tests.
- Modify `tests/test_tracking_chart.py`: Markdown and formal HTML completeness, top-summary placement, and no-truncation tests.
- Modify `SPEC.md`: add the approved ISW, Trump, summary, cache, and acceptance rules.
- Modify `HANDOFF.md`: record the new report sections, commands, and 2026-08-05 delivery state.

---

### Task 1: Make ISW Key Takeaways Complete and One-to-One

**Files:**
- Modify: `tests/test_daily_requirements.py:1504-1593`
- Modify: `jin10_market_report/external_update.py:138-267`
- Modify: `jin10_market_report/external_update.py:293-489`

**Interfaces:**
- Consumes: official ISW article HTML and the existing `_request_text(url, timeout)` transport.
- Produces: `_article_key_takeaways(page: str) -> list[str]` and the public `fetch_latest_isw_update` result containing `source_key_takeaways`, `key_takeaways_zh`, `source_key_takeaways_count`, and `translated_key_takeaways_count`.

- [ ] **Step 1: Replace the supplement-oriented parser test with an eight-item completeness test**

Add this test fixture and assertions to `tests/test_daily_requirements.py`:

```python
def test_article_key_takeaways_preserves_every_item_and_stops_before_body(self) -> None:
    items = [f"Key takeaway {index} contains enough source text to remain a complete paragraph." for index in range(1, 9)]
    page = "\n".join(["Iran Update", "Key Takeaways", *items, "Iran", "Body paragraph must not be collected."])

    parsed = _article_key_takeaways(page)

    self.assertEqual(parsed, items)
    self.assertNotIn("Body paragraph must not be collected.", parsed)
```

Update imports to import `_article_key_takeaways`. Remove assertions that require `supplemental_zh` or body items.

- [ ] **Step 2: Add a cache/count test proving all eight items are translated**

```python
def test_isw_cache_translates_all_key_takeaways_one_to_one(self) -> None:
    cutoff = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
    source_items = [f"Source takeaway {index}." for index in range(1, 9)]
    translated_items = [f"中文要点{index}。" for index in range(1, 9)]
    source = {
        "fetch_method": "官网直连",
        "title": "Iran Update, August 4, 2026",
        "url": "https://understandingwar.org/research/middle-east/iran-update-august-4-2026/",
        "published_at_utc": "2026-08-04T22:30:00Z",
        "source_key_takeaways": source_items,
    }
    with tempfile.TemporaryDirectory() as temp_dir, (
        patch("jin10_market_report.external_update._fetch_from_site", return_value=source),
        patch("jin10_market_report.external_update._translate_bullets", return_value=translated_items),
    ):
        result = fetch_latest_isw_update(cutoff, cache_dir=Path(temp_dir))

    self.assertEqual(result["source_key_takeaways"], source_items)
    self.assertEqual(result["key_takeaways_zh"], translated_items)
    self.assertEqual(result["source_key_takeaways_count"], 8)
    self.assertEqual(result["translated_key_takeaways_count"], 8)
    self.assertNotIn("supplemental_zh", result)
```

- [ ] **Step 3: Add an all-or-nothing translation failure test**

```python
def test_isw_translation_count_mismatch_marks_update_failed(self) -> None:
    cutoff = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
    source = {
        "fetch_method": "官网直连",
        "title": "Iran Update, August 4, 2026",
        "url": "https://understandingwar.org/research/middle-east/iran-update-august-4-2026/",
        "published_at_utc": "2026-08-04T22:30:00Z",
        "source_key_takeaways": ["one", "two", "three"],
    }
    with tempfile.TemporaryDirectory() as temp_dir, (
        patch("jin10_market_report.external_update._fetch_from_site", return_value=source),
        patch("jin10_market_report.external_update._translate_bullets", return_value=["一", "二"]),
    ):
        result = fetch_latest_isw_update(cutoff, cache_dir=Path(temp_dir))

    self.assertEqual(result["status"], "failed")
    self.assertIn("数量不一致", result["failure_reason"])
    self.assertEqual(result["key_takeaways_zh"], [])
```

- [ ] **Step 4: Run the three focused tests and confirm they fail**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_daily_requirements.DailyRequirementTests.test_article_key_takeaways_preserves_every_item_and_stops_before_body \
  tests.test_daily_requirements.DailyRequirementTests.test_isw_cache_translates_all_key_takeaways_one_to_one \
  tests.test_daily_requirements.DailyRequirementTests.test_isw_translation_count_mismatch_marks_update_failed -v
```

Expected: failures because `_article_key_takeaways` and the new count contract do not exist and the old code truncates at six.

- [ ] **Step 5: Implement the unbounded Key Takeaways parser**

In `jin10_market_report/external_update.py`, add:

```python
ISW_BODY_HEADINGS = {
    "The United States", "Iran", "Iraq", "Lebanon", "Syria", "Yemen", "Gaza Strip"
}

def _article_key_takeaways(page: str) -> list[str]:
    lines = [line.strip() for line in _plain_html_text(page).splitlines() if line.strip()]
    start = next((index for index, line in enumerate(lines) if line == "Key Takeaways"), None)
    if start is None:
        return []
    collected: list[str] = []
    for line in lines[start + 1:]:
        if line in ISW_BODY_HEADINGS:
            break
        if line.startswith(("Download the PDF", "Information Cutoff", "The Critical Threats Project")):
            continue
        if len(line) >= 20 and line not in collected:
            collected.append(line)
    return collected
```

Make `_article_sections(page)` a compatibility wrapper returning `(_article_key_takeaways(page), [])`, and change `_fetch_from_site` to require at least one Key Takeaway and return the full collection as `source_key_takeaways` without slices.

- [ ] **Step 6: Remove every translation and cache slice**

Change `_translate_bullets` to:

```python
def _translate_bullets(bullets: list[str]) -> list[str]:
    return [_translate_to_chinese(item) for item in bullets]
```

In `_fetch_from_official_bluesky`, use the thread only to find the official article URL, fetch that page, call `_article_key_takeaways(page)`, and return those official article items. Do not use Bluesky posts as the final Key Takeaways collection.

- [ ] **Step 7: Enforce the one-to-one success contract in `fetch_latest_isw_update`**

Use this result shape:

```python
source_items = [str(item) for item in source.get("source_key_takeaways") or []]
translated = _translate_bullets(source_items)
if not source_items:
    raise ValueError("ISW官网未解析到Key Takeaways")
if len(translated) != len(source_items):
    raise ValueError(f"ISW Key Takeaways原文与翻译数量不一致：{len(source_items)} != {len(translated)}")
result.update({
    **source,
    "status": "success",
    "source_key_takeaways": source_items,
    "key_takeaways_zh": translated,
    "summary_zh": translated,
    "source_key_takeaways_count": len(source_items),
    "translated_key_takeaways_count": len(translated),
    "translation_method": "Google公开机器翻译端点",
})
```

Initialize failed results with empty `source_key_takeaways` and `key_takeaways_zh`, and do not emit `supplemental_zh` or `energy_significance_zh` in new cache files.

- [ ] **Step 8: Run focused and existing ISW tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_daily_requirements.DailyRequirementTests.test_article_key_takeaways_preserves_every_item_and_stops_before_body \
  tests.test_daily_requirements.DailyRequirementTests.test_isw_cache_translates_all_key_takeaways_one_to_one \
  tests.test_daily_requirements.DailyRequirementTests.test_isw_translation_count_mismatch_marks_update_failed \
  tests.test_daily_requirements.DailyRequirementTests.test_failed_isw_cache_can_be_retried_on_demand -v
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit the ISW change**

```bash
git add jin10_market_report/external_update.py tests/test_daily_requirements.py
git commit -m "feat: preserve all ISW key takeaways"
```

---

### Task 2: Add the Jin10 Trump Topic 418 Source

**Files:**
- Create: `jin10_market_report/trump_update.py`
- Create: `tests/test_trump_update.py`

**Interfaces:**
- Consumes: `https://reference-api.jin10.com/topic/getById?id=418&page=N&page_size=20` with headers `x-app-id: irINJPgCgrndSp0F` and `x-version: 1.0.1`.
- Produces: `fetch_trump_statements(window_start_utc: datetime, window_end_utc: datetime, cache_dir: Path = Path("data/external_updates"), timezone_name: str = "Asia/Shanghai", retry_failed_cache: bool = False) -> dict[str, object]`.
- Result statement entries contain `statement_id`, `published_at_utc`, `published_at_bj`, `text`, `topics`, and `source_items`.

- [ ] **Step 1: Write the window, direct-speech, topic, and pagination tests**

Create `tests/test_trump_update.py` with a two-page mocked API. Include these records:

```python
PAGE_ONE = {
    "status": 200,
    "data": {
        "page": 1,
        "page_size": 20,
        "total": 4,
        "list": [
            {"id": 1, "type": "news", "display_datetime": "2026-08-05 08:00:00", "title": "特朗普谈油价", "introduction": "特朗普表示油价必须下降。"},
            {"id": 2, "type": "video", "display_datetime": "2026-08-04 14:29:33", "title": "特朗普喊话石油巨头", "introduction": "特朗普要求埃克森美孚与雪佛龙降低零售油价。"},
            {"id": 3, "type": "news", "display_datetime": "2026-08-04 14:10:59", "title": "特朗普在伊朗战争中面临困境", "introduction": "白宫正在寻找退出路线。"},
        ],
    },
}
PAGE_TWO = {
    "status": 200,
    "data": {
        "page": 2,
        "page_size": 20,
        "total": 4,
        "list": [
            {"id": 4, "type": "news", "display_datetime": "2026-08-04 08:00:00", "title": "特朗普称美伊正在谈判", "introduction": "特朗普警告这是伊朗签署协议的最后机会。"},
            {"id": 5, "type": "news", "display_datetime": "2026-08-04 07:59:59", "title": "特朗普谈霍尔木兹", "introduction": "特朗普表示海峡将开放。"},
        ],
    },
}
```

Assert that ID 1 and ID 5 are outside the half-open window, ID 3 is excluded because it contains analysis rather than a Trump statement, IDs 2 and 4 remain, and the fetch stops after page 2.

- [ ] **Step 2: Write a grouping test that preserves all source rows**

```python
def test_duplicate_reports_group_but_preserve_every_source(self) -> None:
    rows = [
        normalized_item(10, "2026-08-04 11:34:54", "特朗普：这是伊朗的最后机会", "特朗普称美伊谈判正在进行。"),
        normalized_item(11, "2026-08-04 11:43:35", "特朗普：正在谈判", "特朗普称谈判已经开始，这是伊朗最后机会。"),
    ]
    statements = _group_statement_items(rows)
    self.assertEqual(len(statements), 1)
    self.assertEqual([item["id"] for item in statements[0]["source_items"]], [10, 11])
    self.assertIn("伊朗", statements[0]["topics"])
```

- [ ] **Step 3: Write cache and explicit retry tests**

Verify a successful cache is reused, a failed cache is reused by default, and `retry_failed_cache=True` calls the API again and overwrites the failed cache.

- [ ] **Step 4: Run the new test module and confirm it fails**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_trump_update -v
```

Expected: import failure because `jin10_market_report.trump_update` does not exist.

- [ ] **Step 5: Implement API transport, pagination, and normalized rows**

Create `jin10_market_report/trump_update.py` with these constants and public signature:

```python
TRUMP_TOPIC_ID = 418
TRUMP_TOPIC_URL = "https://xnews.jin10.com/topic/418"
TRUMP_TOPIC_API = "https://reference-api.jin10.com/topic/getById"
TRUMP_TOPIC_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "x-app-id": "irINJPgCgrndSp0F",
    "x-version": "1.0.1",
}

def fetch_trump_statements(
    window_start_utc: datetime,
    window_end_utc: datetime,
    cache_dir: Path = Path("data/external_updates"),
    timezone_name: str = "Asia/Shanghai",
    retry_failed_cache: bool = False,
) -> dict[str, object]:
    start = ensure_utc(window_start_utc)
    end = ensure_utc(window_end_utc)
    cache_path = _cache_path(cache_dir, end, timezone_name)
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("status") == "success" or not retry_failed_cache:
            cached["cache_hit"] = True
            cached["cache_path"] = str(cache_path)
            return cached
    try:
        fetched_items = _fetch_topic_items(start, end, timezone_name)
        normalized_items = [_normalize_item(item, timezone_name) for item in fetched_items]
        in_window_items = [
            item for item in normalized_items
            if start <= item["_published_dt_utc"] < end
        ]
        relevant_items = [item for item in in_window_items if _is_relevant_statement(item)]
        statements = _group_statement_items(relevant_items)
        result = _success_payload(
            start, end, fetched_items, in_window_items, relevant_items, statements, cache_path
        )
    except Exception as exc:
        result = _failure_payload(start, end, str(exc), cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
```

Implement `_request_topic_page(page: int, page_size: int = 20) -> dict[str, object]`. Parse `display_datetime` as `Asia/Shanghai`, convert it to UTC, include the start boundary, exclude the end boundary, and stop paging after a page's oldest timestamp is earlier than the window start.

`_normalize_item` keeps an internal aware datetime under `_published_dt_utc` for comparisons and also emits public ISO `published_at_utc` plus display `published_at_bj`. `_group_statement_items` and `_success_payload` must remove the private datetime before writing JSON.

The same file must define the helpers referenced by the public function with these exact signatures:

- `_cache_path(cache_dir: Path, window_end_utc: datetime, timezone_name: str) -> Path`
- `_fetch_topic_items(window_start_utc: datetime, window_end_utc: datetime, timezone_name: str) -> list[dict[str, object]]`
- `_normalize_item(item: dict[str, object], timezone_name: str) -> dict[str, object]`
- `_is_relevant_statement(item: dict[str, object]) -> bool`
- `_group_statement_items(items: list[dict[str, object]]) -> list[dict[str, object]]`
- `_success_payload(start: datetime, end: datetime, fetched_items: list[dict[str, object]], in_window_items: list[dict[str, object]], relevant_items: list[dict[str, object]], statements: list[dict[str, object]], cache_path: Path) -> dict[str, object]`
- `_failure_payload(start: datetime, end: datetime, reason: str, cache_path: Path) -> dict[str, object]`

Implement each helper in Steps 5–8; the signatures are the stable boundaries used by the tests.

- [ ] **Step 6: Implement direct-statement and subject filters**

Use explicit markers and topic groups:

```python
DIRECT_MARKERS = (
    "特朗普：", "特朗普称", "特朗普表示", "特朗普说", "特朗普宣布", "特朗普要求",
    "特朗普警告", "特朗普威胁", "特朗普发文", "特朗普下令", "特朗普公开批评",
    "特朗普坦言", "美国总统特朗普",
)
TOPIC_TERMS = {
    "伊朗": ("伊朗", "美伊", "德黑兰", "核谈判"),
    "霍尔木兹": ("霍尔木兹", "海峡"),
    "战争与军事行动": ("战争", "军事", "打击", "空袭", "导弹", "攻击"),
    "原油与能源": ("原油", "石油", "油价", "汽油", "能源", "油企", "埃克森", "雪佛龙"),
    "制裁": ("制裁", "豁免", "二级关税"),
}
```

The candidate text is `title + introduction + data.content`. Require at least one direct marker and one topic group. Build the original Jin10 URL from `detail_url` when present, otherwise use the video or topic detail URL according to item type.

- [ ] **Step 7: Implement traceable grouping**

Normalize whitespace and punctuation, compare candidate text with `difflib.SequenceMatcher`, and group only when similarity is at least `0.72` and topic sets overlap. Use the longest candidate text as the representative `text`; preserve every normalized source row in `source_items` sorted by timestamp. Distinct topic sets or low-similarity statements remain separate.

- [ ] **Step 8: Implement cache payload and count metadata**

Use `trump_statements_YYYYMMDD_0800.json`. Store:

```python
{
    "status": "success",
    "source": "金十数据特朗普专栏",
    "source_url": TRUMP_TOPIC_URL,
    "topic_id": 418,
    "window_start_utc": iso_utc(window_start_utc),
    "window_end_utc": iso_utc(window_end_utc),
    "raw_topic_item_count": raw_count,
    "in_window_item_count": in_window_count,
    "relevant_source_item_count": len(relevant_items),
    "statement_count": len(statements),
    "statements": statements,
}
```

On failure, write the same window/source metadata plus `status: failed`, `failure_reason`, and an empty `statements` list.

- [ ] **Step 9: Run the Trump source tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_trump_update -v
```

Expected: all tests pass.

- [ ] **Step 10: Commit the Trump source**

```bash
git add jin10_market_report/trump_update.py tests/test_trump_update.py
git commit -m "feat: add jin10 trump statement source"
```

---

### Task 3: Integrate Trump Fetching into Report, Backfill, and Retry Flows

**Files:**
- Modify: `run_jin10_tracker.py:43-59`
- Modify: `run_jin10_tracker.py:120-236`
- Modify: `jin10_market_report/tracker.py:1-34`
- Modify: `jin10_market_report/tracker.py:598-753`
- Modify: `jin10_market_report/tracker.py:759-925`
- Modify: `tests/test_trump_update.py`

**Interfaces:**
- Consumes: `fetch_trump_statements(window_start_utc, window_end_utc, cache_dir, timezone_name, retry_failed_cache)` from Task 2.
- Produces: `trump_update` passed to `build_markdown_report`, CLI flag `--retry-failed-trump`, and tracker result/log fields `trump_update_status` and `trump_update`.

- [ ] **Step 1: Add CLI parsing assertions**

Patch `sys.argv` and assert both commands accept the flag:

```python
with patch.object(sys, "argv", ["run_jin10_tracker.py", "report", "--date", "2026-08-05", "--retry-failed-trump"]):
    args = parse_args()
self.assertTrue(args.retry_failed_trump)

with patch.object(sys, "argv", ["run_jin10_tracker.py", "backfill-day", "--date", "2026-08-05", "--retry-failed-trump"]):
    args = parse_args()
self.assertTrue(args.retry_failed_trump)
```

- [ ] **Step 2: Run the CLI assertions and confirm failure**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_trump_update.TrumpUpdateTests.test_retry_flag_is_available_for_report_and_backfill -v
```

Expected: argparse rejects `--retry-failed-trump`.

- [ ] **Step 3: Add the CLI flag**

Add to `report_p` and `backfill_p`:

```python
p.add_argument(
    "--retry-failed-trump",
    action="store_true",
    help="忽略该截止时间已有的特朗普专栏失败缓存并重新抓取；成功缓存仍复用。",
)
```

Pass it to `run_backfill_day(store, fetch_config, filter_config, brent_config, tracker_config, logger, args.date, generate_report=not args.no_report, retry_failed_isw=args.retry_failed_isw, retry_failed_trump=args.retry_failed_trump)`.

- [ ] **Step 4: Fetch and log Trump statements in the `report` command**

Import `fetch_trump_statements`, call it with `day_start`, `day_end`, the same external cache directory, timezone, and retry flag, then pass `trump_update=trump_update` into `build_markdown_report`. Log status, statement count, relevant source count, and failure reason.

- [ ] **Step 5: Integrate the tracker entry points**

Import the Trump fetcher in `tracker.py`. Extend:

```python
def run_backfill_day(
    store: TrackerStore,
    fetch_config: FetchConfig,
    filter_config: FilterConfig,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    logger: logging.Logger,
    day_label: str,
    generate_report: bool = True,
    retry_failed_isw: bool = False,
    retry_failed_trump: bool = False,
) -> dict[str, object]:
```

When `generate_report` is true, fetch both external inputs, pass both to `build_markdown_report`, and return both payloads and statuses. Apply the same fetch/pass/log behavior in `run_once`. A Trump failure must log a warning and continue report generation.

- [ ] **Step 6: Add mocked integration assertions**

Patch both fetchers and `build_markdown_report`, execute the backfill/report integration path, and assert the builder receives the exact `trump_update` object without inserting it into `TrackerStore`.

- [ ] **Step 7: Run integration and source tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_trump_update -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit integration**

```bash
git add run_jin10_tracker.py jin10_market_report/tracker.py tests/test_trump_update.py
git commit -m "feat: integrate trump statements into report runs"
```

---

### Task 4: Build the Top Summary, Trump Section, Complete ISW Section, and Audit

**Files:**
- Modify: `jin10_market_report/tracking_report.py:45-60`
- Modify: `jin10_market_report/tracking_report.py:4641-4707`
- Modify: `jin10_market_report/tracking_report.py:4710-4927`
- Modify: `jin10_market_report/tracking_report.py:4930-5154`
- Modify: `tests/test_tracking_chart.py:50-139`
- Modify: `tests/test_daily_requirements.py`

**Interfaces:**
- Consumes: complete `external_update`, `trump_update`, `brent_summary`, `movement_review`, `homepage_presentation`, `audit`, and final report status.
- Produces: `_top_summary_items(brent_summary, movement_review, homepage_presentation, trump_update, external_update, report_status, audit) -> list[str]`, `_trump_statement_lines(trump_update) -> list[str]`, complete `_tracking_outlook_lines(external_update)`, and Audit source-integrity sections.

- [ ] **Step 1: Write a Markdown layout and completeness test**

Use eight ISW translations and two Trump statements. Assert:

```python
self.assertEqual(markdown.count("中文Key Takeaway"), 8)
self.assertIn("## 特朗普相关言论", markdown)
self.assertIn("## ISW Iran Update", markdown)
self.assertNotIn("**重要事实：**", markdown)
self.assertNotIn("**对原油供应", markdown)
self.assertLess(markdown.index("## 今日重点（Executive Summary）"), markdown.index("统计窗口："))
summary_section = markdown.split("## 今日重点（Executive Summary）", 1)[1].split("统计窗口：", 1)[0]
self.assertGreaterEqual(summary_section.count("- "), 2)
self.assertLessEqual(summary_section.count("- "), 5)
```

Also assert the Trump section precedes the ISW section and each statement lists time, topic labels, and every Jin10 source link.

- [ ] **Step 2: Write source-integrity Audit assertions**

Assert the Audit contains:

```text
ISW Key Takeaways原文数量：8
ISW Key Takeaways翻译数量：8
ISW Key Takeaways正文渲染数量：8
特朗普专栏窗口内条目数量：4
特朗普相关来源条目数量：3
特朗普独立表态数量：2
```

Also assert every grouped Trump source ID and representative statement ID appear in the Audit.

- [ ] **Step 3: Run the new Markdown/Audit tests and confirm failure**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest \
  tests.test_tracking_chart.TrackingChartTests.test_report_markdown_has_top_summary_trump_and_complete_isw \
  tests.test_daily_requirements.DailyRequirementTests.test_external_source_integrity_audit_counts -v
```

Expected: missing arguments, missing sections, truncated ISW content, and missing Audit counts.

- [ ] **Step 4: Extend the presentation data contract**

Change `FormalReportPresentation` to include defaults after the existing required fields:

```python
top_summary_items: tuple[str, ...] = ()
trump_items: tuple[dict[str, object], ...] = ()
```

Add `trump_update: dict[str, object] | None = None` after `external_update` in the `build_markdown_report` signature and, when no payload is supplied, fetch it for `day_start/day_end` into the existing external cache directory.

- [ ] **Step 5: Implement complete ISW Markdown output**

Change `_tracking_outlook_lines` so a successful section contains metadata plus only:

```markdown
## ISW Iran Update

- 更新时间：纽约时间 2026-08-04 18:30 / 北京时间 2026-08-05 06:30
- 标题：Iran Update, August 4, 2026
- 原文链接：https://understandingwar.org/research/middle-east/iran-update-august-4-2026/

**Key Takeaways：**

1. first translation
2. second translation
8. eighth translation
```

Enumerate `key_takeaways_zh` without slicing. After the list, render `## 今日关注什么`; use the first takeaway only as a monitoring prompt, not as attribution.

- [ ] **Step 6: Implement Trump Markdown output**

Add:

```python
def _trump_statement_lines(trump_update: dict[str, object]) -> list[str]:
    lines = ["## 特朗普相关言论", ""]
    if trump_update.get("status") != "success":
        return lines + [f"- 金十特朗普专栏抓取失败：{trump_update.get('failure_reason') or '未知错误'}", ""]
    statements = list(trump_update.get("statements") or [])
    if not statements:
        return lines + ["- 本窗口金十特朗普专栏未发现符合主题条件的特朗普本人表态。", ""]
    for statement in statements:
        topics = "、".join(str(item) for item in statement.get("topics") or [])
        lines.append(f"### {statement.get('published_at_bj')}｜{topics}")
        lines.append("")
        lines.append(str(statement.get("text") or ""))
        lines.append("")
        for source in statement.get("source_items") or []:
            lines.append(f"- 金十来源：[{source.get('title')}]({source.get('url')})")
        lines.append("")
    return lines
```

Sort statements by `published_at_utc` ascending before rendering.

Add `_trump_dashboard_items(trump_update: dict[str, object]) -> list[dict[str, object]]`. It returns one dashboard item per statement with `statement_id`, `published_at_bj`, `text`, `topics`, and the complete `source_items` list; on failure or an empty success it returns one status item with no fabricated statement.

- [ ] **Step 7: Implement the deterministic 2–5 item summary**

Add `_top_summary_items` with this exact evidence priority:

1. Price line from `brent_summary`, including high/low when available or the data-status caveat when not.
2. Strongest formally attributed movement; otherwise the count of unexplained significant movement groups.
3. Highest-priority unmatched/direct major homepage item when present.
4. Highest-priority Trump statement when the Trump payload succeeded.
5. First ISW Key Takeaway when the ISW payload succeeded and counts match.

Deduplicate normalized text, cap at five, and add a second fallback line about report coverage/status when fewer than two items remain. Prefix external items with `特朗普表态：` and `ISW研判：`.

Use this exact signature:

```python
def _top_summary_items(
    brent_summary: Mapping[str, str],
    movement_review: Mapping[str, object],
    homepage_presentation: FormalReportPresentation,
    trump_update: Mapping[str, object],
    external_update: Mapping[str, object],
    report_status: str,
    audit: Mapping[str, object],
) -> list[str]:
```

- [ ] **Step 8: Place the summary immediately after the title**

Build `top_summary_items` before constructing `lines`, then start:

```python
lines = [
    f"# {_report_title(final_status)}（{title_label}）",
    "",
    "## 今日重点（Executive Summary）",
    "",
    *(f"- {item}" for item in top_summary_items),
    "",
    f"统计窗口：{start_local.strftime('%Y-%m-%d %H:%M')} 至 {end_local.strftime('%Y-%m-%d %H:%M')}（Asia/Shanghai）",
    f"数据来源：金十数据快讯｜{_source_label(brent)}｜金十数据特朗普专栏｜ISW Iran Update（仅用于后续监测）",
    "",
]
```

Insert `_trump_statement_lines(trump_update)` after the market-driver section and before `_tracking_outlook_lines(external_update)`.

- [ ] **Step 9: Add source-integrity Audit output**

Add `trump_update: dict[str, object] | None = None` after `external_update` in `_build_audit_report_lines`, then add `_external_source_integrity_lines(external_update, trump_update)`. Report the exact source/translation/render counts for ISW and raw/in-window/relevant/grouped counts for Trump. For each Trump statement, list its statement ID, topics, representative text, and every source ID/time/URL.

Replace the old `3 <= summary_count <= 6` ISW success check in `_new_requirement_audit_lines` with equality between source and translated counts and a positive count.

- [ ] **Step 10: Populate the presentation contract**

Return:

```python
presentation = FormalReportPresentation(
    panel_groups=panel_groups,
    unmatched_major_items=unmatched_major_items,
    iran_update_items=tuple(_isw_dashboard_items(external_update)),
    top_summary_items=tuple(top_summary_items),
    trump_items=tuple(_trump_dashboard_items(trump_update)),
    all_day_items=homepage_presentation.all_day_items,
    display_summary=homepage_presentation.display_summary,
    event_groups=homepage_presentation.event_groups,
    homepage_representations=homepage_presentation.homepage_representations,
)
```

Do not slice ISW or Trump collections in their dashboard helpers.

- [ ] **Step 11: Run the Markdown and Audit tests**

Run the named new tests plus the existing ISW success/failure tests. Expected: all pass.

- [ ] **Step 12: Commit report-model changes**

```bash
git add jin10_market_report/tracking_report.py tests/test_tracking_chart.py tests/test_daily_requirements.py
git commit -m "feat: add report summary and external statement sections"
```

---

### Task 5: Render Complete Summary, Trump, and ISW Collections in Formal HTML

**Files:**
- Modify: `jin10_market_report/tracking_report.py:5245-5269`
- Modify: `jin10_market_report/tracking_report.py:5380-5684`
- Modify: `jin10_market_report/tracking_report.py:5903-5978`
- Modify: `tests/test_tracking_chart.py:733-1155`

**Interfaces:**
- Consumes: `FormalReportPresentation.top_summary_items`, `.trump_items`, `.iran_update_items`, or their full Markdown fallbacks.
- Produces: formal HTML with one visible top summary, a complete Trump card, a complete ISW card, and the existing tracking card.

- [ ] **Step 1: Write an HTML no-truncation test**

Build a presentation with five summary items, nine Trump items, and eight ISW Key Takeaways plus one ISW metadata item. Assert:

```python
self.assertEqual(rendered.count('data-summary-item="true"'), 5)
self.assertEqual(rendered.count('data-trump-statement="true"'), 9)
self.assertEqual(rendered.count('data-isw-key-takeaway="true"'), 8)
self.assertLess(rendered.index("今日重点"), rendered.index("统计窗口"))
self.assertLess(rendered.index("特朗普相关言论"), rendered.index("ISW Iran Update"))
```

Assert all supplied source URLs are present and have `target="_blank" rel="noopener noreferrer"`.

- [ ] **Step 2: Run the new HTML test and confirm failure**

Expected: the presentation fields do not exist and the current renderer slices ISW at seven.

- [ ] **Step 3: Parse the new Markdown sections without truncation**

In `_parse_formal_dashboard`, read `今日重点（Executive Summary）` and `特朗普相关言论` as explicit sections. Add both headings to the stop lists used by movement, ISW, and watch parsers. Remove `[:7]` from `_isw_markdown_dashboard_items` and `_isw_dashboard_items`.

- [ ] **Step 4: Render the top summary before metadata**

In `_formal_dashboard_body`, immediately after `<h1>` render:

```python
lines.append('<section class="executive-summary" aria-label="今日重点"><h2>今日重点</h2><ul>')
for item in dashboard.get("top_summary_items") or []:
    lines.append(f'<li data-summary-item="true">{_inline_markdown_html(str(item))}</li>')
lines.append("</ul></section>")
```

Then render metadata, warnings, market body, metrics, chart, and the blue panel in the existing order.

- [ ] **Step 5: Render every Trump statement and source**

Add a full-width `trump-card` before the two-card ISW/watch grid. Render every item in `dashboard["trump_items"]` without slicing. Each statement wrapper gets `data-trump-statement="true"`; render topic labels, time, text, and every safe Jin10 source link.

- [ ] **Step 6: Render every ISW Key Takeaway**

Remove `[:7]` from the ISW card loop. Mark only actual takeaway items with `data-isw-key-takeaway="true"`; render the publication metadata item without that marker. Keep all links safe and external.

- [ ] **Step 7: Add responsive styles**

Add styles for `.executive-summary`, `.trump-card`, `.statement-meta`, and `.topic-chip`. The Trump card remains full-width at desktop and mobile; the existing ISW/watch pair may remain a two-column grid above 820 px and stacks below 820 px. Do not reduce body font below the existing report size.

- [ ] **Step 8: Run formal HTML and existing interaction tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest tests.test_tracking_chart -v
```

Expected: all tests pass, including blue-panel interaction, link safety, no old major-event timeline, and the new collection counts.

- [ ] **Step 9: Commit the HTML change**

```bash
git add jin10_market_report/tracking_report.py tests/test_tracking_chart.py
git commit -m "feat: render complete report source collections"
```

---

### Task 6: Update Operating Documentation and Run the Full Regression Suite

**Files:**
- Modify: `SPEC.md`
- Modify: `HANDOFF.md`
- Test: `tests/`

**Interfaces:**
- Consumes: completed behavior from Tasks 1–5.
- Produces: durable operating rules and a green full regression suite before any live backfill.

- [ ] **Step 1: Update `SPEC.md`**

Add:

- Jin10 Trump topic 418 as an external supplemental source that never participates in price attribution.
- The exact half-open 08:00 window, relevant theme list, direct-statement requirement, no item cap, and traceable duplicate grouping.
- ISW Key Takeaways-only, full-order, one-to-one translation and count equality.
- Top `今日重点（Executive Summary）` with 2–5 evidence-mapped items.
- Cache filenames and explicit retry flags.
- Audit and HTML no-truncation acceptance checks.

- [ ] **Step 2: Update `HANDOFF.md`**

Record the new modules, report order, retry command, external failure behavior, and the exact files expected for cutoff `20260805 08:00`. Do not mark the report delivered until Task 7 verification succeeds.

- [ ] **Step 3: Run formatting and placeholder scans**

Run:

```bash
git diff --check
rg -n "T[B]D|T[O]DO|implement [l]ater|fill in [d]etails" \
  jin10_market_report run_jin10_tracker.py tests SPEC.md HANDOFF.md
```

Expected: `git diff --check` exits 0 and the placeholder scan finds no newly introduced placeholders.

- [ ] **Step 4: Run the full test suite**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -v
```

Expected: every existing and new test passes; no test is skipped due to the new source modules.

- [ ] **Step 5: Commit documentation and regression state**

```bash
git add SPEC.md HANDOFF.md
git commit -m "docs: define daily report source completeness rules"
```

---

### Task 7: Backfill, Generate, and Verify the 2026-08-05 Daily Report

**Files:**
- Modify runtime data: `data/jin10_tracker.sqlite`
- Create/update cache: `data/external_updates/isw_iran_update_20260805_0800.json`
- Create/update cache: `data/external_updates/trump_statements_20260805_0800.json`
- Create output: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800*.md`
- Create output: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800*.html`
- Create output: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260805_0800*_brent_24h.svg`
- Create output: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260805_0800*.md`
- Create output: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260805_0800*.html`
- Create runtime log: `logs/jin10_report_tracker_*.log`

**Interfaces:**
- Consumes: public Jin10 flash API, Yahoo Finance `BZ=F` 5-minute chart API, Jin10 topic 418 API, and ISW official source.
- Produces: the user-facing 8.4–8.5 report, Audit, SVG, caches, database rows, and verification evidence.

- [ ] **Step 1: Confirm the report can be finalized after the price protection window**

Check Beijing time. The final backfill should run no earlier than `2026-08-05 09:05 Asia/Shanghai`, because the report requires price protection through the cutoff plus 65 minutes. If earlier, finish code/tests first and run only a non-final diagnostic fetch.

- [ ] **Step 2: Record the pre-backfill database watermark**

Run:

```bash
sqlite3 -readonly data/jin10_tracker.sqlite \
  "select 'raw_news', count(*), min(published_at_utc), max(published_at_utc) from raw_news; \
   select 'news', count(*), min(published_at_utc), max(published_at_utc) from news; \
   select 'brent_klines', count(*), min(ts_utc), max(ts_utc) from brent_klines;"
```

Save the output in the working notes; do not edit old report artifacts.

- [ ] **Step 3: Run the live backfill with explicit external-source retries**

Run:

```bash
python3 run_jin10_tracker.py backfill-day \
  --date 2026-08-05 \
  --retry-failed-isw \
  --retry-failed-trump
```

Expected: the command attempts Jin10, Brent, topic 418, and ISW independently and generates report/Audit artifacts even if a supplemental source fails.

- [ ] **Step 4: Identify the actual report status from generated content**

List only the target artifacts:

```bash
find outputs/tracker -maxdepth 1 -type f -name '*20260805_0800*' -print | sort
```

Read the top of the main Markdown and the Audit automation/data-integrity sections. Do not infer status solely from `_PARTIAL` or `_FAILED` files left by an earlier attempt.

- [ ] **Step 5: Verify source and rendered counts programmatically**

Run a read-only Python assertion script that:

1. Loads both 20260805 cache JSON files.
2. Selects the newest main Markdown/HTML pair matching the actual status.
3. Asserts ISW `source_key_takeaways_count == translated_key_takeaways_count`.
4. Counts numbered Key Takeaways in Markdown and `data-isw-key-takeaway="true"` in HTML and asserts both equal the cache count.
5. Asserts Trump Markdown/HTML statement counts equal `statement_count`.
6. Asserts every Trump source URL is present in both the main report or its grouped statement representation and the Audit.
7. Asserts the top summary has between two and five items in both Markdown and HTML.

Expected: the script exits 0 and prints the four matching counts.

- [ ] **Step 6: Verify database and report-window coverage**

Run SQLite queries for the half-open UTC window `2026-08-04T00:00:00Z <= time < 2026-08-05T00:00:00Z` and the Brent protection interval. Compare raw-news segment counts, price boundaries, `latest_news_plus60_covered`, and price-missing counts with the Audit and last run log. Any disagreement is an acceptance failure.

- [ ] **Step 7: Verify SVG, safe links, and removed legacy UI**

Run:

```bash
python3 -c "import pathlib,xml.etree.ElementTree as ET; p=max(pathlib.Path('outputs/tracker').glob('Jin10_Brent_Rolling_Reaction_20260805_0800*_brent_24h.svg'), key=lambda x:x.stat().st_mtime); ET.parse(p); print(p)"
rg -n "major-event-timeline|data-major-event-key|brent-major-event-data|javascript:" \
  outputs/tracker/*20260805_0800*.html
```

Expected: SVG parses; the HTML scan returns no legacy timeline or unsafe JavaScript URLs.

- [ ] **Step 8: Perform final rendered-report review**

Open the generated HTML in the local browser and verify at desktop and narrow width:

- `今日重点` is the first visible section after the title and has 2–5 items.
- Brent metrics, chart, blue panel, all-day items, and interaction still work.
- Trump statements are complete, chronological, readable, and linked only to Jin10.
- ISW contains every translated Key Takeaway and no later country-section body text.
- Audit link resolves to the same cutoff and actual status.
- No horizontal overflow or hidden/truncated external-source item is present.

- [ ] **Step 9: Re-run the full suite after the live data path**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jin10_pycache python3 -m unittest discover -s tests -v
```

Expected: all tests still pass after cache and output generation.

- [ ] **Step 10: Final handoff**

Report the actual status (`FORMAL`, `PARTIAL`, or `FAILED`), the five target artifact paths, the ISW source/translation/render count, the Trump relevant-source/independent-statement count, the summary item count, full test result, and any disclosed data gap. Do not describe a partial artifact as a complete formal report.
