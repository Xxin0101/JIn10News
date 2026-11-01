from __future__ import annotations

import tempfile
import unittest
import calendar
import io
import inspect
import json
import sys
import types
from contextlib import ExitStack, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from zoneinfo import ZoneInfo


class ForecastFeatureTests(unittest.TestCase):
    def _bars(self, count: int = 36):
        from jin10_market_report.forecast import DailyBar

        start = date(2026, 6, 1)
        bars = []
        value = 70.0
        for offset in range(count):
            day = start + timedelta(days=offset)
            value += 0.4 if offset % 3 else -0.15
            bars.append(
                DailyBar(
                    trading_date=day,
                    open=value - 0.2,
                    high=value + 0.5,
                    low=value - 0.6,
                    close=value,
                    volume=1000.0 + offset,
                )
            )
        return bars

    def test_price_dataset_never_trains_on_the_unknown_target_label(self) -> None:
        """Including the current feature row in training would leak its next-session label."""
        from jin10_market_report.forecast import build_price_dataset

        bars = self._bars()
        samples, current = build_price_dataset(bars, as_of_date=bars[-1].trading_date)

        self.assertEqual(current.feature_date, bars[-1].trading_date)
        self.assertIsNone(current.label)
        self.assertTrue(samples)
        self.assertTrue(all(sample.label_date <= bars[-1].trading_date for sample in samples))
        self.assertTrue(all(sample.feature_date < current.feature_date for sample in samples))

    def test_walk_forward_training_labels_are_known_at_each_prediction_time(self) -> None:
        """A walk-forward fold must not train on a label whose close occurs after the fold date."""
        from jin10_market_report.forecast import build_price_dataset, walk_forward_backtest

        samples, _ = build_price_dataset(self._bars(48), as_of_date=self._bars(48)[-1].trading_date)
        result = walk_forward_backtest(samples, min_train_size=8, test_window=12, model_kind="logistic")

        self.assertEqual(result.test_samples, 12)
        self.assertTrue(result.predictions)
        for prediction in result.predictions:
            self.assertLess(prediction.train_feature_end, prediction.feature_date)
            self.assertLessEqual(prediction.train_label_end, prediction.feature_date)

    def test_metric_values_are_hand_checkable_and_not_accuracy_only(self) -> None:
        """Dropping probability or class-balance metrics would hide a biased model."""
        from jin10_market_report.forecast import classification_metrics

        metrics = classification_metrics([1, 0, 1, 0], [0.8, 0.7, 0.6, 0.1])

        self.assertAlmostEqual(metrics["accuracy"], 0.75)
        self.assertAlmostEqual(metrics["balanced_accuracy"], 0.75)
        self.assertAlmostEqual(metrics["auc"], 0.75)
        self.assertAlmostEqual(metrics["brier_score"], 0.175)
        self.assertAlmostEqual(metrics["f1"], 0.8)
        self.assertAlmostEqual(metrics["predicted_up_rate"], 0.75)

    def test_daily_payload_parser_hard_stops_at_the_as_of_trade_date(self) -> None:
        """A Yahoo bar after August 21 must never enter the August 24 forecast features."""
        from jin10_market_report.forecast import parse_yahoo_daily_payload

        timestamps = [
            calendar.timegm(datetime(2026, 8, 20).timetuple()),
            calendar.timegm(datetime(2026, 8, 21).timetuple()),
            calendar.timegm(datetime(2026, 8, 24).timetuple()),
        ]
        payload = {
            "chart": {
                "error": None,
                "result": [
                    {
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": [70.0, 71.0, 99.0],
                                    "high": [71.0, 72.0, 100.0],
                                    "low": [69.0, 70.0, 98.0],
                                    "close": [70.5, 71.5, 99.5],
                                    "volume": [100.0, 110.0, 999.0],
                                }
                            ]
                        },
                    }
                ],
            }
        }

        bars = parse_yahoo_daily_payload(payload, as_of_date=date(2026, 8, 21))

        self.assertEqual([bar.trading_date for bar in bars], [date(2026, 8, 20), date(2026, 8, 21)])
        self.assertEqual(bars[-1].close, 71.5)

    def test_quantitative_forecast_returns_model_probability_and_walk_forward_metrics(self) -> None:
        """The displayed probability must be produced by the fitted model and backed by dated folds."""
        from jin10_market_report.forecast import quantitative_forecast

        bars = self._bars(90)
        result = quantitative_forecast(
            bars,
            as_of_date=bars[-1].trading_date,
            model_kind="logistic",
            min_train_size=24,
            test_window=16,
        )

        self.assertEqual(result["as_of_date"], bars[-1].trading_date.isoformat())
        self.assertGreaterEqual(result["up_probability"], 0.0)
        self.assertLessEqual(result["up_probability"], 1.0)
        self.assertIn("Logistic Regression", result["model_name"])
        self.assertEqual(result["backtest"]["test_samples"], 16)
        self.assertEqual(
            set(result["backtest"]["metrics"]),
            {"accuracy", "balanced_accuracy", "auc", "brier_score", "f1", "predicted_up_rate"},
        )

    def test_lightgbm_adapter_uses_native_booster_without_sklearn(self) -> None:
        """The production LightGBM path must not add an undeclared scikit-learn dependency."""
        from jin10_market_report.forecast import _LightGBMModel

        captured: dict[str, object] = {}

        class FakeDataset:
            def __init__(self, data, label, free_raw_data=False):
                captured["data"] = data
                captured["label"] = label

        class FakeBooster:
            def predict(self, rows):
                captured["predict_rows"] = rows
                return [0.61]

        def fake_train(params, train_set, num_boost_round):
            captured["params"] = params
            captured["rounds"] = num_boost_round
            return FakeBooster()

        fake_lightgbm = types.SimpleNamespace(Dataset=FakeDataset, train=fake_train)
        fake_numpy = types.SimpleNamespace(asarray=lambda rows, dtype=None: rows)
        with patch.dict(sys.modules, {"lightgbm": fake_lightgbm, "numpy": fake_numpy}):
            model = _LightGBMModel().fit([[1.0, 2.0], [2.0, 3.0]], [0, 1])
            probability = model.predict_probability([3.0, 4.0])

        self.assertEqual(captured["rounds"], 120)
        self.assertEqual(captured["label"], [0, 1])
        self.assertEqual(captured["predict_rows"], [[3.0, 4.0]])
        self.assertAlmostEqual(probability, 0.61)

    def test_short_history_secondary_models_run_expanding_window_without_claiming_reliability(self) -> None:
        """B/C must produce real chronological metrics while flagging a 12-fold holdout as exploratory."""
        from jin10_market_report import forecast as forecast_module
        from jin10_market_report.forecast import PriceSample

        secondary_model_backtests = getattr(forecast_module, "secondary_model_backtests", None)
        self.assertTrue(callable(secondary_model_backtests))
        start = date(2026, 7, 1)
        samples = []
        price_probabilities = {}
        for offset in range(24):
            feature_date = start + timedelta(days=offset)
            label = 1 if offset % 3 else 0
            samples.append(
                PriceSample(
                    feature_date=feature_date,
                    features=(float(label), float(offset % 2), float(offset % 5) / 5.0),
                    label=label,
                    label_date=feature_date + timedelta(days=1),
                )
            )
            price_probabilities[feature_date] = 0.58 if label else 0.42

        result = secondary_model_backtests(
            samples,
            current_news_features=(1.0, 0.0, 0.2),
            price_probabilities=price_probabilities,
            current_price_probability=0.54,
            min_train_size=8,
            test_window=12,
        )

        for key in ("model_a_overlap", "model_b", "model_c"):
            self.assertEqual(result[key]["test_samples"], 12)
            self.assertEqual(
                set(result[key]["metrics"]),
                {"accuracy", "balanced_accuracy", "auc", "brier_score", "f1", "predicted_up_rate"},
            )
        self.assertEqual(result["model_b"]["status"], "short_history_backtest")
        self.assertFalse(result["model_c"]["probability_eligible"])
        self.assertEqual(result["model_c"]["reliability"], "EXPLORATORY")
        self.assertGreaterEqual(result["model_b"]["up_probability"], 0.0)
        self.assertLessEqual(result["model_b"]["up_probability"], 1.0)

    def test_news_feature_samples_use_two_covered_days_and_next_session_label(self) -> None:
        """A missing news day must not be silently treated as a genuine zero-event feature row."""
        from jin10_market_report import forecast as forecast_module
        from jin10_market_report.forecast import PriceSample

        build_news_feature_samples = getattr(forecast_module, "build_news_feature_samples", None)
        self.assertTrue(callable(build_news_feature_samples))
        price_samples = [
            PriceSample(date(2026, 7, day), (0.0,), day % 2, date(2026, 7, day + 1))
            for day in range(3, 9)
        ]
        events = [
            {
                "published_at": f"2026-07-{day:02d}T10:00:00+08:00",
                "direction": "bullish" if day % 2 else "bearish",
                "strength": "strong",
                "category": "Supply",
                "pricing_status": "LOW",
            }
            for day in (3, 4, 6, 7, 8)
        ]

        samples = build_news_feature_samples(
            events,
            price_samples,
            covered_dates={date(2026, 7, day) for day in (3, 4, 6, 7, 8)},
        )

        self.assertEqual([sample.feature_date for sample in samples], [date(2026, 7, 4), date(2026, 7, 7), date(2026, 7, 8)])
        self.assertEqual(samples[-1].label_date, date(2026, 7, 9))

    def test_local_session_bars_end_at_friday_close_and_never_require_weekend_prices(self) -> None:
        """A Friday snapshot must map early-Saturday futures bars back to Friday's session."""
        from jin10_market_report.forecast import build_local_session_daily_bars

        rows = [
            {"ts_utc": "2026-08-21T00:00:00+00:00", "open": 70, "high": 71, "low": 69, "close": 70.5, "volume": 10},
            {"ts_utc": "2026-08-21T20:55:00+00:00", "open": 72, "high": 73, "low": 71, "close": 72.5, "volume": 20},
            {"ts_utc": "2026-08-22T02:00:00+00:00", "open": 99, "high": 100, "low": 98, "close": 99.5, "volume": 99},
        ]

        bars = build_local_session_daily_bars(
            rows,
            as_of_date=date(2026, 8, 21),
            timezone_name="Asia/Shanghai",
        )

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].trading_date, date(2026, 8, 21))
        self.assertEqual(bars[0].close, 72.5)
        self.assertEqual(bars[0].volume, 30.0)

    def test_short_overlap_price_features_allow_four_fold_news_model_review(self) -> None:
        """B/C overlap should not inherit Model A's 20-day warm-up when news history is short."""
        from jin10_market_report.forecast import build_short_price_dataset

        bars = self._bars(12)
        samples, current = build_short_price_dataset(bars, as_of_date=bars[-1].trading_date)

        self.assertEqual(len(samples), 8)
        self.assertEqual(len(current.features), 5)
        self.assertEqual(current.feature_date, bars[-1].trading_date)


