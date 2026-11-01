from __future__ import annotations

import difflib
from collections import Counter

from .config import (
    CORE_GROUPS,
    ENERGY_ANCHORS,
    EVENT_CATEGORIES,
    HIGH_PRIORITY_KEYWORDS,
    KEYWORD_GROUPS,
    MIDDLE_EAST_ANCHORS,
    SECURITY_POLICY_ANCHORS,
    SHIPPING_ANCHORS,
    TRADE_ANCHORS,
    FilterConfig,
)
from .models import ProcessedNews, RawFlash
from .text_utils import compact_spaces, normalize_for_match, split_sentences, truncate
from .eventing import has_direct_oil_market_relevance, major_topic_reason


def _matched_keywords(text: str) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    lower_text = text.lower()
    for category, keywords in KEYWORD_GROUPS.items():
        category_matches = []
        for keyword in keywords:
            if keyword.lower() in lower_text:
                category_matches.append(keyword)
        if category_matches:
            matches[category] = sorted(set(category_matches), key=category_matches.index)
    return matches


def _score(matches: dict[str, list[str]], item: RawFlash) -> int:
    score = 0
    for category, keywords in matches.items():
        weight = 2 if category in CORE_GROUPS else 1
        score += min(4, len(keywords)) * weight
    if item.important:
        score += 2
    return score


def _categories(matches: dict[str, list[str]]) -> list[str]:
    categories = [category for category in EVENT_CATEGORIES if category in matches]
    return categories or ["待人工判断"]


def _high_priority(text: str, item: RawFlash, matches: dict[str, list[str]], score: int) -> bool:
    hit = any(keyword.lower() in text.lower() for keyword in HIGH_PRIORITY_KEYWORDS)
    has_geopolitical = bool({"地缘政治", "战争/安全", "制裁/政策"} & set(matches))
    has_market_channel = bool({"能源供应", "航运物流", "贸易流向", "价格及市场影响"} & set(matches))
    return bool(item.important and hit and (has_geopolitical or has_market_channel)) or (
        hit and has_geopolitical and has_market_channel and score >= 6
    )


def _summary(item: RawFlash) -> str:
    sentences = split_sentences(item.content)
    if not sentences:
        return "待人工判断：原文内容为空或无法解析。"
    if item.title and item.title not in sentences[0]:
        return truncate(f"{item.title}。{sentences[0]}", 180)
    return truncate(sentences[0], 180)


def _contains_any(text: str, words: list[str]) -> bool:
    lower_text = text.lower()
    return any(word.lower() in lower_text for word in words)


def _is_noise_market_overview(
    full_text: str,
    title_text: str,
    has_middle_east: bool,
    has_energy: bool,
    has_shipping: bool,
    has_security_policy: bool,
    has_trade: bool,
) -> bool:
    title_has_strong_anchor = (
        _contains_any(title_text, MIDDLE_EAST_ANCHORS)
        or _contains_any(title_text, ENERGY_ANCHORS)
        or _contains_any(title_text, SHIPPING_ANCHORS)
        or _contains_any(title_text, SECURITY_POLICY_ANCHORS)
        or _contains_any(title_text, TRADE_ANCHORS)
    )
    generic_market_title = [
        "每日市场观察",
        "市场观察",
        "股市",
        "美债",
        "收益率",
        "央行",
        "加息",
        "消费者",
        "通胀",
        "AI驱动",
    ]
    if not title_has_strong_anchor and _contains_any(title_text, generic_market_title):
        return True

    generic_overview = [
        "每日投行/机构观点",
        "今日重点关注的财经数据",
        "全球财经早餐",
        "早盘开盘",
        "早盘收盘",
        "港股收盘",
        "美股",
        "市场赔率",
    ]
    if _contains_any(full_text, generic_overview):
        return True

    gold_macro = ["黄金", "金价", "贵金属"]
    if _contains_any(full_text, gold_macro) and not (has_energy or has_shipping or has_trade):
        return True

    macro_terms = ["央行", "加息", "CPI", "PCE", "收益率", "通胀预期", "GDP"]
    strong_specific_anchor = _contains_any(
        full_text,
        ["伊朗", "以色列", "美伊", "霍尔木兹", "红海", "苏伊士", "油轮", "货船", "LNG", "LPG", "原油", "石油", "天然气"],
    )
    if _contains_any(full_text, macro_terms) and not (has_energy or has_shipping or has_trade or strong_specific_anchor):
        return True

    local_company_project = ["光伏电站", "公司公告", "EPC", "项目合同", "SpaceX", "发射设施"]
    if has_energy and not (has_middle_east or has_shipping or has_security_policy or has_trade):
        if _contains_any(full_text, local_company_project):
            return True

    return False


def _impact_energy(text: str) -> str:
    if _contains_any(text, ["原油", "石油", "油价", "油田", "炼厂", "OPEC", "欧佩克"]):
        return "可能影响原油供应预期或风险溢价；需关注是否进一步传导至实际产量、出口或库存。"
    if _contains_any(text, ["LPG", "液化石油气", "LNG", "液化天然气", "天然气"]):
        return "可能影响 LPG/LNG/天然气供应安全预期；需继续跟踪装船、合同履约和进口安排。"
    if _contains_any(text, ["能源", "供应", "出口", "进口", "制裁", "禁运"]):
        return "可能影响能源供应链或进出口节奏，但原文未给出具体品种和数量，需人工核实。"
    return "待人工判断：原文未直接说明对原油、LPG、LNG或天然气供应的影响。"


