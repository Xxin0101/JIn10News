from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jin10_market_report.trump_update import _group_statement_items, fetch_trump_statements


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
            normalized_item(11, "2026-08-04 11:43:35", "特朗普：正在谈判", "特朗普称谈判已经开始，这是伊朗最后机会。"),
        ]

        statements = _group_statement_items(rows)

        self.assertEqual(len(statements), 1)
        self.assertEqual([item["id"] for item in statements[0]["source_items"]], [10, 11])
        self.assertIn("伊朗", statements[0]["topics"])
        self.assertNotIn("_published_dt_utc", statements[0]["source_items"][0])

    def test_low_similarity_statements_with_the_same_topic_remain_separate(self) -> None:
        """Broad topic overlap alone must not collapse independent Trump statements."""
        rows = [
            normalized_item(20, "2026-08-04 11:34:54", "特朗普称伊朗核谈判是最后机会", "特朗普警告伊朗不要错过机会。"),
            normalized_item(21, "2026-08-04 11:43:35", "特朗普称伊朗核谈判最后机会已经过去", "特朗普表示继续制裁。"),
        ]

        statements = _group_statement_items(rows)

        self.assertEqual([statement["statement_id"] for statement in statements], ["20", "21"])

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


if __name__ == "__main__":
    unittest.main()