class ForecastNewsTests(unittest.TestCase):
    def test_same_day_reactions_never_cross_the_price_cutoff(self) -> None:
        """Late target-morning news cannot use 30m/2h reactions observed after 08:00."""
        from jin10_market_report.forecast import build_news_forecast_from_store

        row = {
            "news_id": "late-event",
            "event_id": "late-event",
            "published_at_utc": "2026-08-23T23:55:00+00:00",
            "published_at_bj": "2026-08-24T07:55:00+08:00",
            "title": "霍尔木兹海峡宣布临时关闭",
            "content": "海峡关闭导致原油运输中断。",
            "event_base_time_utc": "2026-08-23T23:55:00+00:00",
            "event_base_close": 100.0,
            "pct_5m": 0.1,
            "pct_30m": 0.5,
            "pct_60m": 0.8,
            "url": "https://flash.jin10.com/detail/late-event",
        }

        class Store:
            nearest_targets: list[datetime] = []

            def reaction_rows_between(self, start, *_args):
                return [row] if start.year == 2026 else []

            def window_klines(self, *_args):
                return []

            def nearest_kline(self, _symbol, _interval, target):
                self.nearest_targets.append(target)
                return {"ts_utc": target.isoformat(), "close": 102.0}

            def kline_at_or_before(self, *_args):
                return {"ts_utc": "2026-08-24T00:00:00+00:00", "close": 100.1}

        store = Store()
        result = build_news_forecast_from_store(
            store,
            symbol="BZ=F",
            interval="5m",
            start_utc=datetime(2026, 8, 21, 0, 0, tzinfo=ZoneInfo("UTC")),
            end_utc=datetime(2026, 8, 24, 0, 0, tzinfo=ZoneInfo("UTC")),
            price_end_utc=datetime(2026, 8, 24, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        reactions = result["events"][0]["reaction_returns"]
        self.assertEqual(reactions["5m"], 0.1)
        self.assertIsNone(reactions["30m"])
        self.assertIsNone(reactions["2h"])
        self.assertEqual(store.nearest_targets, [])

    def test_a_then_c_uses_oil_transmission_not_article_sentiment(self) -> None:
        """Generic negative prose must not become bearish oil news, while supply cuts must be bullish."""
        from jin10_market_report.forecast import classify_oil_news

        cut = classify_oil_news("OPEC+宣布进一步减产", "多个成员国将削减原油产量和出口。")
        inventory = classify_oil_news("美国原油库存大幅增加", "EIA数据显示商业原油库存增加980万桶。")
        company = classify_oil_news("科技公司利润下滑", "公司称前景严峻，股价下跌。")

        self.assertTrue(cut.is_oil_relevant)
        self.assertEqual((cut.direction, cut.strength, cut.category), ("bullish", "strong", "OPEC+"))
        self.assertIn("供应减少", cut.transmission_chain)
        self.assertEqual((inventory.direction, inventory.strength), ("bearish", "strong"))
        self.assertIn("可用供应增加", inventory.transmission_chain)
        self.assertFalse(company.is_oil_relevant)
        self.assertEqual(company.direction, "neutral")

    def test_a_rejects_macro_story_where_oil_is_only_background_context(self) -> None:
        """A trade-balance article must not become an oil signal because its body mentions Hormuz."""
        from jin10_market_report.forecast import classify_oil_news

        assessment = classify_oil_news(
            "瑞穗：日本贸易逆差短期内或将持续",
            "能源价格推高进口成本，在霍尔木兹海峡航运恢复前进口价格维持高位。",
        )

        self.assertFalse(assessment.is_oil_relevant)
        self.assertEqual(assessment.direction, "neutral")

    def test_c_distinguishes_hormuz_deescalation_from_talks_breakdown(self) -> None:
        """Negotiation progress lowers risk premium; a genuine breakdown raises it."""
        from jin10_market_report.forecast import classify_oil_news

        progress = classify_oil_news(
            "伊朗与阿曼就霍尔木兹海峡船舶通行机制谈判接近尾声",
            "双方讨论恢复正常通行。",
        )
        breakdown = classify_oil_news(
            "美媒：美方认为伊阿会谈几周前便已破裂",
            "会谈曾讨论霍尔木兹海峡收费协议，目前陷入僵局。",
        )

        self.assertEqual(progress.direction, "bearish")
        self.assertIn("风险缓和", progress.transmission_chain)
        self.assertEqual(breakdown.direction, "bullish")
        self.assertIn("风险升级", breakdown.transmission_chain)

    def test_c_treats_lower_military_probability_as_bearish_not_sanctions_bullish(self) -> None:
        """The headline's new fact must outrank background discussion of future sanctions."""
        from jin10_market_report.forecast import classify_oil_news

        lower_risk = classify_oil_news(
            "以色列消息人士：短期与伊朗产生军事对抗可能性降低",
            "官员同时讨论未来通过石油贸易实施经济制裁的可能性。",
        )

        self.assertEqual(lower_risk.direction, "bearish")
        self.assertEqual(lower_risk.category, "Geopolitical")

    def test_c_keeps_calls_for_talks_neutral_until_progress_occurs(self) -> None:
        """A requested diplomatic outcome is a future scenario, not an observed de-escalation."""
        from jin10_market_report.forecast import classify_oil_news

        call = classify_oil_news(
            "联合国呼吁海湾地区有关各方重返谈判",
            "联合国呼吁恢复霍尔木兹海峡安全通行。",
        )

        self.assertTrue(call.is_oil_relevant)
        self.assertEqual(call.direction, "neutral")

    def test_c_uses_crude_production_in_mixed_jodi_release(self) -> None:
        """A large crude-production increase must not flip bullish because product demand fell."""
        from jin10_market_report.forecast import classify_oil_news

        release = classify_oil_news(
            "JODI：沙特6月石油产品需求下降，原油产量环比增加56.2万桶/日",
            "原油产量升至712万桶/日。",
        )

        self.assertEqual(release.direction, "bearish")
        self.assertIn("供应增加", release.transmission_chain)

    def test_c_reads_successful_hormuz_escort_as_risk_reduction(self) -> None:
        """Crude successfully moving through a blockade is de-escalation, not a blockade shock."""
        from jin10_market_report.forecast import classify_oil_news

        escort = classify_oil_news(
            "美军声称正护航霍尔木兹海峡，逾6.6亿桶原油已突破封锁",
            "护航行动帮助原油运出。",
        )

        self.assertEqual(escort.direction, "bearish")

    def test_c_keeps_conditional_military_threat_as_neutral_future_risk(self) -> None:
        """A conditional statement is not an observed closure, reopening, or military action."""
        from jin10_market_report.forecast import classify_oil_news

        scenario = classify_oil_news(
            "若伊朗拒绝重新开放霍尔木兹海峡，美国可能会考虑军事行动",
            "官员描述了尚未发生的条件情景。",
        )

        self.assertTrue(scenario.is_oil_relevant)
        self.assertEqual(scenario.direction, "neutral")

    def test_c_does_not_use_six_year_supply_plan_for_next_monday(self) -> None:
        """A remote production aspiration is not an immediate next-session supply shock."""
        from jin10_market_report.forecast import classify_oil_news

        plan = classify_oil_news(
            "伊拉克计划在六年内将石油产量提高到800-1000万桶/日",
            "官员将讨论未来扩大欧佩克生产配额。",
        )

        self.assertTrue(plan.is_oil_relevant)
        self.assertEqual(plan.direction, "neutral")

    def test_c_does_not_infer_supply_drop_from_background_price_clause(self) -> None:
        """A body sentence about adding supply to lower prices is not evidence that supply fell."""
        from jin10_market_report.forecast import classify_oil_news

        pressure = classify_oil_news(
            "万斯：美国将继续对伊朗施加经济压力",
            "如果能够增加足够多的石油和天然气供应，让能源价格有所下降，将改变谈判考量。",
        )

        self.assertTrue(pressure.is_oil_relevant)
        self.assertEqual(pressure.direction, "neutral")

    def test_a_excludes_multi_event_digests_and_positioning_articles(self) -> None:
        """Mixed digests and market positioning cannot be counted as one causal news event."""
        from jin10_market_report.forecast import classify_oil_news

        digest = classify_oil_news(
            "金十数据整理：每日全球大宗商品市场要闻速递（2026-08-20）",
            "制裁、库存和产量等多条消息汇总。",
        )
        positioning = classify_oil_news(
            "对冲基金转为看涨欧洲柴油",
            "交易员增持看涨押注，背景包括供应紧张。",
        )

        self.assertFalse(digest.is_oil_relevant)
        self.assertFalse(positioning.is_oil_relevant)

    def test_duplicate_updates_count_as_one_event(self) -> None:
        """Repeated flashes for one event must not inflate structured news features."""
        from jin10_market_report.forecast import classify_oil_news, deduplicate_news_events

        first = classify_oil_news(
            "OPEC+宣布进一步减产100万桶/日",
            "OPEC+成员将削减原油产量。",
            news_id="1",
            event_id="opec-cut",
            published_at="2026-08-21T10:00:00+08:00",
        )
        update = classify_oil_news(
            "OPEC+减产消息更新",
            "OPEC+确认此前100万桶/日减产安排。",
            news_id="2",
            event_id="opec-cut",
            published_at="2026-08-21T10:03:00+08:00",
        )

        deduped = deduplicate_news_events([first, update])

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].source_news_ids, ("1", "2"))

    def test_material_update_hours_later_is_not_collapsed_into_the_first_flash(self) -> None:
        """An actual later escalation must survive even when the broad event_id is unchanged."""
        from jin10_market_report.forecast import classify_oil_news, deduplicate_news_events

        first = classify_oil_news(
            "霍尔木兹航运风险上升",
            "一艘油轮报告通行受阻。",
            news_id="1",
            event_id="hormuz",
            published_at="2026-08-21T10:00:00+08:00",
        )
        escalation = classify_oil_news(
            "霍尔木兹海峡宣布临时关闭",
            "海峡关闭导致原油运输中断。",
            news_id="2",
            event_id="hormuz",
            published_at="2026-08-21T18:00:00+08:00",
        )

        deduped = deduplicate_news_events([first, escalation])

        self.assertEqual(len(deduped), 2)

    def test_b_pricing_status_is_relative_to_contemporaneous_volatility(self) -> None:
        """Pricing status must compare the aligned reaction with the local move distribution."""
        from jin10_market_report.forecast import assess_market_pricing

        full = assess_market_pricing(
            "bullish",
            {"5m": 0.25, "30m": 0.82, "2h": 0.91, "end": 0.74},
            historical_abs_30m_returns=[0.08, 0.12, 0.18, 0.24, 0.35, 0.42],
        )
        low = assess_market_pricing(
            "bullish",
            {"5m": 0.01, "30m": 0.03, "2h": -0.02, "end": 0.01},
            historical_abs_30m_returns=[0.08, 0.12, 0.18, 0.24, 0.35, 0.42],
        )

        self.assertEqual(full.status, "FULL")
        self.assertEqual(low.status, "LOW")
        self.assertGreater(full.volatility_multiple, 1.0)

    def test_insufficient_news_history_forbids_numeric_final_probability(self) -> None:
        """A qualitative news view must not be converted into invented percentage points."""
        from jin10_market_report.forecast import combine_forecasts

        combined = combine_forecasts(
            quant_up_probability=0.56,
            quant_metrics={"balanced_accuracy": 0.54, "brier_score": 0.247},
            news_direction="bullish",
            news_model_metrics=None,
            combined_model_metrics=None,
        )

        self.assertEqual(combined.direction, "偏多")
        self.assertEqual(combined.confidence, "MEDIUM")
        self.assertIsNone(combined.final_up_probability)
        self.assertIn("历史新闻样本不足", combined.probability_note)

    def test_four_fold_combined_model_is_shown_but_cannot_override_probability(self) -> None:
        """A real four-fold B/C backtest is evidence, not enough validation for a numeric correction."""
        from jin10_market_report.forecast import combine_forecasts

        combined = combine_forecasts(
            quant_up_probability=0.54,
            quant_metrics={"balanced_accuracy": 0.51, "brier_score": 0.25},
            news_direction="bearish",
            news_model_metrics={"brier_score": 0.33},
            combined_model_metrics={"brier_score": 0.37},
            combined_up_probability=0.21,
            combined_probability_eligible=False,
            secondary_test_samples=4,
        )

        self.assertIsNone(combined.final_up_probability)
        self.assertIn("4折", combined.probability_note)
        self.assertIn("不用于修正", combined.probability_note)

    def test_fully_priced_event_is_explanation_not_forward_signal(self) -> None:
        """A FULL event must stay in priced events and contribute zero to Monday's news score."""
        from jin10_market_report.forecast import aggregate_news_forecast

        summary = aggregate_news_forecast(
            [
                {
                    "title": "供应中断已经推动油价上涨",
                    "direction": "bullish",
                    "strength": "strong",
                    "category": "Supply",
                    "pricing_status": "FULL",
                    "transmission_chain": "供应减少 → Brent利多。",
                },
                {
                    "title": "制裁执行仍在发展",
                    "direction": "bearish",
                    "strength": "medium",
                    "category": "Sanctions",
                    "pricing_status": "PARTIAL",
                    "transmission_chain": "出口供应增加 → Brent利空。",
                },
            ]
        )

        self.assertEqual(summary["direction"], "bearish")
        self.assertEqual(len(summary["reasons"]), 1)
        self.assertIn("制裁执行仍在发展", summary["reasons"][0])
        self.assertEqual(summary["priced_events"], ["供应中断已经推动油价上涨"])


