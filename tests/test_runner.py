"""runner 单测：软过滤、跨主题去重、全链路组装与存档、调用上限（FR-3/10, NFR-3, D-1）。"""
from __future__ import annotations

import json
from datetime import date

import pytest

from news_crawler.digest import DigestItem
from news_crawler.runner import CapExceededError, build_digest, soft_filter, write_archive
from news_crawler.social_source import PlatformResult

DAY = date(2026, 10, 20)


def result(title: str, url: str, score: float = 2.0, authority: int = 3) -> dict:
    return {
        "title": title,
        "url": url,
        "rerank_score": score,
        "authority": authority,
        "summary": "raw summary",
        "snippet": "raw snippet",
    }


class FakeSearch:
    """按查询词包含的关键词返回预设结果。"""

    def __init__(self, by_keyword: dict[str, list[dict]]):
        self.by_keyword = by_keyword
        self.successful_calls = 0

    def search(self, query: str, count: int = 10, need_content: bool = False) -> list[dict]:
        self.successful_calls += 1
        for keyword, items in self.by_keyword.items():
            if keyword in query:
                return items[:count]
        return []


class FakeSummarizer:
    def __init__(self):
        self.successful_calls = 0
        self.labels: list[str] = []

    def summarize_topic(self, label: str, items: list[dict]) -> list[DigestItem]:
        self.successful_calls += 1
        self.labels.append(label)
        return [DigestItem(title=item["title"], url=item["url"], points=f"{label}要点") for item in items]


def test_soft_filter_prefers_high_quality():
    results = [result(f"high{i}", f"u{i}") for i in range(9)] + [
        result(f"low{i}", f"l{i}", score=0.3, authority=1) for i in range(5)
    ]
    kept = soft_filter(results, cap=10)
    assert len(kept) == 9
    assert all(item["rerank_score"] >= 1.5 or item["authority"] >= 3 for item in kept)


def test_soft_filter_relaxes_when_preferred_insufficient():
    results = [result(f"high{i}", f"u{i}") for i in range(3)] + [
        result(f"low{i}", f"l{i}", score=0.3, authority=1) for i in range(9)
    ]
    kept = soft_filter(results, cap=10)
    assert len(kept) == 10  # 优质仅 3 < 8 → 放宽取全部（截断到 cap）


def test_build_digest_full_flow_and_archive(tmp_path):
    by_keyword = {
        "AI tool": [],  # 注意：必须排在 "AI" 之前，避免 "AI" 子串误匹配新工具的第二组查询
        "AI": [
            result("DeepSeek launches search API", "https://a/1"),
            result("Another AI item", "https://a/1"),  # 同 URL → 合并
        ],
        "GPT": [result("New open model drops", "https://a/2")],  # AI 第二组查询
        "gaming": [result("Game News B", "https://b/1")],
        "game studio": [],
        "Godot": [],  # 空主题 → 本日无新增
        "game dev": [],
        "dev tool": [result("DeepSeek launches search API", "https://x/9", score=0.4, authority=1)],  # 标题相同 → 跨主题去重
    }
    digest, raw = build_digest(DAY, FakeSearch(by_keyword), FakeSummarizer(), count=10, call_cap=100)

    assert [s.label for s in digest.sections] == ["AI", "游戏", "游戏引擎", "新工具"]
    assert digest.sections[2].items == []  # 空主题标记
    assert digest.sections[3].items == []  # 标题与 AI 节相同 → 跨主题去重后清空
    assert len(digest.sections[0].items) == 2  # 双查询按 URL 合并后：主链接条 + 第二组查询条
    assert digest.stats["raw_total"] == 4
    assert digest.stats["final_total"] == 3
    assert digest.stats["llm_calls"] == 2  # 空主题与被去重清空的主题都不调 LLM（去重先于摘要，省调用费）

    path = write_archive(digest, raw, archive_dir=str(tmp_path))
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    assert data["day"] == "2026-10-20"
    assert len(data["raw"]["ai"]) == 2  # 原始结果按 URL 合并后保留
    assert data["sections"][0]["items"][0]["points"] == "AI要点"


