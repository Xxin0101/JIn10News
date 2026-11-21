import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from jin10_market_report.daily_research import build_research_sections
from run_jin10_tracker import main


class DailyResearchTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = datetime(2026, 11, 21, tzinfo=timezone.utc)

    def test_evidence_links_direction_and_cutoff(self):
        def row(hour, title, number, url=None):
            return {
                "published_at_utc": f"2026-11-20T{hour:02}:00:00+00:00",
                "title": title,
                "body": title,
                "detail": "",
                "url": url or f"https://flash.jin10.com/detail/{number}",
                "is_summary": 0,
            }

        at_cutoff = row(23, "原油库存增加（截止点）", 4)
        at_cutoff["published_at_utc"] = self.cutoff.isoformat()
        evidence = [
            row(10, "美国原油需求下降", 1),
            row(12, "EIA原油库存增加", 2),
            row(13, "EIA原油库存下降", 3, "https://evil.example/source"),
            at_cutoff,
        ]
        content = "\n".join(build_research_sections(
            evidence, self.cutoff, "Asia/Shanghai", "PARTIAL", None
        ))
        self.assertIn("原油需求下降", content)
        self.assertIn("库存", content)
        self.assertIn("偏空", content)
        self.assertIn("2026-11-20 18:00", content)
        self.assertIn("https://flash.jin10.com/detail/1", content)
        self.assertNotIn("截止点", content)
        self.assertNotIn("evil.example", content)
        self.assertIn("2026-11-23", content)
        self.assertIn("观望", content)

    def test_model_output_only_guides_formal_research(self):
        price_layer = {
            "status": "success", "target_date": "2026-11-23",
            "predicted_direction": "上涨",
        }
        for status, expected in (("FORMAL", "偏多"), ("FAILED", "观望")):
            with self.subTest(status=status):
                sections = build_research_sections(
                    [], self.cutoff, "Asia/Shanghai", status, price_layer
                )
                self.assertIn(expected, "\n".join(sections))

    def test_sample_cli_generates_labeled_bundle_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "argv", ["tracker", "sample-report", "--output-dir", directory]):
                with patch("urllib.request.urlopen", side_effect=AssertionError("network call")):
                    self.assertEqual(main(), 0)
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 5)
            self.assertTrue(all("_SAMPLE" in path.name for path in files))
            report = next(path for path in files if "Reaction" in path.name and path.suffix == ".html")
            body = report.read_text(encoding="utf-8")
            for label in ("测试样本", "供给", "需求", "库存", "OPEC", "地缘", "主观分析", "观望"):
                self.assertIn(label, body)
            self.assertNotIn("javascript:", body.lower())
            ElementTree.parse(next(path for path in files if path.suffix == ".svg"))