class ForecastReadOnlyPipelineTests(unittest.TestCase):
    def _snapshot_payload(self) -> dict[str, object]:
        metrics = {
            "accuracy": 0.52,
            "balanced_accuracy": 0.51,
            "auc": 0.53,
            "brier_score": 0.25,
            "f1": 0.61,
            "predicted_up_rate": 0.70,
        }
        return {
            "analysis_window_start_utc": "2026-08-20T00:00:00+00:00",
            "analysis_window_end_utc": "2026-08-21T21:00:00+00:00",
            "news_window_start_utc": "2026-08-20T00:00:00+00:00",
            "news_window_end_utc": "2026-08-21T16:00:00+00:00",
            "target_date": "2026-08-24",
            "daily_price_last_date": "2026-08-21",
            "future_data_used": False,
            "quant": {
                "as_of_date": "2026-08-21",
                "model_name": "LightGBM",
                "up_probability": 0.54,
                "down_probability": 0.46,
                "reasons": ["周五动量为正"],
                "backtest": {"metrics": metrics, "test_samples": 504},
            },
            "news": {
                "direction": "bullish",
                "reasons": ["周五供应风险仍未完全计价"],
                "priced_events": ["已计价事件"],
                "weekend_risks": ["仅作情景说明，不读取周末新闻"],
                "events": [
                    {
                        "news_id": "friday-event",
                        "published_at": "2026-08-21T23:29:00+08:00",
                        "title": "周五供应风险",
                        "direction": "bullish",
                        "strength": "medium",
                        "category": "Supply",
                        "pricing_status": "PARTIAL",
                        "transmission_chain": "供应风险上升 → Brent利多。",
                    }
                ],
                "model_b": {"metrics": metrics, "test_samples": 12},
                "model_c": {
                    "metrics": metrics,
                    "test_samples": 12,
                    "up_probability": 0.55,
                    "probability_eligible": False,
                },
                "history_sessions": 34,
                "structured_features": {"bullish": 1.0, "bearish": 0.0},
                "important_news": [],
            },
            "final": {
                "direction": "偏多",
                "confidence": "LOW",
                "final_up_probability": None,
                "probability_note": "历史新闻样本不足，不展示最终概率。",
            },
        }

    def _configs(self, directory: str):
        tracker = SimpleNamespace(
            timezone="Asia/Shanghai",
            output_dir=directory,
            database_path=str(Path(directory) / "tracker.sqlite"),
        )
        brent = SimpleNamespace(
            source="Yahoo Finance",
            symbol="BZ=F",
            interval="5m",
        )
        logger = SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)
        return SimpleNamespace(), SimpleNamespace(), brent, tracker, logger

    def test_weekend_pipeline_uses_only_saved_friday_snapshot(self) -> None:
        """Adding any fetch, backfill, refit, or news recomputation must break the weekend command."""
        from jin10_market_report import forecast_pipeline as pipeline

        def forbidden(*_args, **_kwargs):
            raise AssertionError("周末预测不得调用抓取、回补或模型重算")

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            stem = "Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824"
            snapshot_path = output_dir / f"{stem}_friday_snapshot.json"
            snapshot_path.write_text(
                json.dumps(self._snapshot_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            markdown_path = output_dir / f"{stem}.md"
            html_path = output_dir / f"{stem}.html"

            def render_markdown(*_args, **kwargs):
                markdown_path.write_text("# 周一方向预测\n", encoding="utf-8")
                return SimpleNamespace(
                    markdown_path=markdown_path,
                    presentation=SimpleNamespace(forecast=dict(kwargs["forecast"])),
                )

            def render_html(*_args, **_kwargs):
                html_path.write_text("<h1>周一方向预测</h1>", encoding="utf-8")
                return html_path

            fetch_config, filter_config, brent, tracker, logger = self._configs(directory)
            with ExitStack() as stack:
                for name in (
                    "fetch_yahoo_daily_bars",
                    "quantitative_forecast",
                    "build_news_forecast_from_store",
                    "fetch_latest_isw_update",
                    "fetch_trump_statements",
                ):
                    stack.enter_context(patch.object(pipeline, name, side_effect=forbidden, create=True))
                stack.enter_context(
                    patch("jin10_market_report.tracker.run_backfill_day", side_effect=forbidden)
                )
                stack.enter_context(patch.object(pipeline, "build_markdown_report", side_effect=render_markdown))
                stack.enter_context(patch.object(pipeline, "build_html_report", side_effect=render_html))
                result = pipeline.run_forecast_report(
                    object(),
                    fetch_config,
                    filter_config,
                    brent,
                    tracker,
                    logger,
                    start_date="2026-08-20",
                    end_date="2026-08-21",
                    target_date="2026-08-24",
                )

        self.assertEqual(result["backfills"], [])
        self.assertEqual(result["quant"]["up_probability"], 0.54)
        self.assertEqual(result["news"]["direction"], "bullish")
        self.assertEqual(result["forecast"]["direction"], "偏多")

    def test_frozen_audit_does_not_require_a_weekend_end_bar(self) -> None:
        """Removing Saturday bars must not make a validated Friday snapshot stale or partial."""
        from jin10_market_report import tracking_report

        raw_row = {
            "published_at_utc": "2026-08-21T15:55:00+00:00",
            "title": "周五油市消息",
            "body": "",
            "detail": "",
            "is_summary": 0,
        }
        price_row = {
            "ts_utc": "2026-08-21T20:55:00+00:00",
            "open": 70.0,
            "high": 70.2,
            "low": 69.9,
            "close": 70.1,
        }

        class FrozenStore:
            def raw_news_between(self, *_args):
                return [raw_row]

            def news_for_reaction_range(self, *_args):
                return []

            def brent_day_klines(self, *_args):
                return [price_row]

            def reaction_rows_between(self, *_args):
                return []

        audit = tracking_report._build_frozen_snapshot_audit(
            FrozenStore(),
            SimpleNamespace(symbol="BZ=F", interval="5m"),
            datetime(2026, 8, 20, 0, 0, tzinfo=ZoneInfo("UTC")),
            datetime(2026, 8, 22, 0, 0, tzinfo=ZoneInfo("UTC")),
            "Asia/Shanghai",
            news_start_utc=datetime(2026, 8, 20, 0, 0, tzinfo=ZoneInfo("UTC")),
            news_end_utc=datetime(2026, 8, 21, 16, 0, tzinfo=ZoneInfo("UTC")),
        )

        self.assertEqual(audit["freshness_policy"], "frozen_friday_snapshot")
        self.assertEqual(audit["news_freshness"], "not_applicable")
        self.assertEqual(audit["price_freshness"], "not_applicable")
        self.assertTrue(audit["price_end_covered"])
        self.assertFalse(
            any("窗口截止未覆盖" in item for item in audit["coverage_gaps"])
        )

    def test_frozen_report_skips_realtime_freshness_audit(self) -> None:
        """Calling the live freshness audit from a frozen report must fail this test."""
        from jin10_market_report.tracking_report import (
            FormalReportPresentation,
            build_markdown_report,
        )

        raw_row = {
            "published_at_utc": "2026-08-21T15:55:00+00:00",
            "title": "周五油市消息",
            "body": "",
            "detail": "",
            "is_summary": 0,
            "fetch_batch_id": "friday",
            "fetch_page": 1,
            "source_channel": "flash",
        }
        price_row = {
            "ts_utc": "2026-08-21T20:55:00+00:00",
            "open": 70.0,
            "high": 70.2,
            "low": 69.9,
            "close": 70.1,
        }

        class FrozenStore:
            def raw_news_between(self, *_args):
                return [raw_row]

            def news_for_reaction_range(self, *_args):
                return []

            def brent_day_klines(self, *_args):
                return [price_row]

            def reaction_rows_between(self, *_args):
                return []

            def unpriced_watch_rows(self, *_args):
                return []

        movement = {"selected_raw_ids": set(), "groups": [], "unexplained": [], "windows": []}
        empty_homepage = FormalReportPresentation((), (), ())
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(
                patch(
                    "jin10_market_report.tracking_report._build_window_audit",
                    side_effect=AssertionError("冻结快照不得执行实时freshness审计"),
                )
            )
            stack.enter_context(
                patch(
                    "jin10_market_report.tracking_report.classify_report_status",
                    side_effect=AssertionError("冻结快照不得由实时freshness状态降级"),
                )
            )
            stack.enter_context(
                patch(
                    "jin10_market_report.tracking_report._build_price_movement_review",
                    return_value=movement,
                )
            )
            stack.enter_context(
                patch(
                    "jin10_market_report.tracking_report._build_event_grouped_homepage",
                    return_value=empty_homepage,
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
                    return_value={"latest": "70.1", "pct": "+0.2%", "high": "70.2", "low": "69.9"},
                )
            )
            stack.enter_context(
                patch(
                    "jin10_market_report.tracking_report._build_audit_report_lines",
                    return_value=["# audit"],
                )
            )
            stack.enter_context(patch("jin10_market_report.tracking_report.build_html_report"))
            result = build_markdown_report(
                FrozenStore(),
                SimpleNamespace(source="Yahoo Finance", symbol="BZ=F", interval="5m"),
                Path(directory),
                external_update={"status": "not_applicable", "statements": []},
                trump_update={"status": "not_applicable", "statements": []},
                window_start_utc=datetime(2026, 8, 20, 0, 0, tzinfo=ZoneInfo("UTC")),
                window_end_utc=datetime(2026, 8, 22, 0, 0, tzinfo=ZoneInfo("UTC")),
                news_window_start_utc=datetime(2026, 8, 20, 0, 0, tzinfo=ZoneInfo("UTC")),
                news_window_end_utc=datetime(2026, 8, 21, 16, 0, tzinfo=ZoneInfo("UTC")),
                output_stem="frozen_forecast",
                forecast={"target_date": "2026年8月24日", "direction": "偏多", "confidence": "LOW"},
                read_only_source=True,
                frozen_snapshot=True,
            )

            self.assertEqual(result.markdown_path.name, "frozen_forecast.md")
            rendered = result.markdown_path.read_text(encoding="utf-8")
            self.assertNotIn("抓取失败", rendered)
            self.assertIn("周五快照模式未纳入", rendered)

    def test_same_snapshot_reuses_existing_report(self) -> None:
        """Reintroducing a second full render for the same snapshot must break this test."""
        from jin10_market_report import forecast_pipeline as pipeline

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            stem = "Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824"
            snapshot_path = output_dir / f"{stem}_friday_snapshot.json"
            snapshot_path.write_text(
                json.dumps(self._snapshot_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            markdown_path = output_dir / f"{stem}.md"
            html_path = output_dir / f"{stem}.html"

            def render_markdown(*_args, **kwargs):
                markdown_path.write_text("# 首次渲染\n", encoding="utf-8")
                return SimpleNamespace(
                    markdown_path=markdown_path,
                    presentation=SimpleNamespace(forecast=dict(kwargs["forecast"])),
                )

            def render_html(*_args, **_kwargs):
                html_path.write_text("<h1>首次渲染</h1>", encoding="utf-8")
                return html_path

            fetch_config, filter_config, brent, tracker, logger = self._configs(directory)
            with patch.object(pipeline, "build_markdown_report", side_effect=render_markdown), patch.object(
                pipeline,
                "build_html_report",
                side_effect=render_html,
            ):
                first = pipeline.run_forecast_report(
                    object(),
                    fetch_config,
                    filter_config,
                    brent,
                    tracker,
                    logger,
                    start_date="2026-08-20",
                    end_date="2026-08-21",
                    target_date="2026-08-24",
                )

            with patch.object(
                pipeline,
                "build_markdown_report",
                side_effect=AssertionError("相同快照不得重跑整套日报"),
            ), patch.object(
                pipeline,
                "build_html_report",
                side_effect=AssertionError("相同快照不得重建HTML"),
            ):
                second = pipeline.run_forecast_report(
                    object(),
                    fetch_config,
                    filter_config,
                    brent,
                    tracker,
                    logger,
                    start_date="2026-08-20",
                    end_date="2026-08-21",
                    target_date="2026-08-24",
                )

            manifest_path = output_dir / f"{stem}_forecast_result.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["html_path"], str(html_path))
        self.assertEqual(len(manifest["snapshot_sha256"]), 64)
        self.assertEqual(manifest["report_logic_version"], 2)

    def test_old_or_missing_report_logic_version_forces_rerender(self) -> None:
        """A stale render manifest must not reuse files produced by older report logic."""
        from jin10_market_report import forecast_pipeline as pipeline

        for stale_version in (None, 1):
            with self.subTest(stale_version=stale_version), tempfile.TemporaryDirectory() as directory:
                output_dir = Path(directory)
                stem = "Jin10_Brent_Daily_Forecast_20260820_20260821_for_20260824"
                snapshot_path = output_dir / f"{stem}_friday_snapshot.json"
                snapshot_path.write_text(
                    json.dumps(self._snapshot_payload(), ensure_ascii=False),
                    encoding="utf-8",
                )
                markdown_path = output_dir / f"{stem}.md"
                html_path = output_dir / f"{stem}.html"
                manifest_path = output_dir / f"{stem}_forecast_result.json"
                markdown_path.write_text("# 旧渲染\n", encoding="utf-8")
                html_path.write_text("<h1>旧渲染</h1>", encoding="utf-8")
                manifest = {
                    "snapshot_sha256": pipeline._snapshot_sha256(snapshot_path),
                    "markdown_path": str(markdown_path),
                    "html_path": str(html_path),
                }
                if stale_version is not None:
                    manifest["report_logic_version"] = stale_version
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                def render_markdown(*_args, **kwargs):
                    markdown_path.write_text("# 新渲染\n", encoding="utf-8")
                    return SimpleNamespace(
                        markdown_path=markdown_path,
                        presentation=SimpleNamespace(forecast=dict(kwargs["forecast"])),
                    )

                def render_html(*_args, **_kwargs):
                    html_path.write_text("<h1>新渲染</h1>", encoding="utf-8")
                    return html_path

                fetch_config, filter_config, brent, tracker, logger = self._configs(directory)
                with patch.object(
                    pipeline,
                    "build_markdown_report",
                    side_effect=render_markdown,
                ), patch.object(pipeline, "build_html_report", side_effect=render_html):
                    result = pipeline.run_forecast_report(
                        object(),
                        fetch_config,
                        filter_config,
                        brent,
                        tracker,
                        logger,
                        start_date="2026-08-20",
                        end_date="2026-08-21",
                        target_date="2026-08-24",
                    )

                rewritten_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertFalse(result["reused"])
                self.assertEqual(markdown_path.read_text(encoding="utf-8"), "# 新渲染\n")
                self.assertEqual(rewritten_manifest["report_logic_version"], 2)

    def test_friday_snapshot_rejects_weekend_news(self) -> None:
        """A snapshot containing news published after Friday midnight must never be accepted."""
        from jin10_market_report.forecast_pipeline import _load_forecast_snapshot

        payload = self._snapshot_payload()
        payload["news"]["events"] = [
            {
                "published_at": "2026-08-22T00:21:00+08:00",
                "title": "周六新增新闻",
                "direction": "bullish",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "friday_snapshot.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "周五新闻截止时间"):
                _load_forecast_snapshot(
                    path,
                    start_day=date(2026, 8, 20),
                    end_day=date(2026, 8, 21),
                    target_day=date(2026, 8, 24),
                    timezone_name="Asia/Shanghai",
                )

    def test_prepare_friday_snapshot_filters_legacy_weekend_news(self) -> None:
        """The local migration must exclude weekend events before freezing calculations."""
        from jin10_market_report.forecast_pipeline import prepare_friday_snapshot

        payload = self._snapshot_payload()
        payload.pop("news_window_start_utc")
        payload.pop("news_window_end_utc")
        payload["analysis_window_end_utc"] = "2026-08-22T00:00:00+00:00"
        payload["news"]["events"] = [
            {
                "news_id": "friday",
                "published_at": "2026-08-21T23:29:00+08:00",
                "title": "周五供应风险",
                "direction": "bullish",
                "strength": "medium",
                "category": "Supply",
                "pricing_status": "PARTIAL",
                "transmission_chain": "供应风险上升 → Brent利多。",
            },
            {
                "news_id": "weekend",
                "published_at": "2026-08-22T00:21:00+08:00",
                "title": "周六新增新闻",
                "direction": "bearish",
                "strength": "strong",
                "category": "Supply",
                "pricing_status": "LOW",
                "transmission_chain": "供应恢复 → Brent利空。",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "legacy_audit.json"
            destination = Path(directory) / "friday_snapshot.json"
            source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = prepare_friday_snapshot(
                source,
                destination,
                start_date="2026-08-20",
                end_date="2026-08-21",
                target_date="2026-08-24",
                timezone_name="Asia/Shanghai",
            )
            frozen = json.loads(destination.read_text(encoding="utf-8"))

        self.assertEqual(result["excluded_after_friday_cutoff"], 1)
        self.assertEqual([event["news_id"] for event in frozen["news"]["events"]], ["friday"])
        self.assertEqual(frozen["news_window_end_utc"], "2026-08-21T16:00:00+00:00")
        self.assertEqual(frozen["analysis_window_end_utc"], "2026-08-21T21:00:00+00:00")
        self.assertEqual(frozen["final"]["direction"], "偏多")

    def test_missing_snapshot_fails_without_fallback(self) -> None:
        """A missing Friday snapshot must fail locally instead of triggering any recovery fetch."""
        from jin10_market_report import forecast_pipeline as pipeline

        with tempfile.TemporaryDirectory() as directory:
            fetch_config, filter_config, brent, tracker, logger = self._configs(directory)
            with self.assertRaisesRegex(FileNotFoundError, "缺少周五预测快照"):
                pipeline.run_forecast_report(
                    object(),
                    fetch_config,
                    filter_config,
                    brent,
                    tracker,
                    logger,
                    start_date="2026-08-20",
                    end_date="2026-08-21",
                    target_date="2026-08-24",
                )


class SameDayForecastPipelineTests(unittest.TestCase):
    def _base_snapshot(self) -> dict[str, object]:
        metrics = {
            "accuracy": 0.52,
            "balanced_accuracy": 0.51,
            "auc": 0.53,
            "brier_score": 0.25,
            "f1": 0.61,
            "predicted_up_rate": 0.70,
        }
        return {
            "target_date": "2026-08-24",
            "daily_price_last_date": "2026-08-21",
            "future_data_used": False,
            "quant": {
                "as_of_date": "2026-08-21",
                "model_name": "LightGBM",
                "up_probability": 0.54,
                "down_probability": 0.46,
                "reasons": ["周五动量为正"],
                "backtest": {"metrics": metrics, "test_samples": 504},
            },
        }

    def test_same_day_window_stops_at_target_0800_without_later_target_data(self) -> None:
        """Moving the cutoff past 08:00 would leak later August 24 facts into the forecast."""
        from jin10_market_report.forecast_pipeline import same_day_forecast_bounds

        start, end = same_day_forecast_bounds(
            "2026-08-21",
            "2026-08-24",
            "2026-08-24",
            "Asia/Shanghai",
        )

        self.assertEqual(start, datetime(2026, 8, 21, 0, 0, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(end, datetime(2026, 8, 24, 0, 0, tzinfo=ZoneInfo("UTC")))

    def test_closed_market_news_is_a_low_pricing_forward_signal(self) -> None:
        """Dropping weekend news as UNKNOWN would erase valid pre-open directional evidence."""
        from jin10_market_report.forecast_pipeline import _prepare_same_day_news

        news = {
            "direction": "neutral",
            "events": [
                {
                    "news_id": "weekend-risk",
                    "published_at": "2026-08-23T12:00:00+08:00",
                    "title": "周末原油运输中断",
                    "direction": "bullish",
                    "strength": "strong",
                    "category": "Shipping",
                    "pricing_status": "UNKNOWN",
                    "transmission_chain": "原油运输中断 → 供应收紧 → Brent利多。",
                }
            ],
            "model_a_overlap": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
            "model_b": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
            "model_c": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
            "history_sessions": 12,
            "backtest_note": "短历史",
        }

        prepared = _prepare_same_day_news(
            news,
            friday_close_utc=datetime(2026, 8, 21, 21, 0, tzinfo=ZoneInfo("UTC")),
            target_open_utc=datetime(2026, 8, 23, 22, 0, tzinfo=ZoneInfo("UTC")),
        )

        self.assertEqual(prepared["events"][0]["pricing_status"], "LOW")
        self.assertIn("周末休市", prepared["events"][0]["pricing_explanation"])
        self.assertEqual(prepared["direction"], "bullish")
        self.assertIn("周末原油运输中断", prepared["reasons"][0])

    def test_same_day_external_sources_must_be_complete(self) -> None:
        """Rendering a formal same-day report with a failed ISW or Trump source is incomplete."""
        from jin10_market_report.forecast_pipeline import _require_complete_external_updates

        complete_isw = {
            "status": "success",
            "source_key_takeaways": ["one"],
            "key_takeaways_zh": ["一"],
            "source_key_takeaways_count": 1,
            "translated_key_takeaways_count": 1,
        }
        complete_trump = {"status": "success", "statement_count": 0, "statements": []}

        _require_complete_external_updates(complete_isw, complete_trump)
        with self.assertRaisesRegex(RuntimeError, "ISW"):
            _require_complete_external_updates(
                {**complete_isw, "translated_key_takeaways_count": 0},
                complete_trump,
            )
        with self.assertRaisesRegex(RuntimeError, "特朗普"):
            _require_complete_external_updates(
                complete_isw,
                {"status": "failed", "failure_reason": "HTTP 500", "statements": []},
            )

    def test_same_day_pipeline_reuses_friday_model_and_renders_live_sources(self) -> None:
        """Refitting Model A or omitting either supplemental source breaks the approved data boundary."""
        from jin10_market_report import forecast_pipeline as pipeline

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            base_snapshot = output_dir / "friday_snapshot.json"
            base_snapshot.write_text(json.dumps(self._base_snapshot(), ensure_ascii=False), encoding="utf-8")
            captured: dict[str, object] = {}
            markdown_path = output_dir / "same-day.md"
            html_path = output_dir / "same-day.html"
            news = {
                "direction": "bearish",
                "reasons": ["周末供应恢复"],
                "priced_events": [],
                "weekend_risks": [],
                "events": [],
                "important_news": [],
                "structured_features": {"LOW_bearish": 1},
                "history_sessions": 12,
                "model_a_overlap": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
                "model_b": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
                "model_c": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
                "backtest_note": "短历史",
            }
            isw = {
                "status": "success",
                "source_key_takeaways": ["one"],
                "key_takeaways_zh": ["一"],
                "source_key_takeaways_count": 1,
                "translated_key_takeaways_count": 1,
            }
            trump = {"status": "success", "statement_count": 0, "statements": []}

            def render_markdown(*_args, **kwargs):
                captured.update(kwargs)
                markdown_path.write_text("# same-day\n", encoding="utf-8")
                return SimpleNamespace(
                    markdown_path=markdown_path,
                    presentation=SimpleNamespace(forecast=dict(kwargs["forecast"])),
                )

            def render_html(*_args, **_kwargs):
                html_path.write_text("<h1>same-day</h1>", encoding="utf-8")
                return html_path

            tracker = SimpleNamespace(
                timezone="Asia/Shanghai",
                output_dir=directory,
                database_path=str(output_dir / "tracker.sqlite"),
            )
            brent = SimpleNamespace(source="Yahoo Finance", symbol="BZ=F", interval="5m")
            logger = SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(pipeline, "quantitative_forecast", side_effect=AssertionError("不得重训"), create=True)
                )
                stack.enter_context(patch.object(pipeline, "build_news_forecast_from_store", return_value=news))
                stack.enter_context(patch.object(pipeline, "fetch_latest_isw_update", return_value=isw))
                stack.enter_context(patch.object(pipeline, "fetch_trump_statements", return_value=trump))
                stack.enter_context(patch.object(pipeline, "build_markdown_report", side_effect=render_markdown))
                stack.enter_context(patch.object(pipeline, "build_html_report", side_effect=render_html))
                result = pipeline.run_same_day_forecast_report(
                    object(),
                    brent,
                    tracker,
                    logger,
                    start_date="2026-08-21",
                    end_date="2026-08-24",
                    target_date="2026-08-24",
                    base_snapshot_path=base_snapshot,
                    retry_failed_isw=True,
                    retry_failed_trump=True,
                )

        self.assertEqual(result["quant"]["as_of_date"], "2026-08-21")
        self.assertEqual(result["forecast"]["target_date"], "2026年8月24日")
        self.assertEqual(captured["window_start_utc"], datetime(2026, 8, 21, 0, 0, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(captured["window_end_utc"], datetime(2026, 8, 24, 0, 0, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(captured["external_update"], isw)
        self.assertEqual(captured["trump_update"], trump)
        self.assertFalse(captured["frozen_snapshot"])
        self.assertIn("包含周末新闻", result["forecast"]["news_scope_note"])


class ForecastWindowAndLayoutTests(unittest.TestCase):
    def test_same_day_html_uses_target_day_labels_instead_of_two_day_weekend_copy(self) -> None:
        """Keeping frozen-Friday labels would misstate the approved 21-to-24 same-day report."""
        from jin10_market_report.tracking_report import _formal_dashboard_body

        dashboard = {
            "title": "Brent同日预测",
            "top_summary_items": [],
            "forecast": {
                "target_date": "2026年8月24日",
                "direction": "偏多",
                "confidence": "LOW",
                "risk_heading": "24日盘中风险",
                "forecast_kicker": "TARGET DAY",
                "important_news_heading": "21–24日重要原油新闻与价格状态",
                "reaction_end_label": "截至24日08:00",
                "weekend_risks": ["盘中供应风险"],
                "important_news": [
                    {
                        "published_at": "2026-08-23T12:00:00+08:00",
                        "title": "周末原油运输中断",
                        "direction": "bullish",
                        "strength": "strong",
                        "category": "Shipping",
                        "pricing_status": "LOW",
                        "transmission_chain": "运输中断 → Brent利多。",
                        "pricing_explanation": "周末休市。",
                        "reaction_returns": {},
                    }
                ],
            },
            "meta": [],
            "market_items": [],
            "metrics": [("21–24日区间涨跌幅", "+1.2%")],
            "chart_line": "",
            "trump_items": [],
            "iran_update_items": [],
            "tracking_items": [],
        }

        rendered = "".join(_formal_dashboard_body(Path("/tmp/same-day.md"), dashboard))

        self.assertIn("TARGET DAY", rendered)
        self.assertIn("24日盘中风险", rendered)
        self.assertIn("21–24日重要原油新闻与价格状态", rendered)
        self.assertIn("截至24日08:00 N/A", rendered)
        self.assertIn('metric-value metric-up', rendered)
        self.assertNotIn("周末风险", rendered)

    def test_weekend_quant_path_reuses_the_saved_friday_snapshot(self) -> None:
        """Weekend generation must not refetch or refit Model A when a matching Friday snapshot exists."""
        from jin10_market_report.forecast_pipeline import _load_quant_snapshot

        payload = {
            "target_date": "2026-08-24",
            "daily_price_last_date": "2026-08-21",
            "quant": {"as_of_date": "2026-08-21", "model_name": "LightGBM", "up_probability": 0.54},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved_forecast_audit.json"
            path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            quant = _load_quant_snapshot(
                path,
                end_day=date(2026, 8, 21),
                target_day=date(2026, 8, 24),
            )

        self.assertEqual(quant["model_name"], "LightGBM")
        self.assertEqual(quant["up_probability"], 0.54)

    def test_two_trade_date_window_preserves_the_existing_0800_boundary(self) -> None:
        """Using midnight or a one-day window would omit part of the requested two-date report."""
        from jin10_market_report.forecast import forecast_window_bounds

        start, end = forecast_window_bounds("2026-08-20", "2026-08-21", "Asia/Shanghai")

        self.assertEqual(start, datetime(2026, 8, 20, 0, 0, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(end, datetime(2026, 8, 22, 0, 0, tzinfo=ZoneInfo("UTC")))

    def test_news_cutoff_excludes_weekend_while_friday_price_window_reaches_close(self) -> None:
        """Friday news stops at local midnight, while prices may continue to the scheduled session close."""
        from jin10_market_report import forecast as forecast_module

        news_bounds = getattr(forecast_module, "forecast_news_bounds", None)
        price_bounds = getattr(forecast_module, "forecast_price_bounds", None)
        self.assertTrue(callable(news_bounds))
        self.assertTrue(callable(price_bounds))

        news_start, news_end = news_bounds("2026-08-20", "2026-08-21", "Asia/Shanghai")
        price_start, price_end = price_bounds("2026-08-20", "2026-08-21", "Asia/Shanghai")

        self.assertEqual(news_start, datetime(2026, 8, 20, 0, 0, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(news_end, datetime(2026, 8, 21, 16, 0, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(price_start, news_start)
        self.assertEqual(price_end, datetime(2026, 8, 21, 21, 0, tzinfo=ZoneInfo("UTC")))

    def test_prediction_card_is_immediately_below_today_summary(self) -> None:
        """Rendering the forecast after the market body would violate the report's decision-first layout."""
        from jin10_market_report.tracking_report import (
            FormalReportPresentation,
            build_html_report,
        )

        forecast = {
            "target_date": "2026年8月24日",
            "direction": "偏多",
            "confidence": "MEDIUM",
            "quant_up_probability": 0.56,
            "news_direction": "偏多",
            "final_up_probability": None,
            "probability_note": "历史新闻样本不足，不展示最终概率。",
            "quant_reasons": ["5日动量为正"],
            "news_reasons": ["一项供应事件仍在发展"],
            "priced_events": ["库存消息已充分定价"],
            "weekend_risks": ["周末地缘局势升级情景"],
        }
        presentation = FormalReportPresentation(
            panel_groups=(),
            unmatched_major_items=(),
            iran_update_items=(),
            top_summary_items=("Brent两日整体上涨。",),
            forecast=forecast,
        )
        markdown = """# 金十新闻事件 + Brent市场反应日报

## 今日总结

- Brent两日整体上涨。

统计窗口：2026-08-20 08:00 至 2026-08-22 08:00（Asia/Shanghai）
数据来源：金十数据快讯｜Yahoo Finance BZ=F

## 本窗口Brent表现

| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |
| ---: | ---: | ---: | ---: |
| 68.00 | +1.00% | 69.00 | 67.00 |

## 市场主要驱动与下一时段判断

- 供应主线。
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markdown_path = root / "Jin10_Brent_Forecast_20260820_20260821.md"
            markdown_path.write_text(markdown, encoding="utf-8")
            html_path = build_html_report(markdown_path, root, presentation=presentation)
            rendered = html_path.read_text(encoding="utf-8")

        summary_index = rendered.index('class="executive-summary"')
        forecast_index = rendered.index('class="forecast-card"')
        market_index = rendered.index('class="market-lede"')
        self.assertLess(summary_index, forecast_index)
        self.assertLess(forecast_index, market_index)
        self.assertIn(">今日总结</h2>", rendered)
        self.assertIn("2026年8月24日 Brent 预测", rendered)
        self.assertIn("历史新闻样本不足，不展示最终概率", rendered)

    def test_forecast_html_renders_every_directional_important_news_without_a_twelve_item_cap(self) -> None:
        """The visible important-news evidence must not stop at an arbitrary dashboard count."""
        from jin10_market_report.tracking_report import FormalReportPresentation, build_html_report

        important_news = [
            {
                "published_at": f"2026-08-21T{index:02d}:00:00+08:00",
                "title": f"重要原油事件{index}",
                "direction": "bullish" if index % 2 else "bearish",
                "strength": "medium",
                "category": "Supply",
                "pricing_status": "PARTIAL",
                "pricing_explanation": "价格出现方向一致反应，但持续性不足。",
                "transmission_chain": "供应变化 → 市场平衡变化 → Brent方向变化。",
                "reaction_returns": {"5m": 0.001, "30m": 0.002, "2h": 0.003, "end": 0.002},
                "url": f"https://flash.jin10.com/detail/{index}",
            }
            for index in range(15)
        ]
        presentation = FormalReportPresentation(
            panel_groups=(),
            unmatched_major_items=(),
            iran_update_items=(),
            top_summary_items=("Brent两日整体上涨。",),
            forecast={
                "target_date": "2026年8月24日",
                "direction": "偏多",
                "confidence": "LOW",
                "quant_up_probability": 0.54,
                "news_direction": "中性",
                "probability_note": "短历史回测仅供参考。",
                "important_news": important_news,
            },
        )
        markdown = """# 金十新闻事件 + Brent市场反应日报

## 今日总结

- Brent两日整体上涨。

统计窗口：2026-08-20 08:00 至 2026-08-22 05:00（Asia/Shanghai）

## 本窗口Brent表现

| 最新价 | 两日区间涨跌幅 | 区间最高价 | 区间最低价 |
| ---: | ---: | ---: | ---: |
| 94.00 | +2.00% | 95.00 | 91.00 |
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markdown_path = root / "forecast.md"
            markdown_path.write_text(markdown, encoding="utf-8")
            rendered = build_html_report(markdown_path, root, presentation=presentation).read_text(encoding="utf-8")

        self.assertEqual(rendered.count('data-important-news="true"'), 15)
        self.assertIn("两日重要原油新闻与价格状态", rendered)
        self.assertIn("重要原油事件14", rendered)

    def test_report_build_result_preserves_forecast_for_actual_html_generation(self) -> None:
        """The forecast payload must survive the Markdown builder and reach the HTML presentation."""
        from jin10_market_report.tracking_report import (
            FormalReportPresentation,
            build_markdown_report,
        )

        class Store:
            def refresh_multi_event_groups(self, *_args):
                return None

            def unpriced_watch_rows(self, *_args):
                return []

            def reaction_rows_between(self, *_args):
                return []

            def brent_day_klines(self, *_args):
                return []

        forecast = {
            "target_date": "2026年8月24日",
            "direction": "偏多",
            "confidence": "LOW",
            "quant_up_probability": 0.54,
        }
        empty_homepage = FormalReportPresentation((), (), ())
        audit = {"major_candidates": [], "news_freshness": "passed", "price_freshness": "passed"}
        movement = {"selected_raw_ids": set(), "groups": [], "unexplained": [], "windows": []}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch("jin10_market_report.tracking_report._build_window_audit", return_value=audit))
            stack.enter_context(patch("jin10_market_report.tracking_report._build_price_movement_review", return_value=movement))
            stack.enter_context(patch("jin10_market_report.tracking_report._build_event_grouped_homepage", return_value=empty_homepage))
            stack.enter_context(patch("jin10_market_report.tracking_report.aggregate_30m_klines", return_value=[]))
            stack.enter_context(patch("jin10_market_report.tracking_report.prepare_interactive_chart_markers", return_value=([], [])))
            stack.enter_context(patch("jin10_market_report.tracking_report.brent_day_summary", return_value={"latest": "94", "pct": "+2%", "high": "95", "low": "91"}))
            stack.enter_context(patch("jin10_market_report.tracking_report.classify_report_status", return_value=("FORMAL", {})))
            stack.enter_context(patch("jin10_market_report.tracking_report._build_audit_report_lines", return_value=["# audit"]))
            stack.enter_context(patch("jin10_market_report.tracking_report.build_html_report"))
            result = build_markdown_report(
                Store(),
                SimpleNamespace(source="Yahoo Finance", symbol="BZ=F", interval="5m"),
                Path(directory),
                external_update={"status": "failed", "failure_reason": "test"},
                trump_update={"status": "failed", "failure_reason": "test"},
                window_start_utc=datetime(2026, 8, 20, 0, 0, tzinfo=ZoneInfo("UTC")),
                window_end_utc=datetime(2026, 8, 21, 21, 0, tzinfo=ZoneInfo("UTC")),
                forecast=forecast,
            )

        self.assertEqual(result.presentation.forecast, forecast)


class IswRetryTests(unittest.TestCase):
    def test_translation_retries_http_429_before_failing_the_isw_update(self) -> None:
        """One transient rate limit must not erase an otherwise valid official ISW update."""
        from jin10_market_report.external_update import _translate_to_chinese

        error = HTTPError("https://translate.googleapis.com", 429, "Too Many Requests", {}, None)
        translated = None
        with patch(
            "jin10_market_report.external_update._request_text",
            side_effect=[error, '[[["中文翻译","English",null,null]]]'],
        ), patch("time.sleep"):
            try:
                translated = _translate_to_chinese("English")
            except HTTPError:
                pass

        self.assertEqual(translated, "中文翻译")

    def test_audit_renderer_accepts_forecast_context_from_report_builder(self) -> None:
        """The forecast report path must not fail when it forwards its audit context."""
        from jin10_market_report.tracking_report import _build_audit_report_lines

        self.assertIn("forecast", inspect.signature(_build_audit_report_lines).parameters)

    def test_cli_exposes_one_explicit_forecast_report_command(self) -> None:
        """Without a dedicated command the requested dates and target can silently drift to today's window."""
        from unittest.mock import patch

        from run_jin10_tracker import parse_args

        with patch(
            "sys.argv",
            [
                "run_jin10_tracker.py",
                "forecast-report",
                "--start-date",
                "2026-08-20",
                "--end-date",
                "2026-08-21",
                "--target-date",
                "2026-08-24",
            ],
        ):
            args = parse_args()

        self.assertEqual(args.command, "forecast-report")
        self.assertEqual(args.start_date, "2026-08-20")
        self.assertEqual(args.end_date, "2026-08-21")
        self.assertEqual(args.target_date, "2026-08-24")
        self.assertFalse(hasattr(args, "skip_backfill"))
        self.assertFalse(hasattr(args, "model_kind"))

    def test_cli_exposes_local_friday_snapshot_preparation(self) -> None:
        """Friday snapshot preparation must be separate from the weekend read-only command."""
        from unittest.mock import patch

        from run_jin10_tracker import parse_args

        with patch(
            "sys.argv",
            [
                "run_jin10_tracker.py",
                "prepare-forecast-snapshot",
                "--source-path",
                "saved_audit.json",
                "--start-date",
                "2026-08-20",
                "--end-date",
                "2026-08-21",
                "--target-date",
                "2026-08-24",
            ],
        ):
            args = parse_args()

        self.assertEqual(args.command, "prepare-forecast-snapshot")
        self.assertEqual(args.source_path, "saved_audit.json")
        self.assertEqual(args.output_dir, "outputs/tracker")

    def test_cli_exposes_same_day_forecast_command_with_explicit_base_snapshot(self) -> None:
        """Without an explicit base snapshot, the same-day command could silently refit Model A."""
        from unittest.mock import patch

        from run_jin10_tracker import parse_args

        with patch(
            "sys.argv",
            [
                "run_jin10_tracker.py",
                "same-day-forecast-report",
                "--start-date",
                "2026-08-21",
                "--end-date",
                "2026-08-24",
                "--target-date",
                "2026-08-24",
                "--base-snapshot-path",
                "friday_snapshot.json",
                "--retry-failed-isw",
                "--retry-failed-trump",
            ],
        ):
            args = parse_args()

        self.assertEqual(args.command, "same-day-forecast-report")
        self.assertEqual(args.base_snapshot_path, "friday_snapshot.json")
        self.assertTrue(args.retry_failed_isw)
        self.assertTrue(args.retry_failed_trump)

    def test_forecast_cli_prints_lowercase_reused_status(self) -> None:
        """Operators must be able to distinguish a fresh render from an idempotent reuse."""
        from jin10_market_report import forecast_pipeline as pipeline
        import run_jin10_tracker as cli

        args = SimpleNamespace(
            command="forecast-report",
            log_dir="logs",
            start_date="2026-08-20",
            end_date="2026-08-21",
            target_date="2026-08-24",
        )
        logger = SimpleNamespace(info=lambda *_args: None)
        tracker = SimpleNamespace(database_path=":memory:")
        store = SimpleNamespace(close=lambda: None)

        for reused in (False, True):
            with self.subTest(reused=reused):
                result = {
                    "html_path": "report.html",
                    "forecast_snapshot_path": "snapshot.json",
                    "reused": reused,
                }
                stdout = io.StringIO()
                with patch.object(cli, "parse_args", return_value=args), patch.object(
                    cli,
                    "setup_logger",
                    return_value=(logger, "tracker.log"),
                ), patch.object(
                    cli,
                    "build_configs",
                    return_value=(object(), object(), object(), tracker),
                ), patch.object(cli, "TrackerStore", return_value=store), patch.object(
                    pipeline,
                    "run_forecast_report",
                    return_value=result,
                ), redirect_stdout(stdout):
                    exit_code = cli.main()

                self.assertEqual(exit_code, 0)
                self.assertIn(f"reused={'true' if reused else 'false'}", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