def test_title_similarity_dedupe_threshold():
    by_keyword = {
        "AI": [result("OpenAI releases GPT-6", "u1")],
        "gaming": [result("openai releases gpt-6!", "u2")],
    }
    digest, _ = build_digest(DAY, FakeSearch(by_keyword), FakeSummarizer(), count=10, call_cap=100)
    assert len(digest.sections[0].items) == 1
    assert len(digest.sections[1].items) == 0  # 相似标题跨主题去重


def test_call_cap_gates_search(monkeypatch):
    by_keyword = {"AI": [result("A", "u1")]}
    # 三查询结构下 12 次搜索/天（4 主题 × 3 组）；cap=2 时第 3 次查询前触发超限
    with pytest.raises(CapExceededError):
        build_digest(DAY, FakeSearch(by_keyword), FakeSummarizer(), count=10, call_cap=2)


def test_call_cap_gates_summarizer():
    by_keyword = {
        "AI": [result("A", "u1")],
        "gaming": [result("B", "u2")],
        "Godot": [result("C", "u3")],
        "dev tool": [result("D", "u4")],
    }
    # 12 次搜索（4 主题 × 3 组查询）后，cap=12 时第一次 LLM 调用前触发超限
    with pytest.raises(CapExceededError):
        build_digest(DAY, FakeSearch(by_keyword), FakeSummarizer(), count=10, call_cap=12)


# ===== 社媒板块装配（REQ-20261020-2 FR-2/3/4, ADR-010）=====


class FakeSocial:
    def __init__(self, results: list[PlatformResult]):
        self.results = results
        self.fetched = False

    def fetch_all(self):
        self.fetched = True
        return self.results


def test_social_sections_appended_after_news(tmp_path):
    news = {"AI tool": [], "AI": [result("A1", "u1")], "GPT": [], "gaming": [], "game studio": [], "Godot": [], "game dev": [], "dev tool": []}
    social = FakeSocial(
        [
            PlatformResult("zhihu", [DigestItem("热榜一", "https://z/1", "", hot="523 万热度"), DigestItem("热榜一重复", "https://z/1", "", hot="x")]),
            PlatformResult("weibo", [], error="HTTP 500"),
        ]
    )
    digest, _ = build_digest(DAY, FakeSearch(news), FakeSummarizer(), count=10, call_cap=100, social_client=social)

    assert [s.kind for s in digest.sections] == ["news"] * 4 + ["social"] * 2  # 社媒后置
    assert digest.sections[0].items[0].points == "AI要点"
    zhihu, weibo = digest.sections[4], digest.sections[5]
    assert zhihu.label == "知乎热榜" and zhihu.error is None
    assert len(zhihu.items) == 1 and zhihu.items[0].hot == "523 万热度"  # 平台内 URL 去重
    assert weibo.label == "微博热搜" and weibo.error == "HTTP 500" and weibo.items == []
    assert digest.stats["social"]["final_total"] == 1
    assert digest.stats["social"]["per_platform"]["weibo"]["error"] == "HTTP 500"
    assert digest.stats["llm_calls"] == 1  # AC-5：社媒板块零 LLM


def test_no_social_client_keeps_legacy_behavior():
    news = {"AI tool": [], "AI": [result("A1", "u1")], "GPT": [], "gaming": [], "game studio": [], "Godot": [], "game dev": [], "dev tool": []}
    digest, _ = build_digest(DAY, FakeSearch(news), FakeSummarizer(), count=10, call_cap=100, social_client=None)
    assert all(s.kind == "news" for s in digest.sections)  # AC-3：关闭时行为与 REQ-1 一致
    assert digest.stats["social"]["final_total"] == 0
