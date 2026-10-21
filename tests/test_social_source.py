"""social_source 单测：映射/热度格式化/单平台容错/重试语义（FR-1/FR-2, NFR-2'）。全 mock 无网络。"""
from __future__ import annotations

import pytest
import requests

from news_crawler.social_source import DailyHotApiClient, PlatformResult


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None, raw: bytes | None = None):
        self.status_code = status_code
        self._json = json_data
        self._raw = raw

    def json(self):
        if self._json is not None:
            return self._json
        import json as _json

        _json.loads(self._raw or b"")  # 触发解析异常
        raise AssertionError("unreachable")


class FakeRequests:
    # 客户端在 except 子句中解析 requests.RequestException——Fake 需暴露真实异常类
    RequestException = requests.RequestException

    def __init__(self, responses: list):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(monkeypatch, responses: list, count: int = 10):
    fake = FakeRequests(responses)
    monkeypatch.setattr("news_crawler.social_source.requests", fake)
    sleeps: list[float] = []
    client = DailyHotApiClient(
        base_url="http://localhost:6688", platforms=["zhihu"], count=count, sleep=sleeps.append
    )
    return client, fake, sleeps


def _ok(data_rows: list) -> FakeResponse:
    return FakeResponse(200, {"code": 200, "name": "知乎", "data": data_rows})


def test_success_mapping_and_hot_formatting(monkeypatch):
    client, fake, _ = make_client(
        monkeypatch,
        [
            _ok(
                [
                    {"title": "话题一", "url": "https://u/1", "hot": 5234567},
                    {"title": "话题二", "mobileUrl": "https://m/2", "hot": "12.3万"},  # url 缺失回退 mobileUrl
                    {"title": "缺链接的脏数据", "hot": 1},
                ]
            )
        ],
    )
    result = client.fetch_platform("zhihu")
    assert isinstance(result, PlatformResult) and result.error is None
    assert len(result.items) == 2  # 脏数据被丢弃
    assert result.items[0].hot == "523 万热度"
    assert result.items[1].url == "https://m/2"
    assert result.items[1].hot == "12.3万"
    assert result.items[0].points == ""  # 热榜条目零 LLM
    assert fake.calls == ["http://localhost:6688/zhihu"]


def test_count_cap(monkeypatch):
    client, _, _ = make_client(
        monkeypatch, [_ok([{"title": f"t{i}", "url": f"u{i}"} for i in range(5)])], count=3
    )
    assert len(client.fetch_platform("zhihu").items) == 3


def test_empty_data_is_success_with_no_items(monkeypatch):
    client, _, _ = make_client(monkeypatch, [_ok([])])
    result = client.fetch_platform("zhihu")
    assert result.error is None and result.items == []


def test_retryable_500_then_success(monkeypatch):
    client, fake, sleeps = make_client(monkeypatch, [FakeResponse(500), _ok([{"title": "t", "url": "u"}])])
    result = client.fetch_platform("zhihu")
    assert result.error is None and len(result.items) == 1
    assert len(fake.calls) == 2
    assert sleeps == [2.0]


def test_non_retryable_404_fails_fast(monkeypatch):
    client, fake, sleeps = make_client(monkeypatch, [FakeResponse(404)])
    result = client.fetch_platform("zhihu")
    assert result.error == "HTTP 404"
    assert len(fake.calls) == 1 and sleeps == []


def test_network_error_exhausted(monkeypatch):
    client, fake, sleeps = make_client(
        monkeypatch, [requests.ConnectionError("down"), requests.ConnectionError("down")]
    )
    result = client.fetch_platform("zhihu")
    assert result.error is not None and "重试" in result.error
    assert len(fake.calls) == 2 and sleeps == [2.0]


def test_invalid_json_is_error(monkeypatch):
    client, _, _ = make_client(monkeypatch, [FakeResponse(200, raw=b"{broken")])
    result = client.fetch_platform("zhihu")
    assert result.error is not None and "JSON" in result.error


def test_fetch_all_isolates_platforms(monkeypatch):
    """单平台失败不影响其他平台（FR-2）。"""
    fake = FakeRequests([FakeResponse(404), _ok([{"title": "wb", "url": "u"}])])
    monkeypatch.setattr("news_crawler.social_source.requests", fake)
    client = DailyHotApiClient(
        base_url="http://localhost:6688", platforms=["zhihu", "weibo"], sleep=lambda s: None
    )
    results = client.fetch_all()
    assert [r.platform for r in results] == ["zhihu", "weibo"]
    assert results[0].error == "HTTP 404"
    assert results[1].error is None and results[1].items[0].title == "wb"
