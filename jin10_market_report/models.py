from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RawFlash:
    id: str
    published_at: datetime
    raw_time: str
    type: int
    title: str
    content: str
    jin10_url: str
    important: bool
    source_channel: str = "flash"
    source_url: str = ""
    fetch_page: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessedNews:
    id: str
    published_at: datetime
    raw_time: str
    title_or_text: str
    original_text: str
    jin10_url: str
    keywords: list[str]
    categories: list[str]
    summary: str
    impact_energy: str
    impact_shipping: str
    impact_price_trade: str
    high_priority: bool
    score: int
    merged_ids: list[str] = field(default_factory=list)


@dataclass
class RunStats:
    fetch_started_at: datetime
    fetch_finished_at: datetime | None = None
    fetched_total: int = 0
    within_window_total: int = 0
    filtered_total: int = 0
    duplicate_merged_total: int = 0
    high_priority_total: int = 0
    pages_fetched: int = 0
    failures: list[str] = field(default_factory=list)
    api_status: str = "not_checked"
