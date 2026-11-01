from __future__ import annotations

import csv
import json
import math
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from .forecast import build_local_session_daily_bars


EXPECTED_MODELS = (
    "ARIMA_1_1_1",
    "Linear_Regression_MA3_MA9",
    "LSTM",
    "XGBoost",
    "LightGBM",
    "Ensemble_Average",
)

MODEL_DISPLAY_NAMES = {
    "ARIMA_1_1_1": "ARIMA",
    "Linear_Regression_MA3_MA9": "Linear Regression",
    "LSTM": "LSTM",
    "XGBoost": "XGBoost",
    "LightGBM": "LightGBM",
    "Ensemble_Average": "Ensemble",
}


@dataclass(frozen=True)
class PriceLayerPaths:
    suite_root: Path
    project_root: Path
    model_data_path: Path
    results_dir: Path


def price_layer_paths(suite_root: Path | None = None) -> PriceLayerPaths:
    configured = os.environ.get("BRENT_PRICE_LAYER_ROOT")
    root = Path(configured) if configured else suite_root
    if root is None:
        root = Path.home() / "Documents" / "ChatGPT" / "Brent数据量化回测"
    root = Path(root).expanduser().resolve()
    data_root = root / "data" / "raw" / "brent_model_backtest"
    return PriceLayerPaths(
        suite_root=root,
        project_root=root / "brent_backtest",
        model_data_path=data_root / "brent_model_data.csv",
        results_dir=data_root / "results",
    )


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _average_tie_ranks(values: Mapping[str, float], *, descending: bool) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=descending)
    ranks: dict[str, float] = {}
    position = 0
    while position < len(ordered):
        end = position + 1
        while end < len(ordered) and math.isclose(
            ordered[end][1], ordered[position][1], rel_tol=1e-12, abs_tol=1e-15
        ):
            end += 1
        average_rank = ((position + 1) + end) / 2.0
        for index in range(position, end):
            ranks[ordered[index][0]] = average_rank
        position = end
    return ranks


def _model_metrics(rows: Sequence[Mapping[str, str]]) -> dict[str, float | int | str]:
    actual = [float(row["Actual_Return"]) for row in rows]
    predicted = [float(row["Predicted_Return"]) for row in rows]
    if not all(math.isfinite(value) for value in (*actual, *predicted)):
        raise ValueError("六模型OOS记录包含非有限数值。")
    actual_direction = [
        int(float(row.get("Actual_Direction") or _sign(value)))
        for row, value in zip(rows, actual)
    ]
    predicted_direction = [
        int(float(row.get("Predicted_Direction") or _sign(value)))
        for row, value in zip(rows, predicted)
    ]
    errors = [guess - observed for observed, guess in zip(actual, predicted)]
    direction_accuracy = sum(
        observed == guess for observed, guess in zip(actual_direction, predicted_direction)
    ) / len(rows)
    recalls = []
    for direction in sorted(set(actual_direction)):
        indices = [index for index, value in enumerate(actual_direction) if value == direction]
        recalls.append(
            sum(predicted_direction[index] == direction for index in indices) / len(indices)
        )
    return {
        "mae": sum(abs(error) for error in errors) / len(errors),
        "rmse": math.sqrt(sum(error * error for error in errors) / len(errors)),
        "direction_accuracy": direction_accuracy,
        "balanced_accuracy": sum(recalls) / len(recalls),
        "oos_samples": len(rows),
        "first_oos_date": min(str(row["Prediction_Date"]) for row in rows),
        "last_oos_date": max(str(row["Prediction_Date"]) for row in rows),
    }


