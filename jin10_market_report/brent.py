from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import BrentConfig, YAHOO_CHART_API
from .time_utils import UTC, ensure_utc, iso_utc, utc_now


BRENT_INSTRUMENT_METADATA = {
    "instrument_name": "Brent Crude Oil Last Day Financ",
    "source": "Yahoo Finance",
    "ticker": "BZ=F",
    "contract_month": "",
    "contract_type": "Yahoo Finance BZ=F",
}


class BrentFetchError(RuntimeError):
    pass


@dataclass
class BrentKline:
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    source: str = "yahoo"


@dataclass
class BrentFetchResult:
    klines: list[BrentKline]
    source: str
    symbol: str
    fetched_at_utc: datetime
    latest_kline_utc: datetime | None
    is_stale: bool
    note: str = ""


def _request_json(url: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise BrentFetchError(f"Brent价格接口HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise BrentFetchError(f"Brent价格接口网络失败: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise BrentFetchError("Brent价格接口返回内容不是有效JSON") from exc


def _request_json_with_retry(url: str, config: BrentConfig) -> dict[str, Any]:
    last_error: BrentFetchError | None = None
    retry_waits = (5, 15, 30)
    for attempt in range(1, config.retries + 2):
        try:
            return _request_json(url, config.request_timeout)
        except BrentFetchError as exc:
            last_error = exc
            if attempt > config.retries:
                break
            wait_index = min(attempt - 1, len(retry_waits) - 1)
            time.sleep(retry_waits[wait_index])
    assert last_error is not None
    raise last_error


def build_yahoo_chart_url(config: BrentConfig, latest_ts_utc: datetime | None) -> str:
    base = YAHOO_CHART_API.format(symbol=urllib.parse.quote(config.symbol, safe=""))
    if latest_ts_utc:
        period1 = int(
            (
                ensure_utc(latest_ts_utc)
                - timedelta(minutes=config.incremental_overlap_minutes)
            ).timestamp()
        )
        period2 = int((utc_now() + timedelta(minutes=5)).timestamp())
        params = {"period1": period1, "period2": period2, "interval": config.interval}
    else:
        params = {"range": config.first_run_range, "interval": config.interval}
    return f"{base}?{urllib.parse.urlencode(params)}"


def build_yahoo_chart_range_url(config: BrentConfig, start_utc: datetime, end_utc: datetime) -> str:
    base = YAHOO_CHART_API.format(symbol=urllib.parse.quote(config.symbol, safe=""))
    params = {
        "period1": int(ensure_utc(start_utc).timestamp()),
        "period2": int(ensure_utc(end_utc).timestamp()),
        "interval": config.interval,
    }
    return f"{base}?{urllib.parse.urlencode(params)}"


def _parse_yahoo_chart_payload(config: BrentConfig, payload: dict[str, Any]) -> list[BrentKline]:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise BrentFetchError(f"Brent价格接口错误: {chart['error']}")
    result = chart.get("result") or []
    if not result:
        raise BrentFetchError("Brent价格接口未返回K线数据")

    data = result[0]
    timestamps = data.get("timestamp") or []
    quote = (data.get("indicators") or {}).get("quote") or []
    if not timestamps or not quote:
        raise BrentFetchError("Brent价格接口K线字段缺失")
    q = quote[0]
    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    volumes = q.get("volume") or []

    klines: list[BrentKline] = []
    for idx, ts in enumerate(timestamps):
        try:
            open_v = opens[idx]
            high_v = highs[idx]
            low_v = lows[idx]
            close_v = closes[idx]
        except IndexError:
            continue
        if open_v is None or high_v is None or low_v is None or close_v is None:
            continue
        volume_v = volumes[idx] if idx < len(volumes) else None
        klines.append(
            BrentKline(
                ts_utc=datetime.fromtimestamp(int(ts), tz=UTC),
                open=float(open_v),
                high=float(high_v),
                low=float(low_v),
                close=float(close_v),
                volume=float(volume_v) if volume_v is not None else None,
                source=config.source,
            )
        )
    return klines


def _result_from_klines(config: BrentConfig, klines: list[BrentKline]) -> BrentFetchResult:
    latest = max((k.ts_utc for k in klines), default=None)
    fetched_at = utc_now()
    is_stale = bool(latest and fetched_at - latest > timedelta(minutes=config.stale_after_minutes))
    note = ""
    if is_stale:
        note = f"最新Brent 5分钟K线为 {iso_utc(latest)}，可能存在延迟或休市。"

    return BrentFetchResult(
        klines=klines,
        source=config.source,
        symbol=config.symbol,
        fetched_at_utc=fetched_at,
        latest_kline_utc=latest,
        is_stale=is_stale,
        note=note,
    )


def fetch_brent_5m(config: BrentConfig, latest_ts_utc: datetime | None = None) -> BrentFetchResult:
    if config.source != "yahoo":
        raise BrentFetchError(f"不支持的Brent价格源: {config.source}")

    url = build_yahoo_chart_url(config, latest_ts_utc)
    payload = _request_json_with_retry(url, config)
    return _result_from_klines(config, _parse_yahoo_chart_payload(config, payload))


def fetch_brent_5m_range(config: BrentConfig, start_utc: datetime, end_utc: datetime) -> BrentFetchResult:
    if config.source != "yahoo":
        raise BrentFetchError(f"不支持的Brent价格源: {config.source}")

    url = build_yahoo_chart_range_url(config, start_utc, end_utc)
    payload = _request_json_with_retry(url, config)
    return _result_from_klines(config, _parse_yahoo_chart_payload(config, payload))
