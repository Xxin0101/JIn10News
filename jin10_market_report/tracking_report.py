from __future__ import annotations

import html
import json
import re
import socket
import time as time_module
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .brent import BRENT_INSTRUMENT_METADATA, build_yahoo_chart_url
from .config import BrentConfig, DEFAULT_HEADERS, JIN10_FLASH_API, YAHOO_CHART_API
from .eventing import (
    direct_energy_supply_reason,
    has_direct_oil_market_relevance,
    identify_event,
    major_topic_reason,
    market_bias_for_event,
)
from .external_update import ISW_LISTING_URL, fetch_latest_isw_update
from .storage import TrackerStore
from .trump_update import fetch_trump_statements
from .tracking_chart import (
    aggregate_30m_klines,
    prepare_interactive_chart_markers,
    render_brent_24h_svg,
)
from .time_utils import ensure_utc, iso_utc, parse_iso_utc, to_beijing, utc_now


ROLLING_CUTOFF = time(8, 0)
RETRY_WAIT_SECONDS = (5, 15, 30)
PRICE_CONTINUITY_REVIEW_THRESHOLD = 0.05
PENDING_REACTION_NOTE = "新闻已纳入，但截至08:00后续60分钟价格观察尚未完成，暂不做最终归因。"
UNCONFIRMED_REACTION_NOTE = "已有部分价格数据，但暂时无法确认价格反应。"
ISW_FAILURE_COPY = "本次ISW抓取失败，以下内容待下次运行补充。"


@dataclass(frozen=True)
class FormalReportPresentation:
    panel_groups: tuple[dict[str, object], ...]
    unmatched_major_items: tuple[dict[str, object], ...]
    iran_update_items: tuple[dict[str, str], ...]
    top_summary_items: tuple[str, ...] = ()
    trump_items: tuple[dict[str, object], ...] = ()
    all_day_items: tuple[dict[str, object], ...] = ()
    display_summary: Optional[dict[str, object]] = None
    event_groups: tuple[dict[str, object], ...] = ()
    homepage_representations: Optional[dict[str, str]] = None


@dataclass(frozen=True)
class ReportBuildResult:
    markdown_path: Path
    presentation: FormalReportPresentation


def beijing_day_bounds(day: datetime | None = None, timezone_name: str = "Asia/Shanghai") -> tuple[datetime, datetime, str]:
    """Return the fixed 08:00 rolling report window.

    The target date is the report cutoff date. For example, 2026-07-02 returns
    2026-07-01 08:00 <= news < 2026-07-02 08:00 in Asia/Shanghai.
    """
    tz = ZoneInfo(timezone_name)
    if day is None:
        now_local = datetime.now(tz)
        local_end = datetime.combine(now_local.date(), ROLLING_CUTOFF, tzinfo=tz)
        if now_local < local_end:
            local_end -= timedelta(days=1)
    else:
        local_day = day.astimezone(tz) if day.tzinfo else day.replace(tzinfo=tz)
        local_end = datetime.combine(local_day.date(), ROLLING_CUTOFF, tzinfo=tz)
    local_start = local_end - timedelta(days=1)
    return local_start.astimezone(ZoneInfo("UTC")), local_end.astimezone(ZoneInfo("UTC")), local_end.strftime("%Y%m%d_0800")


def _format_bj(value: datetime | None, timezone_name: str = "Asia/Shanghai", with_seconds: bool = False) -> str:
    if not value:
        return "N/A"
    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return to_beijing(value, timezone_name).strftime(fmt)


def _source_window_label(start_utc: datetime, end_utc: datetime, timezone_name: str) -> str:
    start_local = to_beijing(start_utc, timezone_name)
    end_local = to_beijing(end_utc, timezone_name)
    return f"{start_local.strftime('%Y-%m-%d %H:%M')} 至 {end_local.strftime('%Y-%m-%d %H:%M')} 北京时间"


def _top_level_warning(
    day_end_utc: datetime,
    news_failed: bool,
    price_failed: bool,
    audit: Mapping[str, object] | None = None,
) -> str | None:
    if news_failed and price_failed:
        day_label = to_beijing(day_end_utc, "Asia/Shanghai").strftime("%Y-%m-%d")
        return (
            "本次金十新闻与Brent行情外部更新失败。当前内容仅基于本地历史缓存，不构成"
            f"{day_label} 08:00窗口的完整市场复盘。夜间新闻覆盖、价格异动归因和市场主线均不可作为正式结论使用。"
        )
    if news_failed:
        return "本次金十新闻更新或窗口完整性校验未通过。当前内容仅基于已抓取数据与本地缓存，不能作为完整窗口复盘使用。"
    if price_failed:
        price_issues: list[str] = []
        if audit:
            if not audit.get("price_boundary_covered", False):
                price_issues.append("统计窗口价格首尾未完整覆盖")
            if int(audit.get("material_price_gap_count") or 0):
                price_issues.append("统计窗口内K线存在较大缺口")
            if int(audit.get("duplicate_timestamp_count") or 0):
                price_issues.append("统计窗口内K线存在重复时间戳")
            if not audit.get("timestamps_ordered", True):
                price_issues.append("统计窗口内K线时间戳乱序")
        issue_text = "、".join(price_issues) or "行情抓取或完整性校验未通过"
        return (
            f"本次Brent行情未达到正式日报门槛（{issue_text}）。"
            "价格表现与短线归因仅供缓存复盘参考。"
        )
    return None


def _report_title(report_status: str) -> str:
    if report_status == "FAILED":
        return "金十-Brent数据更新失败说明"
    if report_status == "PARTIAL":
        return "金十-Brent滚动日报（缓存复盘版）"
    return "金十新闻事件 + Brent市场反应滚动日报"


def _audit_title(report_status: str) -> str:
    if report_status == "FAILED":
        return "金十-Brent滚动日报审计报告（FAILED）"
    if report_status == "PARTIAL":
        return "金十-Brent滚动日报审计报告（PARTIAL）"
    return "金十-Brent滚动日报审计报告"


def _availability_label(report_status: str) -> str:
    if report_status == "FORMAL":
        return "可作为完整复盘"
    if report_status == "PARTIAL":
        return "仅供缓存参考"
    return "不可用"


def classify_report_status(
    audit: dict[str, object],
    source_checks: list[dict[str, object]] | None = None,
) -> tuple[str, dict[str, str]]:
    source_checks = source_checks or []
    source_status = {str(item.get("source") or ""): item for item in source_checks}

    news_ok = bool(
        (source_status.get("金十新闻") or source_status.get("金十新闻接口") or {}).get("ok", False)
    )
    price_ok = bool(
        (source_status.get("Brent行情") or source_status.get("Yahoo Finance Brent") or {}).get("ok", False)
    )
    news_fresh = str(audit.get("news_freshness") or "failed") == "passed"
    price_fresh = str(audit.get("price_freshness") or "failed") == "passed"
    data_integrity = str(audit.get("data_integrity") or "不完整")

    if source_checks:
        news_update_status = "成功" if news_ok and news_fresh else "失败"
        price_update_status = "成功" if price_ok and price_fresh else "失败"
    else:
        news_update_status = "成功" if news_fresh else "失败"
        price_update_status = "成功" if price_fresh else "失败"

    if news_update_status == "成功" and price_update_status == "成功" and data_integrity == "完整":
        report_status = "FORMAL"
    elif news_update_status == "失败" and price_update_status == "失败" and data_integrity == "不完整":
        report_status = "FAILED"
    else:
        report_status = "PARTIAL"

    summary = {
        "script_execution_status": "成功",
        "news_update_status": news_update_status,
        "price_update_status": price_update_status,
        "data_integrity_status": data_integrity,
        "formal_report_availability": _availability_label(report_status),
        "report_status": report_status,
        "manual_rerun_required": "是" if report_status != "FORMAL" else "否",
    }
    return report_status, summary


def _runtime_summary_lines(
    runtime_summary: dict[str, object] | None,
    source_checks: list[dict[str, object]] | None,
    timezone_name: str,
) -> list[str]:
    if not runtime_summary and not source_checks:
        return []

    lines = ["## 自动化运行摘要", ""]
    if runtime_summary:
        lines.extend(
            [
                f"- 脚本执行状态：{runtime_summary.get('script_execution_status') or '未知'}",
                f"- 金十新闻更新状态：{runtime_summary.get('news_update_status') or '未知'}",
                f"- Brent价格更新状态：{runtime_summary.get('price_update_status') or '未知'}",
                f"- 数据完整性状态：{runtime_summary.get('data_integrity_status') or '未知'}",
                f"- 正式日报可用性：{runtime_summary.get('formal_report_availability') or '未知'}",
                f"- 是否需要人工重跑：{runtime_summary.get('manual_rerun_required') or '未知'}",
            ]
        )
        log_path = runtime_summary.get("log_path")
        if log_path:
            lines.append(f"- 对应日志路径：{log_path}")
        window_label = runtime_summary.get("affected_window")
        if window_label:
            lines.append(f"- 受影响日报窗口：{window_label}")
    if source_checks:
        lines.append("- 数据源预检：")
        for item in source_checks:
            last_success = item.get("last_success_at_utc")
            last_success_text = (
                to_beijing(last_success, timezone_name).strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(last_success, datetime)
                else "N/A"
            )
            lines.append(
                f"  - {item.get('source') or '未知来源'}：{('成功' if item.get('ok') else '失败')} | "
                f"阶段：{item.get('stage') or '未知'} | 错误：{item.get('error') or '无'} | "
                f"重试次数：{item.get('retries', 0)} | 最后一次成功抓取时间：{last_success_text} | "
                f"受影响日报窗口：{item.get('affected_window') or 'N/A'}"
            )
    lines.append("")
    return lines


@dataclass
class SourcePrecheckResult:
    source: str
    ok: bool
    stage: str
    error: str
    retries: int
    last_success_at_utc: datetime | None
    affected_window_start_utc: datetime
    affected_window_end_utc: datetime
    endpoint: str


def _endpoint_probe(
    *,
    source: str,
    endpoint: str,
    timeout: int,
    retries: int,
    last_success_at_utc: datetime | None,
    affected_window_start_utc: datetime,
    affected_window_end_utc: datetime,
    headers: dict[str, str] | None = None,
    validator=None,
) -> SourcePrecheckResult:
    retry_waits = RETRY_WAIT_SECONDS
    parsed = urllib.parse.urlparse(endpoint)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    last_error = ""
    last_stage = "DNS"

    for attempt in range(1, retries + 2):
        try:
            if not host:
                raise ValueError("endpoint缺少host")
            socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            last_stage = "网络连接"
            with socket.create_connection((host, port), timeout=timeout):
                pass

            last_stage = "HTTP状态"
            req = urllib.request.Request(endpoint, headers=headers or DEFAULT_HEADERS, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", resp.getcode())
                if int(status or 0) >= 400:
                    raise RuntimeError(f"HTTP {status}")
                body = resp.read().decode("utf-8")

            last_stage = "数据解析"
            payload = json.loads(body)
            if validator:
                validator(payload)
            return SourcePrecheckResult(
                source=source,
                ok=True,
                stage="ok",
                error="",
                retries=attempt - 1,
                last_success_at_utc=last_success_at_utc,
                affected_window_start_utc=affected_window_start_utc,
                affected_window_end_utc=affected_window_end_utc,
                endpoint=endpoint,
            )
        except socket.gaierror as exc:
            last_stage = "DNS"
            last_error = str(exc)
        except urllib.error.HTTPError as exc:
            last_stage = "HTTP状态"
            last_error = f"HTTP {exc.code}: {exc.reason}"
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.gaierror):
                last_stage = "DNS"
            else:
                last_stage = "网络连接"
            last_error = str(reason)
        except (TimeoutError, OSError, RuntimeError) as exc:
            if isinstance(exc, TimeoutError):
                last_stage = "网络连接"
            last_error = str(exc)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            last_stage = "数据解析"
            last_error = str(exc)

        if attempt > retries:
            break
        time_module.sleep(RETRY_WAIT_SECONDS[min(attempt - 1, len(RETRY_WAIT_SECONDS) - 1)])

    return SourcePrecheckResult(
        source=source,
        ok=False,
        stage=last_stage,
        error=last_error or "未知错误",
        retries=retries,
        last_success_at_utc=last_success_at_utc,
        affected_window_start_utc=affected_window_start_utc,
        affected_window_end_utc=affected_window_end_utc,
        endpoint=endpoint,
    )


def _validate_jin10_probe(payload: dict[str, object]) -> None:
    if payload.get("status") != 200 or "data" not in payload:
        raise ValueError(f"接口返回异常: status={payload.get('status')} message={payload.get('message')!r}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("接口 data 字段不是列表")


def _validate_yahoo_probe(payload: dict[str, object]) -> None:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ValueError(f"Brent价格接口错误: {chart['error']}")
    result = chart.get("result") or []
    if not result:
        raise ValueError("Brent价格接口未返回K线数据")
    data = result[0]
    timestamps = data.get("timestamp") or []
    quote = (data.get("indicators") or {}).get("quote") or []
    if not timestamps or not quote:
        raise ValueError("Brent价格接口K线字段缺失")


def rolling_price_bounds(
    report_start_utc: datetime,
    report_end_utc: datetime,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_name)
    local_start = report_start_utc.astimezone(tz) - timedelta(minutes=30)
    local_end = report_end_utc.astimezone(tz) + timedelta(minutes=65)
    return local_start.astimezone(ZoneInfo("UTC")), local_end.astimezone(ZoneInfo("UTC"))


def _fmt_price(value) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.2f}"


def _fmt_pct(value) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100:+.2f}%"


