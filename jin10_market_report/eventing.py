from __future__ import annotations

import re

from .config import ENERGY_ANCHORS, MIDDLE_EAST_ANCHORS, SECURITY_POLICY_ANCHORS, SHIPPING_ANCHORS, TRADE_ANCHORS
from .text_utils import compact_spaces


MAJOR_UPGRADE_TERMS = [
    "实际",
    "停航",
    "停运",
    "中断",
    "受损",
    "遭袭",
    "袭击",
    "封锁",
    "关闭",
    "限制通行",
    "绕航",
    "落地",
    "取消豁免",
    "正式实施",
    "减产",
    "增产",
    "停产",
    "爆炸",
]


PHYSICAL_WATCH_TERMS = [
    "霍尔木兹",
    "油轮",
    "货船",
    "停航",
    "绕航",
    "通行中断",
    "出口中断",
    "装船",
    "港口",
    "码头",
    "油田",
    "管道",
    "炼厂",
    "停运",
    "受损",
    "制裁",
    "豁免",
    "OPEC",
    "欧佩克",
    "产量决策",
]

HORMUZ_SHIPPING_TERMS = [
    "封锁",
    "通行",
    "通航",
    "恢复",
    "收费",
    "护航",
    "袭击",
    "油轮",
    "货轮",
    "船只",
    "船舶",
    "搁浅",
    "滞留",
    "绕航",
    "驶出",
    "驶入",
    "通过",
    "限制",
    "安全畅通",
    "联合委员会",
    "管理",
]

MILITARY_TERMS = [
    "军事",
    "升级",
    "缓和",
    "停火",
    "冲突",
    "袭击",
    "空袭",
    "导弹",
    "无人机",
    "报复",
    "防空",
    "军方",
    "战争",
]
MILITARY_CORE_TERMS = [
    "军事",
    "停火",
    "冲突",
    "袭击",
    "空袭",
    "导弹",
    "无人机",
    "报复",
    "防空",
    "军方",
    "战争",
    "武装",
    "打击",
    "谈判破裂",
    "威胁",
]

OSP_TERMS = [
    "官方售价",
    "OSP",
    "官价",
    "巴士拉",
    "阿拉伯轻质",
    "原油价格",
    "升水",
    "贴水",
    "升贴水",
]

LNG_SUPPLY_TERMS = [
    "LNG",
    "液化天然气",
    "卡塔尔",
    "出口受阻",
    "紧急采购",
    "供应中断",
    "采购液化天然气",
]

INVESTMENT_BANK_TERMS = [
    "摩根士丹利",
    "高盛",
    "花旗",
    "瑞银",
    "道明证券",
    "摩根大通",
    "巴克莱",
    "投行",
    "机构",
]

