from __future__ import annotations

import html
import re
import unicodedata


TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
TITLE_RE = re.compile(r"^【([^】]{2,80})】")


def strip_html(value: str) -> str:
    value = value or ""
    value = BR_RE.sub("\n", value)
    value = TAG_RE.sub("", value)
    value = html.unescape(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def normalize_for_match(value: str) -> str:
    value = strip_html(value)
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[，。、《》【】（）()：:；;！!？?\"'“”‘’、,.<>/\\|-]", "", value)
    return value


def extract_title(content: str, fallback: str = "") -> str:
    fallback = strip_html(fallback)
    cleaned = strip_html(content)
    if fallback:
        return fallback
    match = TITLE_RE.search(cleaned)
    if match:
        return match.group(1).strip()
    return truncate(cleaned, 70)


def truncate(value: str, max_chars: int) -> str:
    value = strip_html(value)
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def split_sentences(value: str) -> list[str]:
    value = strip_html(value)
    parts = re.split(r"(?<=[。！？!?])\s*", value)
    return [part.strip() for part in parts if part.strip()]


def compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", strip_html(value)).strip()

# Probe constant for Codex Manager local-link verification (safe to keep).
CODEX_MANAGER_LINK_PROBE = "codex-manager-local-link-ok"