def _val(row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _bj_hhmm(value: str, timezone_name: str = "Asia/Shanghai") -> str:
    parsed = parse_iso_utc(value)
    if not parsed:
        return "N/A"
    return to_beijing(parsed, timezone_name).strftime("%H:%M")


def _bj_full(value: str, timezone_name: str = "Asia/Shanghai") -> str:
    parsed = parse_iso_utc(value)
    if not parsed:
        return "N/A"
    return to_beijing(parsed, timezone_name).strftime("%Y-%m-%d %H:%M")


def brent_day_summary(store: TrackerStore, brent: BrentConfig, day_start_utc: datetime, day_end_utc: datetime) -> dict[str, str]:
    rows = store.brent_day_klines(brent.symbol, brent.interval, day_start_utc, day_end_utc)
    if not rows:
        return {
            "first": "价格数据暂不可用",
            "latest": "价格数据暂不可用",
            "high": "价格数据暂不可用",
            "low": "价格数据暂不可用",
            "pct": "价格数据暂不可用",
        }
    first = float(rows[0]["open"])
    latest = float(rows[-1]["close"])
    high = max(float(row["high"]) for row in rows)
    low = min(float(row["low"]) for row in rows)
    pct = (latest - first) / first if first else 0.0
    return {
        "first": _fmt_price(first),
        "latest": _fmt_price(latest),
        "high": _fmt_price(high),
        "low": _fmt_price(low),
        "pct": _fmt_pct(pct),
    }


def _raw_text(row) -> str:
    return f"{_val(row, 'title', '')} {_val(row, 'body', '')} {_val(row, 'detail', '')}"


def _raw_bj(row, timezone_name: str = "Asia/Shanghai") -> str:
    return _bj_full(str(row["published_at_utc"]), timezone_name)


def _raw_oil_status(row) -> tuple[bool, str, str]:
    title = str(row["title"])
    body = str(row["body"] or row["detail"] or "")
    direct, oil_reason = has_direct_oil_market_relevance(title, body)
    major = major_topic_reason(title, body) if direct else None
    return direct, oil_reason, major or ""


def _segment_counts(raw_rows: list, start_utc: datetime, end_utc: datetime, timezone_name: str) -> dict[str, int]:
    tz = ZoneInfo(timezone_name)
    start_local = start_utc.astimezone(tz)
    end_local = end_utc.astimezone(tz)
    mid16 = datetime.combine(start_local.date(), time(16, 0), tzinfo=tz)
    midnight = datetime.combine(end_local.date(), time.min, tzinfo=tz)
    segments = {
        "08:00-16:00": (start_local, mid16),
        "16:00-00:00": (mid16, midnight),
        "00:00-08:00": (midnight, end_local),
    }
    counts = dict.fromkeys(segments.keys(), 0)
    for row in raw_rows:
        published = parse_iso_utc(row["published_at_utc"])
        if not published:
            continue
        local = published.astimezone(tz)
        for label, (seg_start, seg_end) in segments.items():
            if seg_start <= local < seg_end:
                counts[label] += 1
                break
    return counts


def _scan_price_anomalies(store: TrackerStore, brent: BrentConfig, start_utc: datetime, end_utc: datetime) -> list[dict[str, object]]:
    rows = store.brent_day_klines(brent.symbol, brent.interval, start_utc, end_utc)
    if len(rows) < 2:
        return []
    windows = [(15, 3, 0.0025), (30, 6, 0.0035), (60, 12, 0.0050)]
    candidates: list[dict[str, object]] = []
    for minutes, steps, threshold in windows:
        for idx in range(0, max(0, len(rows) - steps)):
            start = rows[idx]
            end = rows[idx + steps]
            start_close = float(start["close"])
            end_close = float(end["close"])
            if not start_close:
                continue
            pct = (end_close - start_close) / start_close
            if abs(pct) >= threshold:
                candidates.append(
                    {
                        "kind": f"{minutes}分钟",
                        "start_utc": str(start["ts_utc"]),
                        "end_utc": str(end["ts_utc"]),
                        "pct": pct,
                        "start_close": start_close,
                        "end_close": end_close,
                        "triggered": True,
                    }
                )
    # Always include the largest absolute moves even if thresholds are quiet.
    for minutes, steps, _ in windows:
        moves = []
        for idx in range(0, max(0, len(rows) - steps)):
            start = rows[idx]
            end = rows[idx + steps]
            start_close = float(start["close"])
            if not start_close:
                continue
            pct = (float(end["close"]) - start_close) / start_close
            moves.append((abs(pct), pct, start, end, minutes))
        for _, pct, start, end, minutes in sorted(moves, key=lambda item: item[0], reverse=True)[:3]:
            candidates.append(
                {
                    "kind": f"{minutes}分钟",
                    "start_utc": str(start["ts_utc"]),
                    "end_utc": str(end["ts_utc"]),
                    "pct": pct,
                    "start_close": float(start["close"]),
                    "end_close": float(end["close"]),
                    "triggered": False,
                }
            )
    unique: dict[tuple[str, str, str], dict[str, object]] = {}
    for item in candidates:
        key = (str(item["kind"]), str(item["start_utc"]), str(item["end_utc"]))
        existing = unique.get(key)
        if existing is None or bool(item["triggered"]):
            unique[key] = item
    return sorted(unique.values(), key=lambda item: abs(float(item["pct"])), reverse=True)[:3]


def _price_continuity_issues(price_rows: Iterable) -> list[dict[str, object]]:
    timed_rows = [(row, _price_ts(row)) for row in price_rows]
    timed_rows = [(row, ts) for row, ts in timed_rows if ts is not None]
    issues: list[dict[str, object]] = []
    for (previous, previous_ts), (current, current_ts) in zip(timed_rows, timed_rows[1:]):
        gap = current_ts - previous_ts
        previous_close = float(previous["close"])
        current_close = float(current["close"])
        if previous_close == 0 or gap > timedelta(minutes=30):
            continue
        pct = (current_close - previous_close) / previous_close
        if abs(pct) < PRICE_CONTINUITY_REVIEW_THRESHOLD:
            continue
        issues.append(
            {
                "start_utc": previous_ts,
                "end_utc": current_ts,
                "start_close": previous_close,
                "end_close": current_close,
                "pct": pct,
                "gap_minutes": int(gap.total_seconds() // 60),
            }
        )
    return issues


def _is_expected_brent_maintenance_gap(
    previous_utc: datetime,
    current_utc: datetime,
    timezone_name: str,
) -> bool:
    """Return whether a gap matches the regular daily Brent maintenance break."""
    previous_local = previous_utc.astimezone(ZoneInfo(timezone_name))
    current_local = current_utc.astimezone(ZoneInfo(timezone_name))
    gap = current_utc - previous_utc
    return (
        previous_local.date() == current_local.date()
        and time(4, 45) <= previous_local.time() <= time(5, 10)
        and time(5, 50) <= current_local.time() <= time(6, 15)
        and timedelta(minutes=45) <= gap <= timedelta(minutes=90)
    )


def _reaction_observation_status(
    published_at_utc: datetime | None,
    reaction,
    report_end_utc: datetime,
) -> tuple[str, str]:
    if published_at_utc and published_at_utc + timedelta(minutes=60) > ensure_utc(report_end_utc):
        return "PENDING", PENDING_REACTION_NOTE
    required_values = (
        _val(reaction, "event_base_close") if reaction else None,
        _val(reaction, "price_5m") if reaction else None,
        _val(reaction, "price_15m") if reaction else None,
        _val(reaction, "price_30m") if reaction else None,
        _val(reaction, "price_60m") if reaction else None,
    )
    if reaction and _val(reaction, "price_status") == "ok" and all(
        value is not None for value in required_values
    ):
        return "CONFIRMED", ""
    return "UNCONFIRMED", UNCONFIRMED_REACTION_NOTE


def _build_window_audit(store: TrackerStore, brent: BrentConfig, start_utc: datetime, end_utc: datetime, timezone_name: str) -> dict[str, object]:
    raw_rows = store.raw_news_between(start_utc, end_utc)
    first_raw = parse_iso_utc(raw_rows[0]["published_at_utc"]) if raw_rows else None
    last_raw = parse_iso_utc(raw_rows[-1]["published_at_utc"]) if raw_rows else None
    reaction_news_rows = store.news_for_reaction_range(start_utc, end_utc)
    last_reaction_news = (
        parse_iso_utc(reaction_news_rows[-1]["published_at_utc"]) if reaction_news_rows else None
    )
    price_start, price_end = start_utc, end_utc
    price_rows = store.brent_day_klines(brent.symbol, brent.interval, price_start, price_end)
    first_price = parse_iso_utc(price_rows[0]["ts_utc"]) if price_rows else None
    last_price = parse_iso_utc(price_rows[-1]["ts_utc"]) if price_rows else None
    price_timestamps = [
        timestamp
        for timestamp in (_price_ts(row) for row in price_rows)
        if timestamp is not None
    ]
    duplicate_timestamp_count = len(price_timestamps) - len(set(price_timestamps))
    timestamps_ordered = price_timestamps == sorted(price_timestamps)
    ordered_unique_timestamps = sorted(set(price_timestamps))
    price_gaps = [
        {
            "start_utc": previous,
            "end_utc": current,
            "gap_minutes": int((current - previous).total_seconds() // 60),
            "expected_maintenance": _is_expected_brent_maintenance_gap(
                previous,
                current,
                timezone_name,
            ),
        }
        for previous, current in zip(ordered_unique_timestamps, ordered_unique_timestamps[1:])
        if current - previous > timedelta(minutes=5)
    ]
    material_price_gaps = [
        gap
        for gap in price_gaps
        if int(gap["gap_minutes"]) > 10 and not bool(gap["expected_maintenance"])
    ]
    expected_maintenance_gaps = [
        gap for gap in price_gaps if bool(gap["expected_maintenance"])
    ]
    segments = _segment_counts(raw_rows, start_utc, end_utc, timezone_name)
    reaction_rows = [
        row
        for row in store.reaction_rows_between(start_utc, end_utc)
        if not int(_val(row, "is_duplicate") or 0)
    ]

    effective = []
    major_candidates = []
    summary_count = 0
    for row in raw_rows:
        if int(row["is_summary"] or 0):
            summary_count += 1
        direct, oil_reason, major_reason = _raw_oil_status(row)
        if direct:
            effective.append((row, oil_reason, major_reason))
        if major_reason:
            major_candidates.append((row, oil_reason, major_reason))

    news_coverage_gaps: list[str] = []
    for label, count in segments.items():
        if count == 0:
            news_coverage_gaps.append(f"{label} 原始新闻数量为0，需复核分页或接口覆盖。")
    if not raw_rows:
        news_coverage_gaps.append("窗口内没有原始新闻记录。")
    else:
        if first_raw and first_raw > start_utc + timedelta(minutes=30):
            news_coverage_gaps.append(
                f"原始新闻最早仅到{_bj_full(iso_utc(first_raw), timezone_name)}，可能未覆盖窗口起始附近。"
            )
        if last_raw and last_raw < end_utc - timedelta(hours=2):
            news_coverage_gaps.append(
                f"原始新闻最晚仅到{_bj_full(iso_utc(last_raw), timezone_name)}，未覆盖其后至窗口截止。"
            )
    coverage_gaps = list(news_coverage_gaps)
    price_coverage_tolerance = timedelta(minutes=10)
    price_start_covered = bool(first_price and first_price <= price_start + price_coverage_tolerance)
    price_end_covered = bool(last_price and last_price >= price_end - price_coverage_tolerance)
    price_boundary_covered = price_start_covered and price_end_covered
    price_continuity_issues = _price_continuity_issues(price_rows)
    price_continuity_ok = not price_continuity_issues
    price_timestamp_quality_ok = duplicate_timestamp_count == 0 and timestamps_ordered

    if not price_rows:
        coverage_gaps.append("Brent 5分钟K线为空。")
    else:
        if not price_start_covered:
            coverage_gaps.append(
                f"Brent统计窗口起点未覆盖：要求到{_bj_full(iso_utc(price_start), timezone_name)}附近，"
                f"实际最早为{_bj_full(iso_utc(first_price), timezone_name) if first_price else 'N/A'}。"
            )
        if not price_end_covered:
            coverage_gaps.append(
                f"Brent统计窗口截止未覆盖：要求到{_bj_full(iso_utc(price_end), timezone_name)}前10分钟内，"
                f"实际最晚为{_bj_full(iso_utc(last_price), timezone_name) if last_price else 'N/A'}。"
            )
        if duplicate_timestamp_count:
            coverage_gaps.append(
                f"Brent统计窗口内存在{duplicate_timestamp_count}个重复K线时间戳。"
            )
        if not timestamps_ordered:
            coverage_gaps.append("Brent统计窗口内K线时间戳乱序。")
        if material_price_gaps:
            gap_text = "、".join(
                f"{_bj_from_dt(gap['start_utc'], timezone_name)}至"
                f"{_bj_from_dt(gap['end_utc'], timezone_name)}（{gap['gap_minutes']}分钟）"
                for gap in material_price_gaps[:6]
            )
            coverage_gaps.append(
                f"Brent统计窗口内存在{len(material_price_gaps)}处大于10分钟的有效交易时段K线缺口：{gap_text}。"
            )

    price_fields = {
        "5m": "price_5m",
        "15m": "price_15m",
        "30m": "price_30m",
        "60m": "price_60m",
    }
    price_point_missing_counts = {
        label: sum(1 for row in reaction_rows if _val(row, field) is None)
        for label, field in price_fields.items()
    }
    reaction_statuses: list[dict[str, str]] = []
    reaction_status_counts = Counter({"CONFIRMED": 0, "PENDING": 0, "UNCONFIRMED": 0})
    for row in reaction_rows:
        published = parse_iso_utc(str(_val(row, "published_at_utc") or ""))
        reaction_status, reaction_status_note = _reaction_observation_status(
            published,
            row,
            end_utc,
        )
        reaction_status_counts[reaction_status] += 1
        reaction_statuses.append(
            {
                "news_id": str(_val(row, "news_id") or ""),
                "published_at_utc": str(_val(row, "published_at_utc") or ""),
                "reaction_status": reaction_status,
                "reaction_status_note": reaction_status_note,
            }
        )
    reaction_status_by_id = {
        item["news_id"]: item for item in reaction_statuses
    }
    price_missing_details: list[dict[str, object]] = []
    for row in reaction_rows:
        required_values = [
            _val(row, "event_base_close"),
            _val(row, "pre30_close"),
            *(_val(row, field) for field in price_fields.values()),
        ]
        if all(value is not None for value in required_values):
            continue
        missing_points = [label for label, field in price_fields.items() if _val(row, field) is None]
        if _val(row, "event_base_close") is None:
            missing_points.insert(0, "事件基准")
        if _val(row, "pre30_close") is None:
            missing_points.insert(0, "前30m")
        price_missing_details.append(
            {
                "news_id": str(_val(row, "news_id") or ""),
                "published_at_utc": str(_val(row, "published_at_utc") or ""),
                "missing_points": missing_points,
                "reason": str(_val(row, "price_missing_reason") or "未记录具体缺失原因"),
                "reaction_status": reaction_status_by_id.get(
                    str(_val(row, "news_id") or ""),
                    {},
                ).get("reaction_status", "UNCONFIRMED"),
                "reaction_status_note": reaction_status_by_id.get(
                    str(_val(row, "news_id") or ""),
                    {},
                ).get("reaction_status_note", UNCONFIRMED_REACTION_NOTE),
            }
        )

    price_missing_count = len(price_missing_details)
    missing_periods = list(coverage_gaps)
    if price_missing_count:
        missing_periods.append(
            f"逐事件价格不完整：{price_missing_count}/{len(reaction_rows)}条；"
            f"5分钟缺失{price_point_missing_counts['5m']}条，"
            f"15分钟缺失{price_point_missing_counts['15m']}条，"
            f"30分钟缺失{price_point_missing_counts['30m']}条，"
            f"60分钟缺失{price_point_missing_counts['60m']}条。"
        )

    news_covered = bool(raw_rows and not news_coverage_gaps)
    # Yahoo omits empty five-minute buckets for this thinly traded continuous
    # contract. Keep those sparse intervals in the audit and event-level N/A
    # checks, but do not downgrade the whole day when both boundaries and the
    # timestamp stream are otherwise valid.
    price_covered = price_boundary_covered and price_timestamp_quality_ok
    news_freshness = "passed" if news_covered and raw_rows else "failed"
    price_freshness = "passed" if price_covered and price_rows else "failed"
    if raw_rows or price_rows:
        data_integrity = "完整" if news_freshness == "passed" and price_freshness == "passed" else "部分缓存"
    else:
        data_integrity = "不完整"
    anomalies = _scan_price_anomalies(store, brent, start_utc, end_utc)
    for anomaly in anomalies:
        center = parse_iso_utc(str(anomaly["start_utc"]))
        nearby = store.raw_news_near(center or start_utc, minutes=45)
        anomaly["nearby_news"] = nearby[:8]
        direct_nearby = []
        for row in nearby:
            direct, oil_reason, major_reason = _raw_oil_status(row)
            if direct or major_reason:
                direct_nearby.append((row, oil_reason, major_reason))
        anomaly["direct_nearby"] = direct_nearby

    return {
        "raw_rows": raw_rows,
        "raw_total": len(raw_rows),
        "effective_total": len(effective),
        "summary_total": summary_count,
        "major_candidates": major_candidates,
        "first_raw": first_raw,
        "last_raw": last_raw,
        "last_reaction_news": last_reaction_news,
        "price_start_required": price_start,
        "price_end_required": price_end,
        "price_coverage_tolerance_minutes": int(price_coverage_tolerance.total_seconds() // 60),
        "segments": segments,
        "price_rows": price_rows,
        "first_price": first_price,
        "last_price": last_price,
        "duplicate_timestamp_count": duplicate_timestamp_count,
        "timestamps_ordered": timestamps_ordered,
        "price_gap_count": len(price_gaps),
        "max_price_gap_minutes": max(
            (int(gap["gap_minutes"]) for gap in price_gaps),
            default=0,
        ),
        "price_gaps": price_gaps,
        "material_price_gap_count": len(material_price_gaps),
        "max_material_price_gap_minutes": max(
            (int(gap["gap_minutes"]) for gap in material_price_gaps),
            default=0,
        ),
        "material_price_gaps": material_price_gaps,
        "expected_maintenance_gap_count": len(expected_maintenance_gaps),
        "news_covered": news_covered,
        "price_start_covered": price_start_covered,
        "price_end_covered": price_end_covered,
        "price_boundary_covered": price_boundary_covered,
        "price_continuity_ok": price_continuity_ok,
        "price_continuity_issues": price_continuity_issues,
        "price_covered": price_covered,
        "news_freshness": news_freshness,
        "price_freshness": price_freshness,
        "data_integrity": data_integrity,
        "coverage_gaps": coverage_gaps,
        "news_coverage_gaps": news_coverage_gaps,
        "missing_periods": missing_periods,
        "reaction_total": len(reaction_rows),
        "reaction_statuses": reaction_statuses,
        "reaction_status_counts": dict(reaction_status_counts),
        "price_missing_count": price_missing_count,
        "price_point_missing_counts": price_point_missing_counts,
        "price_missing_details": price_missing_details,
        "anomalies": anomalies,
        "data_complete": bool(news_freshness == "passed" and price_freshness == "passed"),
    }


def _homepage_candidate_is_eligible(candidate: dict[str, object]) -> bool:
    row = candidate["row"]
    title = str(_val(row, "title") or "").strip()
    body = str(_val(row, "body") or _val(row, "detail") or "")
    return (
        bool(candidate.get("direct_supply_path"))
        and str(candidate.get("freshness") or "") == "新增事实"
        and bool(_valid_jin10_display_url(_val(row, "url")))
        and not bool(candidate.get("is_duplicate"))
        and not _is_institution_forecast_noise(row)
        and not _is_formal_report_noise(row)
        and not bool(candidate.get("price_quote_only"))
        and not bool(candidate.get("is_commentary"))
        and bool(title)
        and bool(direct_energy_supply_reason(title, body))
    )


def _panel_group_window_start(group: dict[str, object], timezone_name: str) -> datetime:
    start_local = to_beijing(ensure_utc(group["start_dt"]), timezone_name).replace(tzinfo=None)
    window_start = datetime.combine(start_local.date(), ROLLING_CUTOFF)
    return window_start - timedelta(days=1) if start_local < window_start else window_start


def _panel_group_key(group: dict[str, object], timezone_name: str) -> str:
    start_local = to_beijing(ensure_utc(group["start_dt"]), timezone_name)
    end_local = to_beijing(ensure_utc(group["end_dt"]), timezone_name)
    return _formal_event_key(
        start_local.strftime("%H:%M"),
        end_local.strftime("%H:%M"),
        _panel_group_window_start(group, timezone_name),
    )


def _candidate_published_dt(candidate: dict[str, object]) -> datetime:
    published = candidate.get("published_dt")
    if isinstance(published, datetime):
        return ensure_utc(published)
    parsed = parse_iso_utc(str(_val(candidate["row"], "published_at_utc") or ""))
    return parsed or datetime.min.replace(tzinfo=ZoneInfo("UTC"))


def _candidate_attribution_priority(candidate: dict[str, object]) -> int:
    explicit = candidate.get("attribution_priority")
    if explicit is not None:
        return int(explicit)
    return {"主要驱动": 4, "次要驱动": 3, "背景信息": 2, "暂无法确认": 1}.get(
        str(candidate.get("attribution_judgment") or ""),
        0,
    )


_HOMEPAGE_EVENT_ACTORS = {
    "美国": "美国",
    "美方": "美国",
    "俄罗斯": "俄罗斯",
    "俄方": "俄罗斯",
    "伊朗": "伊朗",
    "以色列": "以色列",
    "沙特": "沙特",
    "也门": "也门",
    "胡塞": "胡塞武装",
    "胡塞武装": "胡塞武装",
    "阿联酋": "阿联酋",
    "卡塔尔": "卡塔尔",
    "伊拉克": "伊拉克",
    "利比亚": "利比亚",
    "土耳其": "土耳其",
    "哈萨克斯坦": "哈萨克斯坦",
    "乌克兰": "乌克兰",
    "欧盟": "欧盟",
    "英国": "英国",
    "OPEC+": "OPEC+",
    "OPEC": "OPEC+",
    "欧佩克+": "OPEC+",
    "欧佩克": "OPEC+",
    "EIA": "EIA",
    "美国能源信息署": "EIA",
    "API": "API",
}
_HOMEPAGE_EVENT_ROUTES = {
    "霍尔木兹海峡": "霍尔木兹海峡",
    "霍尔木兹": "霍尔木兹海峡",
    "曼德海峡": "曼德海峡",
    "红海": "红海",
    "苏伊士运河": "苏伊士运河",
    "黑海": "黑海",
}
_HOMEPAGE_EVENT_ENTITY_PATTERNS = (
    re.compile(r"[A-Za-z0-9+·\-\u4e00-\u9fff]{2,28}(?:国家石油公司|石油公司|能源公司|管道联盟)"),
    re.compile(r"[A-Za-z0-9+·\-\u4e00-\u9fff]{2,22}(?:港口|油码头|码头|炼油厂|炼厂|油田|输油管道|管道财团|管道|港)"),
    re.compile(r"[A-Za-z0-9+·\-]{2,22}(?:号?油轮|号?货轮|LNG船|LPG船)"),
    re.compile(r"[\u4e00-\u9fff]{2,12}号(?:油轮|货轮)"),
)
_HOMEPAGE_EVENT_ENTITY_SPLITTERS = (
    "据",
    "宣布",
    "重申",
    "确认",
    "针对",
    "制裁",
    "对",
    "称",
    "表示",
    "将",
    "已",
)
_HOMEPAGE_ONGOING_TERMS = (
    "继续",
    "仍",
    "后续",
    "最新",
    "再次",
    "扩大",
    "生效",
    "实施",
    "执行",
    "恢复",
    "重开",
    "解除",
)


def _homepage_candidate_text(candidate: Mapping[str, object]) -> str:
    row = candidate["row"]
    return re.sub(r"\s+", " ", f"{_val(row, 'title') or ''} {_val(row, 'body') or _val(row, 'detail') or ''}").strip()


def _homepage_event_action(candidate: Mapping[str, object]) -> str:
    title = str(_val(candidate["row"], "title") or "")
    if any(term in title for term in ("制裁", "禁运", "出口管制", "豁免")):
        return "sanctions"
    if any(term in title for term in ("停火", "和谈", "谈判协议")):
        return "ceasefire"
    if any(term in title for term in ("库存", "库欣")):
        return "inventory"
    if any(term in title for term in ("恢复通行", "重新开放", "不发放", "限制通行", "封锁", "关闭海峡")):
        return "route_access"
    if (
        any(term in title for term in ("停止向", "暂停向", "暂停接收", "停止接收", "暂停装船", "出口中断", "供应中断", "停产", "减产"))
        or re.search(r"(?:停止|暂停).{0,16}(?:接收|输送|装船|出口)", title)
    ):
        return "supply_disruption"
    if any(term in title for term in ("警告", "威胁")) and any(term in title for term in ("船只", "船舶", "油轮", "航运")):
        return "shipping_threat"
    if any(term in title for term in ("打击", "空袭", "导弹袭击", "军事行动")):
        return "military_strike"
    if any(term in title for term in ("油轮遇袭", "油轮遭袭", "船只遇袭", "船舶遇袭", "发生爆炸", "起火")):
        return "shipping_incident"
    return ""


def _clean_homepage_event_entity(value: str) -> str:
    cleaned = value.strip(" ，。；：、（）()[]【】'\"")
    for alias in sorted(_HOMEPAGE_EVENT_ACTORS, key=len, reverse=True):
        if cleaned.startswith(alias) and len(cleaned) > len(alias) + 1:
            cleaned = cleaned[len(alias) :]
            break
    for splitter in _HOMEPAGE_EVENT_ENTITY_SPLITTERS:
        if splitter in cleaned:
            tail = cleaned.rsplit(splitter, 1)[-1].strip()
            if len(tail) >= 2:
                cleaned = tail
    return cleaned.strip(" 的之")


def _homepage_event_identity(candidate: Mapping[str, object]) -> dict[str, object]:
    text = _homepage_candidate_text(candidate)
    title = str(_val(candidate["row"], "title") or "")
    body = str(_val(candidate["row"], "body") or _val(candidate["row"], "detail") or "")
    event_key, event_topic = identify_event(title, body)
    event_key = str(candidate.get("event_key") or event_key)
    event_topic = str(candidate.get("event_topic") or event_topic)
    actors = {
        canonical
        for alias, canonical in _HOMEPAGE_EVENT_ACTORS.items()
        if alias.lower() in text.lower()
    }
    routes = {
        canonical
        for alias, canonical in _HOMEPAGE_EVENT_ROUTES.items()
        if alias.lower() in text.lower()
    }
    title_actors = {
        canonical
        for alias, canonical in _HOMEPAGE_EVENT_ACTORS.items()
        if alias.lower() in title.lower()
    }
    title_routes = {
        canonical
        for alias, canonical in _HOMEPAGE_EVENT_ROUTES.items()
        if alias.lower() in title.lower()
    }
    entities: set[str] = set()
    for pattern in _HOMEPAGE_EVENT_ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            entity = _clean_homepage_event_entity(match.group(0))
            if len(entity) >= 2 and entity not in routes:
                entities.add(entity.lower())
    quoted = re.findall(r"[“\"]([^”\"]{2,30})[”\"]", text)
    entities.update(value.strip().lower() for value in quoted if value.strip())
    normalized = re.sub(
        r"(?:正式|已经|已|将|可能|预计|宣布|重申|表示|称|媒体|消息|市场|原油|石油|能源|新增|最新|继续|再次|实施|生效|执行|恢复|关闭|暂停|中断|受阻|制裁|出口|装船|通行|风险)",
        "",
        text.lower(),
    )
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)
    return {
        "event_key": event_key,
        "event_topic": event_topic,
        "actors": actors,
        "routes": routes,
        "title_actors": title_actors,
        "title_routes": title_routes,
        "entities": entities,
        "action": _homepage_event_action(candidate),
        "normalized": normalized,
    }


def _homepage_event_state(candidate: Mapping[str, object]) -> str:
    text = _homepage_candidate_text(candidate)
    title = str(_val(candidate["row"], "title") or "")
    event_key = str(_homepage_event_identity(candidate)["event_key"])
    if any(term in text for term in ("恢复", "重开", "重新开放", "恢复通行", "解除限制", "解除封锁", "豁免", "缓和")):
        return "recovery"
    actual_disruption_terms = (
        "实际中断",
        "发生爆炸",
        "起火",
        "遭遇袭击",
        "遭到袭击",
        "被击中",
        "遭不明抛射物击中",
        "已经关闭",
        "已关闭",
        "正式关闭",
        "暂停装船",
        "装船延误",
        "停运",
        "停产",
        "减产",
        "停航",
        "限制通行",
        "受阻",
        "遭袭",
        "受损",
        "扣留",
        "滞留",
    )
    if re.search(r"(?:将|计划|拟).{0,16}(?:停止|暂停|中断|关闭|限制|减产)", title):
        return "threat"
    if any(term in title for term in ("已暂停", "已停止", "停止接收", "暂停接收")):
        return "disruption"
    if (
        any(term in text for term in ("威胁", "讨论", "考虑", "计划", "拟", "可能", "面临", "警告", "风险"))
        and not any(term in text for term in actual_disruption_terms)
    ):
        return "threat"
    if any(
        term in text
        for term in (
            "实际中断",
            "出口中断",
            "供应中断",
            "装船中断",
            "暂停装船",
            "装船延误",
            "港口关闭",
            "已经关闭",
            "已关闭",
            "停运",
            "停产",
            "减产",
            "停航",
            "限制通行",
            "受阻",
            "遭袭",
            "爆炸",
            "起火",
            "被击中",
            "受损",
            "扣留",
            "滞留",
        )
    ):
        return "disruption"
    if any(term in text for term in ("正式生效", "开始生效", "正式实施", "开始实施", "正式执行", "已执行", "落地")):
        return "effective"
    if any(term in text for term in ("正式宣布", "宣布", "决定", "批准", "发布", "通过", "达成")):
        return "announced"
    if any(term in text for term in ("威胁", "讨论", "考虑", "计划", "拟", "可能", "面临", "警告")):
        return "threat"
    if event_key == "sanctions_policy" and any(term in text for term in ("制裁", "禁运", "出口管制")):
        return "announced"
    if re.search(r"\d", text) and any(term in text for term in ("万桶", "船只", "实体", "港口", "范围", "产量", "出口量")):
        return "scope"
    return "fact"


def _homepage_events_match(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    left_identity = _homepage_event_identity(left)
    right_identity = _homepage_event_identity(right)
    left_key = str(left_identity["event_key"])
    right_key = str(right_identity["event_key"])
    left_action = str(left_identity["action"])
    right_action = str(right_identity["action"])
    compatible_physical_keys = {"crude_oil_market", "tanker_shipping", "exports_loading", "shipping_logistics"}
    if left_key != right_key and not (
        left_action
        and left_action == right_action
        and left_key in compatible_physical_keys
        and right_key in compatible_physical_keys
    ):
        return False
    left_dt = _candidate_published_dt(dict(left))
    right_dt = _candidate_published_dt(dict(right))
    delta = abs(right_dt - left_dt)
    shared_entities = set(left_identity["entities"]) & set(right_identity["entities"])
    shared_actors = set(left_identity["actors"]) & set(right_identity["actors"])
    shared_routes = set(left_identity["routes"]) & set(right_identity["routes"])
    shared_title_actors = set(left_identity["title_actors"]) & set(right_identity["title_actors"])
    shared_title_routes = set(left_identity["title_routes"]) & set(right_identity["title_routes"])
    same_action = bool(left_action and left_action == right_action)
    left_entities = set(left_identity["entities"])
    right_entities = set(right_identity["entities"])
    if left_entities and right_entities and not shared_entities:
        return False
    left_actors = set(left_identity["actors"])
    right_actors = set(right_identity["actors"])
    if left_actors and right_actors and not shared_actors and not shared_entities:
        return False
    normalized_left = str(left_identity["normalized"])
    normalized_right = str(right_identity["normalized"])
    shorter = min(len(normalized_left), len(normalized_right))
    text_overlap = bool(
        shorter >= 8
        and (
            normalized_left in normalized_right
            or normalized_right in normalized_left
            or len(set(re.findall(r".{2}", normalized_left)) & set(re.findall(r".{2}", normalized_right)))
            / max(1, len(set(re.findall(r".{2}", normalized_left)) | set(re.findall(r".{2}", normalized_right))))
            >= 0.58
        )
    )
    has_explicit_subject = bool(
        shared_entities
        or (same_action and shared_title_actors and shared_title_routes)
        or (same_action and len(shared_title_actors) >= 2)
        or (len(shared_actors) >= 2 and text_overlap)
        or (shared_actors and shared_routes and text_overlap)
        or (
            left_identity["event_key"] in {"opec_supply", "official_selling_price"}
            and "OPEC+" in shared_actors
        )
        or (
            left_identity["event_key"] == "crude_oil_market"
            and bool(shared_actors & {"EIA", "API"})
        )
    )
    lacks_concrete_entities = not left_entities and not right_entities
    same_subject = has_explicit_subject or (lacks_concrete_entities and text_overlap)
    if not same_subject:
        return False
    if delta <= timedelta(hours=6):
        return True
    exact_continuing_subject = bool(
        shared_entities
        or (len(shared_actors) >= 2 and (shared_routes or text_overlap))
        or (shared_actors and shared_routes and text_overlap)
    )
    ongoing = any(term in _homepage_candidate_text(left) or term in _homepage_candidate_text(right) for term in _HOMEPAGE_ONGOING_TERMS)
    return exact_continuing_subject and ongoing


def _homepage_fact_completeness(candidate: Mapping[str, object]) -> int:
    text = _homepage_candidate_text(candidate)
    identity = _homepage_event_identity(candidate)
    score = min(3, len(text) // 80)
    score += 2 if identity["entities"] else 0
    score += 1 if len(identity["actors"]) >= 2 else 0
    score += 1 if re.search(r"\d", text) else 0
    score += 1 if _homepage_event_state(candidate) not in {"fact", "threat"} else 0
    return score


def _homepage_material_update(
    candidate: Mapping[str, object],
    representatives: Sequence[Mapping[str, object]],
) -> tuple[bool, str]:
    state = _homepage_event_state(candidate)
    previous_states = [_homepage_event_state(item) for item in representatives]
    identity = _homepage_event_identity(candidate)
    previous_entities = set().union(*[set(_homepage_event_identity(item)["entities"]) for item in representatives])
    new_entities = set(identity["entities"]) - previous_entities
    if state == "recovery" and "recovery" not in previous_states:
        return True, "事件出现明确恢复、重开、解除限制或缓和"
    if state == "disruption" and "disruption" not in previous_states:
        return True, "事件由风险、表态或计划升级为实际供应/运输中断"
    if state == "effective" and "effective" not in previous_states:
        return True, "正式措施开始实施或生效"
    if state == "announced" and previous_states and previous_states[-1] == "threat":
        return True, "事件由讨论或威胁升级为正式宣布"
    if state == "scope" and "scope" not in previous_states:
        return True, "首次披露具体影响范围或数量"
    if new_entities and state in {"disruption", "effective", "scope"}:
        return True, "影响范围扩大到新的主体、港口、船只或制裁对象"
    return False, "同一事件重复转述、措辞变化或仅补充背景"


def _homepage_third_update_allowed(
    candidate: Mapping[str, object],
    representatives: Sequence[Mapping[str, object]],
) -> bool:
    if not any(str(item.get("major_reason") or "") for item in [candidate, *representatives]):
        return False
    state = _homepage_event_state(candidate)
    previous_states = {_homepage_event_state(item) for item in representatives}
    if state == "recovery" and "recovery" not in previous_states:
        return True
    identity = _homepage_event_identity(candidate)
    previous_entities = set().union(*[set(_homepage_event_identity(item)["entities"]) for item in representatives])
    return state not in previous_states and bool(set(identity["entities"]) - previous_entities)


def _group_homepage_candidates(
    candidates: Iterable[dict[str, object]],
    timezone_name: str,
) -> list[dict[str, object]]:
    ordered = sorted(
        [dict(candidate) for candidate in candidates],
        key=lambda item: (_candidate_published_dt(item), str(item.get("raw_id") or "")),
    )
    grouped: list[list[dict[str, object]]] = []
    for candidate in ordered:
        target = next(
            (
                members
                for members in reversed(grouped)
                if _homepage_events_match(candidate, members[0])
            ),
            None,
        )
        if target is None:
            grouped.append([candidate])
        else:
            target.append(candidate)

    results: list[dict[str, object]] = []
    for index, members in enumerate(grouped, 1):
        members.sort(key=lambda item: (_candidate_published_dt(item), str(item.get("raw_id") or "")))
        primary = members[0]
        for candidate in members[1:]:
            if (
                _homepage_fact_completeness(primary) <= 2
                and _homepage_fact_completeness(candidate) >= _homepage_fact_completeness(primary) + 3
                and _homepage_event_state(candidate) == _homepage_event_state(primary)
            ):
                primary = candidate
                break
        representatives: list[dict[str, object]] = [primary]
        material_reasons: dict[str, str] = {}
        for candidate in members:
            if candidate is primary:
                continue
            material, reason = _homepage_material_update(candidate, representatives)
            if not material:
                continue
            if len(representatives) == 1 or (len(representatives) == 2 and _homepage_third_update_allowed(candidate, representatives)):
                representatives.append(candidate)
                material_reasons[str(candidate.get("raw_id") or "")] = reason
        representatives.sort(key=lambda item: (_candidate_published_dt(item), str(item.get("raw_id") or "")))
        representative_ids = {str(item.get("raw_id") or "") for item in representatives}
        primary_id = str(primary.get("raw_id") or "")
        identity = _homepage_event_identity(primary)
        member_records: list[dict[str, object]] = []
        for candidate in members:
            raw_id = str(candidate.get("raw_id") or "")
            if raw_id == primary_id:
                role = "primary"
                reason = "事件组代表事实（最早事实或后续更完整事实）"
                represented_by = ""
            elif raw_id in representative_ids:
                role = "material_escalation"
                reason = material_reasons.get(raw_id, "同一事件出现实质状态变化")
                represented_by = ""
            else:
                role = "duplicate_or_context"
                reason = "同一事件重复转述、措辞变化、补充背景或未构成新的状态变化"
                candidate_dt = _candidate_published_dt(candidate)
                prior_representatives = [
                    item
                    for item in representatives
                    if _candidate_published_dt(item) <= candidate_dt
                ]
                represented_by = str(
                    (prior_representatives[-1] if prior_representatives else primary).get("raw_id") or primary_id
                )
            member_records.append(
                {
                    "raw_id": raw_id,
                    "time": _bj_from_dt(_candidate_published_dt(candidate), timezone_name),
                    "title": str(_val(candidate["row"], "title") or "").strip(),
                    "role": role,
                    "is_displayed": raw_id in representative_ids,
                    "reason": reason,
                    "represented_by_news_id": represented_by,
                    "was_selected_news": bool(candidate.get("_homepage_selected")),
                    "homepage_destinations": list(candidate.get("_homepage_destinations") or []),
                    "candidate": candidate,
                }
            )
        topic_title = str(_val(primary["row"], "title") or identity["event_topic"])
        results.append(
            {
                "event_group_id": f"event_group_{index:03d}",
                "event_key": identity["event_key"],
                "topic": _brief_text(topic_title, 70),
                "members": member_records,
                "representatives": representatives,
            }
        )
    return results


def _homepage_display_summary(event_groups: Iterable[dict[str, object]]) -> dict[str, object]:
    groups = list(event_groups)
    members = [member for group in groups for member in list(group.get("members") or [])]
    displayed = [member for member in members if member.get("is_displayed")]
    primary_count = sum(1 for member in displayed if member.get("role") == "primary")
    escalation_count = sum(1 for member in displayed if member.get("role") == "material_escalation")
    merged_count = sum(1 for member in members if not member.get("is_displayed"))
    total = len(displayed)
    event_count = len(groups)
    over_30_reason = ""
    if total > 30:
        if event_count >= escalation_count:
            over_30_reason = "当日独立重大事件较多，全部保留"
        else:
            over_30_reason = "当日实质升级与实际影响更新较多，全部保留"
        text = (
            f"今日展示 {total} 条重要新闻：{event_count} 个独立事件，{escalation_count} 条实质升级。"
            f"因{over_30_reason}。"
        )
    elif total < 20:
        text = (
            f"今日展示 {total} 条重要新闻：{event_count} 个独立事件，{escalation_count} 条实质升级；"
            "均为通过硬条件的独立事实，未为凑数量加入弱相关新闻。"
        )
    else:
        text = (
            f"今日展示 {total} 条重要新闻：{event_count} 个独立事件，{escalation_count} 条实质升级；"
            "同一事件的重复快讯已合并。"
        )
    return {
        "displayed_news_count": total,
        "event_group_count": event_count,
        "primary_count": primary_count,
        "material_escalation_count": escalation_count,
        "merged_count": merged_count,
        "over_30_reason": over_30_reason,
        "text": text,
    }


def _panel_news_item(candidate: dict[str, object], group: dict[str, object], timezone_name: str) -> dict[str, str]:
    row = candidate["row"]
    title = str(_val(row, "title") or "").strip()
    body = str(_val(row, "body") or _val(row, "detail") or "")
    relation = str(candidate.get("attribution_relation") or candidate.get("relation") or "N/A")
    raw_judgment = str(candidate.get("attribution_judgment") or "暂无法确认")
    judgment = raw_judgment if raw_judgment == "主要驱动" else "同期相关信息"
    if candidate.get("reaction_status") in {"PENDING", "UNCONFIRMED"}:
        judgment = "暂无法确认"
    if relation == "价格先于新闻":
        judgment = "价格先于新闻，不作该段归因"
    return {
        "id": str(candidate["raw_id"]),
        "time": _bj_from_dt(_candidate_published_dt(candidate), timezone_name),
        "title": title,
        "summary": _brief_text(body, 120) or title,
        "url": _valid_jin10_display_url(_val(row, "url")),
        "impact": str(candidate.get("logic") or candidate.get("direct_supply_reason") or "直接影响原油供应或能源运输。"),
        "price_status": str(group.get("pricing_status") or "N/A"),
        "relation": relation,
        "judgment": judgment,
        "confidence": str(candidate.get("attribution_confidence") or candidate.get("confidence") or "低"),
        "reaction_status": str(candidate.get("reaction_status") or "UNCONFIRMED"),
        "reaction_status_note": str(candidate.get("reaction_status_note") or ""),
    }


def _build_blue_panel_groups(review: dict[str, object], timezone_name: str) -> list[dict[str, object]]:
    panel_groups: list[dict[str, object]] = []
    for group in list(review.get("groups") or []):
        homepage_news = list(group.get("homepage_news") or [])
        if not homepage_news:
            homepage_news = [
                item
                for item in list(group.get("candidates") or [])
                if _homepage_candidate_is_eligible(item)
                and int(item.get("direction_alignment") or 0) > 0
            ]
        else:
            homepage_news = [
                item
                for item in homepage_news
                if _homepage_candidate_is_eligible(item)
                and int(item.get("direction_alignment") or 0) > 0
            ]
        homepage_news.sort(
            key=lambda item: (
                _candidate_published_dt(item),
                -_candidate_attribution_priority(item),
                str(item.get("raw_id") or ""),
            )
        )
        start_local = to_beijing(ensure_utc(group["start_dt"]), timezone_name)
        end_local = to_beijing(ensure_utc(group["end_dt"]), timezone_name)
        panel_groups.append(
            {
                "key": _panel_group_key(group, timezone_name),
                "time_range": f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}",
                "direction": str(group.get("direction") or "N/A"),
                "cumulative": _fmt_pct(group.get("cumulative_pct")),
                "pct_5m": _fmt_pct(group.get("pct_5m")),
                "pct_15m": _fmt_pct(group.get("pct_15m")),
                "pct_30m": _fmt_pct(group.get("pct_30m")),
                "pct_60m": _fmt_pct(group.get("pct_60m")),
                "max_30m": _fmt_pct(group.get("max_30m_pct")),
                "is_unexplained": not bool(list(group.get("selected_news") or [])),
                "interpretation": str(group.get("attribution_reason") or "附近新闻不足以支持可靠归因。"),
                "news": [_panel_news_item(item, group, timezone_name) for item in homepage_news],
            }
        )
    return panel_groups


def _raw_major_panel_candidate(row, reaction, major_reason: str = "") -> dict[str, object]:
    title = str(_val(row, "title") or "").strip()
    text = _raw_text(row)
    return {
        "raw_id": str(_val(row, "raw_id") or ""),
        "row": row,
        "published_dt": parse_iso_utc(str(_val(row, "published_at_utc") or "")),
        "direct_supply_path": bool(direct_energy_supply_reason(title, text)),
        "direct_supply_reason": direct_energy_supply_reason(title, text) or "",
        "major_reason": major_reason,
        "freshness": _freshness_label(row, text),
        "is_duplicate": bool(int(_val(reaction, "is_duplicate") or 0)) if reaction else False,
        "price_quote_only": _price_quote_only(text),
        "is_commentary": _is_market_commentary(title),
        "attribution_relation": "未匹配显著异动",
        "attribution_confidence": "低",
    }


def _build_unmatched_major_panel_items(
    audit: dict[str, object],
    reaction_rows: Iterable,
    displayed_raw_ids: Iterable[str],
    timezone_name: str,
) -> list[dict[str, str]]:
    reaction_by_id = {str(_val(row, "news_id") or ""): row for row in reaction_rows}
    displayed = {str(raw_id) for raw_id in displayed_raw_ids}
    items: list[dict[str, str]] = []
    for row, _oil_reason, _major_reason in list(audit.get("major_candidates") or []):
        raw_id = str(_val(row, "raw_id") or "")
        candidate = _raw_major_panel_candidate(row, reaction_by_id.get(raw_id))
        if raw_id in displayed or not _homepage_candidate_is_eligible(candidate):
            continue
        title = str(_val(row, "title") or "").strip()
        body = str(_val(row, "body") or _val(row, "detail") or "")
        items.append(
            {
                "id": raw_id,
                "time": _raw_bj(row, timezone_name),
                "title": title,
                "summary": _brief_text(body, 120) or title,
                "url": _valid_jin10_display_url(_val(row, "url")),
                "impact": "暂未发现可直接匹配的Brent显著异动，需持续关注后续影响。",
                "price_status": "未匹配显著异动",
                "relation": "未匹配显著异动",
                "judgment": "⚠ 重大事件",
                "confidence": "低",
            }
        )
    return sorted(items, key=lambda item: (item["time"], item["id"]))


def _homepage_destinations(
    panel_groups: Iterable[dict[str, object]],
    unmatched_major_items: Iterable[dict[str, str]],
) -> dict[str, tuple[str, ...]]:
    destinations: dict[str, list[str]] = defaultdict(list)
    for group in panel_groups:
        destination = f"价格异动组 {group['time_range']}"
        for item in list(group.get("news") or []):
            raw_id = str(item.get("id") or "")
            if raw_id and destination not in destinations[raw_id]:
                destinations[raw_id].append(destination)
    for item in unmatched_major_items:
        raw_id = str(item.get("id") or "")
        if raw_id and "⚠ 重大事件" not in destinations[raw_id]:
            destinations[raw_id].append("⚠ 重大事件")
    return {raw_id: tuple(routes) for raw_id, routes in destinations.items()}


def _homepage_priority_candidate(candidate: Mapping[str, object]) -> Mapping[str, object]:
    if candidate.get("row") is not None:
        return candidate
    published: datetime | None = None
    time_text = str(candidate.get("time") or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            published = datetime.strptime(time_text, fmt).replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            ).astimezone(ZoneInfo("UTC"))
            break
        except ValueError:
            continue
    return {
        **dict(candidate),
        "raw_id": str(candidate.get("id") or ""),
        "published_dt": published or datetime.min.replace(tzinfo=ZoneInfo("UTC")),
        "row": {
            "raw_id": str(candidate.get("id") or ""),
            "title": str(candidate.get("title") or ""),
            "body": str(candidate.get("summary") or ""),
            "url": str(candidate.get("url") or ""),
        },
    }


def _homepage_all_day_priority(candidate: Mapping[str, object], _role: str) -> tuple[int, int, datetime, str]:
    candidate = _homepage_priority_candidate(candidate)
    identity = _homepage_event_identity(candidate)
    state = _homepage_event_state(candidate)
    event_key = str(identity["event_key"])
    if candidate.get("_homepage_selected"):
        priority = 0
    elif state in {"disruption", "recovery"}:
        priority = 1
    elif state == "effective" or (event_key == "sanctions_policy" and state == "announced"):
        priority = 2
    elif event_key in {"opec_supply", "crude_oil_market", "exports_loading", "official_selling_price"}:
        priority = 3
    elif event_key in {"hormuz_shipping", "tanker_shipping", "lng_supply_security", "geopolitical_military"}:
        priority = 4
    else:
        priority = 5
    group_order = candidate.get("_homepage_group_order")
    return (
        priority,
        int(group_order) if group_order is not None else 10_000,
        _candidate_published_dt(dict(candidate)),
        str(candidate.get("raw_id") or ""),
    )


def _build_event_grouped_homepage(
    movement_review: dict[str, object],
    audit: dict[str, object],
    reaction_rows: Iterable,
    selected_raw_ids: Iterable[str],
    timezone_name: str,
) -> FormalReportPresentation:
    selected = {str(raw_id) for raw_id in selected_raw_ids}
    reaction_by_id = {str(_val(row, "news_id") or ""): row for row in reaction_rows}
    candidate_by_id: dict[str, dict[str, object]] = {}
    destinations_by_id: dict[str, list[str]] = defaultdict(list)
    group_keys_by_id: dict[str, list[str]] = defaultdict(list)
    movement_group_by_key: dict[str, dict[str, object]] = {}

    def register(
        candidate: dict[str, object],
        destination: str,
        group_key: str = "",
        group_order: int = 10_000,
    ) -> None:
        raw_id = str(candidate.get("raw_id") or "")
        if not raw_id:
            return
        existing = candidate_by_id.get(raw_id)
        if existing is None or _candidate_attribution_priority(candidate) > _candidate_attribution_priority(existing):
            merged = dict(candidate)
            if existing:
                for key in ("major_reason", "direct_supply_reason"):
                    if not merged.get(key) and existing.get(key):
                        merged[key] = existing[key]
            candidate_by_id[raw_id] = merged
        if destination and destination not in destinations_by_id[raw_id]:
            destinations_by_id[raw_id].append(destination)
        if group_key and group_key not in group_keys_by_id[raw_id]:
            group_keys_by_id[raw_id].append(group_key)
        current_order = candidate_by_id[raw_id].get("_homepage_group_order")
        if current_order is None or group_order < int(current_order):
            candidate_by_id[raw_id]["_homepage_group_order"] = group_order

    movement_groups = list(movement_review.get("groups") or [])
    for group_order, group in enumerate(movement_groups):
        key = _panel_group_key(group, timezone_name)
        movement_group_by_key[key] = group
        start_local = to_beijing(ensure_utc(group["start_dt"]), timezone_name)
        end_local = to_beijing(ensure_utc(group["end_dt"]), timezone_name)
        destination = f"价格异动组 {start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
        homepage_news = list(group.get("homepage_news") or [])
        if not homepage_news:
            homepage_news = [
                item
                for item in list(group.get("candidates") or [])
                if _homepage_candidate_is_eligible(item)
                and int(item.get("direction_alignment") or 0) > 0
            ]
        for candidate in homepage_news:
            if (
                _homepage_candidate_is_eligible(candidate)
                and int(candidate.get("direction_alignment") or 0) > 0
            ):
                register(dict(candidate), destination, key, group_order)

    for row, _oil_reason, major_reason in list(audit.get("major_candidates") or []):
        raw_id = str(_val(row, "raw_id") or "")
        candidate = _raw_major_panel_candidate(row, reaction_by_id.get(raw_id), str(major_reason or ""))
        if not _homepage_candidate_is_eligible(candidate):
            continue
        if raw_id in candidate_by_id:
            if major_reason and not candidate_by_id[raw_id].get("major_reason"):
                candidate_by_id[raw_id]["major_reason"] = str(major_reason)
            continue
        register(candidate, "⚠ 重大事件")

    for raw_id, candidate in candidate_by_id.items():
        candidate["_homepage_selected"] = raw_id in selected
        candidate["_homepage_destinations"] = tuple(destinations_by_id[raw_id])
        candidate["_homepage_group_keys"] = tuple(group_keys_by_id[raw_id])

    event_groups = _group_homepage_candidates(candidate_by_id.values(), timezone_name)
    representation_by_id: dict[str, str] = {}
    representative_routes: dict[str, list[str]] = defaultdict(list)
    representative_group_keys: dict[str, list[str]] = defaultdict(list)
    representative_meta: dict[str, tuple[dict[str, object], str, str]] = {}
    selected_representative_ids: set[str] = set()
    for event_group in event_groups:
        event_group_id = str(event_group["event_group_id"])
        for member in list(event_group.get("members") or []):
            raw_id = str(member.get("raw_id") or "")
            representative_id = raw_id if member.get("is_displayed") else str(member.get("represented_by_news_id") or "")
            if not representative_id:
                continue
            representation_by_id[raw_id] = representative_id
            if member.get("was_selected_news"):
                selected_representative_ids.add(representative_id)
            for destination in list(member.get("homepage_destinations") or []):
                if destination not in representative_routes[representative_id]:
                    representative_routes[representative_id].append(str(destination))
            original_candidate = member.get("candidate")
            if isinstance(original_candidate, Mapping):
                for key in list(original_candidate.get("_homepage_group_keys") or []):
                    if key not in representative_group_keys[representative_id]:
                        representative_group_keys[representative_id].append(str(key))
            if member.get("is_displayed"):
                representative_meta[raw_id] = (dict(member["candidate"]), str(member["role"]), event_group_id)

    for representative_id in selected_representative_ids:
        if representative_id in representative_meta:
            representative_meta[representative_id][0]["_homepage_selected"] = True

    panel_item_by_id: dict[str, dict[str, object]] = {}
    all_day_items: list[dict[str, object]] = []
    unmatched_major_items: list[dict[str, str]] = []
    homepage_destinations: dict[str, tuple[str, ...]] = {}
    for raw_id, (candidate, role, event_group_id) in representative_meta.items():
        routes = representative_routes.get(raw_id, [])
        matched_routes = [route for route in routes if route != "⚠ 重大事件"]
        final_routes = matched_routes or (["⚠ 重大事件"] if "⚠ 重大事件" in routes else [])
        homepage_destinations[raw_id] = tuple(final_routes)
        first_group_key = next(iter(representative_group_keys.get(raw_id, [])), "")
        context_group = movement_group_by_key.get(first_group_key, {"pricing_status": "未匹配显著异动"})
        item = dict(_panel_news_item(candidate, context_group, timezone_name))
        item.update(
            {
                "event_group_id": event_group_id,
                "role": role,
                "destinations": final_routes,
            }
        )
        panel_item_by_id[raw_id] = item
        if matched_routes:
            all_day_items.append(item)
        else:
            item.update(
                {
                    "price_status": "未匹配显著异动",
                    "relation": "未匹配显著异动",
                    "judgment": "⚠ 重大事件",
                    "confidence": "低",
                    "impact": "暂未发现可直接匹配的Brent显著异动，需持续关注后续影响。",
                }
            )
            unmatched_major_items.append(item)  # type: ignore[arg-type]

    role_by_id = {raw_id: meta[1] for raw_id, meta in representative_meta.items()}
    all_day_items.sort(
        key=lambda item: _homepage_all_day_priority(
            representative_meta[str(item["id"])][0], role_by_id[str(item["id"])]
        )
    )
    unmatched_major_items.sort(key=lambda item: (str(item.get("time") or ""), str(item.get("id") or "")))

    base_panel_groups = _build_blue_panel_groups(movement_review, timezone_name)
    for panel_group in base_panel_groups:
        key = str(panel_group.get("key") or "")
        representative_ids = [
            raw_id
            for raw_id, keys in representative_group_keys.items()
            if key in keys and raw_id in panel_item_by_id and "⚠ 重大事件" not in homepage_destinations.get(raw_id, ())
        ]
        representative_ids.sort(
            key=lambda raw_id: _homepage_all_day_priority(
                representative_meta[raw_id][0], representative_meta[raw_id][1]
            )
        )
        panel_group["news"] = [panel_item_by_id[raw_id] for raw_id in representative_ids]

    summary = _homepage_display_summary(event_groups)
    merged_representations = {
        raw_id: representative_id
        for raw_id, representative_id in representation_by_id.items()
        if raw_id != representative_id
    }
    return FormalReportPresentation(
        panel_groups=tuple(base_panel_groups),
        unmatched_major_items=tuple(unmatched_major_items),
        iran_update_items=(),
        all_day_items=tuple(all_day_items),
        display_summary=summary,
        event_groups=tuple(event_groups),
        homepage_representations=merged_representations,
    )


def _homepage_event_group_audit_lines(
    event_groups: Iterable[dict[str, object]],
    summary: Mapping[str, object] | None,
) -> list[str]:
    groups = list(event_groups)
    summary = dict(summary or _homepage_display_summary(groups))
    lines = ["## 首页新闻合并与展示说明", ""]
    lines.extend(
        [
            f"- 首页最终展示新闻数量：{summary.get('displayed_news_count', 0)}",
            f"- 独立事件组数量：{summary.get('event_group_count', 0)}",
            f"- 首条事实数量：{summary.get('primary_count', 0)}",
            f"- 实质升级新闻数量：{summary.get('material_escalation_count', 0)}",
            f"- 被合并为重复/背景/转述的新闻数量：{summary.get('merged_count', 0)}",
        ]
    )
    if summary.get("over_30_reason"):
        lines.append(f"- 超过30条原因：{summary['over_30_reason']}。")
    lines.append("")
    for group in groups:
        lines.append(f"### {group.get('event_group_id')} | {group.get('topic') or '未命名事件'}")
        lines.append("")
        representative_ids = [str(item.get("raw_id") or "") for item in list(group.get("representatives") or [])]
        lines.append(f"- 首页代表新闻 ID：{'、'.join(representative_ids) or '无'}")
        for member in list(group.get("members") or []):
            destinations = "；".join(str(item) for item in list(member.get("homepage_destinations") or [])) or "无"
            represented_by = str(member.get("represented_by_news_id") or "")
            represented_note = f" | represented_by_news_id={represented_by}" if represented_by else ""
            lines.append(
                f"- {member.get('raw_id')} | {member.get('time')} | {member.get('title')} | "
                f"角色={member.get('role')} | 进入首页={'是' if member.get('is_displayed') else '否'} | "
                f"原因={member.get('reason')} | homepage_destinations={destinations}{represented_note}"
            )
            if member.get("was_selected_news") and not member.get("is_displayed"):
                lines.append(f"  - 正式归因新闻追溯：由 {represented_by or '未确定代表新闻'} 代表展示。")
        lines.append("")
    return lines


def _bj_from_dt(value: datetime | None, timezone_name: str = "Asia/Shanghai", with_seconds: bool = False) -> str:
    if not value:
        return "N/A"
    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return to_beijing(value, timezone_name).strftime(fmt)


def _yes_no(value: object) -> str:
    return "是" if bool(value) else "否"


def _instrument_and_coverage_lines(
    audit: Mapping[str, object],
    report_status: str,
    timezone_name: str,
) -> list[str]:
    metadata = BRENT_INSTRUMENT_METADATA
    price_rows = list(audit.get("price_rows") or [])
    coverage_gaps = [str(item) for item in list(audit.get("coverage_gaps") or [])]
    lines = [
        "## 标的信息",
        "",
        f"- instrument_name: {metadata['instrument_name']}",
        f"- source: {metadata['source']}",
        f"- ticker: {metadata['ticker']}",
        f"- contract_month: {metadata['contract_month']}",
        f"- contract_type: {metadata['contract_type']}",
        "",
        "## 数据覆盖范围",
        "",
        f"- 价格统计窗口：{_bj_from_dt(audit.get('price_start_required'), timezone_name, with_seconds=True)} 至 "
        f"{_bj_from_dt(audit.get('price_end_required'), timezone_name, with_seconds=True)} 北京时间（截止时刻不含）",
        f"- 实际新闻覆盖：{_bj_from_dt(audit.get('first_raw'), timezone_name, with_seconds=True)} 至 "
        f"{_bj_from_dt(audit.get('last_raw'), timezone_name, with_seconds=True)} 北京时间",
        f"- 实际价格覆盖：{_bj_from_dt(audit.get('first_price'), timezone_name, with_seconds=True)} 至 "
        f"{_bj_from_dt(audit.get('last_price'), timezone_name, with_seconds=True)} 北京时间",
        f"- Brent 5分钟K线数量：{len(price_rows)}",
        f"- 重复时间戳数量：{audit.get('duplicate_timestamp_count', 0)}",
        f"- 时间戳是否有序：{_yes_no(audit.get('timestamps_ordered', True))}",
        f"- 大于5分钟的K线缺口数量：{audit.get('price_gap_count', 0)}；"
        f"最大缺口：{audit.get('max_price_gap_minutes', 0)}分钟",
        f"- 有效交易时段大于10分钟缺口数量：{audit.get('material_price_gap_count', 0)}；"
        f"最大有效缺口：{audit.get('max_material_price_gap_minutes', 0)}分钟",
        f"- 正常维护休市缺口数量：{audit.get('expected_maintenance_gap_count', 0)}",
        f"- 是否使用部分缓存：{'是' if report_status != 'FORMAL' else '否'}",
    ]
    if coverage_gaps:
        lines.append(f"- 数据状态：{report_status}；原因：{'；'.join(coverage_gaps)}")
    else:
        lines.append(f"- 数据状态：{report_status}；窗口覆盖校验通过。")
    lines.append("")
    return lines


def _data_confidence(audit: dict[str, object]) -> str:
    coverage_gaps = list(audit.get("coverage_gaps") or [])
    news_ok = bool(audit.get("news_covered"))
    price_ok = bool(audit.get("price_covered"))
    if news_ok and price_ok and not coverage_gaps and not audit.get("price_missing_count"):
        return "高"
    if news_ok or price_ok:
        return "中"
    return "低"


def _row_bj_dt(row, timezone_name: str = "Asia/Shanghai") -> datetime | None:
    published = parse_iso_utc(str(row["published_at_utc"]))
    return to_beijing(published, timezone_name) if published else None


def _raw_line(row, timezone_name: str = "Asia/Shanghai") -> str:
    title = str(_val(row, "title") or "").strip() or str(_val(row, "body") or "").strip()[:80]
    source_channel = str(_val(row, "source_channel") or "flash")
    url = str(_val(row, "url") or "")
    direct, oil_reason, major_reason = _raw_oil_status(row)
    oil_label = "油市相关" if direct else "非直接油市"
    major_label = f"；重大规则：{major_reason}" if major_reason else ""
    return f"- {_raw_bj(row, timezone_name)} | {source_channel} | {title} | {oil_label}（{oil_reason}）{major_label} | {url}"


def _data_integrity_lines(audit: dict[str, object], start_utc: datetime, end_utc: datetime, timezone_name: str) -> list[str]:
    raw_rows = list(audit.get("raw_rows") or [])
    batches = {str(_val(row, "fetch_batch_id") or "") for row in raw_rows if _val(row, "fetch_batch_id")}
    pages = [int(_val(row, "fetch_page") or 0) for row in raw_rows]
    channels = Counter(str(_val(row, "source_channel") or "unknown") for row in raw_rows)
    missing = list(audit.get("missing_periods") or [])
    coverage_gaps = list(audit.get("coverage_gaps") or [])
    channel_text = "、".join(f"{name}:{count}" for name, count in channels.most_common()) or "N/A"
    max_page = max(pages) if pages else 0
    segments = audit.get("segments") or {}

    lines = [
        "## 数据完整性检查",
        "",
        f"- 日报新闻窗口：{_bj_from_dt(start_utc, timezone_name)} 至 {_bj_from_dt(end_utc, timezone_name)} 北京时间",
        f"- 原始新闻总数：{audit.get('raw_total', 0)}",
        f"- 有效候选新闻数量：{audit.get('effective_total', 0)}",
        f"- 最早抓到新闻时间：{_bj_from_dt(audit.get('first_raw'), timezone_name, with_seconds=True)}",
        f"- 最晚抓到新闻时间：{_bj_from_dt(audit.get('last_raw'), timezone_name, with_seconds=True)}",
        f"- 08:00-16:00 原始新闻数量：{segments.get('08:00-16:00', 0)}",
        f"- 16:00-00:00 原始新闻数量：{segments.get('16:00-00:00', 0)}",
        f"- 00:00-08:00 原始新闻数量：{segments.get('00:00-08:00', 0)}",
        f"- 实际抓取批次数：{len(batches)}；窗口内最大抓取页码：{max_page}",
        f"- 覆盖来源通道：{channel_text}",
        f"- 最新一条原始新闻发布时间：{_bj_from_dt(audit.get('last_raw'), timezone_name, with_seconds=True)}",
        f"- 最后一根Brent 5分钟K线北京时间：{_bj_from_dt(audit.get('last_price'), timezone_name, with_seconds=True)}",
        f"- 新闻新鲜度：{audit.get('news_freshness') or 'unknown'}",
        f"- Brent价格新鲜度：{audit.get('price_freshness') or 'unknown'}",
        f"- 数据完整性状态：{audit.get('data_integrity') or 'unknown'}",
        f"- 是否完成新闻窗口覆盖：{_yes_no(audit.get('news_covered'))}",
        f"- 价格统计窗口首尾是否覆盖：{_yes_no(audit.get('price_boundary_covered'))}",
        f"- 极端价格波动诊断是否触发：{_yes_no(not audit.get('price_continuity_ok', True))}",
        f"- 有效交易时段大于10分钟缺口数量：{audit.get('material_price_gap_count', 0)}",
        f"- 逐事件价格不完整数量：{audit.get('price_missing_count', 0)}/{audit.get('reaction_total', 0)}",
    ]
    if missing:
        lines.append("- 是否存在缺失时段：是")
        for item in missing:
            lines.append(f"  - {item}")
        if coverage_gaps:
            lines.append("- 日报状态：窗口覆盖不完整，市场主线仅供初步参考，不应作为完整夜间事件复盘。")
        else:
            lines.append("- 日报状态：窗口覆盖校验通过；逐事件缺价已标为N/A并保留原因。")
    else:
        lines.append("- 是否存在缺失时段：否")
        lines.append("- 日报状态：新闻与价格窗口覆盖校验通过。")
    lines.append("")
    return lines


def _audit_data_integrity_lines(audit: dict[str, object], start_utc: datetime, end_utc: datetime, timezone_name: str) -> list[str]:
    raw_rows = list(audit.get("raw_rows") or [])
    batches = {str(_val(row, "fetch_batch_id") or "") for row in raw_rows if _val(row, "fetch_batch_id")}
    pages = [int(_val(row, "fetch_page") or 0) for row in raw_rows]
    missing = list(audit.get("missing_periods") or [])
    price_rows = list(audit.get("price_rows") or [])
    price_point_missing = dict(audit.get("price_point_missing_counts") or {})
    reaction_status_counts = dict(audit.get("reaction_status_counts") or {})
    price_continuity_issues = list(audit.get("price_continuity_issues") or [])
    confidence = _data_confidence(audit)
    lines = [
        "## 1. 数据完整性检查",
        "",
        f"- 日报统计窗口：{_bj_from_dt(start_utc, timezone_name, with_seconds=True)} 至 {_bj_from_dt(end_utc, timezone_name, with_seconds=True)} 北京时间",
        f"- 原始新闻总数：{audit.get('raw_total', 0)}",
        f"- 有效油市相关新闻数量：{audit.get('effective_total', 0)}",
        f"- 强制候选池新闻数量：{len(audit.get('major_candidates') or [])}",
        f"- 最早原始新闻发布时间：{_bj_from_dt(audit.get('first_raw'), timezone_name, with_seconds=True)}",
        f"- 最晚原始新闻发布时间：{_bj_from_dt(audit.get('last_raw'), timezone_name, with_seconds=True)}",
        f"- 最晚进入分析新闻发布时间：{_bj_from_dt(audit.get('last_reaction_news'), timezone_name, with_seconds=True)}",
        f"- 实际抓取批次数：{len(batches)}",
        f"- 实际抓取页数：{max(pages) if pages else 0}",
        f"- 新闻窗口是否完整覆盖：{_yes_no(audit.get('news_covered'))}",
        f"- Brent最早5分钟K线时间：{_bj_from_dt(audit.get('first_price'), timezone_name, with_seconds=True)}",
        f"- Brent最晚5分钟K线时间：{_bj_from_dt(audit.get('last_price'), timezone_name, with_seconds=True)}",
        f"- Brent 5分钟K线数量：{len(price_rows)}",
        f"- 重复时间戳数量：{audit.get('duplicate_timestamp_count', 0)}",
        f"- 时间戳是否有序：{_yes_no(audit.get('timestamps_ordered', True))}",
        f"- 大于5分钟的K线缺口数量：{audit.get('price_gap_count', 0)}",
        f"- 最大K线缺口：{audit.get('max_price_gap_minutes', 0)}分钟",
        f"- 新闻新鲜度：{audit.get('news_freshness') or 'unknown'}",
        f"- Brent价格新鲜度：{audit.get('price_freshness') or 'unknown'}",
        f"- 数据完整性状态：{audit.get('data_integrity') or 'unknown'}",
        f"- 价格统计窗口起点是否覆盖：{_yes_no(audit.get('price_start_covered'))}",
        f"- 价格统计窗口截止是否覆盖：{_yes_no(audit.get('price_end_covered'))}",
        f"- 价格统计窗口首尾是否覆盖：{_yes_no(audit.get('price_boundary_covered'))}",
        f"- 极端价格波动诊断是否触发：{_yes_no(not audit.get('price_continuity_ok', True))}",
        f"- 极端价格波动诊断数量：{len(price_continuity_issues)}",
        f"- 有效交易时段大于10分钟缺口数量：{audit.get('material_price_gap_count', 0)}",
        f"- 最大有效交易时段K线缺口：{audit.get('max_material_price_gap_minutes', 0)}分钟",
        f"- 正常维护休市缺口数量：{audit.get('expected_maintenance_gap_count', 0)}",
        f"- 5分钟K线覆盖容差：{audit.get('price_coverage_tolerance_minutes', 0)}分钟",
        f"- 逐事件价格不完整数量：{audit.get('price_missing_count', 0)}/{audit.get('reaction_total', 0)}",
        f"- 逐事件缺失点计数：5分钟={price_point_missing.get('5m', 0)}；15分钟={price_point_missing.get('15m', 0)}；30分钟={price_point_missing.get('30m', 0)}；60分钟={price_point_missing.get('60m', 0)}",
        f"- 单条新闻反应状态：CONFIRMED={reaction_status_counts.get('CONFIRMED', 0)}；"
        f"PENDING={reaction_status_counts.get('PENDING', 0)}；"
        f"UNCONFIRMED={reaction_status_counts.get('UNCONFIRMED', 0)}",
        f"- 本次日报整体数据可信度：{confidence}",
    ]
    if missing:
        lines.append("- 新闻、价格或接口缺口：")
        for item in missing:
            lines.append(f"  - {item}")
        lines.append(
            "> 当前正式日报仅基于已获取数据生成，若缺口覆盖夜间或价格观察窗口，市场主线判断需谨慎使用。"
        )
    else:
        lines.append("- 新闻、价格或接口缺口：未发现。")
    if price_continuity_issues:
        lines.extend(["", "### 极端价格波动诊断明细", ""])
        for item in price_continuity_issues:
            lines.append(
                f"- {_bj_from_dt(item.get('start_utc'), timezone_name, with_seconds=True)} 至 "
                f"{_bj_from_dt(item.get('end_utc'), timezone_name, with_seconds=True)} | "
                f"{_fmt_price(item.get('start_close'))} -> {_fmt_price(item.get('end_close'))} | "
                f"{_fmt_pct(item.get('pct'))} | 相邻间隔{item.get('gap_minutes')}分钟 | "
                "保留原始值，需结合同期重大新闻人工判断。"
            )
    details = list(audit.get("price_missing_details") or [])
    if details:
        lines.extend(["", "### 逐事件价格缺失明细", ""])
        for item in details:
            published = parse_iso_utc(str(item.get("published_at_utc") or ""))
            published_text = _bj_from_dt(published, timezone_name, with_seconds=True)
            points = "、".join(str(point) for point in list(item.get("missing_points") or [])) or "未识别"
            lines.append(
                f"- {published_text} | {item.get('news_id') or 'N/A'} | "
                f"reaction_status={item.get('reaction_status') or 'UNCONFIRMED'} | "
                f"状态说明：{item.get('reaction_status_note') or '观察完整'} | "
                f"缺失点：{points} | 缺失原因：{item.get('reason') or '未记录'}"
            )
    lines.append("")
    return lines


def _segment_ranges(start_utc: datetime, end_utc: datetime, timezone_name: str) -> dict[str, tuple[datetime, datetime]]:
    tz = ZoneInfo(timezone_name)
    start_local = start_utc.astimezone(tz)
    end_local = end_utc.astimezone(tz)
    mid16 = datetime.combine(start_local.date(), time(16, 0), tzinfo=tz)
    midnight = datetime.combine(end_local.date(), time.min, tzinfo=tz)
    return {
        "08:00-16:00": (start_local, mid16),
        "16:00-00:00": (mid16, midnight),
        "00:00-08:00": (midnight, end_local),
    }


def _raw_in_local_range(row, seg_start: datetime, seg_end: datetime, timezone_name: str) -> bool:
    published = parse_iso_utc(str(_val(row, "published_at_utc") or ""))
    if not published:
        return False
    local = to_beijing(published, timezone_name)
    return seg_start <= local < seg_end


def _group_in_local_range(group: dict[str, object], seg_start: datetime, seg_end: datetime, timezone_name: str) -> bool:
    raw_start = group.get("start_dt") or group.get("start_utc")
    if not raw_start:
        return False
    start_dt = raw_start if isinstance(raw_start, datetime) else parse_iso_utc(str(raw_start))
    if not start_dt:
        return False
    local = to_beijing(start_dt, timezone_name)
    return seg_start <= local < seg_end


def _segment_coverage_lines(
    audit: dict[str, object],
    start_utc: datetime,
    end_utc: datetime,
    timezone_name: str,
    homepage_display_raw_ids: Iterable[str],
    movement_groups: Iterable[dict[str, object]],
) -> list[str]:
    raw_rows = list(audit.get("raw_rows") or [])
    displayed = {str(item) for item in homepage_display_raw_ids}
    groups = list(movement_groups or [])
    lines = ["## 2. 原始新闻抓取与分时段覆盖", ""]
    for label, (seg_start, seg_end) in _segment_ranges(start_utc, end_utc, timezone_name).items():
        segment_rows = [row for row in raw_rows if _raw_in_local_range(row, seg_start, seg_end, timezone_name)]
        oil_rows = []
        major_rows = []
        displayed_rows = []
        for row in segment_rows:
            direct, oil_reason, major_reason = _raw_oil_status(row)
            if direct:
                oil_rows.append((row, oil_reason, major_reason))
            if major_reason:
                major_rows.append((row, oil_reason, major_reason))
            if str(_val(row, "raw_id") or "") in displayed:
                displayed_rows.append(row)
        movement_count = sum(1 for group in groups if _group_in_local_range(group, seg_start, seg_end, timezone_name))
        lines.extend(
            [
                f"### {label}",
                "",
                f"- 原始新闻数量：{len(segment_rows)}",
                f"- 有效油市新闻数量：{len(oil_rows)}",
                f"- 重大事件候选数量：{len(major_rows)}",
                f"- 最终进入正式日报首页展示新闻数量：{len(displayed_rows)}（按首页展示的唯一新闻ID计）",
                f"- 最终进入正式日报的价格异动组数量：{movement_count}",
            ]
        )
        if len(segment_rows) == 0 or (label != "00:00-08:00" and len(segment_rows) < 3):
            lines.append("> 该时段新闻覆盖异常偏低，建议复核金十抓取分页、接口返回或网络状态。")
        lines.append("")
    return lines


def _summary_audit_lines(audit: dict[str, object], timezone_name: str) -> list[str]:
    raw_rows = list(audit.get("raw_rows") or [])
    summary_rows = [row for row in raw_rows if int(_val(row, "is_summary") or 0)]
    lines = ["## 汇总类新闻处理审计", ""]
    lines.append(f"- 汇总类新闻数量：{len(summary_rows)}")
    if not summary_rows:
        lines.append("- 本窗口未识别到晨报、晚报、市场回顾、要闻汇总或ETF汇总类新闻。")
        lines.append("")
        return lines
    for row in summary_rows[:10]:
        direct, oil_reason, major_reason = _raw_oil_status(row)
        status = "提取到具体油市事件" if direct else "未提取到具体油市事件"
        major = f"；重大规则：{major_reason}" if major_reason else ""
        lines.append(f"- {_raw_bj(row, timezone_name)} {row['title']}：{status}（{oil_reason}）{major}")
    if len(summary_rows) > 10:
        lines.append(f"- 另有 {len(summary_rows) - 10} 条汇总类新闻已入原始库，未在正文展开。")
    lines.append("")
    return lines


def _forced_candidate_lines(
    audit: dict[str, object],
    selected_rows: Iterable,
    all_reaction_rows: Iterable,
    timezone_name: str,
    selected_raw_ids: Iterable[str] | None = None,
    unpriced_risk_ids: Iterable[str] | None = None,
    homepage_destinations: Mapping[str, Sequence[str]] | None = None,
    homepage_representations: Mapping[str, str] | None = None,
) -> list[str]:
    unpriced_set = {str(item) for item in (unpriced_risk_ids or [])}
    destinations = {
        str(raw_id): [str(destination) for destination in destination_list]
        for raw_id, destination_list in (homepage_destinations or {}).items()
    }
    representations = {
        str(raw_id): str(representative_id)
        for raw_id, representative_id in (homepage_representations or {}).items()
    }
    reaction_by_id = {str(row["news_id"]): row for row in all_reaction_rows}
    candidates = list(audit.get("major_candidates") or [])
    lines = ["## 4. 重大事件强制候选池审计", ""]
    lines.append(f"- 命中重大主题规则的原始新闻数量：{len(candidates)}")
    if not candidates:
        lines.append("- 本窗口未发现命中强制候选规则的原始新闻。")
        lines.append("")
        return lines
    for row, oil_reason, major_reason in candidates:
        raw_id = str(row["raw_id"])
        source_type, _ = _source_quality(row)
        freshness = _freshness_label(row, _raw_text(row))
        reaction = reaction_by_id.get(raw_id)
        title = str(_val(row, "title") or "").strip()
        body = str(_val(row, "body") or _val(row, "detail") or "")
        direct_supply = bool(direct_energy_supply_reason(title, body))
        valid_url = bool(_valid_jin10_display_url(_val(row, "url")))
        if raw_id in destinations:
            status = f"首页展示：{'；'.join(destinations[raw_id])}"
        elif raw_id in representations:
            status = f"首页合并展示：由 {representations[raw_id]} 代表；未作为普通排除"
        elif reaction and int(_val(reaction, "is_duplicate") or 0):
            status = f"未进入首页：重复报道，已并入 {_val(reaction, 'duplicate_of') or '同事件'}"
        elif freshness != "新增事实":
            status = f"未进入首页：非新增事实（{freshness}）"
        elif not valid_url:
            status = "未进入首页：原文链接无效"
        elif not direct_supply:
            status = "未进入首页：无直接油市传导路径"
        else:
            status = "未进入首页：明确噪音新闻"
        summary_note = "；汇总转述" if int(_val(row, "is_summary") or 0) else ""
        duplicate_text = "是" if reaction and int(_val(reaction, "is_duplicate") or 0) else "否"
        price_insufficient = "否"
        if reaction and _val(reaction, "price_status") not in (None, "ok"):
            price_insufficient = f"是（{_val(reaction, 'price_missing_reason') or _val(reaction, 'price_status')}）"
        in_unpriced = "是" if raw_id in unpriced_set else "否"
        lines.append(
            f"- {_raw_bj(row, timezone_name)} | {row['title']} | 分类：{identify_event(str(row['title']), str(_val(row, 'body') or _val(row, 'detail') or ''))[1]} | "
            f"规则：{major_reason} | 来源类型：{source_type}{summary_note} | 新增事实：{freshness} | "
            f"进入正式日报：{_yes_no(raw_id in destinations or raw_id in representations)} | 未进入原因/状态：{status} | "
            f"进入未充分定价风险：{in_unpriced} | 重复报道合并：{duplicate_text} | 价格数据不足：{price_insufficient} | "
            f"油市相关性：{oil_reason}"
        )
    lines.append("")
    return lines


def _price_anomaly_lines(audit: dict[str, object], timezone_name: str) -> list[str]:
    anomalies = list(audit.get("anomalies") or [])
    lines = ["## Brent异常波动反查", ""]
    if not anomalies:
        lines.append("- 窗口内Brent 5分钟价格数据不足，无法完成异常波动扫描。")
        lines.append("")
        return lines
    for idx, item in enumerate(anomalies, 1):
        start = parse_iso_utc(str(item["start_utc"]))
        end = parse_iso_utc(str(item["end_utc"]))
        trigger = "达到异常阈值" if item.get("triggered") else "窗口内绝对波动Top样本"
        lines.append(
            f"### 异常窗口{idx}：{item['kind']}，{_bj_from_dt(start, timezone_name)} 至 {_bj_from_dt(end, timezone_name)}"
        )
        lines.append("")
        lines.append(
            f"- Brent变动：{_fmt_price(item.get('start_close'))} -> {_fmt_price(item.get('end_close'))}，{_fmt_pct(item.get('pct'))}（{trigger}）"
        )
        direct_nearby = list(item.get("direct_nearby") or [])
        nearby = list(item.get("nearby_news") or [])
        if direct_nearby:
            lines.append("- 附近油市/地缘候选新闻：")
            for row, oil_reason, major_reason in direct_nearby[:6]:
                major = f"；重大规则：{major_reason}" if major_reason else ""
                lines.append(f"  - {_raw_bj(row, timezone_name)} {row['title']}（{oil_reason}{major}）")
        else:
            lines.append("- 价格异常但新闻解释不足：±45分钟内未找到明确油市/地缘/供应/航运候选。")
            if nearby:
                lines.append("- 附近已抓取原始新闻：")
                for row in nearby[:6]:
                    lines.append(f"  - {_raw_bj(row, timezone_name)} {row['title']}")
            else:
                lines.append("- 附近已抓取原始新闻：无。可能存在未抓到新闻、其他市场驱动或隔夜外盘驱动。")
        lines.append("")
    return lines


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    lower = text.lower()
    return any(term.lower() in lower for term in terms)


def _price_ts(row) -> datetime | None:
    return parse_iso_utc(str(row["ts_utc"]))


def _move_direction(pct: float) -> str:
    return "上涨" if pct >= 0 else "下跌"


def _abs_window_label(pct: float, fallback: str = "当日最大独立波动") -> str:
    value = abs(pct)
    if value >= 0.005:
        return "30分钟绝对波动 >= 0.50%"
    if value >= 0.0025:
        return "30分钟绝对波动 >= 0.25%"
    return fallback


def _is_overlapping(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def _top_independent_price_windows(windows: list[dict[str, object]], limit: int = 3) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for item in sorted(windows, key=lambda row: abs(float(row["pct"])), reverse=True):
        start = item["start_dt"]
        end = item["end_dt"]
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            continue
        overlaps = any(
            _is_overlapping(start, end, other["start_dt"], other["end_dt"])
            for other in selected
            if isinstance(other.get("start_dt"), datetime) and isinstance(other.get("end_dt"), datetime)
        )
        if overlaps:
            continue
        selected.append({**item, "trigger_label": _abs_window_label(float(item["pct"]))})
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda row: row["start_dt"])


def _scan_30m_price_windows(
    store: TrackerStore,
    brent: BrentConfig,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[list[dict[str, object]], list]:
    """Return 30-minute Brent move windows for the exact report window.

    The selection is intentionally price-first: strong 0.50% windows win; if the
    market is quieter, 0.25% windows are used. If neither exists, no movement is
    returned, so downstream code cannot attach news to a sub-threshold period.
    """
    rows = store.brent_day_klines(brent.symbol, brent.interval, start_utc, end_utc)
    if len(rows) < 2:
        return [], rows
    all_windows: list[dict[str, object]] = []
    timed_rows = [(row, _price_ts(row)) for row in rows]
    timed_rows = [(row, ts) for row, ts in timed_rows if ts is not None]
    for idx, (start_row, start_dt) in enumerate(timed_rows):
        target_end = start_dt + timedelta(minutes=30)
        later_rows = timed_rows[idx + 1 :]
        if not later_rows:
            continue
        end_row, end_dt = min(later_rows, key=lambda item: abs(item[1] - target_end))
        if abs(end_dt - target_end) > timedelta(minutes=10):
            continue
        start_close = float(start_row["close"])
        end_close = float(end_row["close"])
        if not start_close:
            continue
        pct = (end_close - start_close) / start_close
        all_windows.append(
            {
                "start_dt": start_dt,
                "end_dt": end_dt,
                "start_utc": iso_utc(start_dt),
                "end_utc": iso_utc(end_dt),
                "start_close": start_close,
                "end_close": end_close,
                "pct": pct,
                "direction": _move_direction(pct),
                "trigger_label": _abs_window_label(pct),
                "is_threshold": abs(pct) >= 0.0025,
            }
        )

    strong = [item for item in all_windows if abs(float(item["pct"])) >= 0.005]
    if strong:
        return sorted(strong, key=lambda row: row["start_dt"]), rows
    medium = [item for item in all_windows if abs(float(item["pct"])) >= 0.0025]
    if medium:
        return sorted(medium, key=lambda row: row["start_dt"]), rows
    return [], rows


def _merge_price_windows(windows: list[dict[str, object]], price_rows: list) -> list[dict[str, object]]:
    if not windows:
        return []
    sorted_windows = sorted(windows, key=lambda row: row["start_dt"])
    groups: list[dict[str, object]] = []
    for item in sorted_windows:
        item_start = item["start_dt"]
        item_end = item["end_dt"]
        if not isinstance(item_start, datetime) or not isinstance(item_end, datetime):
            continue
        if groups:
            current = groups[-1]
            same_direction = current["direction"] == item["direction"]
            contiguous = item_start <= current["end_dt"] + timedelta(minutes=5)
            if same_direction and contiguous:
                current["component_windows"].append(item)
                current["start_dt"] = min(current["start_dt"], item_start)
                current["end_dt"] = max(current["end_dt"], item_end)
                best = max(current["component_windows"], key=lambda row: abs(float(row["pct"])))
                current["max_30m_pct"] = float(best["pct"])
                current["max_30m_start_dt"] = best["start_dt"]
                current["max_30m_end_dt"] = best["end_dt"]
                current["trigger_label"] = _abs_window_label(float(best["pct"]), str(best.get("trigger_label") or "当日最大独立波动"))
                continue
        groups.append(
            {
                "start_dt": item_start,
                "end_dt": item_end,
                "direction": item["direction"],
                "component_windows": [item],
                "max_30m_pct": float(item["pct"]),
                "max_30m_start_dt": item_start,
                "max_30m_end_dt": item_end,
                "trigger_label": item.get("trigger_label") or _abs_window_label(float(item["pct"])),
            }
        )

    price_by_ts = {_price_ts(row): row for row in price_rows if _price_ts(row)}
    for idx, group in enumerate(groups, 1):
        start_row = price_by_ts.get(group["start_dt"])
        end_row = price_by_ts.get(group["end_dt"])
        start_close = float(start_row["close"]) if start_row else None
        end_close = float(end_row["close"]) if end_row else None
        cumulative_pct = (end_close - start_close) / start_close if start_close else None
        offset_metrics: dict[str, object] = {}
        for minutes in (5, 15, 30, 60):
            target = group["start_dt"] + timedelta(minutes=minutes)
            target_row = price_by_ts.get(target)
            close = float(target_row["close"]) if target_row else None
            offset_metrics[f"price_time_{minutes}m_dt"] = target if target_row else None
            offset_metrics[f"price_{minutes}m"] = close
            offset_metrics[f"pct_{minutes}m"] = (close - start_close) / start_close if close is not None and start_close else None
        rows_in_group = [
            row
            for row in price_rows
            if (ts := _price_ts(row)) and group["start_dt"] <= ts <= group["end_dt"]
        ]
        highs = [float(row["high"]) for row in rows_in_group]
        lows = [float(row["low"]) for row in rows_in_group]
        group.update(
            {
                "group_id": f"move_{idx:02d}_{iso_utc(group['start_dt']).replace(':', '').replace('-', '').replace('Z', '')}",
                "start_close": start_close,
                "end_close": end_close,
                "cumulative_pct": cumulative_pct,
                "high": max(highs) if highs else None,
                "low": min(lows) if lows else None,
                "search_start_dt": group["start_dt"] - timedelta(minutes=45),
                "search_end_dt": group["end_dt"] + timedelta(minutes=15),
                **offset_metrics,
            }
        )
    return groups


def _source_quality(row) -> tuple[str, int]:
    title = str(_val(row, "title") or "")
    text = _raw_text(row)
    if _is_market_commentary(title):
        return "市场评论/背景", -12
    if int(_val(row, "is_summary") or 0):
        return "汇总转述", -15
    official_terms = [
        "EIA",
        "美国能源信息署",
        "OPEC",
        "欧佩克",
        "IEA",
        "国际能源署",
        "能源部",
        "外交部",
        "军方",
        "官方",
        "部长",
        "央行",
        "API",
        "海关",
    ]
    media_terms = ["路透", "彭博", "华尔街日报", "金融时报", "媒体", "报道称", "据"]
    bank_terms = ["摩根士丹利", "高盛", "花旗", "瑞银", "道明证券", "摩根大通", "巴克莱", "投行"]
    if _contains_any(text, official_terms):
        return "官方/权威机构", 12
    if _contains_any(text, media_terms):
        return "主流媒体", 8
    if _contains_any(text, bank_terms):
        return "行业机构/投行", 6
    return "金十快讯", 4


def _is_institution_forecast_noise(row) -> bool:
    text = _raw_text(row)
    institution_terms = ["摩根士丹利", "高盛", "花旗", "瑞银", "道明证券", "摩根大通", "巴克莱", "投行", "机构", "证券", "研报"]
    forecast_terms = [
        "预测",
        "预计",
        "预期",
        "估计",
        "假设",
        "均价",
        "料将",
        "或将",
        "可能",
        "目标价",
        "警告",
        "变数",
        "前景",
        "面临",
        "复苏",
        "认为",
        "研判",
        "展望",
        "观点",
    ]
    return _contains_any(text, institution_terms) and _contains_any(text, forecast_terms)


def _is_formal_report_noise(row) -> bool:
    text = _raw_text(row)
    hard_noise_terms = [
        "目标价",
        "评级",
        "股价",
        "股票",
        "A股",
        "港股",
        "美股",
        "ETF",
        "IPO",
        "回购",
        "减持",
        "解禁",
        "财报",
        "净利润",
        "增资",
        "股权",
        "ADR",
        "纳斯达克",
        "芯片",
        "半导体",
        "人工智能",
        "换汇",
        "外汇",
        "期货热点追踪",
        "市场行情一览",
        "全球股市指数",
        "点击阅读",
        "点击查看",
        "正在直播",
    ]
    if _contains_any(text, hard_noise_terms):
        return True
    if _contains_any(text, ["出口管制", "先进计算", "计算物项"]) and not _contains_any(
        text, ["原油", "石油", "油轮", "霍尔木兹", "航运", "港口", "装船", "能源"]
    ):
        return True
    return False


def _is_market_commentary(title: str) -> bool:
    return title.startswith(("市场分析", "分析：", "机构观点", "原油头条")) or "为何" in title or "Live分析" in title


def _candidate_tier(
    event_key: str,
    text: str,
    major_reason: str,
    macro_reason: str,
    is_summary: bool,
    is_commentary: bool,
    price_quote_only: bool,
) -> int:
    if is_summary or is_commentary or price_quote_only:
        return 3
    tier_one_topics = {
        "hormuz_shipping",
        "tanker_shipping",
        "exports_loading",
        "sanctions_policy",
        "opec_supply",
        "official_selling_price",
        "lng_supply_security",
        "iran_nuclear",
        "geopolitical_military",
        "crude_trade_flows",
    }
    if event_key in tier_one_topics:
        return 1
    if major_reason and "油价快速变化或市场预期调整" not in major_reason:
        return 1
    if _contains_any(text, ["EIA", "API", "库存", "库欣", "战略石油储备", "产量", "出口", "装船"]):
        return 1
    if macro_reason or event_key in {"bank_oil_forecast", "oil_market_expectations", "crude_oil_market", "gas_lng_lpg"}:
        return 2
    return 3


def _macro_driver_reason(text: str) -> str:
    macro_terms = [
        "美元",
        "美联储",
        "利率",
        "降息",
        "加息",
        "CPI",
        "PCE",
        "PMI",
        "ISM",
        "ADP",
        "非农",
        "就业",
        "失业率",
        "初请",
        "风险偏好",
        "美国国债",
        "库存",
        "EIA",
        "API",
        "战略石油储备",
        "库欣",
    ]
    if _contains_any(text, macro_terms):
        return "宏观/库存/美元或风险偏好线索"
    return ""


def _price_quote_only(text: str) -> bool:
    price_terms = ["日内涨", "日内跌", "涨幅", "跌幅", "现报", "报每桶", "站上", "跌破"]
    driver_terms = ["库存", "供应", "出口", "制裁", "OPEC", "欧佩克", "霍尔木兹", "油轮", "LNG", "地缘", "美元", "利率"]
    if _contains_any(text, ["反映了布伦特原油价格的下降", "价格似乎已稳定", "石油市场尚未恢复正常"]):
        return True
    return _contains_any(text, price_terms) and not _contains_any(text, driver_terms)


def _relation_to_move(published: datetime | None, group: dict[str, object]) -> str:
    if not published:
        return "时间未知"
    if published <= group["start_dt"]:
        return "新闻先于价格"
    if group["start_dt"] < published <= group["end_dt"]:
        return "同期发生"
    return "价格先于新闻"


def _candidate_exclusion_reason(candidate: dict[str, object], core: dict[str, object] | None) -> str:
    if core and candidate["raw_id"] == core["raw_id"]:
        return "核心驱动"
    if not candidate.get("direct_supply_path"):
        return "未直接涉及原油或能源供应端变化"
    if candidate.get("is_summary"):
        return "汇总类或二次转述，优先级低于原始快讯"
    if candidate.get("is_commentary"):
        return "市场分析/背景评论，优先级低于原始突发或实物事件"
    if candidate.get("price_quote_only"):
        return "仅描述油价波动，缺少独立新增事实"
    if float(candidate.get("score") or 0) < 35:
        return "与该段Brent异动缺少明确传导路径"
    if core:
        return "作为次要驱动/同期事件保留，核心解释由更直接事件承担"
    return "未达到核心驱动置信度"


def _market_direction_from_bias(bias: str, group_pct: float | None = None) -> str:
    if bias.startswith("偏多"):
        return "偏多"
    if bias.startswith("偏空"):
        return "偏空"
    if group_pct is not None and abs(group_pct) >= 0.0025:
        return "偏多" if group_pct > 0 else "偏空"
    return "中性"


def _direction_alignment(logic: str, group: dict[str, object]) -> int:
    logic_dir = _market_direction_from_bias(logic)
    if logic_dir == "中性":
        return 0
    price_dir = "偏多" if float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0) > 0 else "偏空"
    return 1 if logic_dir == price_dir else -1


def _pricing_status_for_candidate(candidate: dict[str, object] | None, group: dict[str, object]) -> str:
    if not candidate:
        return "无法判断"
    relation = str(candidate.get("relation") or "")
    if relation == "价格先于新闻":
        return "无法判断"
    bias_dir = _market_direction_from_bias(str(candidate.get("logic") or ""))
    price_dir = "偏多" if float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0) > 0 else "偏空"
    if bias_dir in {"偏多", "偏空"} and bias_dir != price_dir:
        return "与新闻逻辑背离"
    if abs(float(group.get("max_30m_pct") or 0)) >= 0.005:
        return "已部分定价"
    return "尚未充分定价"


def _implied_direction_from_text(text: str) -> str:
    bullish_terms = [
        "袭击",
        "封锁",
        "停航",
        "绕航",
        "制裁",
        "中断",
        "减产",
        "库存下降",
        "供应担忧",
        "风险溢价",
        "紧急采购",
        "出口受阻",
        "装船中断",
        "港口关闭",
        "搁浅",
        "受损",
        "扣留",
        "滞留",
        "获批航线",
        "出口下降",
        "出口降至",
        "出口略微下降",
        "石油出口略微下降",
        "石油出口降",
        "产量下降",
        "产量降幅",
    ]
    bearish_terms = [
        "恢复",
        "停火",
        "缓和",
        "增产",
        "提高产量",
        "增加供应",
        "产量配额上调",
        "配额上调",
        "提高8月份的产量目标",
        "产量目标将增加",
        "进一步提高",
        "增供",
        "上调产量",
        "库存增加",
        "下调",
        "降低",
        "供应充足",
        "风险缓和",
        "需求放缓",
    ]
    has_bullish = _contains_any(text, bullish_terms)
    has_bearish = _contains_any(text, bearish_terms)
    if has_bullish and not has_bearish:
        return "偏多"
    if has_bearish and not has_bullish:
        return "偏空"
    return "中性"


def _freshness_label(row, text: str) -> str:
    title = str(_val(row, "title") or "")
    if int(_val(row, "is_summary") or 0):
        return "汇总转述"
    if _is_market_commentary(title):
        return "不确定"
    fresh_terms = [
        "发生",
        "消息人士",
        "据悉",
        "报道称",
        "宣布",
        "确认",
        "公布",
        "同意",
        "将",
        "上调",
        "下调",
        "提高",
        "降低",
        "袭击",
        "搁浅",
        "中断",
        "恢复",
        "取消",
        "制裁",
        "增产",
        "减产",
        "EIA",
        "API",
        "当周",
        "库存数据",
    ]
    stale_terms = [
        "整理",
        "一览",
        "汇总",
        "回顾",
        "盘点",
        "过去24小时",
        "此前",
        "当地时间6月29日",
        "早些时候",
        "上周",
        "上月",
        "数日前",
        "已公布",
        "再次提及",
        "重申",
        "维持",
    ]
    if _contains_any(title, fresh_terms):
        return "新增事实"
    if _contains_any(title, stale_terms) or _contains_any(text, stale_terms):
        return "已被市场知晓"
    if _contains_any(text, fresh_terms):
        return "新增事实"
    return "不确定"


def _is_isolated_lpg_cp(text: str) -> bool:
    lpg_terms = ["LPG", "液化石油气", "丙烷", "丁烷", "CP"]
    bridge_terms = [
        "原油供应中断",
        "原油出口中断",
        "炼厂原料",
        "替代需求",
        "跨品种套利",
        "港口中断",
        "装船中断",
        "供应链冲击",
        "全球原油供需",
        "原油风险溢价",
    ]
    return _contains_any(text, lpg_terms) and not _contains_any(text, bridge_terms)


def _brent_transmission_level(event_key: str, text: str, direct: bool, major_reason: str) -> str:
    if _is_isolated_lpg_cp(text):
        return "较弱"
    if _price_quote_only(text):
        return "较弱"
    direct_topics = {
        "hormuz_shipping",
        "tanker_shipping",
        "exports_loading",
        "sanctions_policy",
        "opec_supply",
        "crude_trade_flows",
    }
    if event_key in direct_topics:
        return "直接"
    if event_key == "geopolitical_military" and _contains_any(text, ["霍尔木兹", "海湾", "原油", "石油", "油轮", "制裁", "出口"]):
        return "直接"
    if event_key == "iran_nuclear" and _contains_any(text, ["制裁", "霍尔木兹", "原油", "石油", "出口", "油轮"]):
        return "直接"
    if event_key == "official_selling_price" and _contains_any(text, ["原油", "石油", "巴士拉", "阿拉伯轻质"]):
        return "直接"
    if event_key in {"bank_oil_forecast", "oil_market_expectations", "crude_oil_market"}:
        return "间接"
    if _macro_driver_reason(text):
        return "间接"
    return "较弱"


def _us_session_note(group: dict[str, object], timezone_name: str) -> str:
    start = group.get("start_dt")
    if not isinstance(start, datetime):
        return ""
    local_time = to_beijing(start, timezone_name).time()
    if time(20, 0) <= local_time <= time(23, 45):
        return "该时间段接近美国交易时段和EIA/API库存窗口，需优先核查库存数据、能源快讯、交易所公告、实货报价和航运数据。"
    return ""


def _candidate_attribution(candidate: dict[str, object] | None, group: dict[str, object], timezone_name: str) -> dict[str, str]:
    if not candidate:
        return {
            "judgment": "暂无法确认",
            "freshness": "不确定",
            "relation": "N/A",
            "confidence": "低",
            "reason": "附近新闻不足以支持可靠归因。",
        }
    freshness = str(candidate.get("freshness") or "不确定")
    relation = str(candidate.get("relation") or "时间未知")
    transmission = str(candidate.get("brent_transmission") or "较弱")
    score = float(candidate.get("score") or 0)
    alignment = int(candidate.get("direction_alignment") or 0)
    time_ok = relation in {"新闻先于价格", "同期发生"}
    fresh_ok = freshness == "新增事实"
    direct_ok = transmission == "直接"
    confidence = str(candidate.get("confidence") or "低")
    published = candidate.get("published_dt")
    start_dt = group.get("start_dt")
    start_sync_ok = True
    if relation == "同期发生" and isinstance(published, datetime) and isinstance(start_dt, datetime):
        start_sync_ok = published <= start_dt + timedelta(minutes=15)
    session_note = _us_session_note(group, timezone_name)
    text = _raw_text(candidate["row"])
    if not candidate.get("direct_supply_path"):
        return {
            "judgment": "背景信息",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低",
            "reason": "未直接涉及原油或能源供应端变化，不能作为Brent短线核心驱动。",
        }
    session_primary_ok = not session_note or _contains_any(
        text,
        [
            "EIA",
            "API",
            "库存",
            "战略石油储备",
            "OPEC",
            "欧佩克",
            "产量",
            "原油出口",
            "霍尔木兹",
            "油轮",
            "装船",
            "港口",
            "制裁",
            "美元",
            "美联储",
            "PMI",
            "ISM",
            "非农",
        ],
    )

    if relation == "价格先于新闻":
        return {
            "judgment": "背景信息",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低",
            "reason": "价格异动早于新闻发布时间，不能将该新闻作为短线主因。",
        }
    if relation == "同期发生" and not start_sync_ok:
        return {
            "judgment": "背景信息",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低",
            "reason": "新闻发布在本轮价格异动展开一段时间之后，更适合作为后续背景或确认，不能解释整段行情。",
        }
    if candidate.get("is_summary") or freshness == "汇总转述":
        return {
            "judgment": "背景信息",
            "freshness": "汇总转述",
            "relation": relation,
            "confidence": "低",
            "reason": "汇总或二次转述只作为背景，不能直接解释短线Brent波动。",
        }
    if freshness == "已被市场知晓":
        return {
            "judgment": "背景信息",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低",
            "reason": "信息可能已被市场提前消化，更适合作为中期背景。",
        }
    if candidate.get("isolated_lpg_cp"):
        return {
            "judgment": "背景信息",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低",
            "reason": "相关能源市场背景，暂缺乏证据证明其为Brent短线波动的核心驱动。",
        }
    if candidate.get("price_quote_only"):
        return {
            "judgment": "背景信息",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低",
            "reason": "该消息仅描述油价涨跌，缺少独立新增事实，不能反向解释Brent短线波动。",
        }
    if alignment < 0:
        return {
            "judgment": "背景信息",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低",
            "reason": "新闻逻辑与本轮Brent价格方向背离，不能作为该段短线异动的核心解释。",
        }
    if alignment == 0:
        return {
            "judgment": "背景信息",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低",
            "reason": "新闻理论方向为中性或无法判断，不能作为该段Brent异动的驱动因素。",
        }
    if direct_ok and fresh_ok and time_ok and start_sync_ok and alignment > 0 and score >= 70 and session_primary_ok:
        return {
            "judgment": "主要驱动",
            "freshness": freshness,
            "relation": relation,
            "confidence": confidence if confidence in {"高", "中"} else "中",
            "reason": "新增事实、时间顺序和Brent传导路径基本成立。",
        }
    if direct_ok and fresh_ok and time_ok and start_sync_ok and alignment > 0 and score >= 70 and not session_primary_ok:
        return {
            "judgment": "次要驱动",
            "freshness": freshness,
            "relation": relation,
            "confidence": "中" if confidence == "高" else confidence,
            "reason": f"{session_note} 该新闻可作为背景或辅助线索，但不足以单独解释美盘时段Brent短线波动。",
        }
    if direct_ok and time_ok and start_sync_ok and score >= 55:
        reason = "新闻具备直接传导路径，但新鲜度、方向一致性或同期其他因素仍需验证。"
        if session_note and not fresh_ok:
            reason = f"{session_note} 附近能源新闻只能作为辅助解释。"
        return {
            "judgment": "次要驱动",
            "freshness": freshness,
            "relation": relation,
            "confidence": "中" if confidence == "高" else confidence,
            "reason": reason,
        }
    if session_note:
        return {
            "judgment": "暂无法确认",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低",
            "reason": f"{session_note} 附近新闻缺少足够直接、及时且新增的Brent传导证据。",
        }
    if transmission == "间接" and time_ok and start_sync_ok and score >= 45:
        return {
            "judgment": "次要驱动",
            "freshness": freshness,
            "relation": relation,
            "confidence": "低" if confidence == "高" else confidence,
            "reason": "新闻可能影响市场预期，但对Brent短线传导仍偏间接。",
        }
    return {
        "judgment": "背景信息",
        "freshness": freshness,
        "relation": relation,
        "confidence": "低",
        "reason": "新闻与能源市场有关，但缺乏足够直接的短线归因证据。",
    }


def _candidate_is_sufficient_explanation(candidate: dict[str, object] | None) -> bool:
    if not candidate:
        return False
    if not candidate.get("direct_supply_path"):
        return False
    if int(candidate.get("direction_alignment") or 0) <= 0:
        return False
    judgment = str(candidate.get("attribution_judgment") or "")
    if judgment == "主要驱动":
        return True
    if judgment != "次要驱动":
        return False
    if str(candidate.get("attribution_confidence") or "低") == "低":
        return False
    if str(candidate.get("attribution_relation") or candidate.get("relation") or "") == "价格先于新闻":
        return False
    if str(candidate.get("attribution_freshness") or candidate.get("freshness") or "") != "新增事实":
        return False
    if str(candidate.get("brent_transmission") or "较弱") == "较弱":
        return False
    if candidate.get("is_summary") or candidate.get("price_quote_only") or candidate.get("isolated_lpg_cp"):
        return False
    return True


def _candidate_has_original_link(candidate: dict[str, object] | None) -> bool:
    if not candidate:
        return False
    value = str(_val(candidate["row"], "url") or "").strip()
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _score_movement_candidate(row, group: dict[str, object]) -> dict[str, object]:
    title = str(_val(row, "title") or "")
    body = str(_val(row, "body") or _val(row, "detail") or "")
    text = f"{title} {body}"
    direct, oil_reason, major_reason = _raw_oil_status(row)
    direct_supply_reason = direct_energy_supply_reason(title, body)
    direct_supply = bool(direct_supply_reason)
    event_key, event_topic = identify_event(title, body)
    source_label, source_score = _source_quality(row)
    published = parse_iso_utc(str(row["published_at_utc"]))
    relation = _relation_to_move(published, group)
    macro_reason = _macro_driver_reason(text)
    price_quote_only = _price_quote_only(text)
    is_commentary = _is_market_commentary(title)
    freshness = _freshness_label(row, text)
    isolated_lpg_cp = _is_isolated_lpg_cp(text)
    brent_transmission = _brent_transmission_level(event_key, text, direct, major_reason or "")
    if not direct_supply:
        brent_transmission = "较弱"
    score = 0
    reasons: list[str] = []

    if major_reason:
        score += 38 if "油价快速变化或市场预期调整" in major_reason else 72
        reasons.append(major_reason)
    if direct:
        score += 30
        reasons.append(oil_reason)
    if direct_supply:
        score += 30
        reasons.append(direct_supply_reason or "")
    else:
        score -= 55
        reasons.append("未直接涉及原油或能源供应端变化")
    if event_key in {
        "hormuz_shipping",
        "tanker_shipping",
        "exports_loading",
        "sanctions_policy",
        "opec_supply",
        "official_selling_price",
        "lng_supply_security",
        "iran_nuclear",
        "geopolitical_military",
    }:
        score += 38
    elif event_key in {"bank_oil_forecast", "oil_market_expectations", "crude_oil_market", "gas_lng_lpg"}:
        score += 10
    if macro_reason:
        score += 38 if not direct else 18
        reasons.append(macro_reason)
    if source_score:
        score += source_score
    if relation == "新闻先于价格":
        score += 10
    elif relation == "同期发生":
        score += 8
    else:
        score -= 30

    if group["direction"] == "上涨" and _contains_any(
        text,
        ["袭击", "封锁", "停航", "绕航", "制裁", "中断", "减产", "库存下降", "供应担忧", "风险溢价", "紧急采购"],
    ):
        score += 10
    if group["direction"] == "下跌" and _contains_any(
        text,
        ["恢复", "停火", "缓和", "增产", "库存增加", "下调", "需求放缓", "供应充足", "风险缓和"],
    ):
        score += 10
    if price_quote_only:
        score -= 22
        reasons.append("仅描述价格变化，作为背景而非核心事实")
    if is_commentary:
        score -= 32
        reasons.append("市场分析或背景评论，优先级低于原始突发/实物事件")
    if int(_val(row, "is_summary") or 0):
        score -= 20
        reasons.append("汇总类新闻，需优先寻找原始快讯")
    if freshness in {"已被市场知晓", "汇总转述"}:
        score -= 25
        reasons.append("信息新鲜度不足，不能直接作为短线主因")
    if isolated_lpg_cp:
        score -= 45
        reasons.append("LPG/CP等产品级信息缺少直接Brent传导证据")
    if brent_transmission == "较弱":
        score -= 18
        reasons.append("与Brent短线传导路径偏弱")

    logic = market_bias_for_event(title, body, event_topic)
    alignment = _direction_alignment(logic, group)
    if alignment > 0:
        score += 22
        reasons.append("新闻方向与该段Brent异动方向一致")
    elif alignment < 0:
        score -= 16
        reasons.append("新闻方向与该段Brent异动方向相反")
    tier = _candidate_tier(
        event_key,
        text,
        major_reason or "",
        macro_reason,
        bool(int(_val(row, "is_summary") or 0)),
        is_commentary,
        price_quote_only,
    )
    if not direct_supply:
        tier = 3
    if score >= 85 and source_label in {"官方/权威机构", "主流媒体"} and relation != "价格先于新闻":
        confidence = "高"
    elif score >= 58 and relation != "价格先于新闻":
        confidence = "中"
    else:
        confidence = "低"
    if int(_val(row, "is_summary") or 0):
        confidence = "低"

    return {
        "raw_id": str(row["raw_id"]),
        "row": row,
        "published_dt": published,
        "score": score,
        "direct": direct,
        "oil_reason": oil_reason,
        "major_reason": major_reason,
        "direct_supply_path": direct_supply,
        "direct_supply_reason": direct_supply_reason or "",
        "event_key": event_key,
        "event_topic": event_topic,
        "source_label": source_label,
        "relation": relation,
        "macro_reason": macro_reason,
        "price_quote_only": price_quote_only,
        "is_commentary": is_commentary,
        "is_summary": bool(int(_val(row, "is_summary") or 0)),
        "freshness": freshness,
        "isolated_lpg_cp": isolated_lpg_cp,
        "brent_transmission": brent_transmission,
        "tier": tier,
        "direction_alignment": alignment,
        "logic": logic,
        "confidence": confidence,
        "reasons": "；".join(dict.fromkeys(reason for reason in reasons if reason)) or "未命中明确油市/宏观驱动规则",
    }


def _candidate_reaction_status(
    candidate: Mapping[str, object],
    reaction,
    report_end_utc: datetime,
) -> tuple[str, str]:
    published = _candidate_published_dt(dict(candidate))
    return _reaction_observation_status(published, reaction, report_end_utc)


def _build_price_movement_review(
    store: TrackerStore,
    brent: BrentConfig,
    start_utc: datetime,
    end_utc: datetime,
    timezone_name: str,
) -> dict[str, object]:
    windows, price_rows = _scan_30m_price_windows(store, brent, start_utc, end_utc)
    metric_price_rows = store.brent_day_klines(
        brent.symbol,
        brent.interval,
        start_utc,
        end_utc,
    )
    groups = _merge_price_windows(windows, metric_price_rows)
    reaction_rows = store.reaction_rows_between(
        start_utc - timedelta(minutes=45),
        end_utc + timedelta(minutes=15),
    )
    reaction_by_id = {str(_val(row, "news_id") or ""): row for row in reaction_rows}
    selected_raw_ids: set[str] = set()
    core_raw_ids: set[str] = set()
    unexplained: list[dict[str, object]] = []

    for group in groups:
        search_start = ensure_utc(group["search_start_dt"])
        search_end = ensure_utc(group["search_end_dt"])
        nearby = store.raw_news_between(search_start, search_end)
        candidates = [_score_movement_candidate(row, group) for row in nearby]
        for item in candidates:
            reaction = reaction_by_id.get(str(item.get("raw_id") or ""))
            item["is_duplicate"] = bool(int(_val(reaction, "is_duplicate") or 0)) if reaction else False
            attribution = _candidate_attribution(item, group, timezone_name)
            reaction_status, reaction_status_note = _candidate_reaction_status(
                item,
                reaction,
                end_utc,
            )
            item.update(
                {
                    "attribution_judgment": attribution["judgment"],
                    "attribution_freshness": attribution["freshness"],
                    "attribution_relation": attribution["relation"],
                    "attribution_confidence": attribution["confidence"],
                    "attribution_reason": attribution["reason"],
                    "reaction_status": reaction_status,
                    "reaction_status_note": reaction_status_note,
                }
            )
        attribution_priority = {"主要驱动": 4, "次要驱动": 3, "背景信息": 2, "暂无法确认": 1}
        for item in candidates:
            item["attribution_priority"] = attribution_priority.get(str(item.get("attribution_judgment") or ""), 0)
        candidates.sort(
            key=lambda item: (
                attribution_priority.get(str(item.get("attribution_judgment") or ""), 0),
                -int(item.get("tier") or 3),
                int(item.get("direction_alignment") or 0),
                float(item["score"]),
                0 if item["is_summary"] else 1,
                0 if item["relation"] == "价格先于新闻" else 1,
                item["published_dt"] or start_utc,
            ),
            reverse=True,
        )
        main_pool = [item for item in candidates if item.get("attribution_judgment") == "主要驱动"]
        secondary_pool = [
            item
            for item in candidates
            if item.get("attribution_judgment") == "次要驱动" and _candidate_is_sufficient_explanation(item)
        ]
        background_pool = [item for item in candidates if item.get("attribution_judgment") == "背景信息"]
        best_background = background_pool[0] if background_pool else None
        core = main_pool[0] if main_pool else secondary_pool[0] if secondary_pool else None
        selected_news = [
            item
            for item in candidates
            if (
                item.get("attribution_judgment") == "主要驱动"
                or (
                    item.get("attribution_judgment") == "次要驱动"
                    and _candidate_is_sufficient_explanation(item)
                )
            )
            and _candidate_has_original_link(item)
            and int(item.get("direction_alignment") or 0) > 0
            and item.get("brent_transmission") == "直接"
            and not _is_institution_forecast_noise(item["row"])
            and not _is_formal_report_noise(item["row"])
            and not item.get("is_commentary")
            and not item.get("price_quote_only")
        ]
        for item in candidates:
            if item.get("reaction_status") == "CONFIRMED":
                continue
            item["original_attribution_judgment"] = item.get("attribution_judgment")
            item["attribution_judgment"] = "暂无法确认"
            item["attribution_confidence"] = "低"
            item["attribution_reason"] = str(
                item.get("reaction_status_note") or UNCONFIRMED_REACTION_NOTE
            )
        confirmed_selected_news = [
            item for item in selected_news if item.get("reaction_status") == "CONFIRMED"
        ]
        core = confirmed_selected_news[0] if confirmed_selected_news else None
        secondaries = (
            [
                item
                for item in confirmed_selected_news
                if item["raw_id"] != core["raw_id"]
            ]
            if core
            else []
        )
        attribution_judgment = str(core.get("attribution_judgment")) if core else "暂无法确认"
        attribution_confidence = str(core.get("attribution_confidence")) if core else "低"
        attribution_reason = (
            str(core.get("attribution_reason"))
            if core
            else str(best_background.get("attribution_reason"))
            if best_background
            else "附近新闻不足以支持可靠归因。"
        )
        attribution_freshness = str(core.get("attribution_freshness")) if core else "不确定"
        attribution_relation = str(core.get("attribution_relation")) if core else "N/A"
        for item in selected_news:
            selected_raw_ids.add(str(item["raw_id"]))
        if core and attribution_judgment == "主要驱动":
            core_raw_ids.add(str(core["raw_id"]))
        for item in candidates:
            item["exclusion_reason"] = _candidate_exclusion_reason(item, core)
        homepage_news = [
            item
            for item in candidates
            if _homepage_candidate_is_eligible(item)
            and int(item.get("direction_alignment") or 0) > 0
        ]
        homepage_news.sort(
            key=lambda item: (
                _candidate_published_dt(item),
                -_candidate_attribution_priority(item),
                str(item.get("raw_id") or ""),
            )
        )
        group["nearby_news"] = nearby
        group["candidates"] = candidates
        group["homepage_news"] = homepage_news
        group["core_driver"] = core
        group["secondary_drivers"] = secondaries
        group["selected_news"] = selected_news
        group["pricing_status"] = _pricing_status_for_candidate(core, group)
        group["attribution_judgment"] = attribution_judgment
        group["attribution_confidence"] = attribution_confidence
        group["attribution_reason"] = attribution_reason
        group["attribution_freshness"] = attribution_freshness
        group["attribution_relation"] = attribution_relation
        if not core:
            unexplained.append(group)

    return {
        "windows": windows,
        "price_rows": metric_price_rows,
        "groups": groups,
        "unexplained": unexplained,
        "selected_raw_ids": selected_raw_ids,
        "core_raw_ids": core_raw_ids,
        "supplemental_window_used": bool(windows) and not any(abs(float(item["pct"])) >= 0.005 for item in windows),
        "timezone": timezone_name,
    }


def _quarantine_continuity_movements(
    review: dict[str, object],
    audit: Mapping[str, object],
) -> dict[str, object]:
    # Compatibility layer: continuity jumps are retained as audit diagnostics,
    # but they never remove prices, movements, chart markers, or matched news.
    review["quarantined_groups"] = []
    review["quarantined_windows"] = []
    return review


def _degrade_movement_review_for_incomplete_data(review: dict[str, object], audit: dict[str, object]) -> dict[str, object]:
    if bool(audit.get("data_complete", True)):
        return review

    reason = (
        "新闻或价格窗口覆盖不完整；本段仅为缓存数据复盘，"
        "不能确认核心驱动。"
    )
    groups = list(review.get("groups") or [])
    for group in groups:
        for item in list(group.get("candidates") or []):
            if item.get("attribution_judgment") in {"主要驱动", "次要驱动"}:
                item["attribution_judgment"] = "暂无法确认"
            item["attribution_confidence"] = "低"
            item["attribution_reason"] = reason

        group["cache_reference_driver"] = group.get("core_driver")
        group["pricing_status"] = "无法判断"
        group["attribution_judgment"] = "暂无法确认"
        group["attribution_confidence"] = "低"
        group["attribution_reason"] = reason
        group["attribution_freshness"] = "不确定"
        group["attribution_relation"] = "N/A"

    review["degraded"] = True
    review["degradation_reason"] = reason
    review["core_raw_ids"] = set()
    review["unexplained"] = [
        group for group in groups if not list(group.get("selected_news") or [])
    ]
    return review


def _candidate_title(candidate: dict[str, object]) -> str:
    row = candidate["row"]
    title = str(_val(row, "title") or "").strip()
    return title or str(_val(row, "body") or "").strip()[:120]


def _movement_time_range(group: dict[str, object], timezone_name: str) -> str:
    return f"{_bj_from_dt(group.get('start_dt'), timezone_name)} 至 {_bj_from_dt(group.get('end_dt'), timezone_name)}"


def _brief_text(text: str, limit: int = 170) -> str:
    text = " ".join(str(text or "").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _candidate_summary(candidate: dict[str, object] | None) -> str:
    if not candidate:
        return "暂未找到符合条件的金十新闻，原因暂无法确认，需人工继续核查。"
    row = candidate["row"]
    body = str(_val(row, "body") or _val(row, "detail") or "")
    title = _candidate_title(candidate)
    text = body if body and body != title else title
    if text.startswith("【") and "】" in text:
        text = text.split("】", 1)[1].strip()
    if text.startswith("金十数据"):
        for marker in ("日讯，", "日讯：", "日讯"):
            if marker in text[:24]:
                text = text.split(marker, 1)[1].strip(" ，。:：")
                break
    return _brief_text(text, 115)


def _candidate_url(candidate: dict[str, object] | None) -> str:
    if not candidate:
        return "N/A"
    return str(_val(candidate["row"], "url") or "").strip() or "N/A"


def _movement_clock_range(group: dict[str, object], timezone_name: str) -> str:
    return f"{_bj_hhmm(iso_utc(group['start_dt']), timezone_name)}–{_bj_hhmm(iso_utc(group['end_dt']), timezone_name)}"


def _core_selection_reason(candidate: dict[str, object] | None, group: dict[str, object]) -> str:
    if not candidate:
        return "未找到评分和传导路径足够明确的核心新闻。"
    judgment = str(candidate.get("attribution_judgment") or group.get("attribution_judgment") or "暂无法确认")
    reason = str(candidate.get("attribution_reason") or group.get("attribution_reason") or "")
    tier = int(candidate.get("tier") or 3)
    tier_text = "一线实物/政策/风险溢价事件" if tier == 1 else "具备明确传导路径的市场事件" if tier == 2 else "背景/辅助信息"
    alignment = int(candidate.get("direction_alignment") or 0)
    if alignment > 0:
        align_text = "新闻方向与本轮价格方向一致"
    elif alignment < 0:
        align_text = "新闻方向与本轮价格方向背离"
    else:
        align_text = "新闻方向偏中性或需结合其他因素判断"
    return f"{judgment}；{tier_text}；{candidate['relation']}；{align_text}；{reason}"


def _secondary_digest(group: dict[str, object], timezone_name: str, limit: int = 2) -> str:
    items = list(group.get("secondary_drivers") or [])[:limit]
    if not items:
        return "无明显次要驱动。"
    chunks = []
    for item in items:
        row = item["row"]
        chunks.append(f"{_raw_bj(row, timezone_name)[11:16]} {_brief_text(_candidate_title(item), 46)}")
    return "；".join(chunks)


def _movement_interpretation(group: dict[str, object], timezone_name: str) -> str:
    core = group.get("core_driver")
    if not core:
        pending_items = [
            item
            for item in list(group.get("selected_news") or [])
            if item.get("reaction_status") == "PENDING"
        ]
        if pending_items:
            return PENDING_REACTION_NOTE
        if list(group.get("selected_news") or []):
            return UNCONFIRMED_REACTION_NOTE
        return "暂未找到符合条件的金十新闻，原因暂无法确认，需人工继续核查。"
    direction = "上涨" if float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0) >= 0 else "下跌"
    relation = str(core.get("relation") or "时间关系待确认")
    pricing = str(group.get("pricing_status") or "无法判断")
    judgment = str(group.get("attribution_judgment") or core.get("attribution_judgment") or "暂无法确认")
    reason = str(group.get("attribution_reason") or core.get("attribution_reason") or "")
    if judgment in {"背景信息", "暂无法确认"}:
        return (
            f"Brent本轮{direction}与附近新闻时间接近，但目前更适合视为“{judgment}”；{reason}"
            "不能确认该新闻是短线价格异动的主要原因。"
        )
    return (
        f"市场可能在交易：{core['logic']} {relation}，Brent本轮{direction}与新闻逻辑的匹配状态为“{pricing}”。"
        f"归因判断为“{judgment}”，仍需结合同期库存、出口、装船、船运和官方文件验证。"
    )


def _price_movement_review_lines(review: dict[str, object], timezone_name: str) -> list[str]:
    groups = list(review.get("groups") or [])
    lines = ["## 本窗口Brent价格异动复盘", ""]
    if review.get("degraded"):
        lines.append("> 价格异动复盘已降级为缓存数据复盘，不代表完整窗口；所有短线归因均按低置信度处理。")
        lines.append("")
    if not groups:
        lines.append("本窗口未识别到满足阈值的Brent短时间价格异动，因此不强行进行新闻归因。")
        lines.append("")
        return lines

    explained_groups = [group for group in groups if list(group.get("selected_news") or [])]
    if not explained_groups:
        lines.append("本窗口存在符合阈值的Brent价格异动，但金十反查未找到满足供给端硬门槛的新闻，已全部列入下一部分。")
        lines.append("")

    for group in explained_groups:
        pct = float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0.0)
        direction_word = "上涨" if pct >= 0 else "下跌"
        max_30m = float(group.get("max_30m_pct") or 0.0)
        max_label = "最大30分钟涨幅" if max_30m >= 0 else "最大30分钟跌幅"
        lines.append(
            f"### 北京时间 {_movement_clock_range(group, timezone_name)}｜Brent{direction_word} {_fmt_pct(pct)}｜"
            f"{max_label} {_fmt_pct(max_30m)}"
        )
        lines.append("")
        lines.append("**油价反应：**")
        lines.append(
            f"5分钟：{_fmt_pct(group.get('pct_5m'))}｜15分钟：{_fmt_pct(group.get('pct_15m'))}｜"
            f"30分钟：{_fmt_pct(group.get('pct_30m'))}｜60分钟：{_fmt_pct(group.get('pct_60m'))}｜"
            f"本轮累计：{_fmt_pct(group.get('cumulative_pct'))}"
        )
        lines.append("")
        lines.append("**触发标准：**")
        lines.append(str(group.get("trigger_label") or "N/A"))
        lines.append("")
        lines.append("**相关新闻：**")
        for index, candidate in enumerate(list(group.get("selected_news") or []), 1):
            transmission = str(candidate.get("direct_supply_reason") or candidate.get("oil_reason") or candidate.get("logic") or "直接能源供应传导路径")
            lines.extend(
                [
                    f"{index}. 新闻摘要：{_candidate_summary(candidate)}",
                    f"   原文链接：{_candidate_url(candidate)}",
                    f"   时间关系：{candidate.get('attribution_relation') or candidate.get('relation') or 'N/A'}",
                    f"   信息新鲜度：{candidate.get('attribution_freshness') or candidate.get('freshness') or '不确定'}",
                    f"   传导路径：{transmission}；{candidate.get('logic') or '需结合实物流数据验证'}",
                    f"   反应状态：{candidate.get('reaction_status') or 'UNCONFIRMED'}",
                    *(
                        [f"   状态说明：{candidate.get('reaction_status_note')}"]
                        if candidate.get("reaction_status_note")
                        else []
                    ),
                    f"   归因判断：{candidate.get('attribution_judgment') or '暂无法确认'}",
                    f"   置信度：{candidate.get('attribution_confidence') or '低'}",
                    "",
                ]
            )
        lines.append("**市场解读：**")
        lines.append(_movement_interpretation(group, timezone_name))
        lines.append("")
    return lines


def _unexplained_price_movement_lines(
    review: dict[str, object],
    timezone_name: str,
    include_chart_ids: bool = False,
) -> list[str]:
    unexplained = [
        group
        for group in list(review.get("unexplained") or [])
        if not list(group.get("selected_news") or [])
    ]
    lines = ["## 价格异动但新闻解释不足", ""]
    if not unexplained:
        lines.append("本窗口没有需要单列的“价格异动但新闻解释不足”事件。")
        lines.append("")
        return lines
    lines.extend([f"本窗口有{len(unexplained)}段显著价格异动尚无正式新闻解释。", ""])
    displayed_background_ids: set[str] = set()
    for group in unexplained:
        chart_event_id = str(group.get("chart_event_id") or "")
        event_ref = f"{chart_event_id}｜" if include_chart_ids and chart_event_id.startswith("U") else ""
        lines.append(f"### {event_ref}北京时间 {_movement_clock_range(group, timezone_name)}｜Brent{group.get('direction')} {_fmt_pct(group.get('cumulative_pct'))}")
        lines.append("")
        lines.append("**油价反应：**")
        lines.append(
            f"5分钟：{_fmt_pct(group.get('pct_5m'))}｜15分钟：{_fmt_pct(group.get('pct_15m'))}｜"
            f"30分钟：{_fmt_pct(group.get('pct_30m'))}｜60分钟：{_fmt_pct(group.get('pct_60m'))}｜"
            f"本轮累计：{_fmt_pct(group.get('cumulative_pct'))}"
        )
        background_pool = list(group.get("homepage_news") or [])
        if not background_pool:
            background_pool = list(group.get("candidates") or [])
        background_pool = [
            candidate
            for candidate in background_pool
            if _homepage_candidate_is_eligible(candidate)
            and str(candidate.get("raw_id") or "") not in displayed_background_ids
        ]
        group_start = ensure_utc(group["start_dt"])
        group_end = ensure_utc(group["end_dt"])

        def background_rank(candidate: dict[str, object]) -> tuple[int, float, datetime, str]:
            published = _candidate_published_dt(candidate)
            if group_start <= published <= group_end:
                distance = 0.0
            else:
                distance = min(abs((published - group_start).total_seconds()), abs((published - group_end).total_seconds()))
            return (
                -_candidate_attribution_priority(candidate),
                distance,
                published,
                str(candidate.get("raw_id") or ""),
            )

        background_news = sorted(background_pool, key=background_rank)[:2]
        lines.extend(["", "**说明：**"])
        if background_news:
            lines.append("未找到足以可靠解释该段波动的金十新闻；以下仅为同期相关背景，不作价格归因。")
            lines.extend(["", "**相关背景（未作价格归因）：**"])
            movement_range = _movement_clock_range(group, timezone_name)
            for index, candidate in enumerate(background_news, 1):
                displayed_background_ids.add(str(candidate.get("raw_id") or ""))
                transmission = str(
                    candidate.get("direct_supply_reason")
                    or candidate.get("oil_reason")
                    or candidate.get("logic")
                    or "直接影响原油供应或能源运输"
                )
                lines.extend(
                    [
                        f"{index}. 新闻摘要：{_candidate_summary(candidate)}",
                        f"   发布时间：{_bj_from_dt(_candidate_published_dt(candidate), timezone_name)} 北京时间",
                        f"   原文链接：{_candidate_url(candidate)}",
                        f"   时间关系：{candidate.get('attribution_relation') or candidate.get('relation') or '同期相关'}",
                        f"   信息新鲜度：{candidate.get('attribution_freshness') or candidate.get('freshness') or '新增事实'}",
                        f"   传导路径：{transmission}",
                        f"   对应价格异动：北京时间 {movement_range}",
                        "   归因判断：相关背景，未作价格归因",
                        "   置信度：低",
                        "",
                    ]
                )
        else:
            lines.append("暂未找到符合条件的金十新闻，原因暂无法确认，需人工继续核查。")
        lines.extend(
            [
                "",
                "**可能原因：**",
                "美元、美债、美国市场开盘、资金流、技术交易、外盘消息、新闻抓取缺口或非金十来源事件。",
                "",
                "归因判断：暂无法确认",
                "置信度：低",
            ]
        )
        lines.append("")
    lines.append("")
    return lines


def _continuity_quarantine_lines(review: Mapping[str, object], timezone_name: str) -> list[str]:
    # Retained for backward-compatible callers; extreme moves are never
    # quarantined, so this section intentionally emits no reader-facing output.
    return []


def _movement_debug_lines(review: dict[str, object], timezone_name: str) -> list[str]:
    windows = list(review.get("windows") or [])
    groups = list(review.get("groups") or [])
    component_to_group: dict[tuple[str, str], dict[str, object]] = {}
    for group in groups:
        for component in list(group.get("component_windows") or []):
            component_to_group[(str(component.get("start_utc")), str(component.get("end_utc")))] = group
    lines = ["## 5. Brent价格异动扫描与新闻反查", ""]
    lines.append(f"- 识别出的30分钟异常/补充波动区间数量：{len(windows)}")
    for idx, item in enumerate(windows, 1):
        group = component_to_group.get((str(item.get("start_utc")), str(item.get("end_utc"))))
        merged = "是" if group and len(group.get("component_windows") or []) > 1 else "否"
        lines.append(
            f"  - {idx}. {_bj_from_dt(item.get('start_dt'), timezone_name)} 至 {_bj_from_dt(item.get('end_dt'), timezone_name)}："
            f"{_fmt_pct(item.get('pct'))}；{item.get('trigger_label')}；已合并：{merged}"
        )
    lines.append(f"- 合并后的独立价格异动事件组数量：{len(groups)}")
    for idx, group in enumerate(groups, 1):
        core = group.get("core_driver")
        core_title = _candidate_title(core) if core else "暂无法确认"
        lines.extend(
            [
                "",
                f"### 价格异动事件组{idx}：{_movement_time_range(group, timezone_name)}",
                "",
                f"- 价格方向：{group.get('direction')}",
                f"- 本轮累计变化：{_fmt_pct(group.get('cumulative_pct'))}",
                f"- 最大30分钟涨跌幅：{_fmt_pct(group.get('max_30m_pct'))}",
                f"- 5分钟/15分钟/30分钟/60分钟变化：{_fmt_pct(group.get('pct_5m'))} / {_fmt_pct(group.get('pct_15m'))} / {_fmt_pct(group.get('pct_30m'))} / {_fmt_pct(group.get('pct_60m'))}",
                f"- 触发标准：{group.get('trigger_label')}",
                f"- 合并窗口数量：{len(group.get('component_windows') or [])}",
                f"- 新闻反查区间：{_bj_from_dt(group.get('search_start_dt'), timezone_name)} 至 {_bj_from_dt(group.get('search_end_dt'), timezone_name)}",
                f"- 最终识别核心驱动：{core_title}",
                f"- 最终归因判断：{group.get('attribution_judgment') or '暂无法确认'}",
                f"- 归因置信度：{group.get('attribution_confidence') or '低'}",
                f"- 归因理由：{_core_selection_reason(core, group)}",
            ]
        )
        secondary_titles = [
            _candidate_title(item)
            for item in list(group.get("secondary_drivers") or [])
        ]
        if secondary_titles:
            lines.append(f"- 次要/同期线索：{'；'.join(_brief_text(title, 42) for title in secondary_titles)}")
        else:
            lines.append("- 次要/同期线索：无明显高相关候选。")
        candidates = list(group.get("candidates") or [])
        if candidates:
            lines.append("- 反查到的候选新闻：")
            for item in candidates:
                row = item["row"]
                lines.append(
                    f"  - {_raw_bj(row, timezone_name)} | {_brief_text(_candidate_title(item), 90)} | "
                    f"分类：{item.get('event_topic')} | 分数：{item.get('score')} | 来源：{item.get('source_label')} | "
                    f"供给端硬门槛：{'是' if item.get('direct_supply_path') else '否'} | "
                    f"新鲜度：{item.get('attribution_freshness') or item.get('freshness')} | 时间关系：{item.get('attribution_relation') or item.get('relation')} | "
                    f"Brent传导：{item.get('brent_transmission')} | 归因：{item.get('attribution_judgment')} | "
                    f"置信度：{item.get('attribution_confidence')} | 未选/状态：{item.get('exclusion_reason')}"
                )
        else:
            lines.append("- 反查到的候选新闻：无。")
    selected = set(review.get("selected_raw_ids") or set())
    lines.append(f"- 进入价格异动复盘正文的核心/次要原始新闻数量：{len(selected)}")
    lines.append("")
    return lines


def _attribution_rationality_lines(review: dict[str, object], timezone_name: str) -> list[str]:
    groups = list(review.get("groups") or [])
    lines = ["## 6. 归因合理性审计", ""]
    if not groups:
        lines.append("- 本窗口无可审计的独立价格异动事件组。")
        lines.append("")
        return lines
    manual_flags: list[str] = []
    for idx, group in enumerate(groups, 1):
        core = group.get("core_driver")
        lines.append(f"### 归因组{idx}：{_movement_time_range(group, timezone_name)}")
        lines.append("")
        if core:
            row = core["row"]
            reason = str(core.get("attribution_reason") or group.get("attribution_reason") or "")
            lines.extend(
                [
                    f"- 代表新闻：{_raw_bj(row, timezone_name)} {_candidate_title(core)}",
                    f"- 新闻是否为新增事实：{core.get('attribution_freshness') or core.get('freshness')}",
                    f"- 新闻是否在价格异动前/同步发布：{core.get('attribution_relation') or core.get('relation')}",
                    f"- 是否存在直接Brent传导路径：{core.get('brent_transmission')}",
                    f"- 是否满足供给端硬门槛：{'是' if core.get('direct_supply_path') else '否'}",
                    f"- 是否已被市场消化：{'是' if (core.get('attribution_freshness') == '已被市场知晓') else '否/不确定'}",
                    f"- 是否为LPG/CP或局部能源背景：{'是' if core.get('isolated_lpg_cp') else '否'}",
                    f"- 是否可能受美盘库存窗口或能源流动性影响：{_us_session_note(group, timezone_name) or '未处于典型美盘能源交易增强窗口，仍需结合外盘验证。'}",
                    f"- 归因判断：{group.get('attribution_judgment') or core.get('attribution_judgment')}",
                    f"- 归因置信度：{group.get('attribution_confidence') or core.get('attribution_confidence')}",
                    f"- 归因成立/不成立原因：{reason}",
                ]
            )
            if core.get("isolated_lpg_cp") or core.get("attribution_freshness") in {"已被市场知晓", "汇总转述"}:
                manual_flags.append(
            f"{_movement_time_range(group, timezone_name)} 使用了LPG/CP、已知信息或汇总转述作为解释，需人工复核。"
                )
        else:
            candidates = list(group.get("candidates") or [])
            lines.extend(
                [
                    "- 代表新闻：无可靠核心驱动。",
                    "- 不能牵强归因原因：附近新闻未同时满足新增事实、时间顺序合理、Brent传导路径直接和方向一致。",
                    f"- 归因判断：{group.get('attribution_judgment') or '暂无法确认'}",
                    f"- 归因置信度：{group.get('attribution_confidence') or '低'}",
                    f"- 候选新闻数量：{len(candidates)}",
                ]
            )
            if candidates:
                top = candidates[0]
                lines.append(
                    f"- 最相关但未采纳新闻：{_raw_bj(top['row'], timezone_name)} {_candidate_title(top)}；"
                    f"{top.get('attribution_reason') or top.get('reasons')}"
                )
        lines.append("")
    if manual_flags:
        lines.append("### 需人工复核的潜在牵强归因")
        lines.append("")
        for item in manual_flags:
            lines.append(f"- {item}")
    else:
        lines.append("### 需人工复核的潜在牵强归因")
        lines.append("")
        lines.append("- 未发现把LPG/CP、已知信息或汇总转述强行作为核心归因的情况。")
    lines.append("")
    return lines


def _movement_driver_lines(
    review: dict[str, object],
    brent_summary: dict[str, str],
    timezone_name: str,
    audit: dict[str, object] | None = None,
) -> list[str]:
    audit = audit or {}
    data_complete = bool(audit.get("data_complete", True))
    groups = list(review.get("groups") or [])
    core_groups = [group for group in groups if group.get("core_driver")]
    completeness_warning = (
        "当前新闻或价格数据未完整覆盖本窗口，以下判断仅基于已获取数据，不能视为完整市场复盘。"
        if not data_complete
        else ""
    )
    if not groups:
        return [
            "### 本窗口市场主线",
            "",
            completeness_warning or "本窗口未识别到可复盘的Brent独立价格异动，暂不形成方向性市场主线。",
            "",
            "### 下一时段判断",
            "",
            "- 基准情景：等待新的实物供应、航运或政策信号确认。",
            "- 偏多触发：油轮滞留、护航升级、港口装船中断、制裁加码、出口受阻或OPEC+意外减产。",
            "- 偏空触发：海峡通行恢复、出口恢复、军事局势降温、OSP下调扩大或库存增加。",
            "- 重点验证：AIS船位、港口装船、官方制裁文件、OPEC+表态、EIA/API库存、实货报价。",
        ]

    attributed_groups = [
        group
        for group in groups
        if group.get("core_driver") and group.get("attribution_judgment") in {"主要驱动", "次要驱动"}
    ]
    unconfirmed_groups = [
        group
        for group in groups
        if group.get("attribution_judgment") in {"背景信息", "暂无法确认"}
    ]
    pct = brent_summary.get("pct", "N/A")
    if pct.startswith("+"):
        day_direction = "整体偏强"
    elif pct.startswith("-"):
        day_direction = "整体偏弱"
    else:
        day_direction = "整体震荡"
    topics = Counter(str(group["core_driver"]["event_topic"]) for group in attributed_groups)
    dominant = topics.most_common(1)[0][0] if topics else "价格异动但核心新闻解释不足"
    up_groups = sum(1 for group in groups if float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0) > 0)
    down_groups = len(groups) - up_groups
    if up_groups > down_groups:
        main_bias = "上行异动更多，市场更倾向交易供应、航运、地缘或风险溢价。"
    elif down_groups > up_groups:
        main_bias = "下行异动更多，市场更倾向交易风险缓和、供应竞争、库存或需求压力。"
    else:
        main_bias = "多空异动相对均衡，市场更像在围绕库存、供应和地缘消息做短线切换。"

    window_digest = []
    for group in groups:
        pct_value = float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0)
        verb = "涨" if pct_value >= 0 else "跌"
        window_digest.append(f"{_movement_clock_range(group, timezone_name)}{verb}{abs(pct_value) * 100:.2f}%")

    lines = ["### 本窗口市场主线", ""]
    if completeness_warning:
        lines.extend([completeness_warning, ""])
    if not attributed_groups:
        lines.append(
            f"Brent本窗口{day_direction}，窗口涨跌幅为{pct}；识别到{len(groups)}段独立价格异动（{'；'.join(window_digest)}），但未找到足够明确、及时且新增的金十新闻驱动，市场主线置信度为低。"
        )
    else:
        lines.append(
            f"Brent本窗口{day_direction}，窗口涨跌幅为{pct}；识别到{len(groups)}段独立价格异动（{'；'.join(window_digest)}）。可归因线索主要集中在{dominant}，{main_bias}"
        )
        if unconfirmed_groups:
            lines.append(f"其中 {len(unconfirmed_groups)} 段异动仅能归为背景信息或暂无法确认，未纳入主要驱动判断。")
    lines.extend(["", "### 核心驱动窗口", ""])

    ranked_groups = sorted(
        attributed_groups,
        key=lambda group: abs(float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0)),
        reverse=True,
    )[:3]
    if not ranked_groups:
        lines.append("暂无足够明确的核心新闻驱动；请优先查看“价格异动但新闻解释不足”。")
        lines.append("")
    for idx, group in enumerate(ranked_groups, 1):
        core = group["core_driver"]
        direction = "偏多" if float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0) > 0 else "偏空"
        confidence = "低" if not data_complete else core["confidence"]
        lines.append(
            f"{idx}. {_movement_clock_range(group, timezone_name)}，Brent{group.get('direction')}{_fmt_pct(group.get('cumulative_pct'))}："
            f"{_brief_text(_candidate_title(core), 72)}；归因判断{group.get('attribution_judgment')}；油价判断{direction}；定价状态{group.get('pricing_status')}；置信度{group.get('attribution_confidence') or confidence}。"
        )
    lines.extend(["", "### 新闻与价格是否一致", ""])
    inconsistent = [
        group
        for group in attributed_groups
        if group.get("pricing_status") == "与新闻逻辑背离"
    ]
    if inconsistent:
        group = inconsistent[0]
        core = group["core_driver"]
        lines.append(
            f"{_candidate_title(core)} 的理论方向与该段Brent表现不一致，说明同期可能存在库存、供应、航运或其他能源消息覆盖了该新闻逻辑。"
        )
    elif unconfirmed_groups:
        lines.append(
            "部分Brent异动附近虽有能源新闻，但新鲜度、时间顺序或Brent直接传导证据不足，不能把时间接近写成因果关系。"
        )
    else:
        lines.append("可归因价格异动组的新闻逻辑与Brent方向大体一致，但仍不能确认单一新闻因果。")

    lines.extend(
        [
            "",
            "### 下一时段判断",
            "",
            "- 基准情景：若没有新的实物供应、航运或政策证据，Brent更可能围绕库存、供应和地缘消息做区间波动。",
            "- 偏多触发：确认油轮滞留、护航升级、港口装船中断、制裁加码、出口受阻、LNG/原油供应中断或OPEC+意外减产。",
            "- 偏空触发：海峡通行恢复、出口恢复、军事局势降温、OSP下调扩大、库存增加或炼厂开工回落。",
            "- 重点验证：AIS船位、港口装船、官方制裁或豁免文件、海军护航安排、OPEC+官方表态、EIA/API库存。",
            "- 当前未充分定价风险：运费、保险费、船东风险偏好和装船排队可能滞后于新闻反映。",
        ]
    )
    return lines


