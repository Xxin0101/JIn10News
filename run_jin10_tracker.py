#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jin10_market_report.brent import BrentConfig
from jin10_market_report.config import FetchConfig, FilterConfig, TrackerConfig
from jin10_market_report.logging_utils import setup_logger
from jin10_market_report.storage import TrackerStore
from jin10_market_report.tracker import run_backfill_day, run_once


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="金十新闻增量监控 + Brent 5分钟反应分析工具（规则化，无AI调用）。"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, max_pages_default: int = 120, pause_default: float = 0.5) -> None:
        p.add_argument("--db-path", default="data/jin10_tracker.sqlite", help="SQLite数据库路径。")
        p.add_argument("--output-dir", default="outputs/tracker", help="HTML/Markdown日报输出目录。")
        p.add_argument("--log-dir", default="logs", help="运行日志目录。")
        p.add_argument("--timezone", default="Asia/Shanghai", help="日报展示时区，默认北京时间。")
        p.add_argument("--lookback-hours", type=int, default=24, help="首次运行回看小时数。")
        p.add_argument("--max-pages", type=int, default=max_pages_default, help="金十最多分页请求次数。")
        p.add_argument("--pause", type=float, default=pause_default, help="金十分页请求间隔秒数。")
        p.add_argument("--min-score", type=int, default=4, help="沿用现有相关新闻筛选最低分。")
        p.add_argument("--brent-symbol", default="BZ=F", help="Brent价格代码，默认Yahoo Finance BZ=F。")
        p.add_argument("--no-report", action="store_true", help="只更新数据库，不生成HTML/Markdown日报。")

    run_p = sub.add_parser("run-once", help="手动执行一次增量抓取、价格更新、反应分析和日报生成。")
    add_common(run_p)

    auto_p = sub.add_parser("auto", help="自动模式：每5分钟运行一次。")
    add_common(auto_p)
    auto_p.add_argument("--interval-seconds", type=int, default=300, help="自动运行间隔秒数，默认300。")

    report_p = sub.add_parser("report", help="仅基于数据库重新生成HTML/Markdown日报。")
    add_common(report_p)
    report_p.add_argument("--date", help="指定08:00滚动日报截止日期，格式YYYY-MM-DD；例如2026-07-02覆盖7月1日08:00至7月2日08:00。")
    report_p.add_argument(
        "--retry-failed-isw",
        action="store_true",
        help="忽略该截止时间已有的ISW失败缓存并重新抓取；成功缓存仍复用。",
    )
    report_p.add_argument(
        "--retry-failed-trump",
        action="store_true",
        help="忽略该截止时间已有的特朗普专栏失败缓存并重新抓取；成功缓存仍复用。",
    )

    backfill_p = sub.add_parser("backfill-day", help="回补指定日期的金十新闻和Brent 5分钟反应日报。")
    add_common(backfill_p, max_pages_default=1800, pause_default=0.05)
    backfill_p.add_argument("--date", required=True, help="回补08:00滚动日报截止日期，格式YYYY-MM-DD，例如2026-07-02覆盖前一日08:00至当日08:00。")
    backfill_p.add_argument(
        "--retry-failed-isw",
        action="store_true",
        help="忽略该截止时间已有的ISW失败缓存并重新抓取；成功缓存仍复用。",
    )
    backfill_p.add_argument(
        "--retry-failed-trump",
        action="store_true",
        help="忽略该截止时间已有的特朗普专栏失败缓存并重新抓取；成功缓存仍复用。",
    )

    forecast_p = sub.add_parser("forecast-report", help="生成指定交易日期区间日报并预测下一交易日Brent方向。")
    add_common(forecast_p, max_pages_default=1800, pause_default=0.05)
    forecast_p.add_argument("--start-date", required=True, help="分析开始交易日，格式YYYY-MM-DD。")
    forecast_p.add_argument("--end-date", required=True, help="分析结束交易日，格式YYYY-MM-DD。")
    forecast_p.add_argument("--target-date", required=True, help="预测目标交易日，格式YYYY-MM-DD。")

    same_day_p = sub.add_parser(
        "same-day-forecast-report",
        help="复用周五量化快照，结合截止目标日08:00的新闻生成同日方向预测。",
    )
    add_common(same_day_p, max_pages_default=1800, pause_default=0.05)
    same_day_p.add_argument("--start-date", required=True, help="新闻及报告窗口开始日期，格式YYYY-MM-DD。")
    same_day_p.add_argument("--end-date", required=True, help="窗口结束日期；必须等于目标日期。")
    same_day_p.add_argument("--target-date", required=True, help="同日预测目标日期，格式YYYY-MM-DD。")
    same_day_p.add_argument(
        "--base-snapshot-path",
        required=True,
        help="目标周一前周五的已验证预测快照；只复用其Model A结果。",
    )
    same_day_p.add_argument(
        "--retry-failed-isw",
        action="store_true",
        help="忽略截止时间已有的ISW失败缓存并重新抓取。",
    )
    same_day_p.add_argument(
        "--retry-failed-trump",
        action="store_true",
        help="忽略截止时间已有的特朗普专栏失败缓存并重新抓取。",
    )

    prepare_forecast_p = sub.add_parser(
        "prepare-forecast-snapshot",
        help="仅从已保存计算结果整理严格截止周五的预测快照；不抓取任何数据。",
    )
    prepare_forecast_p.add_argument("--source-path", required=True, help="已保存预测计算JSON路径。")
    prepare_forecast_p.add_argument("--output-dir", default="outputs/tracker", help="周五快照输出目录。")
    prepare_forecast_p.add_argument("--log-dir", default="logs", help="运行日志目录。")
    prepare_forecast_p.add_argument("--timezone", default="Asia/Shanghai", help="快照时区。")
    prepare_forecast_p.add_argument("--start-date", required=True, help="分析开始交易日，格式YYYY-MM-DD。")
    prepare_forecast_p.add_argument("--end-date", required=True, help="分析结束交易日，格式YYYY-MM-DD。")
    prepare_forecast_p.add_argument("--target-date", required=True, help="预测目标周一，格式YYYY-MM-DD。")

    init_p = sub.add_parser("init-db", help="初始化SQLite数据库。")
    init_p.add_argument("--db-path", default="data/jin10_tracker.sqlite", help="SQLite数据库路径。")
    return parser.parse_args()


