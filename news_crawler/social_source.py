"""社媒热榜信源（FR-1/FR-2；ADR-010）。

经本地自托管的 DailyHotApi 拉取平台实时热榜（如 http://localhost:6688/zhihu）。
单平台失败/空结果不抛出：返回带 error 标记的 PlatformResult，由组装层在
邮件中标注「暂不可用/本日无新增」，保证单点故障不阻断整封邮件（A-8）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

from .digest import DigestItem

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # NFR-2'：失败/可重试状态码单次重试
BACKOFF_SECONDS = 2.0
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

PLATFORM_LABELS = {
    "zhihu": "知乎热榜",
    "weibo": "微博热搜",
    "douyin": "抖音热点",
}


@dataclass
class PlatformResult:
    platform: str
    items: list[DigestItem] = field(default_factory=list)
    error: str | None = None  # None=成功；否则为失败原因（渲染「暂不可用」）


def _format_hot(raw) -> str | None:
    """热度值统一为展示字符串：纯数字按「万」缩写，其余原样。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value >= 10000:
            return f"{value / 10000:.0f} 万热度"
        return f"{value:.0f} 热度"
    return str(raw)


class DailyHotApiClient:
    def __init__(
        self,
        base_url: str,
        platforms: list[str] | tuple[str, ...],
        count: int = 10,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        sleep=time.sleep,
        http_get=None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._platforms = list(platforms)
        self._count = count
        self._timeout = timeout
        self._sleep = sleep
        self._get = http_get or self._default_get
        self.successful_calls = 0

    def _default_get(self, url: str):
        return requests.get(url, timeout=self._timeout)

    def fetch_all(self) -> list[PlatformResult]:
        """拉取全部启用平台；任一平台失败不影响其他平台。"""
        return [self.fetch_platform(platform) for platform in self._platforms]

    def fetch_platform(self, platform: str) -> PlatformResult:
        url = f"{self._base}/{platform}"
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self._get(url)
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                logger.warning("社媒热榜网络错误 %s（第 %d 次）：%s", platform, attempt + 1, exc)
                self._backoff(attempt)
                continue
            if response.status_code == 200:
                self.successful_calls += 1
                return self._parse(platform, response)
            if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS - 1:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                logger.warning("社媒热榜可重试错误 %s（第 %d 次）：HTTP %d", platform, attempt + 1, response.status_code)
                self._backoff(attempt)
                continue
            return PlatformResult(platform, error=f"HTTP {response.status_code}")
        return PlatformResult(platform, error=f"重试后仍失败：{last_error}")

    def _parse(self, platform: str, response) -> PlatformResult:
        try:
            payload = response.json()
            rows = payload.get("data") or []
        except ValueError as exc:
            logger.warning("社媒热榜响应解析失败 %s：%s", platform, exc)
            return PlatformResult(platform, error=f"响应不是合法 JSON：{exc}")
        items: list[DigestItem] = []
        for row in rows[: self._count]:
            title = (row.get("title") or "").strip()
            url = (row.get("url") or row.get("mobileUrl") or "").strip()
            if not title or not url:
                continue  # 缺标题或链接的脏数据直接丢弃
            items.append(DigestItem(title=title, url=url, points="", hot=_format_hot(row.get("hot"))))
        return PlatformResult(platform, items)

    def _backoff(self, attempt: int) -> None:
        if attempt < MAX_ATTEMPTS - 1:
            self._sleep(BACKOFF_SECONDS)
