from __future__ import annotations

import math
import json
import re
import statistics
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


FEATURE_NAMES = (
    "return_1d",
    "return_3d",
    "return_5d",
    "return_10d",
    "return_20d",
    "sma_3_gap",
    "sma_5_gap",
    "sma_10_gap",
    "sma_20_gap",
    "volatility_5d",
    "volatility_10d",
    "volatility_20d",
    "range_position_20d",
    "intraday_return",
    "volume_change_5d",
)

NEWS_FEATURE_NAMES = (
    "unpriced_strong_bullish",
    "unpriced_strong_bearish",
    "partial_bullish",
    "partial_bearish",
    "supply_net",
    "demand_net",
    "geopolitical_shipping_net",
    "opec_sanctions_net",
    "eligible_event_count",
)


@dataclass(frozen=True)
class DailyBar:
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class PriceSample:
    feature_date: date
    features: tuple[float, ...]
    label: int | None
    label_date: date | None


@dataclass(frozen=True)
class WalkForwardPrediction:
    feature_date: date
    label_date: date
    actual: int
    up_probability: float
    train_feature_end: date
    train_label_end: date


@dataclass(frozen=True)
class BacktestResult:
    model_name: str
    test_samples: int
    metrics: Mapping[str, float | None]
    predictions: tuple[WalkForwardPrediction, ...]


@dataclass(frozen=True)
class NewsAssessment:
    news_id: str
    event_id: str
    published_at: str
    title: str
    is_oil_relevant: bool
    direction: str
    strength: str
    category: str
    transmission_chain: str
    source_news_ids: tuple[str, ...]


@dataclass(frozen=True)
class PricingAssessment:
    status: str
    volatility_multiple: float
    explanation: str


@dataclass(frozen=True)
class CombinedForecast:
    direction: str
    confidence: str
    final_up_probability: float | None
    probability_note: str


def _pct_return(current: float, previous: float) -> float:
    return (current / previous - 1.0) if previous else 0.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def _feature_vector(bars: Sequence[DailyBar], index: int) -> tuple[float, ...]:
    close = bars[index].close
    closes = [bar.close for bar in bars]
    daily_returns = [_pct_return(closes[position], closes[position - 1]) for position in range(1, index + 1)]

    def trailing_return(days: int) -> float:
        return _pct_return(close, closes[index - days])

    def sma_gap(days: int) -> float:
        average = _mean(closes[index - days + 1 : index + 1])
        return _pct_return(close, average)

    def volatility(days: int) -> float:
        return _stdev(daily_returns[-days:]) * math.sqrt(252.0)

    recent = bars[index - 19 : index + 1]
    recent_high = max(bar.high for bar in recent)
    recent_low = min(bar.low for bar in recent)
    price_range = recent_high - recent_low
    position = (close - recent_low) / price_range if price_range else 0.5
    intraday = _pct_return(close, bars[index].open)
    current_volume = bars[index].volume
    volume_history = [bar.volume for bar in bars[index - 4 : index + 1] if bar.volume is not None]
    if current_volume is None or len(volume_history) < 3:
        volume_change = 0.0
    else:
        previous_average = _mean([float(value) for value in volume_history[:-1]])
        volume_change = _pct_return(float(current_volume), previous_average)
    return (
        trailing_return(1),
        trailing_return(3),
        trailing_return(5),
        trailing_return(10),
        trailing_return(20),
        sma_gap(3),
        sma_gap(5),
        sma_gap(10),
        sma_gap(20),
        volatility(5),
        volatility(10),
        volatility(20),
        position,
        intraday,
        volume_change,
    )


def build_price_dataset(
    bars: Sequence[DailyBar],
    as_of_date: date,
) -> tuple[list[PriceSample], PriceSample]:
    available = sorted((bar for bar in bars if bar.trading_date <= as_of_date), key=lambda bar: bar.trading_date)
    if len(available) < 22:
        raise ValueError("至少需要22个完整交易日才能构造价格特征。")
    samples: list[PriceSample] = []
    for index in range(20, len(available) - 1):
        next_bar = available[index + 1]
        samples.append(
            PriceSample(
                feature_date=available[index].trading_date,
                features=_feature_vector(available, index),
                label=1 if next_bar.close > available[index].close else 0,
                label_date=next_bar.trading_date,
            )
        )
    last_index = len(available) - 1
    current = PriceSample(
        feature_date=available[last_index].trading_date,
        features=_feature_vector(available, last_index),
        label=None,
        label_date=None,
    )
    return samples, current


def build_short_price_dataset(
    bars: Sequence[DailyBar],
    as_of_date: date,
) -> tuple[list[PriceSample], PriceSample]:
    """Build a compact price dataset only for like-for-like B/C overlap tests.

    This is deliberately separate from the production LightGBM feature set.
    It uses five transparent three-session features so a short news history can
    be reviewed chronologically without pretending it has Model A's depth.
    """
    available = sorted((bar for bar in bars if bar.trading_date <= as_of_date), key=lambda bar: bar.trading_date)
    if len(available) < 5:
        raise ValueError("至少需要5个交易日才能构造短窗口价格特征。")

    def features(index: int) -> tuple[float, ...]:
        closes = [bar.close for bar in available]
        returns = [_pct_return(closes[position], closes[position - 1]) for position in range(index - 2, index + 1)]
        recent = available[index - 2 : index + 1]
        high = max(bar.high for bar in recent)
        low = min(bar.low for bar in recent)
        position = (available[index].close - low) / (high - low) if high > low else 0.5
        return (
            _pct_return(closes[index], closes[index - 1]),
            _pct_return(closes[index], closes[index - 3]),
            _stdev(returns) * math.sqrt(252.0),
            position,
            _pct_return(available[index].close, available[index].open),
        )

    samples: list[PriceSample] = []
    for index in range(3, len(available) - 1):
        next_bar = available[index + 1]
        samples.append(
            PriceSample(
                feature_date=available[index].trading_date,
                features=features(index),
                label=1 if next_bar.close > available[index].close else 0,
                label_date=next_bar.trading_date,
            )
        )
    last_index = len(available) - 1
    current = PriceSample(available[last_index].trading_date, features(last_index), None, None)
    return samples, current


