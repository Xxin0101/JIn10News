# Brent Report First-Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将正式 Brent 日报 HTML 精简为 K 线交互单页，同时保持正式 Markdown、Audit、抓取筛选和文件命名不变。

**Architecture:** 正式 HTML 渲染器读取现有 Markdown，解析已入选事件为只读展示数据，并把稳定事件键注入内嵌 SVG。Audit 继续使用原有通用 Markdown-to-HTML 渲染路径；不新增数据库字段、数据文件或筛选逻辑。

**Tech Stack:** Python 3 标准库、现有 Markdown/HTML 生成器、内嵌 SVG、原生 JavaScript、`unittest`。

## Global Constraints

- 不修改新闻抓取和筛选逻辑、Brent 异动识别、0.50%/0.25% 阈值、供给端硬门槛、数据库、Audit 和文件命名。
- 正式 Markdown 与 Audit 文件内容保持原样。
- 事件映射优先使用稳定事件 ID；无 ID 时使用规范化北京时间起止时间，不依赖页面输出顺序。
- 每条展示新闻必须有独立有效 HTTP(S) 金十原文链接。
- 点击只更新顶部蓝框，不滚动、不弹窗。
- 页面在桌面端和移动端可用。

---

### Task 1: Add compact-dashboard regression tests

**Files:**
- Modify: `tests/test_tracking_chart.py`

**Interfaces:**
- Consumes: `build_html_report(markdown_path: Path, output_dir: Path) -> Path`
- Produces: 回归断言，固定正式 HTML 的结构、链接、事件键、交互与隐藏长内容行为。

- [ ] **Step 1: Write the failing compact-page test**

新增 `test_formal_html_renders_compact_chart_dashboard`，使用包含一个三新闻已解释事件和一个未解释事件的正式 Markdown fixture，并断言：

```python
self.assertLess(rendered.index('id="related-news-panel"'), rendered.index('class="chart-figure"'))
self.assertIn('id="today-market-card"', rendered)
self.assertIn('id="tracking-card"', rendered)
self.assertNotIn('<h2>本窗口Brent价格异动复盘</h2>', rendered)
self.assertNotIn('<h2>价格异动但新闻解释不足</h2>', rendered)
self.assertNotIn('<h2>未充分定价的重大风险</h2>', rendered)
self.assertEqual(rendered.count('查看金十原文 ↗'), 3)
self.assertIn('data-event-key="event-20260714t1335-20260714t1450"', rendered)
self.assertIn('class="unmatched-band-hit"', rendered)
self.assertNotIn('scrollIntoView', rendered)
self.assertNotIn('window.open', rendered)
self.assertIn('展开全部', rendered)
self.assertIn('@media(max-width:820px)', rendered)
```

- [ ] **Step 2: Write the failing invalid-link test**

