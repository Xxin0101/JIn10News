from __future__ import annotations

import json
import hashlib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .brent import BrentConfig
from .config import FetchConfig, FilterConfig, TrackerConfig
from .external_update import fetch_latest_isw_update
from .forecast import (
    aggregate_news_forecast,
    build_news_forecast_from_store,
    combine_forecasts,
    forecast_news_bounds,
    forecast_price_bounds,
)
from .tracking_report import build_html_report, build_markdown_report
from .trump_update import fetch_trump_statements


REPORT_LOGIC_VERSION = 2


def _date_label(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def _direction_label(value: str) -> str:
    return {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}.get(value, "中性")


def _read_only_external_payload(source: str) -> dict[str, object]:
    return {
        "status": "not_applicable",
        "source": source,
        "failure_reason": "周一预测只读取周五已保存的Brent、金十新闻及计算快照，不读取外部补充来源。",
        "statements": [],
        "key_takeaways": [],
    }


def _load_quant_snapshot(path: Path, *, end_day: date, target_day: date) -> dict[str, object]:
    """Load a previously computed Friday Model A result without network or refit."""
    if not path.exists():
        raise FileNotFoundError(f"缺少周五量化快照: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("quant"), dict):
        raise ValueError(f"量化快照格式无效: {path}")
    if str(payload.get("target_date") or "") != target_day.isoformat():
        raise ValueError("量化快照预测目标与本次目标不一致。")
    quant = dict(payload["quant"])
    if str(payload.get("daily_price_last_date") or quant.get("as_of_date") or "") != end_day.isoformat():
        raise ValueError("量化快照不是目标周一之前的最后完整交易日。")
    if not isinstance(quant.get("up_probability"), (int, float)):
        raise ValueError("量化快照缺少真实模型上涨概率。")
    return quant


def _parse_snapshot_datetime(value: object, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"预测快照的{field_name}格式无效。") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"预测快照的{field_name}必须包含时区。")
    return parsed.astimezone(ZoneInfo("UTC"))


def _load_forecast_snapshot(
    path: Path,
    *,
    start_day: date,
    end_day: date,
    target_day: date,
    timezone_name: str,
) -> dict[str, object]:
    """Load and validate the complete immutable Friday forecast snapshot."""
    if not path.exists():
        raise FileNotFoundError(f"缺少周五预测快照: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"预测快照格式无效: {path}")
    if str(payload.get("target_date") or "") != target_day.isoformat():
        raise ValueError("预测快照目标日期与本次周一目标不一致。")
    if str(payload.get("daily_price_last_date") or "") != end_day.isoformat():
        raise ValueError("预测快照不是目标周一之前的周五完整交易日。")
    if payload.get("future_data_used") is not False:
        raise ValueError("预测快照必须明确标记 future_data_used=false。")
    expected_news_start, expected_news_end = forecast_news_bounds(
        start_day.isoformat(),
        end_day.isoformat(),
        timezone_name,
    )
    expected_price_start, expected_price_end = forecast_price_bounds(
        start_day.isoformat(),
        end_day.isoformat(),
        timezone_name,
    )
    expected_boundaries = {
        "analysis_window_start_utc": expected_price_start,
        "analysis_window_end_utc": expected_price_end,
        "news_window_start_utc": expected_news_start,
        "news_window_end_utc": expected_news_end,
    }
    for field_name, expected in expected_boundaries.items():
        actual = _parse_snapshot_datetime(payload.get(field_name), field_name)
        if actual != expected:
            raise ValueError(f"预测快照的{field_name}与周五冻结窗口不一致。")
    for key in ("quant", "news", "final"):
        if not isinstance(payload.get(key), dict):
            raise ValueError(f"预测快照缺少已保存的{key}计算结果。")
    quant = dict(payload["quant"])
    news = dict(payload["news"])
    final = dict(payload["final"])
    if str(quant.get("as_of_date") or "") != end_day.isoformat():
        raise ValueError("量化结果的截止交易日与周五快照不一致。")
    if not isinstance(quant.get("up_probability"), (int, float)):
        raise ValueError("量化快照缺少真实模型上涨概率。")
    if not isinstance(quant.get("backtest"), dict):
        raise ValueError("量化快照缺少已保存的回测结果。")
    if not isinstance(news.get("direction"), str):
        raise ValueError("新闻快照缺少已保存的方向结果。")
    events = news.get("events")
    if not isinstance(events, list):
        raise ValueError("新闻快照缺少已保存的事件计算结果。")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("新闻快照事件格式无效。")
        published_at = _parse_snapshot_datetime(event.get("published_at"), "新闻发布时间")
        if not expected_news_start <= published_at < expected_news_end:
            raise ValueError("预测快照包含超过周五新闻截止时间的事件。")
    if not isinstance(final.get("direction"), str) or not isinstance(final.get("confidence"), str):
        raise ValueError("预测快照缺少最终方向或置信度。")
    return payload


def prepare_friday_snapshot(
    source_path: Path,
    destination_path: Path,
    *,
    start_date: str,
    end_date: str,
    target_date: str,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, object]:
    """Freeze a strict Friday snapshot from already-saved calculation output."""
    start_day, end_day, target_day = _validate_forecast_dates(start_date, end_date, target_date)
    if not source_path.exists():
        raise FileNotFoundError(f"缺少已保存的预测计算结果: {source_path}")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise ValueError("已保存的预测计算结果格式无效。")
    quant = source.get("quant")
    source_news = source.get("news")
    if not isinstance(quant, dict) or not isinstance(source_news, dict):
        raise ValueError("已保存结果缺少量化或新闻计算结果。")
    if str(source.get("target_date") or "") != target_day.isoformat():
        raise ValueError("已保存结果的目标日期不一致。")
    if str(source.get("daily_price_last_date") or quant.get("as_of_date") or "") != end_day.isoformat():
        raise ValueError("已保存量化结果不是目标周一之前的周五交易日。")
    if source.get("future_data_used") is not False:
        raise ValueError("已保存结果必须明确标记 future_data_used=false。")

    news_start_utc, news_end_utc = forecast_news_bounds(start_date, end_date, timezone_name)
    price_start_utc, price_end_utc = forecast_price_bounds(start_date, end_date, timezone_name)
    source_events = source_news.get("events")
    if not isinstance(source_events, list):
        raise ValueError("已保存结果缺少新闻事件计算记录。")
    frozen_events: list[dict[str, object]] = []
    excluded_dates: set[date] = set()
    for event in source_events:
        if not isinstance(event, dict):
            continue
        published_at = _parse_snapshot_datetime(event.get("published_at"), "新闻发布时间")
        if news_start_utc <= published_at < news_end_utc:
            frozen_events.append(dict(event))
        elif published_at >= news_end_utc:
            excluded_dates.add(published_at.astimezone(ZoneInfo(timezone_name)).date())
    if not frozen_events:
        raise ValueError("周五新闻截止时间内没有可冻结的已保存事件。")

    frozen_summary = aggregate_news_forecast(frozen_events)
    source_history_sessions = int(source_news.get("history_sessions") or 0)
    news: dict[str, object] = {
        **source_news,
        **frozen_summary,
        "events": frozen_events,
        "important_news": [
            event for event in frozen_events if str(event.get("direction") or "") in {"bullish", "bearish"}
        ],
        "history_sessions": max(0, source_history_sessions - len(excluded_dates)),
        "model_a_overlap": {"status": "frozen_friday_snapshot", "test_samples": 0, "metrics": None},
        "model_b": {"status": "frozen_friday_snapshot", "test_samples": 0, "metrics": None},
        "model_c": {
            "status": "frozen_friday_snapshot",
            "test_samples": 0,
            "metrics": None,
            "probability_eligible": False,
        },
        "backtest_note": "周五冻结快照仅复用截止时间内已保存的事件计算；不使用周末事件重算Model B/C。",
    }
    quant_metrics = dict((quant.get("backtest") or {}).get("metrics") or {})
    combined = combine_forecasts(
        quant_up_probability=float(quant["up_probability"]),
        quant_metrics=quant_metrics,
        news_direction=str(news["direction"]),
        news_model_metrics=None,
        combined_model_metrics=None,
    )
    final = {
        "direction": combined.direction,
        "confidence": combined.confidence,
        "final_up_probability": combined.final_up_probability,
        "probability_note": combined.probability_note,
    }
    frozen_payload: dict[str, object] = {
        "schema_version": 1,
        "snapshot_kind": "frozen_friday",
        "analysis_window_start_utc": price_start_utc.isoformat(),
        "analysis_window_end_utc": price_end_utc.isoformat(),
        "news_window_start_utc": news_start_utc.isoformat(),
        "news_window_end_utc": news_end_utc.isoformat(),
        "target_date": target_day.isoformat(),
        "daily_price_last_date": end_day.isoformat(),
        "future_data_used": False,
        "source_calculation_sha256": _snapshot_sha256(source_path),
        "quant": quant,
        "news": news,
        "final": final,
    }
    _write_result_manifest(destination_path, frozen_payload)
    return {
        "snapshot_path": str(destination_path),
        "snapshot_sha256": _snapshot_sha256(destination_path),
        "included_events": len(frozen_events),
        "excluded_after_friday_cutoff": sum(
            1
            for event in source_events
            if isinstance(event, dict)
            and _parse_snapshot_datetime(event.get("published_at"), "新闻发布时间") >= news_end_utc
        ),
    }


def _snapshot_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_reusable_result(
    manifest_path: Path,
    *,
    snapshot_sha256: str,
    markdown_path: Path,
    html_path: Path,
) -> dict[str, object] | None:
    if not manifest_path.exists() or not markdown_path.exists() or not html_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("report_logic_version") != REPORT_LOGIC_VERSION:
        return None
    if str(payload.get("snapshot_sha256") or "") != snapshot_sha256:
        return None
    if str(payload.get("markdown_path") or "") != str(markdown_path):
        return None
    if str(payload.get("html_path") or "") != str(html_path):
        return None
    return payload


def _write_result_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _validate_forecast_dates(start_label: str, end_label: str, target_label: str) -> tuple[date, date, date]:
    start = datetime.strptime(start_label, "%Y-%m-%d").date()
    end = datetime.strptime(end_label, "%Y-%m-%d").date()
    target = datetime.strptime(target_label, "%Y-%m-%d").date()
    if end < start:
        raise ValueError("分析结束日期不能早于开始日期。")
    if target <= end:
        raise ValueError("预测目标必须晚于分析结束日期。")
    if end.weekday() != 4 or target.weekday() != 0 or target != end + timedelta(days=3):
        raise ValueError("周一方向预测必须使用紧邻目标周一之前的周五快照。")
    return start, end, target


def same_day_forecast_bounds(
    start_label: str,
    end_label: str,
    target_label: str,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[datetime, datetime]:
    """Return the approved same-day information window, ending at 08:00 on target day."""
    start = datetime.strptime(start_label, "%Y-%m-%d").date()
    end = datetime.strptime(end_label, "%Y-%m-%d").date()
    target = datetime.strptime(target_label, "%Y-%m-%d").date()
    if end < start:
        raise ValueError("分析结束日期不能早于开始日期。")
    if end != target:
        raise ValueError("同日预测的分析结束日期必须等于预测目标日期。")
    if target.weekday() != 0:
        raise ValueError("当前同日预测流程只允许使用周五量化快照预测紧邻周一。")
    tz = ZoneInfo(timezone_name)
    local_start = datetime.combine(start, time(8, 0), tzinfo=tz)
    local_end = datetime.combine(target, time(8, 0), tzinfo=tz)
    if local_end <= local_start:
        raise ValueError("同日预测窗口结束时间必须晚于开始时间。")
    return local_start.astimezone(ZoneInfo("UTC")), local_end.astimezone(ZoneInfo("UTC"))


def _load_same_day_quant_snapshot(
    path: Path,
    *,
    friday: date,
    target: date,
) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"缺少同日预测所需的周五基础快照: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("quant"), dict):
        raise ValueError("同日预测基础快照格式无效。")
    if str(payload.get("target_date") or "") != target.isoformat():
        raise ValueError("同日预测基础快照的目标日期不一致。")
    quant = dict(payload["quant"])
    last_date = str(payload.get("daily_price_last_date") or quant.get("as_of_date") or "")
    if last_date != friday.isoformat() or str(quant.get("as_of_date") or "") != friday.isoformat():
        raise ValueError("同日预测必须复用目标周一前的周五量化结果。")
    if payload.get("future_data_used") is not False:
        raise ValueError("同日预测基础快照必须明确标记 future_data_used=false。")
    if not isinstance(quant.get("up_probability"), (int, float)):
        raise ValueError("同日预测基础快照缺少真实模型上涨概率。")
    if not isinstance(quant.get("backtest"), dict):
        raise ValueError("同日预测基础快照缺少时间顺序回测。")
    return quant


def _prepare_same_day_news(
    news: Mapping[str, object],
    *,
    friday_close_utc: datetime,
    target_open_utc: datetime,
) -> dict[str, object]:
    """Treat directional news published during the closed market as not yet priced."""
    prepared_events: list[dict[str, object]] = []
    for source_event in list(news.get("events") or []):
        if not isinstance(source_event, Mapping):
            continue
        event = dict(source_event)
        published = _parse_snapshot_datetime(event.get("published_at"), "同日预测新闻发布时间")
        if (
            friday_close_utc <= published < target_open_utc
            and str(event.get("pricing_status") or "") == "UNKNOWN"
            and str(event.get("direction") or "") in {"bullish", "bearish"}
        ):
            event["pricing_status"] = "LOW"
            event["pricing_explanation"] = "周末休市期间发布，截至目标日08:00前尚无充分价格反应，按低定价状态进入定性判断。"
        prepared_events.append(event)
    summary = aggregate_news_forecast(prepared_events)
    return {
        **dict(news),
        **summary,
        "events": prepared_events,
        "important_news": [
            event for event in prepared_events if event.get("direction") in {"bullish", "bearish"}
        ],
    }


def _require_complete_external_updates(
    external_update: Mapping[str, object],
    trump_update: Mapping[str, object],
) -> None:
    source_items = list(external_update.get("source_key_takeaways") or [])
    translated_items = list(external_update.get("key_takeaways_zh") or [])
    source_count = int(external_update.get("source_key_takeaways_count") or 0)
    translated_count = int(external_update.get("translated_key_takeaways_count") or 0)
    if (
        external_update.get("status") != "success"
        or not source_items
        or len(source_items) != len(translated_items)
        or source_count != len(source_items)
        or translated_count != len(translated_items)
    ):
        reason = str(external_update.get("failure_reason") or "Key Takeaways原文/译文不完整")
        raise RuntimeError(f"ISW补充源未通过完整性校验：{reason}")
    statements = list(trump_update.get("statements") or [])
    declared_statements = int(trump_update.get("statement_count") or 0)
    if trump_update.get("status") != "success" or declared_statements != len(statements):
        reason = str(trump_update.get("failure_reason") or "声明数量与列表不一致")
        raise RuntimeError(f"特朗普言论补充源未通过完整性校验：{reason}")


def _model_metrics(model: Mapping[str, object]) -> dict[str, float | None] | None:
    metrics = model.get("metrics")
    return dict(metrics) if isinstance(metrics, Mapping) else None


def run_same_day_forecast_report(
    store,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    logger,
    *,
    start_date: str,
    end_date: str,
    target_date: str,
    base_snapshot_path: Path,
    retry_failed_isw: bool = False,
    retry_failed_trump: bool = False,
) -> dict[str, Any]:
    """Build a target-day 08:00 forecast using Friday Model A plus news through the cutoff."""
    window_start_utc, window_end_utc = same_day_forecast_bounds(
        start_date,
        end_date,
        target_date,
        tracker_config.timezone,
    )
    start_day = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_day = datetime.strptime(end_date, "%Y-%m-%d").date()
    target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
    friday = target_day - timedelta(days=3)
    quant = _load_same_day_quant_snapshot(
        Path(base_snapshot_path),
        friday=friday,
        target=target_day,
    )
    raw_news = build_news_forecast_from_store(
        store,
        symbol=brent_config.symbol,
        interval=brent_config.interval,
        start_utc=window_start_utc,
        end_utc=window_end_utc,
        price_end_utc=window_end_utc,
    )
    friday_close_utc = forecast_price_bounds(
        friday.isoformat(),
        friday.isoformat(),
        tracker_config.timezone,
    )[1]
    target_open_local = datetime.combine(
        target_day,
        time(6, 0),
        tzinfo=ZoneInfo(tracker_config.timezone),
    )
    news = _prepare_same_day_news(
        raw_news,
        friday_close_utc=friday_close_utc,
        target_open_utc=target_open_local.astimezone(ZoneInfo("UTC")),
    )
    model_b = dict(news.get("model_b") or {})
    model_c = dict(news.get("model_c") or {})
    combined = combine_forecasts(
        quant_up_probability=float(quant["up_probability"]),
        quant_metrics=dict((quant.get("backtest") or {}).get("metrics") or {}),
        news_direction=str(news.get("direction") or "neutral"),
        news_model_metrics=_model_metrics(model_b),
        combined_model_metrics=_model_metrics(model_c),
        combined_up_probability=(
            float(model_c["up_probability"])
            if isinstance(model_c.get("up_probability"), (int, float))
            else None
        ),
        combined_probability_eligible=bool(model_c.get("probability_eligible")),
        secondary_test_samples=int(model_c.get("test_samples") or 0),
    )

    cache_dir = Path(tracker_config.database_path).parent / "external_updates"
    external_update = fetch_latest_isw_update(
        window_end_utc,
        cache_dir=cache_dir,
        timezone_name=tracker_config.timezone,
        retry_failed_cache=retry_failed_isw,
    )
    trump_update = fetch_trump_statements(
        window_start_utc,
        window_end_utc,
        cache_dir=cache_dir,
        timezone_name=tracker_config.timezone,
        retry_failed_cache=retry_failed_trump,
    )
    _require_complete_external_updates(external_update, trump_update)

    news_direction = _direction_label(str(news.get("direction") or "neutral"))
    forecast_payload: dict[str, object] = {
        "target_date": _date_label(target_day),
        "analysis_start_date": start_day.isoformat(),
        "analysis_end_date": end_day.isoformat(),
        "direction": combined.direction,
        "confidence": combined.confidence,
        "quant_up_probability": quant["up_probability"],
        "quant_down_probability": quant.get("down_probability", 1.0 - float(quant["up_probability"])),
        "quant_model": quant.get("model_name"),
        "news_direction": news_direction,
        "final_up_probability": combined.final_up_probability,
        "probability_note": combined.probability_note,
        "quant_reasons": list(quant.get("reasons") or [])[:3],
        "news_reasons": list(news.get("reasons") or [])[:3],
        "priced_events": list(news.get("priced_events") or [])[:3],
        "weekend_risks": [
            "Future Risk Event：24日盘中地缘冲突若升级或缓和，可能快速改变Brent风险溢价。",
            "Future Risk Event：24日盘中霍尔木兹海峡、港口或油轮通行变化可能引发价格跳变。",
            "Future Risk Event：24日盘中供应中断扩大或恢复可能改变当日供需预期。",
        ],
        "risk_heading": "24日盘中风险",
        "forecast_kicker": "TARGET DAY",
        "important_news_heading": "21–24日重要原油新闻与价格状态",
        "reaction_end_label": "截至24日08:00",
        "news_scope_note": "包含周末新闻；截至预测日北京时间08:00，不含此后信息",
        "performance_window_label": "21–24日区间涨跌幅",
        "model_a": quant["backtest"],
        "model_b": model_b,
        "model_c": model_c,
        "model_a_overlap": dict(news.get("model_a_overlap") or {}),
        "news_history_sessions": int(news.get("history_sessions") or 0),
        "structured_news_features": dict(news.get("structured_features") or {}),
        "backtest_note": str(news.get("backtest_note") or ""),
        "important_news": list(news.get("important_news") or []),
        "forecast_cutoff_utc": window_end_utc.isoformat(),
        "future_data_used": False,
    }
    output_dir = Path(tracker_config.output_dir)
    output_stem = (
        f"Jin10_Brent_SameDay_Forecast_{start_day.strftime('%Y%m%d')}_"
        f"{end_day.strftime('%Y%m%d')}_for_{target_day.strftime('%Y%m%d')}"
    )
    report_result = build_markdown_report(
        store,
        brent_config,
        output_dir,
        timezone_name=tracker_config.timezone,
        external_update=dict(external_update),
        trump_update=dict(trump_update),
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
        news_window_start_utc=window_start_utc,
        news_window_end_utc=window_end_utc,
        output_stem=output_stem,
        forecast=forecast_payload,
        read_only_source=True,
        frozen_snapshot=False,
    )
    html_path = build_html_report(
        report_result.markdown_path,
        output_dir,
        presentation=report_result.presentation,
    )
    result_json_path = output_dir / f"{output_stem}_forecast_result.json"
    result_payload = {
        "schema_version": 1,
        "report_logic_version": REPORT_LOGIC_VERSION,
        "base_snapshot_path": str(base_snapshot_path),
        "base_snapshot_sha256": _snapshot_sha256(Path(base_snapshot_path)),
        "forecast_cutoff_utc": window_end_utc.isoformat(),
        "future_data_used": False,
        "markdown_path": str(report_result.markdown_path),
        "html_path": str(html_path),
        "target_date": target_day.isoformat(),
        "quant": quant,
        "news": news,
        "final": {
            "direction": combined.direction,
            "confidence": combined.confidence,
            "final_up_probability": combined.final_up_probability,
            "probability_note": combined.probability_note,
        },
        "external_update": dict(external_update),
        "trump_update": dict(trump_update),
    }
    _write_result_manifest(result_json_path, result_payload)
    logger.info(
        "同日预测日报完成: target=%s cutoff=%s model=%s news=%s final=%s html=%s",
        target_day,
        window_end_utc,
        quant.get("model_name"),
        news_direction,
        combined.direction,
        html_path,
    )
    return {
        "html_path": str(html_path),
        "markdown_path": str(report_result.markdown_path),
        "forecast_result_json_path": str(result_json_path),
        "base_snapshot_path": str(base_snapshot_path),
        "forecast": forecast_payload,
        "quant": quant,
        "news": news,
        "external_update": dict(external_update),
        "trump_update": dict(trump_update),
    }


def run_forecast_report(
    store,
    fetch_config: FetchConfig,
    filter_config: FilterConfig,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    logger,
    *,
    start_date: str,
    end_date: str,
    target_date: str,
) -> dict[str, Any]:
    start_day, end_day, target_day = _validate_forecast_dates(start_date, end_date, target_date)
    news_start_utc, news_end_utc = forecast_news_bounds(start_date, end_date, tracker_config.timezone)
    price_start_utc, price_end_utc = forecast_price_bounds(start_date, end_date, tracker_config.timezone)
    output_dir = Path(tracker_config.output_dir)
    output_stem = (
        f"Jin10_Brent_Daily_Forecast_{start_day.strftime('%Y%m%d')}_"
        f"{end_day.strftime('%Y%m%d')}_for_{target_day.strftime('%Y%m%d')}"
    )
    snapshot_path = output_dir / f"{output_stem}_friday_snapshot.json"
    result_json_path = output_dir / f"{output_stem}_forecast_result.json"
    expected_markdown_path = output_dir / f"{output_stem}.md"
    expected_html_path = output_dir / f"{output_stem}.html"
    backfills: list[dict[str, object]] = []
    snapshot = _load_forecast_snapshot(
        snapshot_path,
        start_day=start_day,
        end_day=end_day,
        target_day=target_day,
        timezone_name=tracker_config.timezone,
    )
    snapshot_sha256 = _snapshot_sha256(snapshot_path)
    quant = dict(snapshot["quant"])
    news = dict(snapshot["news"])
    final = dict(snapshot["final"])
    logger.info(
        "周末硬只读模式：复用周五完整预测快照，不回补、不抓取、不重新拟合或重算新闻方向。"
    )
    weekend_risks = list(news.get("weekend_risks") or [])
    if not weekend_risks:
        weekend_risks = [
            "Future Risk Event：周末若出现新的OPEC+供应政策信息，可能改变周一方向。",
            "Future Risk Event：周末地缘冲突若升级或缓和，可能改变风险溢价。",
            "Future Risk Event：关键航道或供应中断若出现新变化，可能造成周一跳空。",
        ]
    forecast_payload: dict[str, object] = {
        "target_date": _date_label(target_day),
        "analysis_start_date": start_day.isoformat(),
        "analysis_end_date": end_day.isoformat(),
        "direction": final["direction"],
        "confidence": final["confidence"],
        "quant_up_probability": quant["up_probability"],
        "quant_down_probability": quant["down_probability"],
        "quant_model": quant["model_name"],
        "news_direction": _direction_label(str(news["direction"])),
        "final_up_probability": final.get("final_up_probability"),
        "probability_note": str(final.get("probability_note") or ""),
        "quant_reasons": list(quant.get("reasons") or [])[:3],
        "news_reasons": list(news.get("reasons") or [])[:3],
        "priced_events": list(news.get("priced_events") or [])[:3],
        "weekend_risks": weekend_risks[:3],
        "model_a": quant["backtest"],
        "model_b": news["model_b"],
        "model_c": news["model_c"],
        "model_a_overlap": dict(news.get("model_a_overlap") or {}),
        "news_history_sessions": news["history_sessions"],
        "structured_news_features": news["structured_features"],
        "backtest_note": news.get("backtest_note"),
        "important_news": list(news.get("important_news") or []),
    }

    reusable_result = _load_reusable_result(
        result_json_path,
        snapshot_sha256=snapshot_sha256,
        markdown_path=expected_markdown_path,
        html_path=expected_html_path,
    )
    if reusable_result is not None:
        logger.info("预测日报输入快照未变化，直接复用既有正式结果: %s", expected_html_path)
        return {
            "html_path": str(expected_html_path),
            "markdown_path": str(expected_markdown_path),
            "forecast_snapshot_path": str(snapshot_path),
            "forecast_result_json_path": str(result_json_path),
            "snapshot_sha256": snapshot_sha256,
            "reused": True,
            "forecast": forecast_payload,
            "quant": quant,
            "news": news,
            "backfills": backfills,
        }

    external_update = _read_only_external_payload("ISW Iran Update")
    trump_update = _read_only_external_payload("金十数据特朗普专栏")

    report_result = build_markdown_report(
        store,
        brent_config,
        output_dir,
        timezone_name=tracker_config.timezone,
        external_update=external_update,
        trump_update=trump_update,
        window_start_utc=price_start_utc,
        window_end_utc=price_end_utc,
        news_window_start_utc=news_start_utc,
        news_window_end_utc=news_end_utc,
        output_stem=output_stem,
        forecast=forecast_payload,
        read_only_source=True,
        frozen_snapshot=True,
    )
    html_path = build_html_report(
        report_result.markdown_path,
        output_dir,
        presentation=report_result.presentation,
    )
    manifest_payload: dict[str, object] = {
        "schema_version": 1,
        "report_logic_version": REPORT_LOGIC_VERSION,
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": snapshot_sha256,
        "markdown_path": str(report_result.markdown_path),
        "html_path": str(html_path),
        "target_date": target_day.isoformat(),
        "daily_price_last_date": end_day.isoformat(),
        "final": final,
    }
    _write_result_manifest(result_json_path, manifest_payload)
    logger.info(
        "预测日报完成: target=%s model=%s quant_up=%.6f final_direction=%s confidence=%s html=%s",
        target_day,
        quant["model_name"],
        quant["up_probability"],
        final["direction"],
        final["confidence"],
        html_path,
    )
    return {
        "html_path": str(html_path),
        "markdown_path": str(report_result.markdown_path),
        "forecast_snapshot_path": str(snapshot_path),
        "forecast_result_json_path": str(result_json_path),
        "snapshot_sha256": snapshot_sha256,
        "reused": False,
        "forecast": forecast_payload,
        "quant": quant,
        "news": news,
        "backfills": backfills,
    }
