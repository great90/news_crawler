"""搜索客户端（FR-2, FR-7）。

DeepSeekSearchClient：DeepSeek Search API，POST {base}/v1/tools/search（Bearer 鉴权）。
BigModelSearchClient：BigModel web_search，DeepSeek 额度耗尽后的默认搜索源（REQ-20261026-2，
2026-10-26 探测实证：std 引擎不支持 site: 语法，含 site: 的查询剥出域名走 search_domain_filter）。
重试策略（FR-7）：408/429/5xx 与网络错误按指数退避（1s/2s/4s）重试至多 3 次；
401 鉴权失败、400 参数错误不重试直接失败。
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


WEB_SEARCH_DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"


def _extract_site_domains(query: str) -> tuple[list[str], str]:
    """抽出 query 中的 site:域 词，返回（域名列表, 剥除后的查询）。"""
    domains: list[str] = []
    tokens: list[str] = []
    for token in query.split():
        if token.lower().startswith("site:"):
            domain = token[len("site:"):].strip()
            if domain:
                domains.append(domain)
        else:
            tokens.append(token)
    return domains, " ".join(tokens).strip()


def _normalize_web_search_item(item: dict) -> dict:
    """web_search 条目 → DeepSeek Search 形态（title/url/summary），下游零改动。"""
    return {
        "title": (item.get("title") or "").strip(),
        "url": (item.get("link") or item.get("url") or "").strip(),
        "summary": (item.get("content") or item.get("snippet") or "").strip(),
    }


class BigModelSearchClient:
    """BigModel web_search 客户端（REQ-20261026-2 验证后的默认搜索源）。

    通用查询带 search_recency=oneWeek；含 site: 的查询剥出域名走 search_domain_filter
    （std 引擎不支持 site: 语法，探测 0 条；域过滤实测 9/9 命中公众号文章）。
    successful_calls 与 DeepSeekSearchClient 同语义，runner 的调用上限逻辑不变。
    """

    _QUERY_MAX_LEN = 256  # web_search 无公开长度上限，剥 site: 后原查询富余

    def __init__(
        self,
        api_key: str,
        base_url: str = WEB_SEARCH_DEFAULT_BASE,
        engine: str = "search_std",
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep=time.sleep,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._engine = engine
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep  # 测试注入
        self.successful_calls = 0  # NFR-3 单日调用计数（runner 层汇总用）

    def search(self, query: str, count: int = 10) -> list[dict]:
        """执行一次搜索，返回 [{title, url, summary}]（空列表表示无结果）。"""
        if not 1 <= len(query) <= self._QUERY_MAX_LEN:
            raise SearchRequestError(f"query 长度须为 1-{self._QUERY_MAX_LEN}，当前 {len(query)}")
        if not 1 <= count <= COUNT_MAX:
            raise SearchRequestError(f"count 须为 1-{COUNT_MAX}，当前 {count}")

        domain_filter, cleaned = _extract_site_domains(query)
        payload: dict = {"search_engine": self._engine, "search_query": cleaned, "count": count}
        if domain_filter:
            payload["search_domain_filter"] = domain_filter
        else:
            payload["search_recency"] = "oneWeek"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = requests.post(
                    f"{self._base}/web_search",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                logger.warning("web_search 网络错误（第 %d 次）：%s", attempt + 1, exc)
                self._backoff(attempt)
                continue

            if response.status_code == 200:
                self.successful_calls += 1
                results = [
                    _normalize_web_search_item(item)
                    for item in (response.json().get("search_result") or [])
                ]
                logger.info("web_search 成功：%d 条结果", len(results))
                return results
            if response.status_code == 401:
                raise SearchAuthError("BigModel API Key 无效（401）")
            if response.status_code == 429 and ("1113" in response.text or "余额不足" in response.text):
                # 429 有两种：真限流（可退避重试）与欠费/无资源包（重试无意义，快败）
                raise SearchBalanceError(
                    "BigModel web_search 余额不足或无可用资源包（429/1113），请充值后重试"
                )
            if response.status_code in RETRYABLE_STATUS:
                last_error = SearchError(f"HTTP {response.status_code}")
                logger.warning("web_search 可重试错误（第 %d 次）：HTTP %d", attempt + 1, response.status_code)
                self._backoff(attempt)
                continue
            raise SearchRequestError(
                f"web_search 请求被拒绝：HTTP {response.status_code} {response.text[:200]}"
            )

        raise SearchError(f"重试 {self._max_retries} 次后仍失败：{last_error}")

    def _backoff(self, attempt: int) -> None:
        """第 attempt 次失败后的退避；最后一次失败不等待，直接留给上层抛错。"""
        if attempt < self._max_retries:
            delay = BACKOFF_BASE_SECONDS * (2**attempt)
            logger.info("退避 %.1fs 后重试", delay)
            self._sleep(delay)