FORECAST_TERMS = [
    "上调",
    "下调",
    "预测",
    "预计",
    "预期",
    "估计",
    "假设",
    "均价",
    "料将",
    "或将",
    "可能",
    "目标价",
    "油价预期",
    "价格预期",
]
NON_ENERGY_COMMODITY_TERMS = [
    "铝价",
    "铝市场",
    "LME铝",
    "铜价",
    "金价",
    "黄金",
    "白银",
    "银锭",
    "贵金属",
    "金银",
    "碳酸锂",
    "锂矿",
    "锂盐",
    "电池",
    "铜冶炼",
]
OIL_GAS_TERMS = ["油价", "原油", "石油", "布伦特", "Brent", "WTI", "LNG", "LPG", "天然气", "液化天然气"]
OIL_MARKET_CONTEXT_TERMS = OIL_GAS_TERMS + [
    "霍尔木兹",
    "油轮",
    "油田",
    "炼厂",
    "炼油商",
    "中东",
    "伊朗",
    "海湾",
    "OPEC",
    "欧佩克",
    "制裁",
    "豁免",
    "OSP",
    "官方售价",
    "巴士拉",
]
GENERIC_TRADE_ONLY_TERMS = ["出口", "港口", "装船", "码头", "贸易"]
MACRO_SURVEY_TERMS = ["信心指数", "短观", "制造商信心", "企业信心", "消费者信心", "PMI"]
MACRO_PASS_THROUGH_TERMS = ["通胀", "央行", "利率", "降息", "加息", "薪资", "CPI", "就业", "失业率", "国债", "收益率", "债券"]
MACRO_ONLY_TITLE_TERMS = [
    "贸易顺差",
    "贸易逆差",
    "GDP",
    "经济增速",
    "经济增长",
    "预算",
    "消费支出",
    "零售销售",
    "批发销售",
]
COMMENTARY_TITLE_TERMS = [
    "直播",
    "市场赔率",
    "怎么看",
    "为什么",
    "为何",
    "多远",
    "复盘",
    "解读",
    "油价近端曲线",
]
PRICE_QUOTE_TITLE_TERMS = [
    "日内涨",
    "日内跌",
    "涨超",
    "跌超",
    "涨幅扩大",
    "跌幅扩大",
    "现报",
    "站上",
    "跌破",
]
SCHEDULE_ONLY_TERMS = ["将于", "十分钟后", "稍后", "待公布", "即将公布"]
SUMMARY_TITLE_TERMS = ["每日", "汇总", "要闻速递", "ETF", "市场要闻", "财经早餐", "一图看", "重点关注", "财经数据与事件"]
DIRECT_OIL_EVENT_TERMS = [
    "原油",
    "油价",
    "布伦特",
    "Brent",
    "WTI",
    "石油",
    "炼油",
    "炼厂",
    "油轮",
    "霍尔木兹",
    "LNG",
    "LPG",
    "液化天然气",
    "天然气",
    "OPEC",
    "欧佩克",
    "OSP",
    "官方售价",
    "巴士拉",
    "装船",
    "港口",
    "出口",
    "制裁",
    "豁免",
]
SUMMARY_STRONG_OIL_TERMS = [
    "原油",
    "油价",
    "布伦特",
    "Brent",
    "WTI",
    "石油",
    "炼油",
    "炼厂",
    "炼油商",
    "产油国",
    "油轮",
    "霍尔木兹",
    "LNG",
    "LPG",
    "液化天然气",
    "天然气",
    "OPEC",
    "欧佩克",
    "OSP",
    "官方售价",
    "巴士拉",
    "装船",
    "港口",
    "油田",
    "管道",
    "能源供应",
]
GENERIC_OIL_CONTEXT_TERMS = OIL_GAS_TERMS + [
    "霍尔木兹",
    "油轮",
    "油田",
    "炼厂",
    "炼油商",
    "产油国",
    "中东",
    "伊朗",
    "海湾",
    "沙特",
    "阿联酋",
    "卡塔尔",
    "伊拉克",
    "俄罗斯",
    "OPEC",
    "欧佩克",
    "OSP",
    "官方售价",
    "巴士拉",
]
DIRECT_OIL_TITLE_TERMS = [
    "原油",
    "油价",
    "布伦特",
    "Brent",
    "WTI",
    "石油",
    "炼油",
    "炼厂",
    "油轮",
    "霍尔木兹",
    "LNG",
    "LPG",
    "液化天然气",
    "天然气",
    "OPEC",
    "欧佩克",
    "OSP",
    "官方售价",
    "巴士拉",
]
GENERIC_SUPPLY_POLICY_TERMS = ["出口", "港口", "装船", "码头", "制裁", "豁免", "产量", "减产", "增产", "库存"]
PRODUCTION_CONTEXT_TERMS = OIL_GAS_TERMS + [
    "油田",
    "炼油",
    "炼厂",
    "炼油厂",
    "产油国",
    "OPEC",
    "欧佩克",
    "沙特",
    "阿联酋",
    "伊拉克",
    "伊朗",
    "俄罗斯",
    "巴西国家石油局",
    "EIA",
    "美国能源信息署",
]
MIDDLE_EAST_SECURITY_CONTEXT_TERMS = [
    "伊朗",
    "以色列",
    "海湾",
    "中东",
    "霍尔木兹",
    "黎巴嫩",
    "加沙",
    "哈马斯",
    "真主党",
    "也门",
    "胡塞",
    "沙特",
    "阿联酋",
    "卡塔尔",
]
COMPANY_MARKET_NOISE_TERMS = [
    "股票",
    "股价",
    "个股",
    "A股",
    "港股",
    "美股",
    "上市公司",
    "财报",
    "业绩",
    "营收",
    "盈利",
    "利润",
    "市值",
    "股东",
    "分红",
    "股息",
    "回购",
    "增持",
    "减持",
    "解禁",
    "基石投资者",
    "目标价",
    "评级",
    "配售",
    "IPO",
    "异动公告",
    "收盘价格",
    "关联交易",
    "主营业务",
    "公司经营",
    "机器人",
    "电子业务",
    "电子材料",
    "概念炒作",
    "过度解读",
    "OpenAI",
    "GPT",
    "Grok",
    "Claude",
    "Gemini",
    "大模型",
    "模型",
    "算力",
    "AI",
    "芯片",
    "半导体",
    "互联网",
]
REAL_ENERGY_EVENT_TERMS = [
    "出口受阻",
    "紧急采购",
    "供应中断",
    "进口中断",
    "停运",
    "停产",
    "短缺",
    "装船",
    "港口",
    "码头",
    "油轮",
    "LNG船",
    "液化天然气船",
    "霍尔木兹",
    "制裁",
    "豁免",
]
DIRECT_SUPPLY_CHANGE_TERMS = [
    "供应中断",
    "供应受阻",
    "供应短缺",
    "出口中断",
    "出口受阻",
    "出口下降",
    "出口减少",
    "装船中断",
    "装船受阻",
    "港口关闭",
    "港口停运",
    "码头停运",
    "炼厂停运",
    "炼厂关闭",
    "炼厂故障",
    "油田停产",
    "油田关闭",
    "管道停运",
    "管道中断",
    "停产",
    "停运",
    "减产",
    "增产",
    "产量配额",
    "产量目标",
    "库存下降",
    "库存减少",
    "库存增加",
    "战略石油储备",
    "制裁",
    "禁运",
    "豁免",
    "出口管制",
    "霍尔木兹",
    "油轮",
    "LNG船",
    "液化天然气船",
    "袭击",
    "遭袭",
    "受损",
    "扣留",
    "滞留",
    "封锁",
    "限制通行",
    "绕航",
    "停航",
    "护航",
    "OPEC",
    "欧佩克",
    "官方售价",
    "OSP",
]
DIRECT_SUPPLY_CONTEXT_TERMS = OIL_GAS_TERMS + [
    "能源供应",
    "油田",
    "油轮",
    "炼厂",
    "炼油厂",
    "炼油商",
    "原油出口",
    "石油出口",
    "原油装船",
    "油港",
    "油码头",
    "港口",
    "装船",
    "霍尔木兹",
    "海湾",
    "OPEC",
    "欧佩克",
    "产油国",
    "沙特",
    "伊朗",
    "伊拉克",
    "俄罗斯",
    "巴西国家石油局",
    "EIA",
    "美国能源信息署",
    "API",
]

