"""四主题定义与查询模板（A-6 英文信源；A-13 开发者向新工具；ADR-009 多查询 + 24h 窗口；FR-7 公众号变体）。

每主题 3 组查询：2 组通用（宽泛 + 垂直变体）+ 1 组公众号站内变体（site:mp.weixin.qq.com，
随新闻管线一起去重与 LLM 摘要）；runner 按 URL 合并各组结果后软过滤；
时间窗用 `after:<昨日>`（UTC）保证晨报有约 24 小时的内容可供精选。
硬约束（E8 实测）：查询词含中文字符会让 DeepSearch 返回 0 结果，build_queries 强制纯 ASCII；
总长受 API 1-128 字符限制。
"""
from __future__ import annotations

from datetime import date, timedelta

TOPIC_LABELS: dict[str, str] = {
    "ai": "AI",
    "game": "游戏",
    "game_engine": "游戏引擎",
    "new_tool": "新工具",
}

TOPIC_QUERIES: dict[str, list[str]] = {
    "ai": [
        '(AI OR LLM OR "artificial intelligence") (release OR launch OR announcement)',
        '(GPT OR Claude OR Gemini OR "open source model") (release OR benchmark OR update)',
        'site:mp.weixin.qq.com (AI OR LLM OR "artificial intelligence")',
    ],
    "game": [
        "(video game OR gaming) (release OR announcement OR update)",
        '(game studio OR "game publisher") (announce OR reveal OR launch)',
        "site:mp.weixin.qq.com (video game OR gaming)",
    ],
    "game_engine": [
        '("game engine" OR Unity OR Unreal OR Godot) (release OR update)',
        '("game engine" OR "game dev tools") (news OR "new version" OR release)',
        'site:mp.weixin.qq.com (Unity OR Unreal OR "game engine")',
    ],
    "new_tool": [
        '(framework OR library OR "dev tool") (launch OR release OR "new version")',
        '(developer tool OR CLI OR "AI tool" OR "AI product") (launch OR announce)',
        'site:mp.weixin.qq.com (developer tool OR "AI tool" OR framework)',
    ],
}

QUERY_MAX_LEN = 128  # Search API 限制
WINDOW_DAYS = 1  # ADR-009：覆盖过去约 24 小时
WEIXIN_SITE = "site:mp.weixin.qq.com"  # FR-7 公众号站内变体前缀

# E8/冒烟实测：mp.weixin 索引页普遍缺日期，带 after: 窗口会全杀——公众号变体豁免时间窗
WINDOW_EXEMPT_PREFIXES = (WEIXIN_SITE,)


def build_queries(topic: str, day: date, lang: str = "en") -> list[str]:
    """生成某主题某日的全部搜索查询词（窗口起点 = day - WINDOW_DAYS）。"""
    if topic not in TOPIC_QUERIES:
        raise KeyError(f"未知主题: {topic}")
    window_start = (day - timedelta(days=WINDOW_DAYS)).isoformat()
    queries = []
    for base in TOPIC_QUERIES[topic]:
        if base.startswith(WINDOW_EXEMPT_PREFIXES):
            queries.append(f"{base} lang:{lang}")
        else:
            queries.append(f"{base} after:{window_start} lang:{lang}")
    for query in queries:
        if not 1 <= len(query) <= QUERY_MAX_LEN:
            raise ValueError(f"查询词长度 {len(query)} 超出 1-{QUERY_MAX_LEN}：{query}")
        if not query.isascii():
            raise ValueError(f"查询词含非 ASCII 字符会导致 DeepSearch 返回 0 结果（E8 实测），须纯英文：{query}")
    return queries