新增 `test_formal_html_excludes_news_without_valid_original_link`，fixture 同时包含有效 `https://flash.jin10.com/detail/1` 和无效 `not-a-url`，断言蓝框事件 JSON 和渲染模板只包含有效新闻，且不出现“原文链接暂无”。

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/jin10_pycache python3 -m unittest tests.test_tracking_chart.TrackingChartTests.test_formal_html_renders_compact_chart_dashboard tests.test_tracking_chart.TrackingChartTests.test_formal_html_excludes_news_without_valid_original_link -v
```

Expected: FAIL，因为现有蓝框位于图表下方、仍渲染长内容、灰区不可点击并调用 `scrollIntoView`。

### Task 2: Implement the formal HTML dashboard renderer

**Files:**
- Modify: `jin10_market_report/tracking_report.py`

**Interfaces:**
- Produces: `_formal_event_key(start_clock: str, end_clock: str, window_start: datetime) -> str`
- Produces: `_parse_formal_dashboard(text: str) -> dict[str, object]`
- Produces: `_enrich_chart_svg(svg: str, dashboard: dict[str, object]) -> tuple[str, dict[str, dict[str, str]]]`
- Consumes: 原有正式 Markdown 文本和 SVG 的 `data-news` 属性。

- [ ] **Step 1: Add stable event-key and parser helpers**

实现事件键格式：

```python
event-YYYYMMDDtHHMM-YYYYMMDDtHHMM
```

北京时间小于统计窗口起始时刻的事件归入下一自然日；跨午夜结束时间归入下一日。解析结果包含 `key`、时间段、累计涨跌幅、最大30分钟涨跌幅、5/15/30/60分钟反应、全部已入选新闻、市场解读、归因和置信度。无有效 HTTP(S) 且域名不属于 `jin10.com` 的新闻从正式 HTML 展示数据中排除。

- [ ] **Step 2: Enrich the inline SVG without changing the SVG file**

读取所有蓝点 `data-news` URL，通过 URL 与事件已入选新闻集合匹配后注入 `data-event-key`。读取浅灰区间 `<title>` 的起止时间，生成相同稳定键，并在 K 线之后、蓝点之前加入透明的 `unmatched-band-hit` 点击层，设置 `role="button"`、`tabindex="0"`、`data-event-key` 和 `aria-label`。不使用节点顺序作为正常映射条件。

- [ ] **Step 3: Render only the compact formal-page sections**

正式 HTML 分支依次输出：标题/窗口/来源、四指标卡、默认蓝框、图表、最多三条市场主线、最多三条跟踪事项、Audit 链接。Audit 分支保持原循环不变。

蓝框默认文案固定为：

```text
点击下方K线图中的蓝色新闻时间点，查看该时段的相关新闻与市场解读。
```

事件 JSON 用 `<script type="application/json" id="brent-event-data">` 内嵌，所有 `<` 转义为 `\u003c`。

- [ ] **Step 4: Implement no-scroll, no-modal interaction**

原生 JavaScript 按 `data-event-key` 更新 `#related-news-panel`，移除其他 `.is-active` 后高亮当前蓝点或灰区。每条新闻标题和“查看金十原文 ↗”均使用同一 URL、`target="_blank"` 与 `rel="noopener noreferrer"`。超过两条时隐藏其余条目并提供“展开全部/收起”。脚本不得包含 `scrollIntoView`、`alert`、`dialog` 或 `window.open`。

- [ ] **Step 5: Run targeted tests and verify GREEN**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/jin10_pycache python3 -m unittest tests.test_tracking_chart -v
```

Expected: 新增和原有图表/HTML 测试全部 PASS。

### Task 3: Rebuild and verify the 20260715 sample

**Files:**
- Regenerate: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260715_0800.html`
- Must not change: `outputs/tracker/Jin10_Brent_Rolling_Reaction_20260715_0800.md`
- Must not change: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260715_0800.md`
- Must not change: `outputs/tracker/Jin10_Brent_Rolling_Audit_20260715_0800.html`

**Interfaces:**
- Consumes: 现有正式 Markdown 和 SVG。
- Produces: 同名精简正式 HTML 样本。

- [ ] **Step 1: Record immutable-file hashes**

Run:

```bash
shasum -a 256 outputs/tracker/Jin10_Brent_Rolling_Reaction_20260715_0800.md outputs/tracker/Jin10_Brent_Rolling_Audit_20260715_0800.md outputs/tracker/Jin10_Brent_Rolling_Audit_20260715_0800.html
```

- [ ] **Step 2: Rebuild only the formal HTML**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/jin10_pycache python3 -c "from pathlib import Path; from jin10_market_report.tracking_report import build_html_report; p=Path('outputs/tracker/Jin10_Brent_Rolling_Reaction_20260715_0800.md'); build_html_report(p, p.parent)"
```

- [ ] **Step 3: Verify hashes and HTML requirements**

重新运行相同 `shasum`，要求三个不可变文件哈希逐字一致。解析正式 HTML，确认蓝框在图表前、长复盘标题不存在、所有事件键唯一、所有展示新闻链接是 HTTP(S) 金十链接、灰区命中层数量等于未解释事件数、脚本不含滚动或弹窗调用。

- [ ] **Step 4: Run the complete test suite**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/jin10_pycache python3 -m unittest discover -s tests -v
```

Expected: 全部测试 PASS，0 failures，0 errors。
