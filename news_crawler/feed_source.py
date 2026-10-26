"""免费源采集器（REQ-20261027-1，SEARCH_PROVIDER=feed）。

以"订阅源 + 关键词过滤"替代搜索：本地 DailyHotApi 路由（36kr/ithome/github/sspai，
零部署）+ 原生 RSS（量子位/Hacker News）。单源失败仅降级该源不阻断主题（沿用 A-8 语义）。
feed 模式无 site: 查询，公众号板块自然不采集（用户 2026-10-27 确认先不管）。
源矩阵与关键词见 TOPIC_SOURCES（2026-10-27 真机探测后冻结于 spec-feed-mode.md）。
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15.0
FETCH_PER_SOURCE = 40  # 每源过滤前最多拉取条数
SUMMARY_MAX_CHARS = 500

_KW_AI = (
    "ai", "人工智能", "大模型", "llm", "gpt", "claude", "gemini", "openai", "anthropic",
    "deepseek", "智能体", "agent", "机器学习", "深度学习", "推理模型", "多模态", "copilot",
)
_KW_GAME = (
    "游戏", "game", "steam", "gdc", "手游", "主机", "电竞", "发行商", "工作室",
    "playstation", "xbox", "nintendo", "任天堂", "switch", "端游", "网游",
)
_KW_ENGINE = (
    "unity", "unreal", "虚幻", "godot", "游戏引擎", "game engine", "cryengine",
    "gamemaker", "defold", "bevy", "物理引擎", "渲染引擎",
)
_KW_TOOL = (
    "工具", "tool", "framework", "框架", "开源", "open source", "cli", "library",
    "库", "sdk", "api", "ide", "编辑器", "editor", "终端", "terminal", "效率",
    "release", "发布", "新版", "beta", "开发者", "developer", "编程", "代码",
)

# 主题 → 源清单：(kind, name, keywords)；keywords=None 表示主题专属源整站收录不过滤
TOPIC_SOURCES: dict[str, list[tuple[str, str, tuple[str, ...] | None]]] = {
    "ai": [
        ("rss", "https://www.qbitai.com/feed", None),
        ("hot", "36kr", _KW_AI),
        ("hot", "ithome", _KW_AI),
    ],
    "game": [
        ("hot", "ithome", _KW_GAME),
        ("hot", "36kr", _KW_GAME),
    ],
    "game_engine": [
        ("hot", "github", _KW_ENGINE),  # 带 desc 后可关键词匹配；全收会让新工具被跨主题去重清空
        ("hot", "ithome", _KW_ENGINE),
        ("rss", "https://hnrss.org/frontpage", _KW_ENGINE),
    ],
    "new_tool": [
        ("hot", "github", None),
        ("hot", "sspai", _KW_TOOL),
        ("rss", "https://hnrss.org/frontpage", _KW_TOOL),
        ("hot", "36kr", _KW_TOOL),
    ],
}


_CJK_ADJACENT_SPACE = re.compile(r"(?<=[\u4e00-\u9fff\u3000-\u303f，。！？、；：])\s+(?=[\u4e00-\u9fff\u3000-\u303f，。！？、；：])")


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()
    return _CJK_ADJACENT_SPACE.sub("", cleaned)  # 标签占位空格在中文之间无意义


def _matches(item: dict, keywords: tuple[str, ...]) -> bool:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    return any(k.lower() in text for k in keywords)


def parse_rss(xml_text: str) -> list[dict]:
    """解析 RSS 2.0/Atom 兼容的 <item> 条目为 {title, url, summary}。"""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"RSS 解析失败：{exc}") from exc
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        if not title or not link:
            continue  # 缺标题或链接的脏数据直接丢弃
        summary = _strip_html(node.findtext("description") or "")[:SUMMARY_MAX_CHARS]
        items.append({"title": title, "url": link, "summary": summary})
    return items


class FeedSearchClient:
    """免费源采集客户端；提供 collect_topic(topic, count)，鸭子类型接入 runner。"""

    def __init__(
        self,
        dailyhot_base: str,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        sleep=time.sleep,
        http_get=None,
    ) -> None:
        self._base = dailyhot_base.rstrip("/")
        self._timeout = timeout
        self._sleep = sleep
        self._get = http_get or self._default_get
        self.successful_calls = 0  # 成功源数（沿用 runner 调用上限闸口径）

    def _default_get(self, url: str):
        return requests.get(url, timeout=self._timeout, headers={"User-Agent": "Mozilla/5.0"})

    def collect_topic(self, topic: str, count: int = 10) -> list[dict]:
        """采集某主题：多源合并 → URL 去重 → 关键词过滤；单源失败降级。"""
        sources = TOPIC_SOURCES.get(topic)
        if sources is None:
            raise KeyError(f"未知主题: {topic}")
        merged: list[dict] = []
        seen: set[str] = set()
        for kind, name, keywords in sources:
            try:
                items = self._fetch_source(kind, name)
                self.successful_calls += 1
            except Exception as exc:  # noqa: BLE001 单源失败不阻断主题（A-8）
                logger.warning("feed 源 %s 拉取失败，降级跳过：%s", name, exc)
                continue
            for item in items:
                url = item.get("url") or ""
                if not url or url in seen:
                    continue
                if keywords and not _matches(item, keywords):
                    continue
                seen.add(url)
                merged.append(item)
        return merged[: max(count * 3, 15)]  # 多留余量给 runner 去重/精选

    def _fetch_source(self, kind: str, name: str) -> list[dict]:
        url = f"{self._base}/{name}" if kind == "hot" else name
        response = self._get(url)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        if kind == "hot":
            rows = (response.json().get("data") or [])[:FETCH_PER_SOURCE]
            items = []
            for row in rows:
                title = (row.get("title") or "").strip()
                link = (row.get("url") or row.get("mobileUrl") or "").strip()
                if not title or not link:
                    continue
                summary = (row.get("desc") or row.get("description") or "").strip()
                items.append({"title": title, "url": link, "summary": summary[:SUMMARY_MAX_CHARS]})
            return items
        return parse_rss(response.text)[:FETCH_PER_SOURCE]
