"""运行编排：采集→软过滤→去重→摘要→组装 Digest→存档（FR-2/3/4/10；D-1/D-2）。"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from difflib import SequenceMatcher

from .config import APP_DIR
from .digest import Digest, DigestItem, DigestSection
from .search_client import BigModelSearchClient, DeepSeekSearchClient, SearchError
from .social_source import PLATFORM_LABELS
from .summarizer import Summarizer
from .topics import TOPIC_LABELS, build_queries

logger = logging.getLogger(__name__)

ARCHIVE_DIR = os.path.join(APP_DIR, "data", "archive")

HIGH_RERANK = 1.5  # Search API 文档：≥1.5 为高质量
HIGH_AUTHORITY = 3  # ≥3 为高权威
MIN_PREFERRED = 8  # 优质条不足此数时放宽（A-4：每主题约 8~10 条）
TITLE_SIMILARITY_THRESHOLD = 0.85  # 标题相似度去重阈值（AC-3）


class CapExceededError(Exception):
    """单日 API 调用次数超上限（NFR-3），中止流程。"""


def soft_filter(results: list[dict], cap: int) -> list[dict]:
    """D-1：优先保留 rerank≥1.5 或 authority≥3 的条目；不足 8 条则放宽，宁缺勿滥不加查。"""
    preferred = [
        item
        for item in results
        if float(item.get("rerank_score") or 0) >= HIGH_RERANK
        or float(item.get("authority") or 0) >= HIGH_AUTHORITY
    ]
    kept = preferred if len(preferred) >= MIN_PREFERRED else list(results)
    return kept[:cap]


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _similar_title_exists(title: str, kept_titles: list[str]) -> bool:
    lowered = title.lower()
    return any(
        SequenceMatcher(None, lowered, other.lower()).ratio() >= TITLE_SIMILARITY_THRESHOLD
        for other in kept_titles
    )


def _dedupe_pending(pending: list[dict]) -> None:
    """跨主题去重（FR-3）：URL 精确匹配 + 标题相似度；重复条目归属先出现的主题。"""
    seen_urls: set[str] = set()
    kept_titles: list[str] = []
    for entry in pending:
        kept = []
        for item in entry["items"]:
            url = _norm_url(item.get("url", ""))
            title = (item.get("title") or "").strip()
            if url and url in seen_urls:
                continue
            if title and _similar_title_exists(title, kept_titles):
                continue
            kept.append(item)
            if url:
                seen_urls.add(url)
            if title:
                kept_titles.append(title)
        entry["items"] = kept


def _ensure_cap(used: int, call_cap: int) -> None:
    if used + 1 > call_cap:
        raise CapExceededError(f"单日调用数将超上限 {call_cap}（已用 {used}），按 NFR-3 中止")


def _build_social_sections(social_client) -> list[DigestSection]:
    """社媒热榜板块（ADR-010）：独立 sections 追加在新闻之后。

    平台内按 URL 去重；不与新闻板块互去重（板块语义独立）；
    失败/空平台以 error 标记透传，渲染层标注「暂不可用/本日无新增」。
    """
    sections: list[DigestSection] = []
    for result in social_client.fetch_all():
        seen_urls: set[str] = set()
        items: list[DigestItem] = []
        for item in result.items:
            url = _norm_url(item.url)
            if url and url in seen_urls:
                continue
            seen_urls.add(url)
            items.append(item)
        sections.append(
            DigestSection(
                topic=result.platform,
                label=PLATFORM_LABELS.get(result.platform, result.platform),
                items=items,
                kind="social",
                error=result.error,
            )
        )
    return sections


def build_digest(
    day: date,
    search_client: DeepSeekSearchClient | BigModelSearchClient,
    summarizer: Summarizer,
    count: int,
    call_cap: int,
    social_client=None,
) -> tuple[Digest, dict]:
    """执行采集→去重→摘要全链路，返回（Digest, 各主题原始结果）。"""
    raw_by_topic: dict[str, list[dict]] = {}
    pending: list[dict] = []
    collect_topic = getattr(search_client, "collect_topic", None)  # feed 模式（REQ-20261027-1）
    for topic, label in TOPIC_LABELS.items():
        collected: list[dict] = []
        seen_urls: set[str] = set()
        if collect_topic is not None:
            _ensure_cap(search_client.successful_calls + summarizer.successful_calls, call_cap)
            try:
                hits = collect_topic(topic, count)
            except Exception as exc:  # noqa: BLE001 采集器内部已按源降级，此处兜底
                logger.warning("主题 %s feed 采集失败，降级跳过（%s）", topic, exc)
                hits = []
            for item in hits:
                url = _norm_url(item.get("url"))
                if url and url in seen_urls:
                    continue
                seen_urls.add(url)
                collected.append(item)
        else:
            for query in build_queries(topic, day):
                _ensure_cap(search_client.successful_calls + summarizer.successful_calls, call_cap)
                try:
                    hits = search_client.search(query, count=count)
                except SearchError as exc:  # 搜索服务不可用（额度/网络）→ 该主题降级为空采，热榜兜底
                    logger.warning("主题 %s 搜索失败，降级跳过（%s）", topic, exc)
                    hits = []
                for item in hits:
                    url = _norm_url(item.get("url"))
                    if url and url in seen_urls:
                        continue  # 同主题双查询的结果按 URL 合并（ADR-009）
                    seen_urls.add(url)
                    collected.append(item)
        raw_by_topic[topic] = collected
        pending.append({"topic": topic, "label": label, "items": soft_filter(collected, count)})

    _dedupe_pending(pending)

    sections: list[DigestSection] = []
    for entry in pending:
        if not entry["items"]:
            sections.append(DigestSection(topic=entry["topic"], label=entry["label"], items=[]))
            continue
        _ensure_cap(search_client.successful_calls + summarizer.successful_calls, call_cap)
        items = summarizer.summarize_topic(entry["label"], entry["items"])
        sections.append(DigestSection(topic=entry["topic"], label=entry["label"], items=items))

    if social_client is not None:
        sections.extend(_build_social_sections(social_client))  # ADR-010：社媒板块后置

    digest = Digest(
        day=day.isoformat(),
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        sections=sections,
        stats=_stats(raw_by_topic, sections, search_client.successful_calls, summarizer.successful_calls),
    )
    return digest, raw_by_topic


def _stats(raw_by_topic: dict, sections: list[DigestSection], search_calls: int, llm_calls: int) -> dict:
    news_sections = [s for s in sections if s.kind == "news"]
    social_sections = [s for s in sections if s.kind == "social"]
    per_topic = {
        section.topic: {"raw": len(raw_by_topic.get(section.topic, [])), "final": len(section.items)}
        for section in news_sections
    }
    return {
        "per_topic": per_topic,
        "raw_total": sum(v["raw"] for v in per_topic.values()),
        "final_total": sum(v["final"] for v in per_topic.values()),
        "search_calls": search_calls,
        "llm_calls": llm_calls,
        "social": {
            "per_platform": {
                s.topic: {"final": len(s.items), "error": s.error} for s in social_sections
            },
            "final_total": sum(len(s.items) for s in social_sections),
        },
    }


def write_archive(digest: Digest, raw_by_topic: dict, archive_dir: str | None = None) -> str:
    """FR-10：原始结果 + 最终摘要按日期落盘 JSON（原子写）。"""
    archive_dir = archive_dir or ARCHIVE_DIR
    os.makedirs(archive_dir, exist_ok=True)
    path = os.path.join(archive_dir, f"{digest.day}.json")
    payload = {**digest.to_dict(), "raw": raw_by_topic}
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    logger.info("存档完成：%s", path)
    return path
