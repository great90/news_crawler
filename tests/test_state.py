"""幂等状态单测（FR-6/AC-4）。"""
from __future__ import annotations

from datetime import date

from news_crawler.state import RunState


def test_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    state = RunState(path)
    assert state.last_success() is None
    assert not state.is_done(date(2026, 10, 20))

    state.mark_success(date(2026, 10, 20))

    reloaded = RunState(path)
    assert reloaded.is_done(date(2026, 10, 20))
    assert not reloaded.is_done(date(2026, 10, 21))


def test_corrupt_state_treated_as_never_succeeded(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken json", encoding="utf-8")
    assert not RunState(str(path)).is_done(date(2026, 10, 20))
