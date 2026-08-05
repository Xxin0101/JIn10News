from __future__ import annotations

import json
import logging
import socket
import time as time_module
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request

from .brent import BrentConfig, BrentFetchError, build_yahoo_chart_url, fetch_brent_5m, fetch_brent_5m_range
from .config import DEFAULT_HEADERS, FetchConfig, FilterConfig, JIN10_FLASH_API, TrackerConfig
from .eventing import event_type_for, has_direct_oil_market_relevance, identify_event, major_topic_reason
from .external_update import fetch_latest_isw_update
from .fetcher import Jin10FetchError, fetch_flashes_between, fetch_incremental_flashes
from .models import RunStats
from .processor import process_flashes
from .reaction import analyze_pending_reactions, analyze_reactions_for_range
from .storage import TrackerStore
from .time_utils import ensure_utc, iso_utc, parse_iso_utc, to_beijing, utc_now
from .tracking_report import (
    audit_html_path_for,
    audit_markdown_path_for,
    classify_report_status,
    beijing_day_bounds,
    _build_window_audit,
    build_html_report,
    build_markdown_report,
    rolling_price_bounds,
)
from .trump_update import fetch_trump_statements

RETRY_WAIT_SECONDS = (5, 15, 30)


def _fetch_trump_update(
    window_start_utc: datetime,
    window_end_utc: datetime,
    *,
    cache_dir: Path,
    timezone_name: str,
    retry_failed_cache: bool = False,
) -> dict[str, object]:
    try:
        return fetch_trump_statements(
            window_start_utc,
            window_end_utc,
            cache_dir=cache_dir,
            timezone_name=timezone_name,
            retry_failed_cache=retry_failed_cache,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "source": "金十数据特朗普专栏",
            "window_start_utc": iso_utc(window_start_utc),
            "window_end_utc": iso_utc(window_end_utc),
            "statement_count": 0,
            "relevant_source_item_count": 0,
            "statements": [],
            "failure_reason": str(exc),
            "cache_path": None,
        }


def _log_trump_update(logger: logging.Logger, trump_update: dict[str, object], *, context: str) -> None:
    if trump_update.get("status") == "success":
        logger.info(
            "%s特朗普专栏抓取完成: status=%s statements=%s relevant_sources=%s cache=%s",
            context,
            trump_update.get("status"),
            trump_update.get("statement_count"),
            trump_update.get("relevant_source_item_count"),
            trump_update.get("cache_path"),
        )
    else:
        logger.warning(
            "%s特朗普专栏抓取失败，不影响金十+Brent主日报: status=%s statements=%s relevant_sources=%s reason=%s cache=%s",
            context,
            trump_update.get("status"),
            trump_update.get("statement_count"),
            trump_update.get("relevant_source_item_count"),
            trump_update.get("failure_reason") or "N/A",
            trump_update.get("cache_path"),
        )


def _source_window_label(start_utc: datetime, end_utc: datetime, timezone_name: str) -> str:
    start_local = to_beijing(start_utc, timezone_name)
    end_local = to_beijing(end_utc, timezone_name)
    return f"{start_local.strftime('%Y-%m-%d %H:%M')} 至 {end_local.strftime('%Y-%m-%d %H:%M')} 北京时间"


def _classify_fetch_error(exc: Exception) -> tuple[str, str]:
    message = str(exc)
    lowered = message.lower()
    if "nodename nor servname" in lowered or "gaierror" in lowered:
        return "DNS", message
    if "http" in lowered:
        return "HTTP状态", message
    if "json" in lowered or "解析" in message:
        return "数据解析", message
    if "timeout" in lowered or "timed out" in lowered or "网络" in message:
        return "网络连接", message
    return "网络连接", message