# Countries and generic military actions are intentionally absent: neither can
# establish a direct oil-market transmission path without a physical anchor.
DIRECT_ENERGY_PHYSICAL_ANCHORS = [
    "原油",
    "石油",
    "LNG",
    "LPG",
    "天然气",
    "液化天然气",
    "能源供应",
    "油气供应",
    "油田",
    "炼厂",
    "炼油厂",
    "油港",
    "油码头",
    "原油出口",
    "石油出口",
    "原油装船",
    "石油装船",
    "输油管道",
    "油气管道",
    "天然气管道",
    "原油库存",
    "库欣原油",
    "战略石油储备",
]
DIRECT_ENERGY_TRANSPORT_ANCHORS = [
    "油轮",
    "LNG船",
    "液化天然气船",
    "原油运输",
    "石油运输",
    "能源运输",
    "油气运输",
    "霍尔木兹",
]
RECOGNIZED_SHIPPING_DISRUPTION_TERMS = [
    "海上禁令",
    "海上航行禁令",
    "航行禁令",
    "航运禁令",
    "通航禁令",
    "海上封锁",
    "航道封锁",
]


def _has(text: str, terms: list[str]) -> bool:
    lower = text.lower()
    for term in terms:
        term_lower = term.lower()
        if term.isascii() and term.replace("+", "").replace("/", "").replace("-", "").isalpha():
            if re.search(rf"(?<![a-z]){re.escape(term_lower)}(?![a-z])", lower):
                return True
            continue
        if term_lower in lower:
            return True
    return False


