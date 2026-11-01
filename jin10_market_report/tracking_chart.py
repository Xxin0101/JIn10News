from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .time_utils import parse_iso_utc


@dataclass(frozen=True)
class ThirtyMinuteCandle:
    start_bj: datetime
    open: float
    high: float
    low: float
    close: float
    source_rows: int


@dataclass(frozen=True)
class ChartEvent:
    event_id: str
    published_bj: datetime
    keyword: str
    bias: str
    pct_30m: float | None
    pct_60m: float | None
    kind: str
    price: float | None


@dataclass(frozen=True)
class ChartArtifact:
    path: Path
    candle_count: int
    event_count: int
    unmatched_count: int
    window_start_bj: datetime
    window_end_bj: datetime


@dataclass(frozen=True)
class ChartNewsItem:
    published_bj: datetime
    title: str
    url: str
    reaction_text: str


@dataclass(frozen=True)
class ChartNewsMarker:
    marker_id: str
    candle_start_bj: datetime
    display_time_bj: datetime
    price: float
    items: tuple[ChartNewsItem, ...]


@dataclass(frozen=True)
class UnmatchedMovement:
    start_bj: datetime
    end_bj: datetime


def _value(row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        if isinstance(row, Mapping):
            return row.get(key, default)
        return default


def _floor_half_hour(value: datetime) -> datetime:
    return value.replace(minute=0 if value.minute < 30 else 30, second=0, microsecond=0)


def aggregate_30m_klines(
    rows: Iterable,
    window_start_utc: datetime,
    window_end_utc: datetime,
    timezone_name: str = "Asia/Shanghai",
) -> list[ThirtyMinuteCandle]:
    """Aggregate the existing 5-minute cache into fixed Beijing-time 30-minute OHLC bars."""
    tz = ZoneInfo(timezone_name)
    buckets: dict[datetime, list] = {}
    for row in rows:
        ts = parse_iso_utc(str(_value(row, "ts_utc") or ""))
        if not ts or not (window_start_utc <= ts < window_end_utc):
            continue
        bucket = _floor_half_hour(ts.astimezone(tz))
        buckets.setdefault(bucket, []).append(row)

    candles: list[ThirtyMinuteCandle] = []
    for bucket, bucket_rows in sorted(buckets.items()):
        bucket_rows = sorted(bucket_rows, key=lambda item: str(_value(item, "ts_utc") or ""))
        candles.append(
            ThirtyMinuteCandle(
                start_bj=bucket,
                open=float(_value(bucket_rows[0], "open")),
                high=max(float(_value(item, "high")) for item in bucket_rows),
                low=min(float(_value(item, "low")) for item in bucket_rows),
                close=float(_value(bucket_rows[-1], "close")),
                source_rows=len(bucket_rows),
            )
        )
    return candles


def _short_keyword(title: str, event_topic: str = "") -> str:
    text = " ".join(str(title or "").replace("\n", " ").split())
    combined = f"{text} {event_topic}"
    rules = (
        (("霍尔木兹", "海峡"), "霍尔木兹通航"),
        (("官方售价",), "原油官方售价"),
        (("OSP",), "原油官方售价"),
        (("欧佩克", "需求"), "OPEC需求预期"),
        (("OPEC", "需求"), "OPEC需求预期"),
        (("库存", "原油"), "原油库存变化"),
        (("油轮", "袭击"), "油轮航运风险"),
        (("油轮", "航运"), "油轮航运风险"),
        (("LNG",), "LNG供应风险"),
        (("液化天然气",), "LNG供应风险"),
        (("制裁",), "制裁与出口"),
        (("出口", "原油"), "原油出口变化"),
        (("打击", "伊朗"), "美伊军事升级"),
        (("袭击", "伊朗"), "美伊军事升级"),
        (("爆炸",), "地区安全事件"),
        (("谈判",), "美伊谈判信号"),
    )
    for terms, label in rules:
        if all(term.lower() in combined.lower() for term in terms):
            return label
    text = re.sub(r"^[【\[].*?[】\]]", "", text).strip(" ：:，,。")
    return (text[:9] + "…") if len(text) > 9 else (text or event_topic[:9] or "重点事件")


def _bias_from_logic(logic: str) -> str:
    if str(logic).startswith("偏多"):
        return "利多"
    if str(logic).startswith("偏空"):
        return "利空"
    return "不确定"


def _reaction_metric(reaction_row, key: str):
    value = _value(reaction_row, key)
    return float(value) if value is not None else None


def _nearest_candle_price(candles: list[ThirtyMinuteCandle], published_bj: datetime) -> float | None:
    if not candles:
        return None
    bucket = _floor_half_hour(published_bj)
    exact = next((item for item in candles if item.start_bj == bucket), None)
    if exact:
        return exact.close
    nearest = min(candles, key=lambda item: abs((item.start_bj - published_bj).total_seconds()))
    if abs((nearest.start_bj - published_bj).total_seconds()) <= 45 * 60:
        return nearest.close
    return None


def _valid_original_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def _candle_reaction_text(candle: ThirtyMinuteCandle) -> str:
    change = candle.close - candle.open
    if change > 0:
        return "该时段价格上涨"
    if change < 0:
        return "该时段价格下跌"
    return "该时段价格基本持平"


def prepare_interactive_chart_markers(
    movement_review: dict[str, object],
    candles: list[ThirtyMinuteCandle],
    window_start_bj: datetime,
    window_end_bj: datetime,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[list[ChartNewsMarker], list[UnmatchedMovement]]:
    """Shape already-selected report news for display without changing selection logic."""
    tz = ZoneInfo(timezone_name)
    candle_by_start = {item.start_bj: item for item in candles}
    items_by_candle: dict[datetime, list[ChartNewsItem]] = {}
    seen_raw_ids: set[str] = set()

    for group in list(movement_review.get("groups") or []):
        if group.get("attribution_judgment") not in {"主要驱动", "次要驱动"}:
            continue
        candidates = []
        core = group.get("core_driver")
        if isinstance(core, dict):
            candidates.append(core)
        candidates.extend(item for item in list(group.get("secondary_drivers") or []) if isinstance(item, dict))
        for candidate in candidates:
            raw_id = str(candidate.get("raw_id") or "")
            if not raw_id or raw_id in seen_raw_ids:
                continue
            published = candidate.get("published_dt")
            row = candidate.get("row")
            if not isinstance(published, datetime) or row is None:
                continue
            published_bj = published.astimezone(tz)
            if not (window_start_bj <= published_bj < window_end_bj):
                continue
            candle_start = _floor_half_hour(published_bj)
            candle = candle_by_start.get(candle_start)
            if candle is None:
                continue
            title = " ".join(str(_value(row, "title") or _value(row, "body") or "").split())
            if not title:
                continue
            seen_raw_ids.add(raw_id)
            items_by_candle.setdefault(candle_start, []).append(
                ChartNewsItem(
                    published_bj=published_bj,
                    title=title,
                    url=_valid_original_url(_value(row, "url")),
                    reaction_text=_candle_reaction_text(candle),
                )
            )

    markers: list[ChartNewsMarker] = []
    for candle_start, news_items in sorted(items_by_candle.items()):
        ordered = tuple(sorted(news_items, key=lambda item: item.published_bj))
        candle = candle_by_start[candle_start]
        markers.append(
            ChartNewsMarker(
                marker_id=f"news-{candle_start.strftime('%Y%m%d-%H%M')}",
                candle_start_bj=candle_start,
                display_time_bj=ordered[0].published_bj,
                price=candle.high,
                items=ordered,
            )
        )

    unmatched: list[UnmatchedMovement] = []
    for group in list(movement_review.get("unexplained") or []):
        start = group.get("start_dt")
        end = group.get("end_dt")
        if not isinstance(start, datetime):
            continue
        start_bj = max(start.astimezone(tz), window_start_bj)
        end_bj = min(
            end.astimezone(tz) if isinstance(end, datetime) else start_bj + timedelta(minutes=30),
            window_end_bj,
        )
        if start_bj < end_bj:
            unmatched.append(UnmatchedMovement(start_bj=start_bj, end_bj=end_bj))
    return markers, unmatched


def prepare_chart_events(
    movement_review: dict[str, object],
    reaction_by_id: Mapping[str, object],
    candles: list[ThirtyMinuteCandle],
    window_start_bj: datetime,
    window_end_bj: datetime,
    timezone_name: str = "Asia/Shanghai",
) -> list[ChartEvent]:
    """Reuse the report's selected core drivers; this function performs no news selection."""
    tz = ZoneInfo(timezone_name)
    event_candidates: list[tuple[datetime, dict[str, object], dict[str, object]]] = []
    for group in list(movement_review.get("groups") or []):
        core = group.get("core_driver")
        if not isinstance(core, dict):
            continue
        if group.get("attribution_judgment") not in {"主要驱动", "次要驱动"}:
            continue
        published = core.get("published_dt")
        if not isinstance(published, datetime):
            continue
        published_bj = published.astimezone(tz)
        if not (window_start_bj <= published_bj < window_end_bj):
            continue
        event_candidates.append((published_bj, group, core))

    # The report already chooses one core driver per movement group. If two groups
    # point to the same 30-minute candle, keep the stronger, more direct core item.
    best_by_bucket: dict[datetime, tuple[datetime, dict[str, object], dict[str, object]]] = {}
    for candidate in event_candidates:
        published_bj, group, core = candidate
        bucket = _floor_half_hour(published_bj)
        priority = (
            1 if group.get("attribution_judgment") == "主要驱动" else 0,
            float(core.get("score") or 0),
            abs(float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0)),
        )
        existing = best_by_bucket.get(bucket)
        if existing is None:
            best_by_bucket[bucket] = candidate
            continue
        existing_priority = (
            1 if existing[1].get("attribution_judgment") == "主要驱动" else 0,
            float(existing[2].get("score") or 0),
            abs(float(existing[1].get("cumulative_pct") or existing[1].get("max_30m_pct") or 0)),
        )
        if priority > existing_priority:
            best_by_bucket[bucket] = candidate

    prepared: list[ChartEvent] = []
    for index, (published_bj, group, core) in enumerate(sorted(best_by_bucket.values(), key=lambda item: item[0]), 1):
        event_id = f"E{index:02d}"
        group["chart_event_id"] = event_id
        row = core.get("row")
        raw_id = str(core.get("raw_id") or "")
        reaction = reaction_by_id.get(raw_id)
        title = str(_value(row, "title") or _value(row, "body") or "")
        prepared.append(
            ChartEvent(
                event_id=event_id,
                published_bj=published_bj,
                keyword=_short_keyword(title, str(core.get("event_topic") or "")),
                bias=_bias_from_logic(str(core.get("logic") or "")),
                pct_30m=_reaction_metric(reaction, "pct_30m"),
                pct_60m=_reaction_metric(reaction, "pct_60m"),
                kind="news",
                price=_nearest_candle_price(candles, published_bj),
            )
        )

    unmatched_groups = []
    for group in list(movement_review.get("unexplained") or []):
        start = group.get("start_dt")
        if not isinstance(start, datetime):
            continue
        start_bj = start.astimezone(tz)
        if window_start_bj <= start_bj < window_end_bj:
            unmatched_groups.append((start_bj, group))
    for index, (start_bj, group) in enumerate(sorted(unmatched_groups, key=lambda item: item[0]), 1):
        event_id = f"U{index:02d}"
        group["chart_event_id"] = event_id
        prepared.append(
            ChartEvent(
                event_id=event_id,
                published_bj=start_bj,
                keyword="未匹配到直接新闻",
                bias="不确定",
                pct_30m=float(group["pct_30m"]) if group.get("pct_30m") is not None else None,
                pct_60m=float(group["pct_60m"]) if group.get("pct_60m") is not None else None,
                kind="unmatched",
                price=_nearest_candle_price(candles, start_bj),
            )
        )
    return sorted(prepared, key=lambda item: (item.published_bj, item.event_id))


def _fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:+.2f}%"


