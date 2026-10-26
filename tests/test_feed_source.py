"""feed_source 单测（REQ-20261027-1）：RSS 解析/关键词过滤/单源降级/runner 分发，全 mock 无网络。"""
from __future__ import annotations

from datetime import date

import pytest

from news_crawler.digest import DigestItem
from news_crawler.feed_source import FeedSearchClient, parse_rss
from news_crawler.runner import CapExceededError, build_digest

DAY = date(2026, 10, 27)

SAMPLE_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>量子位</title>
<item><title>OpenAI 发布新模型</title><link>https://a/1</link>
<description>&lt;p&gt;推理能力提升，&lt;b&gt;价格减半&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>缺链接的脏数据</title><description>x</description></item>
<item><title>另一条 AI 新闻</title><link>https://a/2</link><description>agent 框架开源</description></item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def make_client(responses_by_url: dict[str, object], fail_urls: set[str] = frozenset()):
    """按 URL 精确匹配的假 http_get；未命中 URL 抛错（模拟意外请求）。"""

    def http_get(url: str):
        if url in fail_urls:
            raise RuntimeError("boom")
        response = responses_by_url.get(url)
        if response is None:
            raise AssertionError(f"意外请求：{url}")
        return response

    client = FeedSearchClient(dailyhot_base="http://localhost:6688", http_get=http_get)
    return client


def _hot(items: list[dict]) -> FakeResponse:
    return FakeResponse(200, json_data={"data": items})


def test_parse_rss_maps_items_strips_html_and_drops_dirty():
    items = parse_rss(SAMPLE_RSS)
    assert len(items) == 2  # 缺链接条目被丢弃
    assert items[0]["title"] == "OpenAI 发布新模型"
    assert items[0]["url"] == "https://a/1"
    assert items[0]["summary"] == "推理能力提升，价格减半"  # HTML 剥离
    assert items[1]["summary"] == "agent 框架开源"


def test_collect_topic_keyword_filter_and_dedupe():
    client = make_client({
        "http://localhost:6688/36kr": _hot([
            {"title": "OpenAI 发布 GPT 新模型", "url": "https://a/1"},
            {"title": "某球队夺得冠军", "url": "https://sport/1"},  # 关键词不命中 → 过滤
            {"title": "重复条目", "url": "https://a/1"},  # 同 URL → 去重
            {"title": "AI 数据库发布新版本", "desc": "一个开源框架", "url": "https://a/3"},  # desc 入 summary 并参与匹配
        ]),
        "https://www.qbitai.com/feed": FakeResponse(200, text=SAMPLE_RSS),  # 主题专属源 → 全收
    })
    merged = client.collect_topic("ai", count=10)
    urls = [i["url"] for i in merged]
    assert urls == ["https://a/1", "https://a/2", "https://a/3"]  # 过滤体育条目、去重
    assert merged[2]["summary"] == "一个开源框架"
    assert client.successful_calls == 2


def test_collect_topic_degrades_when_one_source_fails():
    client = make_client({
        "http://localhost:6688/github": _hot([
            {"title": "godot 编辑器插件合集", "url": "https://g/1"},
            {"title": "unreal 性能分析工具", "url": "https://g/2"},
        ]),
        "http://localhost:6688/ithome": _hot([{"title": "Unity 7 发布", "url": "https://i/1"}]),
    }, fail_urls={"https://hnrss.org/frontpage"})
    merged = client.collect_topic("game_engine", count=10)
    assert [i["url"] for i in merged] == ["https://g/1", "https://g/2", "https://i/1"]  # hnrss 失败仅降级该源
    assert client.successful_calls == 2  # github + ithome 成功，hnrss 不计


def test_collect_topic_unknown_topic_raises():
    client = make_client({})
    with pytest.raises(KeyError):
        client.collect_topic("nope", count=10)


# ===== runner 分发（鸭子类型：有 collect_topic 即走 feed 路径）=====


class _Summ:
    successful_calls = 0

    def summarize_topic(self, label, items):
        return [DigestItem(title=i["title"], url=i["url"], points="要点") for i in items]


def test_runner_dispatches_to_collect_topic():
    class FakeFeed:
        successful_calls = 4

        def collect_topic(self, topic, count):
            if topic == "ai":
                return [
                    {"title": "AI 新闻一", "url": "https://a/1", "summary": "s"},
                    {"title": "AI 新闻二", "url": "https://a/2", "summary": "s"},
                ]
            return []

    digest, raw = build_digest(DAY, FakeFeed(), _Summ(), count=10, call_cap=100)
    assert [s.label for s in digest.sections] == ["AI", "游戏", "游戏引擎", "新工具"]
    assert len(digest.sections[0].items) == 2
    assert digest.sections[1].items == []
    assert len(raw["ai"]) == 2


def test_runner_feed_cap_counts_sources():
    class FakeFeed:
        successful_calls = 99

        def collect_topic(self, topic, count):
            return [{"title": "t", "url": "https://a/1", "summary": ""}]

    with pytest.raises(CapExceededError):
        build_digest(DAY, FakeFeed(), _Summ(), count=10, call_cap=10)  # 99 > 10 → 触发上限闸