def select_daily_price_model(results_dir: Path, *, as_of_date: date) -> dict[str, object]:
    """Select from saved OOS rows known by the report cutoff.

    The existing backtest's composite rule remains authoritative: average the
    MAE, RMSE, and Direction Accuracy ranks. Balanced Accuracy is calculated
    for every model and used only as a deterministic tie-breaker so embedding
    the backtest does not silently redesign its model-selection policy.
    """
    prediction_path = Path(results_dir) / "all_models_walk_forward_predictions.csv"
    if not prediction_path.exists():
        raise FileNotFoundError(f"缺少六模型OOS预测记录: {prediction_path}")
    with prediction_path.open(encoding="utf-8", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    grouped: dict[str, list[dict[str, str]]] = {name: [] for name in EXPECTED_MODELS}
    for row in source_rows:
        model_name = str(row.get("Model") or "")
        if model_name not in grouped:
            continue
        try:
            prediction_date = datetime.strptime(str(row["Prediction_Date"]), "%Y-%m-%d").date()
            train_end = datetime.strptime(str(row["Train_End"]), "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("六模型OOS记录包含无效日期。") from exc
        if prediction_date <= as_of_date:
            if train_end >= prediction_date:
                raise ValueError("六模型OOS记录违反PIT：训练截止日未早于预测日。")
            grouped[model_name].append(row)
    missing = [name for name, rows in grouped.items() if not rows]
    if missing:
        raise ValueError(f"截至{as_of_date.isoformat()}缺少模型OOS记录: {', '.join(missing)}")
    date_sets: dict[str, set[str]] = {}
    for model_name, rows in grouped.items():
        dates = [str(row["Prediction_Date"]) for row in rows]
        if len(dates) != len(set(dates)):
            raise ValueError(f"六模型OOS记录包含重复的模型/日期行: {model_name}")
        date_sets[model_name] = set(dates)
    reference_dates = date_sets[EXPECTED_MODELS[0]]
    if any(dates != reference_dates for dates in date_sets.values()):
        raise ValueError("六模型OOS记录的预测日期集合不一致。")

    metrics = {name: _model_metrics(rows) for name, rows in grouped.items()}
    mae_ranks = _average_tie_ranks(
        {name: float(values["mae"]) for name, values in metrics.items()}, descending=False
    )
    rmse_ranks = _average_tie_ranks(
        {name: float(values["rmse"]) for name, values in metrics.items()}, descending=False
    )
    direction_ranks = _average_tie_ranks(
        {name: float(values["direction_accuracy"]) for name, values in metrics.items()},
        descending=True,
    )
    comparison: list[dict[str, object]] = []
    for model_name in EXPECTED_MODELS:
        values = metrics[model_name]
        average_rank = (
            mae_ranks[model_name] + rmse_ranks[model_name] + direction_ranks[model_name]
        ) / 3.0
        comparison.append(
            {
                "model_name": model_name,
                "display_name": MODEL_DISPLAY_NAMES[model_name],
                **values,
                "average_rank": average_rank,
            }
        )
    comparison.sort(
        key=lambda item: (
            float(item["average_rank"]),
            -float(item["balanced_accuracy"]),
            float(item["mae"]),
            float(item["rmse"]),
            str(item["model_name"]),
        )
    )
    selected = dict(comparison[0])
    selected["model_comparison"] = comparison
    selected["selection_method"] = (
        "沿用六模型Walk-Forward的MAE、RMSE、Direction Accuracy平均排名；"
        "Balanced Accuracy仅用于同分排序。"
    )
    return selected


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    length = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * length) // 451
    month = (h + length - 7 * m + 114) // 31
    day = (h + length - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed_uk_winter_holidays(year: int) -> set[date]:
    fixed = {date(year, 12, 25), date(year, 12, 26)}
    observed: set[date] = set()
    for holiday in sorted(fixed):
        substitute = holiday
        while substitute.weekday() >= 5 or substitute in fixed.difference({holiday}) or substitute in observed:
            substitute += timedelta(days=1)
        observed.add(substitute)
    return fixed | observed


def ice_full_closure_dates(year: int) -> set[date]:
    """Return maintained full-day ICE Europe closures used by Brent.

    Early-close sessions remain valid trading days. Operators can add an
    exceptional full closure through BRENT_PRICE_LAYER_HOLIDAYS as a
    comma-separated ISO date list without changing model code.
    """
    new_year = date(year, 1, 1)
    observed_new_year = new_year
    while observed_new_year.weekday() >= 5:
        observed_new_year += timedelta(days=1)
    easter = _easter_sunday(year)
    closures = {
        new_year,
        observed_new_year,
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        *_observed_uk_winter_holidays(year),
    }
    configured = os.environ.get("BRENT_PRICE_LAYER_HOLIDAYS", "")
    for token in configured.split(","):
        token = token.strip()
        if token:
            closures.add(datetime.strptime(token, "%Y-%m-%d").date())
    return closures


def is_ice_trading_day(value: date) -> bool:
    return value.weekday() < 5 and value not in ice_full_closure_dates(value.year)


def next_ice_trading_day(value: date) -> date:
    candidate = value + timedelta(days=1)
    while not is_ice_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def previous_ice_trading_day(value: date) -> date:
    candidate = value - timedelta(days=1)
    while not is_ice_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def validate_complete_price_session(
    rows: Sequence[Mapping[str, object]],
    *,
    expected_session: date,
    timezone_name: str,
) -> float:
    tz = ZoneInfo(timezone_name)
    candidates: list[tuple[datetime, float]] = []
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(str(row["ts_utc"]).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError
            local = timestamp.astimezone(tz)
            close = float(row["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Brent价格缓存包含无效时间或收盘价。") from exc
        if local.time() >= time(8, 0):
            session_date = local.date()
        elif local.time() < time(5, 0):
            session_date = local.date() - timedelta(days=1)
        else:
            continue
        if session_date == expected_session:
            candidates.append((local, close))
    if not candidates:
        raise ValueError(f"Brent价格缓存未覆盖预期交易日 {expected_session.isoformat()}。")
    last_local, last_close = max(candidates, key=lambda item: item[0])
    expected_close_date = expected_session + timedelta(days=1)
    if last_local.date() != expected_close_date or last_local.time() < time(4, 45):
        raise ValueError(
            f"Brent价格缓存未覆盖{expected_session.isoformat()}交易日的收盘边界。"
        )
    return last_close


def build_price_forecast_payload(
    *,
    selection: Mapping[str, object],
    as_of_date: date,
    target_date: date,
    current_close: float,
    predicted_return: float,
) -> dict[str, object]:
    if not math.isfinite(current_close) or current_close <= 0:
        raise ValueError("当前Brent收盘价必须为正数。")
    if not math.isfinite(predicted_return):
        raise ValueError("Price Layer预测收益率无效。")
    predicted_price = current_close * (1.0 + predicted_return)
    if not math.isfinite(predicted_price) or predicted_price <= 0:
        raise ValueError("Price Layer预测价格无效。")
    model_name = str(selection["model_name"])
    display_name = MODEL_DISPLAY_NAMES.get(model_name, model_name)
    direction = "上涨" if predicted_return > 0 else "下跌" if predicted_return < 0 else "持平"
    magnitude = "小幅" if abs(predicted_return) < 0.02 else ""
    summary = (
        f"Price Layer 当前选择 {display_name} 作为历史样本外表现最好的模型，"
        f"预测下一交易日 Brent {magnitude}{direction}，预测价格为 {predicted_price:.2f} 美元/桶，"
        f"预测涨跌幅约 {predicted_return:+.2%}。"
    )
    return {
        "status": "success",
        "layer": "Price Layer",
        "target_date": target_date.isoformat(),
        "as_of_date": as_of_date.isoformat(),
        "current_close": current_close,
        "model_name": model_name,
        "model_display_name": display_name,
        "predicted_price": predicted_price,
        "predicted_return": predicted_return,
        "predicted_direction": direction,
        "direction_accuracy": float(selection["direction_accuracy"]),
        "balanced_accuracy": float(selection["balanced_accuracy"]),
        "mae": float(selection["mae"]),
        "rmse": float(selection["rmse"]),
        "oos_samples": int(selection["oos_samples"]),
        "last_oos_date": str(selection["last_oos_date"]),
        "selection_method": str(selection.get("selection_method") or ""),
        "model_comparison": list(selection.get("model_comparison") or []),
        "summary": summary,
        "future_data_used": False,
        "news_features_used": False,
    }


def _model_prefix(
    model_data_path: Path,
    *,
    as_of_date: date,
    local_bars: Sequence[object],
):
    required = {"Date", "Valid_Trading_Day", "Model_Close", "Model_Return", "Active_Price"}
    with Path(model_data_path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Price Layer模型数据缺少字段: {sorted(missing)}")
        prefix = []
        for row in reader:
            try:
                trading_date = datetime.strptime(str(row["Date"]), "%Y-%m-%d").date()
            except (TypeError, ValueError) as exc:
                raise ValueError("Price Layer模型数据包含无效日期。") from exc
            valid_flag = str(row["Valid_Trading_Day"]).strip().lower() in {"true", "1"}
            if valid_flag and trading_date <= as_of_date:
                prefix.append(
                    {
                        "Date": trading_date.isoformat(),
                        "Model_Close": float(row["Model_Close"]),
                        "Model_Return": float(row["Model_Return"] or 0.0),
                        "Active_Price": float(row["Active_Price"]),
                    }
                )
    if len(prefix) < 252:
        raise ValueError("Price Layer模型数据不足252个历史交易日。")
    prefix.sort(key=lambda row: str(row["Date"]))
    last_date = datetime.strptime(str(prefix[-1]["Date"]), "%Y-%m-%d").date()
    last_model_close = float(prefix[-1]["Model_Close"])
    last_active_price = float(prefix[-1]["Active_Price"])
    for bar in sorted(local_bars, key=lambda item: item.trading_date):
        if bar.trading_date <= last_date or bar.trading_date > as_of_date:
            continue
        model_return = float(bar.close) / last_active_price - 1.0
        last_model_close *= 1.0 + model_return
        last_active_price = float(bar.close)
        prefix.append(
            {
                "Date": bar.trading_date.isoformat(),
                "Model_Close": last_model_close,
                "Model_Return": model_return,
                "Active_Price": last_active_price,
            }
        )
    if datetime.strptime(str(prefix[-1]["Date"]), "%Y-%m-%d").date() != as_of_date:
        raise ValueError(f"Price Layer模型数据未覆盖当前完整交易日 {as_of_date.isoformat()}。")
    return prefix


def _predict_selected_return(model_name: str, prefix, project_root: Path) -> float:
    python_path = project_root / ".venv" / "bin" / "python"
    if not python_path.exists():
        raise FileNotFoundError(f"缺少六模型运行环境: {python_path}")
    runner = """
import json
import sys
import pandas as pd

project_root, model_name = sys.argv[1:3]
sys.path.insert(0, project_root)
from backtest_engine.models import (
    ARIMAAdapter,
    EnsembleAverageAdapter,
    LSTMAdapter,
    LightGBMAdapter,
    LinearMAModelAdapter,
    XGBoostAdapter,
)

prefix = pd.DataFrame(json.load(sys.stdin))
factories = {
    "ARIMA_1_1_1": ARIMAAdapter,
    "Linear_Regression_MA3_MA9": LinearMAModelAdapter,
    "LSTM": LSTMAdapter,
    "XGBoost": XGBoostAdapter,
    "LightGBM": LightGBMAdapter,
}
if model_name == "Ensemble_Average":
    base_returns = [float(factory().predict_return(prefix)) for factory in factories.values()]
    predicted_return = EnsembleAverageAdapter().predict_from_base_returns(base_returns)
else:
    if model_name not in factories:
        raise ValueError(f"Unsupported Price Layer model: {model_name}")
    predicted_return = factories[model_name]().predict_return(prefix)
print(json.dumps({"predicted_return": float(predicted_return)}))
"""
    environment = dict(os.environ)
    environment.setdefault("MPLCONFIGDIR", "/private/tmp/brent-price-layer-mpl")
    completed = subprocess.run(
        [str(python_path), "-c", runner, str(project_root), model_name],
        input=json.dumps(list(prefix), ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
        env=environment,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "未知错误"
        raise RuntimeError(f"选中模型运行失败: {detail}")
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        return float(payload["predicted_return"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("选中模型未返回有效预测结果。") from exc


def build_daily_price_forecast(
    store,
    brent,
    *,
    cutoff_utc: datetime,
    timezone_name: str = "Asia/Shanghai",
    suite_root: Path | None = None,
) -> dict[str, object]:
    paths = price_layer_paths(suite_root)
    cutoff = cutoff_utc.astimezone(ZoneInfo("UTC"))
    rows = store.brent_day_klines(
        brent.symbol,
        brent.interval,
        datetime(2024, 1, 1, tzinfo=ZoneInfo("UTC")),
        cutoff,
    )
    cutoff_local_date = cutoff.astimezone(ZoneInfo(timezone_name)).date()
    expected_session = previous_ice_trading_day(cutoff_local_date)
    validated_close = validate_complete_price_session(
        rows,
        expected_session=expected_session,
        timezone_name=timezone_name,
    )
    local_bars = build_local_session_daily_bars(
        rows,
        as_of_date=cutoff_local_date,
        timezone_name=timezone_name,
    )
    current_bar = next(
        (bar for bar in reversed(local_bars) if bar.trading_date == expected_session),
        None,
    )
    if current_bar is None:
        raise ValueError("日报截止前没有可用的完整Brent交易日价格。")
    if not math.isclose(float(current_bar.close), validated_close, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("Brent日线聚合收盘价与最后一根5分钟K线不一致。")
    as_of_date = current_bar.trading_date
    selection = select_daily_price_model(paths.results_dir, as_of_date=as_of_date)
    prefix = _model_prefix(
        paths.model_data_path,
        as_of_date=as_of_date,
        local_bars=local_bars,
    )
    model_current_price = float(prefix[-1]["Active_Price"])
    reconciliation_tolerance = max(0.05, validated_close * 0.0025)
    if abs(model_current_price - validated_close) > reconciliation_tolerance:
        raise ValueError(
            "Brent日报收盘价与Price Layer模型输入价格不一致，拒绝发布预测。"
        )
    predicted_return = _predict_selected_return(
        str(selection["model_name"]), prefix, paths.project_root
    )
    return build_price_forecast_payload(
        selection=selection,
        as_of_date=as_of_date,
        target_date=next_ice_trading_day(as_of_date),
        current_close=validated_close,
        predicted_return=predicted_return,
    )


def build_daily_price_forecast_safe(
    store,
    brent,
    *,
    cutoff_utc: datetime,
    timezone_name: str = "Asia/Shanghai",
    suite_root: Path | None = None,
) -> dict[str, object]:
    try:
        return build_daily_price_forecast(
            store,
            brent,
            cutoff_utc=cutoff_utc,
            timezone_name=timezone_name,
            suite_root=suite_root,
        )
    except Exception as exc:
        return {
            "status": "unavailable",
            "layer": "Price Layer",
            "failure_reason": f"{type(exc).__name__}: {exc}",
            "future_data_used": False,
            "news_features_used": False,
        }