def chart_index_markdown(events: list[ChartEvent]) -> list[str]:
    lines = [
        "## 图表事件索引",
        "",
        "> E编号对应“本窗口Brent价格异动复盘”中的同一条重点新闻；U编号对应“价格异动但新闻解释不足”。E事件的30/60分钟涨跌幅沿用既有5分钟价格匹配结果，U异动从该段起点计算。",
        "",
        "| 时间（北京时间） | 事件关键词 | 新闻方向 | Brent后续30分钟 | Brent后续60分钟 | 正文对应事件编号 |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    if not events:
        lines.append("| N/A | 本窗口无可标注重点事件 | 不确定 | N/A | N/A | N/A |")
    for item in events:
        lines.append(
            f"| {item.published_bj.strftime('%m-%d %H:%M')} | {item.keyword} | {item.bias} | "
            f"{_fmt_pct(item.pct_30m)} | {_fmt_pct(item.pct_60m)} | [{item.event_id}](#event-{item.event_id.lower()}) |"
        )
    lines.append("")
    return lines


def _price_ticks(low: float, high: float, count: int = 6) -> list[float]:
    span = max(high - low, 0.01)
    raw_step = span / max(count - 1, 1)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    normalized = raw_step / magnitude
    if normalized <= 1:
        nice = 1
    elif normalized <= 2:
        nice = 2
    elif normalized <= 5:
        nice = 5
    else:
        nice = 10
    step = nice * magnitude
    first = math.floor(low / step) * step
    ticks = []
    value = first
    while value <= high + step:
        if value >= low - step * 0.1:
            ticks.append(value)
        value += step
    return ticks


def _event_palette(event: ChartEvent) -> tuple[str, str, str]:
    if event.kind == "unmatched":
        return "#475569", "#F1F5F9", "×"
    if event.bias == "利多":
        return "#B45309", "#FFF7ED", "▲"
    if event.bias == "利空":
        return "#0369A1", "#F0F9FF", "▼"
    return "#6D28D9", "#F5F3FF", "◆"


def render_brent_24h_svg(
    candles: list[ThirtyMinuteCandle],
    events: list[ChartNewsMarker],
    window_start_bj: datetime,
    window_end_bj: datetime,
    output_path: Path,
    source_label: str,
    unmatched_movements: list[UnmatchedMovement] | None = None,
) -> ChartArtifact | None:
    if len(candles) < 2:
        return None

    unmatched_movements = unmatched_movements or []
    width, height = 1440, 650
    plot_left, plot_right = 92.0, 1360.0
    plot_top, plot_bottom = 118.0, 550.0
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top
    prices = [price for candle in candles for price in (candle.low, candle.high)]
    raw_low, raw_high = min(prices), max(prices)
    padding = max((raw_high - raw_low) * 0.08, 0.08)
    price_low, price_high = raw_low - padding, raw_high + padding
    total_seconds = max((window_end_bj - window_start_bj).total_seconds(), 1)
    window_hours = max(1, int(round(total_seconds / 3600)))
    tick_hours = max(2, int(math.ceil((window_hours / 12) / 2) * 2))

    def x_for(value: datetime) -> float:
        return plot_left + ((value - window_start_bj).total_seconds() / total_seconds) * plot_width

    def y_for(price: float) -> float:
        return plot_bottom - ((price - price_low) / (price_high - price_low)) * plot_height

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="chart-title chart-desc">',
        "<style>",
        "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;fill:#172033}",
        ".mono{font-family:'SFMono-Regular',Consolas,'Liberation Mono',monospace}",
        ".axis{font-size:12px;fill:#64748B}.small{font-size:11px;fill:#64748B}.label{font-size:12px;font-weight:700}.title{font-size:22px;font-weight:750}.subtitle{font-size:12px;fill:#475569}",
        ".news-marker{cursor:pointer;outline:none}.news-marker .marker-tag{transition:fill .15s,stroke .15s}.news-marker:hover .marker-tag,.news-marker:focus .marker-tag,.news-marker.is-active .marker-tag{fill:#DBEAFE;stroke:#1D4ED8}.news-marker:hover .marker-dot,.news-marker:focus .marker-dot,.news-marker.is-active .marker-dot{r:7}",
        "</style>",
        '<rect width="1440" height="650" rx="16" fill="#FFFFFF"/>',
        '<rect x="1" y="1" width="1438" height="648" rx="15" fill="none" stroke="#D8E1EA"/>',
        f'<text id="chart-title" x="92" y="34" class="title">Brent {window_hours}小时价格走势</text>',
        f'<text id="chart-desc" x="92" y="57" class="subtitle">{html.escape(window_start_bj.strftime("%Y-%m-%d %H:%M"))} 至 {html.escape(window_end_bj.strftime("%Y-%m-%d %H:%M"))}（北京时间）｜30分钟K线｜{len(candles)}根有效K线</text>',
        '<rect x="92" y="75" width="12" height="12" fill="#C2413B"/><text x="111" y="85" class="small">上涨</text>',
        '<rect x="165" y="75" width="12" height="12" fill="#1B7F5A"/><text x="184" y="85" class="small">下跌</text>',
        '<circle cx="246" cy="81" r="5" fill="#2563EB"/><text x="258" y="85" class="small">可点击新闻时间</text>',
        '<rect x="372" y="74" width="15" height="13" rx="2" fill="#CBD5E1" opacity="0.55"/><text x="395" y="85" class="small">暂无直接新闻解释</text>',
        '<rect x="92" y="118" width="1268" height="432" fill="#FBFCFE" stroke="#E2E8F0"/>',
    ]

    for movement in unmatched_movements:
        start_x = max(plot_left, min(plot_right, x_for(movement.start_bj)))
        end_x = max(plot_left, min(plot_right, x_for(movement.end_bj)))
        band_width = max(end_x - start_x, 8.0)
        label = f"{movement.start_bj.strftime('%H:%M')}–{movement.end_bj.strftime('%H:%M')} 暂无直接新闻解释"
        parts.append(
            f'<rect class="unmatched-band" x="{start_x:.1f}" y="{plot_top:.1f}" width="{band_width:.1f}" height="{plot_height:.1f}" fill="#94A3B8" opacity="0.11"><title>{html.escape(label)}</title></rect>'
        )

    for tick in _price_ticks(price_low, price_high):
        y = y_for(tick)
        if not (plot_top <= y <= plot_bottom):
            continue
        parts.append(f'<line x1="{plot_left:.1f}" y1="{y:.1f}" x2="{plot_right:.1f}" y2="{y:.1f}" stroke="#E5EAF0" stroke-width="1"/>')
        parts.append(f'<text x="{plot_left - 10:.1f}" y="{y + 4:.1f}" text-anchor="end" class="axis mono">{tick:.2f}</text>')

    tick_time = window_start_bj
    while tick_time <= window_end_bj:
        x = x_for(tick_time)
        parts.append(f'<line x1="{x:.1f}" y1="{plot_top:.1f}" x2="{x:.1f}" y2="{plot_bottom:.1f}" stroke="#EDF1F5" stroke-width="1"/>')
        label = tick_time.strftime("%m-%d %H:%M") if tick_time.hour in {0, 8} else tick_time.strftime("%H:%M")
        anchor = "start" if tick_time == window_start_bj else "end" if tick_time == window_end_bj else "middle"
        parts.append(f'<text x="{x:.1f}" y="{plot_bottom + 24:.1f}" text-anchor="{anchor}" class="axis mono">{label}</text>')
        tick_time += timedelta(hours=tick_hours)

    candle_width = max(5.0, (30 * 60 / total_seconds) * plot_width * 0.68)
    for candle in candles:
        center = x_for(candle.start_bj + timedelta(minutes=15))
        color = "#C2413B" if candle.close >= candle.open else "#1B7F5A"
        high_y, low_y = y_for(candle.high), y_for(candle.low)
        open_y, close_y = y_for(candle.open), y_for(candle.close)
        body_y = min(open_y, close_y)
        body_height = max(abs(close_y - open_y), 1.5)
        tooltip = (
            f"{candle.start_bj.strftime('%Y-%m-%d %H:%M')} 北京时间 | "
            f"O {candle.open:.2f} H {candle.high:.2f} L {candle.low:.2f} C {candle.close:.2f} | "
            f"{candle.source_rows}根5分钟K线"
        )
        parts.append(f'<g><title>{html.escape(tooltip)}</title><line x1="{center:.1f}" y1="{high_y:.1f}" x2="{center:.1f}" y2="{low_y:.1f}" stroke="{color}" stroke-width="1.4"/>')
        parts.append(f'<rect x="{center - candle_width / 2:.1f}" y="{body_y:.1f}" width="{candle_width:.1f}" height="{body_height:.1f}" fill="{color}" stroke="{color}" rx="0.8"/></g>')

    latest = candles[-1]
    latest_text = f"最新30分钟  O {latest.open:.2f}  H {latest.high:.2f}  L {latest.low:.2f}  C {latest.close:.2f}"
    parts.append('<rect x="1025" y="69" width="335" height="25" rx="6" fill="#F8FAFC" stroke="#D8E1EA"/>')
    parts.append(f'<text x="1192.5" y="86" text-anchor="middle" class="small mono">{html.escape(latest_text)}</text>')

    placed_tags: list[tuple[float, float, float, float]] = []
    for event in events:
        x = x_for(event.display_time_bj)
        dot_y = max(plot_top + 8.0, y_for(event.price) - 8.0)
        label = event.display_time_bj.strftime("%H:%M")
        if len(event.items) > 1:
            label += f" · {len(event.items)}条"
        box_width = 88.0 if len(event.items) > 1 else 62.0
        box_height = 24.0
        box_x = min(max(x - box_width / 2, plot_left + 2), plot_right - box_width - 2)
        candidates = [dot_y - 34, dot_y + 14, dot_y - 64, dot_y + 44]

        def overlaps(candidate_y: float) -> bool:
            return any(
                box_x < right + 5 and box_x + box_width > left - 5 and candidate_y < bottom + 5 and candidate_y + box_height > top - 5
                for left, top, right, bottom in placed_tags
            )

        box_y = next(
            (
                max(plot_top + 3, min(plot_bottom - box_height - 3, candidate_y))
                for candidate_y in candidates
                if not overlaps(max(plot_top + 3, min(plot_bottom - box_height - 3, candidate_y)))
            ),
            max(plot_top + 3, min(plot_bottom - box_height - 3, candidates[0])),
        )
        placed_tags.append((box_x, box_y, box_x + box_width, box_y + box_height))
        payload = json.dumps(
            [
                {
                    "time": item.published_bj.strftime("%Y-%m-%d %H:%M"),
                    "title": item.title,
                    "url": item.url,
                    "reaction": item.reaction_text,
                }
                for item in event.items
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        aria = f"{label}，{len(event.items)}条相关新闻"
        parts.append(
            f'<g id="{html.escape(event.marker_id, quote=True)}" class="news-marker" role="button" tabindex="0" focusable="true" aria-label="{html.escape(aria, quote=True)}" data-news="{html.escape(payload, quote=True)}">'
        )
        parts.append(f'<circle class="marker-dot" cx="{x:.1f}" cy="{dot_y:.1f}" r="5.5" fill="#2563EB" stroke="#FFFFFF" stroke-width="2"><title>{html.escape(aria)}</title></circle>')
        parts.append(f'<rect class="marker-tag" x="{box_x:.1f}" y="{box_y:.1f}" width="{box_width:.1f}" height="{box_height:.1f}" rx="12" fill="#EFF6FF" stroke="#93C5FD"/>')
        parts.append(f'<text x="{box_x + box_width / 2:.1f}" y="{box_y + 16:.1f}" text-anchor="middle" class="label mono" fill="#1D4ED8">{html.escape(label)}</text>')
        parts.append("</g>")

    parts.extend(
        [
            '<text x="92" y="622" class="small">新闻时间点仅来自既有匹配结果；浅灰区域不作新闻归因。K线空缺时段留白。</text>',
            f'<text x="1360" y="622" text-anchor="end" class="small">价格源：{html.escape(source_label)}</text>',
            "</svg>",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")
    return ChartArtifact(
        path=output_path,
        candle_count=len(candles),
        event_count=len(events),
        unmatched_count=len(unmatched_movements),
        window_start_bj=window_start_bj,
        window_end_bj=window_end_bj,
    )
