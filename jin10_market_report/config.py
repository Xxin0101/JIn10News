from __future__ import annotations

from dataclasses import dataclass


JIN10_HOME_URL = "https://www.jin10.com/"
JIN10_FLASH_API = "https://flash-api.jin10.com/get_flash_list"
JIN10_FLASH_DETAIL = "https://flash.jin10.com/detail/{id}"
JIN10_XNEWS_DETAIL = "https://xnews.jin10.com/details/{id}"
YAHOO_CHART_API = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.jin10.com",
    "Referer": "https://www.jin10.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "x-app-id": "bVBF4FyRTn5NJF5n",
    "x-version": "1.0.0",
}


EVENT_CATEGORIES = (
    "地缘政治",
    "战争/安全",
    "制裁/政策",
    "能源供应",
    "航运物流",
    "贸易流向",
    "价格及市场影响",
)


KEYWORD_GROUPS = {
    "地缘政治": [
        "伊朗",
        "德黑兰",
        "中东",
        "以色列",
        "美国",
        "美伊",
        "俄罗斯",
        "欧洲",
        "欧盟",
        "沙特",
        "阿联酋",
        "卡塔尔",
        "伊拉克",
        "叙利亚",
        "黎巴嫩",
        "也门",
        "胡塞",
        "巴勒斯坦",
        "加沙",
        "哈马斯",
        "真主党",
        "海湾",
        "波斯湾",
        "核设施",
        "国际原子能机构",
        "IAEA",
        "格罗西",
    ],
    "战争/安全": [
        "战争",
        "冲突",
        "袭击",
        "空袭",
        "导弹",
        "无人机",
        "军事行动",
        "爆炸",
        "开火",
        "停火",
        "报复",
        "防空",
        "军方",
        "武装",
        "安全风险",
        "紧张局势",
        "核查",
        "核设施",
    ],
    "制裁/政策": [
        "制裁",
        "禁运",
        "关税",
        "出口管制",
        "政策",
        "协议",
        "谈判",
        "豁免",
        "限制",
        "外交",
        "核协议",
        "临时和平协议",
        "谅解备忘录",
    ],
    "能源供应": [
        "石油",
        "原油",
        "布伦特",
        "WTI",
        "油价",
        "产量",
        "供应",
        "能源",
        "LPG",
        "液化石油气",
        "LNG",
        "液化天然气",
        "天然气",
        "凝析油",
        "炼厂",
        "油田",
        "油轮",
        "出口",
        "进口",
        "库存",
        "OPEC",
        "欧佩克",
    ],
    "航运物流": [
        "霍尔木兹",
        "霍尔木兹海峡",
        "红海",
        "苏伊士",
        "苏伊士运河",
        "巴拿马运河",
        "航运",
        "船舶",
        "油轮",
        "LNG船",
        "LPG船",
        "运费",
        "航线",
        "船期",
        "港口",
        "装船",
        "绕航",
        "通航",
        "封锁",
        "海峡",
        "码头",
        "物流",
        "保险费",
        "海运",
    ],
    "贸易流向": [
        "贸易",
        "贸易流向",
        "出口",
        "进口",
        "采购",
        "供应链",
        "买家",
        "卖家",
        "装货",
        "卸货",
        "合同",
        "现货",
        "长协",
        "亚洲",
        "欧洲",
        "中国",
        "印度",
        "韩国",
        "日本",
    ],
    "价格及市场影响": [
        "价格",
        "市场",
        "涨",
        "跌",
        "波动",
        "风险溢价",
        "通胀",
        "期货",
        "现货",
        "升水",
        "贴水",
        "价差",
        "供应风险",
        "避险",
        "黄金",
        "美元",
        "收益率",
    ],
}


CORE_GROUPS = {
    "地缘政治",
    "战争/安全",
    "制裁/政策",
    "能源供应",
    "航运物流",
    "贸易流向",
}