def build_configs(args: argparse.Namespace) -> tuple[FetchConfig, FilterConfig, BrentConfig, TrackerConfig]:
    tracker_config = TrackerConfig(
        database_path=args.db_path,
        output_dir=args.output_dir,
        log_dir=args.log_dir,
        timezone=args.timezone,
        poll_interval_seconds=getattr(args, "interval_seconds", 300),
        first_run_lookback_hours=args.lookback_hours,
    )
    fetch_config = FetchConfig(
        hours=args.lookback_hours,
        timezone=args.timezone,
        max_pages=args.max_pages,
        pause_seconds=args.pause,
    )
    filter_config = FilterConfig(minimum_score=args.min_score)
    brent_config = BrentConfig(symbol=args.brent_symbol)
    return fetch_config, filter_config, brent_config, tracker_config


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    return str(value)


def _failed_trump_update(day_start: datetime, day_end: datetime, reason: Exception) -> dict[str, object]:
    return {
        "status": "failed",
        "source": "金十数据特朗普专栏",
        "window_start_utc": day_start.isoformat(),
        "window_end_utc": day_end.isoformat(),
        "statement_count": 0,
        "relevant_source_item_count": 0,
        "statements": [],
        "failure_reason": str(reason),
        "cache_path": None,
    }


def main() -> int:
    args = parse_args()
    if args.command == "init-db":
        store = TrackerStore(args.db_path)
        try:
            print(f"数据库已初始化: {args.db_path}")
            print(json.dumps(store.counts(), ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0

    if args.command == "prepare-forecast-snapshot":
        from jin10_market_report.forecast_pipeline import prepare_friday_snapshot

        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        logger, log_path = setup_logger(Path(args.log_dir), f"tracker_{run_id}")
        start_day = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end_day = datetime.strptime(args.end_date, "%Y-%m-%d").date()
        target_day = datetime.strptime(args.target_date, "%Y-%m-%d").date()
        output_stem = (
            f"Jin10_Brent_Daily_Forecast_{start_day.strftime('%Y%m%d')}_"
            f"{end_day.strftime('%Y%m%d')}_for_{target_day.strftime('%Y%m%d')}"
        )
        destination = Path(args.output_dir) / f"{output_stem}_friday_snapshot.json"
        result = prepare_friday_snapshot(
            Path(args.source_path),
            destination,
            start_date=args.start_date,
            end_date=args.end_date,
            target_date=args.target_date,
            timezone_name=args.timezone,
        )
        logger.info("周五预测快照整理完成: log=%s result=%s", log_path, json.dumps(result, ensure_ascii=False))
        print(f"周五预测快照已生成: {result['snapshot_path']}")
        return 0

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger, log_path = setup_logger(Path(args.log_dir), f"tracker_{run_id}")
    fetch_config, filter_config, brent_config, tracker_config = build_configs(args)
    store = TrackerStore(tracker_config.database_path)
    logger.info("金十-Brent追踪工具启动: command=%s log=%s", args.command, log_path)
    logger.info("注意: 每5分钟任务仅执行规则化抓取、入库和公式计算，不调用任何AI接口。")

    try:
        if args.command == "forecast-report":
            from jin10_market_report.forecast_pipeline import run_forecast_report

            result = run_forecast_report(
                store,
                fetch_config,
                filter_config,
                brent_config,
                tracker_config,
                logger,
                start_date=args.start_date,
                end_date=args.end_date,
                target_date=args.target_date,
            )
            logger.info("预测日报运行结果: %s", json.dumps(_json_safe(result), ensure_ascii=False))
            print(f"预测日报已生成: {result['html_path']}")
            print(f"周五预测快照: {result['forecast_snapshot_path']}")
            print(f"reused={'true' if result['reused'] else 'false'}")
            return 0

        if args.command == "same-day-forecast-report":
            from jin10_market_report.forecast_pipeline import run_same_day_forecast_report

            result = run_same_day_forecast_report(
                store,
                brent_config,
                tracker_config,
                logger,
                start_date=args.start_date,
                end_date=args.end_date,
                target_date=args.target_date,
                base_snapshot_path=Path(args.base_snapshot_path),
                retry_failed_isw=args.retry_failed_isw,
                retry_failed_trump=args.retry_failed_trump,
            )
            logger.info("同日预测日报运行结果: %s", json.dumps(_json_safe(result), ensure_ascii=False))
            print(f"同日预测日报已生成: {result['html_path']}")
            print(f"预测结果清单: {result['forecast_result_json_path']}")
            return 0

        if args.command == "report":
            from jin10_market_report.external_update import fetch_latest_isw_update
            from jin10_market_report.trump_update import fetch_trump_statements
            from jin10_market_report.reaction import analyze_reactions_for_range
            from jin10_market_report.tracker import log_window_validation
            from jin10_market_report.tracking_report import (
                _build_price_movement_review,
                audit_html_path_for,
                audit_markdown_path_for,
                build_html_report,
                build_markdown_report,
            )
            from jin10_market_report.tracking_report import beijing_day_bounds

            day = (
                datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tracker_config.timezone))
                if args.date
                else None
            )
            day_start, day_end, _ = beijing_day_bounds(day, timezone_name=tracker_config.timezone)
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
                "本地日报重算完成: analyzed=%s pending=%s price_matched=%s price_missing=%s "
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
            movement_review = _build_price_movement_review(
                store,
                brent_config,
                day_start,
                day_end,
                tracker_config.timezone,
            )
            logger.info(
                "价格异动复盘统计: thirty_min_windows=%s movement_groups=%s unexplained_groups=%s "
                "selected_raw_news=%s core_drivers=%s",
                len(movement_review.get("windows") or []),
                len(movement_review.get("groups") or []),
                len(movement_review.get("unexplained") or []),
                len(movement_review.get("selected_raw_ids") or set()),
                len(movement_review.get("core_raw_ids") or set()),
            )
            external_update = fetch_latest_isw_update(
                day_end,
                cache_dir=Path(tracker_config.database_path).parent / "external_updates",
                timezone_name=tracker_config.timezone,
                retry_failed_cache=args.retry_failed_isw,
            )
            logger.info(
                "外部网站update状态: status=%s method=%s title=%s et=%s bj=%s reason=%s",
                external_update.get("status"),
                external_update.get("fetch_method"),
                external_update.get("title"),
                external_update.get("published_at_et"),
                external_update.get("published_at_bj"),
                external_update.get("failure_reason") or "N/A",
            )
            try:
                trump_update = fetch_trump_statements(
                    day_start,
                    day_end,
                    cache_dir=Path(tracker_config.database_path).parent / "external_updates",
                    timezone_name=tracker_config.timezone,
                    retry_failed_cache=args.retry_failed_trump,
                )
            except Exception as exc:
                trump_update = _failed_trump_update(day_start, day_end, exc)
            if trump_update.get("status") == "success":
                logger.info(
                    "特朗普专栏抓取完成: status=%s statements=%s relevant_sources=%s cache=%s",
                    trump_update.get("status"),
                    trump_update.get("statement_count"),
                    trump_update.get("relevant_source_item_count"),
                    trump_update.get("cache_path"),
                )
            else:
                logger.warning(
                    "特朗普专栏抓取失败，不影响金十+Brent主日报: status=%s statements=%s relevant_sources=%s reason=%s cache=%s",
                    trump_update.get("status"),
                    trump_update.get("statement_count"),
                    trump_update.get("relevant_source_item_count"),
                    trump_update.get("failure_reason") or "N/A",
                    trump_update.get("cache_path"),
                )
            report_result = build_markdown_report(
                store,
                brent_config,
                Path(tracker_config.output_dir),
                timezone_name=tracker_config.timezone,
                day=day,
                external_update=external_update,
                trump_update=trump_update,
            )
            markdown = report_result.markdown_path
            html = build_html_report(
                markdown,
                Path(tracker_config.output_dir),
                presentation=report_result.presentation,
            )
            audit_markdown = audit_markdown_path_for(markdown)
            audit_html = audit_html_path_for(markdown)
            logger.info("日报已重新生成: %s / %s", html, markdown)
            logger.info("审计报告已重新生成: %s / %s", audit_html, audit_markdown)
            return 0

        if args.command == "backfill-day":
            result = run_backfill_day(
                store,
                fetch_config,
                filter_config,
                brent_config,
                tracker_config,
                logger,
                args.date,
                generate_report=not args.no_report,
                retry_failed_isw=args.retry_failed_isw,
                retry_failed_trump=args.retry_failed_trump,
            )
            logger.info("历史回补运行结果: %s", json.dumps(_json_safe(result), ensure_ascii=False))
            return 0

        if args.command == "run-once":
            result = run_once(
                store,
                fetch_config,
                filter_config,
                brent_config,
                tracker_config,
                logger,
                generate_report=not args.no_report,
                log_path=log_path,
            )
            logger.info("运行结果: %s", json.dumps(_json_safe(result), ensure_ascii=False))
            return 0

        if args.command == "auto":
            logger.info("自动模式启动，每 %s 秒运行一次。按 Ctrl+C 停止。", args.interval_seconds)
            while True:
                started = time.time()
                try:
                    result = run_once(
                        store,
                        fetch_config,
                        filter_config,
                        brent_config,
                        tracker_config,
                        logger,
                        generate_report=not args.no_report,
                    )
                    logger.info("本轮运行结果: %s", json.dumps(_json_safe(result), ensure_ascii=False))
                except Exception:
                    logger.exception("本轮自动任务失败，将等待下一轮。")
                elapsed = time.time() - started
                time.sleep(max(1, args.interval_seconds - elapsed))
    except KeyboardInterrupt:
        logger.info("收到停止信号，自动模式退出。")
        return 0
    finally:
        store.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
