from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

from .time_utils import ensure_utc, iso_utc


TRUMP_TOPIC_ID = 418
TRUMP_TOPIC_URL = "https://xnews.jin10.com/topic/418"
TRUMP_TOPIC_API = "https://reference-api.jin10.com/topic/getById"
TRUMP_TOPIC_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "x-app-id": "irINJPgCgrndSp0F",
    "x-version": "1.0.1",
}

DIRECT_MARKERS = (
    "特朗普：", "特朗普称", "特朗普表示", "特朗普说", "特朗普宣布", "特朗普要求",
    "特朗普警告", "特朗普威胁", "特朗普发文", "特朗普下令", "特朗普公开批评",
    "特朗普坦言", "美国总统特朗普",
)
TOPIC_TERMS = {
    "伊朗": ("伊朗", "美伊", "德黑兰", "核谈判"),
    "霍尔木兹": ("霍尔木兹", "海峡"),
    "战争与军事行动": ("战争", "军事", "打击", "空袭", "导弹", "攻击"),
    "原油与能源": ("原油", "石油", "油价", "汽油", "能源", "油企", "埃克森", "雪佛龙"),
    "制裁": ("制裁", "豁免", "二级关税"),
}

_PUNCTUATION = re.compile(r"[\s\W_]+", re.UNICODE)


