"""Render the native Brent dashboard from verified news and optional observations."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

from .brent import BrentFetchError, fetch_brent_5m_range
from .config import BrentConfig, TrackerConfig
from .reaction import analyze_reactions_for_range
from .storage import TrackerStore
from .time_utils import iso_utc
from .tracking_report import build_html_report, build_markdown_report
from .trump_update import fetch_trump_statements

TIMEZONE = "Asia/Shanghai"
BACKGROUND_NOT_CHECKED = {
    "status": "failed",
    "failure_reason": "本次新闻日报未重新核对该独立背景源。",
}


def _news_sections(markdown: str) -> tuple[str, tuple[str, ...], str, str]:
    blocks = re.split(r"(?m)(?=^## )", markdown.strip())
    status_match = re.search(r"新闻覆盖状态：[ \t]*(COMPLETE|PARTIAL)\b", blocks[0])
    if not status_match:
        raise ValueError("已核验的新闻 Markdown 必须包含 COMPLETE 或 PARTIAL 覆盖状态。")

    lead: list[str] = []
    editorial: list[str] = []
    watch = ""
    for block in blocks[1:]:
        heading, _, body = block.partition("\n")
        if heading == "## 最重要进展":
            for item in re.split(r"(?m)(?=^### )", body.strip()):
                title, _, detail = item.partition("\n")
                paragraphs = [p.strip().replace("\n", " ") for p in detail.split("\n\n") if p.strip()]
                if title.startswith("### "):
                    lead.append(f"**{title[4:]}**。{' '.join(paragraphs)}")
        elif heading == "## 下一时段待确认":
            watch = "## 今日关注什么\n" + body.strip()
        elif heading != "## 分时段快讯覆盖":
            editorial.append(re.sub(r"(?m)^(#{2,3}) ", r"\1# ", block.strip()))
    if not lead and not editorial:
        raise ValueError("已核验的新闻 Markdown 必须包含新闻内容。")
    return status_match[1], tuple(lead), "\n\n".join(editorial), watch


def render_news_dashboard(
    day: date,
    database: Path,
    news_markdown: Path,
    output_dir: Path,
    *,
    refresh_sources: bool = False,
) -> Path:
    status, lead, editorial, watch = _news_sections(news_markdown.read_text(encoding="utf-8"))
    tz = ZoneInfo(TIMEZONE)
    end = datetime.combine(day, time(8), tz)
    start = end - timedelta(days=3 if day.weekday() == 0 else 1)
    brent = BrentConfig()
    store = TrackerStore(":memory:")
    try:
        source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            source.backup(store.conn)
        finally:
            source.close()
        trump = BACKGROUND_NOT_CHECKED
        if refresh_sources:
            try:
                result = fetch_brent_5m_range(brent, start - timedelta(minutes=30), end)
                store.upsert_klines(brent.symbol, brent.interval, result.klines, result.fetched_at_utc)
            except BrentFetchError as exc:
                print(f"Brent 行情获取失败，使用已核验的缓存：{exc}", file=sys.stderr)
            trump = fetch_trump_statements(start, end, cache_dir=database.parent / "external_updates")

        # Historical news reactions displayed in this edition must stop at the report cutoff.
        store.conn.execute(
            "DELETE FROM brent_klines WHERE symbol = ? AND interval = ? AND ts_utc >= ?",
            (brent.symbol, brent.interval, iso_utc(end)),
        )
        store.conn.commit()
        analyze_reactions_for_range(store, brent, TrackerConfig(timezone=TIMEZONE), start, end)
        built = build_markdown_report(
            store, brent, output_dir, timezone_name=TIMEZONE,
            window_start_utc=start, window_end_utc=end,
            output_stem=f"Jin10_Brent_Rolling_Reaction_{day:%Y%m%d}_0800",
            external_update=BACKGROUND_NOT_CHECKED, trump_update=trump, news_only=True,
        )
    finally:
        store.close()

    path = built.markdown_path
    source = path.read_text(encoding="utf-8")
    source = source.replace(
        "\n## 今日重点（Executive Summary）",
        "\n新闻覆盖状态：" + status + "。行情覆盖单独核验。\n\n## 今日重点（Executive Summary）",
        1,
    )
    if lead:
        source = re.sub(
            r"(?ms)(^## 今日重点（Executive Summary）\n\n).*?(?=^统计窗口：)",
            lambda match: match[1] + "\n".join(f"- {item}" for item in lead) + "\n\n",
            source,
            count=1,
        )
    if watch:
        source = re.sub(r"(?ms)^## (?:接下来关注什么|今日关注什么)\n.*?(?=^## |^\[查看完整 Audit|\Z)", "", source)
    insertions = "\n\n".join(x for x in (f"## 新闻专题\n\n{editorial}" if editorial else "", watch) if x)
    if insertions:
        source = source.replace("[查看完整 Audit →]", insertions + "\n\n[查看完整 Audit →]", 1)
    path.write_text(source, encoding="utf-8")
    presentation = replace(built.presentation, top_summary_items=lead or built.presentation.top_summary_items)
    return build_html_report(path, output_dir, presentation)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--news-markdown", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--refresh-sources", action="store_true")
    args = parser.parse_args()
    print(render_news_dashboard(
        args.date, args.db_path, args.news_markdown, args.output_dir, refresh_sources=args.refresh_sources
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
