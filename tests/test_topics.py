"""topics 单测：三组查询结构、公众号变体、ASCII/CJK 守卫（FR-7, E8）。"""
from __future__ import annotations

from datetime import date

import pytest

from news_crawler.topics import TOPIC_LABELS, TOPIC_QUERIES, build_queries

DAY = date(2026, 10, 20)


def test_every_topic_has_three_queries_with_weixin_variant():
    for topic in TOPIC_LABELS:
        queries = build_queries(topic, DAY)
        assert len(queries) == 3
        weixin = [q for q in queries if q.startswith("site:mp.weixin.qq.com")]
        assert len(weixin) == 1  # FR-7：每主题一组公众号站内变体
        assert "after:" not in weixin[0]  # 公众号豁免 24h 窗口（索引缺日期，带窗全杀）
        assert "lang:en" in weixin[0]
        non_weixin = [q for q in queries if not q.startswith("site:mp.weixin.qq.com")]
        assert all("after:2026-10-19" in q for q in non_weixin)  # 其余保持 24h 窗口


def test_all_queries_ascii_and_within_length():
    for topic in TOPIC_LABELS:
        for query in build_queries(topic, DAY):
            assert len(query) <= 128
            assert query.isascii(), query  # E8：含 CJK 即 0 结果


def test_cjk_template_rejected_by_guard():
    """把含中文的模板塞进配置，守卫必须在构造时报错（E8 教训固化为代码）。"""
    TOPIC_QUERIES["hack"] = ["site:mp.weixin.qq.com 中文关键词"]
    try:
        with pytest.raises(ValueError, match="ASCII"):
            build_queries("hack", DAY)
    finally:
        del TOPIC_QUERIES["hack"]


def test_unknown_topic_rejected():
    with pytest.raises(KeyError):
        build_queries("nope", DAY)