def parse_yahoo_daily_payload(payload: Mapping[str, object], as_of_date: date) -> list[DailyBar]:
    chart = payload.get("chart")
    if not isinstance(chart, Mapping) or chart.get("error"):
        raise ValueError(f"Yahoo日线响应异常: {chart.get('error') if isinstance(chart, Mapping) else 'missing chart'}")
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], Mapping):
        raise ValueError("Yahoo日线响应缺少result。")
    result = results[0]
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, Mapping):
        raise ValueError("Yahoo日线响应缺少时间戳或指标。")
    quotes = indicators.get("quote")
    if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], Mapping):
        raise ValueError("Yahoo日线响应缺少OHLC。")
    quote = quotes[0]
    columns = {name: quote.get(name) for name in ("open", "high", "low", "close", "volume")}
    if not all(isinstance(columns[name], list) for name in ("open", "high", "low", "close")):
        raise ValueError("Yahoo日线OHLC字段不完整。")
    bars: list[DailyBar] = []
    utc = ZoneInfo("UTC")
    for index, timestamp in enumerate(timestamps):
        try:
            trading_date = datetime.fromtimestamp(int(timestamp), tz=utc).date()
            values = {name: columns[name][index] if isinstance(columns[name], list) and index < len(columns[name]) else None for name in columns}
            if trading_date > as_of_date or any(values[name] is None for name in ("open", "high", "low", "close")):
                continue
            bars.append(
                DailyBar(
                    trading_date=trading_date,
                    open=float(values["open"]),
                    high=float(values["high"]),
                    low=float(values["low"]),
                    close=float(values["close"]),
                    volume=float(values["volume"]) if values["volume"] is not None else None,
                )
            )
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
    if not bars:
        raise ValueError("Yahoo日线响应在截止日之前没有可用OHLC。")
    return sorted(bars, key=lambda bar: bar.trading_date)


def build_local_session_daily_bars(
    rows: Sequence[Mapping[str, object]],
    *,
    as_of_date: date,
    timezone_name: str = "Asia/Shanghai",
) -> list[DailyBar]:
    """Aggregate cached 5-minute bars into Beijing-labelled trading sessions.

    A session starts at 08:00 Beijing time and its scheduled Friday close is
    05:00 the following calendar day.  Bars in the 05:00-08:00 maintenance
    gap are ignored.  This lets weekend report generation consume the saved
    Friday close without asking a data source for Saturday/Sunday bars.
    """
    from .time_utils import parse_iso_utc

    tz = ZoneInfo(timezone_name)
    grouped: dict[date, list[tuple[datetime, Mapping[str, object]]]] = {}
    for row in rows:
        try:
            timestamp = parse_iso_utc(str(row["ts_utc"]))
        except (KeyError, TypeError):
            timestamp = None
        if timestamp is None:
            continue
        local = timestamp.astimezone(tz)
        if local.time() >= time(8, 0):
            session_date = local.date()
        elif local.time() < time(5, 0):
            session_date = local.date() - timedelta(days=1)
        else:
            continue
        if session_date > as_of_date or session_date.weekday() >= 5:
            continue
        grouped.setdefault(session_date, []).append((timestamp, row))

    bars: list[DailyBar] = []
    for session_date, values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda item: item[0])
        first = ordered[0][1]
        last = ordered[-1][1]
        volumes = [float(_row_value(row, "volume")) for _, row in ordered if _row_value(row, "volume") is not None]
        bars.append(
            DailyBar(
                trading_date=session_date,
                open=float(first["open"]),
                high=max(float(row["high"]) for _, row in ordered),
                low=min(float(row["low"]) for _, row in ordered),
                close=float(last["close"]),
                volume=sum(volumes) if volumes else None,
            )
        )
    return bars


def fetch_yahoo_daily_bars(
    symbol: str,
    *,
    start_date: date,
    as_of_date: date,
    timeout_seconds: int = 25,
) -> list[DailyBar]:
    utc = ZoneInfo("UTC")
    period1 = int(datetime.combine(start_date, time(0, 0), tzinfo=utc).timestamp())
    period2 = int(datetime.combine(as_of_date + timedelta(days=1), time(0, 0), tzinfo=utc).timestamp())
    encoded_symbol = urllib.parse.quote(symbol, safe="")
    query = urllib.parse.urlencode(
        {
            "period1": period1,
            "period2": period2,
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
    )
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("Yahoo日线响应不是JSON对象。")
            return parse_yahoo_daily_payload(payload, as_of_date)
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt < 2:
                time_module.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Yahoo Brent日线抓取失败: {last_error}")


class _LogisticModel:
    name = "Logistic Regression (deterministic fallback)"

    def __init__(self, iterations: int = 500, learning_rate: float = 0.08, l2: float = 0.02) -> None:
        self.iterations = iterations
        self.learning_rate = learning_rate
        self.l2 = l2
        self.means: list[float] = []
        self.scales: list[float] = []
        self.weights: list[float] = []
        self.bias = 0.0

    def fit(self, x_rows: Sequence[Sequence[float]], labels: Sequence[int]) -> "_LogisticModel":
        if not x_rows:
            raise ValueError("训练样本不能为空。")
        width = len(x_rows[0])
        self.means = [_mean([float(row[column]) for row in x_rows]) for column in range(width)]
        self.scales = []
        for column in range(width):
            scale = _stdev([float(row[column]) for row in x_rows])
            self.scales.append(scale if scale > 1e-12 else 1.0)
        normalized = [self._normalize(row) for row in x_rows]
        self.weights = [0.0] * width
        up_rate = min(max(_mean([float(value) for value in labels]), 1e-4), 1.0 - 1e-4)
        self.bias = math.log(up_rate / (1.0 - up_rate))
        count = float(len(labels))
        for _ in range(self.iterations):
            gradient = [0.0] * width
            bias_gradient = 0.0
            for row, label in zip(normalized, labels):
                probability = self._sigmoid(self.bias + sum(weight * value for weight, value in zip(self.weights, row)))
                error = probability - float(label)
                bias_gradient += error
                for column, value in enumerate(row):
                    gradient[column] += error * value
            self.bias -= self.learning_rate * bias_gradient / count
            for column in range(width):
                regularized = gradient[column] / count + self.l2 * self.weights[column]
                self.weights[column] -= self.learning_rate * regularized
        return self

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            term = math.exp(-min(value, 700.0))
            return 1.0 / (1.0 + term)
        term = math.exp(max(value, -700.0))
        return term / (1.0 + term)

    def _normalize(self, row: Sequence[float]) -> list[float]:
        return [(float(value) - mean) / scale for value, mean, scale in zip(row, self.means, self.scales)]

    def predict_probability(self, row: Sequence[float]) -> float:
        normalized = self._normalize(row)
        return self._sigmoid(self.bias + sum(weight * value for weight, value in zip(self.weights, normalized)))


class _LightGBMModel:
    name = "LightGBM"

    def __init__(self) -> None:
        import lightgbm as lgb

        self.lgb = lgb
        self.model = None
        self.params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "learning_rate": 0.03,
            "num_leaves": 7,
            "max_depth": 3,
            "min_data_in_leaf": 40,
            "bagging_fraction": 1.0,
            "feature_fraction": 0.9,
            "lambda_l2": 1.0,
            "seed": 20260821,
            "feature_fraction_seed": 20260821,
            "bagging_seed": 20260821,
            "deterministic": True,
            "force_col_wise": True,
            "verbosity": -1,
        }

    def fit(self, x_rows: Sequence[Sequence[float]], labels: Sequence[int]) -> "_LightGBMModel":
        import numpy as np

        dataset = self.lgb.Dataset(
            np.asarray([list(row) for row in x_rows], dtype=float),
            label=list(labels),
            free_raw_data=False,
        )
        self.model = self.lgb.train(self.params, dataset, num_boost_round=120)
        return self

    def predict_probability(self, row: Sequence[float]) -> float:
        if self.model is None:
            raise RuntimeError("LightGBM 模型尚未训练。")
        import numpy as np

        return float(self.model.predict(np.asarray([list(row)], dtype=float))[0])


