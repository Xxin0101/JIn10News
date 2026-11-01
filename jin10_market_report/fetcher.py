from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import DEFAULT_HEADERS, FetchConfig, JIN10_FLASH_API, JIN10_FLASH_DETAIL
from .models import RawFlash, RunStats
from .time_utils import ensure_utc
from .text_utils import extract_title, strip_html


class Jin10FetchError(RuntimeError):
    pass


def parse_jin10_time(value: str, timezone: str) -> datetime:
    tz = ZoneInfo(timezone)
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)


def build_jin10_url(item: dict[str, Any]) -> str:
    data = item.get("data") or {}
    link = str(data.get("link") or "")
    if link.startswith("https://") and "jin10.com" in link:
        return link
    return JIN10_FLASH_DETAIL.format(id=item.get("id", ""))


def _request_json(url: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request_url = f"{url}?{query}"
    req = urllib.request.Request(request_url, headers=DEFAULT_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        raise Jin10FetchError(
            f"HTTP {exc.code}: {exc.reason}; 可能是接口参数、页面结构或访问策略变化"
        ) from exc
    except urllib.error.URLError as exc:
        raise Jin10FetchError(f"网络访问失败: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise Jin10FetchError(f"网络访问失败: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise Jin10FetchError("接口返回内容不是有效 JSON，可能页面结构或接口已变化") from exc


def _source_channel(config: FetchConfig) -> str:
    return f"flash:{config.channel}:vip{config.vip}"


def _flash_api_url(config: FetchConfig, max_time: str | None = None) -> str:
    params: dict[str, Any] = {"channel": config.channel, "vip": config.vip}
    if max_time:
        params["max_time"] = max_time
    return f"{JIN10_FLASH_API}?{urllib.parse.urlencode(params)}"


def _request_json_with_retry(url: str, params: dict[str, Any], config: FetchConfig) -> dict[str, Any]:
    last_error: Jin10FetchError | None = None
    retry_waits = (5, 15, 30)
    for attempt in range(1, config.retries + 2):
        try:
            return _request_json(url, params, config.request_timeout)
        except Jin10FetchError as exc:
            last_error = exc
            if attempt > config.retries:
                break
            wait_index = min(attempt - 1, len(retry_waits) - 1)
            time.sleep(retry_waits[wait_index])
    assert last_error is not None
    raise last_error


def fetch_recent_flashes(config: FetchConfig, stats: RunStats) -> list[RawFlash]:
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    cutoff = now - timedelta(hours=config.hours)
    seen_ids: set[str] = set()
    items: list[RawFlash] = []
    max_time: str | None = None
    reached_cutoff = False
    stopped_with_empty_page = False
    oldest_seen_time: str | None = None

    for page in range(1, config.max_pages + 1):
        params: dict[str, Any] = {"channel": config.channel, "vip": config.vip}
        if max_time:
            params["max_time"] = max_time

        try:
            payload = _request_json_with_retry(JIN10_FLASH_API, params, config)
        except Jin10FetchError as exc:
            if items:
                stats.failures.append(f"分页中途失败，已保留前 {len(items)} 条记录继续生成: {exc}")
                break
            raise
        stats.pages_fetched = page
        if payload.get("status") != 200 or "data" not in payload:
            raise Jin10FetchError(
                f"接口返回异常: status={payload.get('status')} message={payload.get('message')!r}"
            )

        data = payload.get("data") or []
        if not isinstance(data, list):
            raise Jin10FetchError("接口 data 字段不是列表，可能页面结构或接口已变化")
        if not data:
            stopped_with_empty_page = True
            break

        oldest_time_on_page: datetime | None = None
        for item in data:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            raw_time = str(item.get("time") or "")
            if not item_id or not raw_time:
                stats.failures.append(f"跳过缺少 id/time 的记录: {item!r}")
                continue
            try:
                published_at = parse_jin10_time(raw_time, config.timezone)
            except ValueError:
                stats.failures.append(f"无法解析发布时间: id={item_id} time={raw_time!r}")
                continue
            oldest_time_on_page = published_at
            if published_at < cutoff:
                reached_cutoff = True
                continue
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            data_obj = item.get("data") or {}
            title = extract_title(
                str(data_obj.get("content") or ""),
                str(data_obj.get("title") or data_obj.get("name") or ""),
            )
            content = strip_html(
                str(data_obj.get("content") or data_obj.get("title") or data_obj.get("name") or "")
            )
            if not content and title:
                content = title
            items.append(
                RawFlash(
                    id=item_id,
                    published_at=published_at,
                    raw_time=raw_time,
                    type=int(item.get("type") or 0),
                    title=title,
                    content=content,
                    jin10_url=build_jin10_url(item),
                    important=bool(item.get("important")),
                    source_channel=_source_channel(config),
                    source_url=_flash_api_url(config, max_time),
                    fetch_page=page,
                    data=data_obj,
                    raw=item,
                )
            )

        max_time = str(data[-1].get("time") or "")
        if not max_time or reached_cutoff:
            break
        if oldest_time_on_page and oldest_time_on_page < cutoff:
            break
        time.sleep(config.pause_seconds)

    items.sort(key=lambda item: item.published_at, reverse=True)
    stats.fetched_total = len(seen_ids)
    stats.within_window_total = len(items)
    stats.api_status = "ok"
    return items


def fetch_incremental_flashes(
    config: FetchConfig,
    stats: RunStats,
    since_utc: datetime | None,
    known_ids: set[str] | None = None,
    overlap_minutes: int = 10,
) -> list[RawFlash]:
    """Fetch Jin10 flash items newer than the previous successful run.

    This keeps the existing Jin10 API logic intact but stops paging once the
    stream reaches the previous successful fetch window. A small overlap helps
    avoid missing late-arriving items; SQLite de-duplication handles repeats.
    """

    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    cutoff_utc = (
        ensure_utc(since_utc) - timedelta(minutes=overlap_minutes)
        if since_utc
        else ensure_utc(now - timedelta(hours=config.hours))
    )
    known_ids = known_ids or set()
    seen_ids: set[str] = set()
    items: list[RawFlash] = []
    max_time: str | None = None
    reached_cutoff = False

    for page in range(1, config.max_pages + 1):
        params: dict[str, Any] = {"channel": config.channel, "vip": config.vip}
        if max_time:
            params["max_time"] = max_time

        try:
            payload = _request_json_with_retry(JIN10_FLASH_API, params, config)
        except Jin10FetchError as exc:
            if items:
                stats.failures.append(f"分页中途失败，已保留前 {len(items)} 条记录继续处理: {exc}")
                break
            raise

        stats.pages_fetched = page
        if payload.get("status") != 200 or "data" not in payload:
            raise Jin10FetchError(
                f"接口返回异常: status={payload.get('status')} message={payload.get('message')!r}"
            )
        data = payload.get("data") or []
        if not isinstance(data, list):
            raise Jin10FetchError("接口 data 字段不是列表，可能页面结构或接口已变化")
        if not data:
            break

        for item in data:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            raw_time = str(item.get("time") or "")
            if not item_id or not raw_time:
                stats.failures.append(f"跳过缺少 id/time 的记录: {item!r}")
                continue
            try:
                published_at = parse_jin10_time(raw_time, config.timezone)
            except ValueError:
                stats.failures.append(f"无法解析发布时间: id={item_id} time={raw_time!r}")
                continue
            published_utc = ensure_utc(published_at)
            if published_utc < cutoff_utc:
                reached_cutoff = True
                continue
            if item_id in seen_ids or item_id in known_ids:
                continue
            seen_ids.add(item_id)
            data_obj = item.get("data") or {}
            title = extract_title(
                str(data_obj.get("content") or ""),
                str(data_obj.get("title") or data_obj.get("name") or ""),
            )
            content = strip_html(
                str(data_obj.get("content") or data_obj.get("title") or data_obj.get("name") or "")
            )
            if not content and title:
                content = title
            items.append(
                RawFlash(
                    id=item_id,
                    published_at=published_at,
                    raw_time=raw_time,
                    type=int(item.get("type") or 0),
                    title=title,
                    content=content,
                    jin10_url=build_jin10_url(item),
                    important=bool(item.get("important")),
                    source_channel=_source_channel(config),
                    source_url=_flash_api_url(config, max_time),
                    fetch_page=page,
                    data=data_obj,
                    raw=item,
                )
            )

        max_time = str(data[-1].get("time") or "")
        if not max_time or reached_cutoff:
            break
        time.sleep(config.pause_seconds)

    items.sort(key=lambda item: item.published_at)
    stats.fetched_total = len(seen_ids)
    stats.within_window_total = len(items)
    stats.api_status = "ok"
    return items


def fetch_flashes_between(config: FetchConfig, stats: RunStats, start_utc: datetime, end_utc: datetime) -> list[RawFlash]:
    """Fetch public Jin10 flashes within a historical UTC window.

    Jin10's public flash endpoint pages backward from the latest item. The
    website's direct day-query feature may require VIP access, so this function
    intentionally reuses the same public pagination path as the existing
    crawler and stops once the requested start time is reached.
    """

    seen_ids: set[str] = set()
    raw_seen_ids: set[str] = set()
    items: list[RawFlash] = []
    max_time: str | None = ensure_utc(end_utc).astimezone(
        ZoneInfo(config.timezone)
    ).strftime("%Y-%m-%d %H:%M:%S")
    reached_cutoff = False
    stopped_with_empty_page = False
    oldest_seen_time: str | None = None
    raw_min_seen_utc: datetime | None = None

    for page in range(1, config.max_pages + 1):
        params: dict[str, Any] = {"channel": config.channel, "vip": config.vip}
        if max_time:
            params["max_time"] = max_time

        try:
            payload = _request_json_with_retry(JIN10_FLASH_API, params, config)
        except Jin10FetchError as exc:
            if items:
                stats.failures.append(f"历史分页中途失败，已保留前 {len(items)} 条记录继续处理: {exc}")
                break
            raise

        stats.pages_fetched = page
        if payload.get("status") != 200 or "data" not in payload:
            raise Jin10FetchError(
                f"接口返回异常: status={payload.get('status')} message={payload.get('message')!r}"
            )
        data = payload.get("data") or []
        if not isinstance(data, list):
            raise Jin10FetchError("接口 data 字段不是列表，可能页面结构或接口已变化")
        if not data:
            stopped_with_empty_page = True
            break

        for item in data:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            raw_time = str(item.get("time") or "")
            if not item_id or not raw_time:
                stats.failures.append(f"跳过缺少 id/time 的记录: {item!r}")
                continue
            raw_seen_ids.add(item_id)
            try:
                published_at = parse_jin10_time(raw_time, config.timezone)
            except ValueError:
                stats.failures.append(f"无法解析发布时间: id={item_id} time={raw_time!r}")
                continue
            published_utc = ensure_utc(published_at)
            if raw_min_seen_utc is None or published_utc < raw_min_seen_utc:
                raw_min_seen_utc = published_utc
                oldest_seen_time = raw_time
            if published_utc < start_utc:
                reached_cutoff = True
                continue
            if published_utc >= end_utc:
                continue
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            data_obj = item.get("data") or {}
            title = extract_title(
                str(data_obj.get("content") or ""),
                str(data_obj.get("title") or data_obj.get("name") or ""),
            )
            content = strip_html(
                str(data_obj.get("content") or data_obj.get("title") or data_obj.get("name") or "")
            )
            if not content and title:
                content = title
            items.append(
                RawFlash(
                    id=item_id,
                    published_at=published_at,
                    raw_time=raw_time,
                    type=int(item.get("type") or 0),
                    title=title,
                    content=content,
                    jin10_url=build_jin10_url(item),
                    important=bool(item.get("important")),
                    source_channel=_source_channel(config),
                    source_url=_flash_api_url(config, max_time),
                    fetch_page=page,
                    data=data_obj,
                    raw=item,
                )
            )

        max_time = str(data[-1].get("time") or "")
        if raw_min_seen_utc and raw_min_seen_utc <= start_utc:
            reached_cutoff = True
        if not max_time or reached_cutoff:
            break
        time.sleep(config.pause_seconds)

    if not reached_cutoff:
        if stopped_with_empty_page:
            stats.failures.append(
                "历史回补在公开接口继续分页时返回空列表，"
                f"最早已见时间为 {oldest_seen_time or '未知'}；目标日期可能需要登录/VIP历史查询或其他金十公开页面。"
            )
        else:
            stats.failures.append(
                f"历史回补达到max_pages={config.max_pages}仍未到达目标开始时间，可能需要提高--max-pages。"
            )
    items.sort(key=lambda item: item.published_at)
    stats.fetched_total = len(raw_seen_ids)
    stats.within_window_total = len(items)
    stats.api_status = "ok"
    return items
