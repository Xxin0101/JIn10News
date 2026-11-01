from __future__ import annotations

import csv
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo


MODEL_NAMES = (
    "ARIMA_1_1_1",
    "Linear_Regression_MA3_MA9",
    "LSTM",
    "XGBoost",
    "LightGBM",
    "Ensemble_Average",
)


class PriceLayerSelectionTests(unittest.TestCase):
    def _write_oos_fixture(self, results_dir: Path) -> None:
        rows: list[dict[str, object]] = []
        actual_returns = (0.02, -0.01, 0.03)
        dates = ("2026-08-20", "2026-08-21", "2026-08-24")
        for model_name in MODEL_NAMES:
            if model_name == "ARIMA_1_1_1":
                predicted_returns = (0.01, -0.02, -0.04)
            elif model_name == "XGBoost":
                predicted_returns = (-0.03, 0.02, 0.03)
            else:
                predicted_returns = (-0.04, 0.04, -0.02)
            for prediction_date, actual, predicted in zip(
                dates, actual_returns, predicted_returns
            ):
                rows.append(
                    {
                        "Model": model_name,
                        "Train_Start": "2025-01-01",
                        "Train_End": "2026-08-19",
                        "Prediction_Date": prediction_date,
                        "Current_Close": 80.0,
                        "Actual_Return": actual,
                        "Predicted_Return": predicted,
                        "Actual_Direction": 1 if actual > 0 else -1,
                        "Predicted_Direction": 1 if predicted > 0 else -1,
                        "Direction_Correct": (actual > 0) == (predicted > 0),
                        "Train_Size": 252,
                    }
                )
        path = results_dir / "all_models_walk_forward_predictions.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_model_selection_excludes_oos_results_after_the_prediction_cutoff(self) -> None:
        """A later realized result must not change the model chosen for an earlier report."""
        from jin10_market_report.price_layer import select_daily_price_model

        with tempfile.TemporaryDirectory() as directory:
            results_dir = Path(directory)
            self._write_oos_fixture(results_dir)

            selected = select_daily_price_model(results_dir, as_of_date=date(2026, 8, 21))

        self.assertEqual(selected["model_name"], "ARIMA_1_1_1")
        self.assertEqual(selected["oos_samples"], 2)
        self.assertEqual(selected["last_oos_date"], "2026-08-21")
        self.assertAlmostEqual(selected["direction_accuracy"], 1.0)
        self.assertAlmostEqual(selected["balanced_accuracy"], 1.0)
        self.assertEqual(len(selected["model_comparison"]), 6)

    def test_model_selection_rejects_duplicate_oos_model_dates(self) -> None:
        """A duplicated checkpoint row must not silently overweight one model's history."""
        from jin10_market_report.price_layer import select_daily_price_model

        with tempfile.TemporaryDirectory() as directory:
            results_dir = Path(directory)
            self._write_oos_fixture(results_dir)
            path = results_dir / "all_models_walk_forward_predictions.csv"
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join([*lines, lines[1], ""]), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "重复"):
                select_daily_price_model(results_dir, as_of_date=date(2026, 8, 21))

    def test_forecast_payload_converts_the_selected_return_to_a_price(self) -> None:
        """Using the return as a dollar delta would materially misstate the forecast price."""
        from jin10_market_report.price_layer import build_price_forecast_payload

        payload = build_price_forecast_payload(
            selection={
                "model_name": "ARIMA_1_1_1",
                "direction_accuracy": 0.51,
                "balanced_accuracy": 0.50,
                "mae": 0.02,
                "rmse": 0.03,
                "oos_samples": 283,
                "last_oos_date": "2026-08-21",
                "model_comparison": [],
            },
            as_of_date=date(2026, 8, 21),
            target_date=date(2026, 8, 24),
            current_close=80.0,
            predicted_return=0.025,
        )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["predicted_price"], 82.0)
        self.assertEqual(payload["predicted_direction"], "上涨")
        self.assertEqual(payload["target_date"], "2026-08-24")
        self.assertFalse(payload["future_data_used"])
        self.assertIn("ARIMA", payload["summary"])
        self.assertIn("2.50%", payload["summary"])

    def test_ice_session_calendar_skips_easter_and_christmas_closures(self) -> None:
        """A weekday-only calendar would publish closed dates as forecast targets."""
        from jin10_market_report.price_layer import next_ice_trading_day

        self.assertEqual(next_ice_trading_day(date(2026, 4, 2)), date(2026, 4, 7))
        self.assertEqual(next_ice_trading_day(date(2026, 12, 24)), date(2026, 12, 29))
        self.assertEqual(next_ice_trading_day(date(2026, 8, 21)), date(2026, 8, 24))

    def test_latest_price_session_must_match_the_expected_complete_session(self) -> None:
        """Stale or pre-close bars must make Price Layer unavailable instead of publishing old prices."""
        from jin10_market_report.price_layer import validate_complete_price_session

        complete_rows = [
            {"ts_utc": "2026-08-21T20:55:00Z", "close": 94.39},
        ]
        stale_rows = [
            {"ts_utc": "2026-08-20T20:55:00Z", "close": 93.78},
        ]
        partial_rows = [
            {"ts_utc": "2026-08-21T18:00:00Z", "close": 94.10},
        ]

        self.assertEqual(
            validate_complete_price_session(
                complete_rows,
                expected_session=date(2026, 8, 21),
                timezone_name="Asia/Shanghai",
            ),
            94.39,
        )
        with self.assertRaisesRegex(ValueError, "未覆盖预期交易日"):
            validate_complete_price_session(
                stale_rows,
                expected_session=date(2026, 8, 21),
                timezone_name="Asia/Shanghai",
            )
        with self.assertRaisesRegex(ValueError, "收盘边界"):
            validate_complete_price_session(
                partial_rows,
                expected_session=date(2026, 8, 21),
                timezone_name="Asia/Shanghai",
            )