def _request_topic_page(page: int, page_size: int = 20) -> dict[str, object]:
    params = urllib.parse.urlencode({"id": TRUMP_TOPIC_ID, "page": page, "page_size": page_size})
    request = urllib.request.Request(
        f"{TRUMP_TOPIC_API}?{params}", headers=TRUMP_TOPIC_HEADERS, method="GET"
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        http_status = int(getattr(response, "status", response.getcode()) or 0)
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if http_status >= 400:
        raise RuntimeError(f"HTTP {http_status}")
    if not isinstance(payload, dict):
        raise ValueError("金十专题接口未返回JSON对象")
    if payload.get("status") != 200:
        raise RuntimeError(f"金十专题接口状态异常：{payload.get('status')}")
    if not isinstance(payload.get("data"), dict):
        raise ValueError("金十专题接口未返回data对象")
    return payload


def _parse_display_datetime(value: object, timezone_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("金十专题条目缺少display_datetime")
    try:
        local = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"无法解析display_datetime：{value!r}") from exc
    return local.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(ZoneInfo("UTC"))


def _fetch_topic_items(
    window_start_utc: datetime,
    window_end_utc: datetime,
    timezone_name: str,
) -> list[dict[str, object]]:
    """Read descending topic pages until the oldest row is outside the report window."""
    start = ensure_utc(window_start_utc)
    page = 1
    items: list[dict[str, object]] = []
    total: int | None = None
    while True:
        payload = _request_topic_page(page)
        data = payload["data"]
        if not isinstance(data, dict):  # guarded by _request_topic_page; narrows for callers/tests
            raise ValueError("金十专题接口未返回data对象")
        raw_list = data.get("list")
        if not isinstance(raw_list, list):
            raise ValueError("金十专题接口未返回list数组")
        page_items = [item for item in raw_list if isinstance(item, dict)]
        if len(page_items) != len(raw_list):
            raise ValueError("金十专题接口list含非对象条目")
        items.extend(page_items)
        if total is None and isinstance(data.get("total"), int):
            total = data["total"]
        if not page_items:
            break
        oldest = min(_parse_display_datetime(item.get("display_datetime"), timezone_name) for item in page_items)
        if oldest < start:
            break
        if total is not None and len(items) >= total:
            break
        page += 1
    return items


def _item_content(item: dict[str, object]) -> str:
    data = item.get("data")
    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    return str(content).strip() if content is not None else ""


def _topics_for_text(text: str) -> list[str]:
    return [topic for topic, terms in TOPIC_TERMS.items() if any(term in text for term in terms)]


def _source_url(item: dict[str, object]) -> str:
    detail_url = item.get("detail_url")
    if isinstance(detail_url, str) and detail_url.strip():
        return urllib.parse.urljoin("https://xnews.jin10.com/", detail_url.strip())
    item_id = item.get("id")
    if item_id is None:
        raise ValueError("金十专题条目缺少id")
    item_type = str(item.get("type") or "").lower()
    if item_type == "video":
        return f"https://v.jin10.com/details.html?id={item_id}"
    if item_type == "flash":
        return f"https://xnews.jin10.com/details/flash/{item_id}"
    return f"https://xnews.jin10.com/details/{item_id}"


def _normalize_item(item: dict[str, object], timezone_name: str) -> dict[str, object]:
    item_id = item.get("id")
    if item_id is None:
        raise ValueError("金十专题条目缺少id")
    published = _parse_display_datetime(item.get("display_datetime"), timezone_name)
    title = str(item.get("title") or "").strip()
    introduction = str(item.get("introduction") or "").strip()
    content = _item_content(item)
    candidate_text = " ".join(part for part in (title, introduction, content) if part)
    return {
        "id": item_id,
        "type": str(item.get("type") or ""),
        "published_at_utc": iso_utc(published),
        "published_at_bj": published.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S"),
        "title": title,
        "introduction": introduction,
        "content": content,
        "text": candidate_text,
        "topics": _topics_for_text(candidate_text),
        "source_url": _source_url(item),
        "_published_dt_utc": published,
        "_candidate_text": candidate_text,
    }


def _is_relevant_statement(item: dict[str, object]) -> bool:
    candidate_text = str(item.get("_candidate_text") or item.get("text") or "")
    return any(marker in candidate_text for marker in DIRECT_MARKERS) and bool(_topics_for_text(candidate_text))


def _normalized_comparison_text(value: str) -> str:
    return _PUNCTUATION.sub("", value).lower()


def _similarity(left: dict[str, object], right: dict[str, object]) -> float:
    left_text = str(left.get("_candidate_text") or left.get("text") or "")
    right_text = str(right.get("_candidate_text") or right.get("text") or "")
    return SequenceMatcher(
        None, _normalized_comparison_text(left_text), _normalized_comparison_text(right_text)
    ).ratio()


def _public_item(item: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _group_statement_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: list[list[dict[str, object]]] = []
    for item in sorted(items, key=lambda row: row["_published_dt_utc"]):
        item_topics = set(item.get("topics") or [])
        for group in groups:
            group_topics = set().union(*(set(row.get("topics") or []) for row in group))
            if item_topics & group_topics and any(_similarity(item, row) >= 0.72 for row in group):
                group.append(item)
                break
        else:
            groups.append([item])

    statements: list[dict[str, object]] = []
    for group in groups:
        ordered = sorted(group, key=lambda row: row["_published_dt_utc"])
        representative = max(
            ordered, key=lambda row: len(str(row.get("_candidate_text") or row.get("text") or ""))
        )
        topics = [topic for topic in TOPIC_TERMS if any(topic in (row.get("topics") or []) for row in ordered)]
        statements.append(
            {
                "statement_id": str(ordered[0]["id"]),
                "published_at_utc": ordered[0]["published_at_utc"],
                "published_at_bj": ordered[0]["published_at_bj"],
                "text": representative.get("_candidate_text") or representative.get("text") or "",
                "topics": topics,
                "source_items": [_public_item(row) for row in ordered],
            }
        )
    return statements


def _success_payload(
    start: datetime,
    end: datetime,
    fetched_items: list[dict[str, object]],
    in_window_items: list[dict[str, object]],
    relevant_items: list[dict[str, object]],
    statements: list[dict[str, object]],
    cache_path: Path,
) -> dict[str, object]:
    return {
        "status": "success",
        "source": "金十数据特朗普专栏",
        "source_url": TRUMP_TOPIC_URL,
        "topic_id": TRUMP_TOPIC_ID,
        "window_start_utc": iso_utc(start),
        "window_end_utc": iso_utc(end),
        "raw_topic_item_count": len(fetched_items),
        "in_window_item_count": len(in_window_items),
        "relevant_source_item_count": len(relevant_items),
        "statement_count": len(statements),
        "statements": statements,
        "cache_hit": False,
        "cache_path": str(cache_path),
    }


def _failure_payload(start: datetime, end: datetime, reason: str, cache_path: Path) -> dict[str, object]:
    return {
        "status": "failed",
        "source": "金十数据特朗普专栏",
        "source_url": TRUMP_TOPIC_URL,
        "topic_id": TRUMP_TOPIC_ID,
        "window_start_utc": iso_utc(start),
        "window_end_utc": iso_utc(end),
        "raw_topic_item_count": 0,
        "in_window_item_count": 0,
        "relevant_source_item_count": 0,
        "statement_count": 0,
        "failure_reason": reason,
        "statements": [],
        "cache_hit": False,
        "cache_path": str(cache_path),
    }


def _cache_path(cache_dir: Path, window_end_utc: datetime, timezone_name: str) -> Path:
    label = ensure_utc(window_end_utc).astimezone(ZoneInfo(timezone_name)).strftime("%Y%m%d_%H%M")
    return cache_dir / f"trump_statements_{label}.json"


def fetch_trump_statements(
    window_start_utc: datetime,
    window_end_utc: datetime,
    cache_dir: Path = Path("data/external_updates"),
    timezone_name: str = "Asia/Shanghai",
    retry_failed_cache: bool = False,
) -> dict[str, object]:
    start = ensure_utc(window_start_utc)
    end = ensure_utc(window_end_utc)
    cache_path = _cache_path(cache_dir, end, timezone_name)
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("status") == "success" or not retry_failed_cache:
            cached["cache_hit"] = True
            cached["cache_path"] = str(cache_path)
            return cached
    try:
        fetched_items = _fetch_topic_items(start, end, timezone_name)
        normalized_items = [_normalize_item(item, timezone_name) for item in fetched_items]
        in_window_items = [
            item for item in normalized_items if start <= item["_published_dt_utc"] < end
        ]
        relevant_items = [item for item in in_window_items if _is_relevant_statement(item)]
        statements = _group_statement_items(relevant_items)
        result = _success_payload(
            start, end, fetched_items, in_window_items, relevant_items, statements, cache_path
        )
    except Exception as exc:
        result = _failure_payload(start, end, str(exc), cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
