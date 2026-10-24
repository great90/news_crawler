"""pusher 单测：通道调用、失败隔离、配置校验。全 mock 无网络。"""
from __future__ import annotations

import pytest
import requests

from news_crawler import pusher
from news_crawler.config import ConfigError, load


class FakeResp:
    def raise_for_status(self):
        pass


def test_bark_get(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResp()

    monkeypatch.setattr(requests, "get", fake_get)
    pusher.push_bark("https://api.day.app/K", "标题", "正文")
    assert captured["url"].startswith("https://api.day.app/K/")
    assert captured["params"]["level"] == "active"


def test_serverchan_post(monkeypatch):
    captured = {}

    def fake_post(url, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        return FakeResp()

    monkeypatch.setattr(requests, "post", fake_post)
    pusher.push_serverchan("SCTxxx", "t", "md")
    assert captured["url"] == "https://sctapi.ftqq.com/SCTxxx.send"
    assert captured["data"] == {"title": "t", "desp": "md"}


def test_notify_isolates_channel_failure(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: FakeResp())
    monkeypatch.setattr(requests, "post", lambda url, **kw: (_ for _ in ()).throw(requests.RequestException("boom")))
    ok = pusher.notify(["bark", "serverchan"], "https://api.day.app/K", "SCTxxx", "t", "b")
    assert ok == 1  # bark 成功、serverchan 失败不影响整体


def test_notify_missing_credential_never_raises():
    assert pusher.notify(["serverchan"], None, None, "t", "b") == 0


def test_config_rejects_unknown_channel(monkeypatch):
    for key, value in {"DEEPSEEK_API_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
                       "SMTP_AUTH_CODE": "c", "MAIL_TO": "m", "PUSH_CHANNELS": "wecom"}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError, match="未知通道"):
        load()


def test_config_push_defaults_off(monkeypatch):
    for key, value in {"DEEPSEEK_API_KEY": "k", "SMTP_HOST": "h", "SMTP_USER": "u",
                       "SMTP_AUTH_CODE": "c", "MAIL_TO": "m"}.items():
        monkeypatch.setenv(key, value)
    # 显式置空，避免真实 .env 里的值经 load_dotenv 泄入
    monkeypatch.setenv("PUSH_CHANNELS", "")
    monkeypatch.setenv("BARK_URL", "")
    monkeypatch.setenv("SERVERCHAN_SEND_KEY", "")
    cfg = load()
    assert cfg.push_channels == () and cfg.bark_url is None