class PriceLayerRenderingTests(unittest.TestCase):
    def test_price_layer_is_rendered_as_an_independent_news_free_today_section(self) -> None:
        """The new block must not blur historical news attribution with the price-only forecast."""
        from jin10_market_report.tracking_report import (
            _price_layer_html,
            _price_layer_markdown_lines,
        )

        payload = {
            "status": "success",
            "target_date": "2026-08-24",
            "as_of_date": "2026-08-21",
            "current_close": 80.0,
            "model_name": "ARIMA_1_1_1",
            "predicted_price": 82.0,
            "predicted_return": 0.025,
            "predicted_direction": "上涨",
            "direction_accuracy": 0.51,
            "balanced_accuracy": 0.50,
            "mae": 0.02,
            "rmse": 0.03,
            "oos_samples": 283,
            "summary": "Price Layer 当前选择 ARIMA，预测下一交易日 Brent 小幅上涨。",
            "future_data_used": False,
        }

        markdown = "\n".join(_price_layer_markdown_lines(payload))
        html = _price_layer_html(payload)

        for rendered in (markdown, html):
            self.assertIn("今日 Brent 价格预测", rendered)
            self.assertNotIn("明日 Brent 价格预测", rendered)
            self.assertIn("ARIMA", rendered)
            self.assertIn("82.00", rendered)
            self.assertIn("51.00%", rendered)
            self.assertIn("50.00%", rendered)
            self.assertIn("仅使用历史价格数据", rendered)
            self.assertNotIn("CrudeBERT", rendered)

    def test_regular_report_carries_price_layer_to_markdown_and_html_presentation(self) -> None:
        """Passing Price Layer to Audit instead of the page presentation must fail this test."""
        from jin10_market_report.tracking_report import (
            FormalReportPresentation,
            build_markdown_report,
        )

        class Store:
            def raw_news_between(self, *_args):
                return []

            def news_for_reaction_range(self, *_args):
                return []

            def brent_day_klines(self, *_args):
                return []

            def reaction_rows_between(self, *_args):
                return []

            def unpriced_watch_rows(self, *_args):
                return []

        payload = {
            "status": "success",
            "target_date": "2026-09-04",
            "as_of_date": "2026-09-03",
            "current_close": 80.0,
            "model_name": "ARIMA_1_1_1",
            "model_display_name": "ARIMA",
            "predicted_price": 82.0,
            "predicted_return": 0.025,
            "predicted_direction": "上涨",
            "direction_accuracy": 0.51,
            "balanced_accuracy": 0.50,
            "mae": 0.02,
            "rmse": 0.03,
            "oos_samples": 283,
            "summary": "Price Layer 当前选择 ARIMA。",
        }
        movement = {"selected_raw_ids": set(), "groups": [], "unexplained": [], "windows": []}
        homepage = FormalReportPresentation((), (), ())
        audit = {
            "news_freshness": "passed",
            "price_freshness": "passed",
            "data_integrity": "完整",
            "price_continuity_issues": [],
        }
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(
                patch("jin10_market_report.tracking_report._build_window_audit", return_value=audit)
            )
            stack.enter_context(
                patch("jin10_market_report.tracking_report._build_price_movement_review", return_value=movement)
            )
            stack.enter_context(
                patch(
                    "jin10_market_report.tracking_report._build_event_grouped_homepage",
                    return_value=homepage,
                )
            )
            stack.enter_context(
                patch("jin10_market_report.tracking_report.aggregate_30m_klines", return_value=[])
            )
            stack.enter_context(
                patch(
                    "jin10_market_report.tracking_report.prepare_interactive_chart_markers",
                    return_value=([], []),
                )
            )
            stack.enter_context(
                patch(
                    "jin10_market_report.tracking_report.brent_day_summary",
                    return_value={"latest": "80.00", "pct": "+1.00%", "high": "81.00", "low": "79.00"},
                )
            )
            stack.enter_context(
                patch("jin10_market_report.tracking_report.classify_report_status", return_value=("FORMAL", {}))
            )
            stack.enter_context(
                patch("jin10_market_report.tracking_report._build_audit_report_lines", return_value=["# audit"])
            )
            stack.enter_context(patch("jin10_market_report.tracking_report.build_html_report"))
            stack.enter_context(
                patch(
                    "jin10_market_report.price_layer.build_daily_price_forecast_safe",
                    return_value=payload,
                )
            )
            result = build_markdown_report(
                Store(),
                SimpleNamespace(source="Yahoo Finance", symbol="BZ=F", interval="5m"),
                Path(directory),
                day=datetime(2026, 9, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
                external_update={"status": "not_applicable", "key_takeaways": []},
                trump_update={"status": "not_applicable", "statements": []},
                read_only_source=True,
            )

            rendered = result.markdown_path.read_text(encoding="utf-8")

        self.assertIn("## 今日 Brent 价格预测", rendered)
        self.assertNotIn("## 明日 Brent 价格预测", rendered)
        self.assertLess(
            rendered.index("## 今日重点（Executive Summary）"),
            rendered.index("## 今日 Brent 价格预测"),
        )
        self.assertLess(
            rendered.index("## 今日 Brent 价格预测"),
            rendered.index("统计窗口：2026-09-03 08:00 至 2026-09-04 08:00"),
        )
        self.assertEqual(result.presentation.price_layer, payload)

    def test_formal_html_places_today_price_layer_immediately_after_executive_summary(self) -> None:
        """Moving the card back below the report body must break the visible page order."""
        from jin10_market_report.tracking_report import _formal_dashboard_body

        payload = {
            "status": "success",
            "target_date": "2026-09-04",
            "as_of_date": "2026-09-03",
            "current_close": 80.0,
            "model_display_name": "ARIMA",
            "predicted_price": 82.0,
            "predicted_return": 0.025,
            "predicted_direction": "上涨",
            "direction_accuracy": 0.51,
            "balanced_accuracy": 0.50,
            "summary": "Price Layer 当前选择 ARIMA。",
        }
        rendered = "\n".join(
            _formal_dashboard_body(
                Path("/tmp/report.md"),
                {
                    "title": "Brent日报",
                    "top_summary_items": ["摘要"],
                    "price_layer": payload,
                },
            )
        )

        self.assertIn("今日 Brent 价格预测", rendered)
        self.assertLess(rendered.index('class="executive-summary"'), rendered.index('class="price-layer-card"'))
        self.assertLess(rendered.index('class="price-layer-card"'), rendered.index('class="market-lede"'))


if __name__ == "__main__":
    unittest.main()
