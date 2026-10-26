"""search_client 单测：重试/退避/快败语义（FR-7），全部 mock，无网络无密钥。"""
from __future__ import annotations

import pytest

from news_crawler.search_client import (
    BigModelSearchClient,
    DeepSeekSearchClient,
    SearchAuthError,
    SearchBalanceError,
    SearchError,
    SearchRequestError,
)


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self) -> dict:
        return self._json


class FakeRequests:
    """按序返回预设响应，并记录请求参数。"""

    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.responses.pop(0)


def make_client(monkeypatch, responses: list[FakeResponse]) -> tuple[DeepSeekSearchClient, FakeRequests]:
    fake = FakeRequests(responses)
    monkeypatch.setattr("news_crawler.search_client.requests", fake)
    sleeps: list[float] = []
    client = DeepSeekSearchClient(api_key="sk-test", sleep=sleeps.append)
    return client, fake


def test_success_returns_results_and_records_call(monkeypatch):
    client, fake = make_client(
        monkeypatch, [FakeResponse(200, {"results": [{"title": "t", "url": "u"}]})]
    )
    results = client.search("query", count=5)
    assert results == [{"title": "t", "url": "u"}]
    assert client.successful_calls == 1
    assert fake.calls[0]["url"].endswith("/v1/tools/search")
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert fake.calls[0]["json"] == {"query": "query", "count": 5, "need_content": False}


def test_retryable_then_success_uses_exponential_backoff(monkeypatch):
    client, fake = make_client(
        monkeypatch,
        [FakeResponse(429), FakeResponse(503), FakeResponse(200, {"results": []})],
    )
    results = client.search("query")
    assert results == []
    assert len(fake.calls) == 3
    assert client.successful_calls == 1  # 只有成功的 200 计费


def test_retries_exhausted_raises(monkeypatch):
    client, fake = make_client(
        monkeypatch, [FakeResponse(500)] * 4
    )
    with pytest.raises(SearchError):
        client.search("query")
    assert len(fake.calls) == 4  # 首次 + 3 次重试


def test_balance_error_fails_fast_without_retry(monkeypatch):
    client, fake = make_client(monkeypatch, [FakeResponse(402)])
    with pytest.raises(SearchBalanceError):
        client.search("query")
    assert len(fake.calls) == 1


def test_auth_error_fails_fast(monkeypatch):
    client, _ = make_client(monkeypatch, [FakeResponse(401)])
    with pytest.raises(SearchAuthError):
        client.search("query")


def test_bad_request_fails_fast(monkeypatch):
    client, fake = make_client(monkeypatch, [FakeResponse(400, text="bad query")])
    with pytest.raises(SearchRequestError):
        client.search("query")
    assert len(fake.calls) == 1


def test_query_length_guard(monkeypatch):
    client, fake = make_client(monkeypatch, [])
    with pytest.raises(SearchRequestError, match="128"):
        client.search("x" * 129)
    assert fake.calls == []


def test_count_guard(monkeypatch):
    client, fake = make_client(monkeypatch, [])
    with pytest.raises(SearchRequestError, match="count"):
        client.search("query", count=51)
    assert fake.calls == []


# ===== BigModelSearchClient（REQ-20261026-2 搜索源替换）=====


def make_websearch_client(monkeypatch, responses: list[FakeResponse], **kwargs):
    fake = FakeRequests(responses)
    monkeypatch.setattr("news_crawler.search_client.requests", fake)
    sleeps: list[float] = []
    client = BigModelSearchClient(api_key="sk-test", sleep=sleeps.append, **kwargs)
    return client, fake, sleeps


def test_websearch_success_maps_fields(monkeypatch):
    client, fake, _ = make_websearch_client(
        monkeypatch,
        [FakeResponse(200, {"search_result": [
            {"title": " 标题 ", "link": "http://a", "content": "内容"},
            {"title": "t2", "url": "http://b", "snippet": "s2"},
        ]})],
    )
    results = client.search("AI LLM release", count=8)
    assert results == [
        {"title": "标题", "url": "http://a", "summary": "内容"},
        {"title": "t2", "url": "http://b", "summary": "s2"},
    ]
    assert client.successful_calls == 1
    assert fake.calls[0]["url"].endswith("/web_search")
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    payload = fake.calls[0]["json"]
    assert payload["search_engine"] == "search_std"
    assert payload["search_query"] == "AI LLM release"
    assert payload["count"] == 8
    assert payload["search_recency"] == "oneWeek"  # 通用查询带时间偏好
    assert "search_domain_filter" not in payload


def test_websearch_site_query_uses_domain_filter(monkeypatch):
    client, fake, _ = make_websearch_client(monkeypatch, [FakeResponse(200, {"search_result": []})])
    results = client.search("site:mp.weixin.qq.com (AI OR LLM) lang:en", count=8)
    assert results == []
    payload = fake.calls[0]["json"]
    assert payload["search_query"] == "(AI OR LLM) lang:en"  # site: 词剥除
    assert payload["search_domain_filter"] == ["mp.weixin.qq.com"]
    assert "search_recency" not in payload  # 域过滤查询不带时间偏好


def test_websearch_auth_error_fails_fast(monkeypatch):
    client, fake, _ = make_websearch_client(monkeypatch, [FakeResponse(401)])
    with pytest.raises(SearchAuthError):
        client.search("query")
    assert len(fake.calls) == 1


def test_websearch_arrears_429_fails_fast_without_retry(monkeypatch):
    # 429/1113 是欠费不是限流，重试无意义（真机实证：2026-10-26 免费额度耗尽后全量 429）
    body = '{"error":{"code":"1113","message":"余额不足或无可用资源包,请充值。"}}'
    client, fake, _ = make_websearch_client(monkeypatch, [FakeResponse(429, text=body)])
    with pytest.raises(SearchBalanceError):
        client.search("query")
    assert len(fake.calls) == 1


def test_websearch_retryable_then_success_backoff(monkeypatch):
    client, fake, sleeps = make_websearch_client(
        monkeypatch, [FakeResponse(429), FakeResponse(200, {"search_result": []})]
    )
    assert client.search("query") == []
    assert len(fake.calls) == 2
    assert sleeps == [1.0]
    assert client.successful_calls == 1


def test_websearch_count_guard(monkeypatch):
    client, fake, _ = make_websearch_client(monkeypatch, [])
    with pytest.raises(SearchRequestError, match="count"):
        client.search("query", count=51)
    assert fake.calls == []