def _is_night_bj(value: datetime | None, timezone_name: str) -> bool:
    if not value:
        return False
    local = to_beijing(value, timezone_name)
    return local.time() >= time(16, 0) or local.time() < ROLLING_CUTOFF


def _night_audit_lines(
    audit: dict[str, object],
    selected_rows: Iterable,
    timezone_name: str,
    selected_raw_ids: Iterable[str] | None = None,
    movement_groups: Iterable[dict[str, object]] | None = None,
    homepage_destinations: Mapping[str, Sequence[str]] | None = None,
    homepage_representations: Mapping[str, str] | None = None,
) -> list[str]:
    raw_rows = list(audit.get("raw_rows") or [])
    segments = audit.get("segments") or {}
    destinations = {
        str(raw_id): list(labels)
        for raw_id, labels in (homepage_destinations or {}).items()
        if labels
    }
    representations = {
        str(raw_id): str(representative_id)
        for raw_id, representative_id in (homepage_representations or {}).items()
    }
    night_rows = []
    night_oil = []
    night_major = []
    for row in raw_rows:
        published = parse_iso_utc(str(row["published_at_utc"]))
        if not _is_night_bj(published, timezone_name):
            continue
        night_rows.append(row)
        direct, oil_reason, major_reason = _raw_oil_status(row)
        if direct:
            night_oil.append((row, oil_reason, major_reason))
        if major_reason:
            night_major.append((row, oil_reason, major_reason))

    selected_night = []
    selected_ids = {str(row["news_id"]) for row in selected_rows}
    selected_raw_set = {str(item) for item in (selected_raw_ids or [])}
    for row in selected_rows:
        published = parse_iso_utc(str(row["published_at_utc"]))
        if _is_night_bj(published, timezone_name):
            selected_night.append(row)
    selected_night_raw = [
        row
        for row in raw_rows
        if str(_val(row, "raw_id") or "") in selected_raw_set
        and _is_night_bj(parse_iso_utc(str(row["published_at_utc"])), timezone_name)
    ]
    homepage_night_major = [
        item
        for item in night_major
        if str(_val(item[0], "raw_id") or "") in destinations
        or str(_val(item[0], "raw_id") or "") in representations
    ]
    night_movement_groups = []
    for group in list(movement_groups or []):
        raw_group_start = group.get("start_dt") or group.get("start_utc")
        raw_group_end = group.get("end_dt") or group.get("end_utc")
        if not raw_group_start or not raw_group_end:
            continue
        group_start = raw_group_start if isinstance(raw_group_start, datetime) else parse_iso_utc(str(raw_group_start))
        group_end = raw_group_end if isinstance(raw_group_end, datetime) else parse_iso_utc(str(raw_group_end))
        start_local = to_beijing(group_start, timezone_name)
        end_local = to_beijing(group_end, timezone_name)
        crosses_1600 = start_local.time() < time(16, 0) <= end_local.time()
        if _is_night_bj(group_start, timezone_name) or _is_night_bj(group_end, timezone_name) or crosses_1600:
            night_movement_groups.append(group)

    excluded_major = [
        item
        for item in night_major
        if str(_val(item[0], "raw_id") or "") not in selected_ids
        and str(_val(item[0], "raw_id") or "") not in selected_raw_set
        and str(_val(item[0], "raw_id") or "") not in destinations
        and str(_val(item[0], "raw_id") or "") not in representations
    ]
    night_anomalies = [
        item
        for item in list(audit.get("anomalies") or [])
        if _is_night_bj(parse_iso_utc(str(item["start_utc"])), timezone_name)
    ]
    max_anomaly = night_anomalies[0] if night_anomalies else None
    lines = ["## 3. 夜间事件审计", ""]
    lines.extend(
        [
            f"- 16:00-00:00 原始新闻数量：{segments.get('16:00-00:00', 0)}",
            f"- 00:00-08:00 原始新闻数量：{segments.get('00:00-08:00', 0)}",
            f"- 夜间原始新闻总数：{len(night_rows)}",
            f"- 夜间油市相关新闻数量：{len(night_oil)}",
            f"- 夜间重大事件候选数量：{len(night_major)}",
            f"- 夜间价格异动事件组数量：{len(night_movement_groups)}",
            f"- 夜间被价格异动复盘引用新闻数量：{len(set([str(_val(row, 'news_id') or '') for row in selected_night] + [str(_val(row, 'raw_id') or '') for row in selected_night_raw] + [str(_val(row, 'raw_id') or '') for row, _, _ in homepage_night_major]))}",
        ]
    )
    period_ranges = {
        "16:00-00:00": None,
        "00:00-08:00": None,
    }
    raw_segments = audit.get("segments") or {}
    for label in period_ranges:
        period_rows = []
        period_oil = []
        period_major = []
        period_selected = []
        period_groups = []
        for row in raw_rows:
            published = parse_iso_utc(str(row["published_at_utc"]))
            if not published:
                continue
            local = to_beijing(published, timezone_name).time()
            in_period = local >= time(16, 0) if label == "16:00-00:00" else local < ROLLING_CUTOFF
            if not in_period:
                continue
            period_rows.append(row)
            direct, oil_reason, major_reason = _raw_oil_status(row)
            if direct:
                period_oil.append((row, oil_reason, major_reason))
            if major_reason:
                period_major.append((row, oil_reason, major_reason))
            if (
                str(_val(row, "raw_id") or "") in selected_raw_set
                or str(_val(row, "raw_id") or "") in destinations
                or str(_val(row, "raw_id") or "") in representations
            ):
                period_selected.append(row)
        for group in list(movement_groups or []):
            raw_group_start = group.get("start_dt") or group.get("start_utc")
            group_start = raw_group_start if isinstance(raw_group_start, datetime) else parse_iso_utc(str(raw_group_start or ""))
            if not group_start:
                continue
            local = to_beijing(group_start, timezone_name).time()
            in_period = local >= time(16, 0) if label == "16:00-00:00" else local < ROLLING_CUTOFF
            if in_period:
                period_groups.append(group)
        lines.extend(
            [
                f"- {label} 细分审计：原始{len(period_rows)}条，油市{len(period_oil)}条，重大候选{len(period_major)}条，正式日报引用新闻{len(period_selected)}条，价格异动组{len(period_groups)}个。",
            ]
        )
        if period_major and not period_selected and not period_groups:
            lines.append(f"  - {label} 存在重大候选但未进入正式日报：未形成独立价格异动、归因暂无法确认或被判为背景/已消化信息。")
    if not night_rows:
        if audit.get("news_freshness") != "passed":
            lines.append("- 夜间新闻未成功更新，无法判断夜间是否无重大消息。")
        else:
            lines.append("- 夜间原始新闻数量为0：新闻覆盖待复核，不能据此判断夜间无重大消息。")
    elif len(night_rows) < 3:
        lines.append("- 夜间原始新闻数量偏低：新闻覆盖待复核。")
    if night_major and not selected_night_raw and not night_movement_groups:
        lines.append("- 夜间存在重大事件候选但未形成价格异动复盘事件组，以下列出排除原因供人工复核。")
    if homepage_night_major:
        lines.append("- 夜间首页展示的重要候选：")
        for row, oil_reason, major_reason in homepage_night_major[:10]:
            raw_id = str(_val(row, "raw_id") or "")
            destination = (
                "；".join(destinations[raw_id])
                if raw_id in destinations
                else f"由 {representations[raw_id]} 代表展示"
            )
            lines.append(
                f"  - {_raw_bj(row, timezone_name)} {row['title']}：{major_reason}；{oil_reason}；首页展示：{destination}；未作为价格异动复盘核心/辅助新闻。"
            )
    if excluded_major:
        lines.append("- 夜间被排除的重要候选及原因：")
        for row, oil_reason, major_reason in excluded_major[:10]:
            lines.append(f"  - {_raw_bj(row, timezone_name)} {row['title']}：{major_reason}；{oil_reason}；未成为价格异动复盘核心/辅助新闻。")
    else:
        lines.append("- 夜间被排除的重要候选及原因：暂无。")
    if max_anomaly:
        start = parse_iso_utc(str(max_anomaly["start_utc"]))
        end = parse_iso_utc(str(max_anomaly["end_utc"]))
        lines.append(
            f"- 夜间最大Brent价格波动时间段：{_bj_from_dt(start, timezone_name)} 至 {_bj_from_dt(end, timezone_name)}，{_fmt_pct(max_anomaly.get('pct'))}"
        )
        nearby = list(max_anomaly.get("nearby_news") or [])
        if nearby:
            lines.append("- 夜间最大波动附近新闻列表：")
            for row in nearby[:8]:
                lines.append(f"  - {_raw_bj(row, timezone_name)} {row['title']}")
        else:
            lines.append("- 夜间最大波动附近新闻列表：无。")
    else:
        lines.append("- 夜间最大Brent价格波动时间段：无足够夜间K线或未进入Top异常窗口。")
    lines.append("")
    return lines


