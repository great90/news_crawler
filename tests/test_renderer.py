"""渲染单测（FR-5）：分节、空主题标注、autoescape。"""
from __future__ import annotations

from news_crawler.digest import Digest, DigestItem, DigestSection
from news_crawler.renderer import render_digest_html


def test_render_contains_sections_and_empty_marker():
    digest = Digest(
        day="2026-10-20",
        generated_at="2026-10-20T12:00:00+08:00",
        sections=[
            DigestSection(
                "ai",
                "AI",
                [DigestItem("Model released", "https://a.example/1", "要点一", sources=["https://a.example/2"])],
            ),
            DigestSection("game", "游戏", []),
        ],
        stats={"raw_total": 1, "final_total": 1, "search_calls": 4, "llm_calls": 1},
    )
    html = render_digest_html(digest)
    assert "2026-10-20" in html
    assert "【AI】" in html or "AI" in html
    assert "本日无新增" in html
    assert "要点一" in html
    assert 'href="https://a.example/1"' in html
    assert "相关报道" in html  # ADR-008 聚合来源
    assert 'href="https://a.example/2"' in html
    assert "max-width:860px" in html  # ADR-008 加宽


def test_render_escapes_html_in_content():
    digest = Digest(
        day="2026-10-20",
        generated_at="t",
        sections=[DigestSection("ai", "AI", [DigestItem("<script>x</script>", "https://a", "<b>粗</b>")])],
        stats={},
    )
    html = render_digest_html(digest)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>粗</b>" not in html


def test_render_social_section_with_hot_and_error():
    """ADR-010：社媒板块渲染——热度徽章、失败「暂不可用」。"""
    digest = Digest(
        day="2026-10-20",
        generated_at="t",
        sections=[
            DigestSection("zhihu", "知乎热榜", [DigestItem("热榜话题", "https://z/1", "", hot="523 万热度")], kind="social"),
            DigestSection("weibo", "微博热搜", [], kind="social", error="HTTP 500"),
        ],
        stats={"social": {"final_total": 1}},
    )
    html = render_digest_html(digest)
    assert "知乎热榜" in html and "1 条" in html
    assert "🔥 523 万热度" in html  # 热度徽章
    assert "暂不可用 ⚠" in html and "拉取失败：HTTP 500" in html  # 失败降级
    assert "https://z/1" in html
