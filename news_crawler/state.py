"""幂等状态（FR-6，AC-4）：data/state.json 记录最近成功发送日期，同日重复触发不重发。"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

from .config import APP_DIR

STATE_PATH = os.path.join(APP_DIR, "data", "state.json")


class RunState:
    def __init__(self, path: str | None = None) -> None:
        self._path = path or STATE_PATH

    def last_success(self) -> date | None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                raw = json.load(handle).get("last_success_date")
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return date.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    def is_done(self, day: date) -> bool:
        return self.last_success() == day

    def mark_success(self, day: date) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        payload = {
            "last_success_date": day.isoformat(),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self._path)
