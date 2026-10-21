"""DeepSeek Search API 客户端（FR-2, FR-7）。

端点：POST {base}/v1/tools/search（Bearer 鉴权，2026-10-17 开放）。
重试策略（FR-7）：408/429/5xx 与网络错误按指数退避（1s/2s/4s）重试至多 3 次；
402 余额不足、401 鉴权失败、400 参数错误不重试直接失败。
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
QUERY_MAX_LEN = 128
COUNT_MAX = 50
BACKOFF_BASE_SECONDS = 1.0


class SearchError(Exception):
    """搜索失败基类。"""


class SearchAuthError(SearchError):
    """401：API Key 无效。"""


class SearchBalanceError(SearchError):
    """402：账户余额不足，不重试。"""


class SearchRequestError(SearchError):
    """400 等：请求本身不合法，重试无意义。"""


class DeepSeekSearchClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep=time.sleep,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep  # 测试注入
        self.successful_calls = 0  # NFR-3 单日调用计数（runner 层汇总用）

    def search(self, query: str, count: int = 10, need_content: bool = False) -> list[dict]:
        """执行一次搜索，返回结果列表（空列表表示无结果）。"""
        if not 1 <= len(query) <= QUERY_MAX_LEN:
            raise SearchRequestError(f"query 长度须为 1-{QUERY_MAX_LEN}，当前 {len(query)}")
        if not 1 <= count <= COUNT_MAX:
            raise SearchRequestError(f"count 须为 1-{COUNT_MAX}，当前 {count}")

        payload = {"query": query, "count": count, "need_content": need_content}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = requests.post(
                    f"{self._base}/v1/tools/search",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                logger.warning("搜索网络错误（第 %d 次）：%s", attempt + 1, exc)
                self._backoff(attempt)
                continue

            if response.status_code == 200:
                self.successful_calls += 1
                results = response.json().get("results", [])
                logger.info("搜索成功：%d 条结果", len(results))
                return results
            if response.status_code == 402:
                raise SearchBalanceError("DeepSeek 账户余额不足（402），请充值后重试")
            if response.status_code == 401:
                raise SearchAuthError("API Key 无效（401）")
            if response.status_code in RETRYABLE_STATUS:
                last_error = SearchError(f"HTTP {response.status_code}")
                logger.warning("搜索可重试错误（第 %d 次）：HTTP %d", attempt + 1, response.status_code)
                self._backoff(attempt)
                continue
            raise SearchRequestError(
                f"搜索请求被拒绝：HTTP {response.status_code} {response.text[:200]}"
            )

        raise SearchError(f"重试 {self._max_retries} 次后仍失败：{last_error}")

    def _backoff(self, attempt: int) -> None:
        """第 attempt 次失败后的退避；最后一次失败不等待，直接留给上层抛错。"""
        if attempt < self._max_retries:
            delay = BACKOFF_BASE_SECONDS * (2**attempt)
            logger.info("退避 %.1fs 后重试", delay)
            self._sleep(delay)