def _raw_inventory_lines(
    audit: dict[str, object],
    timezone_name: str,
    all_reaction_rows: Iterable | None = None,
    selected_raw_ids: Iterable[str] | None = None,
    homepage_destinations: Mapping[str, Sequence[str]] | None = None,
    homepage_representations: Mapping[str, str] | None = None,
) -> list[str]:
    raw_rows = list(audit.get("raw_rows") or [])
    reaction_by_id = {str(row["news_id"]): row for row in (all_reaction_rows or [])}
    selected = {str(item) for item in (selected_raw_ids or [])}
    destinations = {
        str(raw_id): list(labels)
        for raw_id, labels in (homepage_destinations or {}).items()
        if labels
    }
    representations = {
        str(raw_id): str(representative_id)
        for raw_id, representative_id in (homepage_representations or {}).items()
    }
    major_ids = {str(row["raw_id"]) for row, _, _ in list(audit.get("major_candidates") or [])}
    lines = ["## 7. 原始新闻清单与未入选原因", ""]
    if not raw_rows:
        lines.append("- 窗口内暂无原始新闻记录。")
        lines.append("")
        return lines
    lines.append(f"- 原始新闻总数：{len(raw_rows)}")
    lines.append("- 字段：发布时间 | 来源 | 标题 | 油市相关 | 重大主题 | 候选池 | 正式日报 | 未入选原因 | 链接")
    for row in raw_rows:
        raw_id = str(_val(row, "raw_id") or "")
        title = str(_val(row, "title") or "").strip() or str(_val(row, "body") or "").strip()[:80]
        url = str(_val(row, "url") or "")
        source_channel = str(_val(row, "source_channel") or "flash")
        direct, oil_reason, major_reason = _raw_oil_status(row)
        is_candidate = direct or bool(major_reason)
        in_report = raw_id in selected or raw_id in destinations or raw_id in representations
        reaction = reaction_by_id.get(raw_id)
        if raw_id in destinations:
            reason = f"首页展示：{'；'.join(destinations[raw_id])}；未作为价格异动核心/次要归因"
        elif raw_id in representations:
            reason = f"首页合并展示：由 {representations[raw_id]} 代表展示；保留正式归因追溯"
        elif in_report:
            reason = "进入价格异动复盘正文"
        elif major_reason:
            reason = "重大候选但未成为价格异动核心/次要驱动；详见重大候选池审计"
        elif reaction and _val(reaction, "exclusion_reason"):
            reason = str(_val(reaction, "exclusion_reason"))
        elif reaction and _val(reaction, "price_status") not in (None, "ok"):
            reason = f"价格数据不足或待补算（{_val(reaction, 'price_missing_reason') or _val(reaction, 'price_status')}）"
        elif direct:
            reason = "油市相关但未达到价格异动归因优先级"
        else:
            reason = "与油市无直接关联或缺少明确Brent传导路径"
        lines.append(
            f"- {_raw_bj(row, timezone_name)} | {source_channel} | {title} | "
            f"{'是' if direct else '否'}（{oil_reason}） | {'是' if raw_id in major_ids else '否'}"
            f"{('（' + major_reason + '）') if major_reason else ''} | {'是' if is_candidate else '否'} | "
            f"{'是' if in_report else '否'} | {reason} | {url}"
        )
    lines.append("")
    return lines


