"""摘要数据结构（S2 组装，S3 渲染消费）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class DigestItem:
    title: str
    url: str
    points: str  # 1~2 句中文要点
    sources: list[str] = field(default_factory=list)  # 同事件其他报道链接（ADR-008 聚合）
    hot: str | None = None  # 热度展示（社媒热榜条目专用，ADR-010）


@dataclass
class DigestSection:
    topic: str  # 主题 id（ai/game/game_engine/new_tool）或平台 id（zhihu/weibo/douyin）
    label: str  # 中文标签
    items: list[DigestItem] = field(default_factory=list)  # 空 = 本日无新增
    kind: str = "news"  # news | social（渲染分支，ADR-010）
    error: str | None = None  # 非空 = 该板块拉取失败（渲染「暂不可用」）


@dataclass
class Digest:
    day: str  # YYYY-MM-DD
    generated_at: str  # ISO 时间戳
    sections: list[DigestSection] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "generated_at": self.generated_at,
            "sections": [
                {
                    "topic": section.topic,
                    "label": section.label,
                    "kind": section.kind,
                    "error": section.error,
                    "items": [asdict(item) for item in section.items],
                }
                for section in self.sections
            ],
            "stats": self.stats,
        }
