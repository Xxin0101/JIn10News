from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .config import BrentConfig, TrackerConfig
from .eventing import has_direct_oil_market_relevance, identify_event, major_topic_reason, market_bias_for_event
from .storage import TrackerStore
from .time_utils import ensure_utc, iso_utc, parse_iso_utc, to_beijing, utc_now


CORE_WINDOWS = [
    ("事件后5分钟", "pct_5m"),
    ("事件后15分钟", "pct_15m"),
    ("事件后30分钟", "pct_30m"),
    ("事件后60分钟", "pct_60m"),
    ("前30分钟至后30分钟", "pre30_to_post30_pct"),
]


def _pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:+.2f}%"


def _move(base: float | None, current: float | None) -> float | None:
    if base is None or current is None or base == 0:
        return None
    return (current - base) / base


def _close(row) -> float | None:
    return float(row["close"]) if row is not None and row["close"] is not None else None


def _time(row) -> str | None:
    return str(row["ts_utc"]) if row is not None and row["ts_utc"] is not None else None


def _row_get(row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _bj_time(value: str | None, timezone_name: str) -> str:
    parsed = parse_iso_utc(value)
    if not parsed:
        return "N/A"
    return to_beijing(parsed, timezone_name).strftime("%Y-%m-%d %H:%M:%S")


def _price(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def _kline_after_with_reason(store: TrackerStore, brent: BrentConfig, target_utc: datetime, label: str, max_lag: int):
    row = store.kline_at_or_after(brent.symbol, brent.interval, target_utc)
    if row is None:
        return None, f"{label}缺失：目标时间之后无可用K线，可能为Yahoo数据延迟或休市。"
    matched = parse_iso_utc(row["ts_utc"])
    if not matched:
        return None, f"{label}缺失：K线时间无法解析。"
    lag = matched - ensure_utc(target_utc)
    if lag < timedelta(0) or lag > timedelta(minutes=max_lag):
        return None, f"{label}缺失：最近后续K线滞后{lag}，超过{max_lag}分钟。"
    return row, ""


def _kline_before_with_reason(store: TrackerStore, brent: BrentConfig, target_utc: datetime, label: str, max_lag: int):
    row = store.kline_at_or_before(brent.symbol, brent.interval, target_utc)
    if row is None:
        return None, f"{label}缺失：目标时间之前无可用K线，可能为盘前无K线、休市或Yahoo数据延迟。"
    matched = parse_iso_utc(row["ts_utc"])
    if not matched:
        return None, f"{label}缺失：K线时间无法解析。"
    lag = ensure_utc(target_utc) - matched
    if lag < timedelta(0) or lag > timedelta(minutes=max_lag):
        return None, f"{label}缺失：最近前序K线距离目标时间{lag}，超过{max_lag}分钟。"
    return row, ""


def _nearest_kline_with_reason(store: TrackerStore, brent: BrentConfig, target_utc: datetime, label: str, max_lag: int):
    row = store.nearest_kline(brent.symbol, brent.interval, target_utc)
    if row is None:
        return None, f"{label}缺失：目标时间段无可用K线，可能为盘前无K线、休市或Yahoo数据延迟。"
    matched = parse_iso_utc(row["ts_utc"])
    if not matched:
        return None, f"{label}缺失：K线时间无法解析。"
    lag = abs(matched - ensure_utc(target_utc))
    if lag > timedelta(minutes=max_lag):
        return None, f"{label}缺失：最近K线距离目标时间{lag}，超过{max_lag}分钟。"
    return row, ""


def _trigger_from_values(values: dict[str, float | None], config: TrackerConfig) -> tuple[str | None, float | None]:
    available = [(label, values[key]) for label, key in CORE_WINDOWS if values.get(key) is not None]
    if not available:
        return None, None
    label, value = max(available, key=lambda item: abs(float(item[1] or 0)))
    return label, float(value) if value is not None else None


def _reaction_level(
    values: dict[str, float | None],
    is_major: bool,
    direct_oil: bool,
    complete_price: bool,
    config: TrackerConfig,
) -> tuple[str, bool, bool, str, float | None]:
    trigger_window, trigger_value = _trigger_from_values(values, config)
    if not complete_price:
        return "价格数据不足", bool(is_major and direct_oil), False, trigger_window or "", trigger_value
    if trigger_value is not None and abs(trigger_value) >= config.reaction_threshold:
        direction = "偏利多" if trigger_value > 0 else "偏利空"
        return f"强市场反应：{direction}", direct_oil, True, trigger_window or "", trigger_value
    if trigger_value is not None and abs(trigger_value) >= config.medium_reaction_threshold:
        direction = "偏利多" if trigger_value > 0 else "偏利空"
        return f"中等市场反应：{direction}", direct_oil, True, trigger_window or "", trigger_value
    if is_major and direct_oil:
        return "重要但暂未充分定价", True, False, trigger_window or "", trigger_value
    return "市场反应有限", False, False, trigger_window or "", trigger_value


def _market_pricing_status(level: str, bias: str, trigger_value: float | None) -> str:
    if level == "价格数据不足":
        return "无法判断"
    if trigger_value is not None:
        if "偏多" in bias and trigger_value < -0.0025:
            return "与新闻逻辑背离"
        if "偏空" in bias and trigger_value > 0.0025:
            return "与新闻逻辑背离"
    if level.startswith("强市场反应") or level.startswith("中等市场反应"):
        return "已部分定价"
    if level == "重要但暂未充分定价":
        return "尚未充分定价"
    return "无明显定价"


def _interpretation(
    level: str,
    values: dict[str, float | None],
    trigger_window: str,
    trigger_value: float | None,
    bias: str,
    pricing_status: str,
) -> str:
    if level == "价格数据不足":
        return "价格数据不足：未匹配到完整的前30、事件基准、后5/15/30/60分钟价格，不能默认为无反应。"
    if level.startswith("强市场反应") or level.startswith("中等市场反应"):
        return (
            f"{level}：{trigger_window}Brent涨跌幅为{_pct(trigger_value)}，"
            f"5/15/30/60分钟分别为{_pct(values.get('pct_5m'))}、{_pct(values.get('pct_15m'))}、"
            f"{_pct(values.get('pct_30m'))}、{_pct(values.get('pct_60m'))}。"
            f"市场定价状态：{pricing_status}。{bias}"
        )
    if level == "重要但暂未充分定价":
        return (
            f"该事件命中重大油市主题，但核心窗口最大反应为{_pct(trigger_value)}，未达到0.25%阈值。"
            f"市场定价状态：{pricing_status}。{bias}"
        )
    return f"该事件未命中重大油市主题，且核心窗口最大反应为{_pct(trigger_value)}，暂不进入重点事件。"


def _exclusion_reason(
    direct_oil: bool,
    oil_reason: str,
    is_major: bool,
    complete_price: bool,
    trigger_value: float | None,
    price_missing_reason: str,
    config: TrackerConfig,
) -> str:
    if not direct_oil:
        return f"未进入重点事件：{oil_reason}。"
    if not complete_price and not is_major:
        return f"未进入重点事件：价格数据不足且新闻重要性不足；{price_missing_reason}"
    if not is_major:
        return "未进入重点事件：不属于重大主题。"
    if trigger_value is None or abs(trigger_value) < config.medium_reaction_threshold:
        return (
            f"未进入重点事件：价格反应低于0.25%，核心窗口最大反应{_pct(trigger_value)}，"
            "且未被选为更高优先级事件。"
        )
    return "未进入重点事件：被更高优先级事件替代。"


def _analyze_rows(
    rows,
    store: TrackerStore,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    logger: logging.Logger | None = None,
    force: bool = False,
) -> dict[str, int]:
    now = utc_now()
    stats = {
        "analyzed": 0,
        "pending": 0,
        "daily": 0,
        "price_matched": 0,
        "price_missing": 0,
        "price_5m_missing": 0,
        "price_15m_missing": 0,
        "price_30m_missing": 0,
        "price_60m_missing": 0,
        "major_topic_hits": 0,
        "threshold_hits": 0,
        "effective_oil_news": 0,
        "summary_without_event": 0,
        "excluded": 0,
    }

    for row in rows:
        news_id = str(row["news_id"])
        published = parse_iso_utc(row["published_at_utc"])
        if not published:
            stats["price_missing"] += 1
            continue

        title = str(row["title"])
        content = str(row["content"])
        event_id, event_topic = identify_event(title, content)
        if event_id != _row_get(row, "event_id") or event_topic != _row_get(row, "event_topic"):
            store.update_event_metadata(news_id, event_id, event_topic)

        direct_oil, oil_reason = has_direct_oil_market_relevance(title, content)
        if direct_oil:
            stats["effective_oil_news"] += 1
        elif "汇总类新闻" in oil_reason:
            stats["summary_without_event"] += 1

        importance_reason = major_topic_reason(title, content)
        is_major = bool(importance_reason and direct_oil)
        if is_major:
            stats["major_topic_hits"] += 1

        published_bj = to_beijing(published, tracker_config.timezone)
        if not force and now < ensure_utc(published_bj + timedelta(minutes=tracker_config.extended_reaction_window_minutes)):
            stats["pending"] += 1
            continue

        max_lag = tracker_config.max_price_match_lag_minutes
        missing_reasons: list[str] = []
        pre_event_base, pre_event_reason = _kline_before_with_reason(
            store, brent_config, ensure_utc(published_bj), "新闻前基准K线", max_lag
        )
        event_base, reason = _kline_after_with_reason(
            store, brent_config, ensure_utc(published_bj), "事件基准K线", max_lag
        )
        if reason:
            missing_reasons.append(reason)

        event_base_time = parse_iso_utc(_time(event_base))
        event_base_close = _close(event_base)
        pre_event_base_close = _close(pre_event_base)
        targets: dict[str, datetime | None] = {}
        if event_base_time:
            targets = {
                "pre30": event_base_time - timedelta(minutes=30),
                "5m": event_base_time + timedelta(minutes=5),
                "15m": event_base_time + timedelta(minutes=15),
                "30m": event_base_time + timedelta(minutes=30),
                "60m": event_base_time + timedelta(minutes=60),
            }
        else:
            missing_reasons.append("无法计算目标窗口：事件基准K线缺失。")

        matched: dict[str, Any] = {"pre30": None, "5m": None, "15m": None, "30m": None, "60m": None}
        labels = {
            "pre30": "事件前30分钟价格",
            "5m": "事件后5分钟价格",
            "15m": "事件后15分钟价格",
            "30m": "事件后30分钟价格",
            "60m": "事件后60分钟价格",
        }
        for key, target in targets.items():
            if target is None:
                continue
            row_match, reason = _nearest_kline_with_reason(store, brent_config, target, labels[key], max_lag)
            matched[key] = row_match
            if reason:
                missing_reasons.append(reason)

        pre30_close = _close(matched["pre30"])
        price_5m = _close(matched["5m"])
        price_15m = _close(matched["15m"])
        price_30m = _close(matched["30m"])
        price_60m = _close(matched["60m"])
        values = {
            "pct_5m": _move(event_base_close, price_5m),
            "pct_15m": _move(event_base_close, price_15m),
            "pct_30m": _move(event_base_close, price_30m),
            "pct_60m": _move(event_base_close, price_60m),
            "pre30_to_post30_pct": _move(pre30_close, price_30m),
        }

        if price_5m is None:
            stats["price_5m_missing"] += 1
        if price_15m is None:
            stats["price_15m_missing"] += 1
        if price_30m is None:
            stats["price_30m_missing"] += 1
        if price_60m is None:
            stats["price_60m_missing"] += 1

        complete_price = all(
            value is not None
            for value in [event_base_close, pre30_close, price_5m, price_15m, price_30m, price_60m]
        )
        if event_base_close is not None and price_5m is not None:
            stats["price_matched"] += 1
        if not complete_price:
            stats["price_missing"] += 1

        window_30_klines = []
        if event_base_time:
            window_30_klines = store.window_klines(
                brent_config.symbol,
                brent_config.interval,
                event_base_time,
                event_base_time + timedelta(minutes=tracker_config.reaction_window_minutes),
            )
        window_high = max((float(k["high"]) for k in window_30_klines), default=None)
        window_low = min((float(k["low"]) for k in window_30_klines), default=None)
        max_up = _move(event_base_close, window_high)
        max_down = _move(event_base_close, window_low)

        level, is_in_daily, by_threshold, trigger_window, trigger_value = _reaction_level(
            values,
            is_major,
            direct_oil,
            complete_price,
            tracker_config,
        )
        if by_threshold:
            stats["threshold_hits"] += 1
        if is_in_daily:
            stats["daily"] += 1
        else:
            stats["excluded"] += 1

        price_status = "ok" if complete_price else "price_data_unavailable"
        status = "analyzed"
        if not force and not complete_price:
            status = "Pending Brent Reaction Analysis"
            stats["pending"] += 1

        bias = market_bias_for_event(title, content, event_topic, trigger_value)
        pricing_status = _market_pricing_status(level, bias, trigger_value)
        price_missing_reason = "；".join(dict.fromkeys(missing_reasons))
        debug_missing_reasons = list(missing_reasons)
        if pre_event_reason:
            debug_missing_reasons.append(pre_event_reason)
        debug_missing_reason = "；".join(dict.fromkeys(debug_missing_reasons))
        exclusion_reason = ""
        if not is_in_daily:
            exclusion_reason = _exclusion_reason(
                direct_oil,
                oil_reason,
                is_major,
                complete_price,
                trigger_value,
                price_missing_reason,
                tracker_config,
            )

        if logger:
            logger.info(
                "Brent匹配调试: news_id=%s raw_time=%s news_bj=%s pre_event_base_bj=%s event_base_bj=%s "
                "pre30_bj=%s price_5m_bj=%s price_15m_bj=%s price_30m_bj=%s price_60m_bj=%s "
                "pre_event_base=%s event_base=%s pre30=%s p5=%s p15=%s p30=%s p60=%s "
                "pct5=%s pct15=%s pct30=%s pct60=%s pre30_to_post30=%s "
                "level=%s trigger=%s major=%s direct_oil=%s missing=%s",
                news_id,
                _row_get(row, "raw_time", "N/A") or "N/A",
                published_bj.strftime("%Y-%m-%d %H:%M:%S"),
                _bj_time(_time(pre_event_base), tracker_config.timezone),
                _bj_time(_time(event_base), tracker_config.timezone),
                _bj_time(_time(matched["pre30"]), tracker_config.timezone),
                _bj_time(_time(matched["5m"]), tracker_config.timezone),
                _bj_time(_time(matched["15m"]), tracker_config.timezone),
                _bj_time(_time(matched["30m"]), tracker_config.timezone),
                _bj_time(_time(matched["60m"]), tracker_config.timezone),
                _price(pre_event_base_close),
                _price(event_base_close),
                _price(pre30_close),
                _price(price_5m),
                _price(price_15m),
                _price(price_30m),
                _price(price_60m),
                _pct(values["pct_5m"]),
                _pct(values["pct_15m"]),
                _pct(values["pct_30m"]),
                _pct(values["pct_60m"]),
                _pct(values["pre30_to_post30_pct"]),
                level,
                f"{trigger_window} {_pct(trigger_value)}" if trigger_window else "N/A",
                "yes" if is_major else "no",
                "yes" if direct_oil else "no",
                debug_missing_reason or "无",
            )
            if exclusion_reason:
                logger.info("未进入重点事件原因: news_id=%s title=%s reason=%s", news_id, title, exclusion_reason)

        reaction: dict[str, Any] = {
            "baseline_time_utc": _time(pre_event_base),
            "baseline_close": pre_event_base_close,
            "event_base_time_utc": _time(event_base),
            "event_base_close": event_base_close,
            "window_start_utc": iso_utc(event_base_time) if event_base_time else None,
            "window_end_utc": iso_utc(event_base_time + timedelta(minutes=tracker_config.reaction_window_minutes))
            if event_base_time
            else None,
            "window_high": window_high,
            "window_low": window_low,
            "final_close": price_30m,
            "max_up_pct": max_up,
            "max_down_pct": max_down,
            "final_pct": values["pct_30m"],
            "reaction_label": level,
            "market_interpretation": _interpretation(level, values, trigger_window, trigger_value, bias, pricing_status),
            "price_status": price_status,
            "price_time_5m_utc": _time(matched["5m"]),
            "price_5m": price_5m,
            "pct_5m": values["pct_5m"],
            "price_time_15m_utc": _time(matched["15m"]),
            "price_15m": price_15m,
            "pct_15m": values["pct_15m"],
            "price_time_30m_utc": _time(matched["30m"]),
            "price_30m": price_30m,
            "pct_30m": values["pct_30m"],
            "price_time_60m_utc": _time(matched["60m"]),
            "price_60m": price_60m,
            "pct_60m": values["pct_60m"],
            "pre30_time_utc": _time(matched["pre30"]),
            "pre30_close": pre30_close,
            "pre30_to_post30_pct": values["pre30_to_post30_pct"],
            "price_reaction_level": level,
            "trigger_window": f"{trigger_window} {_pct(trigger_value)}" if trigger_window else "",
            "importance_reason": importance_reason or "",
            "oil_relevance_reason": oil_reason,
            "market_bias": bias,
            "market_pricing_status": pricing_status,
            "price_missing_reason": price_missing_reason,
            "exclusion_reason": exclusion_reason,
            "note": price_missing_reason,
        }
        store.save_reaction(news_id, reaction, is_in_daily=is_in_daily, status=status)
        stats["analyzed"] += 1

    return stats


def analyze_pending_reactions(
    store: TrackerStore,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    logger: logging.Logger | None = None,
) -> dict[str, int]:
    ready_cutoff = utc_now() - timedelta(minutes=tracker_config.extended_reaction_window_minutes)
    rows = store.pending_news_for_analysis(ready_cutoff)
    return _analyze_rows(rows, store, brent_config, tracker_config, logger=logger, force=False)


def analyze_reactions_for_range(
    store: TrackerStore,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    start_utc: datetime,
    end_utc: datetime,
    logger: logging.Logger | None = None,
) -> dict[str, int]:
    rows = store.news_for_reaction_range(start_utc, end_utc)
    return _analyze_rows(rows, store, brent_config, tracker_config, logger=logger, force=True)
