from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from .time_utils import ensure_utc, iso_utc, parse_iso_utc, utc_now


ISW_LISTING_URL = (
    "https://understandingwar.org/research/"
    "?_teams=middle-east&_focus_area_filter=iran-proxies&_product_line_filter=iran-update"
)
ISW_BLUESKY_HANDLE = "thestudyofwar.bsky.social"
ISW_BLUESKY_API = "https://public.api.bsky.app/xrpc"
TRANSLATE_API = "https://translate.googleapis.com/translate_a/single"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
ISW_BODY_HEADINGS = {
    "The United States", "Iran", "Iraq", "Lebanon", "Syria", "Yemen", "Gaza Strip"
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "nav", "footer"}:
            self._skip_depth += 1
        if not self._skip_depth and tag in {"p", "li", "h1", "h2", "h3", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "footer"} and self._skip_depth:
            self._skip_depth -= 1
        if not self._skip_depth and tag in {"p", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts)).replace("\xa0", " ")
        return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_text(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", response.getcode()) or 0)
        body = response.read().decode("utf-8", errors="replace")
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")
    if "Attention Required!" in body or "Sorry, you have been blocked" in body:
        raise RuntimeError("目标网站触发Cloudflare访问限制")
    return body


def _request_json(endpoint: str, params: dict[str, object], timeout: int = 20) -> dict[str, object]:
    url = f"{endpoint}?{urllib.parse.urlencode(params)}"
    payload = json.loads(_request_text(url, timeout=timeout))
    if not isinstance(payload, dict):
        raise ValueError("公开接口未返回JSON对象")
    return payload


def _valid_isw_article_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"understandingwar.org", "www.understandingwar.org"}
        and "/iran-update" in parsed.path.lower()
    )


def _extract_listing_links(page: str) -> list[str]:
    links: list[str] = []
    for raw in re.findall(r"href=[\"']([^\"']+)[\"']", page, flags=re.I):
        url = urllib.parse.urljoin(ISW_LISTING_URL, html.unescape(raw))
        if _valid_isw_article_url(url) and url not in links:
            links.append(url)
    return links


def _plain_html_text(page: str) -> str:
    parser = _TextExtractor()
    parser.feed(page)
    return parser.text()


def _extract_meta(page: str, names: tuple[str, ...]) -> str:
    for name in names:
        patterns = (
            rf"<(?:meta|time)[^>]+(?:property|name|itemprop|datetime)=[\"']{re.escape(name)}[\"'][^>]+content=[\"']([^\"']+)",
            rf"<(?:meta|time)[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name|itemprop)=[\"']{re.escape(name)}[\"']",
        )
        for pattern in patterns:
            match = re.search(pattern, page, flags=re.I)
            if match:
                return html.unescape(match.group(1)).strip()
    return ""


def _title_from_page_or_url(page: str, url: str) -> str:
    title = _extract_meta(page, ("og:title", "headline"))
    if not title:
        match = re.search(r"<h1[^>]*>(.*?)</h1>", page, flags=re.I | re.S)
        if match:
            title = _plain_html_text(match.group(1))
    if title:
        return re.sub(r"\s*\|\s*(?:ISW|Institute for the Study of War).*$", "", title).strip()
    slug = urllib.parse.unquote(urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1])
    words = slug.replace("-", " ").title()
    words = re.sub(r"\bIran\b", "Iran", words)
    match = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2}) (\d{4})$", words)
    if match:
        words = words[: match.start()].rstrip() + f", {match.group(1)} {match.group(2)}, {match.group(3)}"
    return words


def _date_from_title(title: str) -> datetime | None:
    match = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
        title,
        flags=re.I,
    )
    if not match:
        return None
    try:
        return datetime.strptime(" ".join(match.groups()), "%B %d %Y").replace(tzinfo=ZoneInfo("America/New_York"))
    except ValueError:
        return None