def _new_model(model_kind: str):
    if model_kind not in {"auto", "lightgbm", "logistic"}:
        raise ValueError(f"不支持的模型类型: {model_kind}")
    if model_kind in {"auto", "lightgbm"}:
        try:
            return _LightGBMModel()
        except (ImportError, OSError):
            if model_kind == "lightgbm":
                raise
    return _LogisticModel()


def walk_forward_backtest(
    samples: Sequence[PriceSample],
    min_train_size: int = 504,
    test_window: int = 504,
    model_kind: str = "auto",
) -> BacktestResult:
    if len(samples) <= min_train_size:
        raise ValueError("可用价格样本不足以执行扩窗回测。")
    start = max(min_train_size, len(samples) - test_window)
    predictions: list[WalkForwardPrediction] = []
    model_name = ""
    for index in range(start, len(samples)):
        train = list(samples[:index])
        current = samples[index]
        if current.label is None or current.label_date is None:
            continue
        model = _new_model(model_kind)
        model.fit([sample.features for sample in train], [int(sample.label) for sample in train])
        model_name = model.name
        probability = model.predict_probability(current.features)
        last_train = train[-1]
        predictions.append(
            WalkForwardPrediction(
                feature_date=current.feature_date,
                label_date=current.label_date,
                actual=int(current.label),
                up_probability=probability,
                train_feature_end=last_train.feature_date,
                train_label_end=last_train.label_date or last_train.feature_date,
            )
        )
    metrics = classification_metrics(
        [prediction.actual for prediction in predictions],
        [prediction.up_probability for prediction in predictions],
    )
    return BacktestResult(model_name, len(predictions), metrics, tuple(predictions))


