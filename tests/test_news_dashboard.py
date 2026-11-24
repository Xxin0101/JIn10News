from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jin10_market_report.news_dashboard import render_news_dashboard
from jin10_market_report.storage import TrackerStore


class NewsDashboardTests(unittest.TestCase):
    def test_missing_prices_keeps_native_dashboard_and_curated_news(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            store = TrackerStore(folder / "news.sqlite")
            store.close()
            curated = folder / "news.md"
            curated.write_text(
                "# 新闻日报\n\n**新闻覆盖状态：PARTIAL**\n\n"
                "## 最重要进展\n\n### 沙拉拉恢复输油\n\n"
                "国家石油公司证实已恢复。\n\n"
                "[08:01 金十原文](https://flash.jin10.com/detail/123)\n\n"
                "## 供应\n\n### 沙拉拉恢复输油\n\n"
                "国家石油公司证实已恢复。\n\n"
                "[08:01 金十原文](https://flash.jin10.com/detail/123)\n\n"
                "## 下一时段待确认\n\n- 核对装船变化。\n",
                encoding="utf-8",
            )
            with patch(
                "jin10_market_report.price_layer.build_daily_price_forecast_safe",
                side_effect=AssertionError("news dashboard must not call the model"),
            ):
                path = render_news_dashboard(
                    date(2026, 11, 24), folder / "news.sqlite", curated, folder / "out"
                )
            html = path.read_text(encoding="utf-8")
            self.assertIn('class="executive-summary"', html)
            self.assertIn('class="metric-grid"', html)
            self.assertIn('id="related-news-panel"', html)
            self.assertIn("核对装船变化", html)
            self.assertIn("本窗口可用价格数据不足", html)
            self.assertIn("新闻覆盖状态：PARTIAL", html)
            self.assertIn("https://flash.jin10.com/detail/123", html)
            self.assertNotIn('class="price-layer-card"', html)
            self.assertNotIn("下一交易日判断（主观分析）", html)
            self.assertNotIn("点击上方K线图中的蓝色新闻时间点", html)
