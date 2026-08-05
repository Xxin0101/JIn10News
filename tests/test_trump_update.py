from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from jin10_market_report.trump_update import _group_statement_items, _normalize_item, fetch_trump_statements


UTC = ZoneInfo("UTC")

PAGE_ONE = {
    "status": 200,
    "data": {
        "page": 1,
        "page_size": 20,
        "total": 4,
        "list": [
            {
                "id": 1,
                "type": "news",
                "display_datetime": "2026-08-05 08:00:00",
                "title": "特朗普谈油价",
                "introduction": "特朗普表示油价必须下降。",
            },
            {
                "id": 2,
                "type": "video",
                "display_datetime": "2026-08-04 14:29:33",
                "title": "特朗普喊话石油巨头",
                "introduction": "特朗普要求埃克森美孚与雪佛龙降低零售油价。",
                "detail_url": "/details/2?from=topic",
            },
            {
                "id": 3,
                "type": "news",
                "display_datetime": "2026-08-04 14:10:59",
                "title": "特朗普在伊朗战争中面临困境",
                "introduction": "白宫正在寻找退出路线。",
            },
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
            {
                "id": 4,
                "type": "news",
                "display_datetime": "2026-08-04 08:00:00",
                "title": "特朗普称美伊正在谈判",
                "introduction": "特朗普警告这是伊朗签署协议的最后机会。",
            },
            {
                "id": 5,
                "type": "news",
                "display_datetime": "2026-08-04 07:59:59",
                "title": "特朗普谈霍尔木兹",
                "introduction": "特朗普表示海峡将开放。",
            },
        ],
    },
}


def normalized_item(item_id: int, display_datetime: str, title: str, introduction: str) -> dict[str, object]:
    published = datetime.strptime(display_datetime, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=ZoneInfo("Asia/Shanghai")
    ).astimezone(UTC)
    candidate_text = " ".join((title, introduction))
    return {
        "id": item_id,
        "type": "news",
        "published_at_utc": published.isoformat().replace("+00:00", "Z"),
        "published_at_bj": display_datetime,
        "title": title,
        "introduction": introduction,
        "text": candidate_text,
        "topics": ["伊朗"],
        "source_url": f"https://xnews.jin10.com/topic/418/{item_id}",
        "_published_dt_utc": published,
        "_candidate_text": candidate_text,
    }


class TrumpUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
        self.end = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)

    def test_collects_only_in_window_direct_relevant_statements_and_stops_at_oldest_page_item(self) -> None:
        """Removing a window boundary, speech, or topic filter changes report statements."""
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "jin10_market_report.trump_update._request_topic_page",
                side_effect=[PAGE_ONE, PAGE_TWO],
            ) as request_page:
                result = fetch_trump_statements(self.start, self.end, Path(directory))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["raw_topic_item_count"], 5)
        self.assertEqual(result["in_window_item_count"], 3)
        self.assertEqual(result["relevant_source_item_count"], 2)
        self.assertEqual(result["statement_count"], 2)
        self.assertEqual(
            [item["id"] for statement in result["statements"] for item in statement["source_items"]],
            [4, 2],
        )
        source_urls = {
            item["id"]: item["source_url"]
            for statement in result["statements"]
            for item in statement["source_items"]
        }
        self.assertEqual(source_urls[2], "https://xnews.jin10.com/details/2?from=topic")
        self.assertEqual(source_urls[4], "https://xnews.jin10.com/details/4")
        self.assertEqual([call.args[0] for call in request_page.call_args_list], [1, 2])
        self.assertTrue(all("_published_dt_utc" not in item for statement in result["statements"] for item in statement["source_items"]))

    def test_duplicate_reports_group_but_preserve_every_source(self) -> None:
        """Discarding duplicate source rows would make the grouped statement untraceable."""
        rows = [
            normalized_item(10, "2026-08-04 11:34:54", "特朗普：这是伊朗的最后机会", "特朗普称美伊谈判正在进行。"),
            normalized_item(11, "2026-08-04 11:43:35", "特朗普：这是伊朗最后机会", "特朗普称美伊谈判正在进行，这是最后机会。"),
        ]

        statements = _group_statement_items(rows)

        self.assertEqual(len(statements), 1)
        self.assertEqual([item["id"] for item in statements[0]["source_items"]], [10, 11])
        self.assertIn("伊朗", statements[0]["topics"])
        self.assertNotIn("_published_dt_utc", statements[0]["source_items"][0])

    def test_shared_terms_do_not_override_subthreshold_candidate_similarity(self) -> None:
        """A matching vocabulary must not bypass the 0.72 candidate-text similarity gate."""
        rows = [
            normalized_item(20, "2026-08-04 11:34:54", "特朗普称伊朗核谈判是最后机会", "特朗普警告伊朗不要错过机会。"),
            normalized_item(21, "2026-08-04 11:43:35", "特朗普称伊朗核谈判最后机会已经过去", "特朗普表示谈判仍在继续。"),
        ]

        statements = _group_statement_items(rows)

        self.assertEqual([statement["statement_id"] for statement in statements], ["20", "21"])

    def test_video_and_flash_source_url_fallbacks(self) -> None:
        """Absent detail URLs must still lead readers to the correct original Jin10 item."""
        video = _normalize_item(
            {"id": 24044, "type": "video", "display_datetime": "2026-08-04 11:00:00"},
            "Asia/Shanghai",
        )
        flash = _normalize_item(
            {"id": 102829797, "type": "flash", "display_datetime": "2026-08-04 11:01:00"},
            "Asia/Shanghai",
        )

        self.assertEqual(video["source_url"], "https://v.jin10.com/details.html?id=24044")
        self.assertEqual(flash["source_url"], "https://xnews.jin10.com/details/flash/102829797")

    def test_successful_cache_is_reused_without_requesting_api(self) -> None:
        """Ignoring a successful cache would create unnecessary external API traffic."""
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "trump_statements_20260805_0800.json"
            cache_path.write_text(json.dumps({"status": "success", "statements": []}), encoding="utf-8")
            with patch("jin10_market_report.trump_update._request_topic_page") as request_page:
                result = fetch_trump_statements(self.start, self.end, Path(directory))

        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["cache_path"], str(cache_path))
        request_page.assert_not_called()

    def test_failed_cache_is_reused_without_explicit_retry(self) -> None:
        """Retrying an existing failed cache by default would violate the explicit retry contract."""
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "trump_statements_20260805_0800.json"
            cache_path.write_text(json.dumps({"status": "failed", "statements": []}), encoding="utf-8")
            with patch("jin10_market_report.trump_update._request_topic_page") as request_page:
                result = fetch_trump_statements(self.start, self.end, Path(directory))

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cache_hit"])
        request_page.assert_not_called()

    def test_explicit_retry_replaces_failed_cache(self) -> None:
        """A failed cache must not prevent an explicitly requested retry from refreshing it."""
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "trump_statements_20260805_0800.json"
            cache_path.write_text(json.dumps({"status": "failed", "statements": []}), encoding="utf-8")
            empty_page = {"status": 200, "data": {"page": 1, "page_size": 20, "total": 0, "list": []}}
            with patch(
                "jin10_market_report.trump_update._request_topic_page", return_value=empty_page
            ) as request_page:
                result = fetch_trump_statements(
                    self.start, self.end, Path(directory), retry_failed_cache=True
                )

            cached = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "success")
        self.assertFalse(result["cache_hit"])
        self.assertEqual(cached["status"], "success")
        request_page.assert_called_once_with(1)

    def test_retry_flag_is_available_for_report_and_backfill(self) -> None:
        """Dropping either parser flag would make a failed Trump cache impossible to retry."""
        from run_jin10_tracker import parse_args

        with patch.object(
            sys,
            "argv",
            ["run_jin10_tracker.py", "report", "--date", "2026-08-05", "--retry-failed-trump"],
        ):
            self.assertTrue(parse_args().retry_failed_trump)

        with patch.object(
            sys,
            "argv",
            ["run_jin10_tracker.py", "backfill-day", "--date", "2026-08-05", "--retry-failed-trump"],
        ):
            self.assertTrue(parse_args().retry_failed_trump)

    def test_backfill_passes_failed_trump_payload_to_report_without_database_insertion(self) -> None:
        """Omitting the payload, raising on its failure, or storing it would break report continuity."""
        from jin10_market_report.brent import BrentConfig
        from jin10_market_report.config import FetchConfig, FilterConfig, TrackerConfig
        from jin10_market_report.storage import TrackerStore
        from jin10_market_report.tracker import run_backfill_day

        reaction_counts = {
            "analyzed": 0,
            "pending": 0,
            "price_matched": 0,
            "price_missing": 0,
            "price_5m_missing": 0,
            "price_15m_missing": 0,
            "price_30m_missing": 0,
            "price_60m_missing": 0,
            "effective_oil_news": 0,
            "summary_without_event": 0,
            "major_topic_hits": 0,
            "threshold_hits": 0,
            "daily": 0,
            "excluded": 0,
        }
        trump_payload = {
            "status": "failed",
            "statement_count": 0,
            "relevant_source_item_count": 0,
            "failure_reason": "network timeout",
            "statements": [],
        }
        external_update = {"status": "success", "title": "Iran Update"}

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "tracker.sqlite"
            output_dir = Path(directory) / "outputs"
            store = TrackerStore(database_path)
            logger = logging.getLogger("test_trump_backfill")
            tracker_config = TrackerConfig(
                database_path=str(database_path),
                output_dir=str(output_dir),
                timezone="Asia/Shanghai",
            )
            try:
                with ExitStack() as stack:
                    stack.enter_context(patch("jin10_market_report.tracker.fetch_flashes_between", return_value=[]))
                    stack.enter_context(
                        patch(
                            "jin10_market_report.tracker.fetch_brent_5m_range",
                            return_value=SimpleNamespace(klines=[], fetched_at_utc=self.end, note=""),
                        )
                    )
                    stack.enter_context(
                        patch("jin10_market_report.tracker.analyze_reactions_for_range", return_value=reaction_counts)
                    )
                    stack.enter_context(patch("jin10_market_report.tracker.log_window_validation"))
                    stack.enter_context(
                        patch("jin10_market_report.tracker.fetch_latest_isw_update", return_value=external_update)
                    )
                    fetch_trump = stack.enter_context(
                        patch("jin10_market_report.tracker.fetch_trump_statements", return_value=trump_payload)
                    )
                    build_report = stack.enter_context(
                        patch(
                            "jin10_market_report.tracker.build_markdown_report",
                            return_value=SimpleNamespace(
                                markdown_path=output_dir / "report.md",
                                presentation=SimpleNamespace(),
                            ),
                        )
                    )
                    stack.enter_context(
                        patch(
                            "jin10_market_report.tracker.build_html_report",
                            return_value=output_dir / "report.html",
                        )
                    )
                    with self.assertLogs(logger, "WARNING") as captured:
                        result = run_backfill_day(
                            store,
                            FetchConfig(timezone="Asia/Shanghai"),
                            FilterConfig(),
                            BrentConfig(),
                            tracker_config,
                            logger,
                            "2026-08-05",
                            retry_failed_trump=True,
                        )

                self.assertIn("特朗普专栏抓取失败，不影响金十+Brent主日报", "\n".join(captured.output))
                fetch_trump.assert_called_once_with(
                    self.start,
                    self.end,
                    cache_dir=database_path.parent / "external_updates",
                    timezone_name="Asia/Shanghai",
                    retry_failed_cache=True,
                )
                self.assertIs(build_report.call_args.kwargs["trump_update"], trump_payload)
                self.assertIs(result["trump_update"], trump_payload)
                self.assertEqual(result["trump_update_status"], "失败")
                self.assertEqual(store.counts(), {"news": 0, "brent_klines": 0, "pending_news": 0, "daily_news": 0})
            finally:
                store.close()

    def test_backfill_continues_when_trump_fetcher_raises(self) -> None:
        """A cache or transport exception must not prevent the core report from being generated."""
        from jin10_market_report.brent import BrentConfig
        from jin10_market_report.config import FetchConfig, FilterConfig, TrackerConfig
        from jin10_market_report.tracker import run_backfill_day

        reaction_counts = {
            "analyzed": 0,
            "pending": 0,
            "price_matched": 0,
            "price_missing": 0,
            "price_5m_missing": 0,
            "price_15m_missing": 0,
            "price_30m_missing": 0,
            "price_60m_missing": 0,
            "effective_oil_news": 0,
            "summary_without_event": 0,
            "major_topic_hits": 0,
            "threshold_hits": 0,
            "daily": 0,
            "excluded": 0,
        }
        store = MagicMock()
        store.report_focus_rows.return_value = []
        store.counts.return_value = {}
        logger = logging.getLogger("test_trump_exception")

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            output_dir = Path(directory) / "outputs"
            tracker_config = TrackerConfig(
                database_path=str(Path(directory) / "tracker.sqlite"),
                output_dir=str(output_dir),
                timezone="Asia/Shanghai",
            )
            stack.enter_context(patch("jin10_market_report.tracker.fetch_flashes_between", return_value=[]))
            stack.enter_context(
                patch(
                    "jin10_market_report.tracker.fetch_brent_5m_range",
                    return_value=SimpleNamespace(klines=[], fetched_at_utc=self.end, note=""),
                )
            )
            stack.enter_context(
                patch("jin10_market_report.tracker.analyze_reactions_for_range", return_value=reaction_counts)
            )
            stack.enter_context(patch("jin10_market_report.tracker.log_window_validation"))
            stack.enter_context(
                patch("jin10_market_report.tracker.fetch_latest_isw_update", return_value={"status": "success"})
            )
            stack.enter_context(
                patch("jin10_market_report.tracker.fetch_trump_statements", side_effect=OSError("cache unavailable"))
            )
            build_report = stack.enter_context(
                patch(
                    "jin10_market_report.tracker.build_markdown_report",
                    return_value=SimpleNamespace(markdown_path=output_dir / "report.md", presentation=SimpleNamespace()),
                )
            )
            stack.enter_context(
                patch("jin10_market_report.tracker.build_html_report", return_value=output_dir / "report.html")
            )
            with self.assertLogs(logger, "WARNING") as captured:
                result = run_backfill_day(
                    store,
                    FetchConfig(timezone="Asia/Shanghai"),
                    FilterConfig(),
                    BrentConfig(),
                    tracker_config,
                    logger,
                    "2026-08-05",
                )

        self.assertIn("特朗普专栏抓取失败，不影响金十+Brent主日报", "\n".join(captured.output))
        self.assertEqual(build_report.call_args.kwargs["trump_update"]["status"], "failed")
        self.assertIn("cache unavailable", build_report.call_args.kwargs["trump_update"]["failure_reason"])
        self.assertEqual(result["trump_update_status"], "失败")

    def test_report_command_passes_trump_payload_and_retry_flag_to_report_builder(self) -> None:
        """Dropping the report-command payload or retry flag would make CLI regeneration inconsistent."""
        from jin10_market_report.brent import BrentConfig
        from jin10_market_report.config import FetchConfig, FilterConfig, TrackerConfig
        from run_jin10_tracker import main

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            database_path = Path(directory) / "tracker.sqlite"
            output_dir = Path(directory) / "outputs"
            tracker_config = TrackerConfig(
                database_path=str(database_path),
                output_dir=str(output_dir),
                timezone="Asia/Shanghai",
            )
            args = SimpleNamespace(
                command="report",
                date="2026-08-05",
                log_dir=str(Path(directory) / "logs"),
                retry_failed_isw=False,
                retry_failed_trump=True,
            )
            store = MagicMock()
            store.report_focus_rows.return_value = []
            logger = logging.getLogger("test_trump_report_command")
            trump_payload = {"status": "success", "statement_count": 1, "relevant_source_item_count": 1}

            stack.enter_context(patch("run_jin10_tracker.parse_args", return_value=args))
            stack.enter_context(
                patch("run_jin10_tracker.setup_logger", return_value=(logger, Path(directory) / "tracker.log"))
            )
            stack.enter_context(
                patch(
                    "run_jin10_tracker.build_configs",
                    return_value=(FetchConfig(), FilterConfig(), BrentConfig(), tracker_config),
                )
            )
            stack.enter_context(patch("run_jin10_tracker.TrackerStore", return_value=store))
            stack.enter_context(
                patch(
                    "jin10_market_report.reaction.analyze_reactions_for_range",
                    return_value={
                        "analyzed": 0,
                        "pending": 0,
                        "price_matched": 0,
                        "price_missing": 0,
                        "price_5m_missing": 0,
                        "price_15m_missing": 0,
                        "price_30m_missing": 0,
                        "price_60m_missing": 0,
                        "effective_oil_news": 0,
                        "summary_without_event": 0,
                        "major_topic_hits": 0,
                        "threshold_hits": 0,
                        "daily": 0,
                        "excluded": 0,
                    },
                )
            )
            stack.enter_context(patch("jin10_market_report.tracker.log_window_validation"))
            stack.enter_context(patch("jin10_market_report.tracking_report._build_price_movement_review", return_value={}))
            stack.enter_context(
                patch("jin10_market_report.external_update.fetch_latest_isw_update", return_value={"status": "success"})
            )
            fetch_trump = stack.enter_context(
                patch("jin10_market_report.trump_update.fetch_trump_statements", return_value=trump_payload)
            )
            build_report = stack.enter_context(
                patch(
                    "jin10_market_report.tracking_report.build_markdown_report",
                    return_value=SimpleNamespace(markdown_path=output_dir / "report.md", presentation=SimpleNamespace()),
                )
            )
            stack.enter_context(
                patch("jin10_market_report.tracking_report.build_html_report", return_value=output_dir / "report.html")
            )
            stack.enter_context(
                patch("jin10_market_report.tracking_report.audit_markdown_path_for", return_value=output_dir / "audit.md")
            )
            stack.enter_context(
                patch("jin10_market_report.tracking_report.audit_html_path_for", return_value=output_dir / "audit.html")
            )

            self.assertEqual(main(), 0)

        fetch_trump.assert_called_once_with(
            self.start,
            self.end,
            cache_dir=database_path.parent / "external_updates",
            timezone_name="Asia/Shanghai",
            retry_failed_cache=True,
        )
        self.assertIs(build_report.call_args.kwargs["trump_update"], trump_payload)
        store.close.assert_called_once()

    def test_recovery_fetches_each_window_and_passes_its_exact_trump_payload(self) -> None:
        """Skipping a recovered window would leave its rebuilt report without the source payload."""
        from jin10_market_report.brent import BrentConfig
        from jin10_market_report.config import TrackerConfig
        from jin10_market_report.tracker import recover_partial_reports_after_successful_fetch

        reaction_counts = {
            "analyzed": 0,
            "pending": 0,
            "price_matched": 0,
            "price_missing": 0,
            "price_5m_missing": 0,
            "price_15m_missing": 0,
            "price_30m_missing": 0,
            "price_60m_missing": 0,
            "effective_oil_news": 0,
            "summary_without_event": 0,
            "major_topic_hits": 0,
            "threshold_hits": 0,
            "daily": 0,
            "excluded": 0,
        }
        first_payload = {"status": "success", "statement_count": 1, "statements": [{"statement_id": "first"}]}
        second_payload = {"status": "failed", "statement_count": 0, "failure_reason": "network timeout", "statements": []}
        store = MagicMock()
        logger = logging.getLogger("test_trump_recovery")

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            output_dir = Path(directory) / "outputs"
            output_dir.mkdir()
            for day_token in ("20260804", "20260805"):
                (output_dir / f"Jin10_Brent_Rolling_Reaction_{day_token}_0800_PARTIAL.md").write_text(
                    "partial", encoding="utf-8"
                )
            database_path = Path(directory) / "tracker.sqlite"
            tracker_config = TrackerConfig(
                database_path=str(database_path),
                output_dir=str(output_dir),
                timezone="Asia/Shanghai",
            )
            stack.enter_context(
                patch("jin10_market_report.tracker.analyze_reactions_for_range", return_value=reaction_counts)
            )
            stack.enter_context(patch("jin10_market_report.tracker.log_window_validation"))
            stack.enter_context(patch("jin10_market_report.tracker._build_window_audit", return_value={}))
            stack.enter_context(
                patch("jin10_market_report.tracker.classify_report_status", return_value=("FORMAL", {}))
            )
            fetch_trump = stack.enter_context(
                patch(
                    "jin10_market_report.tracker.fetch_trump_statements",
                    side_effect=[first_payload, second_payload],
                )
            )
            build_report = stack.enter_context(
                patch(
                    "jin10_market_report.tracker.build_markdown_report",
                    return_value=SimpleNamespace(markdown_path=output_dir / "report.md", presentation=SimpleNamespace()),
                )
            )
            stack.enter_context(
                patch("jin10_market_report.tracker.build_html_report", return_value=output_dir / "report.html")
            )

            with self.assertLogs(logger, "WARNING") as captured:
                recovered = recover_partial_reports_after_successful_fetch(
                    store,
                    BrentConfig(),
                    tracker_config,
                    [],
                    logger,
                )

        self.assertEqual([item["day"] for item in recovered], ["2026-08-04", "2026-08-05"])
        self.assertIn("特朗普专栏抓取失败，不影响金十+Brent主日报", "\n".join(captured.output))
        self.assertEqual(
            fetch_trump.call_args_list,
            [
                unittest.mock.call(
                    datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
                    datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
                    cache_dir=database_path.parent / "external_updates",
                    timezone_name="Asia/Shanghai",
                    retry_failed_cache=False,
                ),
                unittest.mock.call(
                    datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
                    datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
                    cache_dir=database_path.parent / "external_updates",
                    timezone_name="Asia/Shanghai",
                    retry_failed_cache=False,
                ),
            ],
        )
        self.assertIs(build_report.call_args_list[0].kwargs["trump_update"], first_payload)
        self.assertIs(build_report.call_args_list[1].kwargs["trump_update"], second_payload)
        store.upsert_raw_news.assert_not_called()
        store.upsert_news.assert_not_called()
        store.upsert_klines.assert_not_called()

if __name__ == "__main__":
    unittest.main()