def _endpoint_probe(
    *,
    source: str,
    endpoint: str,
    timeout: int,
    retries: int,
    last_success_at_utc: datetime | None,
    affected_window_start_utc: datetime,
    affected_window_end_utc: datetime,
    headers: dict[str, str],
    validator,
    timezone_name: str,
) -> dict[str, object]:
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
            req = urllib.request.Request(endpoint, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", resp.getcode())
                if int(status or 0) >= 400:
                    raise RuntimeError(f"HTTP {status}")
                body = resp.read().decode("utf-8")
            last_stage = "数据解析"
            payload = json.loads(body)
            validator(payload)
            return {
                "source": source,
                "ok": True,
                "stage": "ok",
                "error": "",
                "retries": attempt - 1,
                "last_success_at_utc": last_success_at_utc,
                "affected_window": _source_window_label(affected_window_start_utc, affected_window_end_utc, timezone_name),
                "endpoint": endpoint,
            }
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
        except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
            if isinstance(exc, TimeoutError):
                last_stage = "网络连接"
            last_error = str(exc)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            last_stage = "数据解析"
            last_error = str(exc)

        if attempt > retries:
            break
        wait_index = min(attempt - 1, len(RETRY_WAIT_SECONDS) - 1)
        time_module.sleep(RETRY_WAIT_SECONDS[wait_index])

    return {
        "source": source,
        "ok": False,
        "stage": last_stage,
        "error": last_error or "未知错误",
        "retries": retries,
        "last_success_at_utc": last_success_at_utc,
        "affected_window": _source_window_label(affected_window_start_utc, affected_window_end_utc, timezone_name),
        "endpoint": endpoint,
    }


def _validate_jin10_probe(payload: dict[str, object]) -> None:
    if payload.get("status") != 200 or "data" not in payload:
        raise ValueError(f"接口返回异常: status={payload.get('status')} message={payload.get('message')!r}")
    if not isinstance(payload.get("data"), list):
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


def preflight_source_checks(
    store: TrackerStore,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    logger: logging.Logger,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> list[dict[str, object]]:
    last_news_success = store.get_meta_datetime("last_successful_fetch_at_utc")
    last_price_success = store.get_meta_datetime("last_successful_brent_fetch_at_utc")
    jin10_endpoint = f"{JIN10_FLASH_API}?channel=-8200&vip=1"
    yahoo_endpoint = build_yahoo_chart_url(
        brent_config,
        store.latest_kline_time(brent_config.symbol, brent_config.interval),
    )
    results = [
        _endpoint_probe(
            source="金十新闻",
            endpoint=jin10_endpoint,
            timeout=20,
            retries=3,
            last_success_at_utc=last_news_success,
            affected_window_start_utc=window_start_utc,
            affected_window_end_utc=window_end_utc,
            headers=DEFAULT_HEADERS,
            validator=_validate_jin10_probe,
            timezone_name=tracker_config.timezone,
        ),
        _endpoint_probe(
            source="Brent行情",
            endpoint=yahoo_endpoint,
            timeout=20,
            retries=3,
            last_success_at_utc=last_price_success,
            affected_window_start_utc=window_start_utc,
            affected_window_end_utc=window_end_utc,
            headers={"User-Agent": "Mozilla/5.0"},
            validator=_validate_yahoo_probe,
            timezone_name=tracker_config.timezone,
        ),
    ]
    for result in results:
        last_success_text = to_beijing(result["last_success_at_utc"], tracker_config.timezone).strftime("%Y-%m-%d %H:%M:%S") if result["last_success_at_utc"] else "N/A"
        if result["ok"]:
            logger.info(
                "数据源预检通过: source=%s endpoint=%s retries=%s last_success=%s window=%s",
                result["source"],
                result["endpoint"],
                result["retries"],
                last_success_text,
                result["affected_window"],
            )
        else:
            logger.warning(
                "数据源预检失败: source=%s stage=%s error=%s retries=%s last_success=%s window=%s endpoint=%s",
                result["source"],
                result["stage"],
                result["error"],
                result["retries"],
                last_success_text,
                result["affected_window"],
                result["endpoint"],
            )
    return results


def log_window_validation(
    store: TrackerStore,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    logger: logging.Logger,
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> None:
    audit = _build_window_audit(
        store,
        brent_config,
        window_start_utc,
        window_end_utc,
        tracker_config.timezone,
    )
    news_rows = store.news_for_reaction_range(window_start_utc, window_end_utc)
    raw_rows = list(audit.get("raw_rows") or [])
    price_rows = list(audit.get("price_rows") or [])

    first_news = parse_iso_utc(news_rows[0]["published_at_utc"]) if news_rows else None
    last_news = parse_iso_utc(news_rows[-1]["published_at_utc"]) if news_rows else None
    first_raw = parse_iso_utc(raw_rows[0]["published_at_utc"]) if raw_rows else None
    last_raw = parse_iso_utc(raw_rows[-1]["published_at_utc"]) if raw_rows else None
    first_kline = parse_iso_utc(price_rows[0]["ts_utc"]) if price_rows else None
    last_kline = parse_iso_utc(price_rows[-1]["ts_utc"]) if price_rows else None
    coverage_ok = bool(audit.get("price_covered"))
    raw_news_ok = bool(audit.get("news_covered"))

    def bj(value: datetime | None) -> str:
        return to_beijing(value, tracker_config.timezone).strftime("%Y-%m-%d %H:%M:%S") if value else "N/A"

    def segment_counts() -> dict[str, int]:
        tz = ZoneInfo(tracker_config.timezone)
        start_local = window_start_utc.astimezone(tz)
        end_local = window_end_utc.astimezone(tz)
        segments = {
            "08:00-16:00": (start_local, datetime.combine(start_local.date(), datetime.min.time(), tzinfo=tz).replace(hour=16)),
            "16:00-00:00": (
                datetime.combine(start_local.date(), datetime.min.time(), tzinfo=tz).replace(hour=16),
                datetime.combine(end_local.date(), datetime.min.time(), tzinfo=tz),
            ),
            "00:00-08:00": (datetime.combine(end_local.date(), datetime.min.time(), tzinfo=tz), end_local),
        }
        result = dict.fromkeys(segments.keys(), 0)
        for row in raw_rows:
            published = parse_iso_utc(row["published_at_utc"])
            if not published:
                continue
            local = published.astimezone(tz)
            for label, (start, end) in segments.items():
                if start <= local < end:
                    result[label] += 1
                    break
        return result

    segments = segment_counts()
    effective_raw = 0
    major_raw = 0
    summary_without_event = 0
    night_oil = 0
    night_major = 0
    tz = ZoneInfo(tracker_config.timezone)
    for row in raw_rows:
        title = str(row["title"] or "")
        body = str(row["body"] or row["detail"] or "")
        direct, _ = has_direct_oil_market_relevance(title, body)
        major = major_topic_reason(title, body) if direct else None
        if direct:
            effective_raw += 1
        if major:
            major_raw += 1
        if int(row["is_summary"] or 0) and not direct:
            summary_without_event += 1
        published = parse_iso_utc(row["published_at_utc"])
        if published:
            local_time = published.astimezone(tz).time()
            is_night = local_time >= datetime.strptime("16:00", "%H:%M").time() or local_time < datetime.strptime("08:00", "%H:%M").time()
            if is_night and direct:
                night_oil += 1
            if is_night and major:
                night_major += 1

    logger.info(
        "08:00滚动窗口校验: report_start_bj=%s report_end_bj=%s "
        "first_raw_bj=%s last_raw_bj=%s first_news_bj=%s last_news_bj=%s "
        "first_brent_bj=%s last_brent_bj=%s price_boundary_covered=%s price_continuity_ok=%s price_window_covered=%s "
        "raw_news_window_covered=%s raw_news_total=%s filtered_news_total=%s "
        "segment_0800_1600=%s segment_1600_0000=%s segment_0000_0800=%s price_klines=%s "
        "price_missing=%s price_5m_missing=%s price_15m_missing=%s price_30m_missing=%s price_60m_missing=%s",
        bj(window_start_utc),
        bj(window_end_utc),
        bj(first_raw),
        bj(last_raw),
        bj(first_news),
        bj(last_news),
        bj(first_kline),
        bj(last_kline),
        "yes" if audit.get("price_boundary_covered") else "no",
        "yes" if audit.get("price_continuity_ok", True) else "no",
        "yes" if coverage_ok else "no",
        "yes" if raw_news_ok else "no",
        len(raw_rows),
        len(news_rows),
        segments["08:00-16:00"],
        segments["16:00-00:00"],
        segments["00:00-08:00"],
        len(price_rows),
        audit.get("price_missing_count", 0),
        (audit.get("price_point_missing_counts") or {}).get("5m", 0),
        (audit.get("price_point_missing_counts") or {}).get("15m", 0),
        (audit.get("price_point_missing_counts") or {}).get("30m", 0),
        (audit.get("price_point_missing_counts") or {}).get("60m", 0),
    )
    logger.info(
        "原始新闻候选池审计: raw_total=%s effective_oil_raw=%s summary_without_event=%s "
        "major_forced_candidates=%s night_raw_total=%s night_oil_raw=%s night_major_candidates=%s",
        len(raw_rows),
        effective_raw,
        summary_without_event,
        major_raw,
        segments["16:00-00:00"] + segments["00:00-08:00"],
        night_oil,
        night_major,
    )


def _partial_report_day_labels(output_dir: Path) -> list[str]:
    labels: set[str] = set()
    for status in ("PARTIAL", "FAILED"):
        for path in output_dir.glob(f"Jin10_Brent_Rolling_Reaction_*_0800_{status}.md"):
            parts = path.stem.split("_")
            if len(parts) < 7:
                continue
            token = parts[4]
            if len(token) == 8 and token.isdigit():
                labels.add(f"{token[:4]}-{token[4:6]}-{token[6:8]}")
    return sorted(labels)


def _source_checks_for_window(
    source_checks: list[dict[str, object]],
    start_utc: datetime,
    end_utc: datetime,
    timezone_name: str,
) -> list[dict[str, object]]:
    window = _source_window_label(start_utc, end_utc, timezone_name)
    return [{**item, "affected_window": window} for item in source_checks]


def _cleanup_superseded_partial_outputs(output_dir: Path, day_token: str, logger: logging.Logger) -> None:
    patterns = [
        f"Jin10_Brent_Rolling_Reaction_{day_token}_0800_PARTIAL.*",
        f"Jin10_Brent_Rolling_Reaction_{day_token}_0800_FAILED.*",
        f"Jin10_Brent_Rolling_Audit_{day_token}_0800_PARTIAL.*",
        f"Jin10_Brent_Rolling_Audit_{day_token}_0800_FAILED.*",
    ]
    for pattern in patterns:
        for path in output_dir.glob(pattern):
            try:
                path.unlink()
                logger.info("已清理被正式回补报告替代的降级文件: %s", path)
            except OSError as exc:
                logger.warning("清理旧降级文件失败: path=%s error=%s", path, exc)


def recover_partial_reports_after_successful_fetch(
    store: TrackerStore,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    source_checks: list[dict[str, object]],
    logger: logging.Logger,
    log_path: Path | None = None,
) -> list[dict[str, object]]:
    output_dir = Path(tracker_config.output_dir)
    recovered: list[dict[str, object]] = []
    for day_label in _partial_report_day_labels(output_dir):
        day = datetime.strptime(day_label, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tracker_config.timezone))
        day_start, day_end, day_token = beijing_day_bounds(day, timezone_name=tracker_config.timezone)
        adjusted_checks = _source_checks_for_window(source_checks, day_start, day_end, tracker_config.timezone)

        reaction_counts = analyze_reactions_for_range(
            store,
            brent_config,
            tracker_config,
            day_start,
            day_end,
            logger=logger,
        )
        log_window_validation(store, brent_config, tracker_config, logger, day_start, day_end)
        store.refresh_multi_event_groups(day_start, day_end)
        audit_snapshot = _build_window_audit(store, brent_config, day_start, day_end, tracker_config.timezone)
        report_status, runtime_status = classify_report_status(audit_snapshot, adjusted_checks)
        runtime_status = {
            **runtime_status,
            "affected_window": _source_window_label(day_start, day_end, tracker_config.timezone),
            "log_path": str(log_path) if log_path else None,
        }
        trump_update = _fetch_trump_update(
            day_start,
            day_end,
            cache_dir=Path(tracker_config.database_path).parent / "external_updates",
            timezone_name=tracker_config.timezone,
        )
        _log_trump_update(logger, trump_update, context=f"历史缺口回补({day_label})")
        report_result = build_markdown_report(
            store,
            brent_config,
            output_dir,
            timezone_name=tracker_config.timezone,
            day=day,
            source_checks=adjusted_checks,
            runtime_summary=runtime_status,
            trump_update=trump_update,
        )
        markdown = report_result.markdown_path
        html = build_html_report(markdown, output_dir, presentation=report_result.presentation)
        audit_markdown = audit_markdown_path_for(markdown)
        audit_html = audit_html_path_for(markdown)
        if report_status == "FORMAL":
            _cleanup_superseded_partial_outputs(output_dir, day_token, logger)
        logger.info(
            "已完成历史缺口回补: day=%s status=%s markdown=%s html=%s audit=%s audit_html=%s analyzed=%s daily=%s",
            day_label,
            report_status,
            markdown,
            html,
            audit_markdown,
            audit_html,
            reaction_counts["analyzed"],
            reaction_counts["daily"],
        )
        recovered.append(
            {
                "day": day_label,
                "report_status": report_status,
                "markdown_path": str(markdown),
                "html_path": str(html),
                "audit_markdown_path": str(audit_markdown),
                "audit_html_path": str(audit_html),
                "reaction_counts": reaction_counts,
            }
        )
    return recovered


def run_once(
    store: TrackerStore,
    fetch_config: FetchConfig,
    filter_config: FilterConfig,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    logger: logging.Logger,
    generate_report: bool = True,
    log_path: Path | None = None,
) -> dict[str, object]:
    run_started = utc_now()
    stats = RunStats(fetch_started_at=run_started)
    inserted_news = 0
    processed_news = 0
    raw_news_rows = 0
    brent_rows = 0
    markdown_path: Path | None = None
    html_path: Path | None = None
    external_update: dict[str, object] = {"status": "not_run"}
    trump_update: dict[str, object] = {"status": "not_run"}
    audit_markdown_path: Path | None = None
    audit_html_path: Path | None = None
    news_fetch_succeeded = False
    price_fetch_succeeded = False
    recovered_reports: list[dict[str, object]] = []

    day_start, day_end, _ = beijing_day_bounds(timezone_name=tracker_config.timezone)
    source_checks = preflight_source_checks(
        store,
        brent_config,
        tracker_config,
        logger,
        day_start,
        day_end,
    )
    source_status = {str(item.get("source") or ""): item for item in source_checks}
    news_precheck_ok = bool((source_status.get("金十新闻") or source_status.get("金十新闻接口") or {}).get("ok"))
    price_precheck_ok = bool((source_status.get("Brent行情") or source_status.get("Yahoo Finance Brent") or {}).get("ok"))

    since_utc = store.get_meta_datetime("last_successful_fetch_at_utc")
    cutoff_for_known = (since_utc or (run_started - timedelta(hours=tracker_config.first_run_lookback_hours))) - timedelta(
        minutes=tracker_config.incremental_overlap_minutes
    )
    known_ids = store.recent_news_ids(cutoff_for_known)

    try:
        if news_precheck_ok:
            raw_items = fetch_incremental_flashes(
                fetch_config,
                stats,
                since_utc=since_utc,
                known_ids=known_ids,
                overlap_minutes=tracker_config.incremental_overlap_minutes,
            )
            fetched_at = utc_now()
            raw_news_rows = store.upsert_raw_news(
                raw_items,
                fetched_at,
                timezone_name=tracker_config.timezone,
                fetch_batch_id=iso_utc(run_started),
            )
            processed, duplicate_count = process_flashes(raw_items, filter_config)
            processed_news = len(processed)
            stats.duplicate_merged_total = duplicate_count
            stats.filtered_total = processed_news
            for item in processed:
                published_utc = ensure_utc(item.published_at)
                event_id, event_topic = identify_event(item.title_or_text, item.original_text)
                has_previous = store.event_has_previous(event_id, published_utc)
                event_type = event_type_for(item.title_or_text, item.original_text, has_previous)
                if store.upsert_news(
                    item,
                    event_id=event_id,
                    event_topic=event_topic,
                    event_type=event_type,
                    fetched_at_utc=fetched_at,
                    timezone_name=tracker_config.timezone,
                ):
                    inserted_news += 1
            store.set_meta("last_successful_fetch_at_utc", iso_utc(fetched_at))
            news_fetch_succeeded = True
            logger.info(
                "金十增量抓取完成: raw=%s filtered=%s inserted=%s duplicate_merged=%s",
                len(raw_items),
                processed_news,
                inserted_news,
                duplicate_count,
            )
            logger.info("金十原始新闻入库完成: raw_items=%s upsert_changes=%s", len(raw_items), raw_news_rows)
        else:
            news_error = source_status.get("金十新闻") or source_status.get("金十新闻接口") or {}
            stats.api_status = "failed"
            if news_error:
                stats.failures.append(
                    "金十新闻预检失败: "
                    f"阶段={news_error.get('stage')} 错误={news_error.get('error')} "
                    f"重试次数={news_error.get('retries')}"
                )
            logger.warning("金十新闻预检失败，本轮跳过正式抓取，改用本地缓存继续分析。")
    except Jin10FetchError as exc:
        stats.api_status = "failed"
        stats.failures.append(str(exc))
        logger.exception("金十新闻抓取失败，Brent更新和既有Pending分析仍将继续: %s", exc)

    try:
        if price_precheck_ok:
            latest = store.latest_kline_time(brent_config.symbol, brent_config.interval)
            brent_result = fetch_brent_5m(brent_config, latest_ts_utc=latest)
            brent_rows = store.upsert_klines(
                brent_config.symbol,
                brent_config.interval,
                brent_result.klines,
                brent_result.fetched_at_utc,
            )
            store.set_meta("last_successful_brent_fetch_at_utc", iso_utc(brent_result.fetched_at_utc))
            price_fetch_succeeded = True
            if brent_result.note:
                logger.warning(brent_result.note)
            logger.info("Brent 5分钟K线更新完成: fetched=%s upsert_changes=%s", len(brent_result.klines), brent_rows)
        else:
            price_error = source_status.get("Brent行情") or source_status.get("Yahoo Finance Brent") or {}
            if price_error:
                logger.warning(
                    "Brent行情预检失败，本轮跳过正式抓取: 阶段=%s 错误=%s 重试次数=%s",
                    price_error.get("stage"),
                    price_error.get("error"),
                    price_error.get("retries"),
                )
            else:
                logger.warning("Brent行情预检失败，本轮跳过正式抓取。")
    except BrentFetchError as exc:
        logger.exception("Brent价格数据抓取失败，新闻保存不受影响: %s", exc)

    reaction_counts = analyze_pending_reactions(store, brent_config, tracker_config, logger=logger)
    if generate_report:
        reaction_counts = analyze_reactions_for_range(
            store,
            brent_config,
            tracker_config,
            day_start,
            day_end,
            logger=logger,
        )
    log_window_validation(store, brent_config, tracker_config, logger, day_start, day_end)
    grouped = store.refresh_multi_event_groups(day_start, day_end)
    focus_rows = store.report_focus_rows(day_start, day_end)
    focus_count = len({str(row["multi_event_group_id"] or row["news_id"]) for row in focus_rows})
    logger.info(
        "Brent反应分析完成: analyzed=%s pending=%s price_matched=%s price_missing=%s "
        "price_5m_missing=%s price_15m_missing=%s price_30m_missing=%s price_60m_missing=%s "
        "effective_oil_news=%s summary_without_event=%s major_topic_hits=%s threshold_hits=%s "
        "final_daily=%s final_output_focus=%s excluded=%s multi_event_groups=%s",
        reaction_counts["analyzed"],
        reaction_counts["pending"],
        reaction_counts["price_matched"],
        reaction_counts["price_missing"],
        reaction_counts["price_5m_missing"],
        reaction_counts["price_15m_missing"],
        reaction_counts["price_30m_missing"],
        reaction_counts["price_60m_missing"],
        reaction_counts["effective_oil_news"],
        reaction_counts["summary_without_event"],
        reaction_counts["major_topic_hits"],
        reaction_counts["threshold_hits"],
        reaction_counts["daily"],
        focus_count,
        reaction_counts["excluded"],
        grouped,
    )

    audit_snapshot = _build_window_audit(store, brent_config, day_start, day_end, tracker_config.timezone)
    report_status, runtime_status = classify_report_status(audit_snapshot, source_checks)
    runtime_status = {
        **runtime_status,
        "affected_window": _source_window_label(day_start, day_end, tracker_config.timezone),
        "log_path": str(log_path) if log_path else None,
    }
    if generate_report:
        external_update = fetch_latest_isw_update(
            day_end,
            cache_dir=Path(tracker_config.database_path).parent / "external_updates",
            timezone_name=tracker_config.timezone,
        )
        if external_update.get("status") == "success":
            logger.info(
                "外部网站update抓取完成: method=%s title=%s et=%s bj=%s url=%s cache=%s",
                external_update.get("fetch_method"),
                external_update.get("title"),
                external_update.get("published_at_et"),
                external_update.get("published_at_bj"),
                external_update.get("url"),
                external_update.get("cache_path"),
            )
        else:
            logger.warning(
                "外部网站update抓取失败，不影响金十+Brent主日报: reason=%s cache=%s",
                external_update.get("failure_reason"),
                external_update.get("cache_path"),
            )
        trump_update = _fetch_trump_update(
            day_start,
            day_end,
            cache_dir=Path(tracker_config.database_path).parent / "external_updates",
            timezone_name=tracker_config.timezone,
        )
        _log_trump_update(logger, trump_update, context="")
        report_result = build_markdown_report(
            store,
            brent_config,
            Path(tracker_config.output_dir),
            timezone_name=tracker_config.timezone,
            report_status=report_status,
            source_checks=source_checks,
            runtime_summary=runtime_status,
            external_update=external_update,
            trump_update=trump_update,
        )
        markdown_path = report_result.markdown_path
        html_path = build_html_report(
            markdown_path,
            Path(tracker_config.output_dir),
            presentation=report_result.presentation,
        )
        audit_markdown_path = audit_markdown_path_for(markdown_path)
        audit_html_path = audit_html_path_for(markdown_path)
        logger.info("日报输出状态: %s / %s", html_path, markdown_path)
        logger.info("审计输出状态: %s / %s", audit_html_path, audit_markdown_path)
        if news_fetch_succeeded and price_fetch_succeeded:
            recovered_reports = recover_partial_reports_after_successful_fetch(
                store,
                brent_config,
                tracker_config,
                source_checks,
                logger,
                log_path=log_path,
            )
            if recovered_reports:
                logger.info("历史缺口回补报告数量: %s", len(recovered_reports))
            else:
                logger.info("历史缺口回补检查完成: 未发现待重算的PARTIAL/FAILED日报。")
        else:
            logger.info(
                "历史缺口回补跳过: news_fetch_succeeded=%s price_fetch_succeeded=%s",
                news_fetch_succeeded,
                price_fetch_succeeded,
            )
    else:
        logger.info("本轮仅更新数据库，不生成日报文件。")

    availability = runtime_status["formal_report_availability"]
    manual_rerun = runtime_status["manual_rerun_required"]
    news_update_status = runtime_status["news_update_status"]
    price_update_status = runtime_status["price_update_status"]
    data_integrity_status = runtime_status["data_integrity_status"]

    logger.info(
        "自动化摘要: 脚本执行状态=成功 | 金十新闻更新状态=%s | Brent价格更新状态=%s | 外部网站update抓取状态=%s | 数据完整性状态=%s | 正式日报可用性=%s | 是否需要人工重跑=%s | 日志路径=%s",
        news_update_status,
        price_update_status,
        "成功" if external_update.get("status") == "success" else "失败",
        data_integrity_status,
        availability,
        manual_rerun,
        str(log_path) if log_path else "N/A",
    )

    serializable_source_checks = []
    for item in source_checks:
        serializable_source_checks.append(
            {
                **item,
                "last_success_at_utc": iso_utc(item["last_success_at_utc"]) if isinstance(item.get("last_success_at_utc"), datetime) else item.get("last_success_at_utc"),
            }
        )

    return {
        "script_status": "success",
        "news_update_status": news_update_status,
        "price_update_status": price_update_status,
        "external_update_status": "成功" if external_update.get("status") == "success" else "失败",
        "external_update": external_update,
        "trump_update_status": "成功" if trump_update.get("status") == "success" else "失败",
        "trump_update": trump_update,
        "data_integrity_status": data_integrity_status,
        "formal_report_availability": availability,
        "manual_rerun_required": manual_rerun,
        "report_status": report_status,
        "log_path": str(log_path) if log_path else None,
        "source_checks": serializable_source_checks,
        "inserted_news": inserted_news,
        "raw_news_rows": raw_news_rows,
        "processed_news": processed_news,
        "brent_rows": brent_rows,
        "reaction_counts": reaction_counts,
        "markdown_path": str(markdown_path) if markdown_path else None,
        "html_path": str(html_path) if html_path else None,
        "audit_markdown_path": str(audit_markdown_path_for(markdown_path)) if markdown_path else None,
        "audit_html_path": str(audit_html_path_for(markdown_path)) if markdown_path else None,
        "recovered_reports": recovered_reports,
        "db_counts": store.counts(),
        "failures": stats.failures,
    }


def run_backfill_day(
    store: TrackerStore,
    fetch_config: FetchConfig,
    filter_config: FilterConfig,
    brent_config: BrentConfig,
    tracker_config: TrackerConfig,
    logger: logging.Logger,
    day_label: str,
    generate_report: bool = True,
    retry_failed_isw: bool = False,
    retry_failed_trump: bool = False,
) -> dict[str, object]:
    tz = ZoneInfo(tracker_config.timezone)
    day = datetime.strptime(day_label, "%Y-%m-%d").replace(tzinfo=tz)
    day_start, day_end, _ = beijing_day_bounds(day, timezone_name=tracker_config.timezone)
    stats = RunStats(fetch_started_at=utc_now())
    inserted_news = 0
    processed_news = 0
    raw_news_rows = 0
    brent_rows = 0
    markdown_path: Path | None = None
    html_path: Path | None = None
    external_update: dict[str, object] = {"status": "not_run"}
    trump_update: dict[str, object] = {"status": "not_run"}

    try:
        raw_items = fetch_flashes_between(fetch_config, stats, day_start, day_end)
        fetched_at = utc_now()
        raw_news_rows = store.upsert_raw_news(
            raw_items,
            fetched_at,
            timezone_name=tracker_config.timezone,
            fetch_batch_id=f"backfill_{day_label}_{iso_utc(fetched_at)}",
        )
        processed, duplicate_count = process_flashes(raw_items, filter_config)
        processed_news = len(processed)
        stats.duplicate_merged_total = duplicate_count
        stats.filtered_total = processed_news
        for item in processed:
            published_utc = ensure_utc(item.published_at)
            event_id, event_topic = identify_event(item.title_or_text, item.original_text)
            has_previous = store.event_has_previous(event_id, published_utc)
            event_type = event_type_for(item.title_or_text, item.original_text, has_previous)
            if store.upsert_news(
                item,
                event_id=event_id,
                event_topic=event_topic,
                event_type=event_type,
                fetched_at_utc=fetched_at,
                timezone_name=tracker_config.timezone,
            ):
                inserted_news += 1
        logger.info(
            "金十历史回补完成: date=%s pages=%s raw=%s filtered=%s inserted=%s duplicate_merged=%s",
            day_label,
            stats.pages_fetched,
            len(raw_items),
            processed_news,
            inserted_news,
            duplicate_count,
        )
        logger.info("金十历史原始新闻入库完成: date=%s raw_items=%s upsert_changes=%s", day_label, len(raw_items), raw_news_rows)
    except Jin10FetchError as exc:
        stats.api_status = "failed"
        stats.failures.append(str(exc))
        logger.exception("金十历史新闻回补失败，仍将尝试Brent价格和本地日报生成: %s", exc)

    try:
        price_start, price_end = rolling_price_bounds(day_start, day_end, tracker_config.timezone)
        brent_result = fetch_brent_5m_range(brent_config, price_start, price_end)
        brent_rows = store.upsert_klines(
            brent_config.symbol,
            brent_config.interval,
            brent_result.klines,
            brent_result.fetched_at_utc,
        )
        if brent_result.note:
            logger.warning(brent_result.note)
        logger.info(
            "Brent历史5分钟K线更新完成: date=%s fetched=%s upsert_changes=%s",
            day_label,
            len(brent_result.klines),
            brent_rows,
        )
    except BrentFetchError as exc:
        logger.exception("Brent历史价格数据抓取失败，新闻保存不受影响: %s", exc)

    reaction_counts = analyze_reactions_for_range(
        store,
        brent_config,
        tracker_config,
        day_start,
        day_end,
        logger=logger,
    )
    grouped = store.refresh_multi_event_groups(day_start, day_end)
    log_window_validation(store, brent_config, tracker_config, logger, day_start, day_end)
    focus_rows = store.report_focus_rows(day_start, day_end)
    focus_count = len({str(row["multi_event_group_id"] or row["news_id"]) for row in focus_rows})
    logger.info(
        "Brent历史反应分析完成: analyzed=%s pending=%s price_matched=%s price_missing=%s "
        "price_5m_missing=%s price_15m_missing=%s price_30m_missing=%s price_60m_missing=%s "
        "effective_oil_news=%s summary_without_event=%s major_topic_hits=%s threshold_hits=%s "
        "final_daily=%s final_output_focus=%s excluded=%s multi_event_groups=%s",
        reaction_counts["analyzed"],
        reaction_counts["pending"],
        reaction_counts["price_matched"],
        reaction_counts["price_missing"],
        reaction_counts["price_5m_missing"],
        reaction_counts["price_15m_missing"],
        reaction_counts["price_30m_missing"],
        reaction_counts["price_60m_missing"],
        reaction_counts["effective_oil_news"],
        reaction_counts["summary_without_event"],
        reaction_counts["major_topic_hits"],
        reaction_counts["threshold_hits"],
        reaction_counts["daily"],
        focus_count,
        reaction_counts["excluded"],
        grouped,
    )

    if generate_report:
        output_dir = Path(tracker_config.output_dir)
        external_update = fetch_latest_isw_update(
            day_end,
            cache_dir=Path(tracker_config.database_path).parent / "external_updates",
            timezone_name=tracker_config.timezone,
            retry_failed_cache=retry_failed_isw,
        )
        logger.info(
            "历史窗口外部update状态: status=%s title=%s et=%s bj=%s reason=%s",
            external_update.get("status"),
            external_update.get("title"),
            external_update.get("published_at_et"),
            external_update.get("published_at_bj"),
            external_update.get("failure_reason") or "N/A",
        )
        trump_update = _fetch_trump_update(
            day_start,
            day_end,
            cache_dir=Path(tracker_config.database_path).parent / "external_updates",
            timezone_name=tracker_config.timezone,
            retry_failed_cache=retry_failed_trump,
        )
        _log_trump_update(logger, trump_update, context="历史窗口")
        report_result = build_markdown_report(
            store,
            brent_config,
            output_dir,
            timezone_name=tracker_config.timezone,
            day=day,
            external_update=external_update,
            trump_update=trump_update,
        )
        markdown_path = report_result.markdown_path
        html_path = build_html_report(markdown_path, output_dir, presentation=report_result.presentation)
        audit_markdown_path = audit_markdown_path_for(markdown_path)
        audit_html_path = audit_html_path_for(markdown_path)
        logger.info("历史HTML/Markdown日报已生成: %s / %s", html_path, markdown_path)
        logger.info("历史HTML/Markdown审计报告已生成: %s / %s", audit_html_path, audit_markdown_path)

    return {
        "date": day_label,
        "pages_fetched": stats.pages_fetched,
        "inserted_news": inserted_news,
        "raw_news_rows": raw_news_rows,
        "processed_news": processed_news,
        "brent_rows": brent_rows,
        "reaction_counts": reaction_counts,
        "external_update_status": "成功" if external_update.get("status") == "success" else "失败",
        "external_update": external_update,
        "trump_update_status": "成功" if trump_update.get("status") == "success" else "失败",
        "trump_update": trump_update,
        "markdown_path": str(markdown_path) if markdown_path else None,
        "html_path": str(html_path) if html_path else None,
        "audit_markdown_path": str(audit_markdown_path_for(markdown_path)) if markdown_path else None,
        "audit_html_path": str(audit_html_path_for(markdown_path)) if markdown_path else None,
        "db_counts": store.counts(),
        "failures": stats.failures,
    }