def _has_oil_context(text: str) -> bool:
    for term in GENERIC_OIL_CONTEXT_TERMS:
        if term == "炼厂" and "冶炼厂" in text and text.count("炼厂") == text.count("冶炼厂"):
            continue
        if _has(text, [term]):
            return True
    return False


def _has_production_oil_context(text: str) -> bool:
    for term in PRODUCTION_CONTEXT_TERMS:
        if term == "炼厂" and "冶炼厂" in text and text.count("炼厂") == text.count("冶炼厂"):
            continue
        if _has(text, [term]):
            return True
    return False


def _has_military_context(text: str) -> bool:
    if _has(text, MILITARY_CORE_TERMS):
        return True
    return _has(text, ["升级", "缓和"]) and _has(
        text,
        ["军事", "冲突", "停火", "谈判", "核", "制裁", "霍尔木兹", "导弹", "无人机", "军方", "武装"],
    )


def _is_schedule_only(title: str, content: str) -> bool:
    title_text = compact_spaces(title)
    text = compact_spaces(f"{title} {content}")
    if not _has(title_text, SCHEDULE_ONLY_TERMS):
        return False
    data_terms = ["EIA", "美国能源信息署", "库存", "数据", "CPI", "PMI", "就业", "钻井", "非农", "初请"]
    if _has(text, SCHEDULE_ONLY_TERMS) and _has(text, ["公布"]):
        return True
    return _has(text, SCHEDULE_ONLY_TERMS) and _has(text, ["发布"]) and _has(text, data_terms)


def _is_macro_pass_through(title: str, content: str) -> bool:
    title_text = compact_spaces(title)
    text = compact_spaces(f"{title} {content}")
    if not _has(title_text, MACRO_PASS_THROUGH_TERMS):
        return False
    direct_supply = [
        "原油库存",
        "库欣原油",
        "战略石油储备",
        "EIA",
        "美国能源信息署",
        "OPEC",
        "欧佩克",
        "霍尔木兹",
        "油轮",
        "原油出口",
        "石油出口",
        "LNG出口",
        "液化天然气出口",
        "炼厂停运",
        "供应中断",
    ]
    return not _has(text, direct_supply)


def _is_company_market_noise(title: str, content: str) -> bool:
    text = compact_spaces(f"{title} {content}")
    return _has(text, COMPANY_MARKET_NOISE_TERMS) and not _has(text, REAL_ENERGY_EVENT_TERMS)