def _source_label(brent: BrentConfig) -> str:
    source = "Yahoo Finance" if brent.source.lower() == "yahoo" else brent.source
    return f"{source} / {brent.symbol} / {brent.interval}"


def audit_markdown_path_for(reaction_markdown_path: Path) -> Path:
    stem = reaction_markdown_path.stem.replace("Jin10_Brent_Rolling_Reaction_", "Jin10_Brent_Rolling_Audit_")
    if stem.endswith("_PARTIAL"):
        stem = f"{stem[:-8]}_FAILED"
    elif stem.endswith("_FAILED"):
        stem = stem
    return reaction_markdown_path.with_name(f"{stem}.md")


def audit_html_path_for(reaction_markdown_path: Path) -> Path:
    return audit_markdown_path_for(reaction_markdown_path).with_suffix(".html")


def _event_summary(row) -> str:
    oil_reason = str(_val(row, "oil_relevance_reason") or "")
    marker = "汇总类新闻提取到具体油市事件："
    if oil_reason.startswith(marker):
        extracted = oil_reason[len(marker) :].strip()
        if extracted:
            return extracted.rstrip("。") + "。"
    content = str(row["content"]).strip()
    title = str(row["title"]).strip()
    if content.startswith(title):
        text = content
    else:
        text = f"{title}。{content}"
    parts = [p for p in text.replace("！", "。").replace("？", "。").split("。") if p.strip()]
    return "。".join(parts[:2]).strip("。") + "。"


def _display_title(row) -> str:
    oil_reason = str(_val(row, "oil_relevance_reason") or "")
    marker = "汇总类新闻提取到具体油市事件："
    if oil_reason.startswith(marker):
        extracted = oil_reason[len(marker) :].strip().rstrip("。")
        if extracted:
            return f"汇总提取事件：{extracted}"
    return str(row["title"])


def _driver_summary(rows) -> str:
    if not rows:
        return "暂无足够新闻和价格数据形成日内驱动判断，Brent主要驱动需结合盘面和后续消息继续观察。"
    topics = Counter(str(row["event_topic"]) for row in rows)
    labels = Counter(str(_val(row, "price_reaction_level") or _val(row, "reaction_label") or "") for row in rows)
    top_topics = "、".join(topic for topic, _ in topics.most_common(4))
    bullish = sum(count for label, count in labels.items() if "偏利多" in label)
    bearish = sum(count for label, count in labels.items() if "偏利空" in label)
    if bullish > bearish:
        direction = "偏利多反应较多"
    elif bearish > bullish:
        direction = "偏利空反应较多"
    else:
        direction = "快速价格反应有限或多空反应相对交织"
    return (
        f"本窗口重点事件主要集中在{top_topics or '中东能源贸易相关主题'}，"
        f"Brent 15/30/60分钟观察窗口内{direction}。需注意同期多条消息可能共同影响价格，无法确认单一新闻因果。"
    )


def _top_events(rows) -> list:
    def move_abs(row) -> float:
        vals = [
            _val(row, "pct_5m"),
            _val(row, "pct_15m"),
            _val(row, "pct_30m"),
            _val(row, "pct_60m"),
            _val(row, "pre30_to_post30_pct"),
            _val(row, "max_up_pct"),
            _val(row, "max_down_pct"),
        ]
        return max(abs(float(v)) for v in vals if v is not None) if any(v is not None for v in vals) else 0.0

    return sorted(rows, key=move_abs, reverse=True)[:5]


def _core_abs(row) -> float:
    vals = [
        _val(row, "pct_5m"),
        _val(row, "pct_15m"),
        _val(row, "pct_30m"),
        _val(row, "pct_60m"),
        _val(row, "pre30_to_post30_pct"),
    ]
    return max((abs(float(v)) for v in vals if v is not None), default=0.0)


def _event_units(rows) -> list[tuple[str, object, list]]:
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        key = str(_val(row, "multi_event_group_id") or row["news_id"])
        groups[key].append(row)
    units = []
    for key, group_rows in groups.items():
        representative = max(group_rows, key=_core_abs)
        units.append((key, representative, group_rows))
    return sorted(units, key=lambda item: _core_abs(item[1]), reverse=True)


def _importance_source(row) -> str:
    has_major = bool(_val(row, "importance_reason"))
    has_trigger = bool(_val(row, "trigger_window"))
    if has_major and has_trigger and (_val(row, "price_reaction_level") or "").startswith(("强市场反应", "中等市场反应")):
        return "重大主题命中 + 价格阈值触发"
    if has_trigger and (_val(row, "price_reaction_level") or "").startswith(("强市场反应", "中等市场反应")):
        return "价格阈值触发"
    if has_major:
        return "重大主题命中"
    return "规则候选"