def _clean_source_bullet(value: str) -> str:
    text = value.replace("\\n", "\n")
    text = re.sub(r"^\s*(?:NEW:\s*)?(?:\d+\s*/(?:\s*\d+)?\s*)?", "", text, flags=re.I)
    text = re.sub(r"\s*\(\d+\s*/\s*\d+\)\s*", " ", text)
    text = re.split(r"\s*(?:Read (?:more|the full report)|More):", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\bisw\.pub/\S+", "", text)
    return " ".join(text.split()).strip(" -")


def _article_key_takeaways(page: str) -> list[str]:
    lines = [line.strip() for line in _plain_html_text(page).splitlines() if line.strip()]
    start = next((index for index, line in enumerate(lines) if line == "Key Takeaways"), None)
    if start is None:
        return []
    collected: list[str] = []
    for line in lines[start + 1 :]:
        if line in ISW_BODY_HEADINGS:
            break
        if line.startswith(("Download the PDF", "Information Cutoff", "The Critical Threats Project")):
            continue
        if len(line) >= 20:
            collected.append(line)
    return collected


def _article_sections(page: str) -> tuple[list[str], list[str]]:
    return _article_key_takeaways(page), []


def _article_bullets(page: str) -> list[str]:
    return _article_key_takeaways(page)


def _translate_to_chinese(text: str) -> str:
    url = f"{TRANSLATE_API}?{urllib.parse.urlencode({'client': 'gtx', 'sl': 'en', 'tl': 'zh-CN', 'dt': 't', 'q': text})}"
    payload = json.loads(_request_text(url, timeout=20))
    if not isinstance(payload, list):
        raise ValueError("机器翻译返回格式异常")
    translated = "".join(
        str(item[0])
        for item in (payload[0] or [])
        if isinstance(item, list) and item and item[0]
    )
    if not translated.strip():
        raise ValueError("机器翻译结果为空")
    return " ".join(translated.split())


def _translate_bullets(bullets: list[str]) -> list[str]:
    return [_translate_to_chinese(item) for item in bullets]


def _resolve_isw_short_url(url: str) -> str:
    if _valid_isw_article_url(url):
        return url
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname != "isw.pub":
        return ""
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS, method="GET")
    try:
        opener.open(request, timeout=20)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            location = exc.headers.get("Location") or ""
            resolved = urllib.parse.urljoin(url, location)
            return resolved if _valid_isw_article_url(resolved) else ""
        raise
    return ""


def _fetch_from_site(as_of_utc: datetime) -> dict[str, object]:
    listing = _request_text(ISW_LISTING_URL)
    as_of_et = ensure_utc(as_of_utc).astimezone(ZoneInfo("America/New_York"))
    errors: list[str] = []
    for url in _extract_listing_links(listing):
        try:
            page = _request_text(url)
            title = _title_from_page_or_url(page, url)
            title_date = _date_from_title(title)
            if title_date and title_date.date() > as_of_et.date():
                continue
            published_raw = _extract_meta(page, ("article:published_time", "datePublished", "dateModified"))
            published = parse_iso_utc(published_raw) if published_raw else None
            if published and published > ensure_utc(as_of_utc):
                continue
            key_takeaways = _article_key_takeaways(page)
            if not key_takeaways:
                raise ValueError("官网未解析到Key Takeaways")
            effective_time = published or title_date or as_of_et
            return {
                "fetch_method": "官网直连",
                "title": title,
                "url": url,
                "published_at_utc": iso_utc(ensure_utc(effective_time)),
                "source_key_takeaways": key_takeaways,
            }
        except Exception as exc:  # continue to the next matching official article
            errors.append(f"{url}: {exc}")
    raise RuntimeError("官网列表未返回可用Iran Update；" + " | ".join(errors[:2]))


def _record_links(record: dict[str, object]) -> list[str]:
    links: list[str] = []
    for facet in record.get("facets") or []:
        if not isinstance(facet, dict):
            continue
        for feature in facet.get("features") or []:
            if isinstance(feature, dict) and feature.get("uri"):
                links.append(str(feature["uri"]))
    return links


def _walk_thread(node: object) -> list[dict[str, object]]:
    if not isinstance(node, dict):
        return []
    found: list[dict[str, object]] = []
    post = node.get("post")
    if isinstance(post, dict):
        found.append(post)
    for reply in node.get("replies") or []:
        found.extend(_walk_thread(reply))
    return found


def _fetch_from_official_bluesky(as_of_utc: datetime) -> dict[str, object]:
    feed = _request_json(
        f"{ISW_BLUESKY_API}/app.bsky.feed.getAuthorFeed",
        {"actor": ISW_BLUESKY_HANDLE, "limit": 100, "filter": "posts_no_replies"},
    )
    cutoff = ensure_utc(as_of_utc)
    roots: list[tuple[datetime, dict[str, object]]] = []
    for item in feed.get("feed") or []:
        if not isinstance(item, dict) or not isinstance(item.get("post"), dict):
            continue
        post = item["post"]
        record = post.get("record") or {}
        if not isinstance(record, dict):
            continue
        created = parse_iso_utc(str(record.get("createdAt") or ""))
        if not created or created > cutoff:
            continue
        text = str(record.get("text") or "")
        links = _record_links(record)
        if "iran update" in text.lower() or any("IranUpdate" in link for link in links):
            roots.append((created, post))
    if not roots:
        raise RuntimeError("截止时点前的ISW官方公开源中未找到Iran Update")
    created, root = max(roots, key=lambda item: item[0])
    uri = str(root.get("uri") or "")
    thread = _request_json(
        f"{ISW_BLUESKY_API}/app.bsky.feed.getPostThread",
        {"uri": uri, "depth": 10},
    )
    links: list[str] = []
    for post in _walk_thread(thread.get("thread")):
        record = post.get("record") or {}
        if isinstance(record, dict):
            links.extend(_record_links(record))
    short_url = next((link for link in links if "isw.pub" in link or _valid_isw_article_url(link)), "")
    original_url = _resolve_isw_short_url(short_url) if short_url else ""
    if not original_url:
        raise RuntimeError("ISW官方线程未提供可解析的官网原文链接")
    page = _request_text(original_url)
    key_takeaways = _article_key_takeaways(page)
    if not key_takeaways:
        raise RuntimeError("ISW官网未解析到Key Takeaways")
    title = _title_from_page_or_url(page, original_url)
    published_raw = _extract_meta(page, ("article:published_time", "datePublished", "dateModified"))
    published = parse_iso_utc(published_raw) if published_raw else None
    return {
        "fetch_method": "ISW官方Bluesky备用源",
        "title": title,
        "url": original_url,
        "published_at_utc": iso_utc(published or created),
        "source_key_takeaways": key_takeaways,
        "official_thread_url": f"https://bsky.app/profile/{ISW_BLUESKY_HANDLE}/post/{uri.rsplit('/', 1)[-1]}",
    }


