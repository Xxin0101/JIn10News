"""Generate a deterministic, clearly labeled demonstration with an in-memory store."""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .brent import BrentKline
from .config import BrentConfig
from .models import RawFlash
from .storage import TrackerStore
from .tracking_report import beijing_day_bounds, build_html_report, build_markdown_report


def build_sample_report(output_dir: Path) -> Path:
    day = datetime(2026, 11, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    start, cutoff, _ = beijing_day_bounds(day)
    brent = BrentConfig(source="合成行情样本")
    store = TrackerStore(":memory:")
    try:
        examples = (
            (1, "合成情景：原油出口中断，油港暂停装船"),
            (4, "合成情景：原油需求下降，炼厂下调加工计划"),
            (9, "合成情景：EIA原油库存增加"),
            (17, "合成情景：OPEC宣布减产并调整原油产量目标"),
            (23, "合成情景：霍尔木兹海峡油轮通行中断"),
        )
        flashes = []
        for index, (hour, title) in enumerate(examples):
            published = start + timedelta(hours=hour)
            flashes.append(RawFlash(
                id=f"sample-{index}", published_at=published, raw_time=published.isoformat(),
                type=0, title=title, content=title, jin10_url="", important=True,
            ))
        store.upsert_raw_news(flashes, cutoff)
        candles = []
        for minute in range(-30, 24 * 60 + 65, 5):
            timestamp = start + timedelta(minutes=minute)
            price = 70.0 + minute / 240 + (minute // 45 % 3) / 4
            candles.append(BrentKline(timestamp, price, price + 0.2, price - 0.2, price + 0.1,
                                      None, source=brent.source))
        store.upsert_klines(brent.symbol, brent.interval, candles, cutoff)
        result = build_markdown_report(store, brent, output_dir, day=day, sample_data=True)
        return build_html_report(result.markdown_path, output_dir, presentation=result.presentation)
    finally:
        store.close()