def direct_energy_supply_reason(title: str, content: str) -> str | None:
    """Return why this item has a direct crude/energy supply transmission path."""
    title_text = compact_spaces(title)
    text = compact_spaces(f"{title} {content}")
    if is_summary_title(title):
        extracted = extract_oil_event_from_summary(title, content)
        if not extracted:
            return None
        title_text = extracted
        text = compact_spaces(extracted)
    if _has(title_text, NON_ENERGY_COMMODITY_TERMS) and not _has(title_text, OIL_GAS_TERMS):
        return None
    if _is_company_market_noise(title, content):
        return None
    if _has(title_text, ["OpenAI", "GPT", "Grok", "Claude", "Gemini", "大模型", "模型", "算力", "芯片", "半导体", "互联网"]) and not _has(
        title_text,
        OIL_GAS_TERMS,
    ):
        return None
    if _has(text, INVESTMENT_BANK_TERMS) and _has(text, FORECAST_TERMS) and _has(text, ["油价", "原油", "布伦特", "Brent"]):
        # Forecast/commentary is not a hard supply-side fact even when it mentions Hormuz or sanctions.
        return None
    if _has(title_text, MACRO_SURVEY_TERMS) and not _has(title_text, OIL_GAS_TERMS):
        return None
    title_has_supply_anchor = _has(title_text, DIRECT_ENERGY_PHYSICAL_ANCHORS) or _has(
        title_text, DIRECT_ENERGY_TRANSPORT_ANCHORS + RECOGNIZED_SHIPPING_DISRUPTION_TERMS
    )
    if _has(title_text, MACRO_PASS_THROUGH_TERMS):
        return None
    if _has(title_text, MACRO_ONLY_TITLE_TERMS) and not title_has_supply_anchor:
        return None
    if _has(title_text, COMMENTARY_TITLE_TERMS):
        return None
    if _has(title_text, PRICE_QUOTE_TITLE_TERMS):
        return None
    if _is_macro_pass_through(title, content):
        return None
    if _is_schedule_only(title, content):
        return None
    if _has(text, DIRECT_SUPPLY_CHANGE_TERMS) and _has(text, DIRECT_ENERGY_PHYSICAL_ANCHORS):
        return "直接涉及原油/能源供应、运输、制裁、库存或产量变化"
    if _has(text, ["OPEC", "欧佩克"]) and _has(text, ["决定", "会议", "配额", "减产", "增产", "产量目标"]):
        return "直接涉及OPEC/OPEC+产量政策"
    if _has(text, ["EIA", "API", "美国能源信息署"]) and _has(text, ["原油库存", "库欣原油", "战略石油储备", "汽油库存", "精炼油库存"]):
        return "直接涉及美国油品库存数据"
    if _has(text, RECOGNIZED_SHIPPING_DISRUPTION_TERMS) and _has(text, ["胡塞", "也门", "红海", "沙特", "霍尔木兹", "海湾"]):
        return "直接涉及正式海上航行限制风险"
    if "霍尔木兹" in text and _has(text, ["油轮", "船", "通行", "封锁", "护航", "绕航", "袭击", "受损", "航运"]):
        return "直接涉及霍尔木兹能源运输风险"
    if _has(text, ["制裁", "禁运", "豁免", "出口管制"]) and _has(
        text,
        DIRECT_ENERGY_PHYSICAL_ANCHORS + DIRECT_ENERGY_TRANSPORT_ANCHORS,
    ):
        return "直接涉及能源制裁或出口限制"
    if _has(text, DIRECT_ENERGY_PHYSICAL_ANCHORS) and _has(
        text,
        ["停运", "停产", "关闭", "中断", "受阻", "恢复", "故障", "袭击", "受损"],
    ):
        return "直接涉及能源基础设施运行变化"
    return None


def has_direct_energy_supply_path(title: str, content: str) -> bool:
    return direct_energy_supply_reason(title, content) is not None


def _has_middle_east_security_context(text: str) -> bool:
    return _has(text, MIDDLE_EAST_SECURITY_CONTEXT_TERMS)


def is_summary_title(title: str) -> bool:
    return _has(compact_spaces(title), SUMMARY_TITLE_TERMS)


def extract_oil_event_from_summary(title: str, content: str) -> str | None:
    if not is_summary_title(title):
        return None
    if _has(title, ["重点关注", "财经数据与事件"]):
        return None
    text = compact_spaces(content)
    chunks = []
    for raw in text.replace("；", "。").replace(";", "。").split("。"):
        part = raw.strip()
        if part:
            chunks.append(part)
    for part in chunks:
        if _is_schedule_only(title, part):
            continue
        if _has(part, SUMMARY_STRONG_OIL_TERMS) and not _has(part, ["ETF", "股票回购", "芯片", "半导体"]):
            return part[:240]
    return None


def has_direct_oil_market_relevance(title: str, content: str) -> tuple[bool, str]:
    title_text = compact_spaces(title)
    text = compact_spaces(f"{title} {content}")
    direct_supply = direct_energy_supply_reason(title, content)
    if _has(title_text, NON_ENERGY_COMMODITY_TERMS) and not _has(title_text, OIL_GAS_TERMS):
        return False, "与油市无直接关联：标题核心为非能源商品或资产交易"
    if _is_company_market_noise(title, content):
        return False, "与油市无直接关联：公司公告或股价异动，仅间接涉及能源业务"
    if _has(title_text, MACRO_SURVEY_TERMS) and not _has(title_text, OIL_GAS_TERMS):
        return False, "与油市无直接关联：标题核心为宏观调查/信心数据"
    if _is_macro_pass_through(title, content):
        return False, "与油市无直接关联：标题核心为宏观/通胀，仅间接提及油价"
    if is_summary_title(title):
        if direct_supply:
            return True, direct_supply
        return False, "汇总类新闻但未提取到具体油市事件"
    if direct_supply:
        return True, direct_supply
    if _is_schedule_only(title, content):
        return False, "油市数据预告，非实际供给端结果"
    if _has(title_text, GENERIC_TRADE_ONLY_TERMS) and not _has(title_text, OIL_MARKET_CONTEXT_TERMS):
        return False, "与油市无直接关联：标题核心为非能源出口、港口或贸易信息"
    return False, "与油市无直接关联或缺少明确能源供应端传导路径"


