"""配置加载与校验（FR-8, AC-6）。

所有配置来自应用根目录的 .env（不入库，模板见 .env.example）；
缺项时抛 MissingConfigError 并携带完整缺失清单，由 CLI 打印后以非零码退出。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 应用根目录（news_crawler/ 包的上一级）
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(APP_DIR, ".env")

SEARCH_REQUIRED = ("DEEPSEEK_API_KEY",)
FULL_REQUIRED = SEARCH_REQUIRED + ("SMTP_HOST", "SMTP_USER", "SMTP_AUTH_CODE", "MAIL_TO")


class ConfigError(Exception):
    """配置错误基类。"""


class MissingConfigError(ConfigError):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("缺少配置项: " + ", ".join(missing))


@dataclass(frozen=True)
class Config:
    deepseek_api_key: str
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_auth_code: str | None = None
    mail_from: str | None = None
    mail_to: str | None = None
    send_time: str = "08:00"
    topic_count: int = 10
    daily_call_cap: int = 200
    social_enabled: bool = False
    social_api_base: str = "http://localhost:6688"
    social_platforms: tuple[str, ...] = ("zhihu", "weibo", "douyin")
    social_count: int = 10
    push_channels: tuple[str, ...] = ()
    bark_url: str | None = None
    serverchan_send_key: str | None = None

    @property
    def mail_sender(self) -> str:
        """发件人地址：优先 MAIL_FROM，其次 SMTP_USER。"""
        return self.mail_from or self.smtp_user or ""


def load(profile: str = "full") -> Config:
    """加载配置。profile='search' 仅要求搜索相关项；'full' 为完整运行所需。"""
    load_dotenv(ENV_PATH)
    required = SEARCH_REQUIRED if profile == "search" else FULL_REQUIRED
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise MissingConfigError(missing)

    def get(key: str, default: str | None = None) -> str | None:
        value = os.getenv(key)
        return value if value else default

    try:
        config = Config(
            deepseek_api_key=os.environ["DEEPSEEK_API_KEY"].strip(),
            deepseek_base_url=(get("DEEPSEEK_BASE_URL", "https://api.deepseek.com") or "").rstrip("/"),
            llm_model=(get("LLM_MODEL", "deepseek-flash") or "").strip(),
            smtp_host=get("SMTP_HOST"),
            smtp_port=int(get("SMTP_PORT", "465")),
            smtp_user=get("SMTP_USER"),
            smtp_auth_code=get("SMTP_AUTH_CODE"),
            mail_from=get("MAIL_FROM"),
            mail_to=get("MAIL_TO"),
            send_time=get("SEND_TIME", "08:00"),
            topic_count=int(get("TOPIC_COUNT", "10")),
            daily_call_cap=int(get("DAILY_CALL_CAP", "200")),
            social_enabled=get("SOCIAL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"),
            social_api_base=(get("SOCIAL_API_BASE", "http://localhost:6688") or "").rstrip("/"),
            social_platforms=tuple(
                p.strip() for p in (get("SOCIAL_PLATFORMS", "zhihu,weibo,douyin") or "").split(",") if p.strip()
            ),
            social_count=int(get("SOCIAL_COUNT", "10")),
            push_channels=tuple(
                p.strip() for p in (get("PUSH_CHANNELS", "") or "").split(",") if p.strip()
            ),
            bark_url=get("BARK_URL"),
            serverchan_send_key=get("SERVERCHAN_SEND_KEY"),
        )
    except ValueError as exc:
        raise ConfigError(f"数值型配置不合法: {exc}") from exc

    if not 1 <= config.topic_count <= 50:
        raise ConfigError("TOPIC_COUNT 取值范围为 1-50（Search API 上限）")
    if config.daily_call_cap < 1:
        raise ConfigError("DAILY_CALL_CAP 至少为 1")
    if config.social_enabled:
        if not config.social_platforms:
            raise ConfigError("SOCIAL_ENABLED 已启用但 SOCIAL_PLATFORMS 为空")
        if not config.social_api_base.startswith(("http://", "https://")):
            raise ConfigError("SOCIAL_API_BASE 须以 http:// 或 https:// 开头")
        if not 1 <= config.social_count <= 50:
            raise ConfigError("SOCIAL_COUNT 取值范围为 1-50")
    unknown_channels = [c for c in config.push_channels if c not in ("bark", "serverchan")]
    if unknown_channels:
        raise ConfigError(f"PUSH_CHANNELS 含未知通道（支持 bark,serverchan）: {unknown_channels}")
    return config
