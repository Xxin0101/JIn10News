"""Evidence and conditional outlook for the regular Brent rolling report."""

import re
from datetime import datetime
from typing import Mapping, Sequence
from urllib.parse import quote, urlsplit

from .eventing import has_direct_oil_market_relevance, identify_event, market_bias_for_event
from .price_layer import next_ice_trading_day, previous_ice_trading_day
from .time_utils import parse_iso_utc, to_beijing


EVIDENCE_HEADING = "来源报道与能源驱动"
OUTLOOK_HEADING = "下一交易日判断（主观分析）"
SAMPLE_NOTICE = "测试样本｜所有行情和新闻均为合成输入，仅供验证生成流程。"
CATEGORIES = ("供给", "需求", "库存", "OPEC/OPEC+", "地缘政治与航运")
OIL_DEMAND = re.compile(r"(?:原油|石油|燃料|成品油|汽油|柴油)(?:的|终端|消费)?需求|炼厂.{0,8}(?:加工|采购)")


def _source_link(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    if host != "jin10.com" and not host.endswith(".jin10.com"):
        return ""
    return quote(value, safe=":/?#&=+%@;,-._~")


def _classify(title: str, body: str) -> str | None:
    text = title + " " + body
    if not any(term in text for term in (
        "原油", "石油", "油价", "布伦特", "Brent", "OPEC", "欧佩克", "油轮", "霍尔木兹", "炼厂",
    )):
        return None
    if "库存" in title:
        return "库存"
    if OIL_DEMAND.search(title):
        return "需求"
    direct, _ = has_direct_oil_market_relevance(title, body)
    if not direct:
        return None
    if any(term in text for term in ("OPEC", "欧佩克")):
        return "OPEC/OPEC+"
    event, _ = identify_event(title, body)
    if event in {"hormuz_shipping", "tanker_shipping", "geopolitical_military", "iran_geopolitics",
                 "iran_nuclear", "israel_levant", "sanctions_policy"}:
        return "地缘政治与航运"
    return "供给"


def _direction_and_mechanism(title: str, body: str, category: str) -> str:
    if category == "需求":
        return "若采购或炼厂用油需求减弱，需求预期下修，Brent 偏空。需求增强则偏多。"
    if category == "库存":
        if any(term in title for term in ("重建", "补库", "持有更大库存")):
            return "若买家实际补库，现货采购需求上升，短期偏多。后续仍需确认采购量及库存数据。"
        return "方向取决于实际库存相对预期的差值。超预期累库通常偏空，超预期去库通常偏多。"
    _, topic = identify_event(title, body)
    return market_bias_for_event(title, body, topic)


def _safe_title(title: str) -> str:
    return " ".join(title.split()).translate(str.maketrans({
        "<": "＜", ">": "＞", "[": "［", "]": "］", "*": "＊", "`": "｀",
    }))


def build_research_sections(
    raw_rows: Sequence[Mapping[str, object]],
    cutoff: datetime,
    timezone_name: str,
    report_status: str,
    price_layer: Mapping[str, object] | None,
    *,
    sample_data: bool = False,
) -> list[str]:
    grouped: dict[str, list[tuple[str, str]]] = {category: [] for category in CATEGORIES}
    seen_titles: set[str] = set()
    for row in reversed(raw_rows):
        published = parse_iso_utc(str(row["published_at_utc"]))
        title = _safe_title(str(row["title"]))
        if not published or published >= cutoff or row["is_summary"] or title in seen_titles:
            continue
        source = _source_link(str(row["url"] or ""))
        if not source and not sample_data:
            continue
        body = str(row["body"] or row["detail"] or "")
        category = _classify(title, body)
        if not category or len(grouped[category]) >= 2:
            continue
        seen_titles.add(title)
        time = to_beijing(published, timezone_name).strftime("%Y-%m-%d %H:%M:%S %z")
        citation = "合成新闻样本" if sample_data else f"[金十数据原文]({source})"
        mechanism = _direction_and_mechanism(title, body, category)
        grouped[category].append((
            f"- **来源报道** {title} · {citation} · {time}",
            f"- **规则解读** {mechanism}",
        ))

    lines = [f"## {EVIDENCE_HEADING}", "", "每类最多展示两条最新的可追溯快讯。规则解读描述可能的传导路径。", ""]
    for category, items in grouped.items():
        lines.extend([f"### {category}", ""])
        lines.extend(line for item in items for line in item)
        if not items:
            lines.append("- 窗口内暂无入选的可追溯快讯。")
        lines.append("")

    local_day = to_beijing(cutoff, timezone_name).date()
    target = next_ice_trading_day(previous_ice_trading_day(local_day)).isoformat()
    direction, reason = "观望", "可核验模型输出缺失，保留条件情景。"
    if sample_data:
        reason = "合成输入仅用于验收流程。"
    elif report_status != "FORMAL":
        reason = "主数据覆盖或更新未通过正式日报门槛。"
    elif price_layer and price_layer.get("status") == "success" and price_layer.get("target_date") == target:
        direction = {"上涨": "偏多", "下跌": "偏空"}.get(price_layer.get("predicted_direction"), "观望")
        reason = "以同一目标会话的纯价格模型为方向参考，需结合后续实物与价格证据验证。"
    lines.extend([
        f"## {OUTLOOK_HEADING}", "",
        f"- **{target} 下一交易日** · {direction} · 定性置信度低。{reason} 置信度为研究判断。",
        "- **上行风险** 若实际减产、出口中断或航运受阻，短期可交付供应收紧，Brent 或获得上行支撑。",
        "- **下行风险** 若供应恢复、需求下修或库存超预期累积，现货紧张程度或风险溢价下降，Brent 或承压。",
        "- **观察条件** 用后续官方库存、产量、装船及航运信息，以及下一会话成交验证情景。",
        "",
    ])
    return lines