def _confidence(row) -> str:
    level = str(_val(row, "price_reaction_level") or "")
    if level.startswith("强市场反应") and _val(row, "price_status") == "ok":
        return "高"
    if level.startswith("中等市场反应") or (level == "重要但暂未充分定价" and _val(row, "price_status") == "ok"):
        return "中"
    return "低"


def _market_direction(row) -> str:
    bias = str(_val(row, "market_bias") or "")
    if bias.startswith("偏多"):
        return "偏多"
    if bias.startswith("偏空"):
        return "偏空"
    return "中性"


def _cutoff_price_note(row, report_end_utc: datetime | None, timezone_name: str) -> str:
    if report_end_utc is None:
        return ""
    published = parse_iso_utc(row["published_at_utc"])
    if not published:
        return ""
    if published + timedelta(minutes=60) <= ensure_utc(report_end_utc):
        return ""
    if _val(row, "pct_60m") is not None and _val(row, "price_status") == "ok":
        return ""
    published_local = to_beijing(published, timezone_name).strftime("%H:%M")
    cutoff_local = to_beijing(report_end_utc, timezone_name).strftime("%H:%M")
    return (
        f"事件后60分钟价格暂不足：该新闻发布于{published_local}，"
        f"日报截止于{cutoff_local}，后续价格将在下一次日报中补充确认。"
    )


def _markdown_item(
    row,
    timezone_name: str,
    label_override: str | None = None,
    report_end_utc: datetime | None = None,
) -> str:
    label = label_override or str(_val(row, "price_reaction_level") or _val(row, "reaction_label") or "价格数据不足")
    if _val(row, "importance_reason"):
        importance = _val(row, "importance_reason")
    elif (_val(row, "price_reaction_level") or "").startswith(("强市场反应", "中等市场反应")):
        importance = "价格阈值触发，未命中重大主题规则"
    else:
        importance = _val(row, "oil_relevance_reason") or "规则候选"
    market_bias = _val(row, "market_bias") or "中性：需要结合后续价格和实物流信息继续判断。"
    lines = [
        f"### 【北京时间 {_bj_hhmm(row['published_at_utc'], timezone_name)}】{label}",
        "",
        f"标题：{row['title']}",
        f"事件类型：{row['event_type']}",
        f"事件分类：{row['event_topic']}",
        f"事件重要性来源：{_importance_source(row)}",
        f"命中依据：{importance}",
        f"新闻摘要：{_event_summary(row)}",
        f"原文链接：{row['url']}",
        "",
        "Brent价格反应：",
        f"- Brent新闻前基准价：{_fmt_price(_val(row, 'baseline_close'))} 美元/桶",
        f"- 事件基准价（新闻后最近5分钟K线）：{_fmt_price(_val(row, 'event_base_close'))} 美元/桶",
        f"- 事件后5分钟涨跌幅：{_fmt_pct(_val(row, 'pct_5m'))}",
        f"- 事件后15分钟涨跌幅：{_fmt_pct(_val(row, 'pct_15m'))}",
        f"- 事件后30分钟涨跌幅：{_fmt_pct(_val(row, 'pct_30m'))}",
        f"- 事件后60分钟涨跌幅：{_fmt_pct(_val(row, 'pct_60m'))}",
        f"- 事件前30分钟至事件后30分钟累计变化：{_fmt_pct(_val(row, 'pre30_to_post30_pct'))}",
        f"- 30分钟内最高/最低价：{_fmt_price(row['window_high'])} / {_fmt_price(row['window_low'])} 美元/桶",
        "",
        f"判断：{label}",
        f"触发窗口：{_val(row, 'trigger_window') or '未触发价格阈值'}",
        f"油价影响：{_market_direction(row)}",
        f"影响原因：{market_bias}",
        f"市场是否已定价：{_val(row, 'market_pricing_status') or '无法判断'}",
        f"市场解读：{row['market_interpretation']}",
    ]
    note = _cutoff_price_note(row, report_end_utc, timezone_name)
    if note:
        lines.append(note)
    return "\n".join(lines)


def _daily_driver_lines(rows, brent_summary: dict[str, str], timezone_name: str, audit: dict[str, object] | None = None) -> list[str]:
    audit = audit or {}
    data_complete = bool(audit.get("data_complete", True))
    completeness_warning = (
        "当前新闻抓取未完整覆盖本窗口，特别是夜间时段可能存在数据缺口。因此以下市场判断仅基于已获取事件和价格数据，不能视为完整日内复盘。"
        if not data_complete
        else ""
    )
    if not rows:
        lines = [
            "### 1. 本窗口市场主线",
            "",
            completeness_warning or "本窗口直接影响原油供应、航运、政策或地缘风险的有效事件有限，暂无法形成清晰的新闻驱动主线。",
            "",
            "### 2. 核心驱动拆解",
            "",
            "暂无足够重点事件。",
            "",
            "### 3. 新闻逻辑与价格表现是否一致",
            "",
            "暂无足够样本判断。",
            "",
            "### 4. 下一时段判断",
            "",
            "- 基准情景：等待新的实物供应、航运或政策信号。",
            "- 偏多触发：油轮滞留、护航升级、港口装船中断、制裁加码或OPEC+意外减产。",
            "- 偏空触发：海峡通行恢复、出口恢复、军事局势降温、库存增加或炼厂开工回落。",
            "- 重点验证：油轮AIS动态、港口装船、官方制裁文件、海军护航安排、OPEC+表态。",
            "- 当前未充分定价风险：航运保险费和船东风险偏好可能滞后于新闻修复。",
        ]
        return lines

    pct = brent_summary.get("pct", "N/A")
    if pct.startswith("+"):
        day_direction = "整体偏强"
    elif pct.startswith("-"):
        day_direction = "整体偏弱"
    else:
        day_direction = "整体震荡"
    topics = Counter(str(row["event_topic"]) for row in rows)
    dominant = topics.most_common(1)[0][0] if topics else "油市相关事件"
    bearish = sum(1 for row in rows if _market_direction(row) == "偏空")
    bullish = sum(1 for row in rows if _market_direction(row) == "偏多")
    if bearish > bullish:
        main_bias = "偏空驱动占优，市场更倾向交易风险缓和、供应竞争或预期下修。"
    elif bullish > bearish:
        main_bias = "偏多驱动占优，市场更关注供应、航运或地缘风险溢价。"
    else:
        main_bias = "多空信号交织，市场更像是在等待实物端验证。"

    lines = [
        "### 1. 本窗口市场主线",
        "",
    ]
    if completeness_warning:
        lines.extend([completeness_warning, ""])
    lines.extend([
        f"Brent本窗口{day_direction}，窗口涨跌幅为{pct}。在已获取数据中，重点事件主要围绕{dominant}，{main_bias}",
        "从事件后5/15/30/60分钟反应看，市场更重视可验证的出口、航运、OSP、产量或库存变化，而不是单纯关键词驱动。",
        "",
        "### 2. 核心驱动拆解",
        "",
    ])
    for idx, (_, row, group_rows) in enumerate(_event_units(rows)[:3], 1):
        if len(group_rows) > 1:
            title = f"同期多事件组（{len(group_rows)}条）：{_display_title(row)}"
        else:
            title = _display_title(row)
        lines.extend(
            [
                f"#### 驱动{idx}：{row['event_topic']}",
                "",
                f"- 对应事件：{_bj_hhmm(row['published_at_utc'], timezone_name)}，{title}",
                f"- 事件事实：{_event_summary(row)}",
                f"- 传导路径：{_val(row, 'market_bias') or '需结合后续实物信息判断。'}",
                (
                    f"- 市场反应：5/15/30/60分钟分别为{_fmt_pct(_val(row, 'pct_5m'))}、"
                    f"{_fmt_pct(_val(row, 'pct_15m'))}、{_fmt_pct(_val(row, 'pct_30m'))}、"
                    f"{_fmt_pct(_val(row, 'pct_60m'))}；前30至后30为{_fmt_pct(_val(row, 'pre30_to_post30_pct'))}。"
                ),
                f"- 当前定价判断：{_val(row, 'market_pricing_status') or '无法判断'}。",
                f"- 油价影响：{_market_direction(row)}。",
                f"- 置信度：{'低' if not data_complete else _confidence(row)}。",
                "",
            ]
        )

    inconsistent = [
        row
        for row in rows
        if _val(row, "market_pricing_status") == "与新闻逻辑背离"
    ]
    limited_major = [
        row
        for row in rows
        if (_val(row, "price_reaction_level") == "重要但暂未充分定价")
    ]
    lines.extend(["### 3. 新闻逻辑与价格表现是否一致", ""])
    if inconsistent:
        item = inconsistent[0]
        lines.append(
            f"{_bj_hhmm(item['published_at_utc'], timezone_name)} 的消息方向与Brent核心窗口反应不一致，"
            "说明同期可能有更强的库存、供应、航运或其他能源消息覆盖了该新闻逻辑。"
        )
    elif limited_major:
        lines.append(
            "部分重大主题消息未触发0.25%价格阈值，说明市场可能已提前交易，或仍在等待实际出口、装船、油轮通行、制裁文件等实物证据。"
        )
    else:
        lines.append("重点事件的价格反应与新闻逻辑大体一致，但仍不能确认单一新闻因果。")

    lines.extend(
        [
            "",
            "### 4. 下一时段判断",
            "",
            "- 基准情景：若霍尔木兹实际通行、原油出口和装船未继续恶化，Brent更可能维持震荡，地缘风险溢价需要实物端证据才能继续扩大。",
            "- 偏多触发：确认油轮滞留、护航升级、港口装船中断、制裁加码、出口受阻、LNG/原油供应中断或OPEC+意外减产。",
            "- 偏空触发：海峡通行恢复、船舶绕航减少、出口恢复、军事局势降温、OSP下调扩大、库存增加或炼厂开工回落。",
            "- 重点验证：油轮AIS动态、港口装船数据、官方制裁或豁免文件、海军护航安排、OPEC+官方表态、主要产油国库存和炼厂运行。",
            "- 当前未充分定价风险：即使新闻显示通行恢复，保险费、运费、船东风险偏好和装船节奏仍可能存在滞后扰动。",
        ]
    )
    return lines


def _risk_priority(row, major_reason: str, oil_reason: str) -> tuple[int, str]:
    title = str(_val(row, "title") or "")
    text = _raw_text(row)
    score = 0
    if not int(_val(row, "is_summary") or 0):
        score += 40
    else:
        score -= 20
    if _contains_any(text, ["霍尔木兹", "油轮", "绕航", "封锁", "护航", "通行", "搁浅", "滞留"]):
        score += 35
    if _contains_any(text, ["OPEC", "欧佩克", "产量", "减产", "增产", "配额", "OSP", "官方售价"]):
        score += 30
    if _contains_any(text, ["制裁", "豁免", "出口", "装船", "港口", "炼厂", "供应中断", "LNG", "液化天然气"]):
        score += 28
    if _contains_any(text, ["伊朗", "美国", "以色列", "停火", "袭击", "军事", "谈判"]):
        score += 18
    if _is_market_commentary(title):
        score -= 25
    return score, f"{major_reason or oil_reason}"


def _unpriced_risk_candidate_items(
    audit: dict[str, object],
    watch_rows: Iterable,
    selected_raw_ids: Iterable[str],
) -> list[tuple[int, object, str, str]]:
    selected = {str(item) for item in selected_raw_ids}
    seen: set[str] = set()
    candidates: list[tuple[int, object, str, str]] = []
    for raw_row, oil_reason, major_reason in list(audit.get("major_candidates") or []):
        raw_id = str(raw_row["raw_id"])
        title = str(raw_row["title"])
        if raw_id in selected or title in seen:
            continue
        if _is_institution_forecast_noise(raw_row):
            continue
        if _is_formal_report_noise(raw_row):
            continue
        if not direct_energy_supply_reason(title, str(_val(raw_row, "body") or _val(raw_row, "detail") or "")):
            continue
        priority, reason = _risk_priority(raw_row, major_reason, oil_reason)
        candidates.append((priority, raw_row, reason, "raw"))
        seen.add(title)
    for row in watch_rows:
        raw_id = str(row["news_id"])
        title = str(row["title"])
        if raw_id in selected or title in seen:
            continue
        if _is_institution_forecast_noise(row):
            continue
        if _is_formal_report_noise(row):
            continue
        if not direct_energy_supply_reason(title, str(_val(row, "content") or "")):
            continue
        reason = str(_val(row, "importance_reason") or _val(row, "oil_relevance_reason") or "重大主题候选")
        candidates.append((25, row, reason, "news"))
        seen.add(title)
    return sorted(candidates, key=lambda item: item[0], reverse=True)


def _unpriced_risk_ids(
    audit: dict[str, object],
    watch_rows: Iterable,
    selected_raw_ids: Iterable[str],
    limit: int = 5,
) -> set[str]:
    ids: set[str] = set()
    for _, row, _, row_type in _unpriced_risk_candidate_items(audit, watch_rows, selected_raw_ids)[:limit]:
        if row_type == "raw":
            ids.add(str(_val(row, "raw_id") or ""))
        else:
            ids.add(str(_val(row, "news_id") or ""))
    return {item for item in ids if item}


def _unpriced_risk_lines(
    audit: dict[str, object],
    watch_rows: Iterable,
    selected_raw_ids: Iterable[str],
    timezone_name: str,
    limit: int = 5,
) -> list[str]:
    lines = ["## 未充分定价的重大风险", ""]
    emitted = 0
    for _, row, reason, row_type in _unpriced_risk_candidate_items(audit, watch_rows, selected_raw_ids):
        if emitted >= limit:
            break
        title = str(_val(row, "title") or "")
        if row_type == "raw":
            event_key, event_topic = identify_event(title, str(_val(row, "body") or _val(row, "detail") or ""))
            time_text = _raw_bj(row, timezone_name)
        else:
            event_topic = str(_val(row, "event_topic") or "重大风险")
            time_text = _bj_full(str(row["published_at_utc"]), timezone_name)
        url = str(_val(row, "url") or "").strip() or "N/A"
        lines.append(
            f"- {time_text} {title}（{event_topic}）：{_brief_text(reason, 80)}；"
            f"本窗口未成为独立价格异动核心驱动，后续继续看是否出现实物流或官方确认。原文链接：{url}"
        )
        emitted += 1
    if emitted == 0:
        lines.append("- 暂无额外未充分定价重大风险记录。")
    lines.append("")
    return lines


def _extra_tracking_items(audit: dict[str, object], selected_raw_ids: Iterable[str], timezone_name: str, limit: int = 3) -> list[str]:
    selected = {str(item) for item in selected_raw_ids}
    items: list[tuple[int, object, str]] = []
    for raw_row, oil_reason, major_reason in list(audit.get("major_candidates") or []):
        raw_id = str(raw_row["raw_id"])
        if raw_id in selected:
            continue
        if _is_institution_forecast_noise(raw_row):
            continue
        if _is_formal_report_noise(raw_row):
            continue
        if not direct_energy_supply_reason(str(_val(raw_row, "title") or ""), str(_val(raw_row, "body") or _val(raw_row, "detail") or "")):
            continue
        priority, reason = _risk_priority(raw_row, major_reason, oil_reason)
        text = _raw_text(raw_row)
        if priority < 65:
            continue
        if not _contains_any(text, ["通行费", "搁浅", "现货石油", "出口", "装船", "库存", "OSP", "官方售价", "谈判", "制裁"]):
            continue
        items.append((priority, raw_row, reason))
    lines = []
    seen: set[str] = set()
    for _, row, reason in sorted(items, key=lambda item: item[0], reverse=True):
        title = str(_val(row, "title") or "")
        if title in seen:
            continue
        seen.add(title)
        url = str(_val(row, "url") or "").strip() or "N/A"
        lines.append(f"- 额外关注：{_raw_bj(row, timezone_name)} {title}；{_brief_text(reason, 70)}。原文链接：{url}")
        if len(lines) >= limit:
            break
    return lines


def _validated_isw_key_takeaways(
    external_update: Mapping[str, object],
) -> tuple[list[str], str]:
    if external_update.get("status") != "success":
        reason = str(external_update.get("failure_reason") or "未知错误")
        return [], f"{ISW_FAILURE_COPY} 失败原因：{reason}"
    source_items = list(external_update.get("source_key_takeaways") or [])
    translated_items = list(external_update.get("key_takeaways_zh") or [])
    try:
        source_count = int(external_update.get("source_key_takeaways_count"))
        translated_count = int(external_update.get("translated_key_takeaways_count"))
    except (TypeError, ValueError):
        source_count = translated_count = -1
    actual_source_count = len(source_items)
    actual_translated_count = len(translated_items)
    if not (
        actual_source_count > 0
        and actual_source_count
        == actual_translated_count
        == source_count
        == translated_count
    ):
        return [], "ISW Key Takeaways 完整性校验失败：原文、翻译及声明数量必须为正数且完全一致。"
    return [str(item) for item in translated_items], ""


def _tracking_outlook_lines(external_update: dict[str, object]) -> list[str]:
    source_url = str(external_update.get("url") or external_update.get("listing_url") or ISW_LISTING_URL)
    key_takeaways, failure_reason = _validated_isw_key_takeaways(external_update)
    fetch_success = external_update.get("status") == "success"
    display_failure = (
        f"抓取成功，但 {failure_reason}"
        if fetch_success and not key_takeaways
        else failure_reason
    )
    lines = [
        "## ISW Iran Update",
        "",
        f"- 更新时间：纽约时间 {external_update.get('published_at_et') or 'N/A'} / "
        f"北京时间 {external_update.get('published_at_bj') or 'N/A'}",
        f"- 标题：{external_update.get('title') or 'N/A'}",
        f"- 原文链接：{source_url}",
        "",
    ]
    if key_takeaways:
        lines.extend(["**Key Takeaways：**", ""])
        for index, item in enumerate(key_takeaways, 1):
            lines.append(f"{index}. {item}")
    else:
        lines.append(f"- {display_failure}")

    isw_watch = _first_isw_takeaway_text(key_takeaways) if key_takeaways else display_failure
    lines.extend(["", "## 今日关注什么", ""])
    if not key_takeaways:
        lines.append(
            "- 抓取成功，但ISW Key Takeaways未通过完整性校验；以下监测清单不引用未校验内容。"
            if fetch_success
            else "- 指定网站本次抓取失败，以下内容根据已获取的相关新闻和ISW信息整理。"
        )
    lines.extend(
        [
            "- 霍尔木兹海峡是否继续通行，以及是否出现收费、封锁或护航安排变化；",
            "- 油轮、港口和AIS船位变化，包括停航、绕航、遭袭、排队和装船节奏；",
            "- 制裁、豁免和出口政策是否出现新的官方文件或执行变化；",
            "- OPEC+及主要产油国政策是否出现产量、配额或官方售价调整；",
            "- 地区冲突是否升级或缓和，重点跟踪军事行动与关键设施安全；",
            "- EIA/API库存或其他重要供需数据是否改变短线供需判断；",
            f"- 当天ISW Iran Update最值得跟踪的事项：{isw_watch}",
            "",
        ]
    )
    return lines


def _first_isw_takeaway_text(key_takeaways: Sequence[object]) -> str:
    first = " ".join(str(key_takeaways[0]).split()) if key_takeaways else ""
    without_editorial_label = re.sub(
        r"^(?:中文\s*)?Key Takeaway(?:\s*\d+)?[：:\s.、-]*",
        "",
        first,
        flags=re.IGNORECASE,
    ).strip()
    return without_editorial_label or "首条Key Takeaway所述事项是否出现新的官方或实物流证据"


def _trump_statement_lines(trump_update: dict[str, object]) -> list[str]:
    lines = ["## 特朗普相关言论", ""]
    if trump_update.get("status") != "success":
        return lines + [f"- 金十特朗普专栏抓取失败：{trump_update.get('failure_reason') or '未知错误'}", ""]
    statements = sorted(
        list(trump_update.get("statements") or []),
        key=lambda statement: str(statement.get("published_at_utc") or ""),
    )
    if not statements:
        return lines + ["- 本窗口金十特朗普专栏未发现符合主题条件的特朗普本人表态。", ""]
    for statement in statements:
        topics = "、".join(str(item) for item in statement.get("topics") or [])
        lines.append(f"### {statement.get('published_at_bj')}｜{topics}")
        lines.append("")
        lines.append(str(statement.get("text") or ""))
        lines.append("")
        for source in statement.get("source_items") or []:
            source_url = source.get("source_url") or source.get("url") or ""
            lines.append(f"- 金十来源：[{source.get('title')}]({source_url})")
        lines.append("")
    return lines


def _trump_dashboard_items(trump_update: dict[str, object]) -> list[dict[str, object]]:
    if trump_update.get("status") != "success":
        return [
            {
                "status": "failed",
                "text": f"金十特朗普专栏抓取失败：{trump_update.get('failure_reason') or '未知错误'}",
                "source_items": [],
            }
        ]
    statements = sorted(
        list(trump_update.get("statements") or []),
        key=lambda statement: str(statement.get("published_at_utc") or ""),
    )
    if not statements:
        return [
            {
                "status": "empty",
                "text": "本窗口金十特朗普专栏未发现符合主题条件的特朗普本人表态。",
                "source_items": [],
            }
        ]
    return [
        {
            "statement_id": str(statement.get("statement_id") or ""),
            "published_at_bj": str(statement.get("published_at_bj") or ""),
            "text": str(statement.get("text") or ""),
            "topics": list(statement.get("topics") or []),
            "source_items": [dict(source) for source in statement.get("source_items") or []],
        }
        for statement in statements
    ]


def _highest_priority_unmatched_homepage_item(
    homepage_presentation: FormalReportPresentation,
) -> dict[str, object] | None:
    items = list(homepage_presentation.unmatched_major_items)
    return min(
        items,
        key=lambda item: _homepage_all_day_priority(item, "unmatched"),
    ) if items else None


_TRUMP_SUMMARY_TOPIC_PRIORITY = (
    "霍尔木兹",
    "战争与军事行动",
    "原油与能源",
    "制裁",
    "伊朗",
)


def _trump_summary_priority(statement: Mapping[str, object]) -> tuple[int, float, str]:
    topics = {str(topic) for topic in statement.get("topics") or []}
    topic_rank = min(
        (index for index, topic in enumerate(_TRUMP_SUMMARY_TOPIC_PRIORITY) if topic in topics),
        default=len(_TRUMP_SUMMARY_TOPIC_PRIORITY),
    )
    published = parse_iso_utc(str(statement.get("published_at_utc") or ""))
    latest_first = -published.timestamp() if published else float("inf")
    return topic_rank, latest_first, str(statement.get("statement_id") or "")


def _highest_priority_trump_statement(
    trump_update: Mapping[str, object],
) -> Mapping[str, object] | None:
    if trump_update.get("status") != "success":
        return None
    statements = list(trump_update.get("statements") or [])
    return min(statements, key=_trump_summary_priority) if statements else None


def _normalized_summary_evidence(text: object) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").casefold(), flags=re.UNICODE)


def _top_summary_items(
    brent_summary: Mapping[str, str],
    movement_review: Mapping[str, object],
    homepage_presentation: FormalReportPresentation,
    trump_update: Mapping[str, object],
    external_update: Mapping[str, object],
    report_status: str,
    audit: Mapping[str, object],
) -> list[str]:
    candidates: list[tuple[str, str]] = []

    def add(rendered: str, evidence: str | None = None) -> None:
        candidates.append((rendered, evidence if evidence is not None else rendered))

    latest = str(brent_summary.get("latest") or "价格数据暂不可用")
    pct = str(brent_summary.get("pct") or "涨跌幅暂不可用")
    high = str(brent_summary.get("high") or "")
    low = str(brent_summary.get("low") or "")
    unavailable = {"", "N/A", "None", "价格数据暂不可用"}
    if high not in unavailable and low not in unavailable:
        add(f"Brent：最新价{latest}，24小时涨跌幅{pct}，区间最高{high}、最低{low}。")
    else:
        data_status = str(audit.get("data_integrity") or report_status or "未知")
        add(f"Brent：最新价{latest}，24小时涨跌幅{pct}；高低点数据暂不可用（数据状态：{data_status}）。")

    attributed = [
        group
        for group in list(movement_review.get("groups") or [])
        if group.get("core_driver") and group.get("attribution_judgment") in {"主要驱动", "次要驱动"}
    ]
    if attributed:
        strongest = max(
            attributed,
            key=lambda group: abs(float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0)),
        )
        core_driver = dict(strongest.get("core_driver") or {})
        title = (
            _candidate_title(core_driver)
            if core_driver.get("row") is not None
            else str(core_driver.get("title") or core_driver.get("event_topic") or "油市供应消息")
        )
        add(
            f"价格归因：Brent{strongest.get('direction') or '异动'}{_fmt_pct(strongest.get('cumulative_pct'))}，"
            f"最强正式归因线索为{title}（{strongest.get('attribution_judgment')}）。",
            title,
        )
    else:
        unexplained_count = len(movement_review.get("unexplained") or [])
        if unexplained_count:
            movement_evidence = f"本窗口有{unexplained_count}段显著价格异动尚无正式新闻解释。"
        else:
            movement_evidence = "本窗口没有需要单列的“价格异动但新闻解释不足”事件。"
        add(f"价格归因：{movement_evidence}", movement_evidence)

    item = _highest_priority_unmatched_homepage_item(homepage_presentation)
    if item:
        homepage_text = str(item.get("title") or item.get("summary") or "").strip()
        if homepage_text:
            add(f"首页重大事项：{homepage_text}", homepage_text)

    statement = _highest_priority_trump_statement(trump_update)
    if statement:
        text = str(statement.get("text") or "").strip()
        if text:
            add(f"特朗普表态：{text}", text)

    translated, _isw_failure_reason = _validated_isw_key_takeaways(external_update)
    if translated:
        first_takeaway = _first_isw_takeaway_text(translated)
        add(f"ISW研判：{first_takeaway}", first_takeaway)

    deduplicated: list[str] = []
    seen: set[str] = set()
    for rendered, evidence in candidates:
        normalized = _normalized_summary_evidence(evidence)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduplicated.append(rendered)
        if len(deduplicated) == 5:
            break
    if len(deduplicated) < 2:
        coverage = str(audit.get("data_integrity") or "未知")
        deduplicated.append(f"报告覆盖：报告状态{report_status}，数据完整性{coverage}。")
    return deduplicated[:5]


def _unmatched_homepage_summary_lines(
    item: Mapping[str, object] | None,
) -> list[str]:
    if not item:
        return []
    title = str(item.get("title") or item.get("summary") or "").strip()
    if not title:
        return []
    time_text = str(item.get("time") or "").strip()
    url = str(item.get("url") or "").strip()
    heading = f"- {time_text}｜{title}" if time_text else f"- {title}"
    lines = ["## 首页重大事项（未匹配显著异动）", "", heading]
    if url:
        lines.append(f"  原文链接：{url}")
    lines.extend(
        [
            "  说明：该事项尚未匹配显著Brent异动，仅作为未定价重大事项展示，不构成正式价格归因。",
            "",
        ]
    )
    return lines


def _new_requirement_audit_lines(
    movement_review: dict[str, object],
    external_update: dict[str, object],
    timezone_name: str,
) -> list[str]:
    windows = list(movement_review.get("windows") or [])
    groups = list(movement_review.get("groups") or [])
    selected_candidates = [
        item
        for group in groups
        for item in list(group.get("selected_news") or [])
    ]
    strong_windows = [item for item in windows if abs(float(item.get("pct") or 0)) >= 0.005]
    subthreshold_with_news = [
        group
        for group in groups
        if abs(float(group.get("max_30m_pct") or 0)) < 0.0025
        and list(group.get("selected_news") or [])
    ]
    missing_links = [item for item in selected_candidates if not _candidate_has_original_link(item)]
    hard_gate_failures = [item for item in selected_candidates if not item.get("direct_supply_path")]
    noise_items = [
        item
        for item in selected_candidates
        if _is_formal_report_noise(item["row"])
        or item.get("price_quote_only")
        or (item.get("macro_reason") and item.get("brent_transmission") != "直接")
    ]
    missing_pct = [
        group
        for group in groups
        if group.get("cumulative_pct") is None or group.get("max_30m_pct") is None
    ]
    validated_takeaways, isw_failure_reason = _validated_isw_key_takeaways(external_update)
    fetch_success = external_update.get("status") == "success"
    integrity_success = bool(validated_takeaways)

    lines = [
        "## 8. 本次新增需求专项审计",
        "",
        f"- 本次识别出的30分钟价格异动窗口：{len(windows)}个。",
    ]
    for index, item in enumerate(windows, 1):
        lines.append(
            f"  - {index}. {_bj_from_dt(item.get('start_dt'), timezone_name)} 至 {_bj_from_dt(item.get('end_dt'), timezone_name)}："
            f"{_fmt_pct(item.get('pct'))}；达到>=0.50%：{'是' if item in strong_windows else '否'}"
        )
    if not windows:
        lines.append("  - 无；本窗口未达到0.25%补充阈值，正式复盘不插入新闻。")
    lines.extend(
        [
            f"- 达到30分钟绝对波动>=0.50%的窗口数：{len(strong_windows)}。",
            f"- 是否使用>=0.25%的补充窗口：{'是' if movement_review.get('supplemental_window_used') else '否'}。",
            f"- 是否存在未达阈值但被插入新闻：{'是' if subthreshold_with_news else '否'}。",
            f"- 价格异动标题是否缺失涨跌幅：{'是' if missing_pct else '否'}。",
            "",
            "### 各价格异动窗口入选新闻全量审计",
            "",
        ]
    )
    if not groups:
        lines.append("- 无符合阈值的独立价格异动事件组。")
    for index, group in enumerate(groups, 1):
        group_news = list(group.get("selected_news") or [])
        lines.append(
            f"- 事件组{index} {_movement_time_range(group, timezone_name)}："
            f"本轮{_fmt_pct(group.get('cumulative_pct'))}，最大30分钟{_fmt_pct(group.get('max_30m_pct'))}，入选新闻{len(group_news)}条。"
        )
        for news_index, item in enumerate(group_news, 1):
            lines.append(
                f"  - {news_index}. {_raw_bj(item['row'], timezone_name)} {_candidate_title(item)} | "
                f"供给端硬门槛：{'是' if item.get('direct_supply_path') else '否'} | "
                f"原文链接：{_candidate_url(item)} | 归因：{item.get('attribution_judgment')}"
            )
    lines.extend(
        [
            "",
            f"- 每条入选新闻是否均满足供给端硬门槛：{'是' if not hard_gate_failures else '否'}。",
            f"- 每条入选新闻是否都有原文链接：{'是' if not missing_links else '否'}。",
            f"- 是否存在股票、公司盘面、汇率、宏观或油价自身快讯被误选：{'是' if noise_items else '否'}。",
            "",
            "### 外部网站update审计",
            "",
            f"- 外部网站是否抓取成功：{'是' if fetch_success else '否'}。",
            f"- 抓取通道：{external_update.get('fetch_method') or 'N/A'}。",
            f"- 外部update网页发布/更新时间（纽约）：{external_update.get('published_at_et') or 'N/A'}。",
            f"- 外部update换算时间（北京）：{external_update.get('published_at_bj') or 'N/A'}。",
            f"- 外部update标题：{external_update.get('title') or 'N/A'}。",
            f"- 外部update链接：{external_update.get('url') or ISW_LISTING_URL}。",
            f"- ISW Key Takeaways完整性校验：{'通过' if integrity_success else '失败' if fetch_success else '未执行'}。",
            f"- 外部内容是否成功翻译并写入“今日关注什么”：{'是' if integrity_success else '否'}。",
        ]
    )
    if fetch_success and not integrity_success:
        lines.append(
            f"- ISW Key Takeaways完整性校验失败原因：{isw_failure_reason.rstrip('。')}。"
        )
    if not fetch_success:
        lines.append(f"- 外部update失败原因：{external_update.get('failure_reason') or '未知'}。")
    lines.append("")
    return lines


def _external_source_integrity_lines(
    external_update: Mapping[str, object],
    trump_update: Mapping[str, object],
) -> list[str]:
    source_count = len(external_update.get("source_key_takeaways") or [])
    translations = list(external_update.get("key_takeaways_zh") or [])
    translated_count = len(translations)
    validated_takeaways, _isw_failure_reason = _validated_isw_key_takeaways(external_update)
    rendered_count = len(validated_takeaways)
    statements = sorted(
        list(trump_update.get("statements") or []),
        key=lambda statement: str(statement.get("published_at_utc") or statement.get("published_at_bj") or ""),
    )
    lines = [
        "## 外部来源完整性审计",
        "",
        f"- ISW Key Takeaways原文数量：{source_count}",
        f"- ISW Key Takeaways翻译数量：{translated_count}",
        f"- ISW Key Takeaways正文渲染数量：{rendered_count}",
        f"- 特朗普专栏原始条目数量：{int(trump_update.get('raw_topic_item_count') or 0)}",
        f"- 特朗普专栏窗口内条目数量：{int(trump_update.get('in_window_item_count') or 0)}",
        f"- 特朗普相关来源条目数量：{int(trump_update.get('relevant_source_item_count') or 0)}",
        f"- 特朗普独立表态数量：{int(trump_update.get('statement_count') if trump_update.get('statement_count') is not None else len(statements))}",
        "",
    ]
    for statement in statements:
        topics = "、".join(str(item) for item in statement.get("topics") or []) or "无"
        lines.extend(
            [
                f"### 表态ID：{statement.get('statement_id')}",
                "",
                f"- 主题：{topics}",
                f"- 代表表态：{statement.get('text') or ''}",
            ]
        )
        for source in statement.get("source_items") or []:
            source_url = source.get("source_url") or source.get("url") or ""
            lines.append(
                f"- 来源ID：{source.get('id')}｜时间：{source.get('published_at_bj') or 'N/A'}｜URL：{source_url}"
            )
        lines.append("")
    return lines


def _formal_audit_reference_lines(audit: dict[str, object], audit_md_path: Path, movement_review: dict[str, object]) -> list[str]:
    confidence = _data_confidence(audit)
    data_text = "完整" if audit.get("data_complete") else "部分缺失"
    night_issue = any("00:00-08:00 原始新闻数量为0" in item or "16:00-00:00 原始新闻数量为0" in item for item in audit.get("missing_periods") or [])
    unexplained_count = len(movement_review.get("unexplained") or [])
    if night_issue:
        night_text = "夜间新闻覆盖存在缺口，市场主线判断置信度下调。"
    else:
        night_text = "夜间新闻与价格覆盖已纳入审计。"
    return [
        f"> 审计结论：数据完整性{data_text}，整体可信度：{confidence}。{night_text} "
        f"价格异动但新闻解释不足：{unexplained_count}段。审计报告：{audit_md_path.name}",
        "",
    ]