MIDDLE_EAST_ANCHORS = [
    "伊朗",
    "德黑兰",
    "中东",
    "以色列",
    "美伊",
    "霍尔木兹",
    "红海",
    "苏伊士",
    "海湾",
    "波斯湾",
    "沙特",
    "阿联酋",
    "卡塔尔",
    "伊拉克",
    "叙利亚",
    "黎巴嫩",
    "也门",
    "胡塞",
    "巴勒斯坦",
    "加沙",
    "哈马斯",
    "真主党",
    "核设施",
    "国际原子能机构",
    "IAEA",
]


ENERGY_ANCHORS = [
    "石油",
    "原油",
    "油价",
    "布伦特",
    "WTI",
    "LPG",
    "液化石油气",
    "LNG",
    "液化天然气",
    "天然气",
    "能源供应",
    "油轮",
    "炼厂",
    "油田",
    "OPEC",
    "欧佩克",
]


SHIPPING_ANCHORS = [
    "霍尔木兹",
    "红海",
    "苏伊士",
    "巴拿马运河",
    "航运",
    "船舶",
    "油轮",
    "LNG船",
    "LPG船",
    "运费",
    "航线",
    "船期",
    "港口",
    "装船",
    "绕航",
    "通航",
    "封锁",
    "海峡",
    "海运",
]


SECURITY_POLICY_ANCHORS = [
    "战争",
    "冲突",
    "袭击",
    "空袭",
    "导弹",
    "无人机",
    "军事行动",
    "爆炸",
    "停火",
    "制裁",
    "禁运",
    "出口管制",
    "核协议",
    "谈判",
    "协议",
]


TRADE_ANCHORS = [
    "贸易",
    "贸易流向",
    "出口",
    "进口",
    "采购",
    "供应链",
    "买家",
    "卖家",
    "装货",
    "卸货",
    "合同",
    "现货",
    "长协",
]


HIGH_PRIORITY_KEYWORDS = [
    "突发",
    "袭击",
    "空袭",
    "导弹",
    "无人机",
    "爆炸",
    "战争",
    "停火",
    "封锁",
    "关闭",
    "霍尔木兹",
    "红海",
    "苏伊士",
    "油轮",
    "LNG船",
    "LPG船",
    "制裁",
    "核设施",
    "军事行动",
]


@dataclass(frozen=True)
class FetchConfig:
    hours: int = 24
    timezone: str = "Asia/Shanghai"
    channel: int = -8200
    vip: int = 1
    max_pages: int = 120
    request_timeout: int = 20
    pause_seconds: float = 0.5
    retries: int = 3
    retry_backoff_seconds: float = 1.5


@dataclass(frozen=True)
class FilterConfig:
    minimum_score: int = 4
    similarity_threshold: float = 0.88


@dataclass(frozen=True)
class BrentConfig:
    symbol: str = "BZ=F"
    interval: str = "5m"
    source: str = "yahoo"
    first_run_range: str = "5d"
    request_timeout: int = 20
    retries: int = 3
    retry_backoff_seconds: float = 1.5
    # Re-fetch enough completed bars to replace a prior live, non-grid candle.
    incremental_overlap_minutes: int = 30
    # Treat data as delayed if the newest kline is older than this many minutes.
    stale_after_minutes: int = 20


@dataclass(frozen=True)
class TrackerConfig:
    database_path: str = "data/jin10_tracker.sqlite"
    output_dir: str = "outputs/tracker"
    log_dir: str = "logs"
    timezone: str = "Asia/Shanghai"
    poll_interval_seconds: int = 300
    reaction_threshold: float = 0.005
    medium_reaction_threshold: float = 0.0025
    reaction_window_minutes: int = 30
    extended_reaction_window_minutes: int = 60
    kline_minutes: int = 5
    max_price_match_lag_minutes: int = 10
    first_run_lookback_hours: int = 24
    incremental_overlap_minutes: int = 10
