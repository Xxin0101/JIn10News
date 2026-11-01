from __future__ import annotations

import html
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jin10_market_report.tracking_chart import (
    ChartNewsItem,
    ChartNewsMarker,
    ThirtyMinuteCandle,
    UnmatchedMovement,
    aggregate_30m_klines,
    prepare_interactive_chart_markers,
    render_brent_24h_svg,
)
from jin10_market_report.tracking_report import (
    FormalReportPresentation,
    ReportBuildResult,
    _forced_candidate_lines,
    _enrich_formal_chart_svg,
    _homepage_event_group_audit_lines,
    _isw_dashboard_items,
    _night_audit_lines,
    _build_blue_panel_groups,
    _price_movement_review_lines,
    _raw_inventory_lines,
    _segment_coverage_lines,
    _tracking_outlook_lines,
    _top_summary_items,
    _top_level_warning,
    _trump_dashboard_items,
    _unexplained_price_movement_lines,
    _audit_title,
    build_html_report,
    build_markdown_report,
)


BJ = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


class TrackingChartTests(unittest.TestCase):
    def _render_outlook_html(
        self,
        directory: str,
        outlook_lines: list[str],
    ) -> tuple[str, str]:
        output_dir = Path(directory)
        markdown = output_dir / "Jin10_Brent_Rolling_Reaction_outlook.md"
        svg = output_dir / "Jin10_Brent_Rolling_Reaction_outlook_brent_24h.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>',
            encoding="utf-8",
        )
        markdown_text = (
            "# 日报\n\n"
            "统计窗口：2026-07-23 08:00 至 2026-07-24 08:00（Asia/Shanghai）\n"
            "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
            "## 本窗口Brent表现\n\n"
            "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
            "| ---: | ---: | ---: | ---: |\n"
            "| 100.34 | +15.11% | 102.01 | 86.06 |\n\n"
            f"![chart]({svg.name})\n\n"
            "## 市场主要驱动与下一时段判断\n\n"
            "### 本窗口市场主线\n\n"
            "极端波动需人工判断。\n\n"
            + "\n".join(outlook_lines)
            + "\n[查看完整 Audit →](Jin10_Brent_Rolling_Audit_outlook.html)\n"
        )
        markdown.write_text(markdown_text, encoding="utf-8")
        rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")
        return markdown_text, rendered

    def test_formal_html_reads_generated_range_headers_into_brent_metric_cards(self) -> None:
        """Changing the generated table labels must not turn real Brent metrics into N/A."""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_metrics.md"
            markdown.write_text(
                "# 日报\n\n"
                "## 今日重点（Executive Summary）\n\n"
                "- 本窗口摘要。\n\n"
                "## 今日 Brent 价格预测\n\n"
                "- 预测日期：2026-09-04\n\n"
                "统计窗口：2026-09-03 08:00 至 2026-09-04 08:00（Asia/Shanghai）\n"
                "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
                "## 本窗口Brent表现\n\n"
                "| 最新价 | 24小时涨跌幅 | 区间最高价 | 区间最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 95.92 | +0.81% | 97.61 | 94.04 |\n",
                encoding="utf-8",
            )

            rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")

        for label, value in (
            ("最新价", "95.92"),
            ("24小时涨跌幅", "+0.81%"),
            ("日内最高价", "97.61"),
            ("日内最低价", "94.04"),
        ):
            self.assertIn(
                f'<span class="metric-label">{label}</span><span class="metric-value',
                rendered,
            )
            self.assertRegex(
                rendered,
                rf'<span class="metric-label">{re.escape(label)}</span><span class="metric-value[^>]*>{re.escape(value)}</span>',
            )

    def test_isw_and_today_watch_always_render_on_success(self) -> None:
        external_update = {
            "status": "success",
            "title": "Iran Update, July 23, 2026",
            "published_at_et": "2026-07-23 18:00",
            "published_at_bj": "2026-07-24 06:00",
            "url": "https://understandingwar.org/research/middle-east/iran-update-july-23-2026/",
            "source_key_takeaways": ["source one", "source two", "source three"],
            "source_key_takeaways_count": 3,
            "translated_key_takeaways_count": 3,
            "key_takeaways_zh": [
                "伊朗部队威胁霍尔木兹附近商船。",
                "地区军事力量正在调整防空部署。",
                "伊朗官员讨论制裁缓解与核谈判。",
            ],
            "supplemental_zh": ["一名油轮运营商报告海湾航线出现新的保险限制。"],
            "energy_significance_zh": ["霍尔木兹通航风险可能抬升原油运输成本。"],
        }

        with tempfile.TemporaryDirectory() as directory:
            markdown, rendered = self._render_outlook_html(
                directory,
                _tracking_outlook_lines(external_update),
            )

        for text in ("## ISW Iran Update", "## 今日关注什么", "**Key Takeaways：**"):
            self.assertIn(text, markdown)
        self.assertIn("伊朗部队威胁霍尔木兹附近商船。", markdown)
        self.assertNotIn("**重要事实：**", markdown)
        self.assertNotIn("**对原油供应", markdown)
        self.assertNotIn("一名油轮运营商报告海湾航线出现新的保险限制。", markdown)
        self.assertNotIn("霍尔木兹通航风险可能抬升原油运输成本。", markdown)
        self.assertIn(external_update["url"], markdown)
        self.assertIn("<h2>ISW Iran Update</h2>", rendered)
        self.assertIn("<h2>今日关注什么</h2>", rendered)
        for topic in ("霍尔木兹海峡", "AIS船位", "制裁、豁免和出口政策", "OPEC+", "地区冲突", "EIA/API库存", "ISW Iran Update"):
            self.assertIn(topic, rendered)

    def test_isw_and_today_watch_always_render_on_failure(self) -> None:
        external_update = {
            "status": "failed",
            "failure_reason": "network timeout",
            "listing_url": "https://understandingwar.org/research/?_product_line_filter=iran-update",
        }

        with tempfile.TemporaryDirectory() as directory:
            markdown, rendered = self._render_outlook_html(
                directory,
                _tracking_outlook_lines(external_update),
            )

        failure_copy = "本次ISW抓取失败，以下内容待下次运行补充。"
        fallback_copy = "指定网站本次抓取失败，以下内容根据已获取的相关新闻和ISW信息整理。"
        self.assertIn("## ISW Iran Update", markdown)
        self.assertIn("## 今日关注什么", markdown)
        self.assertIn(failure_copy, markdown)
        self.assertIn(fallback_copy, markdown)
        self.assertIn("https://understandingwar.org/research/", markdown)
        self.assertIn("<h2>ISW Iran Update</h2>", rendered)
        self.assertIn("<h2>今日关注什么</h2>", rendered)
        self.assertIn(failure_copy, rendered)
        self.assertIn(fallback_copy, rendered)
        self.assertIn("https://understandingwar.org/research/", rendered)

    def test_report_markdown_has_top_summary_trump_and_complete_isw(self) -> None:
        """Dropping summary placement, Trump sources, or any ISW takeaway breaks the report."""
        class Store:
            def refresh_multi_event_groups(self, *_args) -> None:
                return None

            def unpriced_watch_rows(self, *_args) -> list[dict[str, object]]:
                return []

            def reaction_rows_between(self, *_args) -> list[dict[str, object]]:
                return []

            def brent_day_klines(self, *_args) -> list[dict[str, object]]:
                return []

        external_update = {
            "status": "success",
            "title": "Iran Update, August 4, 2026",
            "published_at_et": "2026-08-04 18:30",
            "published_at_bj": "2026-08-05 06:30",
            "url": "https://understandingwar.org/research/middle-east/iran-update-august-4-2026/",
            "source_key_takeaways": [f"Source Key Takeaway {index}" for index in range(1, 9)],
            "source_key_takeaways_count": 8,
            "translated_key_takeaways_count": 8,
            "key_takeaways_zh": [f"中文Key Takeaway {index}：研判{index}" for index in range(1, 9)],
        }
        trump_update = {
            "status": "success",
            "raw_topic_item_count": 4,
            "in_window_item_count": 4,
            "relevant_source_item_count": 3,
            "statement_count": 2,
            "statements": [
                {
                    "statement_id": "trump-2",
                    "published_at_utc": "2026-08-04T11:43:35Z",
                    "published_at_bj": "2026-08-04 19:43:35",
                    "text": "特朗普要求石油企业降低油价。",
                    "topics": ["原油与能源"],
                    "source_items": [
                        {
                            "id": 102,
                            "title": "特朗普喊话石油企业",
                            "published_at_bj": "2026-08-04 19:43:35",
                            "source_url": "https://xnews.jin10.com/details/102",
                        }
                    ],
                },
                {
                    "statement_id": "trump-1",
                    "published_at_utc": "2026-08-04T03:34:54Z",
                    "published_at_bj": "2026-08-04 11:34:54",
                    "text": "特朗普称美伊谈判仍在进行。",
                    "topics": ["伊朗", "核谈判"],
                    "source_items": [
                        {
                            "id": 100,
                            "title": "特朗普谈美伊谈判",
                            "published_at_bj": "2026-08-04 11:34:54",
                            "source_url": "https://xnews.jin10.com/details/100",
                        },
                        {
                            "id": 101,
                            "title": "特朗普重申谈判立场",
                            "published_at_bj": "2026-08-04 11:43:35",
                            "source_url": "https://xnews.jin10.com/details/101",
                        },
                    ],
                },
            ],
        }
        audit = {
            "major_candidates": [],
            "news_freshness": "passed",
            "price_freshness": "passed",
        }
        movement_review = {
            "selected_raw_ids": set(),
            "groups": [],
            "unexplained": [],
            "windows": [],
        }
        homepage = FormalReportPresentation(
            panel_groups=(),
            unmatched_major_items=(
                {
                    "id": "lower-priority",
                    "title": "伊朗称霍尔木兹航运安全",
                    "summary": "这是较低优先级的未匹配事项。",
                    "time": "2026-08-04 09:00",
                    "url": "https://flash.jin10.com/detail/lower-priority",
                },
                {
                    "id": "higher-priority",
                    "title": "利比亚油田已停产",
                    "summary": "油田停产构成实际供应中断。",
                    "time": "2026-08-04 18:20",
                    "url": "https://flash.jin10.com/detail/higher-priority",
                },
            ),
            iran_update_items=(),
        )

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch("jin10_market_report.tracking_report._build_window_audit", return_value=audit))
            stack.enter_context(patch("jin10_market_report.tracking_report._build_price_movement_review", return_value=movement_review))
            stack.enter_context(patch("jin10_market_report.tracking_report._quarantine_continuity_movements", side_effect=lambda review, _audit: review))
            stack.enter_context(patch("jin10_market_report.tracking_report._degrade_movement_review_for_incomplete_data", side_effect=lambda review, _audit: review))
            stack.enter_context(patch("jin10_market_report.tracking_report._build_event_grouped_homepage", return_value=homepage))
            stack.enter_context(patch("jin10_market_report.tracking_report.aggregate_30m_klines", return_value=[]))
            stack.enter_context(patch("jin10_market_report.tracking_report.prepare_interactive_chart_markers", return_value=([], [])))
            stack.enter_context(patch("jin10_market_report.tracking_report.brent_day_summary", return_value={"latest": "84.20", "pct": "+1.25%", "high": "85.10", "low": "82.90"}))
            stack.enter_context(patch("jin10_market_report.tracking_report.classify_report_status", return_value=("FORMAL", {})))
            stack.enter_context(patch("jin10_market_report.tracking_report._build_audit_report_lines", return_value=["# audit"]))
            stack.enter_context(patch("jin10_market_report.tracking_report.build_html_report"))
            result = build_markdown_report(
                Store(),
                SimpleNamespace(source="Yahoo Finance", symbol="BZ=F", interval="5m"),
                Path(directory),
                day=datetime(2026, 8, 5, tzinfo=BJ),
                external_update=external_update,
                trump_update=trump_update,
            )
            markdown = result.markdown_path.read_text(encoding="utf-8")

        self.assertEqual(markdown.count("中文Key Takeaway"), 8)
        self.assertIn("## 特朗普相关言论", markdown)
        self.assertIn("## ISW Iran Update", markdown)
        self.assertNotIn("**重要事实：**", markdown)
        self.assertNotIn("**对原油供应", markdown)
        self.assertLess(
            markdown.index("## 今日重点（Executive Summary）"),
            markdown.index("## 今日 Brent 价格预测"),
        )
        self.assertLess(markdown.index("## 今日 Brent 价格预测"), markdown.index("统计窗口："))
        summary_section = markdown.split("## 今日重点（Executive Summary）", 1)[1].split(
            "## 今日 Brent 价格预测", 1
        )[0]
        self.assertGreaterEqual(summary_section.count("- "), 2)
        self.assertLessEqual(summary_section.count("- "), 5)
        for evidence in (
            "本窗口没有需要单列的“价格异动但新闻解释不足”事件。",
            "首页重大事项：利比亚油田已停产",
            "特朗普表态：特朗普要求石油企业降低油价。",
            "ISW研判：研判1",
        ):
            self.assertIn(evidence, summary_section)
        self.assertLess(markdown.index("## 特朗普相关言论"), markdown.index("## ISW Iran Update"))
        self.assertLess(markdown.index("2026-08-04 11:34:54"), markdown.index("2026-08-04 19:43:35"))
        for expected in (
            "伊朗、核谈判",
            "原油与能源",
            "https://xnews.jin10.com/details/100",
            "https://xnews.jin10.com/details/101",
            "https://xnews.jin10.com/details/102",
        ):
            self.assertIn(expected, markdown)
        self.assertEqual(len(result.presentation.top_summary_items), summary_section.count("- "))
        self.assertEqual(len(result.presentation.trump_items), 2)
        self.assertEqual(sum(len(item["source_items"]) for item in result.presentation.trump_items), 3)
        body = markdown.split("统计窗口：", 1)[1]
        for evidence in (
            "84.20",
            "+1.25%",
            "85.10",
            "82.90",
            "本窗口没有需要单列的“价格异动但新闻解释不足”事件。",
            "利比亚油田已停产",
            "特朗普要求石油企业降低油价。",
            "研判1",
        ):
            self.assertIn(evidence, body)
        self.assertIn("## 首页重大事项（未匹配显著异动）", body)
        self.assertIn("2026-08-04 18:20", body)
        self.assertIn("https://flash.jin10.com/detail/higher-priority", body)
        self.assertIn("不构成正式价格归因", body)

    def test_top_summary_uses_bound_homepage_and_trump_priorities(self) -> None:
        """Input order must not override the bound homepage/topic priority rules."""
        homepage = FormalReportPresentation(
            panel_groups=(),
            unmatched_major_items=(
                {
                    "id": "lower-priority",
                    "time": "2026-08-04 09:00",
                    "title": "伊朗称霍尔木兹航运安全",
                    "summary": "较早但优先级较低。",
                    "url": "https://flash.jin10.com/detail/lower-priority",
                },
                {
                    "id": "higher-priority",
                    "time": "2026-08-04 18:20",
                    "title": "利比亚油田已停产",
                    "summary": "较晚但属于实际供应中断。",
                    "url": "https://flash.jin10.com/detail/higher-priority",
                },
            ),
            iran_update_items=(),
        )
        trump_update = {
            "status": "success",
            "statements": [
                {
                    "statement_id": "iran-latest",
                    "published_at_utc": "2026-08-04T20:00:00Z",
                    "text": "较新的伊朗一般表态。",
                    "topics": ["伊朗"],
                },
                {
                    "statement_id": "hormuz-older",
                    "published_at_utc": "2026-08-04T10:00:00Z",
                    "text": "较早但最高优先级的霍尔木兹表态。",
                    "topics": ["霍尔木兹"],
                },
                {
                    "statement_id": "hormuz-latest-b",
                    "published_at_utc": "2026-08-04T21:00:00Z",
                    "text": "同主题最新但ID较大的霍尔木兹表态。",
                    "topics": ["霍尔木兹"],
                },
                {
                    "statement_id": "hormuz-latest-a",
                    "published_at_utc": "2026-08-04T21:00:00Z",
                    "text": "同主题最新且ID最小的霍尔木兹表态。",
                    "topics": ["霍尔木兹"],
                },
            ],
        }

        items = _top_summary_items(
            {"latest": "80.00", "pct": "+1.00%", "high": "81.00", "low": "79.00"},
            {"groups": [], "unexplained": []},
            homepage,
            trump_update,
            {"status": "failed", "failure_reason": "test"},
            "FORMAL",
            {"data_integrity": "完整"},
        )

        self.assertIn("首页重大事项：利比亚油田已停产", items)
        self.assertNotIn("首页重大事项：伊朗称霍尔木兹航运安全", items)
        self.assertIn("特朗普表态：同主题最新且ID最小的霍尔木兹表态。", items)
        self.assertNotIn("特朗普表态：较新的伊朗一般表态。", items)

    def test_top_summary_extracts_the_relevant_clean_trump_digest_section(self) -> None:
        """A multi-topic digest must not flood the cover summary with HTML and unrelated items."""
        trump_update = {
            "status": "success",
            "statements": [
                {
                    "statement_id": "digest",
                    "published_at_utc": "2026-08-23T10:44:20Z",
                    "topics": ["霍尔木兹", "战争与军事行动"],
                    "text": (
                        '1. 在议息次日调仓 <span class="section-news">1. <b>调整股票持仓</b>'
                        '——卖出Meta并买入Visa。</span><br/>'
                        '<span class="section-news">2. <b>宣称霍尔木兹海峡是美国新领土</b>'
                        '——特朗普发布海峡地图并配文：“美国的新领土。”</span><br/>'
                        '<span class="section-news">3. <b>讥讽加拿大</b>——关税表态。</span>'
                    ),
                }
            ],
        }

        items = _top_summary_items(
            {"latest": "93.40", "pct": "+0.27%", "high": "94.81", "low": "92.77"},
            {"groups": [], "unexplained": []},
            FormalReportPresentation(panel_groups=(), unmatched_major_items=(), iran_update_items=()),
            trump_update,
            {"status": "failed", "failure_reason": "test"},
            "FORMAL",
            {"data_integrity": "完整"},
        )

        summary = next(item for item in items if item.startswith("特朗普表态："))
        self.assertIn("霍尔木兹海峡是美国新领土", summary)
        self.assertNotIn("Meta", summary)
        self.assertNotIn("<span", summary)
        self.assertNotIn("讥讽加拿大", summary)

    def test_top_summary_normalizes_evidence_dedup_and_excludes_failed_sources(self) -> None:
        """Prefix and punctuation differences must not duplicate one evidence item."""
        homepage = FormalReportPresentation(
            panel_groups=(),
            unmatched_major_items=(
                {
                    "id": "shared",
                    "time": "2026-08-04 10:00",
                    "title": "共同 证据！",
                    "summary": "共同证据",
                    "url": "https://flash.jin10.com/detail/shared",
                },
            ),
            iran_update_items=(),
        )
        shared_trump = {
            "status": "success",
            "statements": [
                {
                    "statement_id": "shared",
                    "published_at_utc": "2026-08-04T10:00:00Z",
                    "text": "共同证据",
                    "topics": ["霍尔木兹"],
                }
            ],
        }
        shared_isw = {
            "status": "success",
            "source_key_takeaways": ["shared evidence"],
            "key_takeaways_zh": ["共同证据"],
            "source_key_takeaways_count": 1,
            "translated_key_takeaways_count": 1,
        }

        items = _top_summary_items(
            {"latest": "80.00", "pct": "+1.00%", "high": "81.00", "low": "79.00"},
            {"groups": [], "unexplained": []},
            homepage,
            shared_trump,
            shared_isw,
            "FORMAL",
            {"data_integrity": "完整"},
        )
        normalized_shared = [item for item in items if re.sub(r"[\W_]+", "", item).endswith("共同证据")]
        self.assertEqual(len(normalized_shared), 1)

        failed_items = _top_summary_items(
            {"latest": "80.00", "pct": "+1.00%", "high": "81.00", "low": "79.00"},
            {"groups": [], "unexplained": []},
            FormalReportPresentation(panel_groups=(), unmatched_major_items=(), iran_update_items=()),
            {**shared_trump, "status": "failed", "failure_reason": "Trump failed"},
            {**shared_isw, "status": "failed", "failure_reason": "ISW failed"},
            "PARTIAL",
            {"data_integrity": "部分缓存"},
        )
        failed_text = "\n".join(failed_items)
        self.assertNotIn("特朗普表态：", failed_text)
        self.assertNotIn("ISW研判：", failed_text)
        self.assertNotIn("Trump failed", failed_text)
        self.assertNotIn("ISW failed", failed_text)

    def test_pending_news_status_is_visible_in_markdown_and_blue_panel(self) -> None:
        published = datetime(2026, 7, 23, 23, 49, tzinfo=UTC)
        candidate = {
            "raw_id": "pending-0749",
            "published_dt": published,
            "row": {
                "raw_id": "pending-0749",
                "published_at_utc": published.isoformat().replace("+00:00", "Z"),
                "title": "霍尔木兹海峡油轮通行受阻",
                "body": "油轮通行受阻，原油运输面临中断风险。",
                "url": "https://flash.jin10.com/detail/pending-0749",
            },
            "direct_supply_path": True,
            "direct_supply_reason": "直接影响原油运输",
            "freshness": "新增事实",
            "is_duplicate": False,
            "price_quote_only": False,
            "is_commentary": False,
            "attribution_judgment": "暂无法确认",
            "attribution_confidence": "低",
            "attribution_relation": "新闻先于价格",
            "attribution_priority": 1,
            "logic": "航运受阻抬升原油供应风险溢价。",
            "direction_alignment": 1,
            "reaction_status": "PENDING",
            "reaction_status_note": "新闻已纳入，但截至08:00后续60分钟价格观察尚未完成，暂不做最终归因。",
        }
        group = {
            "start_dt": published - timedelta(minutes=10),
            "end_dt": published + timedelta(minutes=5),
            "direction": "上涨",
            "cumulative_pct": 0.006,
            "max_30m_pct": 0.006,
            "pct_5m": 0.001,
            "pct_15m": 0.003,
            "pct_30m": None,
            "pct_60m": None,
            "trigger_label": "30分钟涨幅达到阈值",
            "pricing_status": "无法判断",
            "attribution_reason": "待观察",
            "selected_news": [candidate],
            "homepage_news": [candidate],
            "candidates": [candidate],
            "core_driver": None,
        }

        markdown = "\n".join(
            _price_movement_review_lines({"groups": [group]}, "Asia/Shanghai")
        )
        panel = _build_blue_panel_groups({"groups": [group]}, "Asia/Shanghai")

        self.assertIn("反应状态：PENDING", markdown)
        self.assertIn(candidate["reaction_status_note"], markdown)
        self.assertEqual(panel[0]["news"][0]["reaction_status"], "PENDING")
        self.assertEqual(
            panel[0]["news"][0]["reaction_status_note"],
            candidate["reaction_status_note"],
        )

    def test_isw_dashboard_success_includes_original_publication_date(self) -> None:
        external_update = {
            "status": "success",
            "published_at_et": "2026-07-23 18:00",
            "published_at_bj": "2026-07-24 06:00",
            "url": "https://understandingwar.org/research/middle-east/iran-update-july-23-2026/",
            "source_key_takeaways": ["source one"],
            "source_key_takeaways_count": 1,
            "translated_key_takeaways_count": 1,
            "key_takeaways_zh": ["霍尔木兹附近商船风险上升。"],
        }

        items = _isw_dashboard_items(external_update)

        self.assertTrue(
            any(
                "原文发布日期" in item["text"]
                and "2026-07-23 18:00" in item["text"]
                and "2026-07-24 06:00" in item["text"]
                for item in items
            )
        )

    def test_price_only_warning_names_price_gaps(self) -> None:
        warning = _top_level_warning(
            datetime(2026, 7, 23, 0, 0, tzinfo=UTC),
            news_failed=False,
            price_failed=True,
            audit={
                "price_boundary_covered": False,
                "material_price_gap_count": 0,
                "duplicate_timestamp_count": 0,
                "timestamps_ordered": True,
                "price_continuity_ok": False,
            },
        )

        self.assertIn("统计窗口价格首尾未完整覆盖", warning or "")
        self.assertNotIn("新闻后60分钟", warning or "")
        self.assertNotIn("行情连续性异常待复核", warning or "")
        self.assertNotIn("外部更新存在失败", warning or "")

    def test_partial_audit_title_preserves_partial_status(self) -> None:
        self.assertIn("PARTIAL", _audit_title("PARTIAL"))
        self.assertNotIn("FAILED", _audit_title("PARTIAL"))

    def test_segment_coverage_counts_unique_homepage_display_news(self) -> None:
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        audit = {
            "raw_rows": [
                {"raw_id": "core", "published_at_utc": "2026-07-20T10:00:00Z", "title": "原油出口受阻", "body": "原油出口受阻。"},
                {"raw_id": "background", "published_at_utc": "2026-07-20T11:00:00Z", "title": "油轮通行受阻", "body": "油轮通行受阻。"},
            ]
        }

        rendered = "\n".join(
            _segment_coverage_lines(
                audit,
                start,
                start + timedelta(days=1),
                "Asia/Shanghai",
                ["core", "background", "background"],
                [],
            )
        )

        self.assertIn("- 最终进入正式日报首页展示新闻数量：2", rendered)
        self.assertNotIn("最终进入正式日报的引用新闻数量", rendered)

    def test_formal_chart_multi_news_marker_aggregates_own_url_and_time(self) -> None:
        marker_payload = html.escape(
            json.dumps(
                [
                    {
                        "time": "2026-07-20 19:55",
                        "title": "较早航运消息",
                        "url": "https://flash.jin10.com/detail/early",
                    },
                    {
                        "time": "2026-07-20 20:20",
                        "title": "后续航运消息",
                        "url": "https://flash.jin10.com/detail/later",
                    },
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            quote=True,
        )
        rendered = _enrich_formal_chart_svg(
            f'<svg><g class="news-marker" data-news="{marker_payload}"></g></svg>',
            {
                "events": [],
                "window_start": datetime(2026, 7, 20, 8, 0),
                "panel_groups": [
                    {
                        "key": "event-20260720t1950-20260720t2005",
                        "time_range": "19:50–20:05",
                        "news": [{"url": "https://flash.jin10.com/detail/later"}],
                    },
                    {
                        "key": "event-20260720t2010-20260720t2040",
                        "time_range": "20:10–20:40",
                        "news": [
                            {"url": "https://flash.jin10.com/detail/early"},
                            {"url": "https://flash.jin10.com/detail/later"},
                        ],
                    },
                ],
            },
        )

        self.assertRegex(
            rendered,
            r'<g\b(?=[^>]*\bclass="[^"]*\bnews-marker\b[^"]*")[^>]*\bdata-event-key="event-20260720t2010-20260720t2040"',
        )

    def test_formal_chart_marker_prefers_group_containing_published_time(self) -> None:
        marker_payload = html.escape(
            json.dumps(
                [
                    {
                        "time": "2026-07-20 20:10",
                        "title": "胡塞武装宣布对沙特实施海上禁令",
                        "url": "https://flash.jin10.com/detail/houthi",
                    }
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            quote=True,
        )
        rendered = _enrich_formal_chart_svg(
            f'<svg><g class="news-marker" data-news="{marker_payload}"></g></svg>',
            {
                "events": [],
                "window_start": datetime(2026, 7, 20, 8, 0),
                "panel_groups": [
                    {
                        "key": "event-20260720t1950-20260720t2005",
                        "time_range": "19:50–20:05",
                        "news": [{"url": "https://flash.jin10.com/detail/houthi"}],
                    },
                    {
                        "key": "event-20260720t2010-20260720t2040",
                        "time_range": "20:10–20:40",
                        "news": [{"url": "https://flash.jin10.com/detail/houthi"}],
                    },
                ],
            },
        )

        self.assertRegex(
            rendered,
            r'<g\b(?=[^>]*\bclass="[^"]*\bnews-marker\b[^"]*")[^>]*\bdata-event-key="event-20260720t2010-20260720t2040"',
        )

    def test_forced_candidate_audit_records_homepage_destination(self) -> None:
        row = {
            "raw_id": "houthi",
            "published_at_utc": "2026-07-20T12:10:00Z",
            "title": "胡塞武装宣布对沙特实施海上禁令",
            "body": "红海油轮和能源运输面临新增风险。",
            "url": "https://flash.jin10.com/detail/houthi",
            "is_summary": 0,
        }
        lines = _forced_candidate_lines(
            {
                "major_candidates": [
                    (row, "直接涉及原油运输", "命中重大主题：地缘冲突影响能源运输或供应安全")
                ]
            },
            selected_rows=[],
            all_reaction_rows=[{"news_id": "houthi", "is_duplicate": 0}],
            timezone_name="Asia/Shanghai",
            homepage_destinations={"houthi": ["价格异动组 20:10–20:40"]},
        )

        rendered = "\n".join(lines)
        self.assertIn("首页展示：价格异动组 20:10–20:40", rendered)
        self.assertNotIn("被更高优先级事件替代", rendered)

    def test_audit_homepage_candidate_is_not_described_as_excluded(self) -> None:
        row = {
            "raw_id": "houthi",
            "published_at_utc": "2026-07-20T12:10:00Z",
            "title": "胡塞武装宣布对沙特实施海上禁令",
            "body": "红海油轮和能源运输面临新增风险。",
            "url": "https://flash.jin10.com/detail/houthi",
            "is_summary": 0,
        }
        audit = {
            "raw_rows": [row],
            "segments": {"16:00-00:00": 1, "00:00-08:00": 0},
            "major_candidates": [
                (row, "直接涉及原油运输", "命中重大主题：地缘冲突影响能源运输或供应安全")
            ],
        }
        destinations = {"houthi": ["价格异动组 19:30–20:25", "价格异动组 20:50–21:35"]}

        night = "\n".join(
            _night_audit_lines(
                audit,
                selected_rows=[],
                timezone_name="Asia/Shanghai",
                homepage_destinations=destinations,
            )
        )
        inventory = "\n".join(
            _raw_inventory_lines(
                audit,
                timezone_name="Asia/Shanghai",
                homepage_destinations=destinations,
            )
        )

        self.assertIn("夜间首页展示的重要候选", night)
        self.assertIn("首页展示：价格异动组 19:30–20:25；价格异动组 20:50–21:35", night)
        self.assertNotIn("胡塞武装宣布对沙特实施海上禁令：命中重大主题：地缘冲突影响能源运输或供应安全；直接涉及原油运输；未成为价格异动复盘核心/辅助新闻。", night)
        self.assertIn("首页展示：价格异动组 19:30–20:25；价格异动组 20:50–21:35；未作为价格异动核心/次要归因", inventory)
        self.assertNotIn("重大候选但未成为价格异动核心/次要驱动；详见重大候选池审计", inventory)

    def test_audit_traces_selected_news_merged_into_homepage_representative(self) -> None:
        event_groups = [
            {
                "event_group_id": "event_group_001",
                "topic": "北方石油公司能源制裁",
                "representatives": [{"raw_id": "primary"}],
                "members": [
                    {
                        "raw_id": "primary",
                        "time": "2026-07-20 08:00",
                        "title": "美国宣布制裁北方石油公司",
                        "role": "primary",
                        "is_displayed": True,
                        "reason": "事件组首条代表事实",
                        "represented_by_news_id": "",
                        "was_selected_news": False,
                        "homepage_destinations": ["价格异动组 08:00–08:30"],
                    },
                    {
                        "raw_id": "selected-repeat",
                        "time": "2026-07-20 08:20",
                        "title": "北方石油公司遭美国制裁",
                        "role": "duplicate_or_context",
                        "is_displayed": False,
                        "reason": "同一制裁事件重复转述",
                        "represented_by_news_id": "primary",
                        "was_selected_news": True,
                        "homepage_destinations": ["价格异动组 08:00–08:30"],
                    },
                ],
            }
        ]

        rendered = "\n".join(
            _homepage_event_group_audit_lines(
                event_groups,
                {
                    "displayed_news_count": 1,
                    "event_group_count": 1,
                    "primary_count": 1,
                    "material_escalation_count": 0,
                    "merged_count": 1,
                    "over_30_reason": "",
                },
            )
        )

        self.assertIn("## 首页新闻合并与展示说明", rendered)
        self.assertIn("event_group_001", rendered)
        self.assertIn("selected-repeat", rendered)
        self.assertIn("duplicate_or_context", rendered)
        self.assertIn("represented_by_news_id=primary", rendered)
        self.assertIn("正式归因新闻追溯：由 primary 代表展示", rendered)
        self.assertNotIn("selected-repeat | 普通排除", rendered)

    def test_markdown_report_returns_complete_blue_panel_presentation(self) -> None:
        class Store:
            def refresh_multi_event_groups(self, *_args) -> None:
                return None

            def unpriced_watch_rows(self, *_args) -> list[dict[str, object]]:
                return []

            def reaction_rows_between(self, *_args) -> list[dict[str, object]]:
                return [{"news_id": "houthi-2010", "is_duplicate": 0}]

            def brent_day_klines(self, *_args) -> list[dict[str, object]]:
                return []

        raw_row = {
            "raw_id": "houthi-2010",
            "published_at_utc": "2026-07-20T12:10:00Z",
            "title": "胡塞武装宣布对沙特实施海上禁令",
            "body": "胡塞武装宣布对沙特实施海上禁令，红海油轮和能源运输面临新增风险。",
            "is_summary": 0,
            "url": "https://flash.jin10.com/detail/houthi-2010",
        }
        houthi = {
            "raw_id": "houthi-2010",
            "row": raw_row,
            "published_dt": datetime(2026, 7, 20, 12, 10, tzinfo=UTC),
            "direct_supply_path": True,
            "direct_supply_reason": "直接影响油轮运输",
            "freshness": "新增事实",
            "is_duplicate": False,
            "price_quote_only": False,
            "is_commentary": False,
            "attribution_judgment": "背景信息",
            "attribution_confidence": "低",
            "attribution_relation": "同期发生",
            "attribution_priority": 2,
            "logic": "海上禁令可能扰动红海原油运输。",
            "direction_alignment": 1,
        }
        audit = {
            "major_candidates": [
                (
                    raw_row,
                    "直接涉及原油运输",
                    "命中重大主题：霍尔木兹通行/护航/封锁/油轮运行风险",
                )
            ],
            "news_freshness": "passed",
            "price_freshness": "passed",
        }
        movement_review = {
            "selected_raw_ids": set(),
            "groups": [
                {
                    "start_dt": datetime(2026, 7, 20, 12, 10, tzinfo=UTC),
                    "end_dt": datetime(2026, 7, 20, 12, 40, tzinfo=UTC),
                    "direction": "上涨",
                    "cumulative_pct": 0.0051,
                    "max_30m_pct": 0.0051,
                    "pct_5m": 0.0012,
                    "pct_15m": 0.0022,
                    "pct_30m": 0.0051,
                    "pct_60m": 0.0062,
                    "pricing_status": "未充分定价",
                    "attribution_reason": "附近新闻不足以支持可靠归因。",
                    "homepage_news": [houthi],
                    "candidates": [houthi],
                    "selected_news": [],
                    "secondary_drivers": [],
                    "core_driver": None,
                }
            ],
            "unexplained": [],
            "windows": [],
        }
        external_update = {"status": "failed", "failure_reason": "test", "listing_url": "https://understandingwar.org/research/"}

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch("jin10_market_report.tracking_report._build_window_audit", return_value=audit))
            stack.enter_context(patch("jin10_market_report.tracking_report._build_price_movement_review", return_value=movement_review))
            stack.enter_context(patch("jin10_market_report.tracking_report._degrade_movement_review_for_incomplete_data", side_effect=lambda review, _audit: review))
            stack.enter_context(patch("jin10_market_report.tracking_report.aggregate_30m_klines", return_value=[]))
            stack.enter_context(patch("jin10_market_report.tracking_report.prepare_interactive_chart_markers", return_value=([], [])))
            stack.enter_context(patch("jin10_market_report.tracking_report.brent_day_summary", return_value={"latest": "89.00", "pct": "-1.83%", "high": "91.08", "low": "86.14"}))
            stack.enter_context(patch("jin10_market_report.tracking_report.classify_report_status", return_value=("FORMAL", {})))
            stack.enter_context(patch("jin10_market_report.tracking_report._build_audit_report_lines", return_value=["# audit"]))
            stack.enter_context(patch("jin10_market_report.tracking_report.build_html_report"))
            result = build_markdown_report(
                Store(),
                SimpleNamespace(source="Yahoo Finance", symbol="BZ=F", interval="5m"),
                Path(directory),
                day=datetime(2026, 7, 21, tzinfo=BJ),
                external_update=external_update,
                trump_update={"status": "failed", "failure_reason": "test"},
            )
            self.assertIsInstance(result, ReportBuildResult)
            self.assertTrue(result.markdown_path.exists())
            self.assertEqual(result.presentation.panel_groups[0]["time_range"], "20:10–20:40")
            self.assertEqual(result.presentation.panel_groups[0]["news"][0]["title"], "胡塞武装宣布对沙特实施海上禁令")
            self.assertEqual(result.presentation.unmatched_major_items, ())
            self.assertNotIn("major_timeline_items", result.presentation.__dataclass_fields__)
            self.assertEqual([item["link_label"] for item in result.presentation.iran_update_items], ["查看ISW列表"])

    def test_aggregate_30m_ohlc_uses_beijing_buckets_and_exact_window(self) -> None:
        start_utc = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        end_utc = start_utc + timedelta(days=1)
        rows = [
            {"ts_utc": "2026-07-13T00:05:00Z", "open": 78.0, "high": 78.4, "low": 77.9, "close": 78.2},
            {"ts_utc": "2026-07-13T00:25:00Z", "open": 78.2, "high": 78.6, "low": 78.1, "close": 78.5},
            {"ts_utc": "2026-07-13T00:30:00Z", "open": 78.5, "high": 78.8, "low": 78.4, "close": 78.7},
            {"ts_utc": "2026-07-14T00:00:00Z", "open": 90.0, "high": 91.0, "low": 89.0, "close": 90.5},
        ]
        candles = aggregate_30m_klines(rows, start_utc, end_utc)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].start_bj.strftime("%Y-%m-%d %H:%M"), "2026-07-13 08:00")
        self.assertEqual((candles[0].open, candles[0].high, candles[0].low, candles[0].close), (78.0, 78.6, 77.9, 78.5))
        self.assertEqual(candles[0].source_rows, 2)

    def test_interactive_markers_reuse_selected_news_and_group_same_candle(self) -> None:
        start_bj = datetime(2026, 7, 13, 8, 0, tzinfo=BJ)
        candles = [
            ThirtyMinuteCandle(start_bj + timedelta(minutes=30 * index), 78, 79, 77, 78.5, 6)
            for index in range(4)
        ]

        def core(raw_id: str, minute: int, title: str, url: str):
            return {
                "raw_id": raw_id,
                "published_dt": (start_bj + timedelta(minutes=minute)).astimezone(UTC),
                "row": {"title": title, "url": url},
            }

        first_group = {
            "start_dt": start_bj.astimezone(UTC),
            "end_dt": (start_bj + timedelta(minutes=30)).astimezone(UTC),
            "core_driver": core("first", 12, "霍尔木兹海峡通航受限", "https://flash.jin10.com/detail/1"),
            "secondary_drivers": [core("second", 5, "同一K线较早新闻", "not-a-real-url")],
            "attribution_judgment": "主要驱动",
            "cumulative_pct": 0.004,
        }
        unmatched = {
            "start_dt": (start_bj + timedelta(hours=1)).astimezone(UTC),
            "end_dt": (start_bj + timedelta(hours=1, minutes=30)).astimezone(UTC),
            "direction": "上涨",
            "pct_30m": 0.003,
            "pct_60m": 0.004,
        }
        review = {"groups": [first_group], "unexplained": [unmatched]}
        markers, unmatched_movements = prepare_interactive_chart_markers(
            review,
            candles,
            start_bj,
            start_bj + timedelta(days=1),
        )
        self.assertEqual(len(markers), 1)
        self.assertEqual([item.published_bj.strftime("%H:%M") for item in markers[0].items], ["08:05", "08:12"])
        self.assertEqual(markers[0].items[0].url, "")
        self.assertEqual(markers[0].items[1].url, "https://flash.jin10.com/detail/1")
        self.assertEqual(markers[0].items[0].reaction_text, "该时段价格上涨")
        self.assertEqual(len(unmatched_movements), 1)
        self.assertNotIn("chart_event_id", first_group)
        self.assertNotIn("chart_event_id", unmatched)

    def test_svg_is_valid_contains_click_data_and_no_system_ids(self) -> None:
        start_bj = datetime(2026, 7, 13, 8, 0, tzinfo=BJ)
        candles = [
            ThirtyMinuteCandle(start_bj, 78.0, 78.8, 77.8, 78.5, 6),
            ThirtyMinuteCandle(start_bj + timedelta(minutes=30), 78.5, 79.0, 78.2, 78.3, 6),
        ]
        events = [
            ChartNewsMarker(
                "news-20260713-0800",
                start_bj,
                start_bj + timedelta(minutes=12),
                78.8,
                (ChartNewsItem(start_bj + timedelta(minutes=12), "真实新闻标题", "https://flash.jin10.com/detail/1", "该时段价格上涨"),),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report_brent_24h.svg"
            artifact = render_brent_24h_svg(
                candles,
                events,
                start_bj,
                start_bj + timedelta(days=1),
                path,
                "Yahoo Finance / BZ=F / 5m",
                [UnmatchedMovement(start_bj + timedelta(minutes=30), start_bj + timedelta(hours=1))],
            )
            self.assertIsNotNone(artifact)
            ET.parse(path)
            svg = path.read_text(encoding="utf-8")
            self.assertIn('class="news-marker"', svg)
            self.assertIn('data-news=', svg)
            self.assertIn('class="unmatched-band"', svg)
            self.assertNotIn("E01", svg)
            self.assertNotIn("U01", svg)
            self.assertIn("O 78.00 H 78.80 L 77.80 C 78.50", svg)
            self.assertIn("2026-07-13 08:00 至 2026-07-14 08:00", svg)

    def test_three_day_svg_uses_actual_duration_and_readable_time_ticks(self) -> None:
        start_bj = datetime(2026, 9, 18, 8, 0, tzinfo=BJ)
        candles = [
            ThirtyMinuteCandle(start_bj, 104.0, 104.2, 103.8, 104.1, 6),
            ThirtyMinuteCandle(start_bj + timedelta(days=2, hours=22), 99.0, 99.2, 98.8, 99.1, 6),
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weekend_brent_24h.svg"
            artifact = render_brent_24h_svg(
                candles,
                [],
                start_bj,
                start_bj + timedelta(days=3),
                path,
                "Yahoo Finance / BZ=F / 5m",
            )
            self.assertIsNotNone(artifact)
            svg = path.read_text(encoding="utf-8")

        self.assertIn("Brent 72小时价格走势", svg)
        self.assertLessEqual(svg.count('y="574.0"'), 13)

    def test_audit_style_unexplained_lines_never_receive_chart_ids_by_default(self) -> None:
        start = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        group = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=30),
            "direction": "上涨",
            "cumulative_pct": 0.006,
            "max_30m_pct": 0.006,
            "trigger_label": "30分钟绝对波动 >= 0.50%",
            "candidates": [],
            "attribution_reason": "附近新闻不足。",
            "chart_event_id": "U01",
        }
        review = {"unexplained": [group]}
        audit_lines = _unexplained_price_movement_lines(review, "Asia/Shanghai")
        report_lines = _unexplained_price_movement_lines(review, "Asia/Shanghai", include_chart_ids=True)
        self.assertNotIn("U01", "\n".join(audit_lines))
        self.assertIn("U01", "\n".join(report_lines))

    def test_unexplained_movement_includes_eligible_news_as_background_only(self) -> None:
        start = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        candidate = {
            "raw_id": "background-1",
            "row": {
                "title": "霍尔木兹海峡油轮通行受阻，原油运输风险上升",
                "body": "霍尔木兹海峡一艘油轮通行受阻，航运安全风险上升。",
                "url": "https://flash.jin10.com/detail/background-1",
                "published_at_utc": "2026-07-13T00:10:00Z",
            },
            "direct_supply_path": True,
            "direct_supply_reason": "影响霍尔木兹海峡油轮通行",
            "freshness": "新增事实",
            "attribution_freshness": "新增事实",
            "attribution_relation": "同期发生",
            "attribution_judgment": "背景信息",
            "attribution_priority": 2,
            "is_duplicate": False,
            "price_quote_only": False,
            "is_commentary": False,
        }
        group = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=30),
            "direction": "上涨",
            "cumulative_pct": 0.006,
            "homepage_news": [candidate],
            "selected_news": [],
        }

        rendered = "\n".join(_unexplained_price_movement_lines({"unexplained": [group]}, "Asia/Shanghai"))

        self.assertIn("相关背景（未作价格归因）", rendered)
        self.assertIn("霍尔木兹海峡一艘油轮通行受阻", rendered)
        self.assertIn("https://flash.jin10.com/detail/background-1", rendered)
        self.assertIn("归因判断：相关背景，未作价格归因", rendered)
        self.assertNotIn("归因判断：主要驱动", rendered)

    def test_formal_html_inlines_svg_adds_click_panel_cards_and_audit_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_test.md"
            svg = output_dir / "Jin10_Brent_Rolling_Reaction_test_brent_24h.svg"
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                '<g class="news-marker" tabindex="0" data-news="[]"><circle cx="5" cy="5" r="2"/></g></svg>',
                encoding="utf-8",
            )
            markdown.write_text(
                "# 日报\n\n"
                "## 价格情况\n\n"
                "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 78.50 | +1.00% | 79.00 | 77.00 |\n\n"
                f"![chart]({svg.name})\n\n"
                "[查看完整 Audit →](Jin10_Brent_Rolling_Audit_test.html)\n",
                encoding="utf-8",
            )
            html_path = build_html_report(markdown, output_dir)
            rendered = html_path.read_text(encoding="utf-8")
            self.assertIn('<figure class="chart-figure"', rendered)
            self.assertIn('class="metric-grid"', rendered)
            self.assertIn('id="related-news-panel"', rendered)
            self.assertIn("点击上方K线图中的蓝色新闻时间点，查看该时段的相关新闻与市场解读。", rendered)
            self.assertLess(rendered.index('class="chart-figure"'), rendered.index('id="related-news-panel"'))
            self.assertNotIn("原文链接暂无", rendered)
            self.assertNotIn("scrollIntoView", rendered)
            self.assertIn("link.target = '_blank'", rendered)
            self.assertIn('href="Jin10_Brent_Rolling_Audit_test.html"', rendered)

    def test_formal_html_renders_complete_source_collections_in_report_order_with_safe_links(self) -> None:
        summary_urls = [f"https://example.com/summary/{index}" for index in range(1, 6)]
        trump_urls = [f"https://flash.jin10.com/detail/trump-{index}" for index in range(1, 10)]
        extra_trump_url = "https://flash.jin10.com/detail/trump-1-extra"
        isw_urls = [f"https://understandingwar.org/research/middle-east/takeaway-{index}/" for index in range(1, 9)]
        metadata_url = "https://understandingwar.org/research/middle-east/iran-update-test/"
        summary_items = tuple(
            f"摘要{index}：[摘要来源]({url})"
            for index, url in enumerate(summary_urls, 1)
        )
        trump_items = tuple(
            {
                "statement_id": f"trump-{index}",
                "published_at_bj": f"2026-08-04 {index:02d}:00:00",
                "text": f"特朗普言论{index}。" if index > 1 else "特朗普言论1含有<script>标记。",
                "topics": ["伊朗", f"主题{index}" if index > 1 else "主题<一>"],
                "source_items": [
                    {
                        "title": f"金十来源{index}",
                        "source_url": url,
                    },
                    *(
                        [
                            {"title": "补充金十来源", "source_url": extra_trump_url},
                            {"title": "不安全来源", "source_url": "javascript:alert(1)"},
                        ]
                        if index == 1
                        else []
                    ),
                ],
            }
            for index, url in enumerate(trump_urls, 1)
        )
        iran_update_items = (
            {
                "text": "原文发布日期：纽约时间 2026-08-04 18:00 / 北京时间 2026-08-05 06:00",
                "url": metadata_url,
                "link_label": "查看ISW原文",
            },
            *(
                {
                    "text": f"ISW Key Takeaway {index}。",
                    "url": url,
                    "link_label": "查看ISW原文",
                }
                for index, url in enumerate(isw_urls, 1)
            ),
        )
        presentation = FormalReportPresentation(
            panel_groups=(),
            unmatched_major_items=(),
            iran_update_items=iran_update_items,
            top_summary_items=summary_items,
            trump_items=trump_items,
        )

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_collections.md"
            markdown.write_text(
                "# 日报\n\n"
                "## 今日重点（Executive Summary）\n\n"
                + "\n".join(f"- {item}" for item in summary_items)
                + "\n\n"
                "统计窗口：2026-08-04 08:00 至 2026-08-05 08:00（Asia/Shanghai）\n"
                "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
                "## 本窗口Brent表现\n\n"
                "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 85.28 | +1.42% | 87.54 | 83.41 |\n\n"
                "## 市场主要驱动与下一时段判断\n\n"
                "### 本窗口市场主线\n\n"
                "可归因线索主要集中在霍尔木兹航运风险。\n\n"
                "## 特朗普相关言论\n\n"
                + "\n".join(
                    f"### 2026-08-04 {index:02d}:00:00｜伊朗、主题{index}\n\n"
                    f"特朗普言论{index}。\n\n"
                    f"- 金十来源：[金十来源{index}]({url})\n"
                    for index, url in enumerate(trump_urls, 1)
                )
                + "\n## ISW Iran Update\n\n"
                f"- 更新时间：纽约时间 2026-08-04 18:00 / 北京时间 2026-08-05 06:00\n"
                "- 标题：Iran Update Test\n"
                f"- 原文链接：{metadata_url}\n\n"
                "**Key Takeaways：**\n\n"
                + "\n".join(
                    f"{index}. ISW Key Takeaway {index}。"
                    for index in range(1, 9)
                )
                + "\n\n**对Brent/能源市场的关注意义：**\n\n- 这是附属监测说明，不是Key Takeaway。"
                + "\n\n## 今日关注什么\n\n- 霍尔木兹通行。\n",
                encoding="utf-8",
            )

            presentation_rendered = build_html_report(
                markdown,
                output_dir,
                presentation=presentation,
            ).read_text(encoding="utf-8")
            fallback_rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")
            status_presentation = FormalReportPresentation(
                panel_groups=(),
                unmatched_major_items=(),
                iran_update_items=(
                    {
                        "text": "本次ISW抓取失败，以下内容待下次运行补充。",
                        "url": metadata_url,
                        "link_label": "查看ISW列表",
                    },
                ),
            )
            status_rendered = build_html_report(
                markdown,
                output_dir,
                presentation=status_presentation,
            ).read_text(encoding="utf-8")
            rendered_variants = (presentation_rendered, fallback_rendered)

        for rendered in rendered_variants:
            self.assertEqual(rendered.count('data-summary-item="true"'), 5)
            self.assertEqual(rendered.count('data-trump-statement="true"'), 9)
            self.assertEqual(rendered.count('data-isw-key-takeaway="true"'), 8)
            self.assertLess(rendered.index("今日重点"), rendered.index("统计窗口"))
            self.assertLess(rendered.index("特朗普相关言论"), rendered.index("ISW Iran Update"))
            for url in (*summary_urls, *trump_urls, metadata_url):
                self.assertIn(
                    f'href="{url}" target="_blank" rel="noopener noreferrer"',
                    rendered,
                )
        self.assertIn(
            f'href="{extra_trump_url}" target="_blank" rel="noopener noreferrer"',
            presentation_rendered,
        )
        self.assertNotIn("javascript:alert(1)", presentation_rendered)
        self.assertIn("特朗普言论1含有&lt;script&gt;标记。", presentation_rendered)
        self.assertIn("主题&lt;一&gt;", presentation_rendered)
        self.assertIn("这是附属监测说明，不是Key Takeaway。", fallback_rendered)
        self.assertNotIn('data-isw-key-takeaway="true">这是附属监测说明', fallback_rendered)
        self.assertEqual(status_rendered.count('data-isw-key-takeaway="true"'), 0)
        for url in isw_urls:
            self.assertIn(
                f'href="{url}" target="_blank" rel="noopener noreferrer"',
                presentation_rendered,
            )

    def test_report_keeps_instrument_and_coverage_out_of_homepage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_test_PARTIAL.md"
            svg = output_dir / "Jin10_Brent_Rolling_Reaction_test_PARTIAL_brent_24h.svg"
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>',
                encoding="utf-8",
            )
            markdown.write_text(
                "# 日报\n\n"
                "统计窗口：2026-07-23 08:00 至 2026-07-24 08:00（Asia/Shanghai）\n"
                "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
                "> 本次Brent行情未达到正式日报门槛（价格保护区间未完整覆盖）。\n\n"
                "> 本时段出现极端价格波动，可能与重大突发事件有关，需结合相关新闻人工判断。\n\n"
                "## 标的信息\n\n"
                "- instrument_name: Brent Crude Oil Last Day Financ\n"
                "- source: Yahoo Finance\n"
                "- ticker: BZ=F\n"
                "- contract_month:\n"
                "- contract_type: Yahoo Finance BZ=F\n\n"
                "## 数据覆盖范围\n\n"
                "- Brent 5分钟K线数量：260\n"
                "- 实际价格覆盖：2026-07-23 09:30:00 至 2026-07-24 08:28:00 北京时间\n"
                "- 重复时间戳数量：0\n"
                "- 时间戳是否有序：是\n"
                "- 大于5分钟的K线缺口数量：3；最大缺口：735分钟\n"
                "- 数据状态：PARTIAL；原因：价格保护区间起点未覆盖。\n\n"
                "## 本窗口Brent表现\n\n"
                "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 100.34 | +15.11% | 102.01 | 86.06 |\n\n"
                f"![chart]({svg.name})\n\n"
                "## 市场主要驱动与下一时段判断\n\n"
                "### 本窗口市场主线\n\n"
                "极端波动需人工判断。\n\n"
                "[查看完整 Audit →](Jin10_Brent_Rolling_Audit_test_FAILED.html)\n",
                encoding="utf-8",
            )

            rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")

            markdown_text = markdown.read_text(encoding="utf-8")
            self.assertIn("## 标的信息", markdown_text)
            self.assertIn("## 数据覆盖范围", markdown_text)
            self.assertNotIn('aria-label="标的信息"', rendered)
            self.assertNotIn('aria-label="数据覆盖范围"', rendered)
            self.assertNotIn("instrument_name: Brent Crude Oil Last Day Financ", rendered)
            self.assertNotIn("Brent 5分钟K线数量：260", rendered)
            self.assertIn(
                "本时段出现极端价格波动，可能与重大突发事件有关，需结合相关新闻人工判断。",
                rendered,
            )
            self.assertLess(rendered.index('class="chart-figure"'), rendered.index('id="related-news-panel"'))

    def test_formal_html_keeps_partial_and_failed_warnings_after_five_summary_items(self) -> None:
        warnings = {
            "PARTIAL": "本次Brent行情未达到正式日报门槛（统计窗口价格首尾未完整覆盖）。价格表现仅供参考。",
            "FAILED": "本次金十新闻与Brent行情外部更新失败。当前内容不可作为正式结论使用。",
        }
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            for status, warning in warnings.items():
                with self.subTest(status=status):
                    markdown = output_dir / f"Jin10_Brent_Rolling_Reaction_warning_{status}.md"
                    markdown.write_text(
                        "# 日报\n\n"
                        "## 今日重点（Executive Summary）\n\n"
                        + "\n".join(f"- 摘要{index}" for index in range(1, 6))
                        + "\n\n"
                        "统计窗口：2026-08-04 08:00 至 2026-08-05 08:00（Asia/Shanghai）\n"
                        "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
                        f"> {warning}\n\n"
                        "> 本时段出现极端价格波动，可能与重大突发事件有关，需结合相关新闻人工判断。\n\n"
                        "## 标的信息\n\n- ticker: BZ=F\n\n"
                        "## 本窗口Brent表现\n\n"
                        "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                        "| ---: | ---: | ---: | ---: |\n"
                        "| N/A | N/A | N/A | N/A |\n",
                        encoding="utf-8",
                    )

                    rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")

                    self.assertIn(warning, rendered)

    def test_formal_html_renders_trump_failure_and_empty_status_without_statement_markers(self) -> None:
        cases = (
            (
                "failure",
                {"status": "failed", "failure_reason": "网络异常"},
                "金十特朗普专栏抓取失败：网络异常",
            ),
            (
                "empty",
                {"status": "success", "statements": []},
                "本窗口金十特朗普专栏未发现符合主题条件的特朗普本人表态。",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            for label, trump_update, status_text in cases:
                with self.subTest(label=label):
                    markdown = output_dir / f"Jin10_Brent_Rolling_Reaction_trump_{label}.md"
                    markdown.write_text(
                        "# 日报\n\n"
                        "统计窗口：2026-08-04 08:00 至 2026-08-05 08:00（Asia/Shanghai）\n"
                        "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
                        "## 本窗口Brent表现\n\n"
                        "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                        "| ---: | ---: | ---: | ---: |\n"
                        "| N/A | N/A | N/A | N/A |\n\n"
                        "## 特朗普相关言论\n\n"
                        f"- {status_text}\n\n"
                        "## ISW Iran Update\n",
                        encoding="utf-8",
                    )
                    presentation = FormalReportPresentation(
                        panel_groups=(),
                        unmatched_major_items=(),
                        iran_update_items=(),
                        trump_items=tuple(_trump_dashboard_items(trump_update)),
                    )
                    legacy_presentation = FormalReportPresentation(
                        panel_groups=(),
                        unmatched_major_items=(),
                        iran_update_items=(),
                        trump_items=tuple(
                            {
                                key: value
                                for key, value in item.items()
                                if key != "kind"
                            }
                            for item in _trump_dashboard_items(trump_update)
                        ),
                    )

                    presentation_rendered = build_html_report(
                        markdown,
                        output_dir,
                        presentation=presentation,
                    ).read_text(encoding="utf-8")
                    legacy_rendered = build_html_report(
                        markdown,
                        output_dir,
                        presentation=legacy_presentation,
                    ).read_text(encoding="utf-8")
                    fallback_rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")

                    for rendered in (presentation_rendered, legacy_rendered, fallback_rendered):
                        self.assertIn(status_text, rendered)
                        self.assertEqual(rendered.count('data-trump-statement="true"'), 0)

    def test_formal_html_renders_compact_chart_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_test.md"
            svg = output_dir / "Jin10_Brent_Rolling_Reaction_test_brent_24h.svg"
            news_payload = html.escape(
                json.dumps(
                    [
                        {
                            "time": f"2026-07-14 13:{40 + index:02d}",
                            "title": f"新闻{index}",
                            "url": f"https://flash.jin10.com/detail/{index}",
                            "reaction": "该时段价格上涨",
                        }
                        for index in range(1, 4)
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                quote=True,
            )
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60">'
                '<rect class="unmatched-band" x="1" y="1" width="30" height="50">'
                '<title>08:00–08:35 暂无直接新闻解释</title></rect>'
                f'<g class="news-marker" tabindex="0" data-news="{news_payload}">'
                '<circle cx="60" cy="30" r="3"/></g></svg>',
                encoding="utf-8",
            )
            news_blocks = "\n\n".join(
                f"{index}. 新闻摘要：新闻{index}摘要\n"
                f"   原文链接：https://flash.jin10.com/detail/{index}\n"
                "   时间关系：同期发生\n"
                "   信息新鲜度：新增事实\n"
                "   传导路径：直接影响能源运输\n"
                f"   归因判断：{'主要驱动' if index == 1 else '次要驱动'}\n"
                f"   置信度：{'高' if index == 1 else '中'}"
                for index in range(1, 4)
            )
            markdown.write_text(
                "# 日报\n\n"
                "统计窗口：2026-07-14 08:00 至 2026-07-15 08:00（Asia/Shanghai）\n"
                "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
                "## 本窗口Brent表现\n\n"
                "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 85.28 | +1.42% | 87.54 | 83.41 |\n\n"
                f"![chart]({svg.name})\n\n"
                "## 本窗口Brent价格异动复盘\n\n"
                "### 北京时间 13:35–14:50｜Brent上涨 +1.57%｜最大30分钟涨幅 +1.11%\n\n"
                "**油价反应：**\n"
                "5分钟：-0.21%｜15分钟：+0.10%｜30分钟：+0.68%｜60分钟：+1.13%｜本轮累计：+1.57%\n\n"
                "**相关新闻：**\n"
                f"{news_blocks}\n\n"
                "**市场解读：**\n"
                "市场主要交易霍尔木兹航运风险。\n\n"
                "## 价格异动但新闻解释不足\n\n"
                "### 北京时间 08:00–08:35｜Brent上涨 +0.58%\n\n"
                "**油价反应：**\n"
                "5分钟：+0.00%｜15分钟：+0.77%｜30分钟：+0.63%｜60分钟：+0.08%｜本轮累计：+0.58%\n\n"
                "归因判断：暂无法确认\n"
                "置信度：低\n\n"
                "## 未充分定价的重大风险\n\n"
                "- 这里是不应出现在正式HTML的完整风险新闻列表。\n\n"
                "## 市场主要驱动与下一时段判断\n\n"
                "### 本窗口市场主线\n\n"
                "Brent本窗口整体偏强，窗口涨跌幅为+1.42%；识别到2段独立价格异动。可归因线索主要集中在霍尔木兹航运风险。\n\n"
                "### 核心驱动窗口\n\n"
                "1. 不应重复显示的核心驱动窗口。\n\n"
                "## 接下来关注什么\n\n"
                "- 霍尔木兹通行、油轮和港口变化；\n"
                "- 制裁、护航和OPEC+政策；\n\n"
                "### 2. 外部网站最新 update 中文总结\n\n"
                "原文链接：https://understandingwar.org/research/middle-east/example/\n\n"
                "**中文总结：**\n"
                "1. 伊朗仍保留袭击商船的能力和意愿。\n"
                "2. 这条长篇翻译不应完整出现在正式HTML。\n\n"
                "[查看完整 Audit →](Jin10_Brent_Rolling_Audit_test.html)\n",
                encoding="utf-8",
            )

            rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")
            self.assertLess(rendered.index('class="chart-figure"'), rendered.index('id="related-news-panel"'))
            self.assertNotIn('id="today-market-card"', rendered)
            self.assertIn('class="market-lede"', rendered)
            self.assertIn('id="iran-update-card"', rendered)
            self.assertIn('id="tracking-card"', rendered)
            self.assertIn("ISW Iran Update", rendered)
            self.assertIn("今日关注什么", rendered)
            self.assertIn("本窗口市场主线", rendered)
            self.assertLess(rendered.index("本窗口市场主线"), rendered.index("Brent关键指标"))
            self.assertNotIn('<h2>本窗口Brent价格异动复盘</h2>', rendered)
            self.assertNotIn('<h2>价格异动但新闻解释不足</h2>', rendered)
            self.assertNotIn('<h2>未充分定价的重大风险</h2>', rendered)
            self.assertNotIn("不应重复显示的核心驱动窗口", rendered)
            self.assertNotIn("这条长篇翻译不应完整出现在正式HTML", rendered)
            self.assertIn("https://understandingwar.org/research/middle-east/example/", rendered)
            self.assertIn("查看ISW原文", rendered)
            self.assertIn("AIS船位", rendered)
            self.assertIn("EIA/API库存", rendered)
            self.assertIn('data-event-key="event-20260714t1335-20260714t1450"', rendered)
            self.assertIn('data-event-key="event-20260714t0800-20260714t0835"', rendered)
            self.assertIn('class="unmatched-band-hit"', rendered)
            self.assertIn("暂未找到符合条件的金十新闻，原因暂无法确认，需人工继续核查。", rendered)
            self.assertLess(
                rendered.index("if(item.kind==='unexplained'"),
                rendered.index("const summary=document.createElement('div')"),
            )
            self.assertIn("查看金十原文 ↗", rendered)
            self.assertIn("展开全部", rendered)
            self.assertNotIn("scrollIntoView", rendered)
            self.assertNotIn("window.open", rendered)
            self.assertNotIn("alert(", rendered)
            self.assertNotIn("<dialog", rendered)
            self.assertIn("@media(max-width:820px)", rendered)

            payload_match = re.search(
                r'<script type="application/json" id="brent-event-data">(.*?)</script>',
                rendered,
                re.DOTALL,
            )
            self.assertIsNotNone(payload_match)
            event_data = json.loads(payload_match.group(1))
            explained = next(item for item in event_data if item["kind"] == "explained")
            self.assertEqual(len(explained["news"]), 3)
            self.assertEqual(
                [item["url"] for item in explained["news"]],
                [f"https://flash.jin10.com/detail/{index}" for index in range(1, 4)],
            )

    def test_formal_unexplained_news_keeps_background_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_test.md"
            svg = output_dir / "Jin10_Brent_Rolling_Reaction_test_brent_24h.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>', encoding="utf-8")
            markdown.write_text(
                "# 日报\n\n"
                "统计窗口：2026-07-14 08:00 至 2026-07-15 08:00（Asia/Shanghai）\n\n"
                "## 本窗口Brent表现\n\n"
                "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 85.28 | +1.42% | 87.54 | 83.41 |\n\n"
                f"![chart]({svg.name})\n\n"
                "## 价格异动但新闻解释不足\n\n"
                "### 北京时间 08:00–08:35｜Brent上涨 +0.58%\n\n"
                "1. 新闻摘要：油轮通行受阻\n"
                "   发布时间：2026-07-14 08:10 北京时间\n"
                "   原文链接：https://flash.jin10.com/detail/background\n"
                "   时间关系：同期发生\n"
                "   信息新鲜度：新增事实\n"
                "   归因判断：相关背景，未作价格归因\n"
                "   置信度：低\n\n"
                "归因判断：暂无法确认\n"
                "置信度：低\n",
                encoding="utf-8",
            )

            rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")
            payload_match = re.search(
                r'<script type="application/json" id="brent-event-data">(.*?)</script>',
                rendered,
                re.DOTALL,
            )
            self.assertIsNotNone(payload_match)
            event_data = json.loads(payload_match.group(1))
            self.assertEqual(event_data[0]["news"][0]["judgment"], "相关背景，未作价格归因")

    def test_formal_html_blue_panel_defaults_to_all_day_news(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_test.md"
            svg = output_dir / "Jin10_Brent_Rolling_Reaction_test_brent_24h.svg"
            marker_payload = html.escape(
                json.dumps(
                    [
                        {
                            "time": "2026-07-20 20:10",
                            "title": "胡塞武装宣布对沙特实施海上禁令",
                            "url": "https://flash.jin10.com/detail/1",
                        }
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                quote=True,
            )
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60">'
                '<rect class="unmatched-band" x="1" y="1" width="30" height="50">'
                '<title>20:10–20:40 暂无直接新闻解释</title></rect>'
                f'<g class="news-marker" tabindex="0" data-news="{marker_payload}">'
                '<circle cx="60" cy="30" r="3"/></g></svg>',
                encoding="utf-8",
            )
            markdown.write_text(
                "# 日报\n\n"
                "统计窗口：2026-07-20 08:00 至 2026-07-21 08:00（Asia/Shanghai）\n"
                "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
                "## 本窗口Brent表现\n\n"
                "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 89.00 | -1.83% | 91.08 | 86.14 |\n\n"
                f"![chart]({svg.name})\n\n"
                "## 本窗口Brent价格异动复盘\n\n"
                "本窗口没有满足硬门槛的核心驱动。\n\n"
                "## 市场主要驱动与下一时段判断\n\n"
                "### 本窗口市场主线\n\n"
                "Brent本窗口整体偏弱。\n\n"
                "## 接下来关注什么\n\n"
                "### 1. 金十 + Brent 价格异动推导出的跟踪事项\n\n"
                "- 霍尔木兹是否出现实际通行限制。\n\n"
                "[查看完整 Audit →](Jin10_Brent_Rolling_Audit_test.html)\n",
                encoding="utf-8",
            )

            rendered = build_html_report(
                markdown,
                output_dir,
                presentation=FormalReportPresentation(
                    panel_groups=(
                        {
                            "key": "event-20260720t2010-20260720t2040",
                            "time_range": "20:10–20:40",
                            "direction": "下跌",
                            "cumulative": "-0.51%",
                            "max_30m": "-0.51%",
                            "pct_5m": "-0.12%",
                            "pct_15m": "-0.22%",
                            "pct_30m": "-0.51%",
                            "pct_60m": "-0.62%",
                            "interpretation": "航运风险消息与价格异动同期出现。",
                            "news": (
                                {
                                    "id": "houthi",
                                    "time": "2026-07-20 20:10",
                                    "title": "胡塞武装宣布对沙特实施海上禁令",
                                    "summary": "胡塞方面宣布新的海上限制措施。",
                                    "url": "https://flash.jin10.com/detail/1",
                                    "impact": "红海与海湾运输风险上升。",
                                    "price_status": "同期相关信息",
                                },
                                {
                                    "id": "hormuz",
                                    "time": "2026-07-20 20:23",
                                    "title": "伊朗称维护霍尔木兹利益",
                                    "summary": "伊朗重申对关键航道的立场。",
                                    "url": "https://flash.jin10.com/detail/2",
                                    "impact": "霍尔木兹风险溢价需要持续跟踪。",
                                    "price_status": "同期相关信息",
                                },
                            ),
                        },
                    ),
                    unmatched_major_items=(
                        {
                            "id": "insurance",
                            "time": "2026-07-20 22:05",
                            "title": "油轮保险限制进一步收紧",
                            "summary": "海湾航线出现新的保险限制。",
                            "url": "https://flash.jin10.com/detail/3",
                            "impact": "暂未发现可直接匹配的Brent显著异动，需持续关注后续影响。",
                            "price_status": "未匹配显著异动",
                        },
                    ),
                    iran_update_items=(),
                ),
            ).read_text(encoding="utf-8")

            self.assertIn("全日重要新闻", rendered)
            self.assertIn("胡塞武装宣布对沙特实施海上禁令", rendered)
            self.assertIn("⚠ 重大事件", rendered)
            self.assertIn("暂未发现可直接匹配的Brent显著异动，需持续关注后续影响。", rendered)
            self.assertIn("查看全日重要新闻", rendered)
            self.assertNotIn('id="major-event-timeline"', rendered)
            self.assertNotIn("data-major-event-key", rendered)
            self.assertNotIn("被更高优先级事件替代", rendered)

            payload_match = re.search(
                r'<script type="application/json" id="brent-panel-data">(.*?)</script>',
                rendered,
                re.DOTALL,
            )
            self.assertIsNotNone(payload_match)
            panel_data = json.loads(payload_match.group(1))
            panel_group_keys = {item["key"] for item in panel_data["panel_groups"]}
            self.assertEqual(
                [item["title"] for item in panel_data["panel_groups"][0]["news"]],
                ["胡塞武装宣布对沙特实施海上禁令", "伊朗称维护霍尔木兹利益"],
            )
            self.assertEqual(
                [item["id"] for item in panel_data["unmatched_major_items"]],
                ["insurance"],
            )
            marker_key = re.search(
                r'<g\b(?=[^>]*\bclass="[^"]*\bnews-marker\b[^"]*")[^>]*\bdata-event-key="([^"]+)"',
                rendered,
            )
            band_key = re.search(r'<rect\b(?=[^>]*\bclass="unmatched-band-hit")[^>]*\bdata-event-key="([^"]+)"', rendered)
            self.assertIsNotNone(marker_key)
            self.assertIsNotNone(band_key)
            self.assertIn(marker_key.group(1), panel_group_keys)
            self.assertIn(band_key.group(1), panel_group_keys)
            self.assertIn("const showGroup = (key) =>", rendered)
            self.assertIn("showGroup(trigger.getAttribute('data-event-key') || '')", rendered)
            self.assertIn("const renderAllDay = () =>", rendered)
            self.assertIn("button.addEventListener('click', renderAllDay)", rendered)

    def test_formal_html_all_day_view_deduplicates_news_and_explains_count(self) -> None:
        summary = "今日展示 16 条重要新闻：均为通过硬条件的独立事实；未为凑数量加入弱相关新闻。"
        news = {
            "id": "shared",
            "time": "2026-07-20 20:10",
            "title": "胡塞武装宣布对沙特实施海上禁令",
            "summary": "胡塞方面宣布新的海上限制措施。",
            "url": "https://flash.jin10.com/detail/shared",
            "impact": "红海与海湾运输风险上升。",
            "price_status": "同期相关信息",
            "event_group_id": "event_group_001",
            "role": "primary",
            "destinations": ["价格异动组 19:30–20:25", "价格异动组 20:50–21:35"],
        }
        presentation = FormalReportPresentation(
            panel_groups=(
                {"key": "event-20260720t1930-20260720t2025", "time_range": "19:30–20:25", "news": (news,)},
                {"key": "event-20260720t2050-20260720t2135", "time_range": "20:50–21:35", "news": (news,)},
            ),
            unmatched_major_items=(),
            iran_update_items=(),
            all_day_items=(news,),
            display_summary={"text": summary, "displayed_news_count": 16},
        )

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_test.md"
            markdown.write_text(
                "# 日报\n\n"
                "统计窗口：2026-07-20 08:00 至 2026-07-21 08:00（Asia/Shanghai）\n"
                "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
                "## 本窗口Brent表现\n\n"
                "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 89.00 | -1.83% | 91.08 | 86.14 |\n",
                encoding="utf-8",
            )

            rendered = build_html_report(markdown, output_dir, presentation= presentation).read_text(encoding="utf-8")
            payload_match = re.search(
                r'<script type="application/json" id="brent-panel-data">(.*?)</script>',
                rendered,
                re.DOTALL,
            )
            self.assertIsNotNone(payload_match)
            payload = json.loads(payload_match.group(1))

            self.assertEqual([item["id"] for item in payload["all_day_items"]], ["shared"])
            self.assertEqual(sum(len(group["news"]) for group in payload["panel_groups"]), 2)
            self.assertIn(summary, rendered)
            self.assertIn("const allDayItems", rendered)
            self.assertIn("node.textContent = value || ''", rendered)
            self.assertNotIn("major-event-timeline", rendered)
            self.assertNotIn("data-major-event-key", rendered)
            self.assertNotIn("brent-major-event-data", rendered)

    def test_formal_html_renders_chinese_isw_key_takeaways(self) -> None:
        items = _isw_dashboard_items(
            {
                "status": "success",
                "title": "Iran Update, July 21, 2026",
                "url": "https://understandingwar.org/research/middle-east/iran-update-july-21-2026/",
                "source_key_takeaways": ["one", "two", "three"],
                "source_key_takeaways_count": 3,
                "translated_key_takeaways_count": 3,
                "key_takeaways_zh": [
                    "伊朗部队威胁霍尔木兹附近商船。",
                    "伊朗官员讨论制裁缓解与核谈判。",
                    "地区军事力量准备追加防空部署。",
                ],
                "supplemental_zh": ["一名油轮运营商报告海湾航线出现新的保险限制。"],
            }
        )

        self.assertEqual(len(items), 3)
        self.assertTrue(all(item["url"].startswith("https://understandingwar.org/") for item in items))
        self.assertIn("霍尔木兹", items[0]["text"])
        self.assertNotIn("保险限制", "\n".join(item["text"] for item in items))

    def test_isw_dashboard_rejects_legacy_summary_cache(self) -> None:
        items = _isw_dashboard_items(
            {
                "status": "success",
                "url": "https://understandingwar.org/research/middle-east/iran-update-july-21-2026/",
                "summary_zh": [
                    "要点一。",
                    "要点二。",
                    "要点三。",
                    "要点四。",
                    "旧版缓存中混入的正文段落。",
                ],
            }
        )

        self.assertEqual(len(items), 1)
        self.assertIn("完整性校验失败", items[0]["text"])
        self.assertNotIn("要点一。", items[0]["text"])

    def test_isw_dashboard_keeps_complete_sentences_when_concising(self) -> None:
        long_sentence = "伊朗方面称将继续推进区域军事部署" + "并评估事态升级风险" * 25 + "。"
        items = _isw_dashboard_items(
            {
                "status": "success",
                "url": "https://understandingwar.org/research/middle-east/iran-update-july-21-2026/",
                "source_key_takeaways": ["one", "two", "three"],
                "source_key_takeaways_count": 3,
                "translated_key_takeaways_count": 3,
                "key_takeaways_zh": [long_sentence, "霍尔木兹航运风险仍需跟踪。", "制裁变化可能影响出口预期。"],
            }
        )

        self.assertEqual(items[0]["text"], long_sentence)
        self.assertTrue(items[0]["text"].endswith("。"))

    def test_formal_html_keeps_every_sentence_of_validated_isw_takeaway(self) -> None:
        complete_takeaway = "第一句说明军事态势。 第二句说明航运影响！ 第三句尾部不得丢失。"
        items = _isw_dashboard_items(
            {
                "status": "success",
                "url": "https://understandingwar.org/research/middle-east/iran-update-complete/",
                "source_key_takeaways": ["one", "two", "three"],
                "source_key_takeaways_count": 3,
                "translated_key_takeaways_count": 3,
                "key_takeaways_zh": [
                    complete_takeaway,
                    "第二条完整译文。",
                    "第三条完整译文。",
                ],
            }
        )

        self.assertEqual(items[0]["text"], complete_takeaway)

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_isw_complete.md"
            markdown.write_text(
                "# 日报\n\n"
                "统计窗口：2026-08-04 08:00 至 2026-08-05 08:00（Asia/Shanghai）\n"
                "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n",
                encoding="utf-8",
            )
            presentation = FormalReportPresentation(
                panel_groups=(),
                unmatched_major_items=(),
                iran_update_items=tuple(items),
            )

            rendered = build_html_report(
                markdown,
                output_dir,
                presentation=presentation,
            ).read_text(encoding="utf-8")

        self.assertIn("第三句尾部不得丢失。", rendered)

    def test_formal_html_links_isw_listing_when_external_update_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_test.md"
            svg = output_dir / "Jin10_Brent_Rolling_Reaction_test_brent_24h.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>', encoding="utf-8")
            markdown.write_text(
                "# 日报\n\n"
                "统计窗口：2026-07-20 08:00 至 2026-07-21 08:00（Asia/Shanghai）\n"
                "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m｜ISW Iran Update（仅用于“接下来关注什么”）\n\n"
                "## 本窗口Brent表现\n\n"
                "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 89.00 | -1.83% | 91.08 | 86.14 |\n\n"
                f"![chart]({svg.name})\n\n"
                "## 本窗口Brent价格异动复盘\n\n"
                "本窗口没有满足硬门槛的核心驱动。\n\n"
                "## 市场主要驱动与下一时段判断\n\n"
                "### 本窗口市场主线\n\n"
                "Brent本窗口整体偏弱，窗口涨跌幅为-1.83%；识别到17段独立价格异动。可归因线索主要集中在油轮/货船航运安全。\n\n"
                "## 接下来关注什么\n\n"
                "### 1. 金十 + Brent 价格异动推导出的跟踪事项\n\n"
                "- 霍尔木兹是否出现实际通行限制、收费、封锁或护航安排变化；\n\n"
                "### 2. 外部网站最新 update 中文总结\n\n"
                "外部网站最新update抓取失败，本次仅基于金十新闻与Brent行情生成。\n"
                "ISW Iran Update列表：https://understandingwar.org/research/?_teams=middle-east&_focus_area_filter=iran-proxies&_product_line_filter=iran-update\n\n"
                "[查看完整 Audit →](Jin10_Brent_Rolling_Audit_test.html)\n",
                encoding="utf-8",
            )

            rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")
            self.assertIn('id="iran-update-card"', rendered)
            self.assertIn("本次ISW抓取失败，以下内容待下次运行补充。", rendered)
            self.assertIn("https://understandingwar.org/research/?_teams=middle-east", rendered)
            self.assertIn("查看ISW列表", rendered)
            self.assertIn("今日关注什么", rendered)

    def test_formal_html_excludes_news_without_valid_original_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            markdown = output_dir / "Jin10_Brent_Rolling_Reaction_test.md"
            svg = output_dir / "Jin10_Brent_Rolling_Reaction_test_brent_24h.svg"
            news_payload = html.escape(
                json.dumps(
                    [
                        {"time": "2026-07-14 13:40", "title": "有效新闻", "url": "https://flash.jin10.com/detail/1", "reaction": "上涨"},
                        {"time": "2026-07-14 13:41", "title": "无效新闻", "url": "not-a-url", "reaction": "上涨"},
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                quote=True,
            )
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60">'
                f'<g class="news-marker" tabindex="0" data-news="{news_payload}">'
                '<circle cx="60" cy="30" r="3"/></g></svg>',
                encoding="utf-8",
            )
            markdown.write_text(
                "# 日报\n\n"
                "统计窗口：2026-07-14 08:00 至 2026-07-15 08:00（Asia/Shanghai）\n"
                "数据来源：金十数据快讯｜Yahoo Finance / BZ=F / 5m\n\n"
                "## 本窗口Brent表现\n\n"
                "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |\n"
                "| ---: | ---: | ---: | ---: |\n"
                "| 85.28 | +1.42% | 87.54 | 83.41 |\n\n"
                f"![chart]({svg.name})\n\n"
                "## 本窗口Brent价格异动复盘\n\n"
                "### 北京时间 13:35–14:50｜Brent上涨 +1.57%｜最大30分钟涨幅 +1.11%\n\n"
                "**油价反应：**\n"
                "5分钟：-0.21%｜15分钟：+0.10%｜30分钟：+0.68%｜60分钟：+1.13%｜本轮累计：+1.57%\n\n"
                "**相关新闻：**\n"
                "1. 新闻摘要：有效新闻\n"
                "   原文链接：https://flash.jin10.com/detail/1\n"
                "   归因判断：主要驱动\n"
                "   置信度：高\n\n"
                "2. 新闻摘要：无效新闻\n"
                "   原文链接：not-a-url\n"
                "   归因判断：次要驱动\n"
                "   置信度：低\n\n"
                "**市场解读：**\n"
                "有效新闻更可能解释异动。\n\n"
                "## 价格异动但新闻解释不足\n\n"
                "本窗口没有需要单列的事件。\n\n"
                "## 市场主要驱动与下一时段判断\n\n"
                "### 本窗口市场主线\n\n"
                "可归因线索主要集中在霍尔木兹航运风险。\n\n"
                "## 接下来关注什么\n\n"
                "- 霍尔木兹通行。\n\n"
                "[查看完整 Audit →](Jin10_Brent_Rolling_Audit_test.html)\n",
                encoding="utf-8",
            )

            rendered = build_html_report(markdown, output_dir).read_text(encoding="utf-8")
            self.assertIn("https://flash.jin10.com/detail/1", rendered)
            self.assertNotIn("not-a-url", rendered)
            payload_match = re.search(
                r'<script type="application/json" id="brent-event-data">(.*?)</script>',
                rendered,
                re.DOTALL,
            )
            self.assertIsNotNone(payload_match)
            event_data = json.loads(payload_match.group(1))
            explained = next(item for item in event_data if item["kind"] == "explained")
            self.assertEqual([item["url"] for item in explained["news"]], ["https://flash.jin10.com/detail/1"])


if __name__ == "__main__":
    unittest.main()
