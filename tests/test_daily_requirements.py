from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from jin10_market_report.brent import BRENT_INSTRUMENT_METADATA, BrentConfig, build_yahoo_chart_url
from jin10_market_report.eventing import direct_energy_supply_reason, market_bias_for_event
from jin10_market_report.external_update import (
    _article_key_takeaways,
    _clean_source_bullet,
    _title_from_page_or_url,
    fetch_latest_isw_update,
)
from jin10_market_report.tracking_report import (
    _audit_data_integrity_lines,
    _build_blue_panel_groups,
    _build_event_grouped_homepage,
    _build_price_movement_review,
    _build_window_audit,
    _candidate_attribution,
    _degrade_movement_review_for_incomplete_data,
    _group_homepage_candidates,
    _homepage_candidate_is_eligible,
    _homepage_display_summary,
    _price_movement_review_lines,
    _price_continuity_issues,
    _quarantine_continuity_movements,
    _scan_30m_price_windows,
    beijing_day_bounds,
    brent_day_summary,
    classify_report_status,
)
from jin10_market_report import tracking_report


UTC = ZoneInfo("UTC")


def _homepage_event_candidate(
    raw_id: str,
    minute: int,
    title: str,
    body: str,
    *,
    selected: bool = False,
    major: bool = True,
) -> dict[str, object]:
    published = datetime(2026, 7, 20, 0, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return {
        "raw_id": raw_id,
        "published_dt": published,
        "row": {
            "raw_id": raw_id,
            "published_at_utc": published.isoformat().replace("+00:00", "Z"),
            "title": title,
            "body": body,
            "url": f"https://flash.jin10.com/detail/{raw_id}",
        },
        "direct_supply_path": True,
        "direct_supply_reason": "直接影响原油供应或能源运输",
        "freshness": "新增事实",
        "is_duplicate": False,
        "price_quote_only": False,
        "is_commentary": False,
        "major_reason": "命中重大主题：原油出口、港口装船、制裁或OPEC/OPEC+产量政策" if major else "",
        "_homepage_selected": selected,
        "_homepage_group_order": minute,
        "_homepage_destinations": (f"价格异动组 {minute:02d}:00–{minute:02d}:30",),
    }


class _PriceStore:
    def __init__(self, closes: list[float]) -> None:
        start = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
        self.rows = [
            {
                "ts_utc": (start + timedelta(minutes=5 * index)).isoformat().replace("+00:00", "Z"),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
            }
            for index, close in enumerate(closes)
        ]

    def brent_day_klines(self, symbol, interval, start_utc, end_utc):
        return self.rows


class DailyRequirementTests(unittest.TestCase):
    def test_external_source_integrity_audit_counts(self) -> None:
        """Changing source counts or omitting grouped source IDs breaks audit traceability."""
        external_update = {
            "status": "success",
            "source_key_takeaways": [f"Source takeaway {index}" for index in range(1, 9)],
            "source_key_takeaways_count": 8,
            "translated_key_takeaways_count": 8,
            "key_takeaways_zh": [f"中文要点{index}" for index in range(1, 9)],
        }
        trump_update = {
            "status": "success",
            "raw_topic_item_count": 4,
            "in_window_item_count": 4,
            "relevant_source_item_count": 3,
            "statement_count": 2,
            "statements": [
                {
                    "statement_id": "100",
                    "published_at_bj": "2026-08-04 11:34:54",
                    "text": "特朗普称美伊谈判仍在进行。",
                    "topics": ["伊朗", "核谈判"],
                    "source_items": [
                        {"id": 100, "published_at_bj": "2026-08-04 11:34:54", "source_url": "https://xnews.jin10.com/details/100"},
                        {"id": 101, "published_at_bj": "2026-08-04 11:43:35", "source_url": "https://xnews.jin10.com/details/101"},
                    ],
                },
                {
                    "statement_id": "102",
                    "published_at_bj": "2026-08-04 19:43:35",
                    "text": "特朗普要求石油企业降低油价。",
                    "topics": ["油价"],
                    "source_items": [
                        {"id": 102, "published_at_bj": "2026-08-04 19:43:35", "source_url": "https://xnews.jin10.com/details/102"},
                    ],
                },
            ],
        }

        audit = "\n".join(
            tracking_report._external_source_integrity_lines(external_update, trump_update)
        )

        for expected in (
            "ISW Key Takeaways原文数量：8",
            "ISW Key Takeaways翻译数量：8",
            "ISW Key Takeaways正文渲染数量：8",
            "特朗普专栏窗口内条目数量：4",
            "特朗普相关来源条目数量：3",
            "特朗普独立表态数量：2",
            "表态ID：100",
            "表态ID：102",
            "来源ID：100",
            "来源ID：101",
            "来源ID：102",
        ):
            self.assertIn(expected, audit)

    def test_isw_strict_output_rejects_malformed_and_legacy_success_payloads(self) -> None:
        """Only one-to-one official Key Takeaways may reach Markdown, dashboard, or rendered Audit counts."""
        malformed = {
            "status": "success",
            "title": "Iran Update, August 4, 2026",
            "published_at_et": "2026-08-04 18:30",
            "published_at_bj": "2026-08-05 06:30",
            "url": "https://understandingwar.org/research/middle-east/iran-update-august-4-2026/",
            "source_key_takeaways": ["source one", "source two"],
            "key_takeaways_zh": ["不应渲染的单条翻译"],
            "source_key_takeaways_count": 2,
            "translated_key_takeaways_count": 1,
        }
        legacy = {
            "status": "success",
            "title": "Legacy Iran Update",
            "url": "https://understandingwar.org/research/middle-east/legacy/",
            "summary_zh": ["旧版正文不应渲染"],
        }
        trump_update = {"status": "failed", "failure_reason": "test"}

        for payload, forbidden in (
            (malformed, "不应渲染的单条翻译"),
            (legacy, "旧版正文不应渲染"),
        ):
            markdown = "\n".join(tracking_report._tracking_outlook_lines(payload))
            dashboard = tracking_report._isw_dashboard_items(payload)
            audit = "\n".join(
                tracking_report._external_source_integrity_lines(payload, trump_update)
            )

            self.assertIn("完整性校验失败", markdown)
            self.assertIn(str(payload["title"]), markdown)
            self.assertIn(str(payload["url"]), markdown)
            self.assertNotIn(forbidden, markdown)
            self.assertNotRegex(markdown, r"(?m)^\d+\. ")
            self.assertEqual(len(dashboard), 1)
            self.assertIn("完整性校验失败", dashboard[0]["text"])
            self.assertNotIn(forbidden, dashboard[0]["text"])
            self.assertIn("ISW Key Takeaways正文渲染数量：0", audit)

    def test_isw_integrity_failure_does_not_claim_transport_failure(self) -> None:
        """A successful fetch with invalid counts must remain distinct from a transport failure."""
        malformed_success = {
            "status": "success",
            "fetch_method": "官网直连",
            "title": "Iran Update, August 4, 2026",
            "published_at_et": "2026-08-04 18:30",
            "published_at_bj": "2026-08-05 06:30",
            "url": "https://understandingwar.org/research/middle-east/iran-update-august-4-2026/",
            "source_key_takeaways": ["source one", "source two"],
            "key_takeaways_zh": ["单条翻译"],
            "source_key_takeaways_count": 2,
            "translated_key_takeaways_count": 1,
        }
        actual_failure = {
            "status": "failed",
            "failure_reason": "network timeout",
            "listing_url": "https://understandingwar.org/research/",
        }
        empty_review = {"groups": [], "windows": []}

        malformed_markdown = "\n".join(
            tracking_report._tracking_outlook_lines(malformed_success)
        )
        malformed_audit = "\n".join(
            tracking_report._new_requirement_audit_lines(
                empty_review, malformed_success, "Asia/Shanghai"
            )
        )
        failed_markdown = "\n".join(
            tracking_report._tracking_outlook_lines(actual_failure)
        )
        failed_audit = "\n".join(
            tracking_report._new_requirement_audit_lines(
                empty_review, actual_failure, "Asia/Shanghai"
            )
        )

        self.assertIn("抓取成功，但 ISW Key Takeaways 完整性校验失败", malformed_markdown)
        self.assertNotIn("指定网站本次抓取失败", malformed_markdown)
        self.assertNotRegex(malformed_markdown, r"(?m)^\d+\. ")
        self.assertIn("外部网站是否抓取成功：是", malformed_audit)
        self.assertIn("ISW Key Takeaways完整性校验：失败", malformed_audit)
        self.assertIn("原文、翻译及声明数量必须为正数且完全一致", malformed_audit)
        self.assertNotIn("外部update失败原因：未知", malformed_audit)

        self.assertIn("指定网站本次抓取失败", failed_markdown)
        self.assertIn("network timeout", failed_markdown)
        self.assertNotIn("抓取成功，但", failed_markdown)
        self.assertIn("外部网站是否抓取成功：否", failed_audit)
        self.assertIn("外部update失败原因：network timeout", failed_audit)

    def test_news_direction_uses_new_fact_instead_of_price_or_risk_keyword(self) -> None:
        denial = market_bias_for_event(
            "伊拉克组织否认袭击沙特石油设施",
            "该组织否认发动无人机袭击，目前未确认供应中断。",
            "地缘冲突",
            pct_30m=0.01,
        )
        recovery = market_bias_for_event(
            "利比亚油田恢复生产",
            "遇袭后生产已恢复，供应已恢复。",
            "原油供应",
        )
        disruption = market_bias_for_event(
            "原油码头遭到袭击",
            "装载暂停，出口中断。",
            "原油供应",
        )
        mixed = market_bias_for_event(
            "原油码头部分恢复",
            "生产已恢复，但装载仍然暂停。",
            "原油供应",
        )

        self.assertTrue(denial.startswith("中性"))
        self.assertTrue(recovery.startswith("偏空"))
        self.assertTrue(disruption.startswith("偏多"))
        self.assertTrue(mixed.startswith("中性"))

    def test_neutral_news_cannot_become_secondary_driver(self) -> None:
        start = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)
        candidate = {
            "row": {"title": "霍尔木兹海峡管理提议"},
            "direct_supply_path": True,
            "freshness": "新增事实",
            "relation": "新闻先于价格",
            "brent_transmission": "直接",
            "score": 100,
            "direction_alignment": 0,
            "confidence": "高",
            "published_dt": start,
        }
        group = {
            "start_dt": start + timedelta(minutes=5),
            "end_dt": start + timedelta(minutes=35),
        }

        result = _candidate_attribution(candidate, group, "Asia/Shanghai")

        self.assertEqual(result["judgment"], "背景信息")
        self.assertIn("中性或无法判断", result["reason"])

    def test_delayed_0840_run_keeps_scheduled_0800_cutoff(self) -> None:
        start, end, label = beijing_day_bounds(
            datetime(2026, 7, 24, 8, 40, tzinfo=ZoneInfo("Asia/Shanghai")),
            "Asia/Shanghai",
        )

        self.assertEqual(
            start,
            datetime(2026, 7, 23, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(
            end,
            datetime(2026, 7, 24, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(label, "20260724_0800")

    def test_window_audit_stops_at_scheduled_0800(self) -> None:
        start = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=1)
        stored_rows = [
            {
                "ts_utc": (start + timedelta(minutes=5 * index))
                .isoformat()
                .replace("+00:00", "Z"),
                "close": 90.0,
            }
            for index in range((24 * 60) // 5)
        ]
        stored_rows.append(
            {
                "ts_utc": (end + timedelta(minutes=60))
                .isoformat()
                .replace("+00:00", "Z"),
                "close": 120.0,
            }
        )

        class Store:
            price_calls: list[tuple[datetime, datetime]] = []

            def raw_news_between(self, *_args):
                return [
                    {
                        "raw_id": raw_id,
                        "published_at_utc": published.isoformat().replace("+00:00", "Z"),
                        "title": "窗口新闻",
                        "body": "普通新闻",
                        "detail": "",
                        "is_summary": 0,
                    }
                    for raw_id, published in [
                        ("raw-start", start + timedelta(minutes=5)),
                        ("raw-evening", start + timedelta(hours=9)),
                        ("raw-night", start + timedelta(hours=17)),
                        ("raw-end", end - timedelta(minutes=5)),
                    ]
                ]

            def news_for_reaction_range(self, *_args):
                return [
                    {
                        "published_at_utc": (end - timedelta(minutes=11))
                        .isoformat()
                        .replace("+00:00", "Z")
                    }
                ]

            def brent_day_klines(self, _symbol, _interval, range_start, range_end):
                self.price_calls.append((range_start, range_end))
                return [
                    row
                    for row in stored_rows
                    if range_start
                    <= datetime.fromisoformat(str(row["ts_utc"]).replace("Z", "+00:00"))
                    < range_end
                ]

            def reaction_rows_between(self, *_args):
                return []

            def raw_news_near(self, *_args, **_kwargs):
                return []

        store = Store()
        audit = _build_window_audit(store, BrentConfig(), start, end, "Asia/Shanghai")

        self.assertEqual(store.price_calls[0], (start, end))
        self.assertEqual(audit["price_start_required"], start)
        self.assertEqual(audit["price_end_required"], end)
        self.assertEqual(audit["last_price"], end - timedelta(minutes=5))
        self.assertTrue(audit["price_boundary_covered"])
        self.assertTrue(audit["price_covered"])
        self.assertNotIn("latest_news_plus60_covered", audit)
        self.assertFalse(
            any("60分钟价格观察期" in item or "09:05" in item for item in audit["coverage_gaps"])
        )
        self.assertEqual(max(row["close"] for row in audit["price_rows"]), 90.0)

    def test_0749_news_is_pending_and_preserved(self) -> None:
        start = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=1)
        published = end - timedelta(minutes=11)
        raw = {
            "raw_id": "news-0749",
            "published_at_utc": published.isoformat().replace("+00:00", "Z"),
            "title": "霍尔木兹海峡油轮通行受阻",
            "body": "油轮通行受阻，原油运输面临中断风险。",
            "detail": "",
            "url": "https://flash.jin10.com/detail/news-0749",
            "is_summary": 0,
        }
        candidate = {
            "raw_id": "news-0749",
            "row": raw,
            "published_dt": published,
            "score": 100,
            "direct": True,
            "oil_reason": "直接影响原油运输",
            "major_reason": "霍尔木兹通行风险",
            "direct_supply_path": True,
            "direct_supply_reason": "直接影响原油运输",
            "event_key": "hormuz_shipping",
            "event_topic": "霍尔木兹航运",
            "source_label": "官方/权威机构",
            "relation": "新闻先于价格",
            "macro_reason": "",
            "price_quote_only": False,
            "is_commentary": False,
            "is_summary": False,
            "freshness": "新增事实",
            "isolated_lpg_cp": False,
            "brent_transmission": "直接",
            "tier": 1,
            "direction_alignment": 1,
            "logic": "航运受阻抬升原油供应风险溢价。",
            "confidence": "高",
            "reasons": "直接供应路径",
        }
        group = {
            "start_dt": end - timedelta(minutes=30),
            "end_dt": end - timedelta(minutes=5),
            "search_start_dt": end - timedelta(minutes=45),
            "search_end_dt": end,
            "direction": "上涨",
            "cumulative_pct": 0.006,
            "max_30m_pct": 0.006,
        }

        class Store:
            price_calls: list[tuple[datetime, datetime]] = []

            def brent_day_klines(self, _symbol, _interval, range_start, range_end):
                self.price_calls.append((range_start, range_end))
                return []

            def reaction_rows_between(self, *_args):
                return [
                    {
                        "news_id": "news-0749",
                        "is_duplicate": 0,
                        "price_status": "ok",
                        "event_base_close": 100.0,
                        "price_5m": 100.2,
                        "price_15m": 100.4,
                        "price_30m": 100.6,
                        "price_60m": 100.8,
                    }
                ]

            def raw_news_between(self, *_args):
                return [raw]

        attribution = {
            "judgment": "主要驱动",
            "freshness": "新增事实",
            "relation": "新闻先于价格",
            "confidence": "高",
            "reason": "新闻方向与价格方向一致。",
        }
        store = Store()
        with (
            patch(
                "jin10_market_report.tracking_report._scan_30m_price_windows",
                return_value=([{"pct": 0.006}], []),
            ),
            patch(
                "jin10_market_report.tracking_report._merge_price_windows",
                return_value=[group],
            ),
            patch(
                "jin10_market_report.tracking_report._score_movement_candidate",
                return_value=candidate,
            ),
            patch(
                "jin10_market_report.tracking_report._candidate_attribution",
                return_value=attribution,
            ),
        ):
            review = _build_price_movement_review(
                store,
                BrentConfig(),
                start,
                end,
                "Asia/Shanghai",
            )

        result_group = review["groups"][0]
        result_candidate = result_group["candidates"][0]
        self.assertEqual(store.price_calls[0], (start, end))
        self.assertEqual(result_candidate["reaction_status"], "PENDING")
        self.assertIn(result_candidate, result_group["selected_news"])
        self.assertIn(result_candidate, result_group["homepage_news"])
        self.assertIsNone(result_group["core_driver"])
        self.assertEqual(result_group["attribution_judgment"], "暂无法确认")
        self.assertEqual(
            result_candidate["reaction_status_note"],
            "新闻已纳入，但截至08:00后续60分钟价格观察尚未完成，暂不做最终归因。",
        )

    def test_pending_reaction_does_not_downgrade_report(self) -> None:
        start = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=1)
        published = end - timedelta(minutes=11)

        class Store:
            def raw_news_between(self, *_args):
                return [
                    {
                        "raw_id": raw_id,
                        "published_at_utc": timestamp.isoformat().replace("+00:00", "Z"),
                        "title": "窗口新闻",
                        "body": "普通新闻",
                        "detail": "",
                        "is_summary": 0,
                    }
                    for raw_id, timestamp in [
                        ("start", start + timedelta(minutes=5)),
                        ("evening", start + timedelta(hours=9)),
                        ("night", start + timedelta(hours=17)),
                        ("end", end - timedelta(minutes=5)),
                    ]
                ]

            def news_for_reaction_range(self, *_args):
                return [
                    {
                        "published_at_utc": published.isoformat().replace("+00:00", "Z"),
                    }
                ]

            def brent_day_klines(self, *_args):
                return [
                    {
                        "ts_utc": (start + timedelta(minutes=5 * index))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "close": 100.0,
                    }
                    for index in range((24 * 60) // 5)
                ]

            def reaction_rows_between(self, *_args):
                return [
                    {
                        "news_id": "pending",
                        "published_at_utc": published.isoformat().replace("+00:00", "Z"),
                        "is_duplicate": 0,
                        "price_status": "ok",
                        "event_base_close": 100.0,
                        "pre30_close": 99.8,
                        "price_5m": 100.1,
                        "price_15m": 100.2,
                        "price_30m": 100.3,
                        "price_60m": 100.4,
                    }
                ]

            def raw_news_near(self, *_args, **_kwargs):
                return []

        audit = _build_window_audit(
            Store(),
            BrentConfig(),
            start,
            end,
            "Asia/Shanghai",
        )
        status, _ = classify_report_status(audit)

        self.assertEqual(audit["reaction_status_counts"]["PENDING"], 1)
        self.assertEqual(audit["reaction_status_counts"]["CONFIRMED"], 0)
        self.assertTrue(audit["price_covered"])
        self.assertEqual(audit["data_integrity"], "完整")
        self.assertEqual(status, "FORMAL")

    def test_bz_f_instrument_metadata_is_fixed(self) -> None:
        self.assertEqual(
            BRENT_INSTRUMENT_METADATA,
            {
                "instrument_name": "Brent Crude Oil Last Day Financ",
                "source": "Yahoo Finance",
                "ticker": "BZ=F",
                "contract_month": "",
                "contract_type": "Yahoo Finance BZ=F",
            },
        )

    def test_extreme_prices_remain_in_summary_metrics(self) -> None:
        start = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)

        class Store:
            def brent_day_klines(self, *_args):
                return [
                    {"open": 87.17, "high": 97.53, "low": 86.06, "close": 87.17},
                    {"open": 87.17, "high": 102.01, "low": 87.17, "close": 100.34},
                ]

        summary = brent_day_summary(
            Store(),
            BrentConfig(),
            start,
            start + timedelta(days=1),
        )

        self.assertEqual(summary["latest"], "100.34")
        self.assertEqual(summary["high"], "102.01")
        self.assertEqual(summary["low"], "86.06")
        self.assertEqual(summary["pct"], "+15.11%")

    def test_window_audit_reports_timestamp_quality(self) -> None:
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=1)
        price_start = start - timedelta(minutes=30)
        price_end = end + timedelta(minutes=65)
        timestamps = []
        current = price_start
        while current <= price_end:
            timestamps.append(current)
            current += timedelta(minutes=5)
        del timestamps[3:5]

        class Store:
            def raw_news_between(self, *_args):
                return [
                    {
                        "raw_id": raw_id,
                        "published_at_utc": published.isoformat().replace("+00:00", "Z"),
                        "title": "窗口新闻",
                        "body": "普通新闻",
                        "detail": "",
                        "is_summary": 0,
                    }
                    for raw_id, published in [
                        ("raw-start", start + timedelta(minutes=5)),
                        ("raw-evening", start + timedelta(hours=9)),
                        ("raw-night", start + timedelta(hours=17)),
                        ("raw-end", end - timedelta(minutes=5)),
                    ]
                ]

            def news_for_reaction_range(self, *_args):
                return [
                    {
                        "published_at_utc": (end - timedelta(hours=1))
                        .isoformat()
                        .replace("+00:00", "Z")
                    }
                ]

            def brent_day_klines(self, *_args):
                return [
                    {
                        "ts_utc": timestamp.isoformat().replace("+00:00", "Z"),
                        "close": 90.0,
                    }
                    for timestamp in timestamps
                ]

            def reaction_rows_between(self, *_args):
                return []

            def raw_news_near(self, *_args, **_kwargs):
                return []

        audit = _build_window_audit(Store(), BrentConfig(), start, end, "Asia/Shanghai")
        status, _ = classify_report_status(audit)

        self.assertEqual(audit["duplicate_timestamp_count"], 0)
        self.assertTrue(audit["timestamps_ordered"])
        self.assertEqual(audit["price_gap_count"], 1)
        self.assertEqual(audit["max_price_gap_minutes"], 15)
        self.assertEqual(audit["material_price_gap_count"], 1)
        self.assertTrue(audit["price_covered"])
        self.assertEqual(audit["price_freshness"], "passed")
        self.assertEqual(status, "FORMAL")

    def test_brent_incremental_fetch_reloads_thirty_minutes(self) -> None:
        latest = datetime(2026, 7, 29, 0, 30, tzinfo=UTC)
        now = datetime(2026, 7, 29, 0, 35, tzinfo=UTC)

        with patch("jin10_market_report.brent.utc_now", return_value=now):
            url = build_yahoo_chart_url(BrentConfig(), latest)

        query = parse_qs(urlparse(url).query)
        self.assertEqual(
            int(query["period1"][0]),
            int((latest - timedelta(minutes=30)).timestamp()),
        )

    def test_continuity_notice_preserves_extreme_movements(self) -> None:
        start = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
        clean = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=30),
            "attribution_judgment": "主要驱动",
            "core_driver": {"raw_id": "clean"},
            "selected_news": [{"raw_id": "clean"}],
            "homepage_news": [{"raw_id": "clean"}],
            "cumulative_pct": 0.006,
        }
        extreme_move = {
            "start_dt": start + timedelta(minutes=30),
            "end_dt": start + timedelta(minutes=75),
            "attribution_judgment": "暂无法确认",
            "core_driver": None,
            "selected_news": [{"raw_id": "extreme-news"}],
            "homepage_news": [{"raw_id": "extreme-news"}],
            "cumulative_pct": -0.11,
        }
        clean_window = {"start_dt": start, "end_dt": start + timedelta(minutes=30), "pct": 0.006}
        extreme_window = {
            "start_dt": start + timedelta(minutes=35),
            "end_dt": start + timedelta(minutes=65),
            "pct": -0.11,
        }
        review = {
            "groups": [clean, extreme_move],
            "unexplained": [extreme_move],
            "windows": [clean_window, extreme_window],
            "selected_raw_ids": {"clean", "extreme-news"},
            "core_raw_ids": {"clean"},
        }
        audit = {
            "price_continuity_issues": [
                {
                    "start_utc": start + timedelta(minutes=40),
                    "end_utc": start + timedelta(minutes=45),
                    "start_close": 94.91,
                    "end_close": 84.39,
                    "pct": -0.1108,
                }
            ]
        }

        result = _quarantine_continuity_movements(review, audit)

        self.assertEqual(result["groups"], [clean, extreme_move])
        self.assertEqual(result["windows"], [clean_window, extreme_window])
        self.assertEqual(result["unexplained"], [extreme_move])
        self.assertEqual(result["quarantined_groups"], [])
        self.assertEqual(result["quarantined_windows"], [])
        self.assertEqual(extreme_move["selected_news"], [{"raw_id": "extreme-news"}])
        self.assertEqual(extreme_move["homepage_news"], [{"raw_id": "extreme-news"}])
        self.assertEqual(result["selected_raw_ids"], {"clean", "extreme-news"})
        self.assertEqual(result["core_raw_ids"], {"clean"})

    def test_partial_preserves_qualified_news_and_windows(self) -> None:
        candidate = {
            "raw_id": "news-1",
            "attribution_judgment": "主要驱动",
            "attribution_confidence": "高",
        }
        group = {
            "candidates": [candidate],
            "core_driver": candidate,
            "secondary_drivers": [],
            "selected_news": [candidate],
            "homepage_news": [candidate],
            "pricing_status": "已计价",
            "attribution_judgment": "主要驱动",
            "attribution_confidence": "高",
            "attribution_reason": "测试归因",
            "attribution_freshness": "新增事实",
            "attribution_relation": "前置",
        }
        window = {"pct": 0.1511}
        review = {
            "groups": [group],
            "windows": [window],
            "unexplained": [],
            "selected_raw_ids": {"news-1"},
            "core_raw_ids": {"news-1"},
        }

        result = _degrade_movement_review_for_incomplete_data(
            review,
            {"data_complete": False},
        )

        self.assertEqual(result["groups"], [group])
        self.assertEqual(result["windows"], [window])
        self.assertEqual(result["groups"][0]["selected_news"], [candidate])
        self.assertEqual(result["groups"][0]["homepage_news"], [candidate])
        self.assertEqual(result["selected_raw_ids"], {"news-1"})
        self.assertEqual(result["core_raw_ids"], set())
        self.assertEqual(result["unexplained"], [])
        self.assertEqual(result["groups"][0]["attribution_judgment"], "暂无法确认")
        self.assertEqual(result["groups"][0]["attribution_confidence"], "低")

    def test_extreme_jump_is_diagnostic_not_status_gate(self) -> None:
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=1)

        class Store:
            def raw_news_between(self, *_args):
                rows = []
                for raw_id, published in [
                    ("raw-start", start + timedelta(minutes=5)),
                    ("raw-evening", start + timedelta(hours=9)),
                    ("raw-night", start + timedelta(hours=17)),
                    ("raw-end", end - timedelta(minutes=5)),
                ]:
                    rows.append(
                        {
                            "raw_id": raw_id,
                            "published_at_utc": published.isoformat().replace("+00:00", "Z"),
                            "title": "窗口新闻",
                            "body": "普通新闻",
                            "detail": "",
                            "is_summary": 0,
                        }
                    )
                return rows

            def news_for_reaction_range(self, *_args):
                return [
                    {
                        "published_at_utc": (end - timedelta(hours=1))
                        .isoformat()
                        .replace("+00:00", "Z")
                    }
                ]

            def brent_day_klines(self, *_args):
                return [
                    {
                        "ts_utc": start.isoformat().replace("+00:00", "Z"),
                        "close": 87.17,
                    },
                    *[
                    {
                        "ts_utc": (start + timedelta(minutes=5 * index))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "close": 100.34,
                    }
                    for index in range(1, (24 * 60) // 5)
                    ],
                ]

            def reaction_rows_between(self, *_args):
                return []

            def raw_news_near(self, *_args, **_kwargs):
                return []

        audit = _build_window_audit(Store(), BrentConfig(), start, end, "Asia/Shanghai")
        status, _ = classify_report_status(audit)

        self.assertFalse(audit["price_continuity_ok"])
        self.assertTrue(audit["price_covered"])
        self.assertEqual(audit["price_freshness"], "passed")
        self.assertEqual(status, "FORMAL")

    def test_price_continuity_flags_short_extreme_jump_but_not_long_gap(self) -> None:
        start = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        rows = [
            {"ts_utc": start.isoformat().replace("+00:00", "Z"), "close": 94.91},
            {
                "ts_utc": (start + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
                "close": 84.39,
            },
            {
                "ts_utc": (start + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                "close": 93.94,
            },
        ]

        issues = _price_continuity_issues(rows)

        self.assertEqual(len(issues), 1)
        self.assertAlmostEqual(float(issues[0]["pct"]), (84.39 - 94.91) / 94.91)

    def test_window_audit_discloses_event_gap_without_downgrading_complete_boundaries(self) -> None:
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=1)

        class Store:
            def raw_news_between(self, *_args):
                rows = []
                for raw_id, published in [
                    ("raw-start", start + timedelta(minutes=5)),
                    ("raw-evening", start + timedelta(hours=9)),
                    ("raw-night", start + timedelta(hours=17)),
                    ("raw-end", end - timedelta(minutes=5)),
                ]:
                    rows.append(
                        {
                            "raw_id": raw_id,
                            "published_at_utc": published.isoformat().replace("+00:00", "Z"),
                            "title": "窗口新闻",
                            "body": "普通新闻",
                            "detail": "",
                            "is_summary": 0,
                        }
                    )
                return rows

            def news_for_reaction_range(self, *_args):
                return [{"published_at_utc": (end - timedelta(hours=1)).isoformat().replace("+00:00", "Z")}]

            def brent_day_klines(self, *_args):
                return [
                    {
                        "ts_utc": (start + timedelta(minutes=5 * index))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "close": 90.0,
                    }
                    for index in range((24 * 60) // 5)
                ]

            def reaction_rows_between(self, *_args):
                return [
                    {
                        "news_id": "gap-event",
                        "is_duplicate": 0,
                        "published_at_utc": (start + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
                        "event_base_close": 90.0,
                        "pre30_close": 89.8,
                        "price_5m": 90.1,
                        "price_15m": 90.2,
                        "price_30m": None,
                        "price_60m": 90.3,
                        "price_missing_reason": "事件后30分钟价格缺失：中间K线断点超过10分钟。",
                    }
                ]

            def raw_news_near(self, *_args, **_kwargs):
                return []

        audit = _build_window_audit(Store(), BrentConfig(), start, end, "Asia/Shanghai")
        status, _ = classify_report_status(audit)
        rendered = "\n".join(_audit_data_integrity_lines(audit, start, end, "Asia/Shanghai"))

        self.assertTrue(audit["price_boundary_covered"])
        self.assertEqual(audit["price_missing_count"], 1)
        self.assertEqual(audit["price_point_missing_counts"]["30m"], 1)
        self.assertEqual(status, "FORMAL")
        self.assertIn("逐事件价格不完整数量：1/1", rendered)
        self.assertIn("事件后30分钟价格缺失", rendered)
        self.assertNotIn("新闻、价格或接口缺口：未发现", rendered)

    def test_window_audit_requires_both_price_guard_boundaries(self) -> None:
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=1)

        class Store:
            def raw_news_between(self, *_args):
                return []

            def news_for_reaction_range(self, *_args):
                return []

            def brent_day_klines(self, *_args):
                return [
                    {
                        "ts_utc": (start + timedelta(hours=12))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "close": 90.0,
                    }
                ]

            def reaction_rows_between(self, *_args):
                return []

            def raw_news_near(self, *_args, **_kwargs):
                return []

        audit = _build_window_audit(Store(), BrentConfig(), start, end, "Asia/Shanghai")

        self.assertFalse(audit["price_start_covered"])
        self.assertFalse(audit["price_end_covered"])
        self.assertFalse(audit["price_boundary_covered"])
        self.assertFalse(audit["price_covered"])
        self.assertTrue(any("统计窗口起点未覆盖" in item for item in audit["coverage_gaps"]))
        self.assertTrue(any("统计窗口截止未覆盖" in item for item in audit["coverage_gaps"]))

    def test_homepage_event_group_merges_repeated_sanctions_restatements(self) -> None:
        candidates = [
            _homepage_event_candidate(
                "sanction-first",
                0,
                "美国宣布制裁北方石油公司",
                "美国宣布对北方石油公司实施能源制裁，限制其原油出口。",
            ),
            _homepage_event_candidate(
                "sanction-restatement",
                45,
                "美方重申对北方石油公司的制裁决定",
                "媒体转述美国针对北方石油公司的原油出口制裁决定。",
            ),
            _homepage_event_candidate(
                "sanction-wording",
                90,
                "北方石油公司遭美国能源制裁",
                "美国对北方石油公司的原油出口制裁没有新增执行变化。",
            ),
        ]

        groups = _group_homepage_candidates(candidates, "Asia/Shanghai")

        self.assertEqual(len(groups), 1)
        self.assertEqual([item["raw_id"] for item in groups[0]["representatives"]], ["sanction-first"])
        self.assertEqual(
            [item["role"] for item in groups[0]["members"]],
            ["primary", "duplicate_or_context", "duplicate_or_context"],
        )

    def test_homepage_event_group_keeps_sanction_effective_as_material_escalation(self) -> None:
        candidates = [
            _homepage_event_candidate(
                "sanction-announced",
                0,
                "美国宣布制裁北方石油公司",
                "美国宣布将限制北方石油公司的原油出口。",
            ),
            _homepage_event_candidate(
                "sanction-effective",
                120,
                "美国对北方石油公司的制裁正式生效",
                "制裁已正式生效并开始限制该公司的原油出口结算。",
            ),
        ]

        groups = _group_homepage_candidates(candidates, "Asia/Shanghai")

        self.assertEqual(
            [item["raw_id"] for item in groups[0]["representatives"]],
            ["sanction-announced", "sanction-effective"],
        )
        self.assertEqual(groups[0]["members"][1]["role"], "material_escalation")

    def test_homepage_event_group_keeps_port_closure_and_later_recovery(self) -> None:
        candidates = [
            _homepage_event_candidate(
                "port-risk",
                0,
                "杰伊汉港面临原油装船中断风险",
                "杰伊汉港天气恶化，原油装船可能受影响。",
            ),
            _homepage_event_candidate(
                "port-closed",
                90,
                "杰伊汉港关闭并暂停原油装船",
                "杰伊汉港已经关闭，原油装船实际中断。",
            ),
            _homepage_event_candidate(
                "port-reopened",
                300,
                "杰伊汉港重新开放并恢复原油装船",
                "杰伊汉港已重开，原油装船和出口恢复。",
            ),
        ]

        groups = _group_homepage_candidates(candidates, "Asia/Shanghai")

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            [item["raw_id"] for item in groups[0]["representatives"]],
            ["port-risk", "port-closed", "port-reopened"],
        )
        self.assertEqual(
            [item["role"] for item in groups[0]["members"]],
            ["primary", "material_escalation", "material_escalation"],
        )

    def test_homepage_event_group_merges_kazakhstan_black_sea_supply_updates(self) -> None:
        planned = _homepage_event_candidate(
            "kazakhstan-plan",
            0,
            "哈萨克斯坦将在油轮遭袭后停止向黑海输送石油",
            "哈萨克斯坦将在油轮遭袭后停止向黑海输送石油。",
        )
        detailed = _homepage_event_candidate(
            "kazakhstan-detail",
            45,
            "油轮遇袭后，哈萨克斯坦将暂停向黑海输送石油",
            "哈萨克斯坦计划暂停向俄罗斯黑海沿岸港口输送原油，里海管道联盟码头已停止接收。",
        )
        effective = _homepage_event_candidate(
            "kazakhstan-effective",
            180,
            "里海管道财团已暂停在黑海终端接收哈萨克斯坦石油",
            "由于油轮遭袭，里海管道财团已暂停在黑海终端接收来自哈萨克斯坦的石油。",
        )
        planned["event_key"] = "crude_oil_market"
        detailed["event_key"] = "tanker_shipping"
        effective["event_key"] = "crude_oil_market"

        groups = _group_homepage_candidates([planned, detailed, effective], "Asia/Shanghai")

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            [item["raw_id"] for item in groups[0]["representatives"]],
            ["kazakhstan-detail", "kazakhstan-effective"],
        )
        self.assertEqual(groups[0]["members"][2]["role"], "material_escalation")

    def test_homepage_event_group_does_not_merge_different_countries_or_ports(self) -> None:
        candidates = [
            _homepage_event_candidate(
                "turkey-port",
                0,
                "土耳其杰伊汉港暂停原油装船",
                "土耳其杰伊汉港关闭，原油出口装船中断。",
            ),
            _homepage_event_candidate(
                "libya-port",
                30,
                "利比亚锡德拉港暂停原油装船",
                "利比亚锡德拉港关闭，原油出口装船中断。",
            ),
        ]

        groups = _group_homepage_candidates(candidates, "Asia/Shanghai")

        self.assertEqual(len(groups), 2)

    def test_homepage_event_group_does_not_merge_distinct_incidents_on_same_route(self) -> None:
        candidates = [
            _homepage_event_candidate(
                "ship-fire",
                0,
                "霍尔木兹海峡一船只起火",
                "英国海上贸易行动办公室称，一艘船在库姆扎尔附近起火，与伊朗隔海相望。",
            ),
            _homepage_event_candidate(
                "tankers-exploded",
                90,
                "伊朗称两艘油轮在霍尔木兹海峡发生爆炸",
                "伊朗革命卫队称，两艘油轮在南部航道发生爆炸并被阻止继续航行。",
            ),
        ]

        groups = _group_homepage_candidates(candidates, "Asia/Shanghai")

        self.assertEqual(len(groups), 2)

    def test_homepage_event_group_does_not_merge_distinct_us_iran_actions(self) -> None:
        candidates = [
            _homepage_event_candidate(
                "us-strike",
                0,
                "美国完成对伊朗的新一轮打击",
                "美国军方打击伊朗境内军事目标，以削弱伊朗威胁霍尔木兹海峡商业航运的能力。",
            ),
            _homepage_event_candidate(
                "ceasefire-proposal",
                120,
                "伊朗提出十天停火方案",
                "伊朗向美国提出十天停火方案，希望就霍尔木兹海峡通行问题达成协议。",
            ),
            _homepage_event_candidate(
                "shipping-threat",
                240,
                "美国警告伊朗不得袭击商业船只",
                "美国称如果伊朗在霍尔木兹海峡袭击商业船只，将采取更强烈的反击。",
            ),
        ]
        for candidate in candidates:
            candidate["event_key"] = "iran_geopolitical"

        groups = _group_homepage_candidates(candidates, "Asia/Shanghai")

        self.assertEqual(len(groups), 3)

    def test_homepage_event_group_does_not_merge_through_bridge_candidate(self) -> None:
        candidates = [
            _homepage_event_candidate("first", 0, "第一项独立事实", "第一项独立事实。"),
            _homepage_event_candidate("bridge", 60, "宽泛中间报道", "宽泛中间报道。"),
            _homepage_event_candidate("third", 120, "第三项独立事实", "第三项独立事实。"),
        ]

        def matches(left: dict[str, object], right: dict[str, object]) -> bool:
            pair = {str(left["raw_id"]), str(right["raw_id"])}
            return pair in ({"first", "bridge"}, {"bridge", "third"})

        with patch("jin10_market_report.tracking_report._homepage_events_match", side_effect=matches):
            groups = _group_homepage_candidates(candidates, "Asia/Shanghai")

        self.assertEqual(
            [[item["raw_id"] for item in group["members"]] for group in groups],
            [["first", "bridge"], ["third"]],
        )

    def test_homepage_event_group_traces_merged_selected_news(self) -> None:
        candidates = [
            _homepage_event_candidate(
                "primary",
                0,
                "美国宣布制裁北方石油公司",
                "美国宣布限制北方石油公司的原油出口。",
            ),
            _homepage_event_candidate(
                "selected-repeat",
                30,
                "北方石油公司遭美国制裁",
                "美国制裁北方石油公司，限制其原油出口。",
                selected=True,
            ),
        ]

        groups = _group_homepage_candidates(candidates, "Asia/Shanghai")
        merged = next(item for item in groups[0]["members"] if item["raw_id"] == "selected-repeat")

        self.assertFalse(merged["is_displayed"])
        self.assertEqual(merged["represented_by_news_id"], "primary")
        self.assertTrue(merged["was_selected_news"])
        self.assertNotIn("排除", merged["reason"])

    def test_homepage_display_summary_has_no_floor_or_ceiling(self) -> None:
        sixteen = [
            _homepage_event_candidate(
                f"port-{index:02d}",
                index,
                f"国家{index:02d} PORT{index:02d}港暂停原油装船",
                f"PORT{index:02d}港关闭，原油出口装船中断。",
            )
            for index in range(16)
        ]
        thirty_five = [
            _homepage_event_candidate(
                f"major-{index:02d}",
                index,
                f"国家{index:02d} TERMINAL{index:02d}港暂停原油装船",
                f"TERMINAL{index:02d}港关闭，原油出口装船中断。",
            )
            for index in range(35)
        ]

        small = _homepage_display_summary(_group_homepage_candidates(sixteen, "Asia/Shanghai"))
        large = _homepage_display_summary(_group_homepage_candidates(thirty_five, "Asia/Shanghai"))

        self.assertEqual(small["displayed_news_count"], 16)
        self.assertIn("未为凑数量加入弱相关新闻", small["text"])
        self.assertEqual(large["displayed_news_count"], 35)
        self.assertIn("全部保留", large["text"])
        self.assertEqual(large["event_group_count"], 35)

    def test_direct_energy_path_requires_physical_energy_or_shipping_anchor(self) -> None:
        false_positives = [
            ("伊朗西部地震已致10人受伤", "伊朗西部发生地震，已致10人受伤。"),
            ("俄一客车遭无人机袭击致5死23伤", "俄罗斯一辆客车遭无人机袭击，造成5人死亡23人受伤。"),
            ("纽约市长称研究逮捕内塔尼亚胡，特朗普回应：绝不会发生", "特朗普称不会允许纽约市长逮捕内塔尼亚胡。"),
            ("伊朗遇袭后油价上涨", "伊朗遭袭后油价上涨，市场关注后续局势。"),
            (
                "分析师：市场对于铝市场供应风险可能过于掉以轻心",
                "LME铝库存偏低，霍尔木兹海峡干扰影响海湾冶炼厂进出口金属。",
            ),
        ]
        for index, (title, body) in enumerate(false_positives):
            candidate = {
                "row": {"title": title, "body": body, "url": f"https://flash.jin10.com/detail/noise-{index}"},
                "direct_supply_path": True,
                "freshness": "新增事实",
                "is_duplicate": False,
                "price_quote_only": False,
                "is_commentary": False,
            }
            self.assertIsNone(direct_energy_supply_reason(title, body))
            self.assertFalse(_homepage_candidate_is_eligible(candidate))

        houthi_title = "胡塞武装宣布对沙特实施海上禁令"
        houthi_body = "胡塞武装宣布正式对沙特实施海上航行禁令，限制船舶通行。"
        self.assertIsNotNone(direct_energy_supply_reason(houthi_title, houthi_body))
        self.assertIsNotNone(
            direct_energy_supply_reason("某炼油厂事故后停运", "该炼油厂因故障停运，原油加工能力下降。")
        )

    def test_sub_025_percent_day_returns_no_formal_movement_windows(self) -> None:
        store = _PriceStore([100.00, 100.01, 100.02, 100.03, 100.04, 100.05, 100.06, 100.07])
        start = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
        windows, _ = _scan_30m_price_windows(store, BrentConfig(), start, start + timedelta(days=1))
        self.assertEqual(windows, [])

    def test_30m_scan_does_not_treat_six_sparse_intervals_as_thirty_minutes(self) -> None:
        start = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)

        class SparseStore:
            def brent_day_klines(self, *_args):
                return [
                    {
                        "ts_utc": (start + timedelta(minutes=20 * index)).isoformat().replace("+00:00", "Z"),
                        "close": 100.0 + index,
                    }
                    for index in range(7)
                ]

        windows, _ = _scan_30m_price_windows(
            SparseStore(), BrentConfig(), start, start + timedelta(days=1)
        )

        self.assertTrue(windows)
        self.assertTrue(
            all(item["end_dt"] - item["start_dt"] <= timedelta(minutes=40) for item in windows)
        )

    def test_strong_windows_are_price_first_and_meet_half_percent(self) -> None:
        store = _PriceStore([100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 100.7, 100.8])
        start = datetime(2026, 7, 14, 0, 0, tzinfo=UTC)
        windows, _ = _scan_30m_price_windows(store, BrentConfig(), start, start + timedelta(days=1))
        self.assertTrue(windows)
        self.assertTrue(all(abs(float(item["pct"])) >= 0.005 for item in windows))

    def test_formal_movement_lists_every_qualified_news_without_cap(self) -> None:
        start = datetime(2026, 7, 14, 12, 50, tzinfo=UTC)
        candidates = []
        for index in range(1, 7):
            candidates.append(
                {
                    "raw_id": str(index),
                    "row": {
                        "title": f"供给端新闻{index}",
                        "body": f"霍尔木兹航运新增事实{index}",
                        "url": f"https://flash.jin10.com/detail/{index}",
                    },
                    "attribution_relation": "新闻先于价格",
                    "attribution_freshness": "新增事实",
                    "direct_supply_reason": "直接影响油轮运输",
                    "logic": "航运受限抬升供应风险溢价",
                    "attribution_judgment": "主要驱动" if index == 1 else "次要驱动",
                    "attribution_confidence": "中",
                }
            )
        group = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=90),
            "direction": "上涨",
            "cumulative_pct": 0.014,
            "max_30m_pct": 0.0137,
            "pct_5m": 0.0012,
            "pct_15m": 0.0035,
            "pct_30m": 0.0088,
            "pct_60m": 0.011,
            "trigger_label": "30分钟绝对波动 >= 0.50%",
            "selected_news": candidates,
            "core_driver": candidates[0],
            "attribution_judgment": "主要驱动",
            "pricing_status": "已部分定价",
        }
        text = "\n".join(_price_movement_review_lines({"groups": [group]}, "Asia/Shanghai"))
        self.assertIn("Brent上涨 +1.40%｜最大30分钟涨幅 +1.37%", text)
        self.assertIn("5分钟：+0.12%", text)
        self.assertEqual(text.count("https://flash.jin10.com/detail/"), 6)
        self.assertIn("6. 新闻摘要", text)

    def test_blue_panel_keeps_all_display_eligible_candidates(self) -> None:
        start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

        def candidate(
            raw_id: str,
            minute: int,
            *,
            title: str = "霍尔木兹海峡油轮通行受阻",
            url: str | None = None,
            freshness: str = "新增事实",
            is_duplicate: bool = False,
            judgment: str = "背景信息",
        ) -> dict[str, object]:
            return {
                "raw_id": raw_id,
                "published_dt": start + timedelta(minutes=minute),
                "row": {
                    "title": title,
                    "body": "油轮通行受阻，原油运输面临中断风险。",
                    "url": url if url is not None else f"https://flash.jin10.com/detail/{raw_id}",
                },
                "direct_supply_path": True,
                "direct_supply_reason": "直接影响原油运输",
                "freshness": freshness,
                "is_duplicate": is_duplicate,
                "price_quote_only": False,
                "is_commentary": False,
                "attribution_judgment": judgment,
                "attribution_confidence": "低",
                "attribution_relation": "同期发生",
                "attribution_priority": 4 if judgment == "主要驱动" else 2,
                "logic": "航运受限抬升原油供应风险溢价。",
                "direction_alignment": 1,
            }

        core = candidate("core", 20, judgment="主要驱动")
        lower_priority = candidate("lower", 5)
        invalid_link = candidate("invalid", 10, url="https://example.com/not-jin10")
        stale = candidate("stale", 15, freshness="汇总转述")
        duplicate = candidate("duplicate", 25, is_duplicate=True)
        group = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=30),
            "group_id": "move_01",
            "direction": "上涨",
            "cumulative_pct": 0.0051,
            "max_30m_pct": 0.0051,
            "pct_5m": 0.0012,
            "pct_15m": 0.0015,
            "pct_30m": 0.0051,
            "pct_60m": 0.0021,
            "attribution_judgment": "主要驱动",
            "attribution_confidence": "高",
            "pricing_status": "已部分定价",
            "attribution_reason": "核心事件已满足归因要求。",
            "candidates": [core, lower_priority, invalid_link, stale, duplicate],
            "selected_news": [core],
        }

        panel_groups = _build_blue_panel_groups({"groups": [group]}, "Asia/Shanghai")

        self.assertEqual([item["id"] for item in panel_groups[0]["news"]], ["lower", "core"])
        self.assertEqual([item["raw_id"] for item in group["selected_news"]], ["core"])

    def test_blue_panel_keeps_houthi_ban_without_core_driver_status(self) -> None:
        start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        houthi = {
            "raw_id": "houthi",
            "published_dt": start + timedelta(minutes=10),
            "row": {
                "title": "胡塞武装宣布对沙特实施海上禁令",
                "body": "胡塞武装宣布对沙特实施海上禁令，红海油轮和能源运输面临新增风险。",
                "url": "https://flash.jin10.com/detail/houthi",
            },
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
        group = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=30),
            "group_id": "move_02",
            "direction": "上涨",
            "cumulative_pct": 0.0051,
            "max_30m_pct": 0.0051,
            "pct_5m": 0.0012,
            "pct_15m": 0.0015,
            "pct_30m": 0.0051,
            "pct_60m": 0.0021,
            "attribution_judgment": "暂无法确认",
            "attribution_confidence": "低",
            "pricing_status": "未充分定价",
            "attribution_reason": "未达到核心驱动置信度。",
            "candidates": [houthi],
            "selected_news": [],
        }

        panel_groups = _build_blue_panel_groups({"groups": [group]}, "Asia/Shanghai")

        self.assertEqual([item["id"] for item in panel_groups[0]["news"]], ["houthi"])
        self.assertEqual(panel_groups[0]["news"][0]["judgment"], "同期相关信息")

    def test_blue_panel_does_not_refill_opposite_direction_candidate(self) -> None:
        start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        recovery = {
            "raw_id": "recovery",
            "published_dt": start + timedelta(minutes=10),
            "row": {
                "title": "炼油厂恢复生产",
                "body": "该炼油厂在遇袭后恢复生产，供应逐步恢复。",
                "url": "https://flash.jin10.com/detail/recovery",
            },
            "direct_supply_path": True,
            "direct_supply_reason": "直接影响原油供应",
            "freshness": "新增事实",
            "is_duplicate": False,
            "price_quote_only": False,
            "is_commentary": False,
            "direction_alignment": -1,
        }
        group = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=30),
            "direction": "上涨",
            "cumulative_pct": 0.006,
            "max_30m_pct": 0.006,
            "homepage_news": [],
            "candidates": [recovery],
            "selected_news": [],
        }

        panel_groups = _build_blue_panel_groups({"groups": [group]}, "Asia/Shanghai")

        self.assertEqual(panel_groups[0]["news"], [])

    def test_event_grouped_homepage_does_not_restore_opposite_direction_candidate(self) -> None:
        start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        recovery = {
            "raw_id": "recovery",
            "published_dt": start + timedelta(minutes=10),
            "row": {
                "title": "炼油厂恢复生产",
                "body": "该炼油厂在遇袭后恢复生产，供应逐步恢复。",
                "url": "https://flash.jin10.com/detail/recovery",
            },
            "direct_supply_path": True,
            "direct_supply_reason": "直接影响原油供应",
            "freshness": "新增事实",
            "is_duplicate": False,
            "price_quote_only": False,
            "is_commentary": False,
            "direction_alignment": -1,
        }
        group = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=30),
            "direction": "上涨",
            "cumulative_pct": 0.006,
            "max_30m_pct": 0.006,
            "homepage_news": [],
            "candidates": [recovery],
            "selected_news": [],
        }

        presentation = _build_event_grouped_homepage(
            {"groups": [group]},
            {"major_candidates": []},
            [],
            [],
            "Asia/Shanghai",
        )

        self.assertEqual(presentation.panel_groups[0]["news"], [])

    def test_blue_panel_rechecks_eligibility_for_supplied_homepage_news(self) -> None:
        start = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

        def candidate(raw_id: str, minute: int, *, is_duplicate: bool = False) -> dict[str, object]:
            return {
                "raw_id": raw_id,
                "published_dt": start + timedelta(minutes=minute),
                "row": {
                    "title": "霍尔木兹海峡油轮通行受阻",
                    "body": "油轮通行受阻，原油运输面临中断风险。",
                    "url": f"https://flash.jin10.com/detail/{raw_id}",
                },
                "direct_supply_path": True,
                "direct_supply_reason": "直接影响原油运输",
                "freshness": "新增事实",
                "is_duplicate": is_duplicate,
                "price_quote_only": False,
                "is_commentary": False,
                "attribution_judgment": "背景信息",
                "attribution_confidence": "低",
                "attribution_relation": "同期发生",
                "attribution_priority": 2,
                "logic": "航运受限抬升原油供应风险溢价。",
                "direction_alignment": 1,
            }

        eligible = candidate("eligible", 10)
        duplicate = candidate("duplicate", 5, is_duplicate=True)
        group = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=30),
            "homepage_news": [duplicate, eligible],
            "candidates": [],
        }

        panel_groups = _build_blue_panel_groups({"groups": [group]}, "Asia/Shanghai")

        self.assertEqual([item["id"] for item in panel_groups[0]["news"]], ["eligible"])

    def test_review_marks_extended_boundary_candidate_duplicate(self) -> None:
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=1)
        raw = {
            "raw_id": "boundary-duplicate",
            "published_at_utc": (start - timedelta(minutes=45)).isoformat().replace("+00:00", "Z"),
            "title": "霍尔木兹海峡油轮通行受阻",
            "body": "油轮通行受阻，原油运输面临中断风险。",
            "url": "https://flash.jin10.com/detail/boundary-duplicate",
        }
        group = {
            "start_dt": start,
            "end_dt": start + timedelta(minutes=30),
            "search_start_dt": start - timedelta(minutes=45),
            "search_end_dt": start + timedelta(minutes=45),
            "direction": "上涨",
            "cumulative_pct": 0.006,
            "max_30m_pct": 0.006,
        }
        calls: list[tuple[datetime, datetime]] = []

        class Store:
            def brent_day_klines(self, *_args) -> list[dict[str, object]]:
                return []

            def reaction_rows_between(self, range_start: datetime, range_end: datetime) -> list[dict[str, object]]:
                calls.append((range_start, range_end))
                return [{"news_id": "boundary-duplicate", "is_duplicate": 1}]

            def raw_news_between(self, *_args) -> list[dict[str, object]]:
                return [raw]

        candidate = {
            "raw_id": "boundary-duplicate",
            "row": raw,
            "published_dt": start - timedelta(minutes=45),
            "score": 1.0,
            "tier": 1,
            "direction_alignment": 1,
            "is_summary": False,
            "relation": "新闻先于价格",
            "direct_supply_path": True,
            "freshness": "新增事实",
            "price_quote_only": False,
            "is_commentary": False,
        }
        attribution = {
            "judgment": "背景信息",
            "freshness": "新增事实",
            "relation": "新闻先于价格",
            "confidence": "低",
            "reason": "仅用于重复状态回归测试。",
        }

        with (
            patch("jin10_market_report.tracking_report._scan_30m_price_windows", return_value=([{"pct": 0.006}], [])),
            patch("jin10_market_report.tracking_report._merge_price_windows", return_value=[group]),
            patch("jin10_market_report.tracking_report._score_movement_candidate", return_value=candidate),
            patch("jin10_market_report.tracking_report._candidate_attribution", return_value=attribution),
        ):
            review = _build_price_movement_review(Store(), BrentConfig(), start, end, "Asia/Shanghai")

        self.assertEqual(calls, [(start - timedelta(minutes=45), end + timedelta(minutes=15))])
        self.assertTrue(review["groups"][0]["candidates"][0]["is_duplicate"])
        self.assertEqual(review["groups"][0]["homepage_news"], [])

    def test_official_thread_cleanup_and_title_derivation(self) -> None:
        cleaned = _clean_source_bullet(
            "2/ Iran attacked three vessels in the Strait of Hormuz. (2/4)\n\nRead more: isw.pub/example"
        )
        self.assertEqual(cleaned, "Iran attacked three vessels in the Strait of Hormuz.")
        title = _title_from_page_or_url(
            "",
            "https://understandingwar.org/research/middle-east/iran-update-special-report-july-15-2026/",
        )
        self.assertEqual(title, "Iran Update Special Report, July 15, 2026")

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
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("jin10_market_report.external_update._fetch_from_site", return_value=source),
            patch("jin10_market_report.external_update._translate_bullets", return_value=translated_items),
        ):
            result = fetch_latest_isw_update(cutoff, cache_dir=Path(temp_dir))
        self.assertEqual(result["source_key_takeaways"], source_items)
        self.assertEqual(result["key_takeaways_zh"], translated_items)
        self.assertEqual(result["source_key_takeaways_count"], 8)
        self.assertEqual(result["translated_key_takeaways_count"], 8)
        self.assertNotIn("supplemental_zh", result)

    def test_article_key_takeaways_preserves_every_item_and_stops_before_body(self) -> None:
        items = [f"Key takeaway {index} contains enough source text to remain a complete paragraph." for index in range(1, 9)]
        page = "\n".join(["Iran Update", "Key Takeaways", *items, "Iran", "Body paragraph must not be collected."])

        parsed = _article_key_takeaways(page)

        self.assertEqual(parsed, items)
        self.assertNotIn("Body paragraph must not be collected.", parsed)

    def test_article_key_takeaways_preserves_repeated_official_items(self) -> None:
        repeated = "Repeated official takeaway must remain a separate source item."
        page = "\n".join(["Key Takeaways", repeated, repeated, "Iran"])

        parsed = _article_key_takeaways(page)

        self.assertEqual(parsed, [repeated, repeated])

    def test_isw_translation_count_mismatch_marks_update_failed(self) -> None:
        cutoff = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
        source = {
            "fetch_method": "官网直连",
            "title": "Iran Update, August 4, 2026",
            "url": "https://understandingwar.org/research/middle-east/iran-update-august-4-2026/",
            "published_at_utc": "2026-08-04T22:30:00Z",
            "source_key_takeaways": ["one", "two", "three"],
        }
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("jin10_market_report.external_update._fetch_from_site", return_value=source),
            patch("jin10_market_report.external_update._translate_bullets", return_value=["一", "二"]),
        ):
            result = fetch_latest_isw_update(cutoff, cache_dir=Path(temp_dir))

        self.assertEqual(result["status"], "failed")
        self.assertIn("数量不一致", result["failure_reason"])
        self.assertEqual(result["key_takeaways_zh"], [])

    def test_failed_isw_cache_can_be_retried_on_demand(self) -> None:
        cutoff = datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
        source = {
            "fetch_method": "官网直连",
            "title": "Iran Update Special Report, July 15, 2026",
            "url": "https://understandingwar.org/research/middle-east/iran-update-special-report-july-15-2026/",
            "published_at_utc": "2026-07-16T23:30:00Z",
            "source_key_takeaways": ["one", "two", "three"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            cache_path = cache_dir / "isw_iran_update_20260717_0800.json"
            cache_path.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
            with (
                patch("jin10_market_report.external_update._fetch_from_site", return_value=source) as fetch_site,
                patch(
                    "jin10_market_report.external_update._translate_bullets",
                    return_value=["一", "二", "三"],
                ),
            ):
                result = fetch_latest_isw_update(
                    cutoff,
                    cache_dir=cache_dir,
                    retry_failed_cache=True,
                )

            self.assertEqual(result["status"], "success")
            self.assertFalse(result["cache_hit"])
            fetch_site.assert_called_once_with(cutoff)
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))["status"], "success")


if __name__ == "__main__":
    unittest.main()