def _build_audit_report_lines(
    store: TrackerStore,
    brent: BrentConfig,
    audit: dict[str, object],
    movement_review: dict[str, object],
    watch_rows: Iterable,
    all_reaction_rows: Iterable,
    selected_rows: Iterable,
    selected_raw_ids: Iterable[str],
    day_start: datetime,
    day_end: datetime,
    title_label: str,
    timezone_name: str,
    audit_status: str = "FORMAL",
    warning_banner: str | None = None,
    runtime_summary: dict[str, object] | None = None,
    source_checks: list[dict[str, object]] | None = None,
    external_update: dict[str, object] | None = None,
    trump_update: dict[str, object] | None = None,
    homepage_destinations: Mapping[str, Sequence[str]] | None = None,
    homepage_event_groups: Iterable[dict[str, object]] | None = None,
    homepage_display_summary: Mapping[str, object] | None = None,
    homepage_representations: Mapping[str, str] | None = None,
) -> list[str]:
    external_update = external_update or {"status": "failed", "failure_reason": "未执行外部update抓取"}
    trump_update = trump_update or {"status": "failed", "failure_reason": "未执行特朗普专栏抓取"}
    selected_raw_set = {str(item) for item in selected_raw_ids}
    unpriced_ids = _unpriced_risk_ids(audit, watch_rows, selected_raw_set)
    lines = [
        f"# {_audit_title(audit_status)}（{title_label}）",
        "",
        "本报告用于核查数据是否完整、夜间新闻是否漏抓、重大候选是否被错误排除、Brent价格异动是否被合理解释。正式日报用于快速交易复盘，本审计报告保留完整复核痕迹。",
        "",
        f"- 新闻来源：金十数据",
        f"- Brent价格源：{_source_label(brent)}",
        f"- 审计生成时间：{datetime.now(ZoneInfo(timezone_name)).strftime('%Y-%m-%d %H:%M:%S')} 北京时间",
        "",
    ]
    if warning_banner:
        lines.extend([f"> {warning_banner}", ""])
    continuity_issues = list(audit.get("price_continuity_issues") or [])
    if continuity_issues:
        lines.extend(
            [
                "> 本时段出现极端价格波动，可能与重大突发事件有关，需结合相关新闻人工判断。",
                "",
            ]
        )
    lines.extend(_runtime_summary_lines(runtime_summary, source_checks, timezone_name))
    lines.extend(_audit_data_integrity_lines(audit, day_start, day_end, timezone_name))
    lines.extend(
        _homepage_event_group_audit_lines(
            homepage_event_groups or [],
            homepage_display_summary,
        )
    )
    lines.extend(
        _segment_coverage_lines(
            audit,
            day_start,
            day_end,
            timezone_name,
            set((homepage_destinations or {}).keys()),
            list(movement_review.get("groups") or []),
        )
    )
    lines.extend(
        _night_audit_lines(
            audit,
            selected_rows,
            timezone_name,
            selected_raw_ids=selected_raw_set,
            movement_groups=list(movement_review.get("groups") or []),
            homepage_destinations=homepage_destinations,
            homepage_representations=homepage_representations,
        )
    )
    lines.extend(
        _forced_candidate_lines(
            audit,
            selected_rows,
            all_reaction_rows,
            timezone_name,
            selected_raw_ids=selected_raw_set,
            unpriced_risk_ids=unpriced_ids,
            homepage_destinations=homepage_destinations,
            homepage_representations=homepage_representations,
        )
    )
    lines.extend(_movement_debug_lines(movement_review, timezone_name))
    lines.extend(_continuity_quarantine_lines(movement_review, timezone_name))
    lines.extend(_unexplained_price_movement_lines(movement_review, timezone_name))
    lines.extend(_attribution_rationality_lines(movement_review, timezone_name))
    lines.extend(_new_requirement_audit_lines(movement_review, external_update, timezone_name))
    lines.extend(_external_source_integrity_lines(external_update, trump_update))
    lines.extend(_summary_audit_lines(audit, timezone_name))
    lines.extend(_price_anomaly_lines(audit, timezone_name))
    lines.extend(
        _raw_inventory_lines(
            audit,
            timezone_name,
            all_reaction_rows,
            selected_raw_set,
            homepage_destinations=homepage_destinations,
            homepage_representations=homepage_representations,
        )
    )
    return lines


def _formal_summary_text(brent_summary: dict[str, str], movement_review: dict[str, object]) -> str:
    pct = brent_summary.get("pct", "价格数据暂不可用")
    if pct.startswith("+"):
        movement = f"上涨 {pct[1:]}"
    elif pct.startswith("-"):
        movement = f"下跌 {pct[1:]}"
    elif pct.endswith("%"):
        movement = f"基本持平（{pct}）"
    else:
        movement = pct

    attributed = [
        group
        for group in list(movement_review.get("groups") or [])
        if group.get("core_driver") and group.get("attribution_judgment") in {"主要驱动", "次要驱动"}
    ]
    unexplained_count = len(movement_review.get("unexplained") or [])
    first_sentence = f"Brent过去24小时{movement}。"
    if attributed:
        strongest = max(
            attributed,
            key=lambda group: abs(float(group.get("cumulative_pct") or group.get("max_30m_pct") or 0)),
        )
        topic = str(strongest["core_driver"].get("event_topic") or "油市供应消息")
        second_sentence = f"已匹配新闻中，{topic}是最主要线索"
    else:
        second_sentence = "目前没有找到足够直接的新闻解释"
    if unexplained_count:
        second_sentence += f"；另有{unexplained_count}段较大异动暂未找到直接新闻解释"
    return first_sentence + second_sentence + "。"


def build_markdown_report(
    store: TrackerStore,
    brent: BrentConfig,
    output_dir: Path,
    timezone_name: str = "Asia/Shanghai",
    day: datetime | None = None,
    report_status: str = "FORMAL",
    source_checks: list[dict[str, object]] | None = None,
    runtime_summary: dict[str, object] | None = None,
    external_update: dict[str, object] | None = None,
    trump_update: dict[str, object] | None = None,
    external_cache_dir: Path = Path("data/external_updates"),
) -> ReportBuildResult:
    day_start, day_end, day_label = beijing_day_bounds(day, timezone_name)
    if external_update is None:
        external_update = fetch_latest_isw_update(
            day_end,
            cache_dir=external_cache_dir,
            timezone_name=timezone_name,
        )
    if trump_update is None:
        trump_update = fetch_trump_statements(
            day_start,
            day_end,
            cache_dir=external_cache_dir,
            timezone_name=timezone_name,
        )
    store.refresh_multi_event_groups(day_start, day_end)
    audit = _build_window_audit(store, brent, day_start, day_end, timezone_name)
    watch_rows = store.unpriced_watch_rows(day_start, day_end)
    all_reaction_rows = store.reaction_rows_between(day_start, day_end)
    movement_review = _build_price_movement_review(store, brent, day_start, day_end, timezone_name)
    movement_review = _quarantine_continuity_movements(movement_review, audit)
    movement_review = _degrade_movement_review_for_incomplete_data(movement_review, audit)
    selected_raw_ids = {str(item) for item in (movement_review.get("selected_raw_ids") or set())}
    reaction_by_id = {str(row["news_id"]): row for row in all_reaction_rows}
    homepage_presentation = _build_event_grouped_homepage(
        movement_review,
        audit,
        all_reaction_rows,
        selected_raw_ids,
        timezone_name,
    )
    panel_groups = homepage_presentation.panel_groups
    unmatched_major_items = homepage_presentation.unmatched_major_items
    homepage_destinations = _homepage_destinations(panel_groups, unmatched_major_items)
    chart_price_rows = store.brent_day_klines(brent.symbol, brent.interval, day_start, day_end)
    chart_candles = aggregate_30m_klines(
        chart_price_rows,
        day_start,
        day_end,
        timezone_name,
    )
    chart_events, unmatched_movements = prepare_interactive_chart_markers(
        movement_review,
        chart_candles,
        to_beijing(day_start, timezone_name),
        to_beijing(day_end, timezone_name),
        timezone_name,
    )
    selected_order: list[str] = []
    for group in list(movement_review.get("groups") or []):
        core = group.get("core_driver")
        if core and str(core["raw_id"]) not in selected_order:
            selected_order.append(str(core["raw_id"]))
        for item in list(group.get("secondary_drivers") or []):
            raw_id = str(item["raw_id"])
            if raw_id not in selected_order:
                selected_order.append(raw_id)
    rows = [reaction_by_id[raw_id] for raw_id in selected_order if raw_id in reaction_by_id]
    brent_summary = brent_day_summary(store, brent, day_start, day_end)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_status, status_summary = classify_report_status(audit, source_checks)
    if runtime_summary:
        status_summary = {**status_summary, **runtime_summary}
    status_summary.setdefault("report_status", final_status)
    suffix = "" if final_status == "FORMAL" else f"_{final_status}"
    path = output_dir / f"Jin10_Brent_Rolling_Reaction_{day_label}{suffix}.md"
    audit_path = audit_markdown_path_for(path)
    # Keep the historical _FAILED audit filename for a partial report, but make
    # the document's status and availability match the actual report state.
    audit_status = final_status
    audit_summary = {
        **status_summary,
        "report_status": audit_status,
        "formal_report_availability": _availability_label(audit_status),
        "manual_rerun_required": "是" if audit_status != "FORMAL" else "否",
    }
    start_local = to_beijing(day_start, timezone_name)
    end_local = to_beijing(day_end, timezone_name)
    chart_path = path.with_name(f"{path.stem}_brent_24h.svg")
    chart_artifact = render_brent_24h_svg(
        chart_candles,
        chart_events,
        start_local,
        end_local,
        chart_path,
        _source_label(brent),
        unmatched_movements,
    )
    title_label = end_local.strftime("%Y%m%d 08:00")
    warning_banner = None
    if final_status != "FORMAL":
        warning_banner = _top_level_warning(
            day_end,
            audit.get("news_freshness") != "passed",
            audit.get("price_freshness") != "passed",
            audit,
        )

    summary_homepage_item = _highest_priority_unmatched_homepage_item(homepage_presentation)
    top_summary_items = _top_summary_items(
        brent_summary,
        movement_review,
        homepage_presentation,
        trump_update,
        external_update,
        final_status,
        audit,
    )
    lines = [
        f"# {_report_title(final_status)}（{title_label}）",
        "",
        "## 今日重点（Executive Summary）",
        "",
        *(f"- {item}" for item in top_summary_items),
        "",
        f"统计窗口：{start_local.strftime('%Y-%m-%d %H:%M')} 至 {end_local.strftime('%Y-%m-%d %H:%M')}（Asia/Shanghai）",
        f"数据来源：金十数据快讯｜{_source_label(brent)}｜金十数据特朗普专栏｜ISW Iran Update（仅用于后续监测）",
        "",
    ]
    if warning_banner:
        lines.extend([f"> {warning_banner}", ""])
    continuity_issues = list(audit.get("price_continuity_issues") or [])
    if continuity_issues:
        lines.extend(
            [
                "> 本时段出现极端价格波动，可能与重大突发事件有关，需结合相关新闻人工判断。",
                "",
            ]
        )
    lines.extend(_instrument_and_coverage_lines(audit, final_status, timezone_name))
    lines.extend(
        [
            "## 本窗口Brent表现",
            "",
            "| 最新价 | 24小时涨跌幅 | 日内最高价 | 日内最低价 |",
            "| ---: | ---: | ---: | ---: |",
            f"| {brent_summary['latest']} | {brent_summary['pct']} | {brent_summary['high']} | {brent_summary['low']} |",
            "",
            *(
                ["> 极端价格仍参与最高价、最低价、区间涨跌幅、异动识别和新闻匹配。", ""]
                if continuity_issues
                else []
            ),
            "30分钟K线，红色表示上涨、绿色表示下跌；蓝色时间点是已匹配新闻，浅灰区域表示暂无直接新闻解释。",
            "",
        ]
    )
    if chart_artifact:
        lines.extend(
            [
                f"![Brent 24小时30分钟K线与关键新闻事件]({chart_artifact.path.name})",
                "",
            ]
        )
    else:
        lines.extend(["- 本窗口可用价格数据不足，未生成空图。", ""])
    lines.extend(_continuity_quarantine_lines(movement_review, timezone_name))
    lines.extend(_price_movement_review_lines(movement_review, timezone_name))
    lines.extend(_unexplained_price_movement_lines(movement_review, timezone_name, include_chart_ids=False))
    lines.extend(_unpriced_risk_lines(audit, watch_rows, selected_raw_ids, timezone_name))
    lines.extend(_unmatched_homepage_summary_lines(summary_homepage_item))
    lines.extend(["## 市场主要驱动与下一时段判断", ""])
    lines.extend(_movement_driver_lines(movement_review, brent_summary, timezone_name, audit=audit))
    lines.extend([""])
    lines.extend(_trump_statement_lines(trump_update))
    lines.extend(_tracking_outlook_lines(external_update))
    lines.extend([f"[查看完整 Audit →]({audit_path.with_suffix('.html').name})", ""])

    audit_lines = _build_audit_report_lines(
        store,
        brent,
        audit,
        movement_review,
        watch_rows,
        all_reaction_rows,
        rows,
        selected_raw_ids,
        day_start,
        day_end,
        title_label,
        timezone_name,
        audit_status=audit_status,
        warning_banner=warning_banner,
        runtime_summary=audit_summary,
        source_checks=source_checks,
        external_update=external_update,
        trump_update=trump_update,
        homepage_destinations=homepage_destinations,
        homepage_event_groups=homepage_presentation.event_groups,
        homepage_display_summary=homepage_presentation.display_summary,
        homepage_representations=homepage_presentation.homepage_representations,
    )
    audit_path.write_text("\n".join(audit_lines), encoding="utf-8")
    build_html_report(audit_path, output_dir)

    path.write_text("\n".join(lines), encoding="utf-8")
    presentation = FormalReportPresentation(
        panel_groups=panel_groups,
        unmatched_major_items=unmatched_major_items,
        iran_update_items=tuple(_isw_dashboard_items(external_update)),
        top_summary_items=tuple(top_summary_items),
        trump_items=tuple(_trump_dashboard_items(trump_update)),
        all_day_items=homepage_presentation.all_day_items,
        display_summary=homepage_presentation.display_summary,
        event_groups=homepage_presentation.event_groups,
        homepage_representations=homepage_presentation.homepage_representations,
    )
    return ReportBuildResult(markdown_path=path, presentation=presentation)


def _inline_markdown_html(value: str) -> str:
    links: list[str] = []

    def stash_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1))
        target = match.group(2).strip()
        is_external = target.startswith(("https://", "http://"))
        is_anchor = target.startswith("#")
        is_local_html = bool(re.fullmatch(r"[A-Za-z0-9_.-]+\.html", target))
        if not (is_external or is_anchor or is_local_html):
            return label
        token = f"@@JIN10LINK{len(links)}@@"
        extra = ' target="_blank" rel="noopener noreferrer"' if is_external else ""
        links.append(f'<a href="{html.escape(target, quote=True)}"{extra}>{label}</a>')
        return token

    staged = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", stash_link, value)
    rendered = html.escape(staged)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)
    for index, link in enumerate(links):
        rendered = rendered.replace(f"@@JIN10LINK{index}@@", link)
    if not links and rendered.strip().startswith(("https://", "http://")) and " " not in rendered.strip():
        url = rendered.strip()
        rendered = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
    return rendered


def _markdown_table_cells(line: str) -> list[str]:
    return [item.strip() for item in line.strip().strip("|").split("|")]


def _is_markdown_table_separator(line: str) -> bool:
    cells = _markdown_table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


_FORMAL_EVENT_HEADING = re.compile(
    r"^###\s+(?:【(?P<event_id>[EU]\d{2})｜)?北京时间\s+"
    r"(?P<start>\d{2}:\d{2})[–-](?P<end>\d{2}:\d{2})｜"
    r"Brent(?P<direction>上涨|下跌)\s+(?P<cumulative>[+-]?\d+(?:\.\d+)?%)"
    r"(?:｜最大30分钟(?:涨幅|跌幅)\s+(?P<max_30m>[+-]?\d+(?:\.\d+)?%))?$"
)


def _valid_jin10_display_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        return ""
    if host != "jin10.com" and not host.endswith(".jin10.com"):
        return ""
    return url


def _formal_report_window(text: str) -> tuple[datetime, datetime]:
    match = re.search(
        r"^统计窗口：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+至\s+"
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})",
        text,
        re.MULTILINE,
    )
    if not match:
        fallback = datetime(1970, 1, 1, 8, 0)
        return fallback, fallback + timedelta(days=1)
    return (
        datetime.strptime(match.group(1), "%Y-%m-%d %H:%M"),
        datetime.strptime(match.group(2), "%Y-%m-%d %H:%M"),
    )


def _formal_event_datetimes(start_clock: str, end_clock: str, window_start: datetime) -> tuple[datetime, datetime]:
    start_time = datetime.strptime(start_clock, "%H:%M").time()
    end_time = datetime.strptime(end_clock, "%H:%M").time()
    start_dt = datetime.combine(window_start.date(), start_time)
    if start_dt < window_start:
        start_dt += timedelta(days=1)
    end_dt = datetime.combine(start_dt.date(), end_time)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def _formal_event_key(start_clock: str, end_clock: str, window_start: datetime) -> str:
    start_dt, end_dt = _formal_event_datetimes(start_clock, end_clock, window_start)
    return f"event-{start_dt.strftime('%Y%m%dT%H%M')}-{end_dt.strftime('%Y%m%dT%H%M')}".lower()


def _formal_section(lines: list[str], heading: str, stop_headings: tuple[str, ...]) -> list[str]:
    try:
        start = lines.index(f"## {heading}") + 1
    except ValueError:
        return []
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index] in {f"## {item}" for item in stop_headings}:
            end = index
            break
    return lines[start:end]


def _formal_bullet_items(
    lines: list[str],
    heading: str,
    stop_headings: tuple[str, ...],
) -> list[str]:
    return [
        line.strip()[2:].strip()
        for line in _formal_section(lines, heading, stop_headings)
        if line.strip().startswith("- ")
    ]


def _formal_event_blocks(section_lines: list[str]) -> list[list[str]]:
    starts = [index for index, line in enumerate(section_lines) if _FORMAL_EVENT_HEADING.match(line)]
    return [
        section_lines[start : starts[position + 1] if position + 1 < len(starts) else len(section_lines)]
        for position, start in enumerate(starts)
    ]


def _formal_event_reactions(block: list[str]) -> dict[str, str]:
    pattern = re.compile(
        r"5分钟：(?P<pct_5m>[^｜]+)｜15分钟：(?P<pct_15m>[^｜]+)｜"
        r"30分钟：(?P<pct_30m>[^｜]+)｜60分钟：(?P<pct_60m>[^｜]+)｜"
        r"本轮累计：(?P<cumulative>\S+)"
    )
    for line in block:
        match = pattern.search(line.strip())
        if match:
            return {key: value.strip() for key, value in match.groupdict().items()}
    return {"pct_5m": "N/A", "pct_15m": "N/A", "pct_30m": "N/A", "pct_60m": "N/A", "cumulative": "N/A"}