def _impact_shipping(text: str) -> str:
    if _contains_any(text, ["霍尔木兹", "红海", "苏伊士", "巴拿马运河", "海峡", "封锁", "通航"]):
        return "可能影响关键航道通行风险、保险成本和绕航概率；需关注船舶等待时间及运费报价。"
    if _contains_any(text, ["航运", "船舶", "油轮", "LNG船", "LPG船", "运费", "航线", "船期", "港口", "装船"]):
        return "可能影响船期、装卸节奏或运费；需结合船舶 AIS、港口排队和经纪报价验证。"
    if _contains_any(text, ["战争", "冲突", "袭击", "导弹", "无人机", "军事行动"]):
        return "存在区域安全风险外溢至航运保险和船东避险行为的可能，需继续观察是否波及航道或港口。"
    return "待人工判断：原文未直接说明对航线、运费、船期或出口节奏的影响。"


def _impact_price_trade(text: str) -> str:
    if _contains_any(text, ["价格", "涨", "跌", "波动", "期货", "现货", "升水", "贴水", "风险溢价", "通胀"]):
        return "可能影响相关大宗商品价格或市场风险偏好；需关注是否形成持续性风险溢价。"
    if _contains_any(text, ["贸易", "出口", "进口", "采购", "买家", "供应链", "制裁", "禁运", "关税"]):
        return "可能改变贸易流向、采购节奏或合同执行成本；需结合后续政策细节和实际成交验证。"
    if _contains_any(text, ["停火", "协议", "谈判", "核查", "核设施"]):
        return "若局势降温，可能压低地缘风险溢价；但需确认协议执行与各方表态是否稳定。"
    return "待人工判断：原文未直接说明对市场价格或贸易流向的影响。"


def _is_relevant(
    matches: dict[str, list[str]],
    score: int,
    config: FilterConfig,
    full_text: str,
    title_text: str,
) -> bool:
    if score < config.minimum_score:
        return False
    has_middle_east = _contains_any(full_text, MIDDLE_EAST_ANCHORS)
    has_energy = _contains_any(full_text, ENERGY_ANCHORS)
    has_shipping = _contains_any(full_text, SHIPPING_ANCHORS)
    has_security_policy = _contains_any(full_text, SECURITY_POLICY_ANCHORS)
    has_trade = _contains_any(full_text, TRADE_ANCHORS)

    if _is_noise_market_overview(
        full_text,
        title_text,
        has_middle_east,
        has_energy,
        has_shipping,
        has_security_policy,
        has_trade,
    ):
        return False

    if has_middle_east:
        return has_security_policy or has_energy or has_shipping or has_trade or score >= 6
    if has_shipping and (has_security_policy or has_energy or has_trade):
        return True
    if has_energy and ("价格及市场影响" in matches or "能源供应" in matches):
        return True
    if has_energy and (has_security_policy or has_trade):
        return True
    if has_trade and (has_security_policy and has_energy):
        return True
    return False


def process_flashes(items: list[RawFlash], config: FilterConfig) -> tuple[list[ProcessedNews], int]:
    processed: list[ProcessedNews] = []
    for item in items:
        text = compact_spaces(f"{item.title} {item.content}")
        title_text = compact_spaces(item.title or item.content)
        matches = _matched_keywords(text)
        score = _score(matches, item)
        direct_oil, _ = has_direct_oil_market_relevance(item.title or item.content, item.content)
        forced_major = bool(major_topic_reason(item.title or item.content, item.content) and direct_oil)
        if not forced_major and not _is_relevant(matches, score, config, text, title_text):
            continue
        keywords = []
        for category in EVENT_CATEGORIES:
            keywords.extend(matches.get(category, []))
        categories = _categories(matches)
        high_priority = _high_priority(text, item, matches, score)
        processed.append(
            ProcessedNews(
                id=item.id,
                published_at=item.published_at,
                raw_time=item.raw_time,
                title_or_text=item.title or truncate(item.content, 80),
                original_text=item.content,
                jin10_url=item.jin10_url,
                keywords=sorted(set(keywords), key=keywords.index),
                categories=categories,
                summary=_summary(item),
                impact_energy=_impact_energy(text),
                impact_shipping=_impact_shipping(text),
                impact_price_trade=_impact_price_trade(text),
                high_priority=high_priority,
                score=score,
            )
        )

    deduped, duplicate_count = dedupe_processed(processed, config.similarity_threshold)
    deduped.sort(key=lambda item: item.published_at, reverse=True)
    return deduped, duplicate_count


def dedupe_processed(items: list[ProcessedNews], threshold: float) -> tuple[list[ProcessedNews], int]:
    result: list[ProcessedNews] = []
    duplicate_count = 0
    signatures: list[str] = []

    for item in items:
        signature = normalize_for_match(item.original_text or item.title_or_text)
        duplicate_index = None
        for idx, existing_signature in enumerate(signatures):
            if signature == existing_signature:
                duplicate_index = idx
                break
            if signature and existing_signature:
                ratio = difflib.SequenceMatcher(None, signature, existing_signature).ratio()
                if ratio >= threshold:
                    duplicate_index = idx
                    break
        if duplicate_index is None:
            result.append(item)
            signatures.append(signature)
            continue
        duplicate_count += 1
        existing = result[duplicate_index]
        existing.merged_ids.append(item.id)
        existing.high_priority = existing.high_priority or item.high_priority
        existing.score = max(existing.score, item.score)
        existing.keywords = list(dict.fromkeys(existing.keywords + item.keywords))
        existing.categories = list(dict.fromkeys(existing.categories + item.categories))

    return result, duplicate_count


def keyword_counter(items: list[ProcessedNews]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for item in items:
        counter.update(item.keywords)
    return counter
