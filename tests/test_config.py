"""config 单测：缺失清单、默认值、数值校验（AC-6 相关逻辑）。"""
from __future__ import annotations

import pytest

from news_crawler import config as config_mod
from news_crawler.config import ConfigError, MissingConfigError, load


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """让 load() 读一个不存在的 .env，用环境变量精确控制测试输入。"""
    monkeypatch.setattr(config_mod, "ENV_PATH", str(tmp_path / ".env"))
    for key in (
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "SEARCH_PROVIDER", "SMTP_HOST", "SMTP_PORT",
        "SMTP_USER", "SMTP_AUTH_CODE", "MAIL_FROM", "MAIL_TO",
        "SEND_TIME", "TOPIC_COUNT", "DAILY_CALL_CAP",
        "SOCIAL_ENABLED", "SOCIAL_API_BASE", "SOCIAL_PLATFORMS", "SOCIAL_COUNT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_missing_full_profile_lists_all_missing():
    with pytest.raises(MissingConfigError) as exc_info:
        load(profile="full")
    assert set(exc_info.value.missing) == {
        "DEEPSEEK_API_KEY", "SMTP_HOST", "SMTP_USER", "SMTP_AUTH_CODE", "MAIL_TO",
    }


def test_missing_search_profile_reports_only_key():
    with pytest.raises(MissingConfigError) as exc_info:
        load(profile="search")
    assert exc_info.value.missing == ["DEEPSEEK_API_KEY"]


def test_search_profile_loads_with_defaults(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    config = load(profile="search")
    assert config.deepseek_api_key == "sk-test"
    assert config.deepseek_base_url == "https://api.deepseek.com"
    assert config.topic_count == 10
    assert config.daily_call_cap == 200
    assert config.smtp_port == 465


def test_invalid_topic_count_rejected(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("TOPIC_COUNT", "99")
    with pytest.raises(ConfigError, match="TOPIC_COUNT"):
        load(profile="search")


# ===== SEARCH_PROVIDER（REQ-20261026-2 搜索源切换）=====


def test_search_provider_defaults_to_websearch(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    config = load(profile="search")
    assert config.search_provider == "websearch"


def test_search_provider_invalid_rejected(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SEARCH_PROVIDER", "google")
    with pytest.raises(ConfigError, match="SEARCH_PROVIDER"):
        load(profile="search")


def test_search_provider_deepseek_accepted(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SEARCH_PROVIDER", "DeepSeek")  # 大小写不敏感
    config = load(profile="search")
    assert config.search_provider == "deepseek"


def test_full_profile_requires_smtp(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    with pytest.raises(MissingConfigError) as exc_info:
        load(profile="full")
    assert set(exc_info.value.missing) == {"SMTP_HOST", "SMTP_USER", "SMTP_AUTH_CODE", "MAIL_TO"}


# ===== SOCIAL_* 配置（REQ-20261020-2 FR-5，默认关闭向后兼容）=====


def test_social_defaults_disabled(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    config = load(profile="search")
    assert config.social_enabled is False  # 默认关闭 → 行为与 REQ-1 完全一致（AC-3）
    assert config.social_api_base == "http://localhost:6688"
    assert config.social_platforms == ("zhihu", "weibo", "douyin")
    assert config.social_count == 10


def test_social_enabled_parses(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SOCIAL_PLATFORMS", "zhihu , weibo")
    monkeypatch.setenv("SOCIAL_COUNT", "15")
    config = load(profile="search")
    assert config.social_enabled is True
    assert config.social_platforms == ("zhihu", "weibo")  # 去空格
    assert config.social_count == 15


def test_social_enabled_with_empty_platforms_rejected(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SOCIAL_PLATFORMS", " , ")
    with pytest.raises(ConfigError, match="SOCIAL_PLATFORMS"):
        load(profile="search")


def test_social_enabled_with_bad_base_rejected(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SOCIAL_ENABLED", "1")
    monkeypatch.setenv("SOCIAL_API_BASE", "ftp://localhost")
    with pytest.raises(ConfigError, match="SOCIAL_API_BASE"):
        load(profile="search")


def test_social_count_out_of_range_rejected(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SOCIAL_COUNT", "0")
    with pytest.raises(ConfigError, match="SOCIAL_COUNT"):
        load(profile="search")
