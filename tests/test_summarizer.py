"""summarizer 单测：JSON 解析与修复重试、退避重试、快败、合并回退（FR-4, FR-7）。"""
from __future__ import annotations

import pytest

from news_crawler.summarizer import SummarizeBalanceError, SummarizeError, Summarizer


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self) -> dict:
        return self._json


class FakeRequests:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def chat_response(content: str) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def make_summarizer(monkeypatch, responses: list[FakeResponse], model: str = "deepseek-flash"):
    fake = FakeRequests(responses)
    monkeypatch.setattr("news_crawler.summarizer.requests", fake)
    sleeps: list[float] = []
    summarizer = Summarizer(api_key="sk-test", model=model, sleep=sleeps.append)
    return summarizer, fake, sleeps


ITEMS = [{"title": "T1", "url": "u1", "summary": "sum1"}, {"title": "T2", "url": "u2", "summary": "sum2"}]


def test_parses_fenced_json_and_merges(monkeypatch):
    content = '```json\n[{"title": "标题一", "url": "u1", "points": "要点一"}, {"title": "标题二", "url": "u2", "points": "要点二"}]\n```'
    summarizer, fake, _ = make_summarizer(monkeypatch, [chat_response(content)])
    items = summarizer.summarize_topic("AI", ITEMS)
    assert [i.points for i in items] == ["要点一", "要点二"]
    assert items[0].title == "标题一"
    assert fake.calls[0]["url"].endswith("/chat/completions")
    assert fake.calls[0]["json"]["model"] == "deepseek-flash"  # 默认模型
    assert summarizer.successful_calls == 1


def test_model_param_reaches_payload(monkeypatch):
    summarizer, fake, _ = make_summarizer(
        monkeypatch, [chat_response('[{"title": "一", "url": "u1", "points": "p"}]')],
        model="glm-5.3-flash")
    summarizer.summarize_topic("AI", ITEMS)
    assert fake.calls[0]["json"]["model"] == "glm-5.3-flash"  # 模型可切换


def test_repairs_once_then_raises_on_unparseable(monkeypatch):
    summarizer, fake, _ = make_summarizer(monkeypatch, [chat_response("抱歉我不会"), chat_response("还是不会")])
    with pytest.raises(SummarizeError, match="JSON"):
        summarizer.summarize_topic("AI", ITEMS)
    assert len(fake.calls) == 2  # 原始 + 一次修复重试


def test_merged_entries_absorb_sources(monkeypatch):
    """ADR-008：同事件聚合——输入 2 条，LLM 合并为 1 条 + sources。"""
    content = '[{"title": "Mercury 3 发布", "url": "u1", "points": "综合要点", "sources": ["u2"]}]'
    summarizer, _, _ = make_summarizer(monkeypatch, [chat_response(content)])
    items = summarizer.summarize_topic("AI", ITEMS)
    assert len(items) == 1
    assert items[0].url == "u1"
    assert items[0].points == "综合要点"
    assert items[0].sources == ["u2"]


def test_omitted_item_falls_back_to_own_entry(monkeypatch):
    """LLM 遗漏某条输入时兜底自成一条，绝不丢内容。"""
    content = '[{"title": "T1", "url": "u1", "points": "p1"}]'  # u2 未出现在输出中
    summarizer, _, _ = make_summarizer(monkeypatch, [chat_response(content)])
    items = summarizer.summarize_topic("AI", ITEMS)
    assert len(items) == 2
    assert items[1].title == "T2"
    assert items[1].points == "sum2"  # 回退到原始摘要
    assert items[1].sources == []


def test_retryable_then_success(monkeypatch):
    summarizer, fake, sleeps = make_summarizer(
        monkeypatch, [FakeResponse(429), FakeResponse(503), chat_response('[{"title":"a","url":"u1","points":"p1"},{"title":"b","url":"u2","points":"p2"}]')]
    )
    items = summarizer.summarize_topic("游戏", ITEMS)
    assert len(items) == 2
    assert len(fake.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_balance_error_fails_fast(monkeypatch):
    summarizer, fake, _ = make_summarizer(monkeypatch, [FakeResponse(402)])
    with pytest.raises(SummarizeBalanceError):
        summarizer.summarize_topic("AI", ITEMS)
    assert len(fake.calls) == 1


def test_empty_items_skips_call(monkeypatch):
    summarizer, fake, _ = make_summarizer(monkeypatch, [])
    assert summarizer.summarize_topic("AI", []) == []
    assert fake.calls == []


def test_merge_falls_back_to_raw_summary_when_points_empty(monkeypatch):
    content = '[{"title": "T1", "url": "u1", "points": ""}, {"title": "T2", "url": "u2", "points": "好的要点"}]'
    summarizer, _, _ = make_summarizer(monkeypatch, [chat_response(content)])
    items = summarizer.summarize_topic("AI", ITEMS)
    assert items[0].points == "sum1"  # 回退到原始摘要
    assert items[1].points == "好的要点"
