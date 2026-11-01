from __future__ import annotations

import json
import sqlite3
import difflib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .brent import BrentKline
from .eventing import direct_energy_supply_reason
from .models import ProcessedNews, RawFlash
from .time_utils import ensure_utc, iso_utc, parse_iso_utc, to_beijing, utc_now


SCHEMA_VERSION = "4"


SUMMARY_MARKERS = ("每日", "汇总", "要闻速递", "ETF", "晨报", "晚报", "市场回顾", "盘点")


def _is_summary_raw(title: str, content: str) -> bool:
    text = f"{title} {content}"
    return any(marker in text for marker in SUMMARY_MARKERS)


class TrackerStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS news (
                news_id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT '金十数据',
                url TEXT NOT NULL,
                raw_time TEXT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                published_at_utc TEXT NOT NULL,
                published_at_bj TEXT NOT NULL,
                fetched_at_utc TEXT NOT NULL,
                keywords_json TEXT NOT NULL,
                categories_json TEXT NOT NULL,
                is_duplicate INTEGER NOT NULL DEFAULT 0,
                duplicate_of TEXT,
                reaction_status TEXT NOT NULL DEFAULT 'Pending Brent Reaction Analysis',
                price_status TEXT NOT NULL DEFAULT 'pending',
                is_in_daily INTEGER NOT NULL DEFAULT 0,
                is_event_update INTEGER NOT NULL DEFAULT 0,
                event_type TEXT NOT NULL DEFAULT '首次事件',
                event_id TEXT NOT NULL,
                event_topic TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_news_published_at ON news(published_at_utc);
            CREATE INDEX IF NOT EXISTS idx_news_reaction_status ON news(reaction_status);
            CREATE INDEX IF NOT EXISTS idx_news_event_id ON news(event_id);
            CREATE INDEX IF NOT EXISTS idx_news_daily ON news(is_in_daily, published_at_utc);

            CREATE TABLE IF NOT EXISTS raw_news (
                raw_id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT '金十数据',
                source_channel TEXT NOT NULL,
                source_url TEXT NOT NULL,
                url TEXT NOT NULL,
                raw_publish_time TEXT,
                publish_time_beijing TEXT NOT NULL,
                published_at_utc TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                fetch_batch_id TEXT NOT NULL,
                fetch_page INTEGER NOT NULL DEFAULT 0,
                is_summary INTEGER NOT NULL DEFAULT 0,
                raw_type INTEGER NOT NULL DEFAULT 0,
                important INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}',
                fetched_at_utc TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_raw_news_published_at ON raw_news(published_at_utc);
            CREATE INDEX IF NOT EXISTS idx_raw_news_channel ON raw_news(source_channel, published_at_utc);
            CREATE INDEX IF NOT EXISTS idx_raw_news_summary ON raw_news(is_summary, published_at_utc);

            CREATE TABLE IF NOT EXISTS brent_klines (
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                ts_utc TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL,
                source TEXT NOT NULL,
                fetched_at_utc TEXT NOT NULL,
                PRIMARY KEY (symbol, interval, ts_utc)
            );

            CREATE INDEX IF NOT EXISTS idx_brent_ts ON brent_klines(ts_utc);

            CREATE TABLE IF NOT EXISTS brent_reactions (
                news_id TEXT PRIMARY KEY,
                baseline_time_utc TEXT,
                baseline_close REAL,
                window_start_utc TEXT,
                window_end_utc TEXT,
                window_high REAL,
                window_low REAL,
                final_close REAL,
                max_up_pct REAL,
                max_down_pct REAL,
                final_pct REAL,
                reaction_label TEXT NOT NULL,
                market_interpretation TEXT NOT NULL,
                analyzed_at_utc TEXT NOT NULL,
                price_status TEXT NOT NULL,
                multi_event_group_id TEXT,
                multi_event_group_reason TEXT,
                event_base_time_utc TEXT,
                event_base_close REAL,
                price_time_5m_utc TEXT,
                price_5m REAL,
                pct_5m REAL,
                price_time_15m_utc TEXT,
                price_15m REAL,
                pct_15m REAL,
                price_time_30m_utc TEXT,
                price_30m REAL,
                pct_30m REAL,
                price_time_60m_utc TEXT,
                price_60m REAL,
                pct_60m REAL,
                pre30_time_utc TEXT,
                pre30_close REAL,
                pre30_to_post30_pct REAL,
                price_reaction_level TEXT,
                trigger_window TEXT,
                importance_reason TEXT,
                oil_relevance_reason TEXT,
                market_bias TEXT,
                market_pricing_status TEXT,
                price_missing_reason TEXT,
                exclusion_reason TEXT,
                note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(news_id) REFERENCES news(news_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_reactions_label ON brent_reactions(reaction_label);
            CREATE INDEX IF NOT EXISTS idx_reactions_group ON brent_reactions(multi_event_group_id);
            """
        )
        self._migrate_schema()
        self.set_meta("schema_version", SCHEMA_VERSION)
        self.conn.commit()

    def _existing_columns(self, table: str) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        if column not in self._existing_columns(table):
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_schema(self) -> None:
        self._ensure_column("news", "raw_time", "TEXT")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_news (
                raw_id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT '金十数据',
                source_channel TEXT NOT NULL DEFAULT 'flash',
                source_url TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                raw_publish_time TEXT,
                publish_time_beijing TEXT NOT NULL DEFAULT '',
                published_at_utc TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                fetch_batch_id TEXT NOT NULL DEFAULT '',
                fetch_page INTEGER NOT NULL DEFAULT 0,
                is_summary INTEGER NOT NULL DEFAULT 0,
                raw_type INTEGER NOT NULL DEFAULT 0,
                important INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}',
                fetched_at_utc TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL DEFAULT '',
                updated_at_utc TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_raw_news_published_at ON raw_news(published_at_utc);
            CREATE INDEX IF NOT EXISTS idx_raw_news_channel ON raw_news(source_channel, published_at_utc);
            CREATE INDEX IF NOT EXISTS idx_raw_news_summary ON raw_news(is_summary, published_at_utc);
            """
        )
        for column, definition in {
            "multi_event_group_reason": "TEXT",
            "event_base_time_utc": "TEXT",
            "event_base_close": "REAL",
            "price_time_5m_utc": "TEXT",
            "price_5m": "REAL",
            "pct_5m": "REAL",
            "price_time_15m_utc": "TEXT",
            "price_15m": "REAL",
            "pct_15m": "REAL",
            "price_time_30m_utc": "TEXT",
            "price_30m": "REAL",
            "pct_30m": "REAL",
            "price_time_60m_utc": "TEXT",
            "price_60m": "REAL",
            "pct_60m": "REAL",
            "pre30_time_utc": "TEXT",
            "pre30_close": "REAL",
            "pre30_to_post30_pct": "REAL",
            "price_reaction_level": "TEXT",
            "trigger_window": "TEXT",
            "importance_reason": "TEXT",
            "oil_relevance_reason": "TEXT",
            "market_bias": "TEXT",
            "market_pricing_status": "TEXT",
            "price_missing_reason": "TEXT",
            "exclusion_reason": "TEXT",
        }.items():
            self._ensure_column("brent_reactions", column, definition)

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta_datetime(self, key: str) -> datetime | None:
        return parse_iso_utc(self.get_meta(key))

    def recent_news_ids(self, since_utc: datetime) -> set[str]:
        rows = self.conn.execute(
            "SELECT news_id FROM news WHERE published_at_utc >= ?",
            (iso_utc(since_utc),),
        ).fetchall()
        return {str(row["news_id"]) for row in rows}

    def title_or_url_duplicate(self, title: str, url: str, news_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT news_id FROM news WHERE news_id != ? AND (url = ? OR title = ?) "
            "ORDER BY published_at_utc DESC LIMIT 1",
            (news_id, url, title),
        ).fetchone()
        return str(row["news_id"]) if row else None

    def event_has_previous(self, event_id: str, published_at_utc: datetime) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM news WHERE event_id = ? AND published_at_utc < ? LIMIT 1",
            (event_id, iso_utc(published_at_utc)),
        ).fetchone()
        return bool(row)

    def upsert_news(
        self,
        item: ProcessedNews,
        event_id: str,
        event_topic: str,
        event_type: str,
        fetched_at_utc: datetime,
        timezone_name: str = "Asia/Shanghai",
    ) -> bool:
        published_utc = ensure_utc(item.published_at)
        now = utc_now()
        duplicate_of = self.title_or_url_duplicate(item.title_or_text, item.jin10_url, item.id)
        is_duplicate = bool(duplicate_of)
        is_event_update = event_type != "首次事件"
        already_exists = bool(
            self.conn.execute("SELECT 1 FROM news WHERE news_id = ? LIMIT 1", (item.id,)).fetchone()
        )

        cur = self.conn.execute(
            """
            INSERT INTO news (
                news_id, url, raw_time, title, content, published_at_utc, published_at_bj,
                fetched_at_utc, keywords_json, categories_json, is_duplicate,
                duplicate_of, is_event_update, event_type, event_id, event_topic,
                created_at_utc, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(news_id) DO UPDATE SET
                raw_time = excluded.raw_time,
                title = excluded.title,
                content = excluded.content,
                keywords_json = excluded.keywords_json,
                categories_json = excluded.categories_json,
                is_event_update = excluded.is_event_update,
                event_type = excluded.event_type,
                event_id = excluded.event_id,
                event_topic = excluded.event_topic,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                item.id,
                item.jin10_url,
                item.raw_time,
                item.title_or_text,
                item.original_text,
                iso_utc(published_utc),
                to_beijing(published_utc, timezone_name).isoformat(timespec="seconds"),
                iso_utc(fetched_at_utc),
                json.dumps(item.keywords, ensure_ascii=False),
                json.dumps(item.categories, ensure_ascii=False),
                1 if is_duplicate else 0,
                duplicate_of,
                1 if is_event_update else 0,
                event_type,
                event_id,
                event_topic,
                iso_utc(now),
                iso_utc(now),
            ),
        )
        self.conn.commit()
        return bool(cur.rowcount > 0 and not already_exists)

    def upsert_raw_news(
        self,
        items: Iterable[RawFlash],
        fetched_at_utc: datetime,
        timezone_name: str = "Asia/Shanghai",
        fetch_batch_id: str = "",
    ) -> int:
        now = utc_now()
        rows = []
        for item in items:
            published_utc = ensure_utc(item.published_at)
            title = item.title or item.content[:120]
            body = item.content or title
            rows.append(
                (
                    item.id,
                    "金十数据",
                    item.source_channel or "flash",
                    item.source_url or "",
                    item.jin10_url,
                    item.raw_time,
                    to_beijing(published_utc, timezone_name).isoformat(timespec="seconds"),
                    iso_utc(published_utc),
                    title,
                    body,
                    body,
                    fetch_batch_id or iso_utc(fetched_at_utc),
                    int(item.fetch_page or 0),
                    1 if _is_summary_raw(title, body) else 0,
                    int(item.type or 0),
                    1 if item.important else 0,
                    json.dumps(item.raw, ensure_ascii=False),
                    iso_utc(fetched_at_utc),
                    iso_utc(now),
                    iso_utc(now),
                )
            )
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT INTO raw_news (
                raw_id, source, source_channel, source_url, url, raw_publish_time,
                publish_time_beijing, published_at_utc, title, body, detail,
                fetch_batch_id, fetch_page, is_summary, raw_type, important,
                raw_json, fetched_at_utc, created_at_utc, updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raw_id) DO UPDATE SET
                source = excluded.source,
                source_channel = excluded.source_channel,
                source_url = excluded.source_url,
                url = excluded.url,
                raw_publish_time = excluded.raw_publish_time,
                publish_time_beijing = excluded.publish_time_beijing,
                published_at_utc = excluded.published_at_utc,
                title = excluded.title,
                body = excluded.body,
                detail = excluded.detail,
                fetch_batch_id = excluded.fetch_batch_id,
                fetch_page = excluded.fetch_page,
                is_summary = excluded.is_summary,
                raw_type = excluded.raw_type,
                important = excluded.important,
                raw_json = excluded.raw_json,
                fetched_at_utc = excluded.fetched_at_utc,
                updated_at_utc = excluded.updated_at_utc
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def upsert_klines(
        self,
        symbol: str,
        interval: str,
        klines: Iterable[BrentKline],
        fetched_at_utc: datetime,
    ) -> int:
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT INTO brent_klines (
                symbol, interval, ts_utc, open, high, low, close, volume, source, fetched_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, interval, ts_utc) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                source = excluded.source,
                fetched_at_utc = excluded.fetched_at_utc
            """,
            [
                (
                    symbol,
                    interval,
                    iso_utc(k.ts_utc),
                    k.open,
                    k.high,
                    k.low,
                    k.close,
                    k.volume,
                    k.source,
                    iso_utc(fetched_at_utc),
                )
                for k in klines
            ],
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def latest_kline_time(self, symbol: str, interval: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT ts_utc FROM brent_klines WHERE symbol = ? AND interval = ? ORDER BY ts_utc DESC LIMIT 1",
            (symbol, interval),
        ).fetchone()
        return parse_iso_utc(row["ts_utc"]) if row else None

    def pending_news_for_analysis(self, cutoff_utc: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM news
            WHERE reaction_status != 'analyzed'
              AND is_duplicate = 0
              AND published_at_utc <= ?
            ORDER BY published_at_utc ASC
            """,
            (iso_utc(cutoff_utc),),
        ).fetchall()

    def baseline_kline(self, symbol: str, interval: str, baseline_cutoff_utc: datetime) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM brent_klines
            WHERE symbol = ? AND interval = ? AND ts_utc <= ?
            ORDER BY ts_utc DESC LIMIT 1
            """,
            (symbol, interval, iso_utc(baseline_cutoff_utc)),
        ).fetchone()

    def window_klines(
        self,
        symbol: str,
        interval: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM brent_klines
            WHERE symbol = ? AND interval = ?
              AND ts_utc >= ? AND ts_utc < ?
            ORDER BY ts_utc ASC
            """,
            (symbol, interval, iso_utc(start_utc), iso_utc(end_utc)),
        ).fetchall()

    def kline_at_or_before(self, symbol: str, interval: str, target_utc: datetime) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM brent_klines
            WHERE symbol = ? AND interval = ? AND ts_utc <= ?
            ORDER BY ts_utc DESC LIMIT 1
            """,
            (symbol, interval, iso_utc(target_utc)),
        ).fetchone()

    def kline_at_or_after(self, symbol: str, interval: str, target_utc: datetime) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM brent_klines
            WHERE symbol = ? AND interval = ? AND ts_utc >= ?
            ORDER BY ts_utc ASC LIMIT 1
            """,
            (symbol, interval, iso_utc(target_utc)),
        ).fetchone()

    def nearest_kline(self, symbol: str, interval: str, target_utc: datetime) -> sqlite3.Row | None:
        before = self.kline_at_or_before(symbol, interval, target_utc)
        after = self.kline_at_or_after(symbol, interval, target_utc)
        if before is None:
            return after
        if after is None:
            return before
        before_ts = parse_iso_utc(before["ts_utc"])
        after_ts = parse_iso_utc(after["ts_utc"])
        if not before_ts:
            return after
        if not after_ts:
            return before
        target = ensure_utc(target_utc)
        return before if target - before_ts <= after_ts - target else after

    def news_for_reaction_range(self, start_utc: datetime, end_utc: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM news
            WHERE is_duplicate = 0
              AND published_at_utc >= ?
              AND published_at_utc < ?
            ORDER BY published_at_utc ASC
            """,
            (iso_utc(start_utc), iso_utc(end_utc)),
        ).fetchall()

    def raw_news_between(self, start_utc: datetime, end_utc: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM raw_news
            WHERE published_at_utc >= ?
              AND published_at_utc < ?
            ORDER BY published_at_utc ASC
            """,
            (iso_utc(start_utc), iso_utc(end_utc)),
        ).fetchall()

    def raw_news_near(self, center_utc: datetime, minutes: int = 45) -> list[sqlite3.Row]:
        start = ensure_utc(center_utc) - timedelta(minutes=minutes)
        end = ensure_utc(center_utc) + timedelta(minutes=minutes)
        return self.raw_news_between(start, end)

    def raw_news_bounds(self, start_utc: datetime, end_utc: datetime) -> tuple[datetime | None, datetime | None, int]:
        row = self.conn.execute(
            """
            SELECT MIN(published_at_utc) AS min_ts,
                   MAX(published_at_utc) AS max_ts,
                   COUNT(*) AS c
            FROM raw_news
            WHERE published_at_utc >= ?
              AND published_at_utc < ?
            """,
            (iso_utc(start_utc), iso_utc(end_utc)),
        ).fetchone()
        return (
            parse_iso_utc(row["min_ts"]) if row and row["min_ts"] else None,
            parse_iso_utc(row["max_ts"]) if row and row["max_ts"] else None,
            int(row["c"] or 0) if row else 0,
        )

    def update_event_metadata(self, news_id: str, event_id: str, event_topic: str) -> None:
        self.conn.execute(
            """
            UPDATE news
            SET event_id = ?, event_topic = ?, updated_at_utc = ?
            WHERE news_id = ?
            """,
            (event_id, event_topic, iso_utc(utc_now()), news_id),
        )
        self.conn.commit()

    def save_reaction(
        self,
        news_id: str,
        reaction: dict[str, Any],
        is_in_daily: bool,
        status: str = "analyzed",
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO brent_reactions (
                news_id, baseline_time_utc, baseline_close, window_start_utc,
                window_end_utc, window_high, window_low, final_close,
                max_up_pct, max_down_pct, final_pct, reaction_label,
                market_interpretation, analyzed_at_utc, price_status,
                multi_event_group_id, multi_event_group_reason,
                event_base_time_utc, event_base_close,
                price_time_5m_utc, price_5m, pct_5m,
                price_time_15m_utc, price_15m, pct_15m,
                price_time_30m_utc, price_30m, pct_30m,
                price_time_60m_utc, price_60m, pct_60m,
                pre30_time_utc, pre30_close, pre30_to_post30_pct,
                price_reaction_level, trigger_window, importance_reason,
                oil_relevance_reason, market_bias, market_pricing_status,
                price_missing_reason, exclusion_reason, note
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(news_id) DO UPDATE SET
                baseline_time_utc = excluded.baseline_time_utc,
                baseline_close = excluded.baseline_close,
                window_start_utc = excluded.window_start_utc,
                window_end_utc = excluded.window_end_utc,
                window_high = excluded.window_high,
                window_low = excluded.window_low,
                final_close = excluded.final_close,
                max_up_pct = excluded.max_up_pct,
                max_down_pct = excluded.max_down_pct,
                final_pct = excluded.final_pct,
                reaction_label = excluded.reaction_label,
                market_interpretation = excluded.market_interpretation,
                analyzed_at_utc = excluded.analyzed_at_utc,
                price_status = excluded.price_status,
                multi_event_group_reason = excluded.multi_event_group_reason,
                event_base_time_utc = excluded.event_base_time_utc,
                event_base_close = excluded.event_base_close,
                price_time_5m_utc = excluded.price_time_5m_utc,
                price_5m = excluded.price_5m,
                pct_5m = excluded.pct_5m,
                price_time_15m_utc = excluded.price_time_15m_utc,
                price_15m = excluded.price_15m,
                pct_15m = excluded.pct_15m,
                price_time_30m_utc = excluded.price_time_30m_utc,
                price_30m = excluded.price_30m,
                pct_30m = excluded.pct_30m,
                price_time_60m_utc = excluded.price_time_60m_utc,
                price_60m = excluded.price_60m,
                pct_60m = excluded.pct_60m,
                pre30_time_utc = excluded.pre30_time_utc,
                pre30_close = excluded.pre30_close,
                pre30_to_post30_pct = excluded.pre30_to_post30_pct,
                price_reaction_level = excluded.price_reaction_level,
                trigger_window = excluded.trigger_window,
                importance_reason = excluded.importance_reason,
                oil_relevance_reason = excluded.oil_relevance_reason,
                market_bias = excluded.market_bias,
                market_pricing_status = excluded.market_pricing_status,
                price_missing_reason = excluded.price_missing_reason,
                exclusion_reason = excluded.exclusion_reason,
                note = excluded.note
            """,
            (
                news_id,
                reaction.get("baseline_time_utc"),
                reaction.get("baseline_close"),
                reaction.get("window_start_utc"),
                reaction.get("window_end_utc"),
                reaction.get("window_high"),
                reaction.get("window_low"),
                reaction.get("final_close"),
                reaction.get("max_up_pct"),
                reaction.get("max_down_pct"),
                reaction.get("final_pct"),
                reaction["reaction_label"],
                reaction["market_interpretation"],
                iso_utc(now),
                reaction.get("price_status", "ok"),
                reaction.get("multi_event_group_id"),
                reaction.get("multi_event_group_reason"),
                reaction.get("event_base_time_utc"),
                reaction.get("event_base_close"),
                reaction.get("price_time_5m_utc"),
                reaction.get("price_5m"),
                reaction.get("pct_5m"),
                reaction.get("price_time_15m_utc"),
                reaction.get("price_15m"),
                reaction.get("pct_15m"),
                reaction.get("price_time_30m_utc"),
                reaction.get("price_30m"),
                reaction.get("pct_30m"),
                reaction.get("price_time_60m_utc"),
                reaction.get("price_60m"),
                reaction.get("pct_60m"),
                reaction.get("pre30_time_utc"),
                reaction.get("pre30_close"),
                reaction.get("pre30_to_post30_pct"),
                reaction.get("price_reaction_level"),
                reaction.get("trigger_window"),
                reaction.get("importance_reason"),
                reaction.get("oil_relevance_reason"),
                reaction.get("market_bias"),
                reaction.get("market_pricing_status"),
                reaction.get("price_missing_reason"),
                reaction.get("exclusion_reason"),
                reaction.get("note", ""),
            ),
        )
        self.conn.execute(
            """
            UPDATE news
            SET reaction_status = ?, price_status = ?, is_in_daily = ?, updated_at_utc = ?
            WHERE news_id = ?
            """,
            (
                status,
                reaction.get("price_status", "ok"),
                1 if is_in_daily else 0,
                iso_utc(now),
                news_id,
            ),
        )
        self.conn.commit()

    def mark_news_pending(self, news_id: str, price_status: str, note: str) -> None:
        now = utc_now()
        self.conn.execute(
            """
            UPDATE news
            SET reaction_status = 'Pending Brent Reaction Analysis',
                price_status = ?,
                updated_at_utc = ?
            WHERE news_id = ?
            """,
            (price_status, iso_utc(now), news_id),
        )
        self.conn.execute(
            """
            INSERT INTO brent_reactions (
                news_id, reaction_label, market_interpretation, analyzed_at_utc,
                price_status, note
            )
            VALUES (?, 'Pending Brent Reaction Analysis', ?, ?, ?, ?)
            ON CONFLICT(news_id) DO UPDATE SET
                reaction_label = excluded.reaction_label,
                market_interpretation = excluded.market_interpretation,
                analyzed_at_utc = excluded.analyzed_at_utc,
                price_status = excluded.price_status,
                note = excluded.note
            """,
            (news_id, note, iso_utc(now), price_status, note),
        )
        self.conn.commit()

    def refresh_multi_event_groups(self, day_start_utc: datetime, day_end_utc: datetime) -> int:
        self.conn.execute(
            """
            UPDATE brent_reactions
            SET multi_event_group_id = NULL,
                multi_event_group_reason = NULL
            WHERE news_id IN (
                SELECT news_id FROM news
                WHERE published_at_utc >= ? AND published_at_utc < ?
            )
            """,
            (iso_utc(day_start_utc), iso_utc(day_end_utc)),
        )
        rows = self.conn.execute(
            """
            SELECT n.news_id, n.published_at_utc, n.event_id, n.event_topic, n.title
            FROM news n
            JOIN brent_reactions r ON r.news_id = n.news_id
            WHERE n.is_in_daily = 1
              AND n.published_at_utc >= ?
              AND n.published_at_utc < ?
            ORDER BY n.published_at_utc ASC
            """,
            (iso_utc(day_start_utc), iso_utc(day_end_utc)),
        ).fetchall()

        def is_same_cluster(cluster: list[sqlite3.Row], row: sqlite3.Row) -> bool:
            first = cluster[0]
            first_time = parse_iso_utc(first["published_at_utc"])
            row_time = parse_iso_utc(row["published_at_utc"])
            if not first_time or not row_time:
                return False
            if abs(row_time - first_time) > timedelta(minutes=5):
                return False
            first_title = str(first["title"])
            row_title = str(row["title"])
            if "EIA" in first_title and "EIA" in row_title:
                return True
            if str(first["event_id"]) == str(row["event_id"]):
                return True
            specific_topics = {
                "霍尔木兹航运风险",
                "官方售价/供应竞争",
                "LNG供应中断/能源安全",
                "市场预期/投行观点",
                "伊朗核设施/核协议",
                "油轮/货船航运安全",
            }
            if str(first["event_topic"]) == str(row["event_topic"]) and str(row["event_topic"]) in specific_topics:
                return True
            org_markers = ["摩根士丹利", "高盛", "花旗", "瑞银", "道明证券", "摩根大通", "巴克莱"]
            if any(marker in first_title and marker in row_title for marker in org_markers):
                return True
            ratio = difflib.SequenceMatcher(None, str(first["title"]), str(row["title"])).ratio()
            return ratio >= 0.72

        clusters: list[list[sqlite3.Row]] = []
        for row in rows:
            for cluster in clusters:
                if is_same_cluster(cluster, row):
                    cluster.append(row)
                    break
            else:
                clusters.append([row])

        group_count = 0
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            group_count += 1
            earliest = parse_iso_utc(cluster[0]["published_at_utc"])
            latest = parse_iso_utc(cluster[-1]["published_at_utc"])
            key = iso_utc(earliest or day_start_utc)
            group_id = f"group_{key.replace(':', '').replace('-', '').replace('Z', '')}_{group_count}"
            topics = {str(row["event_topic"]) for row in cluster}
            ids = {str(row["event_id"]) for row in cluster}
            if len(ids) == 1:
                kind = "同一事件进展或重复报道"
            elif len(topics) == 1:
                kind = "5分钟内同一细分主题"
            else:
                kind = "5分钟内多条相关消息"
            reason = (
                f"{kind}；共{len(cluster)}条；最早{to_beijing(earliest or day_start_utc).strftime('%H:%M:%S')}，"
                f"最晚{to_beijing(latest or day_start_utc).strftime('%H:%M:%S')}；"
                "价格反应无法可靠归因于单一新闻。"
            )
            for item in cluster:
                self.conn.execute(
                    "UPDATE brent_reactions SET multi_event_group_id = ?, multi_event_group_reason = ? WHERE news_id = ?",
                    (group_id, reason, str(item["news_id"])),
                )
        self.conn.commit()
        return group_count

    def daily_report_rows(self, day_start_utc: datetime, day_end_utc: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT n.*, r.*
            FROM news n
            JOIN brent_reactions r ON r.news_id = n.news_id
            WHERE n.is_in_daily = 1
              AND n.published_at_utc >= ?
              AND n.published_at_utc < ?
            ORDER BY n.published_at_utc ASC
            """,
            (iso_utc(day_start_utc), iso_utc(day_end_utc)),
        ).fetchall()

    def reaction_rows_between(self, day_start_utc: datetime, day_end_utc: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT n.*, r.*
            FROM news n
            LEFT JOIN brent_reactions r ON r.news_id = n.news_id
            WHERE n.published_at_utc >= ?
              AND n.published_at_utc < ?
            ORDER BY n.published_at_utc ASC
            """,
            (iso_utc(day_start_utc), iso_utc(day_end_utc)),
        ).fetchall()

    def report_focus_rows(
        self,
        day_start_utc: datetime,
        day_end_utc: datetime,
        minimum: int = 3,
        limit: int = 5,
    ) -> list[sqlite3.Row]:
        daily = self.daily_report_rows(day_start_utc, day_end_utc)
        if not daily:
            return []

        groups: dict[str, list[sqlite3.Row]] = {}
        order: list[str] = []
        for row in daily:
            key = str(row["multi_event_group_id"] or row["news_id"])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(row)

        def core_abs(row: sqlite3.Row) -> float:
            vals = [
                row["pct_5m"],
                row["pct_15m"],
                row["pct_30m"],
                row["pct_60m"],
                row["pre30_to_post30_pct"],
            ]
            return max((abs(float(v)) for v in vals if v is not None), default=0.0)

        def directness_score(row: sqlite3.Row) -> int:
            text = f"{row['title']} {row['content']} {row['event_topic']} {row['importance_reason'] or ''}"
            summary_penalty = -2 if str(row["oil_relevance_reason"] or "").startswith("汇总类新闻") else 0
            title = str(row["title"] or "")
            topic = str(row["event_topic"] or "")
            if not direct_energy_supply_reason(title, str(row["content"] or "")):
                return 0
            if ("将于" in title and "公布" in title) or "十分钟后" in title or "待公布" in title:
                return max(0, 1 + summary_penalty)
            if (
                title.startswith(("市场分析", "分析师", "机构观点"))
                or "原油头条" in title
                or "为何" in title
            ) and topic in {"市场预期/油价影响", "市场预期/投行观点", "原油市场/能源供应", "霍尔木兹航运风险"}:
                return max(0, 1 + summary_penalty)
            if any(term in title for term in ["原油库存减少", "原油库存增加", "库欣原油库存", "战略石油储备库存"]):
                return 6 + summary_penalty
            physical_terms = [
                "霍尔木兹",
                "油轮",
                "通行",
                "封锁",
                "护航",
                "绕航",
                "滞留",
                "搁浅",
                "遭袭",
                "装船",
                "港口",
                "出口中断",
                "炼厂停运",
                "制裁",
                "豁免",
                "OPEC",
                "欧佩克",
                "官方售价",
                "OSP",
                "LNG",
                "液化天然气",
            ]
            if any(term in text for term in physical_terms):
                return 5 + summary_penalty
            if row["importance_reason"]:
                return 3 + summary_penalty
            if str(row["event_topic"]) in {
                "原油市场/能源供应",
                "官方售价/供应竞争",
                "LNG供应中断/能源安全",
            }:
                return 2 + summary_penalty
            return max(0, 1 + summary_penalty)

        def group_score(key: str) -> tuple[int, int, int, int, float, str]:
            rows = groups[key]
            representative = max(rows, key=core_abs)
            level = str(representative["price_reaction_level"] or representative["reaction_label"] or "")
            level_score = 3 if level.startswith("强市场反应") else 2 if level.startswith("中等市场反应") else 1
            major_score = 1 if any(str(row["importance_reason"] or "") for row in rows) else 0
            direct_score = max(directness_score(row) for row in rows)
            original_score = 0 if any(str(row["oil_relevance_reason"] or "").startswith("汇总类新闻") for row in rows) else 1
            return (
                direct_score,
                major_score,
                original_score,
                level_score,
                max(core_abs(row) for row in rows),
                str(rows[0]["published_at_utc"]),
            )

        selected_keys = sorted(order, key=group_score, reverse=True)[:limit]
        selected: list[sqlite3.Row] = []
        for key in selected_keys:
            selected.extend(groups[key])
        return sorted(selected, key=lambda row: str(row["published_at_utc"]))

    def unpriced_watch_rows(self, day_start_utc: datetime, day_end_utc: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT n.*, r.*
            FROM news n
            LEFT JOIN brent_reactions r ON r.news_id = n.news_id
            WHERE n.published_at_utc >= ?
              AND n.published_at_utc < ?
              AND n.is_duplicate = 0
              AND n.is_in_daily = 0
              AND COALESCE(r.oil_relevance_reason, '') NOT LIKE '与油市无直接关联%'
              AND COALESCE(r.oil_relevance_reason, '') != '汇总类新闻但未提取到具体油市事件'
              AND (
                n.event_type = '重大升级'
                OR COALESCE(r.importance_reason, '') != ''
                OR n.content LIKE '%霍尔木兹%'
                OR n.content LIKE '%油轮%'
                OR n.content LIKE '%出口中断%'
                OR n.content LIKE '%制裁%'
                OR n.content LIKE '%LNG%'
                OR n.content LIKE '%液化天然气%'
                OR n.content LIKE '%官方售价%'
                OR n.content LIKE '%巴士拉%'
              )
            ORDER BY n.published_at_utc ASC
            LIMIT 20
            """,
            (iso_utc(day_start_utc), iso_utc(day_end_utc)),
        ).fetchall()

    def brent_day_klines(self, symbol: str, interval: str, day_start_utc: datetime, day_end_utc: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM brent_klines
            WHERE symbol = ? AND interval = ?
              AND ts_utc >= ? AND ts_utc < ?
            ORDER BY ts_utc ASC
            """,
            (symbol, interval, iso_utc(day_start_utc), iso_utc(day_end_utc)),
        ).fetchall()

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for name, query in {
            "news": "SELECT COUNT(*) AS c FROM news",
            "brent_klines": "SELECT COUNT(*) AS c FROM brent_klines",
            "pending_news": "SELECT COUNT(*) AS c FROM news WHERE reaction_status != 'analyzed'",
            "daily_news": "SELECT COUNT(*) AS c FROM news WHERE is_in_daily = 1",
        }.items():
            row = self.conn.execute(query).fetchone()
            result[name] = int(row["c"])
        return result