def classification_metrics(labels: Sequence[int], probabilities: Sequence[float]) -> dict[str, float | None]:
    if len(labels) != len(probabilities) or not labels:
        raise ValueError("标签与概率必须非空且长度相同。")
    predicted = [1 if probability >= 0.5 else 0 for probability in probabilities]
    count = len(labels)
    tp = sum(1 for actual, guess in zip(labels, predicted) if actual == 1 and guess == 1)
    tn = sum(1 for actual, guess in zip(labels, predicted) if actual == 0 and guess == 0)
    fp = sum(1 for actual, guess in zip(labels, predicted) if actual == 0 and guess == 1)
    fn = sum(1 for actual, guess in zip(labels, predicted) if actual == 1 and guess == 0)
    positives = tp + fn
    negatives = tn + fp
    accuracy = (tp + tn) / count
    recall_up = tp / positives if positives else 0.0
    recall_down = tn / negatives if negatives else 0.0
    balanced = (recall_up + recall_down) / 2.0 if positives and negatives else None
    f1 = 2.0 * tp / (2.0 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0
    brier = _mean([(float(probability) - int(label)) ** 2 for label, probability in zip(labels, probabilities)])
    positive_scores = [float(probability) for label, probability in zip(labels, probabilities) if label == 1]
    negative_scores = [float(probability) for label, probability in zip(labels, probabilities) if label == 0]
    auc: float | None = None
    if positive_scores and negative_scores:
        wins = 0.0
        for positive in positive_scores:
            for negative in negative_scores:
                wins += 1.0 if positive > negative else 0.5 if positive == negative else 0.0
        auc = wins / (len(positive_scores) * len(negative_scores))
    return {
        "accuracy": accuracy,
        "balanced_accuracy": balanced,
        "auc": auc,
        "brier_score": brier,
        "f1": f1,
        "predicted_up_rate": _mean([float(value) for value in predicted]),
    }


def structured_news_feature_vector(events: Sequence[Mapping[str, object]]) -> tuple[float, ...]:
    eligible = [
        event
        for event in events
        if str(event.get("pricing_status") or "UNKNOWN") in {"LOW", "PARTIAL"}
        and str(event.get("direction") or "neutral") in {"bullish", "bearish"}
    ]

    def count(*, direction: str, pricing: str | None = None, strength: str | None = None) -> float:
        return float(
            sum(
                1
                for event in eligible
                if str(event.get("direction")) == direction
                and (pricing is None or str(event.get("pricing_status")) == pricing)
                and (strength is None or str(event.get("strength")) == strength)
            )
        )

    def category_net(categories: set[str]) -> float:
        bullish = sum(
            1
            for event in eligible
            if str(event.get("category")) in categories and str(event.get("direction")) == "bullish"
        )
        bearish = sum(
            1
            for event in eligible
            if str(event.get("category")) in categories and str(event.get("direction")) == "bearish"
        )
        return float(bullish - bearish)

    return (
        count(direction="bullish", pricing="LOW", strength="strong"),
        count(direction="bearish", pricing="LOW", strength="strong"),
        count(direction="bullish", pricing="PARTIAL"),
        count(direction="bearish", pricing="PARTIAL"),
        category_net({"Supply"}),
        category_net({"Demand"}),
        category_net({"Geopolitical", "Shipping"}),
        category_net({"OPEC+", "Sanctions"}),
        float(len(eligible)),
    )


def build_news_feature_samples(
    events: Sequence[Mapping[str, object]],
    price_samples: Sequence[PriceSample],
    *,
    covered_dates: set[date],
) -> list[PriceSample]:
    by_date: dict[date, list[Mapping[str, object]]] = {}
    for event in events:
        published = str(event.get("published_at") or "")
        try:
            published_date = datetime.fromisoformat(published.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        by_date.setdefault(published_date, []).append(event)

    samples: list[PriceSample] = []
    for sample in sorted(price_samples, key=lambda item: item.feature_date):
        if sample.label is None or sample.label_date is None:
            continue
        required_dates = (sample.feature_date - timedelta(days=1), sample.feature_date)
        if not all(day in covered_dates for day in required_dates):
            continue
        window_events = [event for day in required_dates for event in by_date.get(day, [])]
        samples.append(
            PriceSample(
                feature_date=sample.feature_date,
                features=structured_news_feature_vector(window_events),
                label=int(sample.label),
                label_date=sample.label_date,
            )
        )
    return samples


def _serialized_backtest(
    result: BacktestResult,
    *,
    up_probability: float | None = None,
    probability_eligible: bool = False,
    improves_price_model: bool | None = None,
) -> dict[str, object]:
    reliability = "LIMITED" if result.test_samples >= 30 else "EXPLORATORY"
    return {
        "status": "validated" if reliability == "LIMITED" else "short_history_backtest",
        "model_name": result.model_name,
        "test_samples": result.test_samples,
        "metrics": dict(result.metrics),
        "first_prediction_date": result.predictions[0].feature_date.isoformat() if result.predictions else None,
        "last_prediction_date": result.predictions[-1].feature_date.isoformat() if result.predictions else None,
        "up_probability": up_probability,
        "probability_eligible": probability_eligible,
        "reliability": reliability,
        "improves_price_model": improves_price_model,
        "folds": [
            {
                "feature_date": prediction.feature_date.isoformat(),
                "label_date": prediction.label_date.isoformat(),
                "train_feature_end": prediction.train_feature_end.isoformat(),
                "train_label_end": prediction.train_label_end.isoformat(),
            }
            for prediction in result.predictions
        ],
    }


def secondary_model_backtests(
    news_samples: Sequence[PriceSample],
    *,
    current_news_features: Sequence[float],
    price_probabilities: Mapping[date, float],
    current_price_probability: float,
    price_model_name: str = "Price probability (same short overlap)",
    min_train_size: int = 8,
    test_window: int = 12,
) -> dict[str, object]:
    eligible = [sample for sample in news_samples if sample.feature_date in price_probabilities]
    if len(eligible) <= min_train_size:
        return {
            "model_a_overlap": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
            "model_b": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
            "model_c": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
        }

    model_b_result = walk_forward_backtest(
        eligible,
        min_train_size=min_train_size,
        test_window=test_window,
        model_kind="logistic",
    )
    combined_samples = [
        replace(
            sample,
            features=(float(price_probabilities[sample.feature_date]), *sample.features),
        )
        for sample in eligible
    ]
    model_c_result = walk_forward_backtest(
        combined_samples,
        min_train_size=min_train_size,
        test_window=test_window,
        model_kind="logistic",
    )
    evaluation_dates = [prediction.feature_date for prediction in model_c_result.predictions]
    labels_by_date = {sample.feature_date: int(sample.label) for sample in eligible if sample.label is not None}
    price_labels = [labels_by_date[day] for day in evaluation_dates]
    price_probabilities_oos = [float(price_probabilities[day]) for day in evaluation_dates]
    price_metrics = classification_metrics(price_labels, price_probabilities_oos)
    price_predictions = tuple(
        WalkForwardPrediction(
            feature_date=day,
            label_date=next(sample.label_date for sample in eligible if sample.feature_date == day) or day,
            actual=labels_by_date[day],
            up_probability=float(price_probabilities[day]),
            train_feature_end=day - timedelta(days=1),
            train_label_end=day,
        )
        for day in evaluation_dates
    )
    price_result = BacktestResult(price_model_name, len(price_predictions), price_metrics, price_predictions)

    news_model = _LogisticModel().fit(
        [sample.features for sample in eligible],
        [int(sample.label) for sample in eligible],
    )
    model_b_probability = news_model.predict_probability(current_news_features)
    combined_model = _LogisticModel().fit(
        [sample.features for sample in combined_samples],
        [int(sample.label) for sample in combined_samples],
    )
    model_c_probability = combined_model.predict_probability(
        (float(current_price_probability), *tuple(float(value) for value in current_news_features))
    )

    c_metrics = model_c_result.metrics
    price_balanced = price_metrics.get("balanced_accuracy")
    combined_balanced = c_metrics.get("balanced_accuracy")
    price_brier = price_metrics.get("brier_score")
    combined_brier = c_metrics.get("brier_score")
    improves_price = bool(
        price_balanced is not None
        and combined_balanced is not None
        and price_brier is not None
        and combined_brier is not None
        and float(combined_balanced) >= float(price_balanced) + 0.01
        and float(combined_brier) < float(price_brier)
    )
    probability_eligible = model_c_result.test_samples >= 30 and improves_price
    return {
        "model_a_overlap": _serialized_backtest(price_result),
        "model_b": _serialized_backtest(model_b_result, up_probability=model_b_probability),
        "model_c": _serialized_backtest(
            model_c_result,
            up_probability=model_c_probability,
            probability_eligible=probability_eligible,
            improves_price_model=improves_price,
        ),
    }


def quantitative_forecast(
    bars: Sequence[DailyBar],
    *,
    as_of_date: date,
    model_kind: str = "auto",
    min_train_size: int = 504,
    test_window: int = 504,
) -> dict[str, object]:
    samples, current = build_price_dataset(bars, as_of_date)
    backtest = walk_forward_backtest(
        samples,
        min_train_size=min_train_size,
        test_window=test_window,
        model_kind=model_kind,
    )
    model = _new_model(model_kind)
    model.fit([sample.features for sample in samples], [int(sample.label) for sample in samples])
    probability = model.predict_probability(current.features)
    features = dict(zip(FEATURE_NAMES, current.features))
    position = float(features["range_position_20d"])
    reasons = [
        f"1日收益率 {float(features['return_1d']):+.2%}，5日动量 {float(features['return_5d']):+.2%}。",
        f"20日收益率 {float(features['return_20d']):+.2%}，价格位于20日区间约 {position:.0%} 分位。",
        f"20日年化历史波动率 {float(features['volatility_20d']):.1%}。",
    ]
    return {
        "as_of_date": current.feature_date.isoformat(),
        "model_name": model.name,
        "up_probability": probability,
        "down_probability": 1.0 - probability,
        "feature_names": list(FEATURE_NAMES),
        "features": features,
        "reasons": reasons,
        "backtest": {
            "model_name": backtest.model_name,
            "test_samples": backtest.test_samples,
            "metrics": dict(backtest.metrics),
            "first_prediction_date": backtest.predictions[0].feature_date.isoformat() if backtest.predictions else None,
            "last_prediction_date": backtest.predictions[-1].feature_date.isoformat() if backtest.predictions else None,
        },
    }


def _has_any(text: str, terms: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def classify_oil_news(
    title: str,
    content: str,
    *,
    news_id: str = "",
    event_id: str = "",
    published_at: str = "",
) -> NewsAssessment:
    headline = " ".join(str(title).split())
    body = " ".join(str(content).split())
    text = f"{headline} {body}".strip()
    source_ids = (news_id,) if news_id else ()

    # 混合事件摘要和仓位评论不是一个可归因的新闻事件，不能作为结构化计数单位。
    aggregate_or_positioning = bool(
        re.search(r"^金十数据整理[:：].*(?:速递|汇总|跟踪|都忙了什么)", headline)
        or re.search(r"(?:\||｜)\s*Plus日报$", headline, flags=re.IGNORECASE)
        or _has_any(headline, ("期货热点追踪", "每日热门ETF要闻汇总", "对冲基金转为看涨", "对冲基金转为看空"))
    )
    if aggregate_or_positioning:
        return NewsAssessment(
            news_id,
            event_id,
            published_at,
            title,
            False,
            "neutral",
            "weak",
            "Other",
            "多事件摘要或市场仓位评论无法形成单一、可检验的原油传导链。",
            source_ids,
        )

    oil_terms = (
        "原油",
        "石油",
        "油价",
        "汽油",
        "柴油",
        "燃油",
        "brent",
        "wti",
        "opec",
        "产油",
        "炼油",
        "油轮",
        "成品油",
        "石油设施",
    )
    shipping_terms = ("霍尔木兹", "油轮", "原油运输", "石油运输", "海峡通行", "石油港口")
    geo_entities = ("伊朗", "伊阿", "以色列", "中东", "海湾", "胡塞", "俄罗斯", "乌克兰")
    headline_oil = _has_any(headline, oil_terms)
    headline_shipping = _has_any(headline, shipping_terms)
    headline_geo = _has_any(headline, geo_entities)
    full_oil = _has_any(text, oil_terms)
    full_shipping = _has_any(text, shipping_terms)

    # A：正文中的背景提及不能把一篇无原油事件锚点的普通宏观报道变成油价新闻。
    headline_event_anchor = headline_oil or headline_shipping or headline_geo or _has_any(headline, ("欧佩克", "opec"))
    inventory = _has_any(text, ("原油库存", "商业原油库存", "api库存", "eia库存")) and headline_event_anchor
    opec = _has_any(text, ("opec", "欧佩克")) and headline_event_anchor
    title_sanctions = _has_any(headline, ("制裁", "豁免", "二级关税", "禁运"))
    sanctions = _has_any(text, ("制裁", "豁免", "二级关税", "禁运")) and headline_event_anchor and full_oil
    shipping = full_shipping and headline_event_anchor
    geopolitical = headline_geo and (full_oil or full_shipping or sanctions)
    demand = _has_any(text, ("原油需求", "石油需求", "燃料需求")) and headline_event_anchor
    supply = full_oil and headline_event_anchor and _has_any(
        text,
        ("产量", "产能", "出口", "供应", "减产", "增产", "停产", "中断", "恢复生产", "装船", "炼油厂"),
    )
    relevant = bool(inventory or opec or sanctions or shipping or geopolitical or demand or supply)
    if not relevant:
        return NewsAssessment(
            news_id,
            event_id,
            published_at,
            title,
            False,
            "neutral",
            "weak",
            "Other",
            "无法建立明确原油传导链。",
            source_ids,
        )

    category = (
        "Inventory"
        if inventory
        else "OPEC+"
        if opec
        else "Shipping"
        if headline_shipping
        else "Sanctions"
        if title_sanctions
        else "Geopolitical"
        if geopolitical
        else "Demand"
        if demand
        else "Supply"
    )
    direction = "neutral"
    strength = "medium"
    chain = "事件信息不完整 → 供需或风险溢价影响不清楚 → Brent中性。"
    strong_words = ("大幅", "显著", "全面", "紧急", "创纪录", "万桶/日", "万桶", "封锁", "中断")
    strong = _has_any(text, strong_words)

    requested_outcome = _has_any(headline, ("呼吁", "敦促", "寻求", "希望", "讨论恢复", "应结束"))
    conditional_scenario = bool(
        _has_any(headline, ("若", "可能会", "考虑对其采取", "或将"))
        and not _has_any(headline, ("可能性降低", "概率骤降"))
    )
    deferred_long_term = _has_any(headline, ("六年内", "2-3年", "2—3年", "2050年", "长期计划"))
    observed_deescalation = _has_any(
        headline,
        (
            "达成停火",
            "停火协议",
            "谈判取得进展",
            "谈判接近尾声",
            "会谈接近尾声",
            "恢复正常通行",
            "重新开放",
            "军事对抗可能性降低",
            "军事行动概率骤降",
            "冲突风险下降",
            "已突破封锁",
            "供应限制解除",
        ),
    ) or (_has_any(headline, ("护航",)) and _has_any(headline, ("原油", "油轮", "通行")))
    observed_escalation = _has_any(
        headline,
        (
            "会谈几周前便已破裂",
            "会谈破裂",
            "谈判破裂",
            "陷入僵局",
            "袭击",
            "宣布关闭",
            "通行中断",
            "航运中断",
            "扣押油轮",
            "流量已降",
            "流量降至",
            "计划袭击",
        ),
    )

    supply_increase = bool(
        re.search(r"(?:原油|石油|汽油|柴油|燃油).{0,14}(?:产量|产能|出口|供应).{0,10}(?:增加|上升|提高|恢复|扩大|重启)", headline)
        or re.search(r"(?:产量|产能|出口|供应).{0,10}(?:增加|上升|提高|恢复|扩大|重启)", headline)
        or _has_any(headline, ("恢复出口", "恢复生产", "提前销售", "释放储备"))
    )
    supply_decrease = bool(
        re.search(r"(?:原油|石油|汽油|柴油|燃油).{0,14}(?:产量|产能|出口|供应).{0,10}(?:下降|减少|削减|暂停|中断)", headline)
        or re.search(r"(?:产量|产能|出口|供应).{0,10}(?:下降|减少|削减|暂停|中断)", headline)
        or _has_any(headline, ("停产", "暂停出口"))
    )

    if inventory and _has_any(text, ("增加", "上升", "累库", "增幅")):
        direction, strength = "bearish", "strong" if strong else "medium"
        chain = "原油库存增加 → 可用供应增加 → 市场偏宽松 → Brent利空。"
    elif inventory and _has_any(text, ("下降", "减少", "去库", "降幅")):
        direction, strength = "bullish", "strong" if strong else "medium"
        chain = "原油库存下降 → 可用供应减少 → 市场趋紧 → Brent利多。"
    elif opec and _has_any(text, ("减产", "削减", "下调产量", "延长减产")):
        direction, strength = "bullish", "strong" if strong or _has_any(text, ("宣布", "决定", "协议")) else "medium"
        chain = "OPEC+减产 → 全球供应减少 → 市场收紧 → Brent利多。"
    elif opec and _has_any(text, ("增产", "提高产量", "恢复产量", "上调产量")):
        direction, strength = "bearish", "strong" if strong else "medium"
        chain = "OPEC+增产 → 全球供应增加 → 市场趋宽松 → Brent利空。"
    elif supply_increase and not deferred_long_term:
        direction, strength = "bearish", "strong" if strong else "weak"
        chain = "原油或成品油产量/出口增加 → 可用供应增加 → 市场趋宽松 → Brent利空。"
    elif supply_decrease and not deferred_long_term:
        direction, strength = "bullish", "strong" if strong else "medium"
        chain = "原油或成品油产量/出口下降 → 可用供应减少 → 市场收紧 → Brent利多。"
    elif (
        (shipping or geopolitical)
        and observed_deescalation
        and not requested_outcome
        and not conditional_scenario
    ):
        direction = "bearish"
        chain = "地缘或航运风险缓和 → 供应中断概率下降 → 风险溢价回落 → Brent利空。"
    elif (shipping or geopolitical) and observed_escalation:
        direction, strength = "bullish", "strong" if strong else "medium"
        chain = "地缘或航运风险升级 → 潜在运输与供应中断风险上升 → 风险溢价提高 → Brent利多。"
    elif sanctions and _has_any(text, ("解除", "放宽", "豁免", "暂停", "取消")):
        direction = "bearish"
        chain = "石油制裁放宽或豁免 → 出口供应约束下降 → 可用供应增加 → Brent利空。"
    elif sanctions and title_sanctions and _has_any(headline, ("加强", "扩大", "实施", "收紧", "新增", "制裁")):
        direction, strength = "bullish", "strong" if strong else "medium"
        chain = "石油制裁收紧 → 出口供应受限 → 市场收紧 → Brent利多。"
    elif demand and _has_any(text, ("下降", "疲弱", "下调", "萎缩", "放缓")):
        direction, strength = "bearish", "strong" if strong else "medium"
        chain = "全球原油需求走弱 → 供需平衡趋宽松 → Brent利空。"
    elif demand and _has_any(text, ("增长", "强劲", "上调", "改善", "回升")):
        direction, strength = "bullish", "strong" if strong else "medium"
        chain = "全球原油需求改善 → 供需平衡趋紧 → Brent利多。"
    return NewsAssessment(news_id, event_id, published_at, title, True, direction, strength, category, chain, source_ids)


def _normalized_event_text(value: str) -> str:
    value = re.sub(r"\d+(?:\.\d+)?", "#", value.lower())
    value = re.sub(r"(?:更新|最新|续报|快讯|消息)", "", value)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value)[:80]


def deduplicate_news_events(items: Sequence[NewsAssessment]) -> list[NewsAssessment]:
    deduplicated: list[NewsAssessment] = []

    def published(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    for item in items:
        if not item.is_oil_relevant:
            continue
        duplicate_index: int | None = None
        for index, current in enumerate(deduplicated):
            same_event = bool(item.event_id and current.event_id and item.event_id == current.event_id)
            same_text = _normalized_event_text(item.title) == _normalized_event_text(current.title)
            item_time = published(item.published_at)
            current_time = published(current.published_at)
            close_in_time = False
            if item_time is not None and current_time is not None:
                try:
                    close_in_time = abs((item_time - current_time).total_seconds()) <= 600
                except TypeError:
                    close_in_time = False
            if same_text or (same_event and close_in_time):
                duplicate_index = index
                break
        if duplicate_index is None:
            deduplicated.append(item)
            continue
        current = deduplicated[duplicate_index]
        rank = {"weak": 1, "medium": 2, "strong": 3}
        representative = item if rank.get(item.strength, 0) > rank.get(current.strength, 0) else current
        ids = tuple(dict.fromkeys((*current.source_news_ids, *item.source_news_ids)))
        deduplicated[duplicate_index] = replace(representative, source_news_ids=ids)
    return deduplicated


def aggregate_news_forecast(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    strength_weight = {"strong": 3.0, "medium": 2.0, "weak": 1.0}
    pricing_weight = {"LOW": 1.0, "PARTIAL": 0.5}
    persistent_categories = {"OPEC+", "Supply", "Demand", "Geopolitical", "Shipping", "Sanctions"}
    score = 0.0
    reasons: list[str] = []
    priced_events: list[str] = []
    structured: dict[str, int] = {}
    risk_categories: list[str] = []
    for event in events:
        title = str(event.get("title") or "未命名事件")
        direction = str(event.get("direction") or "neutral")
        strength = str(event.get("strength") or "weak")
        category = str(event.get("category") or "Other")
        pricing = str(event.get("pricing_status") or "UNKNOWN")
        structured[f"{pricing}_{direction}"] = structured.get(f"{pricing}_{direction}", 0) + 1
        structured[f"category_{category}"] = structured.get(f"category_{category}", 0) + 1
        if category not in risk_categories:
            risk_categories.append(category)
        if pricing == "FULL":
            priced_events.append(title)
            continue
        if pricing not in pricing_weight or category not in persistent_categories or direction not in {"bullish", "bearish"}:
            continue
        signed = 1.0 if direction == "bullish" else -1.0
        score += signed * strength_weight.get(strength, 1.0) * pricing_weight[pricing]
        direction_label = "利多" if direction == "bullish" else "利空"
        reasons.append(
            f"{title}（{direction_label}/{strength}，{pricing}）：{str(event.get('transmission_chain') or '')}"
        )
    direction = "bullish" if score >= 1.0 else "bearish" if score <= -1.0 else "neutral"
    weekend_risks: list[str] = []
    risk_copy = {
        "Geopolitical": "Future Risk Event：周末地缘冲突若升级或缓和，可能快速改变Brent风险溢价。",
        "Shipping": "Future Risk Event：周末关键海峡、港口或油轮通行若出现新变化，可能造成周一跳空。",
        "Sanctions": "Future Risk Event：周末若出现新的制裁、豁免或执行口径，可能改变可出口供应预期。",
        "OPEC+": "Future Risk Event：OPEC+若在周末发布新的配额或供应政策信息，可能改变周一方向。",
        "Supply": "Future Risk Event：已识别的供应中断若在周末扩大或恢复，可能改变周一供需预期。",
    }
    for category in risk_categories:
        if category in risk_copy and risk_copy[category] not in weekend_risks:
            weekend_risks.append(risk_copy[category])
    return {
        "direction": direction,
        "score": score,
        "reasons": reasons[:3],
        "priced_events": priced_events[:3],
        "weekend_risks": weekend_risks[:3],
        "structured_features": structured,
    }


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _return_from_price(base: float | None, current: float | None) -> float | None:
    if base is None or current is None or base == 0:
        return None
    return (current - base) / base


def _nearest_price_return(
    store,
    symbol: str,
    interval: str,
    target_utc: datetime,
    base: float,
    max_lag_minutes: int = 15,
) -> float | None:
    from .time_utils import parse_iso_utc

    row = store.nearest_kline(symbol, interval, target_utc)
    if row is None:
        return None
    row_time = parse_iso_utc(str(row["ts_utc"]))
    if row_time is None or abs((row_time - target_utc).total_seconds()) > max_lag_minutes * 60:
        return None
    return _return_from_price(base, float(row["close"]))


def _historical_abs_30m_returns(store, symbol: str, interval: str, end_utc: datetime) -> list[float]:
    from .time_utils import parse_iso_utc

    rows = store.window_klines(symbol, interval, end_utc - timedelta(days=30), end_utc)
    values: list[float] = []
    for index in range(6, len(rows)):
        start_time = parse_iso_utc(str(rows[index - 6]["ts_utc"]))
        finish_time = parse_iso_utc(str(rows[index]["ts_utc"]))
        if start_time is None or finish_time is None:
            continue
        elapsed = (finish_time - start_time).total_seconds() / 60.0
        if not 25 <= elapsed <= 35:
            continue
        move = _return_from_price(float(rows[index - 6]["close"]), float(rows[index]["close"]))
        if move is not None:
            values.append(abs(move))
    return values


def build_news_forecast_from_store(
    store,
    *,
    symbol: str,
    interval: str,
    start_utc: datetime,
    end_utc: datetime,
    price_end_utc: datetime | None = None,
) -> dict[str, object]:
    from .time_utils import ensure_utc, parse_iso_utc

    price_end = ensure_utc(price_end_utc or end_utc)

    def build_events(source_rows: Sequence[Mapping[str, object]], *, historical: bool) -> list[dict[str, object]]:
        by_id = {str(_row_value(row, "news_id", "")): row for row in source_rows}
        assessments = [
            classify_oil_news(
                str(_row_value(row, "title", "")),
                str(_row_value(row, "content", "")),
                news_id=str(_row_value(row, "news_id", "")),
                event_id=str(_row_value(row, "event_id", "")),
                published_at=str(_row_value(row, "published_at_bj", "")),
            )
            for row in source_rows
        ]
        built: list[dict[str, object]] = []
        for assessment in deduplicate_news_events(assessments):
            row = by_id.get(assessment.news_id)
            pricing = PricingAssessment("UNKNOWN", 0.0, "缺少事件价格记录，无法判断是否已定价。")
            reaction_returns: dict[str, float | None] = {"5m": None, "30m": None, "2h": None, "end": None}
            if row is not None:
                base_raw = _row_value(row, "event_base_close")
                published = parse_iso_utc(str(_row_value(row, "published_at_utc", "")))
                base_time = parse_iso_utc(str(_row_value(row, "event_base_time_utc", ""))) or published
                base = float(base_raw) if base_raw is not None else None
                if historical:
                    # Historical features stop at the stored 60-minute reaction.
                    # A later report close would leak information from the label day.
                    reaction_returns["5m"] = (
                        float(_row_value(row, "pct_5m")) if _row_value(row, "pct_5m") is not None else None
                    )
                    reaction_returns["30m"] = (
                        float(_row_value(row, "pct_30m")) if _row_value(row, "pct_30m") is not None else None
                    )
                    pct_60m = _row_value(row, "pct_60m")
                    reaction_returns["2h"] = float(pct_60m) if pct_60m is not None else None
                    reaction_returns["end"] = reaction_returns["2h"]
                elif base is not None and base_time is not None:
                    base_time = ensure_utc(base_time)
                    if base_time + timedelta(minutes=5) <= price_end:
                        reaction_returns["5m"] = (
                            float(_row_value(row, "pct_5m"))
                            if _row_value(row, "pct_5m") is not None
                            else None
                        )
                    if base_time + timedelta(minutes=30) <= price_end:
                        reaction_returns["30m"] = (
                            float(_row_value(row, "pct_30m"))
                            if _row_value(row, "pct_30m") is not None
                            else None
                        )
                    if base_time + timedelta(hours=2) <= price_end:
                        reaction_returns["2h"] = _nearest_price_return(
                            store, symbol, interval, base_time + timedelta(hours=2), base
                        )
                    end_row = store.kline_at_or_before(symbol, interval, price_end)
                    reaction_returns["end"] = _return_from_price(
                        base, float(end_row["close"]) if end_row is not None else None
                    )
                history = _historical_abs_30m_returns(store, symbol, interval, published or start_utc)
                pricing = assess_market_pricing(assessment.direction, reaction_returns, history)
            built.append(
                {
                    "news_id": assessment.news_id,
                    "source_news_ids": list(assessment.source_news_ids),
                    "published_at": assessment.published_at,
                    "title": assessment.title,
                    "direction": assessment.direction,
                    "strength": assessment.strength,
                    "category": assessment.category,
                    "transmission_chain": assessment.transmission_chain,
                    "pricing_status": pricing.status,
                    "pricing_explanation": pricing.explanation,
                    "volatility_multiple": pricing.volatility_multiple,
                    "reaction_returns": reaction_returns,
                    "url": str(_row_value(row, "url", "")) if row is not None else "",
                }
            )
        return built

    rows = list(store.reaction_rows_between(start_utc, end_utc))
    events = build_events(rows, historical=False)
    events.sort(key=lambda item: str(item.get("published_at") or ""))
    summary = aggregate_news_forecast(events)
    historical_rows = list(
        store.reaction_rows_between(
            datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC")),
            end_utc,
        )
    )
    historical_by_date: dict[str, list[Mapping[str, object]]] = {}
    for row in historical_rows:
        date_label = str(_row_value(row, "published_at_bj", ""))[:10]
        if date_label:
            historical_by_date.setdefault(date_label, []).append(row)
    historical_events: list[dict[str, object]] = []
    for date_label in sorted(historical_by_date):
        historical_events.extend(build_events(historical_by_date[date_label], historical=True))
    covered_dates = {datetime.strptime(value, "%Y-%m-%d").date() for value in historical_by_date}

    secondary: dict[str, object] = {
        "model_a_overlap": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
        "model_b": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
        "model_c": {"status": "insufficient_history", "test_samples": 0, "metrics": None},
    }
    price_rows = list(
        store.window_klines(
            symbol,
            interval,
            datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC")),
            price_end,
        )
    )
    local_bars = build_local_session_daily_bars(
        price_rows,
        as_of_date=price_end.astimezone(ZoneInfo("Asia/Shanghai")).date(),
    )
    if len(local_bars) >= 5:
        price_samples, current_price_sample = build_short_price_dataset(local_bars, local_bars[-1].trading_date)
        news_samples = build_news_feature_samples(historical_events, price_samples, covered_dates=covered_dates)
        target_dates = {sample.feature_date for sample in news_samples}
        price_probabilities: dict[date, float] = {}
        for index, sample in enumerate(price_samples):
            if sample.feature_date not in target_dates or index < 4:
                continue
            train = price_samples[:index]
            model = _LogisticModel().fit(
                [item.features for item in train],
                [int(item.label) for item in train],
            )
            price_probabilities[sample.feature_date] = model.predict_probability(sample.features)
        if price_samples:
            current_price_model = _LogisticModel().fit(
                [sample.features for sample in price_samples],
                [int(sample.label) for sample in price_samples],
            )
            current_short_price_probability = current_price_model.predict_probability(current_price_sample.features)
            usable_count = sum(1 for sample in news_samples if sample.feature_date in price_probabilities)
            if usable_count >= 3:
                test_window = min(4, usable_count - 2)
                secondary = secondary_model_backtests(
                    news_samples,
                    current_news_features=structured_news_feature_vector(events),
                    price_probabilities=price_probabilities,
                    current_price_probability=current_short_price_probability,
                    price_model_name="Logistic price probability (B/C同短窗口)",
                    min_train_size=usable_count - test_window,
                    test_window=test_window,
                )
    summary.update(
        {
            "events": events,
            "important_news": [event for event in events if event.get("direction") in {"bullish", "bearish"}],
            "history_sessions": len(covered_dates),
            "model_a_overlap": secondary["model_a_overlap"],
            "model_b": secondary["model_b"],
            "model_c": secondary["model_c"],
            "backtest_note": "Model B/C仅使用本地连续新闻覆盖日，扩窗回测最多保留最近4折；样本少于30折均标记为EXPLORATORY。",
        }
    )
    return summary


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def assess_market_pricing(
    direction: str,
    reaction_returns: Mapping[str, float | None],
    historical_abs_30m_returns: Sequence[float],
) -> PricingAssessment:
    if direction not in {"bullish", "bearish"} or not historical_abs_30m_returns:
        return PricingAssessment("UNKNOWN", 0.0, "方向或历史波动率数据不足，无法判断是否已定价。")
    sign = 1.0 if direction == "bullish" else -1.0
    aligned = {key: sign * float(value) for key, value in reaction_returns.items() if value is not None}
    if not aligned:
        return PricingAssessment("UNKNOWN", 0.0, "事件后价格窗口缺失，无法判断是否已定价。")
    absolute_history = [abs(float(value)) for value in historical_abs_30m_returns]
    median = max(_quantile(absolute_history, 0.5), 1e-9)
    upper = max(_quantile(absolute_history, 0.75), median)
    core_peak = max(aligned.get("30m", float("-inf")), aligned.get("2h", float("-inf")))
    end_move = aligned.get("end")
    multiple = max(0.0, max(aligned.values())) / upper
    if core_peak >= upper and end_move is not None and end_move >= median:
        return PricingAssessment("FULL", multiple, "价格按事件方向出现高于同期75分位的反应，且截至窗口末仍保持，基本充分定价。")
    if max(aligned.values()) < median or (end_move is not None and end_move <= 0 and core_peak < upper):
        return PricingAssessment("LOW", multiple, "事件方向反应低于同期波动中位数或已反向，市场反应较少。")
    return PricingAssessment("PARTIAL", multiple, "价格出现方向一致反应，但强度或持续性未同时达到充分定价条件。")


def combine_forecasts(
    *,
    quant_up_probability: float,
    quant_metrics: Mapping[str, float | None],
    news_direction: str,
    news_model_metrics: Mapping[str, float | None] | None,
    combined_model_metrics: Mapping[str, float | None] | None,
    combined_up_probability: float | None = None,
    combined_probability_eligible: bool = False,
    secondary_test_samples: int = 0,
) -> CombinedForecast:
    quant_direction = "bullish" if quant_up_probability >= 0.55 else "bearish" if quant_up_probability <= 0.45 else "neutral"
    if quant_direction == "neutral":
        final_direction = news_direction
    elif news_direction == "neutral" or news_direction == quant_direction:
        final_direction = quant_direction
    else:
        final_direction = "neutral"
    direction_label = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}.get(final_direction, "中性")

    final_probability: float | None = None
    if combined_probability_eligible and isinstance(combined_up_probability, (int, float)):
        final_probability = min(max(float(combined_up_probability), 0.0), 1.0)
        probability_note = "Model C在同窗口历史回测中稳定优于价格基线，采用其模型概率。"
    elif secondary_test_samples:
        probability_note = (
            f"Model B/C已完成{secondary_test_samples}折扩窗回测，但样本仍属EXPLORATORY，"
            "不用于修正LightGBM概率；新闻仅作定性方向判断。"
        )
    else:
        probability_note = "历史新闻样本不足，新闻仅作定性方向判断，不展示最终概率。"
    balanced = quant_metrics.get("balanced_accuracy")
    brier = quant_metrics.get("brier_score")
    aligned = news_direction in {"neutral", quant_direction}
    if balanced is not None and brier is not None and float(balanced) >= 0.57 and float(brier) <= 0.235 and aligned:
        confidence = "HIGH"
    elif balanced is not None and brier is not None and float(balanced) >= 0.52 and float(brier) <= 0.26 and final_direction != "neutral":
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    return CombinedForecast(direction_label, confidence, final_probability, probability_note)


def forecast_window_bounds(
    start_date: str,
    end_date: str,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[datetime, datetime]:
    start_day = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_day = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end_day < start_day:
        raise ValueError("结束日期不能早于开始日期。")
    tz = ZoneInfo(timezone_name)
    local_start = datetime.combine(start_day, time(8, 0), tzinfo=tz)
    local_end = datetime.combine(end_day + timedelta(days=1), time(8, 0), tzinfo=tz)
    return local_start.astimezone(ZoneInfo("UTC")), local_end.astimezone(ZoneInfo("UTC"))


def forecast_news_bounds(
    start_date: str,
    end_date: str,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[datetime, datetime]:
    """Return the prior-news window, excluding local dates after ``end_date``."""
    start_day = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_day = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end_day < start_day:
        raise ValueError("结束日期不能早于开始日期。")
    tz = ZoneInfo(timezone_name)
    local_start = datetime.combine(start_day, time(8, 0), tzinfo=tz)
    local_end = datetime.combine(end_day + timedelta(days=1), time(0, 0), tzinfo=tz)
    return local_start.astimezone(ZoneInfo("UTC")), local_end.astimezone(ZoneInfo("UTC"))


def forecast_price_bounds(
    start_date: str,
    end_date: str,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[datetime, datetime]:
    """Return the price window through the scheduled close of the last session."""
    start_day = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_day = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end_day < start_day:
        raise ValueError("结束日期不能早于开始日期。")
    tz = ZoneInfo(timezone_name)
    local_start = datetime.combine(start_day, time(8, 0), tzinfo=tz)
    end_clock = time(5, 0) if end_day.weekday() == 4 else time(8, 0)
    local_end = datetime.combine(end_day + timedelta(days=1), end_clock, tzinfo=tz)
    return local_start.astimezone(ZoneInfo("UTC")), local_end.astimezone(ZoneInfo("UTC"))