def identify_event(title: str, content: str) -> tuple[str, str]:
    text = compact_spaces(f"{title} {content}")
    title_text = compact_spaces(title)
    direct_supply = direct_energy_supply_reason(title, content)
    if is_summary_title(title):
        extracted = extract_oil_event_from_summary(title, content)
        if extracted:
            text = compact_spaces(extracted)
            title_text = text
        else:
            return "other_relevant", "其他相关事件"
    if _has(title_text, NON_ENERGY_COMMODITY_TERMS) and not _has(title_text, OIL_GAS_TERMS):
        return "other_relevant", "其他相关事件"
    if _is_company_market_noise(title, content):
        return "other_relevant", "其他相关事件"
    if _has(title_text, MACRO_SURVEY_TERMS) and not _has(title_text, OIL_GAS_TERMS):
        return "other_relevant", "其他相关事件"
    if _is_macro_pass_through(title, content):
        return "other_relevant", "其他相关事件"
    if _has(text, LNG_SUPPLY_TERMS) and _has(
        text, ["LNG", "液化天然气", "出口受阻", "紧急采购", "供应中断", "进口中断", "短缺", "停运"]
    ):
        return "lng_supply_security", "LNG供应中断/能源安全"
    if _has(text, INVESTMENT_BANK_TERMS) and _has(text, FORECAST_TERMS) and _has(text, ["油价", "原油", "布伦特", "Brent"]):
        return "bank_oil_forecast", "市场预期/投行观点"
    if _has(title_text, ["油价", "能源价格"]) and _has(text, ["回落", "回吐", "预期", "风险溢价", "供应担忧", "短缺"]):
        return "oil_market_expectations", "市场预期/油价影响"
    if _has(text, OSP_TERMS) and _has(text, ["上调", "下调", "降低", "提高", "价格", "售价", "升水", "贴水", "折扣"]):
        return "official_selling_price", "官方售价/供应竞争"
    if _has(text, ["炼油商", "采购渠道", "现货市场采购", "长期合同", "减少对中东的依赖"]) and _has(
        text, ["原油", "石油", "炼油", "供应冲击", "中东"]
    ):
        return "crude_trade_flows", "原油贸易流向/采购调整"
    if "霍尔木兹" in text and _has(text, HORMUZ_SHIPPING_TERMS):
        return "hormuz_shipping", "霍尔木兹航运风险"
    if _has(text, ["油轮", "货船", "船舶", "LNG船", "LPG船"]) and _has(text, ["袭击", "停航", "绕航", "通行", "航线"]):
        return "tanker_shipping", "油轮/货船航运安全"
    if "伊朗" in text and _has(text, ["核", "IAEA", "国际原子能机构", "核设施"]):
        return "iran_nuclear", "伊朗核设施/核协议"
    if (
        _has(text, ["伊朗", "美国", "以色列", "海湾国家", "沙特", "阿联酋", "卡塔尔"])
        and _has_middle_east_security_context(text)
        and _has_military_context(text)
        and direct_supply
    ):
        return "geopolitical_military", "地缘政治/军事局势"
    if "伊朗" in text:
        return "iran_geopolitics", "伊朗相关地缘局势"
    if _has(text, ["以色列", "黎巴嫩", "加沙", "哈马斯", "真主党"]):
        return "israel_levant", "以色列/黎巴嫩/加沙安全局势"
    if _has(text, ["制裁", "禁运", "出口管制", "豁免"]) and _has_oil_context(text):
        return "sanctions_policy", "制裁/政策变化"
    if _has(text, ["OPEC", "欧佩克"]) or (_has(text, ["减产", "增产", "产量"]) and _has_production_oil_context(text)):
        return "opec_supply", "OPEC/OPEC+供应决策"
    if _has(text, ["出口", "装船", "港口", "码头"]) and _has(text, OIL_MARKET_CONTEXT_TERMS):
        return "exports_loading", "出口/装船/港口节奏"
    if _has(text, ["LNG", "LPG", "液化天然气", "液化石油气", "天然气"]):
        return "gas_lng_lpg", "天然气/LNG/LPG供应"
    if _has(text, ENERGY_ANCHORS):
        return "crude_oil_market", "原油市场/能源供应"
    if _has(text, SHIPPING_ANCHORS):
        return "shipping_logistics", "航运物流"
    if _has(text, MIDDLE_EAST_ANCHORS):
        return "middle_east_geopolitics", "中东地缘局势"
    if _has(text, SECURITY_POLICY_ANCHORS):
        return "security_policy", "安全/政策事件"
    if _has(text, TRADE_ANCHORS) and _has(text, OIL_MARKET_CONTEXT_TERMS):
        return "trade_flows", "贸易流向"
    return "other_relevant", "其他相关事件"