def _formal_event_news(block: list[str]) -> list[dict[str, str]]:
    starts = [index for index, line in enumerate(block) if re.match(r"^\d+\. 新闻摘要：", line.strip())]
    news: list[dict[str, str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(block)
        item_lines = block[start:end]
        summary = re.sub(r"^\d+\. 新闻摘要：", "", item_lines[0].strip()).strip()
        fields: dict[str, str] = {}
        for line in item_lines[1:]:
            match = re.match(r"^\s*(发布时间|原文链接|时间关系|信息新鲜度|传导路径|归因判断|置信度)：(.*)$", line)
            if match:
                fields.setdefault(match.group(1), match.group(2).strip())
        url = _valid_jin10_display_url(fields.get("原文链接"))
        if not url:
            continue
        news.append(
            {
                "time": fields.get("发布时间", "N/A").removesuffix(" 北京时间"),
                "title": summary,
                "url": url,
                "relation": fields.get("时间关系", "N/A"),
                "freshness": fields.get("信息新鲜度", "不确定"),
                "judgment": fields.get("归因判断", "暂无法确认"),
                "confidence": fields.get("置信度", "低"),
            }
        )
    return news


def _formal_market_interpretation(block: list[str]) -> str:
    try:
        start = block.index("**市场解读：**") + 1
    except ValueError:
        return ""
    parts = []
    for line in block[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("### ", "## ")):
            break
        parts.append(stripped)
    return " ".join(parts)


def _parse_formal_events(
    section_lines: list[str],
    window_start: datetime,
    kind: str,
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in _formal_event_blocks(section_lines):
        heading = _FORMAL_EVENT_HEADING.match(block[0])
        if not heading:
            continue
        values = heading.groupdict()
        start_clock = str(values["start"])
        end_clock = str(values["end"])
        start_dt, end_dt = _formal_event_datetimes(start_clock, end_clock, window_start)
        reactions = _formal_event_reactions(block)
        news = _formal_event_news(block)
        if kind == "explained" and not news:
            continue
        judgment_match = next((re.match(r"^归因判断：(.*)$", line.strip()) for line in block if line.strip().startswith("归因判断：")), None)
        confidence_match = next((re.match(r"^置信度：(.*)$", line.strip()) for line in block if line.strip().startswith("置信度：")), None)
        judgment = news[0]["judgment"] if news else judgment_match.group(1).strip() if judgment_match else "暂无法确认"
        confidence = news[0]["confidence"] if news else confidence_match.group(1).strip() if confidence_match else "低"
        events.append(
            {
                "key": str(values.get("event_id") or "").lower() or _formal_event_key(start_clock, end_clock, window_start),
                "kind": kind,
                "start": start_dt.strftime("%Y-%m-%d %H:%M"),
                "end": end_dt.strftime("%Y-%m-%d %H:%M"),
                "time_range": f"北京时间 {start_clock}–{end_clock}",
                "direction": values["direction"],
                "cumulative": values["cumulative"],
                "max_30m": values.get("max_30m") or reactions.get("pct_30m") or "N/A",
                "pct_5m": reactions["pct_5m"],
                "pct_15m": reactions["pct_15m"],
                "pct_30m": reactions["pct_30m"],
                "pct_60m": reactions["pct_60m"],
                "news": news,
                "interpretation": _formal_market_interpretation(block),
                "judgment": judgment,
                "confidence": confidence,
            }
        )
    return events


def _formal_metric_values(lines: list[str]) -> list[tuple[str, str]]:
    expected = ["最新价", "24小时涨跌幅", "日内最高价", "日内最低价"]
    for index, line in enumerate(lines[:-2]):
        if _markdown_table_cells(line) != expected or not _is_markdown_table_separator(lines[index + 1]):
            continue
        values = _markdown_table_cells(lines[index + 2])
        if len(values) == len(expected):
            return list(zip(expected, values))
    return [(label, "N/A") for label in expected]


def _first_external_update(text: str) -> tuple[str, str]:
    section_match = re.search(r"^### 2\. 外部网站最新 update 中文总结\s*$([\s\S]*?)(?=^\[查看完整 Audit|\Z)", text, re.MULTILINE)
    section = section_match.group(1) if section_match else ""
    url_match = re.search(r"^(?:原文链接|ISW Iran Update列表)：(https?://\S+)", section, re.MULTILINE)
    summary_match = re.search(r"^1\.\s+(.+)$", section, re.MULTILINE)
    return (
        _brief_text(summary_match.group(1).strip(), 130) if summary_match else "外部网站最新update暂无可用结论。",
        url_match.group(1).strip() if url_match else "",
    )


def _external_update_dashboard_items(text: str) -> list[dict[str, str]]:
    section_match = re.search(
        r"^### 2\. 外部网站最新 update 中文总结\s*$([\s\S]*?)(?=^\[查看完整 Audit|\Z)",
        text,
        re.MULTILINE,
    )
    section = section_match.group(1) if section_match else ""
    url_match = re.search(r"^(?:原文链接|ISW Iran Update列表)：(https?://\S+)", section, re.MULTILINE)
    source_url = url_match.group(1).strip() if url_match else ISW_LISTING_URL
    title_match = re.search(r"^标题：(.+)$", section, re.MULTILINE)
    summary_items = [
        _brief_text(match.group(1).strip(), 118)
        for match in re.finditer(r"^\d+\.\s+(.+)$", section, re.MULTILINE)
    ]
    energy_section = ""
    energy_match = re.search(r"^\*\*对Brent/LPG/能源市场的关注意义：\*\*\s*$([\s\S]*?)(?=^> |\Z)", section, re.MULTILINE)
    if energy_match:
        energy_section = energy_match.group(1)
    energy_items = [
        _brief_text(match.group(1).strip(" ；。"), 118)
        for match in re.finditer(r"^-\s+(.+)$", energy_section, re.MULTILINE)
    ]
    if "抓取失败" in section:
        return [
            {
                "text": ISW_FAILURE_COPY,
                "url": "",
                "link_label": "",
            },
            {
                "text": "可手动打开ISW Iran Update列表，核对截止后是否发布新的伊朗局势更新。",
                "url": source_url,
                "link_label": "查看ISW列表",
            },
            {
                "text": "若ISW恢复可用，后续只把它作为下一时段风险监测补充，不反向改写本窗口归因。",
                "url": "",
                "link_label": "",
            },
        ]
    heading = title_match.group(1).strip() if title_match else "ISW Iran Update"
    items: list[dict[str, str]] = []
    if summary_items:
        items.append(
            {
                "text": f"{heading}：{summary_items[0]}",
                "url": source_url,
                "link_label": "查看ISW原文",
            }
        )
    else:
        items.append(
            {
                "text": "ISW专题暂无可提炼摘要，保留原文入口供人工核对。",
                "url": source_url,
                "link_label": "查看ISW原文",
            }
        )
    if energy_items:
        items.append({"text": f"能源市场含义：{energy_items[0]}", "url": "", "link_label": ""})
    else:
        items.append(
            {
                "text": "该专题只作为下一时段风险监测补充，不参与前文Brent价格异动归因。",
                "url": "",
                "link_label": "",
            }
        )
    return items[:3]


def _isw_markdown_dashboard_items(lines: list[str]) -> list[dict[str, str]]:
    section = _formal_section(
        lines,
        "ISW Iran Update",
        ("今日关注什么", "接下来关注什么"),
    )
    if not section:
        return []
    source_url = ISW_LISTING_URL
    for line in section:
        url_match = re.match(r"^-\s*原文链接：(https?://\S+)", line.strip())
        if url_match:
            source_url = url_match.group(1)
            break
    items: list[dict[str, str]] = []
    for line in section:
        stripped = line.strip()
        if stripped == ISW_FAILURE_COPY:
            items.append(
                {
                    "text": stripped,
                    "url": source_url,
                    "link_label": "查看ISW列表",
                }
            )
            continue
        numbered = re.match(r"^\d+\.\s+(.+)$", stripped)
        bullet = re.match(r"^-\s+(.+)$", stripped)
        text = numbered.group(1) if numbered else bullet.group(1) if bullet else ""
        if not text or text.startswith(("更新时间：", "标题：", "原文链接：")):
            continue
        if text not in {item["text"] for item in items}:
            items.append(
                {
                    "text": _brief_text(text, 150),
                    "url": source_url,
                    "link_label": "查看ISW原文",
                }
            )
    return items[:7]


def _concise_isw_dashboard_text(text: str) -> str:
    sentences = [part.strip() for part in re.split(r"(?<=[。！？])", " ".join(str(text).split())) if part.strip()]
    return "".join(sentences[:2]) if sentences else str(text).strip()


def _isw_dashboard_items(external_update: dict[str, object]) -> list[dict[str, str]]:
    source_url = str(external_update.get("url") or external_update.get("listing_url") or ISW_LISTING_URL)
    key_takeaways, failure_reason = _validated_isw_key_takeaways(external_update)
    if not key_takeaways:
        return [
            {
                "text": failure_reason,
                "url": source_url,
                "link_label": "查看ISW列表",
            }
        ]

    published_et = str(external_update.get("published_at_et") or "")
    published_bj = str(external_update.get("published_at_bj") or "")
    items: list[dict[str, str]] = []
    if published_et or published_bj:
        items.append(
            {
                "text": f"原文发布日期：纽约时间 {published_et or 'N/A'} / 北京时间 {published_bj or 'N/A'}",
                "url": source_url,
                "link_label": "查看ISW原文",
            }
        )
    for text in key_takeaways:
        concise = _concise_isw_dashboard_text(str(text))
        if concise:
            items.append({"text": concise, "url": source_url, "link_label": "查看ISW原文"})
    if not items:
        items.append(
            {
                "text": "ISW专题暂无可提炼摘要，保留原文入口供人工核对。",
                "url": source_url,
                "link_label": "查看ISW原文",
            }
        )
    return items


def _parse_formal_dashboard(
    text: str,
    presentation: FormalReportPresentation | None = None,
) -> dict[str, object]:
    lines = text.splitlines()
    window_start, window_end = _formal_report_window(text)
    explained_lines = _formal_section(
        lines,
        "本窗口Brent价格异动复盘",
        (
            "价格异动但新闻解释不足",
            "未充分定价的重大风险",
            "市场主要驱动与下一时段判断",
            "ISW Iran Update",
            "今日关注什么",
            "接下来关注什么",
        ),
    )
    unexplained_lines = _formal_section(
        lines,
        "价格异动但新闻解释不足",
        (
            "未充分定价的重大风险",
            "市场主要驱动与下一时段判断",
            "ISW Iran Update",
            "今日关注什么",
            "接下来关注什么",
        ),
    )
    explained = _parse_formal_events(explained_lines, window_start, "explained")
    unexplained = _parse_formal_events(unexplained_lines, window_start, "unexplained")
    metrics = _formal_metric_values(lines)
    pct = dict(metrics).get("24小时涨跌幅", "N/A")
    if pct.startswith("+"):
        overall = f"Brent本窗口整体上涨 {pct}。"
    elif pct.startswith("-"):
        overall = f"Brent本窗口整体下跌 {pct}。"
    else:
        overall = f"Brent本窗口涨跌幅 {pct}。"
    logic_match = re.search(r"可归因线索主要集中在([^，。；]+)", text)
    logic = logic_match.group(1).strip() if logic_match else "暂无足够明确的单一交易主线"
    title = next((line[2:] for line in lines if line.startswith("# ")), "Brent滚动日报")
    meta = [line for line in lines if line.startswith(("统计窗口：", "数据来源："))][:2]
    warning = next((line[2:] for line in lines if line.startswith("> ") and lines.index(line) < 8), "")
    extreme_notice = next(
        (
            line[2:]
            for line in lines
            if line
            == "> 本时段出现极端价格波动，可能与重大突发事件有关，需结合相关新闻人工判断。"
        ),
        "",
    )
    chart_line = next((line for line in lines if re.fullmatch(r"!\[[^\]]*\]\([^)]+\.svg\)", line.strip())), "")
    audit_link = next((line.strip() for line in lines if line.strip().startswith("[查看完整 Audit →]")), "")
    instrument_items = _formal_bullet_items(
        lines,
        "标的信息",
        ("数据覆盖范围", "本窗口Brent表现"),
    )
    coverage_items = _formal_bullet_items(
        lines,
        "数据覆盖范围",
        ("本窗口Brent表现",),
    )
    markdown_isw_items = _isw_markdown_dashboard_items(lines)
    today_watch_items = [
        {"text": item, "url": ""}
        for item in _formal_bullet_items(
            lines,
            "今日关注什么",
            ("接下来关注什么",),
        )
    ]
    return {
        "title": title,
        "meta": meta,
        "instrument_items": instrument_items,
        "coverage_items": coverage_items,
        "warning": warning,
        "extreme_notice": extreme_notice,
        "window_start": window_start,
        "window_end": window_end,
        "metrics": metrics,
        "events": explained + unexplained,
        "explained_count": len(explained),
        "unexplained_count": len(unexplained),
        "market_items": [
            overall,
            f"最主要交易逻辑：{logic}。",
            f"已解释异动 {len(explained)} 段，未解释异动 {len(unexplained)} 段。",
        ],
        "iran_update_items": (
            list(presentation.iran_update_items)
            if presentation and presentation.iran_update_items
            else markdown_isw_items or _external_update_dashboard_items(text)
        ),
        "has_panel_presentation": presentation is not None,
        "panel_groups": list(presentation.panel_groups) if presentation else [],
        "unmatched_major_items": list(presentation.unmatched_major_items) if presentation else [],
        "all_day_items": list(presentation.all_day_items) if presentation else [],
        "display_summary": dict(presentation.display_summary or {}) if presentation else {},
        "tracking_items": today_watch_items or [
            {"text": "霍尔木兹海峡与曼德海峡：盯AIS船位、护航/收费/封锁通告、保险费和绕航报价。", "url": ""},
            {"text": "油轮、港口与出口：核对停航、绕航、遭袭、港口排队和装船节奏是否变化。", "url": ""},
            {"text": "制裁、豁免和出口政策：跟踪官方文件与执行变化。", "url": ""},
            {"text": "OPEC+及主要产油国政策：跟踪产量、配额和官方售价调整。", "url": ""},
            {"text": "地区冲突：关注是否进一步升级或缓和。", "url": ""},
            {"text": "EIA/API库存及其他供需数据：核对短线供需变化。", "url": ""},
            {"text": "ISW Iran Update：核对当天最值得继续跟踪的军事与航运事项。", "url": ""},
        ],
        "chart_line": chart_line,
        "audit_link": audit_link,
    }


def _enrich_formal_chart_svg(svg: str, dashboard: dict[str, object]) -> str:
    events = list(dashboard.get("events") or [])
    explained = [item for item in events if item.get("kind") == "explained"]
    unexplained = {str(item["key"]): item for item in events if item.get("kind") == "unexplained"}
    presentation_groups = [
        item
        for item in list(dashboard.get("panel_groups") or [])
        if isinstance(item, Mapping) and str(item.get("key") or "")
    ]
    presentation_groups_by_key = {str(item["key"]): item for item in presentation_groups}
    news_metadata: dict[str, dict[str, str]] = {}

    def presentation_group_for_marker(marker_news: list[dict[str, object]]) -> Mapping[str, object] | None:
        if not marker_news:
            return None

        def group_interval(group: Mapping[str, object]) -> tuple[datetime, datetime] | None:
            match = re.fullmatch(
                r"event-(\d{8}t\d{4})-(\d{8}t\d{4})",
                str(group.get("key") or "").lower(),
            )
            if not match:
                return None
            try:
                return (
                    datetime.strptime(match.group(1), "%Y%m%dT%H%M"),
                    datetime.strptime(match.group(2), "%Y%m%dT%H%M"),
                )
            except ValueError:
                return None

        def marker_time(item: Mapping[str, object]) -> datetime | None:
            try:
                return datetime.strptime(str(item.get("time") or ""), "%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                return None

        ranked_groups = []
        for group in presentation_groups:
            group_urls = {
                _valid_jin10_display_url(news.get("url"))
                for news in list(group.get("news") or [])
                if isinstance(news, Mapping) and _valid_jin10_display_url(news.get("url"))
            }
            matched_news = [
                (item, marker_time(item))
                for item in marker_news
                if str(item["url"]) in group_urls
            ]
            if not matched_news:
                continue
            interval = group_interval(group)
            contained_matches = 0
            distances: list[float] = []
            if interval is not None:
                start, end = interval
                for _, published_at in matched_news:
                    if published_at is None:
                        continue
                    if start <= published_at <= end:
                        contained_matches += 1
                    else:
                        distances.append(
                            min(
                                abs((published_at - start).total_seconds()),
                                abs((published_at - end).total_seconds()),
                            )
                        )
            ranked_groups.append(
                (
                    -contained_matches,
                    -len(matched_news),
                    min(distances) if distances else 0.0 if contained_matches else float("inf"),
                    str(group["key"]),
                    group,
                )
            )
        return min(ranked_groups, key=lambda item: item[:4])[4] if ranked_groups else None

    def set_event_key(attrs: str, event_key: str) -> str:
        attribute = f' data-event-key="{html.escape(event_key, quote=True)}"'
        if re.search(r'\bdata-event-key="[^"]*"', attrs):
            return re.sub(r'\bdata-event-key="[^"]*"', attribute.strip(), attrs, count=1)
        return attrs + attribute

    marker_pattern = re.compile(r'<g\b(?P<attrs>[^>]*\bclass="[^"]*\bnews-marker\b[^"]*"[^>]*)>')

    def enrich_marker(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        payload_match = re.search(r'\bdata-news="([^"]*)"', attrs)
        payload: list[dict[str, object]] = []
        if payload_match:
            try:
                decoded = json.loads(html.unescape(payload_match.group(1)))
                if isinstance(decoded, list):
                    payload = [item for item in decoded if isinstance(item, dict)]
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = []
        valid_payload = []
        for item in payload:
            url = _valid_jin10_display_url(item.get("url"))
            if not url:
                continue
            cleaned = {**item, "url": url}
            valid_payload.append(cleaned)
            news_metadata[url] = {
                "time": str(item.get("time") or "N/A"),
                "title": str(item.get("title") or ""),
            }
        urls = {str(item["url"]) for item in valid_payload}
        candidates = [
            event
            for event in explained
            if urls.intersection({str(news["url"]) for news in list(event.get("news") or [])})
        ]
        event = None
        if len(candidates) == 1:
            event = candidates[0]
        elif candidates:
            marker_time = None
            try:
                marker_time = datetime.strptime(str(valid_payload[0].get("time")), "%Y-%m-%d %H:%M")
            except (IndexError, TypeError, ValueError):
                marker_time = None
            if marker_time:
                event = next(
                    (
                        item
                        for item in candidates
                        if datetime.strptime(str(item["start"]), "%Y-%m-%d %H:%M")
                        <= marker_time
                        <= datetime.strptime(str(item["end"]), "%Y-%m-%d %H:%M")
                    ),
                    None,
                )
            event = event or sorted(candidates, key=lambda item: str(item["key"]))[0]
        sanitized = html.escape(json.dumps(valid_payload, ensure_ascii=False, separators=(",", ":")), quote=True)
        if payload_match:
            attrs = attrs[: payload_match.start(1)] + sanitized + attrs[payload_match.end(1) :]
        presentation_group = presentation_group_for_marker(valid_payload)
        if presentation_group:
            attrs = set_event_key(attrs, str(presentation_group["key"]))
        elif event and "data-event-key=" not in attrs:
            attrs = set_event_key(attrs, str(event["key"]))
        return f"<g{attrs}>"

    svg = marker_pattern.sub(enrich_marker, svg)
    for event in explained:
        for news in list(event.get("news") or []):
            metadata = news_metadata.get(str(news["url"]))
            if metadata:
                news["time"] = metadata["time"]

    band_pattern = re.compile(
        r'<rect\b(?P<attrs>[^>]*\bclass="unmatched-band"[^>]*)>'
        r'<title>(?P<title>[^<]*)</title></rect>'
    )
    hit_areas: list[str] = []
    window_start = dashboard.get("window_start")
    if isinstance(window_start, datetime):
        for match in band_pattern.finditer(svg):
            range_match = re.search(r"(\d{2}:\d{2})[–-](\d{2}:\d{2})", html.unescape(match.group("title")))
            if not range_match:
                continue
            event_key = _formal_event_key(range_match.group(1), range_match.group(2), window_start)
            panel_group = presentation_groups_by_key.get(event_key)
            if panel_group is None and event_key not in unexplained:
                continue
            geometry = []
            for attribute in ("x", "y", "width", "height", "rx"):
                value_match = re.search(rf'\b{attribute}="([^"]+)"', match.group("attrs"))
                if value_match:
                    geometry.append(f'{attribute}="{html.escape(value_match.group(1), quote=True)}"')
            if panel_group is not None:
                aria = f"{panel_group.get('time_range') or html.unescape(match.group('title'))}，查看该时段重要新闻"
            else:
                aria = (
                    f"{unexplained[event_key]['time_range']}，"
                    "暂未找到符合条件的金十新闻，原因暂无法确认，需人工继续核查。"
                )
            hit_areas.append(
                '<rect class="unmatched-band-hit" '
                + " ".join(geometry)
                + f' role="button" tabindex="0" focusable="true" data-event-key="{event_key}" '
                f'aria-label="{html.escape(aria, quote=True)}" fill="transparent"/>'
            )
    if hit_areas:
        marker_start = re.search(r'<g\b[^>]*\bclass="[^"]*\bnews-marker\b', svg)
        insertion = marker_start.start() if marker_start else svg.rfind("</svg>")
        svg = svg[:insertion] + "".join(hit_areas) + svg[insertion:]
    return svg


def _embedded_chart_html(
    markdown_path: Path,
    line: str,
    dashboard: dict[str, object] | None = None,
) -> str | None:
    match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+\.svg)\)", line.strip())
    if not match:
        return None
    target = match.group(2).strip()
    if Path(target).name != target:
        return None
    candidate = (markdown_path.parent / target).resolve()
    if candidate.parent != markdown_path.parent.resolve() or not candidate.exists():
        return None
    if not candidate.name.startswith(markdown_path.stem):
        return None
    svg = candidate.read_text(encoding="utf-8")
    svg = re.sub(r"^<\?xml[^>]+>\s*", "", svg)
    if dashboard is not None:
        svg = _enrich_formal_chart_svg(svg, dashboard)
    return f'<figure class="chart-figure" aria-label="{html.escape(match.group(1), quote=True)}">{svg}</figure>'


def _formal_dashboard_body(markdown_path: Path, dashboard: dict[str, object]) -> list[str]:
    lines = [f'<main class="report-shell"><h1>{_inline_markdown_html(str(dashboard["title"]))}</h1>']
    meta = list(dashboard.get("meta") or [])
    if meta:
        lines.append('<div class="report-meta">')
        lines.extend(f"<p>{_inline_markdown_html(str(item))}</p>" for item in meta)
        lines.append("</div>")
    if dashboard.get("warning"):
        lines.append(f'<blockquote>{_inline_markdown_html(str(dashboard["warning"]))}</blockquote>')
    if dashboard.get("extreme_notice") and dashboard.get("extreme_notice") != dashboard.get("warning"):
        lines.append(
            f'<blockquote class="extreme-notice">{_inline_markdown_html(str(dashboard["extreme_notice"]))}</blockquote>'
        )
    lines.append('<section class="market-lede" aria-label="本窗口市场主线"><h2>本窗口市场主线</h2>')
    for item in list(dashboard.get("market_items") or [])[:3]:
        lines.append(f"<p>{_inline_markdown_html(str(item))}</p>")
    lines.append("</section>")
    lines.append('<h2 class="section-title">Brent关键指标</h2>')
    lines.append('<div class="metric-grid" aria-label="Brent关键指标">')
    for label, value in list(dashboard.get("metrics") or []):
        movement_class = ""
        if label == "24小时涨跌幅":
            movement_class = " metric-up" if str(value).startswith("+") else " metric-down" if str(value).startswith("-") else ""
        lines.append(
            '<div class="metric-card">'
            f'<span class="metric-label">{_inline_markdown_html(str(label))}</span>'
            f'<span class="metric-value{movement_class}">{_inline_markdown_html(str(value))}</span>'
            "</div>"
        )
    lines.append("</div>")
    chart_line = str(dashboard.get("chart_line") or "")
    chart = _embedded_chart_html(markdown_path, chart_line, dashboard) if chart_line else None
    if chart:
        lines.append(chart)
    else:
        lines.append('<p class="chart-missing">本窗口可用价格数据不足，未生成K线图。</p>')
    lines.append(
        '<div id="related-news-panel" class="news-panel" aria-live="polite">'
        '<p class="news-panel-empty">点击上方K线图中的蓝色新闻时间点，查看该时段的相关新闻与市场解读。</p>'
        "</div>"
    )
    lines.append('<div class="summary-grid">')
    lines.append('<section id="iran-update-card" class="summary-card"><h2>ISW Iran Update</h2><ul>')
    for item in list(dashboard.get("iran_update_items") or [])[:7]:
        text = html.escape(str(item.get("text") or ""))
        url = str(item.get("url") or "")
        label = str(item.get("link_label") or "查看ISW原文")
        if url.startswith(("https://", "http://")):
            text += f' <a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(label)} ↗</a>'
        lines.append(f"<li>{text}</li>")
    lines.append("</ul></section>")
    lines.append('<section id="tracking-card" class="summary-card"><h2>今日关注什么</h2><ul>')
    for item in list(dashboard.get("tracking_items") or []):
        text = html.escape(str(item.get("text") or ""))
        url = str(item.get("url") or "")
        if url.startswith(("https://", "http://")):
            text += f' <a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>'
        lines.append(f"<li>{text}</li>")
    lines.append("</ul></section></div>")
    audit_link = str(dashboard.get("audit_link") or "")
    if audit_link:
        lines.append(f'<div class="audit-link">{_inline_markdown_html(audit_link)}</div>')
    event_json = json.dumps(list(dashboard.get("events") or []), ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    lines.append(f'<script type="application/json" id="brent-event-data">{event_json}</script>')
    panel_payload = {
        "presentation_available": bool(dashboard.get("has_panel_presentation")),
        "panel_groups": list(dashboard.get("panel_groups") or []),
        "unmatched_major_items": list(dashboard.get("unmatched_major_items") or []),
        "all_day_items": list(dashboard.get("all_day_items") or []),
        "display_summary": dict(dashboard.get("display_summary") or {}),
    }
    panel_json = json.dumps(panel_payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    lines.append(f'<script type="application/json" id="brent-panel-data">{panel_json}</script>')
    lines.append("</main>")
    return lines


def build_html_report(
    markdown_path: Path,
    output_dir: Path,
    presentation: FormalReportPresentation | None = None,
) -> Path:
    text = markdown_path.read_text(encoding="utf-8")
    stem = markdown_path.stem
    is_audit = "Rolling_Audit" in stem
    if is_audit:
        if "FAILED" in stem:
            page_title = "金十-Brent滚动日报审计报告（FAILED）"
        elif "PARTIAL" in stem:
            page_title = "金十-Brent滚动日报审计报告（PARTIAL）"
        else:
            page_title = "金十-Brent滚动日报审计报告"
    elif "FAILED" in stem:
        page_title = "金十-Brent数据更新失败说明"
    elif "PARTIAL" in stem:
        page_title = "金十-Brent滚动日报（缓存复盘版）"
    else:
        page_title = "金十新闻事件 + Brent市场反应滚动日报"
    audit_styles = [
        ":root{color-scheme:light;--ink:#182033;--blue:#2e74b5;--line:#d8e1ef;--soft:#f5f7fb}",
        "*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.62;margin:40px auto;padding:0 28px;max-width:1320px;color:var(--ink);background:#fff;overflow-x:hidden}",
        "h1{color:#0b2545;line-height:1.25}h2{color:var(--blue);border-bottom:1px solid var(--line);padding-bottom:5px;margin-top:34px}h3{color:#8f2525;scroll-margin-top:18px}h4{color:#334155}",
        "blockquote{background:var(--soft);border-left:4px solid var(--blue);padding:9px 14px;color:#4b5563;margin-left:0}",
        "code{background:#f2f4f7;padding:1px 4px}a{color:#0758b8;text-underline-offset:2px}ul{padding-left:24px}li{margin:4px 0}",
        ".chart-figure{margin:18px 0 24px;padding:0;max-width:100%;overflow-x:auto}.chart-figure svg{display:block;width:100%;height:auto;min-width:980px}",
        ".table-wrap{overflow-x:auto;margin:14px 0 26px;border:1px solid var(--line);border-radius:10px}table{width:100%;border-collapse:collapse;font-size:14px;white-space:nowrap}th{background:#f5f8fc;color:#334155;text-align:left}th,td{padding:9px 11px;border-bottom:1px solid #e6ebf1}tbody tr:last-child td{border-bottom:0}td:nth-last-child(-n+3){font-family:'SFMono-Regular',Consolas,monospace}",
        "@media(max-width:760px){body{margin:20px auto;padding:0 16px}.chart-figure{margin-left:-16px;margin-right:-16px;padding:0 16px}h1{font-size:27px}h2{font-size:21px}}",
        "@media print{body{max-width:none;margin:16px;padding:0}.chart-figure svg{min-width:0}a{color:inherit;text-decoration:none}}",
    ]
    formal_styles = [
        ":root{color-scheme:light;--ink:#172033;--muted:#64748b;--line:#dfe6ef;--soft:#f6f8fb;--news:#2563eb;--up:#c2413b;--down:#1b7f5a}",
        "*{box-sizing:border-box}body{margin:0;padding:34px 24px;background:#f3f6fa;color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.62;overflow-x:hidden}",
        ".report-shell{max-width:1320px;margin:0 auto;padding:42px 48px 34px;background:#fff;border:1px solid #e4eaf1;border-radius:22px;box-shadow:0 18px 48px rgba(15,23,42,.07)}",
        "h1{margin:0;color:#0f2747;font-size:30px;line-height:1.25;letter-spacing:-.02em}.report-meta{margin:10px 0 28px}.report-meta p{margin:2px 0;color:var(--muted);font-size:14px}",
        "h2{margin:0;color:#152a46;font-size:19px;line-height:1.35}.section-title{margin:30px 0 12px}p{margin:8px 0;color:#334155}blockquote{margin:16px 0;padding:10px 14px;border-left:3px solid #94a3b8;background:var(--soft);color:#475569}",
        "a{color:#165dcc;text-underline-offset:3px}ul{margin:8px 0;padding-left:22px}li{margin:8px 0}",
        ".context-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:18px 0 24px}.context-card{padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:var(--soft)}.context-card h2{margin-bottom:8px}.context-card ul{margin-bottom:0}.context-card li{color:#475569;font-size:13px}",
        ".market-lede{margin:22px 0 26px;padding:16px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.market-lede h2{margin:0 0 8px}.market-lede p{margin:4px 0;color:#2c3b52;font-size:15px}",
        ".metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:12px 0 22px}.metric-card{padding:18px 18px 16px;border:1px solid var(--line);border-radius:14px;background:#fff}.metric-label{display:block;margin-bottom:7px;color:var(--muted);font-size:13px}.metric-value{display:block;font:700 26px/1.15 'SFMono-Regular',Consolas,monospace;color:#152a46}.metric-value.metric-up{color:var(--up)}.metric-value.metric-down{color:var(--down)}",
        ".news-panel{min-height:92px;margin:14px 0 0;padding:18px 20px;border:1px solid #8bb7f0;border-radius:14px;background:#eff6ff;box-shadow:0 8px 22px rgba(37,99,235,.08)}.news-panel-empty{margin:14px 0;color:#315c91;text-align:center}.news-panel-title{margin:0;color:#163f75;font-size:18px}.news-display-summary{margin:5px 0 12px;color:#496581;font-size:13px}.event-summary{display:flex;flex-wrap:wrap;gap:8px 18px;margin:8px 0 12px;color:#35506f;font-size:14px}.event-reactions{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:12px 0}.event-reaction{padding:8px 10px;border:1px solid #c7dcf5;border-radius:9px;background:#fff}.event-reaction span{display:block;color:var(--muted);font-size:11px}.event-reaction strong{font:700 14px/1.3 'SFMono-Regular',Consolas,monospace}.news-list-heading{margin:16px 0 4px;color:#244b78;font-size:14px}.news-entry{padding:12px 0}.news-entry+.news-entry{border-top:1px solid #cbdcf1}.news-entry[hidden]{display:none}.news-time{color:var(--muted);font:600 12px/1.4 'SFMono-Regular',Consolas,monospace}.news-title{display:block;margin:4px 0 5px;color:#173f7a;font-size:15px;font-weight:650}.news-meta{margin:4px 0 0;color:#52647b;font-size:13px}.news-source-link{display:inline-block;margin-top:7px;font-size:13px;font-weight:650}.news-expand,.all-day-button{margin-top:10px;padding:7px 12px;border:1px solid #8eb7ea;border-radius:8px;background:#fff;color:#174d88;font:650 13px/1.2 inherit;cursor:pointer}.market-interpretation{margin-top:14px;padding-top:12px;border-top:1px solid #cbdcf1}.attribution-line{margin-top:9px;color:#244b78;font-weight:650}.unexplained-message{margin:14px 0 6px;color:#334155;font-weight:650}",
        ".chart-figure{margin:0;max-width:100%;overflow-x:auto;border:1px solid var(--line);border-radius:16px;background:#fff}.chart-figure svg{display:block;width:100%;height:auto;min-width:1040px}.unmatched-band-hit{cursor:pointer;pointer-events:all;outline:none;fill:transparent;stroke:transparent;stroke-width:2}.unmatched-band-hit:hover,.unmatched-band-hit:focus,.unmatched-band-hit.is-active{fill:rgba(148,163,184,.18);stroke:#64748b}.news-marker.is-active .marker-tag{fill:#dbeafe;stroke:#1d4ed8}.news-marker.is-active .marker-dot{r:7}",
        ".summary-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:20px 0 0}.summary-card{padding:20px 22px;border:1px solid var(--line);border-radius:14px;background:#fff}.summary-card h2{margin-bottom:10px}.summary-card ul{margin-bottom:0}.summary-card li{color:#3d4e65}",
        ".audit-link{margin-top:34px;padding-top:22px;border-top:1px solid var(--line)}.audit-link a{display:inline-flex;padding:10px 15px;border:1px solid #b9c8db;border-radius:10px;background:#fff;color:#244b78;font-weight:650;text-decoration:none}.audit-link a:hover{background:#f5f8fc}",
        ".table-wrap{overflow-x:auto;margin:14px 0 26px;border:1px solid var(--line);border-radius:10px}table{width:100%;border-collapse:collapse;font-size:14px;white-space:nowrap}th,td{padding:9px 11px;border-bottom:1px solid #e6ebf1}",
        "@media(max-width:820px){body{padding:0;background:#fff}.report-shell{padding:26px 16px 30px;border:0;border-radius:0;box-shadow:none}.context-grid{grid-template-columns:1fr}.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.event-reactions{grid-template-columns:repeat(2,minmax(0,1fr))}.summary-grid{grid-template-columns:1fr}.chart-figure{margin-left:-16px;margin-right:-16px;border-radius:0;padding:0 16px;border-left:0;border-right:0}h1{font-size:25px}.metric-value{font-size:22px}.news-panel{padding:16px 14px}.summary-card{padding:18px 16px}}",
        "@media print{body{padding:0;background:#fff}.report-shell{max-width:none;padding:0;border:0;box-shadow:none}.chart-figure svg{min-width:0}.news-panel{display:none}a{color:inherit;text-decoration:none}}",
    ]
    html_lines = [
        "<!doctype html>",
        "<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>{html.escape(page_title)}</title>",
        "<style>",
        *(audit_styles if is_audit else formal_styles),
        f"</style></head><body class=\"{'audit-report' if is_audit else 'formal-report'}\">",
    ]
    if not is_audit:
        dashboard = _parse_formal_dashboard(text, presentation)
        html_lines.extend(_formal_dashboard_body(markdown_path, dashboard))
        html_lines.extend(
            [
                "<script>",
                """(() => {
  const panel = document.getElementById('related-news-panel');
  const panelDataNode = document.getElementById('brent-panel-data');
  if (!panel || !panelDataNode) return;
  let panelData = {};
  try { panelData = JSON.parse(panelDataNode.textContent || '{}'); } catch (error) { panelData = {}; }
  const panelGroups = Array.isArray(panelData.panel_groups) ? panelData.panel_groups : [];
  const unmatchedMajorItems = Array.isArray(panelData.unmatched_major_items) ? panelData.unmatched_major_items : [];
  const allDayItems = Array.isArray(panelData.all_day_items) ? panelData.all_day_items : [];
  const displaySummary = panelData.display_summary && typeof panelData.display_summary === 'object' ? panelData.display_summary : {};
  const addText = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value || '';
    return node;
  };
  const validJin10Url = (value) => {
    try {
      const url = new URL(String(value || ''));
      return (url.protocol === 'http:' || url.protocol === 'https:')
        && (url.hostname === 'jin10.com' || url.hostname.endsWith('.jin10.com')) ? url.href : '';
    } catch (error) {
      return '';
    }
  };
  const appendNews = (news, attributionLabel = '') => {
    const entry = document.createElement('article');
    entry.className = 'news-entry';
    entry.appendChild(addText('div', 'news-time', (news.time || 'N/A') + ' 北京时间'));
    entry.appendChild(addText('h5', 'news-title', news.title || '重大新闻'));
    if (news.summary) entry.appendChild(addText('p', 'news-meta', news.summary));
    entry.appendChild(addText('p', 'news-meta', '影响说明：' + (news.impact || 'N/A')));
    entry.appendChild(addText('p', 'news-meta', '价格状态：' + (news.price_status || 'N/A')));
    entry.appendChild(addText('p', 'news-meta', '反应状态：' + (news.reaction_status || 'UNCONFIRMED')));
    if (news.reaction_status_note) entry.appendChild(addText('p', 'news-meta', news.reaction_status_note));
    if (attributionLabel) entry.appendChild(addText('p', 'attribution-line', attributionLabel));
    const url = validJin10Url(news.url);
    if (url) {
      const link = document.createElement('a');
      link.className = 'news-source-link';
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = '查看金十原文 ↗';
      entry.appendChild(link);
    }
    panel.appendChild(entry);
  };
  const appendGroup = (group) => {
    panel.appendChild(addText('h4', 'news-list-heading', (group.time_range || '价格异动') + '｜Brent' + (group.direction || '') + ' ' + (group.cumulative || '')));
    const summary = document.createElement('div');
    summary.className = 'event-summary';
    summary.appendChild(addText('span', '', group.max_30m === 'N/A' ? '最大30分钟：N/A' : '最大30分钟涨跌幅：' + (group.max_30m || 'N/A')));
    panel.appendChild(summary);
    const reactions = document.createElement('div');
    reactions.className = 'event-reactions';
    [['5分钟', group.pct_5m], ['15分钟', group.pct_15m], ['30分钟', group.pct_30m], ['60分钟', group.pct_60m], ['本轮累计', group.cumulative]].forEach(([label, value]) => {
      const card = document.createElement('div');
      card.className = 'event-reaction';
      card.appendChild(addText('span', '', label));
      card.appendChild(addText('strong', '', value || 'N/A'));
      reactions.appendChild(card);
    });
    panel.appendChild(reactions);
    const news = Array.isArray(group.news) ? group.news : [];
    if (group.is_unexplained) {
      panel.appendChild(addText('p', 'unexplained-message', news.length
        ? '该段波动原因暂无法确认；以下新闻仅作相关背景，不作价格归因。'
        : '暂未找到符合条件的金十新闻，原因暂无法确认，需人工继续核查。'));
    } else if (group.interpretation) {
      panel.appendChild(addText('p', 'market-interpretation', group.interpretation));
    }
    panel.appendChild(addText('h4', 'news-list-heading', group.is_unexplained ? '该时段相关背景（未作价格归因）' : '该时段重要新闻'));
    if (news.length) news.forEach((item) => appendNews(item, group.is_unexplained ? '相关背景，未作价格归因' : ''));
    else panel.appendChild(addText('p', 'unexplained-message', '暂未找到符合条件的金十新闻，原因暂无法确认，需人工继续核查。'));
  };
  const appendUnmatchedMajorItems = () => {
    if (!unmatchedMajorItems.length) return;
    panel.appendChild(addText('h4', 'news-list-heading', '⚠ 重大事件'));
    unmatchedMajorItems.forEach(appendNews);
  };
  const panelByKey = new Map(panelGroups.map((group) => [group.key, group]));
  const triggers = [...document.querySelectorAll('[data-event-key]')];
  const clearActive = () => triggers.forEach((item) => item.classList.remove('is-active'));
  const renderAllDay = () => {
    clearActive();
    panel.replaceChildren();
    panel.appendChild(addText('h3', 'news-panel-title', '全日重要新闻'));
    if (displaySummary.text) panel.appendChild(addText('p', 'news-display-summary', displaySummary.text));
    if (allDayItems.length) allDayItems.forEach(appendNews);
    else if (!unmatchedMajorItems.length) panel.appendChild(addText('p', 'unexplained-message', '本窗口暂无符合展示口径的新闻。'));
    appendUnmatchedMajorItems();
  };
  const buildAllDayButton = () => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'all-day-button';
    button.textContent = '查看全日重要新闻';
    button.addEventListener('click', renderAllDay);
    return button;
  };
  const showGroup = (key) => {
    const group = panelByKey.get(key);
    if (!group) return;
    clearActive();
    triggers.filter((item) => item.getAttribute('data-event-key') === key).forEach((item) => item.classList.add('is-active'));
    panel.replaceChildren();
    panel.appendChild(buildAllDayButton());
    appendGroup(group);
  };
  if (panelData.presentation_available) {
    triggers.forEach((trigger) => {
      const show = () => showGroup(trigger.getAttribute('data-event-key') || '');
      trigger.addEventListener('click', show);
      trigger.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          show();
        }
      });
    });
    renderAllDay();
    return;
  }
  const dataNode = document.getElementById('brent-event-data');
  if (!dataNode) return;
  let events = [];
  try { events = JSON.parse(dataNode.textContent || '[]'); } catch (error) { events = []; }
  const byKey = new Map(events.map((item) => [item.key, item]));
  const showEvent = (trigger) => {
    const item = byKey.get(trigger.getAttribute('data-event-key') || '');
    if (!item) return;
    clearActive();
    trigger.classList.add('is-active');
    panel.replaceChildren();
    const heading = addText('h3', 'news-panel-title', item.time_range + '｜Brent' + item.direction + ' ' + item.cumulative);
    panel.appendChild(heading);
    if(item.kind==='unexplained'){
      const backgroundNews = Array.isArray(item.news) ? item.news : [];
      panel.appendChild(addText('p', 'unexplained-message', backgroundNews.length
        ? '该段波动原因暂无法确认；以下新闻仅作相关背景，不作价格归因。'
        : '暂未找到符合条件的金十新闻，原因暂无法确认，需人工继续核查。'));
      panel.appendChild(addText('p', 'attribution-line', '归因：暂无法确认｜置信度：低'));
      if (backgroundNews.length) {
        panel.appendChild(addText('h4', 'news-list-heading', '该时段相关背景（未作价格归因）'));
        backgroundNews.forEach((news) => {
          const entry = document.createElement('article');
          entry.className = 'news-entry';
          entry.appendChild(addText('div', 'news-time', (news.time || 'N/A') + ' 北京时间'));
          const title = document.createElement('a');
          title.className = 'news-title';
          title.href = news.url;
          title.target = '_blank';
          title.rel = 'noopener noreferrer';
          title.textContent = news.title;
          entry.appendChild(title);
          entry.appendChild(addText('p', 'news-meta', '时间关系：' + news.relation + '｜信息新鲜度：' + news.freshness));
          entry.appendChild(addText('p', 'attribution-line', '相关背景，未作价格归因'));
          panel.appendChild(entry);
        });
      }
      return;
    }
    const summary=document.createElement('div');
    summary.className = 'event-summary';
    summary.appendChild(addText('span', '', item.max_30m === 'N/A' ? '最大30分钟：N/A' : '最大30分钟涨跌幅：' + item.max_30m));
    panel.appendChild(summary);
    const reactions = document.createElement('div');
    reactions.className = 'event-reactions';
    [['5分钟', item.pct_5m], ['15分钟', item.pct_15m], ['30分钟', item.pct_30m], ['60分钟', item.pct_60m], ['本轮累计', item.cumulative]].forEach(([label, value]) => {
      const card = document.createElement('div');
      card.className = 'event-reaction';
      card.appendChild(addText('span', '', label));
      card.appendChild(addText('strong', '', value));
      reactions.appendChild(card);
    });
    panel.appendChild(reactions);
    panel.appendChild(addText('h4', 'news-list-heading', '该时段正式入选新闻'));
    item.news.forEach((news, index) => {
      const entry = document.createElement('article');
      entry.className = 'news-entry';
      if (index >= 2) entry.hidden = true;
      entry.appendChild(addText('div', 'news-time', (news.time || 'N/A') + ' 北京时间'));
      const title = document.createElement('a');
      title.className = 'news-title';
      title.href = news.url;
      title.target = '_blank';
      title.rel = 'noopener noreferrer';
      title.textContent = news.title;
      entry.appendChild(title);
      entry.appendChild(addText('p', 'news-meta', '时间关系：' + news.relation + '｜信息新鲜度：' + news.freshness));
      const link = document.createElement('a');
      link.className = 'news-source-link';
      link.href = news.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = '查看金十原文 ↗';
      entry.appendChild(link);
      panel.appendChild(entry);
    });
    if (item.news.length > 2) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'news-expand';
      button.textContent = '展开全部';
      button.addEventListener('click', () => {
        const hidden = button.textContent === '展开全部';
        panel.querySelectorAll('.news-entry').forEach((entry, index) => {
          if (index >= 2) entry.hidden = !hidden;
        });
        button.textContent = hidden ? '收起' : '展开全部';
      });
      panel.appendChild(button);
    }
    if (item.interpretation) panel.appendChild(addText('p', 'market-interpretation', item.interpretation));
    panel.appendChild(addText('p', 'attribution-line', '归因：' + item.judgment + '｜置信度：' + item.confidence));
  };
  triggers.forEach((trigger) => {
    trigger.addEventListener('click', () => showEvent(trigger));
    trigger.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        showEvent(trigger);
      }
    });
  });
})();""",
                "</script>",
                "</body></html>",
            ]
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path = output_dir / (markdown_path.stem + ".html")
        html_path.write_text("\n".join(html_lines), encoding="utf-8")
        return html_path
    if not is_audit:
        html_lines.append('<main class="report-shell">')
    lines = text.splitlines()
    index = 0
    list_open = False

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            html_lines.append("</ul>")
            list_open = False

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        embedded = _embedded_chart_html(markdown_path, line)
        if embedded:
            close_list()
            html_lines.append(embedded)
            if not is_audit:
                html_lines.append(
                    '<div id="related-news-panel" class="news-panel" aria-live="polite">'
                    '<p class="news-panel-empty">点击图上的新闻时间点，查看新闻原文。</p></div>'
                )
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and _is_markdown_table_separator(lines[index + 1]):
            close_list()
            headers = _markdown_table_cells(line)
            index += 2
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_markdown_table_cells(lines[index]))
                index += 1
            if not is_audit and headers == ["最新价", "24小时涨跌幅", "日内最高价", "日内最低价"] and len(rows) == 1:
                html_lines.append('<div class="metric-grid" aria-label="Brent价格情况">')
                for label, value in zip(headers, rows[0]):
                    movement_class = ""
                    if label == "24小时涨跌幅":
                        movement_class = " metric-up" if value.startswith("+") else " metric-down" if value.startswith("-") else ""
                    html_lines.append(
                        '<div class="metric-card">'
                        f'<span class="metric-label">{_inline_markdown_html(label)}</span>'
                        f'<span class="metric-value{movement_class}">{_inline_markdown_html(value)}</span>'
                        "</div>"
                    )
                html_lines.append("</div>")
                continue
            html_lines.append('<div class="table-wrap"><table><thead><tr>')
            html_lines.extend(f"<th>{_inline_markdown_html(cell)}</th>" for cell in headers)
            html_lines.append("</tr></thead><tbody>")
            for row in rows:
                html_lines.append("<tr>")
                html_lines.extend(f"<td>{_inline_markdown_html(cell)}</td>" for cell in row)
                html_lines.append("</tr>")
            html_lines.append("</tbody></table></div>")
            continue
        if line.startswith("# "):
            close_list()
            html_lines.append(f"<h1>{_inline_markdown_html(line[2:])}</h1>")
        elif line.startswith("## "):
            close_list()
            html_lines.append(f"<h2>{_inline_markdown_html(line[3:])}</h2>")
        elif line.startswith("### "):
            close_list()
            heading = line[4:]
            event_match = re.search(r"【([EU]\d{2})｜", heading)
            anchor = f' id="event-{event_match.group(1).lower()}"' if event_match else ""
            html_lines.append(f"<h3{anchor}>{_inline_markdown_html(heading)}</h3>")
        elif line.startswith("#### "):
            close_list()
            html_lines.append(f"<h4>{_inline_markdown_html(line[5:])}</h4>")
        elif line.startswith("> "):
            close_list()
            html_lines.append(f"<blockquote>{_inline_markdown_html(line[2:])}</blockquote>")
        elif re.match(r"^\s*[-*] ", line):
            if not list_open:
                html_lines.append("<ul>")
                list_open = True
            item = re.sub(r"^\s*[-*] ", "", line)
            html_lines.append(f"<li>{_inline_markdown_html(item)}</li>")
        elif stripped == "":
            close_list()
        elif not is_audit and stripped.startswith("[查看完整 Audit →]"):
            close_list()
            html_lines.append(f'<div class="audit-link">{_inline_markdown_html(stripped)}</div>')
        else:
            close_list()
            html_lines.append(f"<p>{_inline_markdown_html(line)}</p>")
        index += 1
    close_list()
    if not is_audit:
        html_lines.extend(
            [
                "</main>",
                "<script>",
                "(()=>{const panel=document.getElementById('related-news-panel');const markers=[...document.querySelectorAll('.news-marker')];if(!panel)return;const show=(marker)=>{markers.forEach(item=>item.classList.remove('is-active'));marker.classList.add('is-active');let news=[];try{news=JSON.parse(marker.getAttribute('data-news')||'[]')}catch(error){news=[]}panel.replaceChildren();const heading=document.createElement('h3');heading.className='news-panel-title';heading.textContent='相关新闻';panel.appendChild(heading);news.forEach(item=>{const entry=document.createElement('article');entry.className='news-entry';const time=document.createElement('div');time.className='news-time';time.textContent=item.time+' 北京时间';entry.appendChild(time);let title;if(item.url){title=document.createElement('a');title.href=item.url;title.target='_blank';title.rel='noopener noreferrer'}else{title=document.createElement('span');title.classList.add('no-link')}title.classList.add('news-title');title.textContent=item.title;entry.appendChild(title);const reaction=document.createElement('p');reaction.className='news-reaction';reaction.textContent=item.reaction;entry.appendChild(reaction);if(item.url){const link=document.createElement('a');link.className='news-source-link';link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='打开新闻原文 ↗';entry.appendChild(link)}else{const missing=document.createElement('span');missing.className='news-link-missing';missing.textContent='原文链接暂无';entry.appendChild(missing)}panel.appendChild(entry)});panel.scrollIntoView({behavior:'smooth',block:'nearest'})};markers.forEach(marker=>{marker.addEventListener('click',()=>show(marker));marker.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();show(marker)}})})})();",
                "</script>",
            ]
        )
    html_lines.append("</body></html>")
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / (markdown_path.stem + ".html")
    html_path.write_text("\n".join(html_lines), encoding="utf-8")
    return html_path
