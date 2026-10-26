"""LLM 中文摘要（FR-4；决策 D-2 批量摘要）。

每主题一次调用 deepseek-flash，输入为 Search API 自带的 summary 字段（模型友好），
要求严格输出 JSON 数组；解析失败自动带修复提示重试一次，仍失败抛 SummarizeError
（按 FR-7 语义整体失败并告警，不降级发送英文原摘要）。
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from .digest import DigestItem

logger = logging.getLogger(__name__)

MODEL = "deepseek-flash"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
BACKOFF_BASE_SECONDS = 1.0
SUMMARY_INPUT_MAX_CHARS = 600

SYSTEM_PROMPT = "你是严谨的中文科技资讯编辑。只输出合法 JSON，不输出任何其他文字或解释。"


class SummarizeError(Exception):
    """摘要失败基类。"""


class SummarizeBalanceError(SummarizeError):
    """402：账户余额不足，不重试。"""


class Summarizer:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = MODEL,
        timeout: float = 60.0,
        max_retries: int = 3,
        sleep=time.sleep,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep
        self.successful_calls = 0

    def summarize_topic(self, label: str, items: list[dict]) -> list[DigestItem]:
        """对某主题的去重后条目做批量摘要，返回带中文要点的 DigestItem 列表。"""
        if not items:
            return []
        prompt = self._build_prompt(label, items)
        content = self._chat(prompt)
        parsed = self._parse(content)
        if parsed is None:
            logger.warning("LLM 输出不可解析，带修复提示重试一次")
            content = self._chat(prompt + "\n注意：上一次输出不是合法 JSON 数组。请重新输出，严格只输出 JSON 数组本身。")
            parsed = self._parse(content)
        if parsed is None:
            raise SummarizeError(f"LLM 未返回可解析的 JSON（{label}，{len(items)} 条）")
        return self._merge(items, parsed)

    def _build_prompt(self, label: str, items: list[dict]) -> str:
        lines = []
        for index, item in enumerate(items, 1):
            summary = (item.get("summary") or item.get("snippet") or "").strip().replace("\n", " ")
            lines.append(f"{index}. 标题: {item.get('title', '')}\n   链接: {item.get('url', '')}\n   原始摘要: {summary[:SUMMARY_INPUT_MAX_CHARS]}")
        joined = "\n".join(lines)
        return (
            f"以下是「{label}」主题今日的 {len(items)} 条资讯（编号、标题、链接、原始摘要）。\n"
            f"请为每条生成中文要点：标题保留原文（可轻度清理），要点 1~2 句中文，说清"
            f"“发生了什么、为什么值得注意”。\n"
            f"多条资讯报道同一事件时合并为一条：title 概括事件、points 综合各源信息，"
            f"url 取最权威或最直接的一条，其余来源链接原样放入该条目的 sources 数组。\n"
            f"严格输出 JSON 数组（合并后长度可小于 {len(items)}），每项形如 "
            f'{{"title": "...", "url": "...", "points": "...", "sources": ["其他来源url"]}}，'
            f"url 与 sources 均原样照抄，绝不杜撰链接。\n\n{joined}"
        )

    def _chat(self, prompt: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = requests.post(
                    f"{self._base}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                logger.warning("摘要网络错误（第 %d 次）：%s", attempt + 1, exc)
                self._backoff(attempt)
                continue

            if response.status_code == 200:
                self.successful_calls += 1
                return response.json()["choices"][0]["message"]["content"]
            if response.status_code == 402:
                raise SummarizeBalanceError("DeepSeek 账户余额不足（402），请充值后重试")
            if response.status_code == 401:
                raise SummarizeError("API Key 无效（401）")
            if response.status_code in RETRYABLE_STATUS:
                last_error = SummarizeError(f"HTTP {response.status_code}")
                logger.warning("摘要可重试错误（第 %d 次）：HTTP %d", attempt + 1, response.status_code)
                self._backoff(attempt)
                continue
            raise SummarizeError(f"摘要请求被拒绝：HTTP {response.status_code} {response.text[:200]}")

        raise SummarizeError(f"重试 {self._max_retries} 次后仍失败：{last_error}")

    @staticmethod
    def _parse(content: str) -> list[dict] | None:
        """解析 LLM 输出为 JSON 数组；结构不合法返回 None（触发修复重试）。

        合并同事件后长度可小于输入条数，因此不校验长度，只校验结构。
        """
        if not content:
            return None
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("["), text.rfind("]")
            if start != -1 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    return None
        if not isinstance(data, list) or not data:
            return None
        if not all(isinstance(x, dict) and x.get("url") and "points" in x for x in data):
            return None
        return data

    @staticmethod
    def _merge(raw_items: list[dict], parsed: list[dict]) -> list[DigestItem]:
        """按 url 对齐解析结果（ADR-008 同事件聚合）：

        - 出现在某条目 sources 中的原始条目视为已被合并，不单列；
        - LLM 遗漏的条目兜底自成一条（缺要点回退原始摘要前 80 字），绝不丢内容。
        """
        primary_by_url = {entry.get("url"): entry for entry in parsed}
        absorbed = {url for entry in parsed for url in (entry.get("sources") or [])}
        merged = []
        for item in raw_items:
            url = item.get("url", "")
            fallback = (item.get("summary") or item.get("snippet") or "").strip().replace("\n", " ")
            fallback = fallback[:80] + ("…" if len(fallback) > 80 else "")
            entry = primary_by_url.get(url)
            if entry is None:
                if url in absorbed:
                    continue  # 已并入其他条目的"相关报道"
                merged.append(DigestItem(title=(item.get("title") or "").strip(), url=url, points=fallback))
                continue
            points = (entry.get("points") or "").strip() or fallback
            sources = [u for u in (entry.get("sources") or []) if u and u != url]
            merged.append(
                DigestItem(
                    title=(entry.get("title") or item.get("title") or "").strip(),
                    url=url,
                    points=points,
                    sources=sources,
                )
            )
        return merged

    def _backoff(self, attempt: int) -> None:
        if attempt < self._max_retries:
            delay = BACKOFF_BASE_SECONDS * (2**attempt)
            logger.info("退避 %.1fs 后重试", delay)
            self._sleep(delay)