def event_type_for(title: str, content: str, has_previous: bool) -> str:
    if not has_previous:
        return "首次事件"
    text = compact_spaces(f"{title} {content}")
    if _has(text, MAJOR_UPGRADE_TERMS):
        return "重大升级"
    return "事件更新"


def is_physical_watch_event(title: str, content: str, event_type: str) -> bool:
    text = compact_spaces(f"{title} {content}")
    if major_topic_reason(title, content):
        return True
    if event_type == "重大升级" and _has(text, PHYSICAL_WATCH_TERMS):
        return True
    if "霍尔木兹" in text and _has(text, ["通行", "停航", "中断", "限制", "封锁", "绕航", "护航", "收费", "滞留"]):
        return True
    if _has(text, ["油轮", "货船", "LNG船", "LPG船"]) and _has(text, ["袭击", "停航", "绕航", "受损"]):
        return True
    if _has(text, ["出口中断", "装船中断", "港口停运", "码头停运", "油田停产", "管道停运", "炼厂停运"]):
        return True
    if _has(text, ["制裁正式", "制裁落地", "取消豁免", "豁免取消"]):
        return True
    if _has(text, ["OPEC", "欧佩克"]) and _has(text, ["决定", "减产", "增产", "配额"]):
        return True
    return False


def major_topic_reason(title: str, content: str) -> str | None:
    text = compact_spaces(f"{title} {content}")
    title_text = compact_spaces(title)
    direct_supply = direct_energy_supply_reason(title, content)
    if is_summary_title(title):
        extracted = extract_oil_event_from_summary(title, content)
        if not extracted:
            return None
        text = compact_spaces(extracted)
        title_text = text
    if _has(title_text, NON_ENERGY_COMMODITY_TERMS) and not _has(title_text, OIL_GAS_TERMS):
        return None
    if _is_company_market_noise(title, content):
        return None
    if _has(title_text, MACRO_SURVEY_TERMS) and not _has(title_text, OIL_GAS_TERMS):
        return None
    if _is_macro_pass_through(title, content):
        return None
    if _is_schedule_only(title, content):
        return None
    if not direct_supply:
        return None
    if _has(text, LNG_SUPPLY_TERMS) and (
        _has(text, ["出口受阻", "紧急采购", "供应中断", "进口中断", "短缺", "停运"])
        or (_has(text, ["采购", "进口", "购买", "招标"]) and _has(text, ["紧急", "寻求", "现货", "短缺", "中断"]))
    ):
        return "命中重大主题：LNG出口中断、紧急采购或能源安全"
    if _has(text, OSP_TERMS) and _has(text, ["上调", "下调", "降低", "提高", "价格", "售价", "升水", "贴水", "折扣"]):
        return "命中重大主题：主要产油国官方售价或供应竞争"
    if _has(text, ["炼油商", "采购渠道", "现货市场采购", "长期合同", "减少对中东的依赖"]) and _has(
        text, ["原油", "石油", "炼油", "供应冲击", "中东"]
    ):
        return "命中重大主题：原油采购流向或供应冲击"
    if "霍尔木兹" in text and _has(text, HORMUZ_SHIPPING_TERMS):
        return "命中重大主题：霍尔木兹通行/护航/封锁/油轮运行风险"
    if (
        _has(text, ["伊朗", "美国", "以色列", "海湾国家", "沙特", "阿联酋", "卡塔尔"])
        and _has_middle_east_security_context(text)
        and _has_military_context(text)
    ):
        return "命中重大主题：地缘冲突影响能源运输或供应安全"
    if (
        _has(text, ["原油出口", "石油出口", "出口中断", "原油装船", "港口装船", "油港", "油码头", "OPEC", "欧佩克"])
        or (_has(text, ["制裁", "豁免", "禁运", "出口管制"]) and _has_oil_context(text))
        or (_has(text, ["产量", "减产", "增产", "配额", "库存"]) and _has_production_oil_context(text))
    ):
        return "命中重大主题：原油出口、港口装船、制裁或OPEC/OPEC+产量政策"
    return None