def _cache_path(cache_dir: Path, as_of_utc: datetime, timezone_name: str) -> Path:
    label = ensure_utc(as_of_utc).astimezone(ZoneInfo(timezone_name)).strftime("%Y%m%d_%H%M")
    return cache_dir / f"isw_iran_update_{label}.json"


def fetch_latest_isw_update(
    as_of_utc: datetime,
    cache_dir: Path = Path("data/external_updates"),
    timezone_name: str = "Asia/Shanghai",
    retry_failed_cache: bool = False,
) -> dict[str, object]:
    """Fetch once per report cutoff unless a failed cache is explicitly retried."""
    cache_path = _cache_path(cache_dir, as_of_utc, timezone_name)
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict):
                raise ValueError("缓存内容不是JSON对象")
        except Exception as exc:
            if not retry_failed_cache:
                return {
                    "status": "failed",
                    "listing_url": ISW_LISTING_URL,
                    "as_of_utc": iso_utc(ensure_utc(as_of_utc)),
                    "fetched_at_utc": iso_utc(utc_now()),
                    "cache_hit": False,
                    "cache_path": str(cache_path),
                    "attempt_errors": [f"ISW缓存读取失败：{exc}"],
                    "failure_reason": f"ISW缓存读取失败：{exc}",
                    "summary_zh": [],
                    "source_key_takeaways": [],
                    "key_takeaways_zh": [],
                    "source_key_takeaways_count": 0,
                    "translated_key_takeaways_count": 0,
                }
        else:
            if cached.get("status") == "success" or not retry_failed_cache:
                cached["cache_hit"] = True
                cached["cache_path"] = str(cache_path)
                return cached

    result: dict[str, object] = {
        "status": "failed",
        "listing_url": ISW_LISTING_URL,
        "as_of_utc": iso_utc(ensure_utc(as_of_utc)),
        "fetched_at_utc": iso_utc(utc_now()),
        "cache_hit": False,
        "cache_path": str(cache_path),
        "attempt_errors": [],
        "summary_zh": [],
        "source_key_takeaways": [],
        "key_takeaways_zh": [],
        "source_key_takeaways_count": 0,
        "translated_key_takeaways_count": 0,
    }
    attempts: list[str] = []
    source: dict[str, object] | None = None
    try:
        source = _fetch_from_site(as_of_utc)
    except Exception as exc:
        attempts.append(f"官网直连失败：{exc}")
    if source is None:
        try:
            source = _fetch_from_official_bluesky(as_of_utc)
        except Exception as exc:
            attempts.append(f"ISW官方Bluesky备用源失败：{exc}")

    if source is not None:
        try:
            source_items = [str(item) for item in source.get("source_key_takeaways") or []]
            translated = _translate_bullets(source_items)
            if not source_items:
                raise ValueError("ISW官网未解析到Key Takeaways")
            if len(translated) != len(source_items):
                raise ValueError(
                    f"ISW Key Takeaways原文与翻译数量不一致：{len(source_items)} != {len(translated)}"
                )
            published = parse_iso_utc(str(source.get("published_at_utc") or ""))
            if not published:
                raise ValueError("未解析到外部update发布时间")
            result.update(
                {
                    **source,
                    "status": "success",
                    "source_key_takeaways": source_items,
                    "key_takeaways_zh": translated,
                    "summary_zh": translated,
                    "source_key_takeaways_count": len(source_items),
                    "translated_key_takeaways_count": len(translated),
                    "published_at_et": published.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M"),
                    "published_at_bj": published.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M"),
                    "translation_method": "Google公开机器翻译端点",
                }
            )
        except Exception as exc:
            attempts.append(f"中文翻译/摘要失败：{exc}")

    result["attempt_errors"] = attempts
    if result.get("status") != "success":
        result["failure_reason"] = " | ".join(attempts) or "未知外部update抓取错误"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
