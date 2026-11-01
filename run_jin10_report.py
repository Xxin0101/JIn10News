#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jin10_market_report.config import FetchConfig, FilterConfig
from jin10_market_report.fetcher import Jin10FetchError, fetch_recent_flashes
from jin10_market_report.logging_utils import setup_logger, write_summary_json
from jin10_market_report.models import RunStats
from jin10_market_report.processor import process_flashes
from jin10_market_report.report import build_docx_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取金十数据过去24小时内伊朗/中东/能源/贸易/航运相关快讯并生成Word日报。"
    )
    parser.add_argument("--hours", type=int, default=24, help="抓取回看小时数，默认24。")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="金十发布时间解析时区，默认Asia/Shanghai。")
    parser.add_argument("--output-dir", default="outputs", help="Word日报输出目录。")
    parser.add_argument("--log-dir", default="logs", help="运行日志输出目录。")
    parser.add_argument("--max-pages", type=int, default=120, help="最多分页请求次数。")
    parser.add_argument("--channel", type=int, default=-8200, help="金十快讯通道，默认使用官网市场快讯通道。")
    parser.add_argument("--min-score", type=int, default=4, help="关键词相关性最低分。")
    parser.add_argument("--pause", type=float, default=0.5, help="分页请求间隔秒数，默认0.5。")
    parser.add_argument("--dry-run", action="store_true", help="只抓取和筛选，不生成Word。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tz = ZoneInfo(args.timezone)
    run_id = datetime.now(tz).strftime("%Y%m%d_%H%M%S")
    logger, log_path = setup_logger(Path(args.log_dir), run_id)
    stats = RunStats(fetch_started_at=datetime.now(tz))
    output_docx: Path | None = None

    logger.info("开始运行金十数据中东能源贸易日报工具")
    logger.info("日志文件: %s", log_path)
    logger.info("抓取范围: 过去 %s 小时; 时区: %s", args.hours, args.timezone)

    try:
        fetch_config = FetchConfig(
            hours=args.hours,
            timezone=args.timezone,
            channel=args.channel,
            max_pages=args.max_pages,
            pause_seconds=args.pause,
        )
        filter_config = FilterConfig(minimum_score=args.min_score)
        raw_items = fetch_recent_flashes(fetch_config, stats)
        logger.info("公开实时资讯接口可获取，抓取页数: %s", stats.pages_fetched)
        logger.info("过去%s小时内抓取条数: %s", args.hours, len(raw_items))

        processed, duplicate_count = process_flashes(raw_items, filter_config)
        stats.duplicate_merged_total = duplicate_count
        stats.filtered_total = len(processed)
        stats.high_priority_total = sum(1 for item in processed if item.high_priority)
        logger.info("筛选后条数: %s", stats.filtered_total)
        logger.info("重复合并数量: %s", stats.duplicate_merged_total)
        logger.info("High Priority数量: %s", stats.high_priority_total)

        if not args.dry_run:
            today = datetime.now(tz).strftime("%Y%m%d")
            output_docx = Path(args.output_dir) / f"Jin10_Iran_MiddleEast_Market_News_{today}.docx"
            build_docx_report(processed, stats, output_docx, timezone=args.timezone)
            logger.info("Word日报已生成: %s", output_docx)
        else:
            logger.info("dry-run模式，不生成Word日报")
        return_code = 0
    except Jin10FetchError as exc:
        stats.api_status = "failed"
        stats.failures.append(str(exc))
        logger.exception("抓取失败: %s", exc)
        return_code = 2
    except Exception as exc:
        stats.failures.append(f"未预期错误: {exc}")
        logger.exception("运行失败")
        return_code = 1
    finally:
        stats.fetch_finished_at = datetime.now(tz)
        summary_path = write_summary_json(
            Path(args.log_dir),
            run_id,
            stats,
            output_docx,
            extra={"dry_run": args.dry_run},
        )
        logger.info("运行摘要JSON: %s", summary_path)
        if stats.failures:
            logger.info("失败/警告记录: %s", " | ".join(stats.failures))
        logger.info(
            "完成: 抓取总条数=%s, 筛选后条数=%s, 重复合并=%s",
            stats.within_window_total,
            stats.filtered_total,
            stats.duplicate_merged_total,
        )

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