def market_bias_for_event(title: str, content: str, event_topic: str, pct_30m: float | None = None) -> str:
    text = compact_spaces(f"{title} {content}")
    bullish_operational_change = _has(
        text,
        [
            "暂停运营",
            "停止运营",
            "生产中断",
            "供应中断",
            "出口中断",
            "装载暂停",
            "装载仍暂停",
            "装载仍然暂停",
            "停止装载",
            "通行中断",
            "通行仍中断",
            "通行仍然中断",
            "出口受阻",
            "港口关闭",
            "港口仍关闭",
            "港口仍然关闭",
            "油田关闭",
            "油田仍关闭",
            "油田仍然关闭",
            "管道关闭",
            "管道仍关闭",
            "管道仍然关闭",
            "实施海上封锁",
            "继续海上封锁",
            "实施制裁",
            "宣布制裁",
            "制裁生效",
            "收紧制裁",
            "取消豁免",
            "减产",
            "库存下降",
            "供应担忧",
            "紧急采购",
        ],
    )
    bullish_attack = _has(text, ["遭到袭击", "遭袭", "遇袭", "发动了打击"])
    bullish_state_change = bullish_operational_change or bullish_attack
    bearish_state_change = _has(
        text,
        [
            "恢复生产",
            "生产已恢复",
            "供应已恢复",
            "恢复供应",
            "恢复装载",
            "恢复装船",
            "恢复通行",
            "通行恢复",
            "恢复正常",
            "重新开放",
            "计划增产",
            "提高产量",
            "上调产量配额",
            "库存增加",
            "制裁放松",
            "新增豁免",
            "达成停火",
            "停火协议",
            "风险下降",
            "实际供应未受影响",
        ],
    )
    denial_or_statement = (
        _has(text, ["否认", "谴责", "呼吁", "提议", "可能", "计划", "警告"])
        and _has(text, ["袭击", "封锁", "中断", "关闭", "制裁"])
    )
    confirmed_open = _has(text, ["并未被关闭", "并未关闭", "未被关闭", "航道畅通"])

    if bullish_operational_change and (bearish_state_change or confirmed_open):
        return "中性：新闻同时包含利多与利空状态变化，无法确认净影响。"
    if bearish_state_change:
        return "偏空：事件增加供应、恢复运输或降低风险溢价。"
    if denial_or_statement:
        return "中性：仅有否认、谴责、警告或提议，未确认实际供应或运输变化。"
    if confirmed_open:
        return "偏空：航道未关闭或保持畅通，供应风险溢价下降。"
    if bullish_state_change:
        return "偏多：事件可能增加供应、出口、航运或地缘风险溢价。"
    if _has(text, ["安全畅通", "停火", "缓和", "下调油价", "下调油价预期", "下调", "降低", "折扣"]) and _has(
        text, ["霍尔木兹", "油价", "原油价格", "售价", "预期"]
    ):
        return "偏空：事件可能缓解供应/航运风险，或反映官方售价竞争。"
    if "LNG" in text or "液化天然气" in text:
        return "偏多：LNG供应或采购压力可能增强区域能源安全担忧，但对Brent传导需继续观察。"
    if "官方售价/供应竞争" in event_topic:
        return "偏空：官方售价下调通常反映供应竞争或需求压力，但需结合幅度和区域价差判断。"
    return "中性：事件具备跟踪价值，但方向需要结合后续价格、实物流和官方信息确认。"
